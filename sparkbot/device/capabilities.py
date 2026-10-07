"""能力层：把原始协议动作包装成「机器人语义」。

``Robot`` 是 agent 工具与设备协议之间的适配器。它只做三件事：

1. **能力校验** —— 先查 ``hello.capabilities``，不支持就立刻给出清晰错误，
   而不是发一条注定失败的指令等超时；
2. **参数归一化与安全钳制** —— 把速度、时长、表情名收敛到合法范围；
3. **返回结构化结果** —— 上层工具直接把返回值喂给模型，无需再解析 JSON 信封。

它刻意**不**包含任何决策逻辑；「什么时候该往前走」永远由 agent 决定。
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import io
import logging
import time
import wave
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

from ..config import Settings
from ..core.errors import DeviceError, DeviceOfflineError, SparkBotError
from .gateway import DeviceConnection, DeviceGateway, Frame
from .protocol import Action, EventName, Emotion

logger = logging.getLogger(__name__)

#: ``hello.capabilities`` 中可能出现的标准能力名。
CAP_CAMERA = "camera"
CAP_MICROPHONE = "microphone"
CAP_SPEAKER = "speaker"
CAP_DISPLAY = "display"
CAP_MOTOR = "motor"

#: 设备未声明支持某字节时，退回该表情名（保证显示层永远有东西可画）。
FALLBACK_EMOTION = Emotion.NEUTRAL

#: 正在采集音频的设备。麦克风是独占资源：语音闭环与 listen 工具若同时
#: 消费同一个音频队列，会把音频抢成碎片，两边都识别不出内容。
#: 放在模块级而不是实例级，是因为同一个 Gateway 会为同一条连接
#: 创建多个 Robot 门面对象，实例级标志挡不住它们。
_RECORDING: set[str] = set()


class CapabilityError(SparkBotError):
    """设备不支持所请求的能力。"""


@dataclass(slots=True)
class MotionResult:
    """一次运动指令的执行结果。"""

    linear: float
    angular: float
    duration_ms: int
    accepted: bool = True

    def to_dict(self) -> dict[str, Any]:
        """转成给模型看的字典。"""
        direction = "前进" if self.linear > 0 else "后退" if self.linear < 0 else "原地"
        turn = "左转" if self.angular > 0 else "右转" if self.angular < 0 else "直行"
        return {
            "ok": self.accepted,
            "linear_mps": self.linear,
            "angular_rps": self.angular,
            "duration_ms": self.duration_ms,
            "summary": f"{direction}{turn}，线速度 {self.linear:.2f} m/s，角速度 {self.angular:.2f} rad/s，持续 {self.duration_ms} ms",
        }


@dataclass(slots=True)
class LookResult:
    """一次抓帧的结果，同时携带原始字节与可回喂模型的可读描述。"""

    frame: Frame
    data_uri: str
    width: int | None
    height: int | None

    def to_dict(self, *, include_image: bool = False) -> dict[str, Any]:
        """转成给模型看的字典；默认不重复塞 base64 以免撑爆上下文。"""
        payload: dict[str, Any] = {
            "ok": True,
            "format": self.frame.fmt,
            "width": self.width,
            "height": self.height,
            "approx_kb": self.frame.approx_kb,
            "note": "图像已捕获，内容请调用 describe_view 或直接由视觉模型分析",
        }
        if include_image:
            payload["image_data_uri"] = self.data_uri
        return payload


def audio_duration_s(audio: bytes, fmt: str, sample_rate: int | None = None) -> float | None:
    """算出一段音频的**精确**播放时长（秒）；算不出返回 ``None``。

    WAV 直接读头里的帧数÷采样率，比"按字节数估算"准得多 ——
    分段播报要靠它衔接，估算偏一点就会出现抢拍或空档。
    """
    name = (fmt or "").lower()
    if name == "wav":
        try:
            with wave.open(io.BytesIO(audio)) as handle:
                return handle.getnframes() / float(handle.getframerate())
        except (wave.Error, OSError, ZeroDivisionError):
            return None
    if name in ("pcm_s16le", "pcm"):
        rate = sample_rate or 16000
        return (len(audio) / 2) / float(rate)
    return None


class Robot:
    """一台在线机器人的能力门面。

    典型用法::

        robot = gateway.resolve()          # 单机器人场景
        await robot.drive(0.3, 0.0, 1000)  # 前进一秒
        await robot.set_face(Emotion.HAPPY)
        view = await robot.look()          # 抓一帧
    """

    def __init__(self, conn: DeviceConnection, settings: Settings) -> None:
        self.conn = conn
        self.settings = settings
        self._last_motion_at: float = 0.0

    # ------------------------------------------------------------------ #
    # 基础属性
    # ------------------------------------------------------------------ #
    @property
    def device_id(self) -> str:
        """设备唯一 id。"""
        return self.conn.device_id

    @property
    def name(self) -> str:
        """设备自称的名字，取不到就用 id。"""
        info = self.conn.info
        return (info.name if info and info.name else self.device_id)

    @property
    def capabilities(self) -> frozenset[str]:
        """设备声明的能力集合。"""
        info = self.conn.info
        return info.capabilities if info else frozenset()

    def has(self, capability: str) -> bool:
        """是否具备某能力。"""
        return capability in self.capabilities

    def require(self, capability: str) -> None:
        """断言具备某能力。

        Raises:
            CapabilityError: 设备没有声明该能力。
        """
        if not self.has(capability):
            raise CapabilityError(
                f"设备 {self.device_id} 不支持「{capability}」能力",
                device_id=self.device_id,
                capability=capability,
                available=sorted(self.capabilities),
            )

    @property
    def battery(self) -> dict[str, Any]:
        """电量信息，取不到返回空字典。"""
        telemetry = self.conn.telemetry.get("battery")
        return telemetry if isinstance(telemetry, dict) else {}

    @property
    def motion(self) -> dict[str, float]:
        """当前运动状态 ``{"linear": ..., "angular": ...}``。"""
        return dict(self.conn.motion)

    def status(self) -> dict[str, Any]:
        """给模型/控制台看的单行状态摘要。"""
        battery = self.battery
        return {
            "device_id": self.device_id,
            "name": self.name,
            "capabilities": sorted(self.capabilities),
            "battery_percent": battery.get("percent"),
            "battery_voltage": battery.get("voltage"),
            "motion": self.motion,
            "last_seen_ago_s": round(time.time() - self.conn.last_seen, 1),
        }

    # ------------------------------------------------------------------ #
    # 安全钳制
    # ------------------------------------------------------------------ #
    def _clamp_motion(self, linear: float, angular: float) -> tuple[float, float]:
        """把速度钳到配置上限内，并尊重设备自己声明的物理上限。"""
        cfg = self.settings.behavior
        max_linear = cfg.max_linear_mps
        max_angular = cfg.max_angular_rps

        info = self.conn.info
        if info and info.motor:
            with contextlib.suppress(TypeError, ValueError):
                max_linear = min(max_linear, float(info.motor.get("max_linear", max_linear)))
            with contextlib.suppress(TypeError, ValueError):
                max_angular = min(max_angular, float(info.motor.get("max_angular", max_angular)))

        clamped_linear = max(-max_linear, min(max_linear, float(linear)))
        clamped_angular = max(-max_angular, min(max_angular, float(angular)))
        if clamped_linear != linear or clamped_angular != angular:
            logger.debug(
                "速度被钳制: (%.2f, %.2f) -> (%.2f, %.2f)",
                linear, angular, clamped_linear, clamped_angular,
            )
        return clamped_linear, clamped_angular

    def _guard_rate(self) -> None:
        """限制运动指令频率，避免高频抖动损伤电机与齿轮箱。

        Raises:
            SparkBotError: 距离上次运动指令过近。
        """
        if not self.settings.behavior.safety_enabled:
            return
        min_interval = self.settings.behavior.min_command_interval_ms / 1000.0
        elapsed = time.time() - self._last_motion_at
        if self._last_motion_at and elapsed < min_interval:
            raise SparkBotError(
                f"运动指令过于频繁，请间隔至少 {self.settings.behavior.min_command_interval_ms} ms",
                elapsed_ms=int(elapsed * 1000),
            )

    # ------------------------------------------------------------------ #
    # 底盘运动
    # ------------------------------------------------------------------ #
    async def drive(
        self,
        linear: float,
        angular: float = 0.0,
        duration_ms: int = 800,
        *,
        respect_safety: bool = True,
    ) -> MotionResult:
        """让底盘按给定线/角速度移动一段时间。

        Args:
            linear: 前进速度 m/s，负数表示后退。
            angular: 转向角速度 rad/s，正数左转（逆时针）。
            duration_ms: 持续时间毫秒，``0`` 表示持续到收到下一条运动指令。
            respect_safety: 是否套用速度钳制与频率限制。
        """
        self.require(CAP_MOTOR)

        if respect_safety:
            linear, angular = self._clamp_motion(linear, angular)
            self._guard_rate()
            limit = self.settings.behavior.max_duration_ms
            if duration_ms > limit:
                logger.debug("时长被钳制: %d -> %d ms", duration_ms, limit)
                duration_ms = limit

        duration_ms = max(0, int(duration_ms))
        await self.conn.command(
            Action.DRIVE,
            {"linear": linear, "angular": angular, "duration_ms": duration_ms},
            # 开环定时动作：给足「指令时长 + 收尾时间」再判超时。
            timeout_ms=max(self.settings.device.command_timeout_ms, duration_ms + 1500),
        )
        self._last_motion_at = time.time()
        self.conn.motion = {"linear": linear, "angular": angular}
        return MotionResult(linear=linear, angular=angular, duration_ms=duration_ms)

    async def stop(self, *, emergency: bool = False) -> None:
        """立即停止底盘。

        Args:
            emergency: 为 ``True`` 时走 intent 通道（不等待确认），用于急停。
        """
        self.require(CAP_MOTOR)
        if emergency:
            await self.conn.intent(Action.STOP, {"emergency": True})
        else:
            await self.conn.command(Action.STOP, {})
        self._last_motion_at = time.time()
        self.conn.motion = {"linear": 0.0, "angular": 0.0}

    async def turn_in_place(self, angular: float, duration_ms: int = 600) -> MotionResult:
        """原地转向的语法糖：线速度固定为 0。"""
        return await self.drive(0.0, angular, duration_ms)

    async def forward(self, distance_m: float = 0.3, speed_mps: float = 0.25) -> MotionResult:
        """按「走多远」而不是「走多久」移动，便于上层用自然语言描述。

        时长由距离换算，速度取正数（自动取绝对值方向）。
        """
        self.require(CAP_MOTOR)
        speed = max(0.05, abs(speed_mps))
        direction = 1.0 if distance_m >= 0 else -1.0
        duration_ms = int(abs(distance_m) / speed * 1000)
        return await self.drive(direction * speed, 0.0, duration_ms)

    async def set_motion_limits(
        self, *, max_linear: float | None = None, max_angular: float | None = None
    ) -> None:
        """把软限幅下发给设备，让固件侧也做一层保护。"""
        self.require(CAP_MOTOR)
        params: dict[str, Any] = {}
        if max_linear is not None:
            params["max_linear"] = float(max_linear)
        if max_angular is not None:
            params["max_angular"] = float(max_angular)
        if params:
            await self.conn.command(Action.SET_MOTION_LIMITS, params)

    # ------------------------------------------------------------------ #
    # 显示 / 表情
    # ------------------------------------------------------------------ #
    async def set_face(self, emotion: Emotion | str, *, intensity: float = 1.0) -> dict[str, Any]:
        """切换屏幕表情。

        Args:
            emotion: :class:`Emotion` 之一或其字符串值；未知值退回 neutral。
            intensity: 表情强度 0..1，用于眨眼、抖动幅度之类的表现。
        """
        self.require(CAP_DISPLAY)
        try:
            value = emotion.value if isinstance(emotion, Emotion) else Emotion(str(emotion)).value
        except ValueError:
            logger.debug("未知表情 %r，退回 %s", emotion, FALLBACK_EMOTION.value)
            value = FALLBACK_EMOTION.value

        await self.conn.command(
            Action.SET_FACE, {"emotion": value, "intensity": max(0.0, min(1.0, float(intensity)))}
        )
        return {"ok": True, "emotion": value}

    async def set_text(self, text: str, *, duration_ms: int = 2000) -> dict[str, Any]:
        """在屏幕上叠加一行文字（例如显示识别结果）。"""
        self.require(CAP_DISPLAY)
        await self.conn.command(Action.SET_TEXT, {"text": text[:120], "duration_ms": int(duration_ms)})
        return {"ok": True, "text": text[:120]}

    async def show_image(self, image: bytes, *, fmt: str = "jpeg") -> dict[str, Any]:
        """把一整帧图片推到 LCD（用于显示拍到的照片或自定义画面）。"""
        self.require(CAP_DISPLAY)
        await self.conn.command(
            Action.DISPLAY_FRAME,
            {
                "format": fmt,
                "data_b64": base64.b64encode(image).decode("ascii"),
                "encoding": "base64",
            },
            timeout_ms=max(self.settings.device.command_timeout_ms, 10_000),
        )
        return {"ok": True, "bytes": len(image), "format": fmt}

    async def set_backlight(self, percent: int) -> dict[str, Any]:
        """设置屏幕背光亮度 0..100。"""
        self.require(CAP_DISPLAY)
        value = max(0, min(100, int(percent)))
        await self.conn.command(Action.SET_BACKLIGHT, {"percent": value})
        return {"ok": True, "percent": value}

    # ------------------------------------------------------------------ #
    # 视觉
    # ------------------------------------------------------------------ #
    async def look(
        self,
        *,
        width: int | None = None,
        height: int | None = None,
        quality: int | None = None,
        use_buffer: bool = False,
    ) -> LookResult:
        """抓一帧图像。

        Args:
            width: 期望宽度，默认取配置值。
            height: 期望高度，默认取配置值。
            quality: JPEG 质量 1..100，越小传输越快。
            use_buffer: 为 ``True`` 且本地已缓存帧时直接复用，省一次往返。

        Raises:
            CapabilityError: 设备没有摄像头。
            DeviceError: 抓帧失败。
        """
        self.require(CAP_CAMERA)
        vision = self.settings.vision

        if use_buffer and self.conn.frames:
            frame = self.conn.frames[-1]
            # 缓存帧过旧就不要了，宁可重新抓。
            if time.time() - frame.ts < 1.0:
                return LookResult(
                    frame=frame, data_uri=self._to_data_uri(frame), width=frame.width, height=frame.height
                )

        await self.conn.command(
            Action.SNAPSHOT,
            {
                "width": int(width or vision.request_width),
                "height": int(height or vision.request_height),
                "quality": int(quality or vision.jpeg_quality),
                "format": "jpeg",
            },
            timeout_ms=max(self.settings.device.command_timeout_ms, 10_000),
        )

        if not self.conn.frames:
            raise DeviceError(f"设备 {self.device_id} 声称抓帧成功但没有回传图像数据")

        frame = self.conn.frames[-1]
        return LookResult(frame=frame, data_uri=self._to_data_uri(frame), width=frame.width, height=frame.height)

    @staticmethod
    def _to_data_uri(frame: Frame) -> str:
        """把帧编码成 ``data:image/jpeg;base64,...``，可直接给 VLM 使用。"""
        mime = "image/png" if frame.fmt.lower() == "png" else "image/jpeg"
        return f"data:{mime};base64,{base64.b64encode(frame.data).decode('ascii')}"

    async def set_stream(self, *, enabled: bool, fps: float = 5.0, width: int = 320, height: int = 240) -> dict[str, Any]:
        """开启/关闭连续推流（用于控制台预览或持续视觉监控）。"""
        self.require(CAP_CAMERA)
        await self.conn.command(
            Action.SET_STREAM,
            {
                "enabled": bool(enabled),
                "fps": max(0.5, min(30.0, float(fps))),
                "width": int(width),
                "height": int(height),
            },
        )
        return {"ok": True, "enabled": enabled, "fps": fps}

    # ------------------------------------------------------------------ #
    # 音频
    # ------------------------------------------------------------------ #
    async def say(
        self,
        audio: bytes,
        *,
        fmt: str = "wav",
        sample_rate: int | None = None,
        wait: bool = False,
    ) -> dict[str, Any]:
        """把 PC 侧 TTS 生成的音频推给设备播放。

        Args:
            audio: 完整音频字节。
            fmt: ``wav`` / ``mp3`` / ``pcm_s16le``。
            sample_rate: PCM 裸流必须给；容器格式可省略。
            wait: 是否等到 ``audio_done`` 事件才返回（会阻塞，谨慎使用）。
        """
        self.require(CAP_SPEAKER)

        # 单条 WebSocket 消息有长度上限，而 base64 会把音频放大到 4/3 倍。
        # 超限时**设备侧会直接断开连接**（不是回一个错误），
        # 表现为"设备无故掉线"。所以在这里提前拦住并给出可读的原因。
        #
        # 设备的实际上限见固件 main/bot_net.c 的 WS_MAX_MSG。
        # 这里留 10% 余量给 JSON 外壳与其它字段。
        max_b64 = int(1024 * 1024 * 0.9)
        max_raw = max_b64 * 3 // 4
        if len(audio) > max_raw:
            raise SparkBotError(
                f"音频过大：{len(audio)} 字节（上限约 {max_raw} 字节，"
                f"约 {max_raw / 32 / 1000:.0f} 秒 16kHz 单声道）。"
                "超限的单条消息会让设备断开连接。请缩短文本，"
                "或改用更低采样率。"
            )

        params: dict[str, Any] = {
            "format": fmt,
            "encoding": "base64",
            "data_b64": base64.b64encode(audio).decode("ascii"),
            "wait": bool(wait),
        }
        if sample_rate:
            params["sample_rate"] = int(sample_rate)

        # 精确时长优先（分段播报靠它衔接，估算偏一点就会抢拍或留空档）；
        # 算不出再退回 16kHz 估算（32000 字节/秒 → 每毫秒 32 字节）。
        duration_s = audio_duration_s(audio, fmt, sample_rate)
        audio_ms = int((duration_s if duration_s is not None else len(audio) / 32.0) * 1000)
        estimated_ms = audio_ms + 3_000

        # ``wait=True`` 必须在**发送之前**订阅 audio_done：
        # 固件并不认 ``wait`` 参数（它一开始播放就回 result），"播完了"是靠
        # 随后上报的 audio_done 事件表达的。若先发后订阅，短音频可能在订阅
        # 建立之前就播完，事件被漏掉，只会白等一个超时。
        if wait:
            bus = getattr(self.conn, "bus", None)
            if bus is not None:
                async with bus.subscribe() as queue:
                    await self.conn.command(
                        Action.PLAY_AUDIO, params,
                        timeout_ms=max(self.settings.device.command_timeout_ms, estimated_ms),
                    )
                    done = await self._wait_audio_done(queue, duration_s=duration_s)
                return {"ok": True, "bytes": len(audio), "format": fmt, "done": done}

        await self.conn.command(
            Action.PLAY_AUDIO, params, timeout_ms=max(self.settings.device.command_timeout_ms, estimated_ms)
        )
        return {"ok": True, "bytes": len(audio), "format": fmt}

    async def _wait_device_event(
        self, queue: asyncio.Queue[Any], name: EventName | str, *, timeout_s: float
    ) -> bool:
        """在**独立订阅队列**上等本设备的某个事件，超时返回 ``False``。

        用 EventBus 的订阅队列，而不是 ``conn.events``：后者是所有设备共用的
        一条队列，从那里等会把同时到达的其它事件（电量、表情、运动完成）
        一并吞掉。订阅队列是每个订阅者独享的，互不影响。
        """
        target = name.value if isinstance(name, EventName) else str(name)
        deadline = time.monotonic() + timeout_s
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return False
            try:
                event = await asyncio.wait_for(queue.get(), timeout=remaining)
            except asyncio.TimeoutError:
                return False
            if event.topic != "device.event":
                continue
            if event.payload.get("device_id") != self.device_id:
                continue
            if event.payload.get("event") == target:
                return True

    async def _wait_audio_done(
        self, queue: asyncio.Queue[Any], *, duration_s: float | None
    ) -> bool:
        """等设备的 ``audio_done``（播放真的结束），超时返回 ``False``。

        超时上限取"精确时长 + 0.4s"，这里有两个考虑：

        * 事件正常情况下与播放结束同时到达，只等事件最准，衔接无空档；
        * **不要**另起一个 ``sleep(精确时长)`` 去竞速 —— 两者会在同一刻
          到期，睡眠经常先返回，于是明明收到了事件还报"等待超时"
          （这是实测踩过的坑）。

        超时值用 WAV 头的真实时长而不是估算，所以即便固件不发事件
        （旧固件有回调被 memset 清掉的 bug），也只在句末多停 0.4 秒。
        """
        return await self._wait_device_event(
            queue,
            EventName.AUDIO_DONE,
            timeout_s=(duration_s if duration_s is not None else 30.0) + 0.4,
        )

    async def wait_audio_done(self, *, timeout_s: float = 3.0) -> bool:
        """等设备把**缓冲区里剩下的音频播完**（``audio_done`` 事件）；超时返回 False。

        为什么需要它：``audio_stream_end`` 只表示"数据发完了"，设备那边
        还有最多一个环形缓冲（64KB ≈ 2 秒）没播。如果这时候就去开下一轮
        麦克风采集，设备会**一边播一边上传麦克风音频** —— 主循环被挤住，
        ``audio_stream_write`` 的应答变慢，推送出现空档，环形缓冲抽干，
        听感就是"说到一半滋滋"。

        超时给得比环形缓冲略大：正常情况事件很快就到；万一设备没发事件
        （旧固件），也只在句末多停几秒，不会卡住整条链路。
        """
        bus = getattr(self.conn, "bus", None)
        if bus is None:  # pragma: no cover - 离线替身没有事件总线
            return False
        async with bus.subscribe() as queue:
            return await self._wait_device_event(
                queue, EventName.AUDIO_DONE, timeout_s=timeout_s
            )

    # ------------------------------------------------------------------ #
    # 流式播报（真流式：设备边收边播，段落之间不断流）
    # ------------------------------------------------------------------ #
    async def audio_stream_begin(self) -> None:
        """进入流式播放模式（会打断设备上正在播的内容）。"""
        self.require(CAP_SPEAKER)
        await self.conn.command(Action.AUDIO_STREAM_BEGIN, {}, timeout_ms=5_000)

    async def audio_stream_write(self, pcm: bytes) -> int:
        """往流里追加一块 PCM（板子采样率的单声道 16bit 裸流）。

        返回设备报告的写入字节数。缓冲满时设备侧会等播放任务消费，
        所以这个调用自带背压 —— 调用方不必额外节流，也就不会把
        64KB 的播放环形缓冲灌爆。
        """
        self.require(CAP_SPEAKER)
        params = {
            "encoding": "base64",
            "data_b64": base64.b64encode(pcm).decode("ascii"),
        }
        # 缓冲满时设备会阻塞等消费，单个命令的超时要留够
        timeout_ms = max(self.settings.device.command_timeout_ms, 10_000)
        envelope = await self.conn.command(Action.AUDIO_STREAM_WRITE, params,
                                           timeout_ms=timeout_ms)
        return int((envelope.raw.get("data") or {}).get("bytes") or 0)

    async def audio_stream_end(self) -> None:
        """结束流：设备把缓冲里剩下的播完后才上报 ``audio_done``。"""
        self.require(CAP_SPEAKER)
        await self.conn.command(Action.AUDIO_STREAM_END, {}, timeout_ms=5_000)

    async def play_tone(self, frequency_hz: float = 880.0, duration_ms: int = 120) -> dict[str, Any]:
        """播放一声提示音，用于「听到了」「准备好了」之类即时反馈。"""
        self.require(CAP_SPEAKER)
        await self.conn.command(
            Action.PLAY_TONE,
            {"frequency_hz": float(frequency_hz), "duration_ms": int(duration_ms)},
            timeout_ms=max(self.settings.device.command_timeout_ms, duration_ms + 1_000),
        )
        return {"ok": True, "frequency_hz": frequency_hz, "duration_ms": duration_ms}

    async def set_volume(self, percent: int) -> dict[str, Any]:
        """设置喇叭音量 0..100。"""
        self.require(CAP_SPEAKER)
        value = max(0, min(100, int(percent)))
        await self.conn.command(Action.SET_VOLUME, {"percent": value})
        return {"ok": True, "percent": value}

    async def start_listen(self, *, timeout_ms: int | None = None, wake_word: bool = False) -> None:
        """让设备开始采集麦克风并回传音频帧。

        Args:
            timeout_ms: 设备侧静音超时，到点自动结束并发 ``listen_timeout``。
            wake_word: 是否要求先命中本地唤醒词才开始上传。
        """
        self.require(CAP_MICROPHONE)
        params: dict[str, Any] = {"wake_word": bool(wake_word)}
        if timeout_ms is not None:
            params["timeout_ms"] = int(timeout_ms)
        await self.conn.command(Action.START_LISTEN, params)

    async def stop_listen(self) -> None:
        """停止麦克风采集。"""
        self.require(CAP_MICROPHONE)
        await self.conn.command(Action.STOP_LISTEN, {})

    async def collect_audio(
        self,
        *,
        max_seconds: float = 8.0,
        silence_timeout_s: float = 1.2,
        start: bool = True,
        stale_grace_s: float = 1.5,
        on_chunk: Callable[[bytes], Awaitable[None]] | None = None,
    ) -> bytes:
        """采集一段语音并返回原始 PCM。

        结束条件（任一满足）：
          * 设备发来结束标记（``end``）且**已经收过音频片**；
          * 收到过音频片、且之后连续静音超过 ``silence_timeout_s``；
          * 超过 ``max_seconds``。

        Args:
            max_seconds: 单次采集的**绝对上限**（用户最长可以说多久）。
            silence_timeout_s: 判静音的时间——收到过声音之后，连续这么久
                没有新分片就认为说完了。
            start: 是否先下发 ``start_listen``（若已在监听可传 ``False``）。
            stale_grace_s: **保护期**。这段时间内收到的 ``end`` 标记视为
                上一轮遗留并忽略。
            on_chunk: 每收到一片音频就回调一次（**边收边转**用）。
                流式识别就是靠它把 20ms 的音频片实时喂给 ASR 服务；
                回调抛异常不影响采集，只是记一条日志。

        Note:
            ``silence_timeout_s`` 与 ``max_seconds`` 是两件事，别混用：
            前者是"说完了"的判定，后者是"最多等多久"。
            早期版本把 ``silence_timeout_s`` 当成设备侧的采集超时下发，
            结果设备 1.2 秒就自动收尾，PC 侧只能拿到 0~2 片音频。
            设备侧的超时应该由 ``max_seconds`` 决定。
        """
        self.require(CAP_MICROPHONE)

        # 后台语音闭环可能正在采集。用户主动要求「听」时应该排队等它结束，
        # 而不是直接报错——等待上限取一次完整采集的合理时长。
        if await self._wait_until_free_recorder(timeout_s=max_seconds + 2.0) is False:
            raise SparkBotError(
                f"设备 {self.conn.device_id} 正在被其它流程采集音频，请稍后再试",
                device_id=self.conn.device_id,
            )

        _RECORDING.add(self.conn.device_id)
        try:
            return await self._collect_audio_locked(
                max_seconds=max_seconds,
                silence_timeout_s=silence_timeout_s,
                start=start,
                stale_grace_s=stale_grace_s,
                on_chunk=on_chunk,
            )
        finally:
            _RECORDING.discard(self.conn.device_id)

    async def _wait_until_free_recorder(self, *, timeout_s: float) -> bool:
        """等待本设备不再被其它流程占用；超时返回 ``False``。"""
        if self.conn.device_id not in _RECORDING:
            return True
        logger.info("设备 %s 正在被其它流程采集，等待其结束…", self.conn.device_id)
        deadline = time.monotonic() + timeout_s
        while time.monotonic() < deadline:
            await asyncio.sleep(0.1)
            if self.conn.device_id not in _RECORDING:
                return True
        return False

    async def _collect_audio_locked(
        self,
        *,
        max_seconds: float,
        silence_timeout_s: float,
        start: bool,
        stale_grace_s: float,
        on_chunk: Callable[[bytes], Awaitable[None]] | None = None,
    ) -> bytes:
        """真正的采集实现；调用前必须已持有该设备的采集权。"""
        if start:
            # 设备侧的超时用 max_seconds（用户最长说多久），
            # **不是** silence_timeout_s（静音判定）—— 见 collect_audio 的说明。
            await self.start_listen(timeout_ms=int(max_seconds * 1000))
            # 给设备一点时间真正打开麦克风，避免丢掉开头几个字。
            await self._drain_audio()

        chunks: list[bytes] = []
        started_at = time.monotonic()
        deadline = started_at + max_seconds

        #: 保护期：这之前收到的 end 标记视为上一轮遗留。
        #:
        #: 为什么需要它、而且要给到 1.5 秒：`stop_listen` 之后设备会补发一个
        #: end 到链路上，那条消息可能在本轮 ``_drain_audio()`` **之后**才到。
        #: 如果立刻把它当成本轮结束，采集会一段音频都没收到就返回空。
        protect_until = started_at + stale_grace_s

        last_voice = started_at
        end_marks = 0
        timeouts = 0

        while True:
            now = time.monotonic()
            if now >= deadline:
                break

            wait = max(0.05, min(deadline - now, silence_timeout_s))
            try:
                chunk = await asyncio.wait_for(self.conn.audio.get(), timeout=wait)
            except asyncio.TimeoutError:
                timeouts += 1
                # 收到过声音且已经静了足够久 → 说完了
                if chunks and time.monotonic() - last_voice >= silence_timeout_s:
                    break
                # 一片都没收到，且已经过了保护期 → 用户没说话，不必空等到
                # max_seconds；控制在保护期 + 一个静音窗口的量级。
                if not chunks and time.monotonic() >= protect_until + silence_timeout_s:
                    break
                continue

            if chunk.fmt == "__end__":
                end_marks += 1
                if not chunks and time.monotonic() < protect_until:
                    logger.debug(
                        "忽略保护期内的陈旧音频结束标记（已收 %d 片，启动后 %.2fs）",
                        len(chunks),
                        time.monotonic() - started_at,
                    )
                    continue
                break

            if not chunk.data:
                continue

            chunks.append(chunk.data)
            if on_chunk is not None:
                # 边收边喂给流式识别。回调失败只记日志：识别链路坏了
                # 不该把采集也带崩，采集完还可以走整段识别的回退路径。
                try:
                    await on_chunk(chunk.data)
                except Exception as exc:  # noqa: BLE001
                    logger.warning("音频分片回调失败（流式识别可能已断）: %s", exc)
            if self._has_voice_energy(chunk.data):
                last_voice = time.monotonic()

        if start:
            with contextlib.suppress(DeviceError, DeviceOfflineError):
                await self.stop_listen()
        #: 供上层诊断用：这一轮采集收到了多少分片 / 结束标记 / 空转次数。
        #: 麦克风链路出问题时，"一片都没收到"和"收到了但被静音判定提前结束"
        #: 是两种完全不同的原因，没有这些计数就分不清。
        self.last_audio_chunks = len(chunks)
        logger.info(
            "collect_audio 结束: %d 片 / %d 字节 / end标记=%d 超时=%d",
            len(chunks),
            sum(len(c) for c in chunks),
            end_marks,
            timeouts,
        )
        return b"".join(chunks)

    @staticmethod
    def _has_voice_energy(pcm: bytes, *, threshold: int = 500) -> bool:
        """粗略判断一段 16-bit PCM 是否含语音能量（无 numpy 依赖）。

        只看绝对值峰值与抽样平均，足够做「静音检测」这种二值判断。
        """
        if len(pcm) < 4:
            return False
        import array

        samples = array.array("h")
        samples.frombytes(pcm[: len(pcm) - (len(pcm) % 2)])
        if not samples:
            return False
        peak = max(abs(s) for s in samples)
        return peak >= threshold

    async def _drain_audio(self) -> None:
        """丢弃当前队列里的陈旧音频分片。"""
        while True:
            try:
                self.conn.audio.get_nowait()
            except asyncio.QueueEmpty:
                return

    # ------------------------------------------------------------------ #
    # 系统
    # ------------------------------------------------------------------ #
    async def set_led(self, *, r: int, g: int, b: int) -> dict[str, Any]:
        """设置板载 RGB 灯颜色。"""
        await self.conn.command(
            Action.SET_LED,
            {"r": max(0, min(255, int(r))), "g": max(0, min(255, int(g))), "b": max(0, min(255, int(b)))},
        )
        return {"ok": True, "rgb": [r, g, b]}

    async def reboot(self) -> dict[str, Any]:
        """重启设备固件。"""
        try:
            await self.conn.command(Action.REBOOT, {}, timeout_ms=3_000)
        except DeviceTimeoutError:
            # 重启会让连接立刻断，收不到 result 是正常现象。
            logger.info("设备 %s 已重启（未收到 result，符合预期）", self.device_id)
        return {"ok": True, "device_id": self.device_id}

    # ------------------------------------------------------------------ #
    # 事件等待
    # ------------------------------------------------------------------ #
    async def wait_for_event(
        self, name: EventName | str, *, timeout_s: float = 10.0
    ) -> dict[str, Any] | None:
        """等待某个设备事件的到达，超时返回 ``None``。

        注意：会消费事件队列中不匹配的事件（直接丢弃），因此只应在
        没有其它并发消费者的阶段使用。
        """
        target = name.value if isinstance(name, EventName) else str(name)
        deadline = time.monotonic() + timeout_s
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return None
            try:
                envelope = await asyncio.wait_for(self.conn.events.get(), timeout=remaining)
            except asyncio.TimeoutError:
                return None
            if envelope.raw.get("event") == target:
                return envelope.raw.get("data") or {}


class RobotProvider:
    """``Robot`` 的工厂：从网关解析设备并缓存门面对象。"""

    def __init__(self, gateway: DeviceGateway, settings: Settings) -> None:
        self.gateway = gateway
        self.settings = settings
        self._cache: dict[str, Robot] = {}

    def get(self, device_id: str | None = None) -> Robot:
        """解析目标设备并返回其能力门面。

        Raises:
            DeviceOfflineError: 没有可用设备。
        """
        conn = self.gateway.resolve(device_id)
        robot = self._cache.get(conn.device_id)
        if robot is None or robot.conn is not conn:
            robot = Robot(conn, self.settings)
            self._cache[conn.device_id] = robot
        return robot

    def try_get(self, device_id: str | None = None) -> Robot | None:
        """解析目标设备，没有则返回 ``None``。

        之前的实现把这个三分支逻辑塞进了一个嵌套三元表达式，结果在
        ``conn is None`` 时仍然去读 ``conn.device_id`` —— 只要传了一个
        不存在的 device_id 就会抛 AttributeError（HTTP 层表现为 500）。
        展开成普通分支，顺便让"为什么返回 None"一目了然。
        """
        if device_id:
            conn = self.gateway.try_get(device_id)
        else:
            devices = self.gateway.all()
            conn = devices[0] if devices else None

        if conn is None:
            return None

        robot = self._cache.get(conn.device_id)
        if robot is None or robot.conn is not conn:
            robot = Robot(conn, self.settings)
            self._cache[conn.device_id] = robot
        return robot
