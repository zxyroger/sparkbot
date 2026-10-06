"""长期记忆：跨会话保留的事实，带持久化与检索。

与 ``memory.py`` 的区别（两者互补，不是替代）：
    * ``memory.py``（短期）：本轮对话的原始消息，有顺序、会成对驱逐、重启即丢。
      它回答的是"刚才我们聊到哪了"。
    * 本模块（长期）：从对话中沉淀下来的**结论**，无序、按相关度召回、落盘保存。
      它回答的是"关于这个人/这台机器人，我已知什么"。

为什么不做向量检索
------------------
    桌面机器人只需要记住"主人叫什么""喜欢喝美式""怕吵"这类少量事实，
    而不是做语义搜索。引入 embeddings 会带来模型下载、CPU 占用和首字延迟，
    收益远小于成本。这里用**词元重合度 + 时间衰减**排序：
    * 中文按**二元组（bigram）**切分 —— 不依赖分词库，且对中文足够有效；
    * 英文/数字按单词切分；
    * 再叠加重要性、使用频次与时间衰减。

存储格式
--------
    JSONL，一行一条事实。选 JSONL 而不是单个 JSON 数组，是因为**追加即可**：
    记一条新事实不需要重写整个文件，进程被杀也不会留下半个数组而全部丢失。
    删除/更新用"重写文件"实现（事实总量很小，代价可忽略）。
"""

from __future__ import annotations

import json
import logging
import math
import re
import threading
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable

logger = logging.getLogger(__name__)

#: 单条事实的长度上限。太长的"事实"其实是对话内容，不该进长期记忆。
MAX_CONTENT_LEN = 240

#: CJK 字符范围（用于切二元组）。
_CJK = re.compile(r"[\u4e00-\u9fff\u3400-\u4dbf]")
#: 英文单词与数字。
_WORD = re.compile(r"[a-zA-Z][a-zA-Z0-9_'-]*|\d+")
#: 纯空白。
_SPACE = re.compile(r"\s+")

#: 检索时忽略的高频词（中英混合）。它们几乎出现在每句话里，
#: 参与打分只会引入噪声。
_STOPWORDS = frozenset(
    """
    的 了 是 在 我 你 他 她 它 们 有 和 与 就 都 而 及 或 一个 什么 怎么
    吗 呢 吧 啊 这 那 这个 那个 不 也 还 要 会 能 可以 请 帮 把 给 对
    a an the is are was were be been being of to in on at for with and or
    i you he she it we they me my your what how why when where do does did
    """.split()
)


# --------------------------------------------------------------------------- #
# 词元化
# --------------------------------------------------------------------------- #
def tokenize(text: str) -> list[str]:
    """把文本切成语元列表。

    中文同时产出**单字与二元组**：
        ``"喜欢喝美式"`` → ``["喜","喜喜"…]`` 实际为
        ``["喜","欢","喝","美","式","喜欢","欢喝","喝美","美式"]``

    为什么要带单字：只切二元组时，语义相关但用词不同的句子可能**完全不
    共享词元**。实测"他叫什么名字"与"主人叫张伟"就没有共同二元组
    （前者有「名字」「他叫」，后者只有「主人」「叫张」），于是查名字
    查不到 —— 而这是 Agent 里最高频的查询之一。带上单字后，两者共享
    「叫」，配合加权打分就能召回。

    单字权重低于二元组（见 :func:`token_weights`），所以它只作为
    补充信号，不会把匹配质量拉低。
    """
    if not text:
        return []

    lowered = text.lower()
    tokens: list[str] = []

    # 英文与数字
    for m in _WORD.finditer(lowered):
        tokens.append(m.group(0))

    # 中文：连续 CJK 段内做单字 + 二元组
    for run in re.findall(r"[\u4e00-\u9fff\u3400-\u4dbf]+", lowered):
        for i, ch in enumerate(run):
            if ch not in _STOPWORDS:
                tokens.append(ch)
        for i in range(len(run) - 1):
            bigram = run[i : i + 2]
            if bigram not in _STOPWORDS:
                tokens.append(bigram)

    # 过滤停用词与无意义的单字符英文
    out: list[str] = []
    for t in tokens:
        if t in _STOPWORDS:
            continue
        if len(t) == 1 and not t.isdigit() and not _CJK.match(t):
            continue
        out.append(t)
    return out


