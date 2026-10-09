"""运行编排：把网关、感知、工具与 Agent 装配成一个可运行的机器人系统。

分工：
* ``sparkbot.app`` 只管 HTTP 路由；
* 本模块负责**装配与生命周期**——建 provider、注册工具、按设备创建 Agent、
  驱动语音闭环、在关闭时干净地释放资源。

这样拆开的好处是：测试可以直接构造 :class:`SparkBotRuntime`，
不经过任何 HTTP 层就能跑一轮完整对话。
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from dataclasses import dataclass
from typing import Any

from .brain.agent import Agent, AgentTurn, face_db_path, memory_path
from .brain.long_term import get_memory
from .brain.tools import ToolContext, build_registry, tool_catalog
from .config import Settings
from .core.errors import DeviceOfflineError, ProviderError, SparkBotError
from .core.events import EventBus
from .core.tools import ToolRegistry
from .device.capabilities import RobotProvider
from .device.gateway import DeviceGateway
from .device.protocol import Emotion, EventName
from .llm.base import LLMProvider, create_provider
from .perception.face import get_face_db
from .perception.speech import ASRProvider, TTSProvider, create_asr, create_tts
from .perception.vision import VisionAnalyzer

logger = logging.getLogger(__name__)


@dataclass(slots=True)
class RuntimeStatus:
    """系统总体状态，供 ``/api/status`` 使用。"""

    llm_provider: str
    llm_model: str
    vision_provider: str
    asr_provider: str
    tts_provider: str
    tools: int
    devices_online: int
    agents: int
    voice_loop: bool
    uptime_s: float
    #: 当前是否处于「对话态」（唤醒后保持聆听、不必再喊唤醒词）。
    voice_session: bool = False

    def to_dict(self) -> dict[str, Any]:
        """转成可 JSON 序列化字典。"""
        return {
            "llm_provider": self.llm_provider,
            "llm_model": self.llm_model,
            "vision_provider": self.vision_provider,
            "asr_provider": self.asr_provider,
            "tts_provider": self.tts_provider,
            "tools": self.tools,
            "devices_online": self.devices_online,
            "agents": self.agents,
            "voice_loop": self.voice_loop,
            "uptime_s": round(self.uptime_s, 1),
            "voice_session": self.voice_session,
        }


class AgentManager:
    """按设备 id 维护 Agent 实例。

    每台机器人一个 Agent（各自独立的历史与人格上下文），
    但共享同一份 provider 与工具注册表——它们是只读的，无需重复创建。
    """

    def __init__(self, runtime: SparkBotRuntime) -> None:
        self._runtime = runtime
        self._agents: dict[str, Agent] = {}

    def get(self, device_id: str | None = None) -> Agent:
        """取（或创建）某台设备的 Agent。

        Args:
            device_id: 目标设备；``None`` 表示当前唯一在线设备。
        """
        runtime = self._runtime
        if device_id is None:
            robot = runtime.robots.get(None)
            device_id = robot.device_id
        agent = self._agents.get(device_id)
        if agent is None:
            agent = Agent(
                settings=runtime.settings,
                provider=runtime.provider,
                registry=runtime.registry,
                context=runtime.tool_context,
                robots=runtime.robots,
                bus=runtime.bus,
                device_id=device_id,
            )
            self._agents[device_id] = agent
            logger.info("为设备 %s 创建 Agent", device_id)
        return agent

    def all(self) -> dict[str, Agent]:
        """当前全部 Agent。"""
        return dict(self._agents)

    def reset(self, device_id: str | None = None) -> int:
        """清空一台或全部设备的对话历史，返回受影响的数量。"""
        if device_id is not None:
            agent = self._agents.get(device_id)
            if agent is None:
                return 0
            agent.reset()
            return 1
        for agent in self._agents.values():
            agent.reset()
        return len(self._agents)

    def forget(self, device_id: str) -> None:
        """设备下线时丢弃其 Agent，避免注册表无限增长。"""
        self._agents.pop(device_id, None)


class SparkBotRuntime:
    """整个 PC 端服务的装配根。"""

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.bus = EventBus()
        self.started_at = time.time()
        self._voice_task: asyncio.Task[None] | None = None
        #: 是否处于「对话态」（一次唤醒后保持聆听，直到安静超时）。
        #: 只用于状态展示与排查：用户问"现在还要不要喊唤醒词"时看它。
        self.voice_session_active = False

        # --- 设备层 ---------------------------------------------------- #
        self.gateway = DeviceGateway(
            settings, self.bus, server_name=f"sparkbot/{self._version()}"
        )
        self.robots = RobotProvider(self.gateway, settings)

        # --- 模型层 ---------------------------------------------------- #
        self.provider: LLMProvider = create_provider(settings.llm, purpose="chat")
        self.vision: VisionAnalyzer | None = None
        if settings.vision.enabled:
            try:
                self.vision = VisionAnalyzer.from_settings(settings.llm, settings.vision)
            except ProviderError as exc:
                logger.warning("视觉能力不可用: %s", exc.message)

        # --- 语音层 ---------------------------------------------------- #
        self.asr: ASRProvider | None = self._safe_create(create_asr, settings, "ASR")
        self.tts: TTSProvider | None = self._safe_create(create_tts, settings, "TTS")

        # --- 工具与 Agent ---------------------------------------------- #
        self.tool_context = ToolContext(
            robots=self.robots,
            vision=self.vision,
            asr=self.asr,
            tts=self.tts,
            settings=settings,
        )
        self.registry: ToolRegistry = build_registry(self.tool_context)
        self.agents = AgentManager(self)

        logger.info(
            "运行时就绪: llm=%s/%s 工具=%d 视觉=%s ASR=%s TTS=%s",
            self.provider.name,
            getattr(self.provider, "model", "?"),
            len(self.registry),
            "开" if self.vision else "关",
            self.asr.name if self.asr else "关",
            self.tts.name if self.tts else "关",
        )

    # ------------------------------------------------------------------ #
    # 生命周期
    # ------------------------------------------------------------------ #
    @staticmethod
    def _version() -> str:
        """读取包版本，失败时给个占位值。"""
        from . import __version__

        return __version__

    @staticmethod
    def _safe_create(factory: Any, settings: Settings, label: str) -> Any:
        """构造可选的 provider，失败时降级为 ``None`` 而不是让服务起不来。"""
        try:
            return factory(settings.speech)
        except ProviderError as exc:
            logger.warning("%s 不可用: %s", label, exc.message)
            return None

    async def start(self) -> None:
        """启动后台任务：设备看护 + 语音闭环。"""
        await self.gateway.start()
        self.bus.publish("runtime.started")
        if self.asr is not None:
            self._voice_task = asyncio.create_task(self._voice_loop(), name="voice-loop")

    async def stop(self) -> None:
        """关闭所有资源。顺序很重要：先停语音，再断设备，最后放网络句柄。"""
        if self._voice_task is not None:
            self._voice_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._voice_task
            self._voice_task = None

        await self.gateway.stop()

        for closable in (self.vision, self.asr, self.tts, self.provider):
            if closable is None:
                continue
            with contextlib.suppress(Exception):
                await closable.aclose()

        self.bus.publish("runtime.stopped")
        self.bus.close()

    # ------------------------------------------------------------------ #
    # 对话入口
    # ------------------------------------------------------------------ #
    async def chat(
        self,
        text: str,
        *,
        device_id: str | None = None,
        images: list[str] | None = None,
        announce: bool = False,
        allow_tools: bool = True,
    ) -> AgentTurn:
        """从文本（或文本+图片）走一轮完整对话。"""
        agent = self.agents.get(device_id)
        return await agent.run(text, images=images, announce=announce, allow_tools=allow_tools)

    async def transcribe_and_chat(
        self, pcm: bytes, *, device_id: str | None = None, sample_rate: int | None = None
    ) -> tuple[str, AgentTurn | None]:
        """把一段 PCM 音频转成文本并走一轮对话。

        Returns:
            ``(识别文本, 对话结果)``；识别为空时对话结果为 ``None``。
        """
        if self.asr is None:
            raise ProviderError("语音识别未启用")

        rate = sample_rate or self.settings.speech.input_sample_rate
        transcript = await self.asr.transcribe(pcm, sample_rate=rate)
        self.bus.publish(
            "speech.transcribed",
            device_id=device_id,
            text=transcript.text,
            duration_s=transcript.duration_s,
        )
        if not transcript.text.strip():
            return transcript.text, None

        # 语音输入天然应该用语音回应。
        turn = await self.chat(transcript.text, device_id=device_id, announce=True)
        return transcript.text, turn

    # ------------------------------------------------------------------ #
    # 语音闭环
    # ------------------------------------------------------------------ #
    async def _voice_loop(self) -> None:
        """监听设备事件，自动完成「唤醒 → 采集 → 识别 → 回复 → 播报」。

        只对**设备主动上报**的唤醒/按键事件作出反应；文本输入走
        ``/api/chat``，两条入口互不干扰。
        """
        logger.info("语音闭环已启动，等待设备事件")
        triggers = {EventName.WAKE_WORD.value, EventName.BUTTON.value}

        # 只订阅一次，长期复用同一个队列，避免每次唤醒都重建订阅。
        async with self.bus.subscribe() as queue:
            while True:
                try:
                    event = await queue.get()
                    if event.topic != "device.event":
                        continue

                    envelope = _EventEnvelope(event.payload)
                    if envelope.raw.get("event") not in triggers:
                        continue

                    # 回声自触发保护：设备就在喇叭旁边，它**听得见自己说话**。
                    # 播报期间若再响一次唤醒词，这里就会开新一轮采集，
                    # 而新一轮的 audio_stream_begin 会把正在播的语音**硬切掉**
                    # 再插进新的一段 —— 用户听到的就是断续/杂音。
                    #
                    # agent 的锁在整个回合（含播报收尾）期间是持有的，
                    # 所以"锁住 = 这一轮还没说完"，直接忽略这次唤醒即可。
                    agent = self.agents.all().get(envelope.device_id)
                    if agent is not None and agent.busy:
                        logger.info(
                            "设备 %s 正在说话/思考，忽略这次唤醒（回声自触发）",
                            envelope.device_id,
                        )
                        continue

                    await self._handle_voice_turn(envelope.device_id)
                except asyncio.CancelledError:
                    logger.info("语音闭环已停止")
                    raise
                except Exception as exc:  # noqa: BLE001 - 闭环必须永远活着
                    logger.exception("语音闭环异常（将继续运行）: %s", exc)
                    await asyncio.sleep(0.5)


    async def voice_turn(
        self, device_id: str | None = None, *, beep: bool = True
    ) -> dict[str, Any]:
        """完成一次完整的语音交互，并返回各阶段结果。

        Args:
            device_id: 目标设备；``None`` 表示当前唯一在线设备。
            beep: 开始采集前是否"叮"一声提示可以说话了。会话态里只有第一轮
                需要 —— 每轮都响会变成噪音（见 ``_handle_voice_turn``）。

        成功时返回的字典包含：

        * ``stage``   —— 走到哪一步（``done`` / ``no_speech`` / ``empty_text``）
        * ``text``    —— 识别出的文本
        * ``reply``   —— agent 的回复
        * ``recorded_s`` / ``pcm_bytes`` —— 采集到的音频规模
        * ``turn``    —— 完整的 ``AgentTurn`` 信息（工具调用、用量、耗时）
        * ``error``   —— 失败原因（有值时后面几个字段为空）

        失败不抛异常，而是把原因放进 ``error`` —— 语音链路的每一环都可能
        单独坏掉（麦克风、ASR、模型、喇叭），调用方需要知道是**哪一环**坏了，
        而不是只收到一个笼统的失败。
        """
        result: dict[str, Any] = {"stage": "start", "text": "", "reply": ""}

        if self.asr is None:
            result["error"] = "语音识别未启用（SPARKBOT_SPEECH_ASR_PROVIDER=disabled）"
            return result

        try:
            robot = self.robots.get(device_id)
        except DeviceOfflineError as exc:
            result["error"] = exc.message
            return result

        if not robot.has("microphone"):
            result["error"] = f"设备 {robot.device_id} 没有麦克风"
            return result

        logger.info("设备 %s 语音交互：开始采集", robot.device_id)

        # 即时反馈：先"叮"一声，让用户知道可以说话了。
        # 这一声很关键 —— 没有它，用户不知道什么时候该开口。
        if beep and robot.has("speaker"):
            with contextlib.suppress(SparkBotError):
                await robot.play_tone(frequency_hz=880.0, duration_ms=90)

        rate = self.settings.speech.input_sample_rate

        # ---- 流式识别会话（能用就用，用不了自动回退整段识别） -------------- #
        # 为什么要在这里就开：采集是"边收边推"的，识别必须**同时**进行，
        # 等采集完再连就晚了一步。partial 文本会实时发到事件总线，
        # 控制台因此能边听边出字。
        session = None
        if self.asr is not None and getattr(self.asr, "supports_streaming", False):
            try:
                session = await self.asr.open_stream(
                    sample_rate=rate,
                    on_partial=lambda text: self.bus.publish(
                        "speech.partial", device_id=robot.device_id, text=text
                    ),
                )
            except Exception as exc:  # noqa: BLE001 - 服务端没流式端点时回退
                logger.warning("流式识别不可用，回退到整段识别: %s", exc)
                session = None

        try:
            pcm = await robot.collect_audio(
                max_seconds=max(3.0, self.settings.speech.listen_timeout_s + 4.0),
                silence_timeout_s=self.settings.speech.listen_timeout_s,
                on_chunk=session.push if session is not None else None,
            )
        except SparkBotError as exc:
            if session is not None:
                await session.aclose()
            result["error"] = f"采集音频失败: {exc.message}"
            logger.warning(result["error"])
            return result

        streamed_text = ""
        if session is not None:
            try:
                streamed_text = await session.finish()
            except ProviderError as exc:
                logger.warning("流式识别收尾失败，回退整段识别: %s", exc.message)
            finally:
                await session.aclose()

        result["pcm_bytes"] = len(pcm)
        result["recorded_s"] = round(len(pcm) / (rate * 2), 2)
        result["stage"] = "recorded"
        result["chunks"] = getattr(robot, "last_audio_chunks", None)

        # 峰值与 RMS 在这里就算出来并打进日志：它们是判断"人声到底有没有
        # 进麦克风"最直接的两个数 —— 唤醒不灵、识别听不清时先看这两个。
        # 参考值：安静房间底噪 RMS≈100、峰值≈200；对着板子正常说话是几千。
        #
        # 刻意**不用** _peak_amplitude()：它只看开头 0.25 秒，而用户往往
        # 在"叮"一声之后才开口，采样窗口很容易正好错过说话的部分。
        peak_now, rms_now = self._audio_level(pcm)
        logger.info(
            "采集结果: %d 字节 ≈ %.2f 秒（%.0f 帧可识别，麦克风 峰值=%d RMS=%d）",
            len(pcm),
            len(pcm) / (rate * 2),
            len(pcm) / (rate * 2) * 100,
            peak_now,
            rms_now,
        )

        if not pcm:
            result["stage"] = "no_speech"
            result["error"] = (
                "没有采集到音频。排查顺序："
                "1) 板子麦克风是否真的在工作（固件侧 start_listen 是否打开了 ADC）；"
                "2) audio 分片有没有经 WebSocket 上行到 PC（看服务端日志的 device.audio_start/end 事件）；"
                "3) 采集窗口内是否确实有声音"
            )
            logger.warning(result["error"])
            return result

        # 能量检查：全零或极弱说明麦克风没真正工作，与"用户没说话"是两回事。
        peak = peak_now
        result["peak"] = peak
        result["rms"] = rms_now
        if peak < 200:
            result["stage"] = "silent"
            result["error"] = f"采集到的音频几乎全静音（峰值 {peak}）—— 麦克风可能没在工作"
            logger.warning(result["error"])
            return result

        # 语音门槛：电平低于门槛就按静音处理，**不进 ASR、也不回复**。
        #
        # 为什么必须挡：会话态下每轮都会走完整流程，而 ASR 对静音/环境噪声
        # 经常吐出一个句点之类的非空文本，于是机器人会对着一片安静不停
        # 自言自语（实测每 5 秒插一句"我就守在这儿，没动～"）。
        # 按电平挡掉最直接，也顺带省掉一次识别 + 一次模型调用。
        min_rms = max(0, int(self.settings.behavior.speech_min_rms))
        if min_rms and rms_now < min_rms:
            result["stage"] = "silent"
            result["hint"] = f"低于语音门槛（RMS {rms_now} < {min_rms}），按静音处理"
            logger.info(
                "本轮按静音处理（RMS=%d 峰值=%d，未进 ASR）", rms_now, peak
            )
            return result

        try:
            if streamed_text.strip():
                # 流式链路已经出文本：不再重复识别，直接进对话。
                # 这就是流式识别的收益 —— 话一说完就有文本，省掉整段识别的往返。
                text = streamed_text
                turn = await self.chat(text, device_id=robot.device_id, announce=True)
                self.bus.publish(
                    "speech.transcribed",
                    device_id=robot.device_id,
                    text=text,
                    duration_s=round(len(pcm) / (rate * 2), 2),
                    streamed=True,
                )
            else:
                text, turn = await self.transcribe_and_chat(
                    pcm, device_id=robot.device_id, sample_rate=rate
                )
        except ProviderError as exc:
            result["error"] = f"语音识别失败: {exc.message}"
            logger.warning(result["error"])
            return result

        result["text"] = text
        result["stage"] = "transcribed"

        if not text.strip():
            result["stage"] = "empty_text"
            result["error"] = "识别结果为空（没听清 / ASR 没配好）"
            return result

        if turn is not None:
            result["reply"] = turn.reply
            result["turn"] = turn.to_dict()
            result["stage"] = "done"
        else:
            result["stage"] = "no_reply"

        logger.info("语音交互完成: %r → %r", text, result.get("reply", ""))
        return result

    @staticmethod
    def _peak_amplitude(pcm: bytes, *, limit: int = 4000) -> int:
        """取 PCM 的峰值绝对值（只抽样前 limit 个采样，够用且快）。"""
        import array

        usable = len(pcm) - (len(pcm) % 2)
        samples = array.array("h")
        samples.frombytes(pcm[: min(usable, limit * 2)])
        if not samples:
            return 0
        return max(abs(s) for s in samples)

    @staticmethod
    def _audio_level(pcm: bytes) -> tuple[int, int]:
        """算整段 PCM 的 ``(峰值, RMS)``，用来判断人声有没有进麦克风。

        为什么不用 :meth:`_peak_amplitude`：它只看开头 0.25 秒，
        而用户通常等"叮"响过之后才开口，采样窗口很容易整段都是静音，
        于是"电平正常"和"麦克风坏了"分不出来。

        参考值（实测本板 ES8311，输入增益 30 dB）：
        * 安静房间底噪：峰值 ≈200，RMS ≈100
        * 喇叭在 10cm 处放测试音：峰值 ≈2200，RMS ≈1030
        * 人对着板子正常说话：峰值几千（低于 1000 基本就是说得太轻或太远）
        """
        import array
        import math

        usable = len(pcm) - (len(pcm) % 2)
        if usable <= 0:
            return 0, 0
        samples = array.array("h")
        samples.frombytes(pcm[:usable])
        if not samples:
            return 0, 0
        peak = max(abs(s) for s in samples)
        rms = int(math.sqrt(sum(s * s for s in samples) / len(samples)))
        return peak, rms

    async def _handle_voice_turn(self, device_id: str | None) -> None:
        """一次唤醒后的**语音会话**：保持"对话态"，直到安静足够久。

        设备只在收到 ``start_listen`` 时才上传音频，所以"采集 → 识别 →
        回复 → 播报"跑完录音就停了；用户再说第二句时麦克风根本没开，
        自然没有回应 —— 这就是"唤醒后只能对话一句"。
        这里在每轮结束后自动再开一轮采集，把"唤醒态"保持住。

        结束条件（任一满足）：

        * 连续 ``behavior.voice_session_idle_timeout_s`` 秒没听到人声
          （默认 300 秒 = 5 分钟）→ 退出对话态，回到"要喊唤醒词"的状态；
        * 轮数达到 ``behavior.voice_session_turns``（0 = 不限）；
        * 出错（设备掉线等）。

        关键细节："没听清"不等于"没说话"。识别结果为空、但麦克风电平明显
        有语音时，仍然算用户在说话、照样续期 —— 否则一句话被 ASR 听漏，
        会话就当场结束，用户会觉得"又只能问一句"。
        """
        behavior = self.settings.behavior
        idle_timeout = max(0.0, float(behavior.voice_session_idle_timeout_s))
        turn_cap = max(0, int(behavior.voice_session_turns))
        gap = max(0.0, float(behavior.voice_session_gap_s))

        self.voice_session_active = True
        last_voice_at = time.monotonic()
        rounds = 0
        consecutive_errors = 0
        logger.info(
            "语音会话开始（静音 %.1f 秒后退出，轮数上限 %s）",
            idle_timeout,
            turn_cap or "不限",
        )
        try:
            while True:
                # 只有第一轮"叮"一声：会话态里每轮都响会变成噪音。
                result = await self.voice_turn(device_id, beep=(rounds == 0))
                rounds += 1

                if result.get("error"):
                    # 偶发失败（一次上行丢包、设备正忙）不该直接掐掉整段会话，
                    # 连续多次才认输 —— 否则用户会莫名"被退出对话态"。
                    consecutive_errors += 1
                    logger.info(
                        "语音会话第 %d 轮出错（连续 %d 次）：%s",
                        rounds, consecutive_errors, result.get("error"),
                    )
                    if consecutive_errors >= 3:
                        break
                    await asyncio.sleep(gap)
                    continue
                consecutive_errors = 0

                heard = self._heard_speech(result)
                if heard:
                    last_voice_at = time.monotonic()
                elif idle_timeout <= 0:
                    # 老行为：静音一轮就收工（voice_session_turns 模式）
                    if rounds > 1:
                        logger.info("连续对话结束（第 %d 轮没听到人声: %s）",
                                    rounds, result.get("stage"))
                    break

                if turn_cap and rounds >= turn_cap:
                    break

                quiet_for = time.monotonic() - last_voice_at
                if idle_timeout > 0 and quiet_for >= idle_timeout:
                    logger.info(
                        "语音会话结束：已安静 %.0f 秒（共 %d 轮），回到等待唤醒词",
                        quiet_for, rounds,
                    )
                    break

                # 轮间等待：让扬声器把话说完，并给用户反应时间。
                # 不留这段时间的话，麦克风会把机器人自己的声音收进去，
                # 变成"自己跟自己说话"。
                #
                # 但**没听到人声的那一轮不用等**：那一轮根本没有播报，
                # 不存在回声风险；等满 1.2 秒只会制造一段"听不见你说话"的
                # 盲区 —— 用户恰好在这段开口时，前半句就被切掉了。
                await asyncio.sleep(gap if heard else gap * 0.15)
        finally:
            self.voice_session_active = False
            await self._close_voice_session(device_id)

    def _heard_speech(self, result: dict[str, Any]) -> bool:
        """这一轮是否"听到了人在说话"。

        判据有两个，满足其一即可：
        * ASR 出了非空文本 —— 正常情况；
        * 麦克风电平明显高于底噪 —— ASR 听漏了也不算"没人说话"。

        实测本板（ES8311，输入增益 30 dB）：安静房间 RMS≈100，
        人对着板子说话 RMS 几百以上，所以门槛取 300。
        """
        if str(result.get("text") or "").strip():
            return True
        return int(result.get("rms") or 0) >= max(
            0, int(self.settings.behavior.speech_min_rms)
        )

    async def _close_voice_session(self, device_id: str | None) -> None:
        """退出对话态时给一个**看得见**的提示。

        为什么不只打日志：用户在跟机器人说话时看不到日志，会话静默结束
        他只会觉得"怎么又不理我了"。表情切到 sleepy 是个不吵人的信号 ——
        半夜里"叮"一声反而吓人。没有显示屏的设备就只记日志。
        """
        try:
            robot = self.robots.get(device_id)
        except DeviceOfflineError:
            return
        if not robot.has("display"):
            return
        with contextlib.suppress(SparkBotError, DeviceOfflineError):
            await robot.set_face(Emotion.SLEEPY, intensity=0.6)
            logger.info("对话态已关闭：表情切到 sleepy，等待下一次唤醒")

    # ------------------------------------------------------------------ #
    # 维护
    # ------------------------------------------------------------------ #
    async def emergency_stop(self) -> list[str]:
        """对所有在线设备触发急停，返回被停止的设备 id。"""
        stopped: list[str] = []
        for robot in (self.robots.get(conn.device_id) for conn in self.gateway.all()):
            with contextlib.suppress(SparkBotError):
                await robot.stop(emergency=True)
                stopped.append(robot.device_id)
        # 同时清掉各 Agent 的在途动作记录，避免历史里残留「正在移动」。
        for agent in self.agents.all().values():
            agent.memory.trim_for_retry()
        logger.warning("急停完成: %s", stopped)
        return stopped

    # ------------------------------------------------------------------ #
    # 运行期重配置
    # ------------------------------------------------------------------ #
    def apply_config(self, settings: Settings, *, record: bool = True) -> dict[str, Any]:
        """应用新的配置并**重建受影响的 provider**。

        背景：``SparkBotRuntime.__init__`` 把 provider 实例化了，配置改了
        但实例不变就等于没改。所以这里重建 LLM / 视觉 / ASR / TTS，
        并把这些对象的引用同步给已存在的 Agent 和工具上下文。

        可热切换的是「模型与语音」这类无状态依赖；
        监听端口、CORS 等启动期设置不在此列（见 ``config.EDITABLE_FIELDS``）。
        """
        self.settings = settings

        # --- 重建 LLM provider ---------------------------------------- #
        try:
            self.provider = create_provider(settings.llm, purpose="chat")
        except ProviderError as exc:
            raise SparkBotError(f"LLM 配置无效: {exc.message}") from exc

        # --- 重建视觉 analyzer ---------------------------------------- #
        self.vision = None
        if settings.vision.enabled:
            try:
                self.vision = VisionAnalyzer.from_settings(settings.llm, settings.vision)
            except ProviderError as exc:
                logger.warning("视觉能力不可用: %s", exc.message)

        # --- 重建语音链路（配置禁用时置空） ---------------------------- #
        self.asr = self._safe_create(create_asr, settings, "ASR")
        self.tts = self._safe_create(create_tts, settings, "TTS")

        # --- 同步给工具上下文与所有 Agent ------------------------------ #
        self.tool_context.robots = self.robots
        self.tool_context.vision = self.vision
        self.tool_context.asr = self.asr
        self.tool_context.tts = self.tts
        self.tool_context.settings = settings

        # --- 长期记忆 -------------------------------------------------- #
        # 记忆库是"有状态"的，所以不能像 provider 那样每次重建 ——
        # 重建会把刚记住的事实丢掉。这里只在**首次**初始化时创建，
        # 之后仅同步开关与容量；关闭开关时置空，工具会明确告诉模型
        # "记不住"，而不是默默假装记住了。
        memory = None
        if settings.memory.enabled:
            memory = get_memory(
                memory_path(settings),
                capacity=settings.memory.capacity,
                enabled=True,
            )
            # 容量可能被改小，需要按新上限裁剪
            memory.capacity = max(10, settings.memory.capacity)
        self.tool_context.long_term = memory

        # --- 人脸库 ---------------------------------------------------- #
        # 与长期记忆同理：它是**有状态**的（存着谁是谁），不能每次重建，
        # 否则刚绑定的名字会消失。取单例，只同步开关与阈值。
        face_db = None
        if settings.face.enabled:
            try:
                face_db = get_face_db(
                    face_db_path(settings),
                    threshold=settings.face.threshold,
                    max_samples=settings.face.max_samples,
                    enabled=True,
                )
            except Exception:  # noqa: BLE001 - 人脸库坏了不该让服务起不来
                logger.exception("人脸库初始化失败，本次将以无人脸识别模式运行")
        self.tool_context.face_db = face_db

        for agent in self.agents.all().values():
            agent.settings = settings
            agent.provider = self.provider
            agent.ctx = self.tool_context
            agent.long_term = memory
            agent.face_db = face_db

        if record:
            self.bus.publish(
                "runtime.reconfigured",
                llm_provider=self.provider.name,
                llm_model=getattr(self.provider, "model", "?"),
                vision=bool(self.vision),
                asr=self.asr.name if self.asr else None,
                tts=self.tts.name if self.tts else None,
            )

        logger.info(
            "配置已热更新: llm=%s/%s 视觉=%s ASR=%s TTS=%s",
            self.provider.name,
            getattr(self.provider, "model", "?"),
            "开" if self.vision else "关",
            self.asr.name if self.asr else "关",
            self.tts.name if self.tts else "关",
        )
        return self.status().to_dict()

    async def test_llm(self) -> dict[str, Any]:
        """对当前 LLM 做一次最小连通性测试，供控制台「测试连接」按钮使用。

        刻意不带工具、只要一个字的输出——目标是验证
        「key / base_url / 模型名」这三件事是否配对，而不是评测模型能力。
        """
        from .llm.base import ChatMessage as _Message

        started = time.perf_counter()
        try:
            response = await self.provider.chat(
                [_Message.user("请只回复两个字：收到")],
                tools=None,
                temperature=0.0,
                # 给足预算：deepseek-flash 是**推理模型**，会先输出
                # reasoning_content，再输出正文。上限给到 32 时预算会被
                # 推理吃光（finish_reason=length，content 为空串），
                # 控制台的「测试连接」就变成"连通成功但回复为空"，像坏了。
                # 256 对"收到"两个字绰绰有余，成本可忽略。
                max_tokens=256,
            )
        except ProviderError as exc:
            return {
                "ok": False,
                "error": exc.message,
                "provider": self.provider.name,
                "model": str(getattr(self.provider, "model", "?")),
            }

        return {
            "ok": True,
            "provider": self.provider.name,
            "model": response.model or str(getattr(self.provider, "model", "?")),
            "reply": (response.content or "").strip()[:60],
            "latency_ms": int((time.perf_counter() - started) * 1000),
            "usage": {
                "prompt_tokens": response.usage.prompt_tokens,
                "completion_tokens": response.usage.completion_tokens,
            },
        }

    def status(self) -> RuntimeStatus:
        """汇总当前运行状态。"""
        vision_provider = "off"
        if self.vision is not None:
            vision_provider = self.vision.provider.name
        return RuntimeStatus(
            llm_provider=self.provider.name,
            llm_model=str(getattr(self.provider, "model", "unknown")),
            vision_provider=vision_provider,
            asr_provider=self.asr.name if self.asr else "off",
            tts_provider=self.tts.name if self.tts else "off",
            tools=len(self.registry),
            devices_online=self.gateway.connected_count,
            agents=len(self.agents.all()),
            voice_loop=self._voice_task is not None and not self._voice_task.done(),
            uptime_s=time.time() - self.started_at,
            voice_session=self.voice_session_active,
        )

    def tools_catalog(self) -> list[dict[str, Any]]:
        """导出工具清单（含参数字段），用于自动生成前端表单与文档。"""
        return tool_catalog(self.registry)


class _EventEnvelope:
    """把总线事件包装成与协议信封同形的对象，便于语音闭环统一处理。"""

    __slots__ = ("raw",)

    def __init__(self, payload: dict[str, Any]) -> None:
        self.raw = {
            "event": payload.get("event"),
            "data": payload.get("data") or {},
            "_device_id": payload.get("device_id"),
        }

    @property
    def device_id(self) -> str | None:
        """事件来源设备 id。"""
        return self.raw.get("_device_id")
