"""长期记忆的自动化测试。

覆盖：持久化、检索准确率、去重、容量淘汰、遗忘、自动抽取、提示词注入。
不依赖 LLM 与硬件。

跑法::

    python tests/test_long_term_memory.py
"""

from __future__ import annotations

import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / ".vendor"))

from sparkbot.brain.long_term import (  # noqa: E402
    LongTermMemory,
    Fact,
    token_weights,
    tokenize,
)

_passed = 0
_failed: list[str] = []


def check(cond: bool, label: str) -> None:
    """记录断言。"""
    global _passed
    if cond:
        _passed += 1
    else:
        _failed.append(label)
        print(f"  [FAIL] {label}")


def fresh(name: str) -> LongTermMemory:
    """建一个干净的临时记忆库。"""
    path = Path(tempfile.gettempdir()) / f"ltm-{name}.jsonl"
    path.unlink(missing_ok=True)
    return LongTermMemory(path, capacity=50)


# --------------------------------------------------------------------------- #
# 词元化
# --------------------------------------------------------------------------- #
def test_tokenize() -> None:
    """中文出单字与二元组，英文出单词。"""
    print("词元化")
    toks = set(tokenize("喜欢喝美式咖啡"))
    check("咖啡" in toks, "应有二元组「咖啡」")
    check("喜" in toks, "应有单字「喜」（用于跨用词召回）")
    check("喜欢" in toks, "应有二元组「喜欢」")

    en = set(tokenize("OpenAI GPT-4 costs 20"))
    check("openai" in en, "英文应小写并成词")
    # 连字符词作为一个整体保留：GPT-4 / gpt-4o 这类模型名是一个术语，
    # 拆成 gpt 和 4 反而会与"4 个""第 4"之类误匹配。
    check("gpt-4" in en, "连字符词应保留为一个整体")
    check("20" in en, "数字应保留")

    check(tokenize("") == [], "空串应返回空列表")

    # 停用词应被过滤
    stop = set(tokenize("的是什么了"))
    check("的" not in stop, "停用词「的」应被过滤")


def test_token_weights() -> None:
    """二元组权重高于单字，英文最高。"""
    print("词元权重")
    w = token_weights({"咖啡", "咖", "openai"})
    check(w["咖啡"] > w["咖"], "二元组权重大于单字")
    check(w["openai"] >= w["咖啡"], "英文单词权重不低于中文二元组")


# --------------------------------------------------------------------------- #
# 持久化
# --------------------------------------------------------------------------- #
def test_persist_and_reload() -> None:
    """写盘后新实例能读回。"""
    print("持久化")
    m = fresh("persist")
    m.remember("主人叫张伟", importance=5)
    m.remember("主人喜欢喝美式咖啡", importance=4)
    check(m.count() == 2, "应有 2 条")

    m2 = LongTermMemory(m.path, capacity=50)
    check(m2.count() == 2, "重新加载应有 2 条")
    contents = {f.content for f in m2.all()}
    check("主人叫张伟" in contents, "内容应完整保留")


def test_corrupt_line_tolerated() -> None:
    """文件里混入坏行时，好行仍能加载。"""
    print("坏行容错")
    m = fresh("corrupt")
    m.remember("有效事实一")
    # 手工追加坏行
    with m.path.open("a", encoding="utf-8") as fh:
        fh.write("这不是 JSON\n")
        fh.write('{"no_content": true}\n')

    m2 = LongTermMemory(m.path, capacity=50)
    check(m2.count() >= 1, "坏行不应导致整个文件加载失败")


def test_disabled_memory() -> None:
    """关掉后不读也不写。"""
    print("关闭开关")
    m = fresh("disabled")
    m.remember("先写一条")
    m.enabled = False
    m2 = LongTermMemory(m.path, capacity=50, enabled=False)
    check(m2.count() == 0, "关闭时不应加载任何记忆")
    check(m2.remember("不该被写入") is None, "关闭时 remember 应返回 None")


# --------------------------------------------------------------------------- #
# 去重与淘汰
# --------------------------------------------------------------------------- #
def test_dedup() -> None:
    """高度重复的内容应更新原条而不是新增。"""
    print("去重")
    m = fresh("dedup")
    m.remember("主人喜欢喝美式咖啡", importance=3)
    n1 = m.count()
    m.remember("主人喜欢喝美式咖啡", importance=3)
    check(m.count() == n1, f"重复内容不应新增（{n1} -> {m.count()}）")

    # 完全一样的内容
    m.remember("主人喜欢喝美式咖啡", importance=4)
    check(m.count() == n1, "完全相同的内容也不应新增")

    # 不同的事实不该被误判为重复
    m.remember("主人对花生过敏", importance=5)
    check(m.count() == n1 + 1, "不同事实应新增")


