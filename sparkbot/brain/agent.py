"""Agent 循环：感知 → 思考 → 调用工具 → 反馈 → 回复。

这是整个框架的中枢。一轮 ``run()`` 的完整流程：

1. 把用户输入（文字，可选图片）并入记忆；
2. 组装 system prompt（人格 + 当前设备状态 + 可用能力）；
3. 调用 LLM，并按当前设备能力过滤可用的工具；
4. 若模型要求调用工具 —— 经安全审查后执行，把结果回喂，回到第 3 步；
5. 模型给出最终文本 —— 收尾，并按需驱动表情/播报。

**工具轮次上限**是必需的护栏：模型偶尔会陷入「看看 → 想想 → 再看看」的循环，
``max_tool_rounds`` 保证它最终一定会说话，而不是把机器人卡在半路。
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, AsyncIterator

from ..config import Settings
from ..core.errors import DeviceOfflineError, ProviderError, SparkBotError
from ..core.events import EventBus
from ..core.tools import ToolRegistry, ToolResult
from ..device.capabilities import (
    CAP_CAMERA,
    CAP_DISPLAY,
    CAP_MOTOR,
    CAP_SPEAKER,
    Robot,
    RobotProvider,
)
from ..device.protocol import Emotion
from ..llm.base import ChatMessage, LLMProvider, LLMResponse, ToolCallRequest, Usage
from ..paths import vendor_dir
from ..perception.face import FaceDB, FaceMatch, FaceScan, get_face_db
from .long_term import LongTermMemory, get_memory
from .memory import Memory
from .tools import ToolContext

logger = logging.getLogger(__name__)


def memory_path(settings: Settings) -> Path:
    """解析长期记忆文件路径。

    相对路径以**项目根目录**为基准，而不是当前工作目录 —— 否则从不同
    目录启动服务会读到不同的记忆文件，出现"我的记忆丢了"这种假象。
    绝对路径原样使用。
    """
    raw = Path(settings.memory.path)
    if raw.is_absolute():
        return raw
    # paths.vendor_dir() = <项目根>/.vendor
    return vendor_dir().parent / raw


def face_db_path(settings: Settings) -> Path:
    """解析人脸库文件路径；相对路径的基准与长期记忆一致（项目根目录）。"""
    raw = Path(settings.face.path)
    if raw.is_absolute():
        return raw
    return vendor_dir().parent / raw


@dataclass(slots=True)
class IdentityHint:
    """从一句话里抽出的**身份信息**（用来绑人脸 + 写长期记忆）。

    ``label`` 是绑到人脸上、以后用来称呼对方的标签。它**可以是姓名，
    也可以是一个关系称呼**（「小明的爸爸」）—— 机器人并不需要知道
    户口本上的名字，只要每次都能对上同一个称呼，就能稳定地认出"这是谁"、
    并把之前聊过的内容算到他头上。
    """

    label: str
    """标签，如「王可旭」「小明的爸爸」。"""

    fact: str
    """写进长期记忆的完整事实句（自包含，脱离上下文也读得懂）。"""

    kind: str
    """``name``（自报姓名）/ ``relation``（关系）。"""

    def __str__(self) -> str:  # 方便日志里直接插值
        return self.label


def extract_identity(text: str) -> IdentityHint | None:
    """从用户话语里抽出**身份信息**：姓名，或关系（「小明的爸爸」）。

    支持的句式（**顺序即优先级**）：

    * 关系型：``我是小明的爸爸`` / ``我叫小明的妈妈`` / ``我是爸爸``
    * 整句就是一个身份短语（在回答"你是谁"）：``小明的爸爸``
    * 姓名型：``我叫王可旭`` / ``我是李白`` / ``我的名字是…`` / ``叫我小明``

    为什么关系型必须排在最前面：``我是小明的爸爸`` 若先走姓名规则，
    会被正则截出「小明的爸」当成名字 —— 这不是推测，是实测遇到的。

    为什么"关系"也算身份：机器人不需要知道对方户口本上的名字，
    只要每次都对得上同一个称呼（"小明的爸爸"）就够了。反过来，
    **纯职业不算**（"我是老师""我是工程师"）—— 把一张脸绑到"老师"上，
    下一个老师进来就会被认成同一个人，所以职业只有在**挂在某个名字下面**
    时才算身份（"我是小明的老师"可以，"我是老师"不行）。

    抽不到、或看起来不像"人"时返回 ``None``。
    """
    haystack = " ".join((text or "").split())
    if not haystack or len(haystack) > 60:
        return None

    for pattern, kind in _IDENTITY_PATTERNS:
        m = pattern.search(haystack)
        if not m:
            continue
        groups = m.groupdict()

        if kind == "relation":
            owner = (groups.get("owner") or "").strip(" 　.·、,，")
            relation = groups.get("relation") or ""
            # 名字里不会出现「的」，也不会以人称代词开头。
            # 出现这两种情况说明是切错了（例如把「我是王」当成名字，
            # 拼出「我是王的爸爸」这种荒唐标签）。宁可不绑，也不要绑错。
            if "的" in owner or owner.startswith(("我", "你", "他", "她")):
                continue
            if owner in _NOT_A_NAME:
                continue
            label = f"{owner}的{relation}" if owner else relation
            return IdentityHint(label=label, fact=f"用户是{label}", kind="relation")

        name = (groups.get("name") or "").strip(" 　.·、,，")
        if not name:
            continue
        if "的" in name:
            # 「我是小明的爸爸」这类句子没被上面的关系规则吃掉时，
            # 姓名规则会截出「小明的爸」—— 带「的」的一律不算名字。
            continue
        if name in _NOT_A_NAME:
            logger.debug("「%s」看着不像名字，跳过人脸绑定", name)
            continue
        return IdentityHint(label=name, fact=f"用户叫{name}", kind="name")
    return None


def extract_self_name(text: str) -> str | None:
    """只取**姓名**（兼容旧调用）；关系型身份返回 ``None``。"""
    hint = extract_identity(text)
    return hint.label if hint and hint.kind == "name" else None


#: 一个"名字"：中文 2~4 字，或英文（可带空格、点、连字符）。
_NAME_RE = r"(?:[\u4e00-\u9fff]{2,4}|[A-Za-z][A-Za-z .'\-]{0,20})"

#: **亲属称谓**：这类词本身就能构成身份（"我是爸爸"），可以单独绑脸。
_KINSHIP = (
    "爸爸", "妈妈", "父亲", "母亲", "老爸", "老妈", "爹", "娘",
    "儿子", "女儿", "老公", "老婆", "丈夫", "妻子", "爱人",
    "哥哥", "姐姐", "弟弟", "妹妹",
    "爷爷", "奶奶", "外公", "外婆", "姥姥", "姥爷",
)

#: **其他关系/职业**：必须挂在名字下面才算身份（"小明的老师"），
#: 单独出现（"我是老师"）不绑 —— 否则下一个老师会被认成同一个人。
_ROLE = (
    "老师", "同学", "同事", "朋友", "老板", "教练", "师傅", "房东",
    "叔叔", "阿姨", "舅舅", "姑姑", "伯伯", "婶婶",
)

_KINSHIP_ALT = "|".join(_KINSHIP)
_ROLE_ALT = "|".join(_ROLE)
_ANY_RELATION_ALT = f"{_KINSHIP_ALT}|{_ROLE_ALT}"

#: 身份句式表：``(正则, kind)``。**顺序即优先级**，关系型必须在姓名型之前。
_IDENTITY_PATTERNS: list[tuple[re.Pattern[str], str]] = [
    # 关系型（带名字）：我是小明的爸爸 / 我叫小明的老师
    (re.compile(rf"我(?:是|叫)\s*(?P<owner>{_NAME_RE})的(?P<relation>{_ANY_RELATION_ALT})"),
     "relation"),
    # 关系型（不带名字，仅限亲属）：我是爸爸 / 我是妈妈
    (re.compile(rf"我(?:是|叫)\s*(?P<relation>{_KINSHIP_ALT})"), "relation"),
    # 整句就是一个身份短语（在回答"你是谁"）：小明的爸爸
    # 整句身份短语：必须以**非人称代词**开头，否则「我是王的爸爸」会被
    # 当成 owner="我是王" 而拼出一个荒唐的标签（踩过）。
    (re.compile(rf"^(?!我|你|他|她)(?P<owner>{_NAME_RE})的(?P<relation>{_ANY_RELATION_ALT})$"),
     "relation"),
    # 姓名型
    (re.compile(rf"我(?:的名字)?(?:叫|是)\s*(?P<name>{_NAME_RE})"), "name"),
    (re.compile(rf"(?:我的)?名字(?:是|叫)\s*(?P<name>{_NAME_RE})"), "name"),
    (re.compile(rf"叫我\s*(?P<name>{_NAME_RE})"), "name"),
]

#: 自我介绍句式里常见、但**不是名字**的词。
#: 不拦的话 "我是学生" 会把一张脸绑到"学生"上，之后所有学生都被叫"学生"。
_NOT_A_NAME = frozenset(
    """
    学生 老师 工程师 程序员 医生 司机 老板 大人 机器人 助理 记者 律师
    护士 警察 同事 朋友 新人 新来的 一个 好人 坏人 小孩子 大人
    """.split()
)


#: 自动记忆的抽取规则：(正则, 重要度)。
#:
#: 顺序有意义：**具体的、信息量大的模式排在前面**，因为一句话只抽一条。
#: 例如"我对花生过敏"应当命中过敏规则，而不是被更靠前的"我需要…"截走。
#: 重要度用于检索排序，也决定记忆库满了以后谁先被淘汰。
_MEMORY_PATTERNS: list[tuple[str, int]] = [
    # 过敏/禁忌属于安全信息，必须记住
    (r"我对[^，。！？,!.?]{1,12}(?:过敏|忌口|不能吃)", 5),
    # 身份
    (r"我(?:的名字)?(?:叫|是)[^，。！？,!.?]{1,12}", 5),
    (r"我(?:今年)?\s*\d{1,3}\s*岁", 4),
    (r"我是[^，。！？,!.?]{1,16}(?:的学生|的老师|的工程师|程序员|医生|司机)", 4),
    (r"我住在[^，。！？,!.?]{1,16}", 4),
    (r"我(?:在|在)[^，。！？,!.?]{1,12}(?:上班|工作|上学)", 4),
    # 偏好
    (r"我(?:很|最|特别)?(?:喜欢|爱|讨厌|不喜欢|害怕)[^，。！？,!.?]{1,16}", 4),
    # 习惯/约定
    (r"我(?:习惯|通常|一般)[^，。！？,!.?]{1,16}", 3),
    (r"以后[^，。！？,!.?]{1,16}", 3),
]


# --------------------------------------------------------------------------- #
# 结果结构
# --------------------------------------------------------------------------- #
@dataclass(slots=True)
class ToolInvocation:
    """一次工具调用的完整记录，供日志、面板与测试断言使用。"""

    name: str
    arguments: dict[str, Any]
    ok: bool
    result: Any = None
    error: str | None = None
    duration_ms: int = 0

    def to_dict(self) -> dict[str, Any]:
        """转成可序列化字典。"""
        return {
            "name": self.name,
            "arguments": self.arguments,
            "ok": self.ok,
            "result": self.result,
            "error": self.error,
            "duration_ms": self.duration_ms,
        }


@dataclass(slots=True)
class AgentTurn:
    """一轮对话的产出。"""

    reply: str
    tool_invocations: list[ToolInvocation] = field(default_factory=list)
    usage: Usage = field(default_factory=Usage)
    rounds: int = 0
    duration_ms: int = 0
    error: str | None = None
    #: 本轮开始前扫到的人脸匹配结果（见 :meth:`Agent._look_at_faces`）。
    #: 空列表表示没扫到脸、或人脸能力没启用。
    faces: list[dict[str, Any]] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        """本轮是否正常完成（没有致命错误）。"""
        return self.error is None

    def to_dict(self) -> dict[str, Any]:
        """转成可序列化字典。"""
        return {
            "reply": self.reply,
            "tools": [t.to_dict() for t in self.tool_invocations],
            "usage": {
                "prompt_tokens": self.usage.prompt_tokens,
                "completion_tokens": self.usage.completion_tokens,
                "total_tokens": self.usage.total_tokens,
            },
            "rounds": self.rounds,
            "duration_ms": self.duration_ms,
            "error": self.error,
            "faces": self.faces,
        }


# --------------------------------------------------------------------------- #
# 情绪线索
# --------------------------------------------------------------------------- #
#: 从回复文本里推断表情的关键词表。放在 PC 侧的好处是
#: 模型不必为「换个表情」多花一次工具调用，动作与说话能同时发生。
_EMOTION_CUES: list[tuple[tuple[str, ...], Emotion]] = [
    (("太好了", "真棒", "哈哈", "开心", "喜欢", "谢谢", "好耶", "!"), Emotion.HAPPY),
    (("抱歉", "对不起", "没能", "做不到", "遗憾"), Emotion.SAD),
    (("不行", "别碰", "危险", "住手", "警告"), Emotion.ANGRY),
    (("哇", "竟然", "真的吗", "没想到", "居然"), Emotion.SURPRISED),
    (("让我想想", "思考", "考虑", "正在想"), Emotion.THINKING),
    (("不确定", "不清楚", "看不懂", "什么", "?"), Emotion.CONFUSED),
    (("困", "休息", "晚安", "睡觉"), Emotion.SLEEPY),
]


def infer_emotion(text: str) -> Emotion | None:
    """从回复文本推断一个合适的表情；推断不出返回 ``None``。

    只在模型本轮**没有**显式调 ``show_emotion`` 时才使用，
    避免覆盖模型的主动表达。
    """
    haystack = text or ""
    for keywords, emotion in _EMOTION_CUES:
        if any(keyword in haystack for keyword in keywords):
            return emotion
    return None


#: 播报分段用的句末标点（中英文都算）。
_SENTENCE_RE = re.compile(r"[^。！？!?；;\n]*[。！？!?；;\n]+|[^。！？!?；;\n]+")

#: 上面的规则还切不开时，再按这些"停顿感稍弱"的标点切。
_CLAUSE_RE = re.compile(r"[^，,、：:]*[，,、：:]+|[^，,、：:]+")

#: 单个播报段落的字符上限。
#: 依据是硬限制而非口味：单条 WebSocket 消息约 708KB ≈ 22 秒 ≈ 95 字，
#: 超过就整段发不出去。取 60 字（≈ 14 秒 ≈ 450KB）留足余量，
#: 让"停顿"落在标点上，听感自然。
MAX_SPEECH_SEGMENT_CHARS = 60

#: 流式播报每块 PCM 的字节数（4096 = 2048 采样 = 128ms 音频）。
#: 太小会让命令往返次数暴增，太大则首块延迟变大、单条消息也更大。
STREAM_CHUNK_BYTES = 4096

#: 流式播报要求的采样率 —— 固件就是按 16kHz 播的，不做重采样。
STREAM_SAMPLE_RATE = 16000


def split_for_speech(
    text: str, *, max_chars: int = MAX_SPEECH_SEGMENT_CHARS
) -> list[str]:
    """把回复切成适合逐段播报的片段。

    为什么需要分段：单条 WebSocket 消息有长度上限（约 22 秒 / 95 字音频），
    超出时 PC 端会直接拒发 —— 现象就是"短句有声、背古诗整段没声"。
    切开以后每段都远小于上限；附带好处是第一段合成完就能开口，
    不用等整篇合成完。

    切分优先级：句末标点 → 逗号类标点 → 硬切（保证任何输入都不会超限）。
    """
    text = (text or "").strip()
    if not text:
        return []

    segments: list[str] = []
    for sentence in _SENTENCE_RE.findall(text):
        sentence = sentence.strip()
        if not sentence:
            continue
        if len(sentence) <= max_chars:
            segments.append(sentence)
            continue

        # 句子太长：按逗号类标点二次切分
        buffer = ""
        for clause in _CLAUSE_RE.findall(sentence):
            if len(buffer) + len(clause) <= max_chars:
                buffer += clause
                continue
            if buffer.strip():
                segments.append(buffer.strip())
            buffer = ""
            # 单条 clause 仍然超限（没有标点的长串）：硬切
            while len(clause) > max_chars:
                segments.append(clause[:max_chars])
                clause = clause[max_chars:]
            buffer = clause
        if buffer.strip():
            segments.append(buffer.strip())

    return segments


# --------------------------------------------------------------------------- #
# Agent
# --------------------------------------------------------------------------- #
class Agent:
    """一台机器人的对话与决策中枢。"""

    def __init__(
        self,
        *,
        settings: Settings,
        provider: LLMProvider,
        registry: ToolRegistry,
        context: ToolContext,
        robots: RobotProvider,
        bus: EventBus,
        device_id: str | None = None,
    ) -> None:
        self.settings = settings
        self.provider = provider
        self.registry = registry
        self.ctx = context
        self.robots = robots
        self.bus = bus
        self.device_id = device_id
        self.memory = Memory(
            system_prompt=self._base_system_prompt(),
            limit=settings.behavior.history_limit,
        )
        # 长期记忆：与上面的短期会话记忆互补。见 brain/long_term.py 的说明。
        # 用 get_memory() 取单例，这样 Agent 因配置热更新被重建时，
        # 记忆库还是同一份（否则刚记住的东西会"消失"）。
        self.long_term: LongTermMemory | None = None
        if settings.memory.enabled:
            try:
                self.long_term = get_memory(
                    memory_path(settings),
                    capacity=settings.memory.capacity,
                    enabled=True,
                )
            except Exception:  # noqa: BLE001 - 记忆坏了不该让机器人起不来
                logger.exception("长期记忆初始化失败，将以无长期记忆模式运行")
        # 把记忆库交给工具上下文，供 remember / recall / forget 工具使用。
        # ctx 可能为 None（纯聊天场景；测试里就常传 None），所以要判空。
        if self.ctx is not None:
            self.ctx.long_term = self.long_term

        # 人脸库：名字 ↔ 512 维人脸特征。**推理在设备上做，名字留在 PC 侧** ——
        # 设备只回特征向量，认人和绑名都在这里（见 perception/face.py）。
        # 同样取单例，配置热更新重建 Agent 时不会把已绑定的名字弄丢。
        self.face_db: FaceDB | None = None
        if settings.face.enabled:
            try:
                self.face_db = get_face_db(
                    face_db_path(settings),
                    threshold=settings.face.threshold,
                    max_samples=settings.face.max_samples,
                    enabled=True,
                )
            except Exception:  # noqa: BLE001 - 人脸库坏了不该让机器人起不来
                logger.exception("人脸库初始化失败，将以无人脸识别模式运行")
        if self.ctx is not None:
            self.ctx.face_db = self.face_db
        self._lock = asyncio.Lock()
        """串行化同一台机器人的对话，避免两轮对话抢同一个底盘。"""

    @property
    def busy(self) -> bool:
        """这一轮是否还没结束（含"播报收尾"）。

        语音闭环用它做**回声自触发保护**：设备就贴在喇叭旁边，播报时它
        听得见自己；如果这时又响一次唤醒词就开新一轮采集，新的一轮会把
        正在播的语音硬切掉，听感就是断续/杂音。

        ``run()`` 全程持有 ``_lock``（包括 ``_express`` 里等设备播完），
        所以"锁被占用"就等于"还没说完"，语义正好。
        """
        return self._lock.locked()

    # ------------------------------------------------------------------ #
    # 提示词
    # ------------------------------------------------------------------ #
    def _base_system_prompt(self) -> str:
        """人格与行为准则。"""
        return (
            f"{self.settings.persona}\n\n"
            "行为准则：\n"
            "1. 你控制一台真实存在的机器人，它会真的移动，所以移动前要先确认前方安全。\n"
            "2. 任何关于「周围有什么」的问题，都必须先调用 look_around 看图再回答，不许猜。\n"
            "3. 移动类工具调用要克制，一次只走一小步，不要连续长距离移动。\n"
            "4. 用户表达情绪或对话气氛变化时，主动调用 show_emotion 换表情。\n"
            "5. 回复要短，两三句话说完，因为是要被语音播报出来的。\n"
            "6. 工具返回失败时，用自然语言说明做不到，不要假装成功。\n"
            "7. 一旦知道面前的人是谁 —— 姓名，或者「小明的爸爸」「我是妈妈」"
            "这类称呼 —— 就调用 bind_face 把眼前这张脸和这个称呼绑起来，"
            "以后才认得出他。称呼用用户自己说的说法，不要改写成别的词。\n"
        )

    def _device_context(self) -> str:
        """把当前设备状态渲染成一段提示词，让模型知道自己有什么能力。"""
        try:
            robot = self.robots.get(self.device_id)
        except DeviceOfflineError:
            return "\n当前状态：没有机器人在线，你无法执行任何动作，只能进行纯文本对话。\n"

        status = robot.status()
        capabilities = status.get("capabilities") or []
        lines = [f"\n当前状态：你正在控制机器人「{status.get('name')}」（id={status.get('device_id')}）。"]
        lines.append(f"具备能力：{'、'.join(capabilities) if capabilities else '无'}。")
        if status.get("battery_percent") is not None:
            lines.append(f"当前电量：{status['battery_percent']}%。")
        motion = status.get("motion") or {}
        if motion.get("linear") or motion.get("angular"):
            lines.append("当前正在移动中。")
        else:
            lines.append("当前处于静止状态。")

        missing = [
            label
            for cap, label in (
                ("camera", "摄像头"),
                ("microphone", "麦克风"),
                ("speaker", "喇叭"),
                ("display", "显示屏"),
                ("motor", "电机"),
            )
            if cap not in capabilities
        ]
        if missing:
            lines.append(f"注意：本机没有 {'、'.join(missing)}，相关工具不可用。")
        return "\n".join(lines) + "\n"

    def _auto_remember(self, user_text: str) -> None:
        """从用户话语里自动抽取值得长期记住的事实。

        为什么要自动抽取，而不只依赖模型调用 ``remember`` 工具：
        模型很可能在闲聊时忽略"顺便记一下"，而"我叫张伟""我对花生过敏"
        这类信息一旦漏记就永远丢了。这里用规则兜底，保证关键信息一定进库。

        **只记第一人称的陈述**（"我叫…""我喜欢…"），不记疑问句 ——
        否则"你叫什么名字"会被当成事实存下来。
        """
        if self.long_term is None or not self.settings.memory.auto_extract:
            return
        text = (user_text or "").strip()
        if not text or len(text) > 200:
            return
        # 疑问句不是事实陈述
        if text.endswith(("？", "?", "吗", "呢")) or "什么" in text:
            return

        for pattern, importance in _MEMORY_PATTERNS:
            m = re.search(pattern, text)
            if not m:
                continue
            fact = m.group(0).strip("，。！,!. ")
            # 门槛定在 4 个字符：中文姓名类的短句就是 4 个字（「我叫张伟」），
            # 定成 5 会把最常见的自我介绍漏掉。同时 4 已能滤掉只匹配到
            # 「我」「我是」这类噪声。
            if len(fact) < 4:
                continue
            try:
                stored = self.long_term.remember(fact, importance=importance, source="auto")
            except Exception:  # noqa: BLE001 - 记忆写失败不该影响对话
                logger.debug("自动记忆写入失败", exc_info=True)
                return
            if stored is not None:
                logger.info("自动记住: %s", stored.content)
                self.bus.publish(
                    "memory.remembered",
                    device_id=self.device_id,
                    text=stored.content,
                    source="auto",
                )
            return  # 一句话只抽一条，避免同一句话被模式重复命中

    def _memory_context(self, query: str) -> str:
        """把与当前输入相关的长期记忆渲染成提示词片段。

        检索用**用户这句话**做查询，所以注入的是当下可能用得上的几条，
        而不是把整个记忆库塞进上下文 —— 后者又费 token 又容易让模型
        被无关信息带偏。
        """
        if self.long_term is None or not self.long_term.enabled:
            return ""
        try:
            facts = self.long_term.search(query, limit=self.settings.memory.max_injected)
        except Exception:  # noqa: BLE001 - 记忆检索失败不该影响对话
            logger.debug("长期记忆检索失败", exc_info=True)
            return ""
        if not facts:
            return ""
        body = self.long_term.render(facts)
        return (
            "\n关于用户的长期记忆（跨会话保留，可能过时，必要时应确认）：\n"
            f"{body}\n"
        )

    def _compose_messages(self, *, query: str = "") -> list[ChatMessage]:
        """组装本轮要发给模型的完整消息。

        system prompt 每轮都重建，因为设备状态（电量、运动、在线情况）
        在对话过程中会变，而这些信息直接影响模型该不该移动。
        长期记忆也在这里注入，且只注入与当前输入相关的那几条。
        """
        messages = self.memory.messages()
        messages[0] = ChatMessage.system(
            self._base_system_prompt()
            + self._device_context()
            + self._memory_context(query)
            + self._face_context()
        )
        return messages

    # ------------------------------------------------------------------ #
    # 人脸：认人 + 绑名
    # ------------------------------------------------------------------ #
    def _face_context(self) -> str:
        """把"现在面前是谁"渲染成提示词片段（本轮扫脸的结果，见 _look_at_faces）。"""
        hint = getattr(self, "_face_hint", "")
        if not hint:
            return ""
        return (
            "\n人脸识别（设备端本地推理，可能出错；拿不准就用自然语言确认一下）：\n"
            f"{hint}\n"
        )

    async def _look_at_faces(self) -> tuple[FaceScan | None, str]:
        """扫一帧人脸并匹配姓名，返回 ``(扫描结果, 注入提示词)``。

        为什么每轮都扫：用户是站在机器人面前的，认不出说话的人是谁，
        "自动绑定姓名"就无从谈起。代价是设备端一次推理约 0.4~0.8 秒，
        所以给了 ``face.auto_scan`` 开关。

        任何一步失败都只记日志、返回空提示 —— 人脸是锦上添花的能力，
        不能因为它坏了就让对话进行不下去。
        """
        self._face_hint = ""
        self._face_matches: list[FaceMatch] = []
        if self.face_db is None or not self.settings.face.enabled:
            return None, ""
        if not self.settings.face.auto_scan:
            return None, ""

        try:
            robot = self.robots.get(self.device_id)
        except DeviceOfflineError:
            return None, ""
        if not robot.has(CAP_CAMERA):
            return None, ""

        try:
            scan = await robot.identify_faces()
        except SparkBotError as exc:
            logger.info("人脸扫描跳过（%s）", exc.message)
            return None, ""
        except Exception as exc:  # noqa: BLE001 - 感知失败不该影响对话
            logger.debug("人脸扫描异常: %s", exc, exc_info=True)
            return None, ""

        if not scan.faces:
            if scan.too_dark:
                # 别把"太黑"说成"没人"—— 前者要人去开灯/调角度，
                # 后者是正常结果，混在一起用户会以为是识别坏了。
                return scan, (
                    f"摄像头画面很暗（平均亮度 {scan.mean_luma}/255），看不清前面有没有人。"
                    "可以提醒对方把灯打开或让光线照到镜头这一侧。"
                )
            if scan.dropped:
                # 设备说检到了脸、特征却用不了（字段缺失/解不开）。
                # 这不是"没人"，而是链路坏了，必须说出来而不是显示成 0。
                logger.warning(
                    "设备报告 %d 张脸，但特征不可用的有 %d 张（疑似固件与 PC 侧版本不匹配）",
                    scan.reported_count,
                    scan.dropped,
                )
                return scan, (
                    f"设备其实检测到 {scan.reported_count} 张脸，但人脸特征数据不完整，"
                    "没法认人。这通常是固件与 PC 端版本不匹配，需要更新固件。"
                )
            return scan, "摄像头里没有检测到人脸。"

        try:
            matches = self.face_db.match_scan(scan.faces)
        except Exception as exc:  # noqa: BLE001
            logger.debug("人脸匹配失败: %s", exc, exc_info=True)
            return scan, ""

        hint = self._render_face_hint(matches)
        self._face_hint = hint
        self._face_matches = matches
        logger.info(
            "人脸扫描: %d 张脸 → %s",
            len(scan.faces),
            "、".join(
                f"{m.name or '未登记'}({m.similarity:.2f})" for m in matches
            ),
        )
        self.bus.publish(
            "face.scanned",
            device_id=self.device_id,
            faces=[m.to_dict() for m in matches],
        )
        return scan, hint

    def _render_face_hint(self, matches: list[FaceMatch]) -> str:
        """把匹配结果写成一句模型能直接用的中文。"""
        known = [m for m in matches if m.known]
        if not known:
            return (
                f"摄像头里看到 {len(matches)} 张脸，但都不在已登记的人脸库里。"
                "不要瞎猜对方是谁；可以请他做个自我介绍，"
                "确认姓名后再调用 bind_face 把这张脸绑上去。"
            )

        limit = max(1, int(self.settings.face.max_injected))
        parts = [f"「{m.name}」（相似度 {m.similarity:.2f}）" for m in known[:limit]]
        unknown = len(matches) - len(known)
        tail = f"；另外还有 {unknown} 张没登记过的脸" if unknown > 0 else ""
        return (
            f"现在站在你面前的最像：{'、'.join(parts)}{tail}。"
            "可以直接用名字称呼对方，但别把不认识的人认成他。"
        )

    async def scan_faces(self, *, include_feature: bool = False) -> dict[str, Any]:
        """主动扫一次脸并返回结构化结果（给控制台/接口用，不经过模型）。"""
        try:
            robot = self.robots.get(self.device_id)
        except DeviceOfflineError as exc:
            raise SparkBotError(exc.message) from exc
        if self.face_db is None or not self.settings.face.enabled:
            raise SparkBotError("人脸识别未启用")

        scan = await robot.identify_faces()
        # 逐张脸匹配后排序。刻意**不**复用 match_scan()：那个方法会丢掉
        # "哪张脸对应哪条结果"的配对关系（它按相似度重排），而控制台要画框，
        # 必须让每条结果带着自己那张脸的检测分数。
        rows: list[dict[str, Any]] = []
        for face in scan.faces:
            m = self.face_db.match(face.feat)
            rows.append(
                {
                    "name": m.name,
                    "similarity": round(m.similarity, 4),
                    "known": m.known,
                    "box": list(face.box),
                    "score": round(float(face.score), 4),
                }
            )
        rows.sort(key=lambda r: r["similarity"], reverse=True)
        return {
            "ok": True,
            "device_id": robot.device_id,
            "count": len(scan.faces),
            "reported_count": scan.reported_count,
            "dropped": scan.dropped,
            "width": scan.width,
            "height": scan.height,
            "mean_luma": scan.mean_luma,
            "too_dark": scan.too_dark,
            "faces": rows,
            "people": self.face_db.snapshot(),
        }

    async def bind_face(self, name: str, *, role: str = "user") -> dict[str, Any]:
        """把当前画面里**最大的那张脸**绑到指定名字上。

        为什么取"最大"：离摄像头最近的人通常就是正在说话的人。设备端的
        ``HumanFaceRecognizer`` 在多张脸时也是这么选的（box 面积最大）。

        Raises:
            SparkBotError: 没人脸能力、没检测到脸、或特征写不进去。
        """
        if self.face_db is None or not self.settings.face.enabled:
            raise SparkBotError("人脸识别未启用")
        robot = self.robots.get(self.device_id)
        if not robot.has(CAP_CAMERA):
            raise SparkBotError(f"设备 {robot.device_id} 没有摄像头")

        scan = await robot.identify_faces()
        if not scan.faces:
            if scan.too_dark:
                raise SparkBotError(
                    f"画面太暗（平均亮度 {scan.mean_luma}/255），看不清脸；"
                    "请先把灯打开或让光线照到这一侧再试"
                )
            if scan.dropped:
                raise SparkBotError(
                    f"设备检测到 {scan.reported_count} 张脸，但人脸特征数据不完整，绑不了"
                    "（通常是固件与 PC 端版本不匹配，需要更新固件）"
                )
            raise SparkBotError("这一帧里没有检测到人脸，请让对象正对摄像头再试一次")

        face = max(
            scan.faces,
            key=lambda f: max(0, f.box[2] - f.box[0]) * max(0, f.box[3] - f.box[1]),
        )
        entry = self.face_db.enroll(name, face.feat, source=role)
        if entry is None:
            raise SparkBotError(f"「{name}」不是有效的名字，或人脸特征不完整")

        logger.info("人脸绑定: %s（累计 %d 条特征）", entry["name"], entry["samples"])
        self.bus.publish(
            "face.bound", device_id=robot.device_id, name=entry["name"], samples=entry["samples"]
        )
        # 同时写进长期记忆：下次对话即使没扫到脸（背对摄像头、光线太暗），
        # 也还能靠名字与之前聊过的内容认出这个人是"谁"。
        if self.long_term is not None:
            with contextlib.suppress(Exception):
                self.long_term.remember(
                    f"用户叫{entry['name']}（已绑定人脸）", importance=5, source="face"
                )
        return {"ok": True, **entry}

    def _auto_bind_face(self, user_text: str, scan: FaceScan | None) -> None:
        """从这句话里认出身份时，自动绑脸 + 写长期记忆 —— 「自动关联名字」的核心。

        身份**不限于姓名**：``我叫王可旭``、``我是小明的爸爸``、``我是妈妈``、
        以及把整个句子当回答用的 ``小明的爸爸`` 都算（见 :func:`extract_identity`）。
        关系型身份和姓名同等对待 —— 机器人不需要知道户口本上的名字，
        只要每次都对得上同一个称呼就够了。

        绑脸要求**同一轮里既说了身份、又刚好扫到脸**：
        * 报了身份但画面里没脸（人在镜头外）→ 只记事实，不绑；
        * 画面里有一堆脸而没报身份 → 不知道绑谁，不绑。
        之所以取"最大的那张脸"：离摄像头最近的人通常就是正在说话的人。
        """
        hint = extract_identity(user_text)
        if hint is None:
            return

        can_bind = (
            self.face_db is not None
            and self.settings.face.enabled
            and self.settings.face.auto_enroll
            and scan is not None
            and bool(scan.faces)
        )
        if not can_bind:
            # 没拍到脸也要把身份记下来：下次认不出来时还能靠称呼对上人。
            self._remember_identity(hint, bound=False)
            return

        face = max(
            scan.faces,
            key=lambda f: max(0, f.box[2] - f.box[0]) * max(0, f.box[3] - f.box[1]),
        )
        try:
            entry = self.face_db.enroll(hint.label, face.feat, source="auto")
        except Exception as exc:  # noqa: BLE001 - 绑不上不该影响对话
            logger.debug("自动人脸绑定失败: %s", exc, exc_info=True)
            return
        if entry is None:
            return

        logger.info(
            "自动人脸绑定: %s（%s，累计 %d 条特征）",
            entry["name"], hint.kind, entry["samples"],
        )
        self.bus.publish(
            "face.bound",
            device_id=self.device_id,
            name=entry["name"],
            samples=entry["samples"],
            kind=hint.kind,
            reason="auto",
        )
        self._remember_identity(hint, bound=True)

    def _remember_identity(self, hint: IdentityHint, *, bound: bool) -> None:
        """把身份写进长期记忆；``bound`` 表示这次是否真的绑上了脸。

        只有真绑上了才写「（已绑定人脸）」—— 没拍到脸时说这句就是假的。
        """
        if self.long_term is None:
            return
        fact = f"{hint.fact}（已绑定人脸）" if bound else hint.fact
        try:
            self.long_term.remember(fact, importance=5, source="face")
        except Exception:  # noqa: BLE001 - 记忆写失败不该影响对话
            logger.debug("身份写长期记忆失败: %s", fact, exc_info=True)

    # ------------------------------------------------------------------ #
    # 主循环
    # ------------------------------------------------------------------ #
    async def run(
        self,
        user_text: str,
        *,
        images: list[str] | None = None,
        announce: bool = True,
        allow_tools: bool = True,
    ) -> AgentTurn:
        """处理一轮用户输入并返回结果。

        Args:
            user_text: 用户说的话（或识别出的文本）。
            images: 随输入一起送进模型的多模态图片（data URI 或 URL）。
            announce: 是否把回复用 TTS 播报出来。
            allow_tools: 是否开放工具；纯聊天场景可关掉以省 token。

        Raises:
            这里不会向上抛业务异常——失败被收敛进 ``AgentTurn.error``，
            因为语音链路必须永远能给出一点反馈。
        """
        started = time.perf_counter()
        turn = AgentTurn(reply="")

        if not user_text.strip() and not images:
            turn.error = "空输入"
            return turn

        async with self._lock:
            self.memory.append(ChatMessage.user(user_text, images))
            self.bus.publish("agent.user_message", device_id=self.device_id, text=user_text)

            # 先扫一眼"面前是谁"，再让模型开口。
            # 放在 _think 之前是因为这一轮的 system prompt 需要它；放在锁内
            # 是因为同一台机器人同时只跑一轮对话，两个扫描抢摄像头没有意义。
            face_scan, face_hint = await self._look_at_faces()
            self._face_hint = face_hint
            turn.faces = [m.to_dict() for m in self._face_matches]

            try:
                await self._think(turn, allow_tools=allow_tools, query=user_text)
            except ProviderError as exc:
                turn.error = f"模型调用失败: {exc.message}"
                turn.reply = "我的大脑有点连不上，稍后再试试吧。"
                logger.warning("provider 失败: %s", exc)
            except Exception as exc:  # noqa: BLE001 - 对话不能因意外中断
                turn.error = f"内部错误: {type(exc).__name__}: {exc}"
                turn.reply = "我这里出了点小问题。"
                logger.exception("agent 循环异常")

            turn.duration_ms = int((time.perf_counter() - started) * 1000)
            self.memory.append(ChatMessage(role="assistant", content=turn.reply))

            # 从用户这句话里抽取值得长期记住的事实。
            # 放在 _think 之后：即使模型调用失败，用户刚说的信息也值得记下来。
            self._auto_remember(user_text)
            # 用户自报姓名 → 把眼前这张脸绑上（"自动关联名字"）。
            self._auto_bind_face(user_text, face_scan)

            if turn.reply:
                self.bus.publish(
                    "agent.reply",
                    device_id=self.device_id,
                    text=turn.reply,
                    tools=[t.name for t in turn.tool_invocations],
                    duration_ms=turn.duration_ms,
                )
                await self._express(turn, announce=announce)

        return turn

    async def _think(self, turn: AgentTurn, *, allow_tools: bool, query: str = "") -> None:
        """执行「模型 ↔ 工具」的多轮交互，直到模型给出最终文本。

        ``query`` 只用于检索长期记忆：它决定本轮往 system prompt 里注入
        哪几条相关事实，不参与别的逻辑。
        """
        cfg = self.settings
        tools = self._available_tools() if allow_tools else []
        max_rounds = max(1, cfg.llm.max_tool_rounds)

        for round_index in range(max_rounds):
            turn.rounds = round_index + 1
            messages = self._compose_messages(query=query)

            response = await self.provider.chat(
                messages,
                tools=tools or None,
                temperature=cfg.llm.temperature,
                max_tokens=cfg.llm.max_tokens,
            )
            turn.usage = turn.usage + response.usage

            logger.debug(
                "模型第 %d 轮返回: content=%r tool_calls=%s",
                turn.rounds,
                (response.content or "")[:60],
                [c.name for c in response.tool_calls],
            )
            if not response.wants_tools:
                turn.reply = (response.content or "").strip() or "嗯，我在。"
                return

            # 记录 assistant 的工具调用意图，构成完整的协议往返。
            self.memory.append(
                ChatMessage(
                    role="assistant",
                    content=response.content or "",
                    tool_calls=response.tool_calls,
                )
            )

            for call in response.tool_calls:
                invocation = await self._execute_tool(call)
                turn.tool_invocations.append(invocation)
                payload = ToolResult(
                    name=invocation.name,
                    ok=invocation.ok,
                    data=invocation.result,
                    error=invocation.error,
                )
                self.memory.append(ChatMessage.tool(call.id, payload.to_json()))

            logger.debug(
                "第 %d 轮工具执行完毕: %s",
                turn.rounds,
                [t.name for t in turn.tool_invocations],
            )

        # 轮次耗尽：强制要一句人话，避免机器人「想太多」而沉默。
        logger.warning("工具轮次达到上限 %d，强制要求模型给出最终回复", max_rounds)
        try:
            final = await self.provider.chat(
                self._compose_messages(),
                tools=None,
                temperature=cfg.llm.temperature,
                max_tokens=cfg.llm.max_tokens,
            )
            turn.usage = turn.usage + final.usage
            turn.reply = (final.content or "").strip() or "我看了半天，还没拿定主意。"
        except ProviderError:
            turn.reply = "我看了半天，还没拿定主意。"

    def _available_tools(self) -> list[dict[str, Any]]:
        """按当前设备能力筛出可下发的工具定义。"""
        try:
            robot = self.robots.get(self.device_id)
            capabilities = robot.capabilities
        except DeviceOfflineError:
            # 没有设备时仍然开放不依赖硬件的工具（例如 get_status）。
            capabilities = frozenset()
        return self.registry.openai_schemas(capabilities)

    # ------------------------------------------------------------------ #
    # 工具执行
    # ------------------------------------------------------------------ #
    async def _execute_tool(self, call: ToolCallRequest) -> ToolInvocation:
        """执行一次工具调用，并广播过程事件。

        未知工具名和参数错误都交给注册表统一处理成失败结果，
        模型据此能自我纠正，而不是让整轮对话失败。
        """
        self.bus.publish(
            "agent.tool_call",
            device_id=self.device_id,
            tool=call.name,
            arguments=call.arguments,
        )

        # 工具参数里允许带 device_id；没带就默认用本 agent 绑定的设备。
        arguments = dict(call.arguments)
        if "device_id" in arguments and not arguments["device_id"]:
            arguments["device_id"] = self.device_id or ""

        result = await self.registry.execute(call.name, arguments)
        invocation = ToolInvocation(
            name=result.name,
            arguments=arguments,
            ok=result.ok,
            result=result.data,
            error=result.error,
            duration_ms=result.duration_ms,
        )

        self.bus.publish(
            "agent.tool_result",
            device_id=self.device_id,
            tool=result.name,
            ok=result.ok,
            error=result.error,
            duration_ms=result.duration_ms,
        )
        if not result.ok:
            logger.info("工具 %s 失败: %s", result.name, result.error)
        return invocation

    # ------------------------------------------------------------------ #
    # 表达：表情 + 播报
    # ------------------------------------------------------------------ #
    async def _express(self, turn: AgentTurn, *, announce: bool) -> None:
        """根据本轮结果驱动屏幕表情与语音播报。

        这两件事都**不该**让对话失败：喇叭没接、屏幕不响应，
        用户至少还能在控制台上看到文字回复。表情与播报也彼此独立——
        ``announce=False`` 只关掉语音，表情照常变化。
        """
        try:
            robot = self.robots.get(self.device_id)
        except DeviceOfflineError:
            return

        if not turn.reply:
            return

        if self.settings.behavior.talking_animation:
            explicit = any(t.name == "show_emotion" and t.ok for t in turn.tool_invocations)
            if not explicit:
                emotion = infer_emotion(turn.reply)
                if emotion is not None and robot.has(CAP_DISPLAY):
                    try:
                        await robot.set_face(emotion, intensity=0.8)
                    except SparkBotError as exc:
                        logger.debug("自动表情失败: %s", exc)

        if not announce:
            return
        # 没有语音合成链路（配置禁用或测试替身）时不播报。
        if self.ctx is None or self.ctx.tts is None:
            logger.info("播报跳过: TTS 未启用（ctx=%s, tts=%s）",
                        self.ctx is not None,
                        getattr(self.ctx, "tts", None) is not None if self.ctx else None)
            return
        if not robot.has(CAP_SPEAKER):
            logger.info("播报跳过: 设备 %s 未声明 speaker 能力", robot.device_id)
            return

        # 这几条日志是排查"文本回复成功但板子没出声"的关键分界点：
        #   * 只有"开始合成"没有"合成完成" → 卡在 TTS 服务
        #   * 有"合成完成"没有"下发完成"   → 卡在设备命令（极可能是断连）
        #   * 有"下发完成"但仍无声          → 设备侧播放/喇叭问题
        # 没有它们时，"play_audio 从未到达设备"到底是没合成还是没发送，
        # 只能靠猜。
        # 长回复必须分段下发：单条 WebSocket 消息有长度上限
        # （约 22 秒 / 95 字音频），超了 PC 端会直接拒发，现象就是
        # "短句有声、背古诗整段没声"。分段后每段都远小于上限。
        segments = split_for_speech(turn.reply)
        if not segments:
            logger.warning("播报跳过: 文本切不出可播报的片段")
            return

        logger.info("播报开始: %d 字 → %d 段（设备=%s）",
                    len(turn.reply), len(segments), robot.device_id)
        try:
            # 走哪条下发路径由配置决定（见 speech.tts_stream_playback）：
            #   * 默认整段下发 —— 老路径，实测音频最干净；
            #   * 打开开关才走流式下发 —— 首字延迟低，但在本板上有可闻杂音。
            if (
                self.settings.speech.tts_stream_playback
                and getattr(self.ctx.tts, "supports_streaming", False)
            ):
                await self._speak_segments(robot, segments, full_text=turn.reply.strip())
            elif len(segments) == 1:
                # 一定要等播完：不等的话 run() 会在喇叭还响着的时候返回，
                # 紧接着的下一轮采集会把机器人自己的声音录成"用户说话"。
                await self._speak_segment(robot, segments[0], wait=True)
            else:
                await self._speak_segments(robot, segments)
        except SparkBotError as exc:
            logger.warning("播报失败: %s", exc.message)
        except Exception as exc:  # noqa: BLE001 - 这里出错不该让对话失败
            logger.exception("播报异常: %s", exc)

    async def _speak_segment(self, robot: Robot, text: str, *, wait: bool = False) -> None:
        """合成一段并下发；``wait=True`` 时等到设备真的播完再返回。

        ``wait`` 默认 False（不阻塞）只适合"后面不会再听麦克风"的场合。
        语音闭环里必须传 True —— 实测不等播完时，下一轮采集会把机器人
        自己的话录进去（第二轮识别出「次好吗？」，正是上一句
        "你再说一次好吗？"的尾巴），于是它开始跟自己聊天。
        """
        t0 = time.monotonic()
        audio, fmt = await self.ctx.tts.synthesize(text)
        logger.info("播报: 合成完成 %d 字节 fmt=%s 耗时=%.0fms（%d 字）",
                    len(audio), fmt, (time.monotonic() - t0) * 1000, len(text))
        if not audio:
            logger.warning("播报跳过: 该段 TTS 返回空音频（%r）", text[:20])
            return

        t1 = time.monotonic()
        await robot.say(audio, fmt=fmt, wait=wait)
        logger.info("播报: 下发完成 耗时=%.0fms（设备已接收%s）",
                    (time.monotonic() - t1) * 1000, "，已播完" if wait else "")

    async def _speak_segments(
        self, robot: Robot, segments: list[str], *, full_text: str | None = None
    ) -> None:
        """流式播报：优先走**真流式**，设备不支持时回退到逐段下发。

        ``full_text`` 是**未分段**的原文。流式路径用它一次合成整段：
        分段本来只是老的整段下发为了绕开单条 WebSocket 消息上限（约 22 秒
        音频）才需要的；流式每块只有几 KB，没有这个限制。而分段会带来一个
        副作用 —— 每段各自算一次响度增益，**段与段之间音量会跳变**，
        跳变在波形上就是一个台阶，听感上是"咔"的一声（用户描述为"滋滋"）。

        回退路径（设备不支持流式播放）仍然用 ``segments``，因为它要受
        单条消息上限约束。
        """
        try:
            await robot.audio_stream_begin()
        except SparkBotError as exc:
            logger.warning("设备不支持流式播放（%s），回退为逐段下发", exc.message)
            await self._speak_segments_sequential(robot, segments)
            return
        await self._speak_streaming(
            robot, full_text or "".join(segments), fallback=segments
        )

    async def _speak_streaming(
        self, robot: Robot, text: str, *, fallback: list[str] | None = None
    ) -> None:
        """真流式播报：**边合成边下发**，设备边收边播。

        两段流水线叠在一起：

        * 合成侧：``tts.synthesize_stream()`` 每算出一小块就交出来；
        * 下发侧：立刻 ``audio_stream_write`` 推给设备。

        所以首字延迟 ≈ **第一块**合成时间，而不是整句合成时间（实测
        8.5 秒的句子：整段 2.9s → 首块 0.55s）。段与段之间不断流，
        背压由设备 64KB 环形缓冲天然提供。
        """
        sent = 0
        started = time.monotonic()
        first_audio_ms: float | None = None
        need_fallback = False

        try:
            wait_read_ms = 0.0
            t_read = time.monotonic()
            async for pcm in self.ctx.tts.synthesize_stream(
                text, sample_rate=STREAM_SAMPLE_RATE
            ):
                # 把"等下一块合成"和"推给设备"分开计时：
                # 前者大 = TTS 供不上；后者大 = 设备侧环形缓冲满（正常背压）。
                # 两者都小却有停顿，说明是 PC 自己的事件循环被别的活儿占住了。
                wait_read_ms = (time.monotonic() - t_read) * 1000
                if first_audio_ms is None:
                    first_audio_ms = (time.monotonic() - started) * 1000
                t_push = time.monotonic()
                for off in range(0, len(pcm), STREAM_CHUNK_BYTES):
                    await robot.audio_stream_write(pcm[off:off + STREAM_CHUNK_BYTES])
                push_ms = (time.monotonic() - t_push) * 1000
                sent += len(pcm)
                logger.info(
                    "播报: 流式推送 %d 字节（累计 %.2fs 音频，等合成 %.0fms，推送 %.0fms）",
                    len(pcm), sent / 2 / STREAM_SAMPLE_RATE, wait_read_ms, push_ms,
                )
                t_read = time.monotonic()
        except ProviderError as exc:
            # 服务端没有流式端点、或合成中途失败 → 回退逐段下发。
            # 已经推出去的部分不会浪费：设备会把它播完，不会静音。
            logger.warning("流式合成失败（%s），回退为逐段下发", exc.message)
            need_fallback = True
        finally:
            # 无论成功失败都要收尾：否则设备会一直停在流式模式，
            # 缓冲播空后不发 audio_done，后续"等播完"的时序全乱。
            with contextlib.suppress(SparkBotError, Exception):
                await robot.audio_stream_end()

        if need_fallback:
            await self._speak_segments_sequential(robot, fallback or [text])
            return

        # 等设备真的播完再返回。不然后续流程（下一轮采集）会在喇叭还在响的
        # 时候就开麦，设备一边播一边上传音频，主循环被挤住 → 推送出现空档
        # → 2 秒环形缓冲抽干 → 断流（用户听到的"说一半滋滋"）。
        done = False
        with contextlib.suppress(Exception):
            done = await robot.wait_audio_done(timeout_s=3.0)

        logger.info(
            "播报: 流式推送完成 %d 字节 / %.2fs 音频，首块=%.0fms，总耗时=%.0fms，播完=%s",
            sent, sent / 2 / STREAM_SAMPLE_RATE,
            first_audio_ms if first_audio_ms is not None else -1.0,
            (time.monotonic() - started) * 1000,
            done,
        )

    async def _speak_segments_sequential(self, robot: Robot, segments: list[str]) -> None:
        """逐段下发（回退路径）：等上一段播完再发下一段。

        固件的 ``play_audio`` 是"新语音打断旧语音"，所以必须等 ``audio_done``；
        代价是段间有一次网络往返的空隙。设备不支持流式时走这条路。

        最后一段同样要等（原来只等倒数第二段）：播报没结束就返回，
        下一轮采集会把机器人自己的声音收进来 —— 见 :meth:`_speak_segment`。
        """
        pending = asyncio.create_task(self.ctx.tts.synthesize(segments[0]))
        for i in range(len(segments)):
            audio, fmt = await pending
            if i + 1 < len(segments):
                # 预取下一段：它与本段的播放并行
                pending = asyncio.create_task(self.ctx.tts.synthesize(segments[i + 1]))

            if not audio:
                logger.warning("播报跳过: 第 %d/%d 段 TTS 返回空音频",
                               i + 1, len(segments))
                continue

            t0 = time.monotonic()
            result = await robot.say(audio, fmt=fmt, wait=True)
            logger.info(
                "播报: 第 %d/%d 段 %d 字节，耗时=%.0fms%s",
                i + 1, len(segments), len(audio), (time.monotonic() - t0) * 1000,
                "（已播完）" if result.get("done") else "（等待超时，按估算时长继续）",
            )

    # ------------------------------------------------------------------ #
    # 流式（供 Web 控制台使用）
    # ------------------------------------------------------------------ #
    async def stream(self, user_text: str, *, images: list[str] | None = None) -> AsyncIterator[dict[str, Any]]:
        """把一轮对话拆成事件流，便于控制台实时展示。

        注意：本方法是 :meth:`run` 的**薄包装**，实际执行仍是一次性的，
        产出的是同一轮的阶段快照而非逐 token 增量——对于「看一秒、
        动一秒」的机器人场景，逐 token 的收益远小于复杂度。
        """
        yield {"type": "start", "text": user_text}
        turn = await self.run(user_text, images=images)
        for invocation in turn.tool_invocations:
            yield {"type": "tool", **invocation.to_dict()}
        yield {"type": "reply", "text": turn.reply, "error": turn.error}
        yield {"type": "done", "duration_ms": turn.duration_ms, "rounds": turn.rounds}

    # ------------------------------------------------------------------ #
    # 维护
    # ------------------------------------------------------------------ #
    def reset(self) -> None:
        """清空对话历史（保留人格设定）。"""
        self.memory.clear()
        self.bus.publish("agent.reset", device_id=self.device_id)

    async def emergency_stop(self) -> None:
        """急停：绕过模型直接刹停底盘。

        这是唯一一条允许跳过 agent 循环的通路——安全逻辑不该等模型回复。
        """
        try:
            robot = self.robots.get(self.device_id)
        except DeviceOfflineError:
            return
        if robot.has(CAP_MOTOR):
            try:
                await robot.stop(emergency=True)
                logger.warning("急停已触发 device=%s", robot.device_id)
            except SparkBotError as exc:
                logger.error("急停失败: %s", exc.message)
