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

#: 判定"有人在说话"需要的**有效语音分片数**（每片 20ms → 8 片 = 160ms）。
#:
#: 为什么用"片数"而不是整段 RMS：一个 3.6 秒的采集窗口里，用户往往只说
#: 一两秒，整段 RMS 会被静音稀释 —— 实测用户正常说"峰值 5742 / RMS 140"，
#: 而安静房间本身就"峰值 1444 / RMS 100"，两者在 RMS 上几乎分不开，
#: 结果就是**用户说了话却被判成静音、机器人没反应**（用户原话："等好久没反应"）。
#: 分片级别的能量计数没有这个问题：说话会连续几百片超门槛，瞬态噪声只有几片。
VOICE_CHUNKS_MIN = 8


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
        self,
        device_id: str | None = None,
        *,
        beep: bool = True,
        listening: bool = False,
    ) -> dict[str, Any]:
        """完成一次完整的语音交互，并返回各阶段结果。

        Args:
            device_id: 目标设备；``None`` 表示当前唯一在线设备。
            beep: 开始采集前是否"叮"一声提示可以说话了。会话态里只有第一轮
                需要 —— 每轮都响会变成噪音（见 ``_handle_voice_turn``）。
            listening: 设备的**音频上行是否已经开着**。连续对话时会话只在
                开始时开一次上行，之后每轮都传 ``True`` —— 这样麦克风不会
                被周期性开关（开关一次就是一次"听不见"的盲区）。

        成功时返回的字典包含：

        * ``stage``   —— 走到哪一步（``done`` / ``no_speech`` / ``empty_text``）
        * ``text``    —— 识别出的文本
        * ``reply``   —— agent 的回复
        * ``mic_open``—— 返回时上行是否还开着（静音轮保持开着，
                        要播报前会被关掉）
        * ``recorded_s`` / ``pcm_bytes`` —— 采集到的音频规模
        * ``turn``    —— 完整的 ``AgentTurn`` 信息（工具调用、用量、耗时）
        * ``error``   —— 失败原因（有值时后面几个字段为空）

        失败不抛异常，而是把原因放进 ``error`` —— 语音链路的每一环都可能
        单独坏掉（麦克风、ASR、模型、喇叭），调用方需要知道是**哪一环**坏了，
        而不是只收到一个笼统的失败。
        """
        result: dict[str, Any] = {
            "stage": "start",
            "text": "",
            "reply": "",
            # 出错时也要如实告诉调用方上行是开是关，否则会话结束时
            # 会漏掉一次 stop_listen，设备那边一直往下推音频。
            "mic_open": bool(listening),
        }

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
            # 让"叮"的尾音先散掉再开采集。
            # 不等的后果实测过：连续采集下第一轮的电平被自己的提示音顶到
            # RMS≈900 / 峰值≈10000，直接被判成"有人在说话"，机器人回一句
            # 莫名其妙的话。（这些数看采集结果那行日志就能对上。）
            # 0.5 秒是量出来的：留 0.3 秒时第一轮 RMS 还有 241，紧贴 250 的门槛，
            # 再遇到回声大一点的房间就会误判。
            await asyncio.sleep(0.5)

        rate = self.settings.speech.input_sample_rate

        # ---- 音频上行：连续对话时**只在会话开始时开一次** ------------------ #
        # 设备侧麦克风本来就是常开的（唤醒词要一直听），start_listen 只是
        # 打开"音频上行"。周期性开关上行有两个坏处：每次开关都是一段
        # "听不见"的盲区（用户刚好在这时开口，前半句就没了），而且设备
        # 每轮都要发 start/end 标记。所以这里默认把它开着，直到要播报时才关。
        if not listening:
            try:
                await robot.start_listen(timeout_ms=self._listen_window_ms())
                # 丢掉打开瞬间可能残留的旧分片（上一次会话的尾巴）
                await self._drain_device_audio(robot)
            except SparkBotError as exc:
                result["error"] = f"打开麦克风失败: {exc.message}"
                logger.warning(result["error"])
                return result
            result["mic_open"] = True

        # ---- 流式识别会话：**听到人声才连** -------------------------------- #
        # 连续采集模式下静音轮很多（每 1.5 秒一轮），如果每轮都连一次 ASR，
        # 服务端会被一堆空会话淹没。所以先只在本地缓存分片，第一次出现人声
        # 能量时才连 ASR，并把本轮**从头攒下的分片**补进去 —— 一个词都不丢；
        # 静音轮则完全不连 ASR、不进模型、不花钱。
        session = None
        buffered: list[bytes] = []
        voice_chunks = 0

        async def feed(chunk: bytes) -> None:
            """采集回调（在采集协程里被 await，所以可以安全地连 ASR）。"""
            nonlocal session, voice_chunks
            if session is not None:
                await session.push(chunk)
                return

            buffered.append(chunk)
            if self._chunk_has_voice(chunk):
                voice_chunks += 1
            else:
                # 静音期间只留最近 1 秒，避免长静音把内存撑大
                while sum(len(item) for item in buffered) > rate * 2:
                    buffered.pop(0)
                return

            # **连续**够多片才算"有人在说话"。
            #
            # 不能一有尖峰就连：键盘敲一下、风扇抖一下都会让某一两片超过
            # 能量门槛（实测安静窗口的峰值能到 1444~6871，但 RMS 只有 100 上下），
            # 那样每个窗口都会连一次 ASR，既浪费又把服务端拖满。
            # 一个分片 20ms，8 片 = 160ms —— 人说话远不止这么长，
            # 而一次瞬态噪声远不到。
            if voice_chunks < VOICE_CHUNKS_MIN:
                return

            if self.asr is None or not getattr(self.asr, "supports_streaming", False):
                return  # 没有流式识别：留给后面的整段识别兜底
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
                return
            for item in buffered:
                await session.push(item)
            buffered.clear()

        try:
            pcm = await robot.collect_audio(
                max_seconds=max(3.0, self.settings.speech.listen_timeout_s + 4.0),
                silence_timeout_s=self.settings.speech.listen_timeout_s,
                # 上行已经开着，这里不再重复 start_listen / stop_listen ——
                # 开关时机由会话层统一决定（见 _handle_voice_turn）。
                start=False,
                on_chunk=feed,
            )
        except SparkBotError as exc:
            if session is not None:
                await session.aclose()
            await self._stop_device_audio(robot)
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
            await self._stop_device_audio(robot)
            result["stage"] = "no_speech"
            result["error"] = (
                "没有采集到音频。排查顺序："
                "1) 板子麦克风是否真的在工作（固件侧 start_listen 是否打开了 ADC）；"
                "2) audio 分片有没有经 WebSocket 上行到 PC（看服务端日志的 device.audio_start/end 事件）；"
                "3) 采集窗口内是否确实有声音"
            )
            logger.warning(result["error"])
            return result

        result["peak"] = peak_now
        result["rms"] = rms_now
        result["voice_chunks"] = voice_chunks

        # 全零 = 麦克风真的没在工作，与"用户没说话"是两回事。
        # 注意门槛从"峰值 <200"改成"== 0"：连续采集下每轮只有 ~1.5 秒，
        # 安静时峰值本来就接近 200，再拿 200 当门槛会把正常静音误判成故障。
        if peak_now == 0:
            await self._stop_device_audio(robot)
            result["stage"] = "silent"
            result["error"] = "采集到的音频全是 0 —— 麦克风可能没在工作"
            logger.warning(result["error"])
            return result

        # 语音门槛：电平低于门槛就按静音处理，**不进 ASR、也不回复**。
        #
        # 为什么必须挡：会话态下每轮都会走完整流程，而 ASR 对静音/环境噪声
        # 经常吐出一个句点之类的非空文本，于是机器人会对着一片安静不停
        # 自言自语（实测每 5 秒插一句"我就守在这儿，没动～"）。
        # 按电平挡掉最直接，也顺带省掉一次识别 + 一次模型调用。
        # "有人说话"的判据：**分片级能量计数为主，整段 RMS 为辅**。
        # 只看 RMS 会把"说了几句、中间有停顿"的正常语音判成静音（见
        # VOICE_CHUNKS_MIN 的说明）；只看片数又可能被长时间的低频噪声蒙到，
        # 所以两条任一成立就算听到了。
        min_rms = max(0, int(self.settings.behavior.speech_min_rms))
        heard = voice_chunks >= VOICE_CHUNKS_MIN or bool(min_rms and rms_now >= min_rms)
        if not heard:
            result["stage"] = "silent"
            result["hint"] = (
                f"没听到人声（有效语音分片 {voice_chunks}/{VOICE_CHUNKS_MIN}，"
                f"RMS={rms_now}），按静音处理"
            )
            # **保持上行**：连续采集的关键 —— 静音期间不关麦克风，
            # 下一轮立刻接着听，用户不会遇到"刚好开口时前半句被吃掉"。
            result["mic_open"] = True
            logger.info(
                "本轮按静音处理（有效语音分片 %d/%d，RMS=%d 峰值=%d，未进 ASR）",
                voice_chunks, VOICE_CHUNKS_MIN, rms_now, peak_now,
            )
            return result

        # 有人说话：**先关上行再识别/回复/播报**。
        # 播报时必须关：设备就贴着喇叭，不关就会把自己的声音录进来
        # （实测第二轮识别出"次好吗？"，正是上一句的尾巴）。
        await self._stop_device_audio(robot)
        result["mic_open"] = False

        # 流式识别给了文本、但**没有实词**（"."、"。。" 这类）时，用整段识别
        # 复核一遍再决定。ASR 面对噪声常常吐出这种"非空但没内容"的结果，
        # 直接喂给模型它就会顺着往下编（实测机器人对着空气答"嗯，我在听着呢"）。
        if streamed_text.strip() and not self._text_is_meaningful(streamed_text):
            logger.info("流式识别结果没有实词（%r），用整段识别复核", streamed_text[:16])
            with contextlib.suppress(SparkBotError):
                confirm = await self.asr.transcribe(pcm, sample_rate=rate)
                if self._text_is_meaningful(confirm.text):
                    streamed_text = confirm.text

        if streamed_text.strip() and not self._text_is_meaningful(streamed_text):
            # 复核后仍然没有实词：当成"没听清"，不回复、继续听。
            # 这里**不设置 error**：会话继续，不打断"保持唤醒"。
            result["stage"] = "garbled"
            result["hint"] = f"识别结果没有实词（{streamed_text[:16]!r}），不回复"
            logger.info("本轮按「没听清」处理：%r（不上报给模型）", streamed_text[:24])
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

    @staticmethod
    def _text_is_meaningful(text: str) -> bool:
        """识别结果里有没有**实词**（至少一个汉字或字母数字）。

        为什么不只看"非空"：ASR 面对噪声/静音经常回一个句点（"."）、
        省略号之类 —— 那是非空文本，喂给模型它就会顺着编一句
        （实测机器人对着空气答"嗯，我在听着呢，慢慢说～"）。
        判据刻意放宽到"一个字符"：用户答"好""嗯"都算数。
        """
        for ch in text or "":
            if ch.isalnum() or "\u4e00" <= ch <= "\u9fff":
                return True
        return False

    @staticmethod
    def _chunk_has_voice(pcm: bytes, *, threshold: int = 500) -> bool:
        """单个分片里有没有语音能量（峰值门槛 500）。

        与 ``Robot._has_voice_energy`` 同一判据，但放在 Runtime 里：
        采集回调是 runtime 的对象，不该去引用另一个类的私有方法
        （踩过 —— 回调里抛 NameError 会被采集循环吞成一行 warning，
        结果"永远听不到人声"，排查起来极绕）。
        """
        import array

        if len(pcm) < 4:
            return False
        samples = array.array("h")
        samples.frombytes(pcm[: len(pcm) - (len(pcm) % 2)])
        if not samples:
            return False
        return max(abs(s) for s in samples) >= threshold

    def _listen_window_ms(self) -> int:
        """设备侧一次 ``start_listen`` 的超时（毫秒）。

        连续对话时上行**一次开到底**，所以这个窗口要盖住整段会话。
        设备到点会自己结束并发 ``listen_timeout``，我们也会显式 stop，
        所以给足余量即可；上限 30 分钟，防止配置写错把设备锁死。
        """
        idle = max(0.0, float(self.settings.behavior.voice_session_idle_timeout_s))
        window_s = max(60.0, idle + 60.0)
        return int(min(window_s, 1800.0) * 1000)

    async def _drain_device_audio(self, robot: Robot) -> None:
        """丢掉设备已经推上来、但上一轮没用到的陈旧分片。

        刚打开上行时队列里可能还躺着上一次会话的尾巴，不清掉就会被当成
        "用户说的第一句话"。
        """
        with contextlib.suppress(Exception):
            await robot._drain_audio()  # noqa: SLF001 - 同包内部方法

    async def _stop_device_audio(self, robot: Robot) -> None:
        """关掉音频上行（播报前 / 会话结束时）。

        忘记关的后果很具体：设备会一直把录音推上来而 PC 不再消费，
        白占 WiFi，而且下次打开上行时队列里全是陈旧音频。
        """
        with contextlib.suppress(SparkBotError, DeviceOfflineError):
            await robot.stop_listen()

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
        #: 设备音频上行是否开着。连续采集的核心：**静音期间保持开着**，
        #: 只有要播报时才关，播完再开。开关一次就是一次"听不见"的盲区。
        mic_open = False
        logger.info(
            "语音会话开始（静音 %.1f 秒后退出，轮数上限 %s）",
            idle_timeout,
            turn_cap or "不限",
        )
        try:
            while True:
                # 只有第一轮"叮"一声：会话态里每轮都响会变成噪音。
                try:
                    result = await self.voice_turn(
                        device_id, beep=(rounds == 0), listening=mic_open
                    )
                except asyncio.CancelledError:
                    raise
                except Exception as exc:  # noqa: BLE001
                    # 一轮里的**意外**（代码 bug、协议解析失败）不该让整段会话
                    # 死在无人区：/api/voice/trigger 是 fire-and-forget 任务，
                    # 异常只会变成一条 "Task exception was never retrieved"。
                    # 这里记全栈并收工，日志里能直接看到是哪一行。
                    logger.exception("语音会话第 %d 轮异常，结束本次会话: %s", rounds + 1, exc)
                    break
                rounds += 1
                mic_open = bool(result.get("mic_open"))

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
                    mic_open = False
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

                # 只有**播报过**的那一轮才需要等：那一轮麦克风刚被关掉，
                # 要留出时间让扬声器把话说完，否则会把自己的声音收进来。
                # 静音轮既没有播报、上行也一直开着，立刻接着听就行 ——
                # 这一等一开就是用户"刚开口前半句被吃掉"的根源。
                if heard:
                    await asyncio.sleep(gap)
        finally:
            self.voice_session_active = False
            if mic_open:
                with contextlib.suppress(Exception):
                    await self._stop_device_audio(self.robots.get(device_id))
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
        if int(result.get("voice_chunks") or 0) >= VOICE_CHUNKS_MIN:
            return True
        return int(result.get("rms") or 0) >= max(0, int(self.settings.behavior.speech_min_rms))

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