def token_weights(tokens: set[str]) -> dict[str, float]:
    """给词元分配权重。

    依据是**区分度**：越长的词元（二元组、英文单词）越具体，权重越高；
    单个汉字太常见（"的""是"之外仍有很多通用字），只作为弱信号。

    这样"共享一个二元组"（例如「咖啡」）会明显强于"共享一个单字"，
    符合直觉，也让排序更稳。
    """
    weights: dict[str, float] = {}
    for t in tokens:
        if len(t) >= 2 and _CJK.match(t):
            weights[t] = 2.0      # 中文二元组
        elif len(t) >= 2:
            weights[t] = 2.5      # 英文单词/数字，区分度最高
        elif t.isdigit():
            weights[t] = 1.5
        else:
            weights[t] = 0.6      # 单个汉字：弱信号
    return weights


@dataclass(slots=True)
class Fact:
    """一条长期记忆。"""

    content: str
    id: str = field(default_factory=lambda: uuid.uuid4().hex[:12])
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)
    #: 1~5，越大越重要。检索排序时参与加权。
    importance: int = 3
    #: 被召回（以及被更新）的次数，用来体现"这件事常被提到"。
    hits: int = 0
    source: str = ""
    """来源标记，例如 ``user`` / ``agent`` / ``tool``，便于排查谁写进来的。"""

    def to_dict(self) -> dict[str, Any]:
        """序列化。"""
        return {
            "id": self.id,
            "content": self.content,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "importance": self.importance,
            "hits": self.hits,
            "source": self.source,
        }

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "Fact | None":
        """反序列化；字段坏掉时返回 None 而不是抛异常。

        长期记忆是落盘数据，可能被手工编辑或被旧版本写入过。
        **一条坏记录不该让整个记忆库无法加载**，所以这里容错。
        """
        content = str(raw.get("content") or "").strip()
        if not content:
            return None
        try:
            importance = int(raw.get("importance", 3))
        except (TypeError, ValueError):
            importance = 3
        return cls(
            content=content[:MAX_CONTENT_LEN],
            id=str(raw.get("id") or uuid.uuid4().hex[:12]),
            created_at=float(raw.get("created_at") or time.time()),
            updated_at=float(raw.get("updated_at") or time.time()),
            importance=max(1, min(5, importance)),
            hits=max(0, int(raw.get("hits") or 0)),
            source=str(raw.get("source") or ""),
        )

    def age_days(self, *, now: float | None = None) -> float:
        """距最后更新的天数。"""
        return max(0.0, ((now or time.time()) - self.updated_at) / 86400.0)