def test_dedup_across_phrasings() -> None:
    """同一条信息的两种措辞应合并，不同事实不该误合并。

    这是真实踩过的 bug：同一条信息会被两处写入 ——
    规则抽取产生「我叫张伟」，模型调用 remember 工具产生「用户叫张伟。」。
    两者 Jaccard 相似度只有约 0.2（长度和措辞都不同），
    只靠 Jaccard 判不出来，结果每条都存了两遍。
    """
    print("跨措辞去重")
    m = fresh("dedup2")
    cases = [
        ("我叫张伟", "用户叫张伟。"),
        ("我对花生过敏", "用户张伟对花生过敏。"),
        ("我喜欢喝美式咖啡", "用户张伟喜欢喝美式咖啡。"),
    ]
    for a, b in cases:
        m.clear()
        m.remember(a, importance=5, source="auto")
        m.remember(b, importance=5, source="agent")
        check(m.count() == 1, f"「{a}」与「{b}」应合并为一条（得到 {m.count()}）")

    # 不同事实必须保留，即使它们共享一个人名
    m.clear()
    m.remember("主人叫张伟", importance=5)
    m.remember("张伟的生日是三月十二号", importance=4)
    check(m.count() == 2, f"不同事实不应被合并（得到 {m.count()}）")

    # 短句不能被误判：喜欢咖啡 vs 喜欢喝茶
    m.clear()
    m.remember("喜欢咖啡")
    m.remember("喜欢喝茶")
    check(m.count() == 2, f"短句不应被误判为同一条（得到 {m.count()}）")


def test_capacity_eviction() -> None:
    """超出容量时淘汰，且优先保留重要的。"""
    print("容量淘汰")
    path = Path(tempfile.gettempdir()) / "ltm-evict.jsonl"
    path.unlink(missing_ok=True)
    m = LongTermMemory(path, capacity=10)

    m.remember("极其重要的安全信息：主人对花生过敏", importance=5)
    for i in range(20):
        m.remember(f"无关紧要的琐事编号 {i}", importance=1)

    check(m.count() <= 10, f"应不超过容量 10（得到 {m.count()}）")
    kept = {f.content for f in m.all()}
    check(
        "极其重要的安全信息：主人对花生过敏" in kept,
        "高重要度的记忆应在淘汰中被保留",
    )


# --------------------------------------------------------------------------- #
# 检索
# --------------------------------------------------------------------------- #
def test_retrieval_precision() -> None:
    """检索要能命中相关的，且不返回无关的。"""
    print("检索准确率")
    m = fresh("retrieval")
    m.remember("主人叫张伟", importance=5)
    m.remember("主人喜欢喝美式咖啡", importance=4)
    m.remember("机器人怕吵，晚上要安静", importance=3)
    m.remember("张伟的生日是三月十二号", importance=4)
    m.remember("主人对花生过敏", importance=5)

    def top(query: str) -> str:
        hits = m.search(query)
        return hits[0].content if hits else ""

    check(top("咖啡") == "主人喜欢喝美式咖啡", "「咖啡」应命中咖啡那条")
    check(top("怕吵") == "机器人怕吵，晚上要安静", "「怕吵」应命中安静那条")
    check(top("过敏") == "主人对花生过敏", "「过敏」应命中过敏那条")
    check(top("什么时候生日") == "张伟的生日是三月十二号", "「生日」应命中生日那条")

    # 这条曾经因为二元组切分而漏召回，是单字权重修复的回归测试
    check(top("他叫什么名字") == "主人叫张伟", "「他叫什么名字」应命中姓名那条")

    # 完全不相关的查询不该返回任何东西。
    # 注意要选**与已有事实没有任何共同语素**的查询：早先用「量子计算机原理」
    # 会命中「喜欢」里的「机」字，那是共享单字的正常行为，不是 bug。
    check(len(m.search("今天天气怎么样")) == 0, "无关查询不应返回结果")
    check(len(m.search("介绍一下牛顿力学定律")) == 0, "无关查询不应返回结果")


def test_retrieval_multi_hit() -> None:
    """同一关键词关联多条时都应召回。"""
    print("多命中")
    m = fresh("multi")
    m.remember("主人叫张伟", importance=5)
    m.remember("张伟的生日是三月十二号", importance=4)
    m.remember("主人喜欢喝美式咖啡", importance=4)
    hits = {f.content for f in m.search("张伟")}
    check(len(hits) >= 2, f"「张伟」应召回至少两条（得到 {hits}）")


def test_hits_increment() -> None:
    """被检索到会累计 hits，体现常用程度。"""
    print("使用计数")
    m = fresh("hits")
    f = m.remember("主人喜欢喝美式咖啡")
    assert f is not None
    before = f.hits
    m.search("咖啡")
    check(m.get(f.id).hits > before, "检索后 hits 应增加")


