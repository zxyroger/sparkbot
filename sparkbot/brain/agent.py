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
from ..device.capabilities import CAP_DISPLAY, CAP_MOTOR, CAP_SPEAKER, Robot, RobotProvider
from ..device.protocol import Emotion
from ..llm.base import ChatMessage, LLMProvider, LLMResponse, ToolCallRequest, Usage
from ..paths import vendor_dir
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
        self._lock = asyncio.Lock()
        """串行化同一台机器人的对话，避免两轮对话抢同一个底盘。"""

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
        )
        return messages

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
        logger.info("播报开始: 合成 %d 字（设备=%s）", len(turn.reply), robot.device_id)
        try:
            t0 = time.monotonic()
            audio, fmt = await self.ctx.tts.synthesize(turn.reply)
            synth_ms = (time.monotonic() - t0) * 1000
            logger.info("播报: 合成完成 %d 字节 fmt=%s 耗时=%.0fms",
                        len(audio), fmt, synth_ms)
            if not audio:
                logger.warning("播报跳过: TTS 返回空音频")
                return

            t1 = time.monotonic()
            await robot.say(audio, fmt=fmt)
            logger.info("播报: 下发完成 耗时=%.0fms（设备已接收）",
                        (time.monotonic() - t1) * 1000)
        except SparkBotError as exc:
            logger.warning("播报失败: %s", exc.message)
        except Exception as exc:  # noqa: BLE001 - 这里出错不该让对话失败
            logger.exception("播报异常: %s", exc)

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