# --------------------------------------------------------------------------- #
# 记忆库
# --------------------------------------------------------------------------- #
class LongTermMemory:
    """落盘的长期事实库，进程内线程安全。

    并发模型：所有读写都在 ``_lock`` 下进行。写盘是"先改内存再重写文件"，
    因此即使写盘失败，进程内的记忆仍然是新的（只是没落盘），
    调用方可以通过 ``persist()`` 的返回值知道写盘是否成功。
    """

    def __init__(
        self,
        path: str | Path,
        *,
        capacity: int = 500,
        enabled: bool = True,
    ) -> None:
        self.path = Path(path)
        self.capacity = max(10, capacity)
        self.enabled = enabled
        self._lock = threading.RLock()
        self._facts: dict[str, Fact] = {}
        self._dirty = False
        #: 落盘失败次数，供状态接口暴露（写不进去时用户应当知道）
        self.write_errors = 0
        if enabled:
            self.load()

    # ------------------------------------------------------------------ #
    # 持久化
    # ------------------------------------------------------------------ #
    def load(self) -> int:
        """从磁盘加载。返回加载到的事实数。

        文件不存在很正常（第一次运行），不算错误。
        """
        with self._lock:
            self._facts.clear()
            if not self.path.is_file():
                return 0

            bad = 0
            try:
                with self.path.open("r", encoding="utf-8") as fh:
                    for line in fh:
                        line = line.strip()
                        if not line:
                            continue
                        try:
                            raw = json.loads(line)
                        except json.JSONDecodeError:
                            bad += 1
                            continue
                        if not isinstance(raw, dict):
                            bad += 1
                            continue
                        fact = Fact.from_dict(raw)
                        if fact is None:
                            bad += 1
                            continue
                        self._facts[fact.id] = fact
            except OSError as exc:
                logger.warning("长期记忆读取失败 %s: %s", self.path, exc)
                return 0

            if bad:
                logger.warning("长期记忆有 %d 行无法解析，已跳过（其余正常加载）", bad)
            logger.info("长期记忆已加载: %d 条（%s）", len(self._facts), self.path)
            return len(self._facts)

    def persist(self) -> bool:
        """把内存中的事实原子地写回磁盘。

        先写临时文件再 ``replace``：这样即使写到一半进程被杀，
        原来的记忆文件仍然完好 —— 不会出现"半个文件、全部丢失"。
        """
        if not self.enabled:
            return False
        with self._lock:
            if not self._dirty:
                return True
            try:
                self.path.parent.mkdir(parents=True, exist_ok=True)
                tmp = self.path.with_suffix(self.path.suffix + ".tmp")
                with tmp.open("w", encoding="utf-8") as fh:
                    for fact in self._facts.values():
                        fh.write(json.dumps(fact.to_dict(), ensure_ascii=False) + "\n")
                tmp.replace(self.path)
                self._dirty = False
                return True
            except OSError as exc:
                self.write_errors += 1
                logger.warning("长期记忆写盘失败: %s", exc)
                return False

    # ------------------------------------------------------------------ #
    # 写入
    # ------------------------------------------------------------------ #
    def remember(
        self,
        content: str,
        *,
        importance: int = 3,
        source: str = "agent",
    ) -> Fact | None:
        """记住一条事实。

        如果内容与已有事实**高度重复**（词元几乎相同），就更新那一条
        （刷新时间、抬升 importance、hits+1）而不是新增 —— 否则用户反复
        说同一件事会把记忆塞满重复项，挤掉真正有用的信息。

        Returns:
            写入的 Fact；内容为空或未启用时返回 None。
        """
        cleaned = _SPACE.sub(" ", (content or "").strip())[:MAX_CONTENT_LEN]
        if not cleaned:
            return None
        if not self.enabled:
            return None

        importance = max(1, min(5, int(importance)))
        tokens = set(tokenize(cleaned))

        with self._lock:
            dup = self._find_duplicate(cleaned, tokens)
            if dup is not None:
                dup.updated_at = time.time()
                dup.hits += 1
                # 再次提到说明更重要；但不越过用户显式给的上限 5
                dup.importance = min(5, max(dup.importance, importance) + (1 if dup.hits % 3 == 0 else 0))
                # 保留信息量更大的表述；相同时优先保留模型写的（更像完整句子，
                # 通常带主语，脱离上下文也能读懂）。
                if len(cleaned) > len(dup.content) + 2 or (
                    source == "agent" and len(cleaned) >= len(dup.content)
                ):
                    dup.content = cleaned
                    dup.source = source
                self._dirty = True
                fact = dup
            else:
                fact = Fact(content=cleaned, importance=importance, source=source)
                self._facts[fact.id] = fact
                self._dirty = True
                self._evict_if_needed()

        self.persist()
        return fact

    def forget(self, fact_id: str) -> bool:
        """按 id 删除一条。"""
        with self._lock:
            if fact_id not in self._facts:
                return False
            del self._facts[fact_id]
            self._dirty = True
        return self.persist()

    def forget_matching(self, text: str) -> int:
        """删除内容包含 ``text`` 的所有事实，返回删除条数。

        给"忘掉关于咖啡的事"这类请求用 —— 让模型先搜索再逐条删除
        太笨拙，直接按子串删更符合直觉。
        """
        needle = (text or "").strip()
        if not needle:
            return 0
        with self._lock:
            doomed = [fid for fid, f in self._facts.items() if needle in f.content]
            for fid in doomed:
                del self._facts[fid]
            if doomed:
                self._dirty = True
        if doomed:
            self.persist()
        return len(doomed)

    def clear(self) -> int:
        """清空全部长期记忆，返回清掉的条数。"""
        with self._lock:
            n = len(self._facts)
            self._facts.clear()
            self._dirty = True
        self.persist()
        return n

    def _evict_if_needed(self) -> None:
        """超出容量时淘汰价值最低的事实。

        淘汰分数 = 重要性 + 使用频次 - 时间衰减。优先丢"不重要、
        没人再提、又很久没更新"的条目。
        """
        if len(self._facts) <= self.capacity:
            return
        now = time.time()

        def score(f: Fact) -> float:
            recency = math.exp(-f.age_days(now=now) / 30.0)
            return f.importance * 2.0 + math.log1p(f.hits) + recency * 2.0

        ordered = sorted(self._facts.values(), key=score)
        drop = len(self._facts) - self.capacity
        for fact in ordered[:drop]:
            del self._facts[fact.id]
        logger.info("长期记忆超出容量，淘汰 %d 条", drop)

    def _find_duplicate(self, content: str, tokens: set[str]) -> Fact | None:
        """找与给定内容表述同一件事的已有事实。

        为什么要两个判据：同一条信息会被两处写入 —— 规则抽取产生
        「我叫张伟」，模型调用 remember 工具产生「用户叫张伟。」。
        两者的 Jaccard 相似度只有约 0.2（因为长度和措辞都不同），
        单靠 Jaccard 判不出来，于是同一条被存两遍（实测过）。

        所以再加**包含度**：短的那一方的语素几乎都出现在长的一方里，
        就认为说的是同一件事。

        * Jaccard ≥ 0.7：措辞基本一致（同义复述）
        * 包含度 ≥ 0.7：一方是另一方的细化表述

        词元很少（< 2）时不做判重，否则「喜欢咖啡」和「喜欢喝茶」
        这类短句会被误判为同一条。
        """
        if len(tokens) < 2:
            return None

        best: Fact | None = None
        best_score = 0.0

        for fact in self._facts.values():
            if fact.content == content:
                return fact
            other = set(tokenize(fact.content))
            if len(other) < 2:
                continue

            inter = len(tokens & other)
            union = len(tokens | other)
            jaccard = inter / union if union else 0.0
            containment = inter / min(len(tokens), len(other))

            score = max(jaccard, containment)
            if score > best_score:
                best_score = score
                best = fact

        return best if best_score >= 0.7 else None

    # ------------------------------------------------------------------ #
    # 读取
    # ------------------------------------------------------------------ #
    def all(self) -> list[Fact]:
        """全部事实（按最后更新倒序）。"""
        with self._lock:
            return sorted(self._facts.values(), key=lambda f: f.updated_at, reverse=True)

    def count(self) -> int:
        """条数。"""
        with self._lock:
            return len(self._facts)

    def get(self, fact_id: str) -> Fact | None:
        """按 id 取。"""
        with self._lock:
            return self._facts.get(fact_id)

    def search(self, query: str, *, limit: int = 5) -> list[Fact]:
        """按相关度检索。

        **两阶段**，这是保证准确率的关键：

        阶段一（候选过滤）：只保留**与查询有词元重合、或构成子串**的事实。
            没有这一步，重要性/频次/新鲜度的固定加成会让几乎所有事实
            都进入排序（实测搜"咖啡"会把"机器人怕吵"也带出来）。
        阶段二（排序）：对候选打分，再按**相对阈值**（最高分的一定比例）
            截断，避免把勉强沾边的也塞进上下文。

        打分构成：
            * 词元重合度：Jaccard × 4
            * 子串命中：查询整体出现在事实里 +3（最可靠的信号）
            * 重要性：importance / 5 × 1
            * 使用频次：log1p(hits) / 3
            * 时间新鲜度：exp(-age/30天) × 1.5

        返回的每条都会 ``hits += 1``，以体现"常常被用到"。
        """
        query = (query or "").strip()
        if not query:
            return []

        q_tokens = set(tokenize(query))
        q_weights = token_weights(q_tokens)
        now = time.time()

        with self._lock:
            candidates: list[tuple[float, bool, Fact]] = []
            for fact in self._facts.values():
                f_tokens = set(tokenize(fact.content))
                overlap = q_tokens & f_tokens
                substring = query in fact.content or fact.content in query

                # 阶段一：没有任何词元重合、也不是子串 —— 直接淘汰
                if not overlap and not substring:
                    continue

                # 加权覆盖率：命中的词元权重占查询总权重的比例。
                # 用覆盖率而不是 Jaccard，是因为事实通常比查询长得多，
                # Jaccard 会被事实的额外词元稀释，导致该召回的排不上。
                total_w = sum(q_weights.values()) or 1.0
                hit_w = sum(q_weights.get(t, 0.5) for t in overlap)
                coverage = hit_w / total_w

                score = coverage * 5.0
                if substring:
                    score += 3.0
                score += (fact.importance / 5.0) * 1.0
                score += math.log1p(fact.hits) / 3.0
                score += math.exp(-fact.age_days(now=now) / 30.0) * 1.5

                candidates.append((score, substring, hit_w, coverage, fact))

            if not candidates:
                return []

            # 阶段二：排序 + **相对**阈值截断。
            #
            # 这里刻意**不做**"覆盖率必须大于某个固定值"的判断。
            # 原因是短查询里单个汉字的信号会被很长的归一化分母稀释：
            # 实测「他叫什么名字」与「主人叫张伟」只共享一个「叫」，
            # 覆盖率只有 0.055，用固定阈值 0.15 会把这条正确结果挡掉。
            # 改为以"命中的最好一条"为基准做相对比较，短查询就不会吃亏。
            candidates.sort(key=lambda item: item[0], reverse=True)
            top = candidates[0][0]
            cutoff = top * 0.6
            picked = [
                f
                for score, sub, _hit, _cov, f in candidates[:limit]
                if score >= cutoff or sub
            ]

            for f in picked:
                f.hits += 1
            if picked:
                self._dirty = True

        if picked:
            self.persist()
        return picked

    def render(self, facts: Iterable[Fact]) -> str:
        """把事实渲染成可放进 system prompt 的文本。

        带上"多久之前记的"，让模型能判断信息是否可能过时
        （例如"上个月说喜欢美式"就需要确认一下）。
        """
        rows = list(facts)
        if not rows:
            return ""
        lines: list[str] = []
        now = time.time()
        for f in rows:
            days = f.age_days(now=now)
            if days < 1:
                when = "今天"
            elif days < 2:
                when = "昨天"
            elif days < 30:
                when = f"{int(days)} 天前"
            else:
                when = f"{int(days / 30)} 个月前"
            lines.append(f"- {f.content}（{when}）")
        return "\n".join(lines)


# --------------------------------------------------------------------------- #
# 进程级单例（按路径区分）
# --------------------------------------------------------------------------- #
_memories: dict[str, LongTermMemory] = {}
_memory_lock = threading.Lock()


def get_memory(path: str | Path, *, capacity: int = 500, enabled: bool = True) -> LongTermMemory:
    """取得（或创建）指定路径的记忆库。

    用单例而不是每次 new：Agent 可能被重建（配置热更新会重建 provider），
    但记忆库应当保持同一份，否则刚记住的东西会在重建后"消失"。
    """
    key = str(Path(path).resolve())
    with _memory_lock:
        mem = _memories.get(key)
        if mem is None or mem.capacity != capacity or mem.enabled != enabled:
            mem = LongTermMemory(path, capacity=capacity, enabled=enabled)
            _memories[key] = mem
        return mem