def test_render_includes_age() -> None:
    """渲染成提示词时应带时间信息。"""
    print("渲染")
    m = fresh("render")
    m.remember("主人叫张伟")
    text = m.render(m.all())
    check("主人叫张伟" in text, "渲染结果应含事实内容")
    check("今天" in text or "天前" in text, "应带「多久之前」的时间标记")


# --------------------------------------------------------------------------- #
# 遗忘
# --------------------------------------------------------------------------- #
def test_forget_matching() -> None:
    """按关键词删除。"""
    print("遗忘")
    m = fresh("forget")
    m.remember("主人喜欢喝美式咖啡")
    m.remember("主人喜欢喝茶")
    m.remember("主人叫张伟")
    n = m.forget_matching("咖啡")
    check(n == 1, f"应删除 1 条（得到 {n}）")
    left = {f.content for f in m.all()}
    check("主人喜欢喝美式咖啡" not in left, "咖啡那条应被删除")
    check("主人叫张伟" in left, "无关的那条应保留")

    # 确认删除已落盘
    m2 = LongTermMemory(m.path, capacity=50)
    check(
        "主人喜欢喝美式咖啡" not in {f.content for f in m2.all()},
        "删除应当已落盘",
    )

    check(m.forget_matching("不存在的东西") == 0, "删不到应返回 0")


def test_clear() -> None:
    """清空。"""
    print("清空")
    m = fresh("clear")
    m.remember("a")
    m.remember("b")
    n = m.clear()
    check(n == 2, f"应清掉 2 条（得到 {n}）")
    check(m.count() == 0, "清空后应为空")


# --------------------------------------------------------------------------- #
# 自动抽取（走 Agent 里的规则）
# --------------------------------------------------------------------------- #
def test_auto_extraction_patterns() -> None:
    """自动抽取规则：记陈述、不记疑问。"""
    print("自动抽取规则")
    import re

    from sparkbot.brain.agent import _MEMORY_PATTERNS

    def extract(text: str) -> str | None:
        if text.endswith(("？", "?", "吗", "呢")) or "什么" in text:
            return None
        for pat, _ in _MEMORY_PATTERNS:
            m = re.search(pat, text)
            if m:
                got = m.group(0).strip("，。！,!. ")
                # 与 Agent._auto_remember 一致：门槛 4 个字，
                # 这样「我叫张伟」这种最短的自述也能记住。
                return got if len(got) >= 4 else None
        return None

    check(extract("我叫张伟") == "我叫张伟", "应抽取姓名")
    check(extract("我对花生过敏") == "我对花生过敏", "应抽取过敏信息")
    check(extract("我喜欢喝美式咖啡") is not None, "应抽取偏好")
    check(extract("我住在杭州") is not None, "应抽取住址")
    check(extract("我今年 28 岁") is not None, "应抽取年龄（含空格）")

    # 疑问句与无关句不该被当成事实
    check(extract("你叫什么名字") is None, "疑问句不应被记为事实")
    check(extract("今天天气怎么样") is None, "无关句不应被记为事实")
    check(extract("帮我把灯打开") is None, "指令不应被记为事实")


# --------------------------------------------------------------------------- #
def test_fact_roundtrip() -> None:
    """Fact 的序列化/反序列化。"""
    print("Fact 序列化")
    f = Fact(content="主人叫张伟", importance=5)
    back = Fact.from_dict(f.to_dict())
    check(back is not None and back.content == f.content, "内容应往返一致")
    check(back is not None and back.importance == 5, "重要度应保留")

    check(Fact.from_dict({"content": ""}) is None, "空内容应返回 None")
    check(Fact.from_dict({"content": "ok", "importance": "坏值"}) is not None,
          "坏的重要度不应导致整条丢弃")


def main() -> int:
    """跑全部用例。"""
    print("=" * 62)
    print("长期记忆测试")
    print("=" * 62)
    print()

    test_tokenize()
    test_token_weights()
    test_persist_and_reload()
    test_corrupt_line_tolerated()
    test_disabled_memory()
    test_dedup()
    test_dedup_across_phrasings()
    test_capacity_eviction()
    test_retrieval_precision()
    test_retrieval_multi_hit()
    test_hits_increment()
    test_render_includes_age()
    test_forget_matching()
    test_clear()
    test_auto_extraction_patterns()
    test_fact_roundtrip()

    print()
    print("=" * 62)
    total = _passed + len(_failed)
    print(f"断言汇总: {_passed}/{total} 通过")
    if _failed:
        print()
        print("失败项:")
        for f in _failed:
            print(f"  - {f}")
        return 1
    print("全部通过")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
