"""模拟设备的协议实现：与真实固件行为一致。

运行::

    python -m sparkbot.mock_device                 # 连本机默认端口
    python -m sparkbot.mock_device --url ws://192.168.1.5:8765/robot
    python -m sparkbot.mock_device --trigger 5     # 每 5 秒模拟一次唤醒词

``--trigger`` 特别有用：它模拟用户对着机器人说唤醒词，
从而在不接真硬件的情况下跑通「唤醒 → 采集 → 识别 → 回复 → 播报」整条语音闭环。
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import logging
import signal
import time
from dataclasses import dataclass, field
from typing import Any

from ..device import protocol
from ..device.protocol import Action, EventName, MsgType
from .scene import SceneRenderer, SceneSpec, encode_b64, speech_like_pcm

logger = logging.getLogger(__name__)

#: 模拟设备默认声明的能力与硬件参数，与 ``docs/protocol.md`` 的示例一致。
DEFAULT_CAPABILITIES = ("camera", "microphone", "speaker", "display", "motor")

#: 人脸特征维度，必须与固件的 ``BOT_FACE_FEAT_LEN`` 一致。
FACE_FEAT_LEN = 512


def mock_face_feature(name: str, *, noise: float = 0.0) -> list[float]:
    """按名字生成一段稳定的 512 维人脸特征（模拟 MFN 模型的输出）。

    两个要点，都是为了**让模拟数据真的能验出问题**：
    * **同一个名字永远得到同一个向量**（用名字的 sha256 做随机种子），
      所以"绑一次 → 再扫"应当匹配上，否则说明 PC 侧链路断了；
    * **默认带一点噪声**：真实摄像头每帧的向量都不一样，如果模拟数据永远
      精确相等，阈值判断（0.5）那条分支就永远走不到，测了等于没测。

    返回的是 L2 归一化后的 float32 列表 —— 与设备端 esp-dl 的输出一致，
    也与离线协议里 ``feat_b64``（float32 小端）对得上。
    """
    import hashlib
    import math
    import random

    seed = int.from_bytes(hashlib.sha256(name.encode("utf-8")).digest()[:8], "big")
    rng = random.Random(seed)
    vector = [rng.gauss(0.0, 1.0) for _ in range(FACE_FEAT_LEN)]
    if noise > 0:
        jitter = random.Random(seed ^ 0x5EED)
        vector = [v + jitter.gauss(0.0, noise) for v in vector]
    norm = math.sqrt(sum(v * v for v in vector)) or 1.0
    return [v / norm for v in vector]


def encode_face_feat(feat: list[float]) -> str:
    """把特征编码成离线协议约定的 base64（float32 小端）。

    刻意**不**复用 PC 侧的 ``perception.face.encode_feat_b64``：
    模拟设备是"设备"这一侧的实现，独立按协议写一遍，
    这样编解码不匹配时才真的会被测出来。
    """
    import base64
    import struct

    return base64.b64encode(struct.pack(f"<{len(feat)}f", *feat)).decode("ascii")


@dataclass
class MockDeviceConfig:
    """模拟设备的行为参数。"""

    url: str = "ws://127.0.0.1:8765/robot"
    device_id: str = "esp32s3-mock01"
    name: str = "小星（模拟）"
    model: str = "ESP32-S3-N16R8(mock)"
    fw: str = "0.1.0-mock"
    capabilities: tuple[str, ...] = DEFAULT_CAPABILITIES
    telemetry_ms: int = 3_000
    """遥测上报周期；比协议默认的 10s 更密，便于调试观察。"""

    trigger_interval_s: float = 0.0
    """大于 0 时，每隔该秒数模拟一次唤醒词事件。"""

    frame_width: int = 640
    frame_height: int = 480
    frame_quality: int = 80
    stream_fps: float = 0.0
    """大于 0 时启动即开启连续推流。"""

    audio_seconds: float = 1.8
    """每次采集模拟多长的语音。"""

    face_people: list[str] = field(default_factory=list)
    """模拟「站在摄像头前的人」；每个名字都会被识别成一张稳定的脸。"""

    face_noise: float = 0.03
    """人脸特征的抖动量，模拟同一个人不同帧之间的差异。"""

    face_luma: int = 42
    """人脸识别时回报的画面平均亮度（0~255）。设成 <20 可模拟"太暗"。"""

    face_strip_feat: bool = False
    """置 true 时故意**不回** feat_b64（只回框和分数）。

    这是照着真实固件踩过的坑做的回归开关：`mbedtls_base64_encode(NULL, 0, ...)`
    查长度会返回错误码而不是 0，于是特征串根本没写进 JSON，设备报"检测到 1 张"
    而 PC 侧只能认成"0 张"。用它守住"这种半截数据必须显式报出来"的行为。
    """

    stuck_after_s: float = 0.0
    """大于 0 时模拟设备在该秒数后失联（用于测试掉线处理）。"""

    verbose: bool = False


class MockDevice:
    """一台用 Python 模拟出来的 ESP32-S3 机器人。"""

    def __init__(self, config: MockDeviceConfig) -> None:
        self.config = config
        self.renderer = SceneRenderer()

        # --- 模拟硬件状态 ------------------------------------------------ #
        self.motion = {"linear": 0.0, "angular": 0.0}
        self.rotation_deg = 0.0
        self.face = "neutral"
        self.volume = 70
        self.backlight = 100
        self.led = (0, 0, 0)
        self.battery_percent = 86.0
        self.battery_voltage = 8.15
        self.uptime_start = time.time()

        # --- 连接与任务 -------------------------------------------------- #
        self._ws: Any = None
        self._reader_task: asyncio.Task[None] | None = None
        self._telemetry_task: asyncio.Task[None] | None = None
        self._trigger_task: asyncio.Task[None] | None = None
        self._stream_task: asyncio.Task[None] | None = None
        self._motion_task: asyncio.Task[None] | None = None
        self._listening = False
        self._listen_task: asyncio.Task[None] | None = None
        self._frame_seq = 0
        self._stop = asyncio.Event()
        self._send_lock = asyncio.Lock()

        #: 记录收到的指令，便于测试断言。
        self.command_log: list[dict[str, Any]] = []
        #: 每次成功执行 command 后投递其结果，便于测试确认「指令真的到达并被执行」。
        self.results: asyncio.Queue[dict[str, Any]] = asyncio.Queue(maxsize=512)

    # ------------------------------------------------------------------ #
    # 生命周期
    # ------------------------------------------------------------------ #
    async def run(self) -> None:
        """连接 PC 并持续服务，直到被要求停止。"""
        import websockets

        logger.info("模拟设备 %s 正在连接 %s", self.config.device_id, self.config.url)
        async with websockets.connect(
            self.config.url,
            ping_interval=None,  # 心跳由协议层的 telemetry 负责，不用 WS 内建 ping
            max_size=8 * 1024 * 1024,
        ) as websocket:
            self._ws = websocket
            await self._send_hello()

            self._reader_task = asyncio.create_task(self._read_loop(), name="mock-reader")
            self._telemetry_task = asyncio.create_task(self._telemetry_loop(), name="mock-telemetry")
            if self.config.trigger_interval_s > 0:
                self._trigger_task = asyncio.create_task(self._trigger_loop(), name="mock-trigger")
            if self.config.stream_fps > 0:
                self._stream_task = asyncio.create_task(
                    self._stream_loop(self.config.stream_fps), name="mock-stream"
                )

            try:
                await self._stop.wait()
            finally:
                await self._shutdown()

    async def stop(self) -> None:
        """请求停止，并等待收尾。"""
        self._stop.set()

    async def _shutdown(self) -> None:
        """取消所有后台任务并关闭连接。"""
        for task in (
            self._stream_task,
            self._listen_task,
            self._motion_task,
            self._trigger_task,
            self._telemetry_task,
            self._reader_task,
        ):
            if task is None or task.done():
                continue
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task
        logger.info("模拟设备已停止")

    # ------------------------------------------------------------------ #
    # 发送
    # ------------------------------------------------------------------ #
    async def _send(self, payload: dict[str, Any]) -> None:
        """发送一条信封。"""
        if self._ws is None:
            return
        async with self._send_lock:
            try:
                await self._ws.send(protocol.dumps(payload))
            except Exception as exc:  # noqa: BLE001 - 断线时静默退出
                logger.debug("发送失败: %s", exc)
                self._stop.set()

    async def _send_hello(self) -> None:
        """发送握手帧。"""
        await self._send(
            {
                "v": protocol.PROTOCOL_VERSION,
                "type": MsgType.HELLO.value,
                "ts": protocol.now_ms(),
                "device": {
                    "id": self.config.device_id,
                    "model": self.config.model,
                    "fw": self.config.fw,
                    "name": self.config.name,
                },
                "capabilities": list(self.config.capabilities),
                "display": {"width": 240, "height": 240, "color": "rgb565", "emotions": "full"},
                "camera": {
                    "max_width": self.config.frame_width,
                    "max_height": self.config.frame_height,
                    "formats": ["jpeg"],
                },
                "audio": {"input_rate": 16000, "output_rate": 16000, "channels": 1},
                "motor": {"kind": "differential", "max_linear": 0.8, "max_angular": 2.5},
            }
        )

    async def _send_result(
        self, msg_id: str | None, *, ok: bool, data: dict[str, Any] | None = None,
        error: dict[str, Any] | None = None,
    ) -> None:
        """回复一条 command。"""
        await self._send(
            {
                "v": protocol.PROTOCOL_VERSION,
                "type": MsgType.RESULT.value,
                "id": msg_id,
                "ts": protocol.now_ms(),
                "ok": ok,
                "data": data or {},
                "error": error,
            }
        )

    async def _send_event(self, event: EventName | str, **data: Any) -> None:
        """上报一个异步事件。"""
        name = event.value if isinstance(event, EventName) else str(event)
        await self._send(
            {
                "v": protocol.PROTOCOL_VERSION,
                "type": MsgType.EVENT.value,
                "ts": protocol.now_ms(),
                "event": name,
                "data": data,
            }
        )

    # ------------------------------------------------------------------ #
    # 接收
    # ------------------------------------------------------------------ #
    async def _read_loop(self) -> None:
        """读取 PC 下发的每一帧。"""
        assert self._ws is not None
        while not self._stop.is_set():
            try:
                raw = await self._ws.recv()
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001
                logger.info("连接结束: %s", exc)
                self._stop.set()
                return

            try:
                envelope = protocol.parse_envelope(raw)
            except protocol.ProtocolError as exc:
                logger.warning("收到非法信封: %s", exc)
                continue

            if self.config.verbose:
                logger.debug("← %s", envelope.raw)

            try:
                await self._dispatch(envelope)
            except Exception:  # noqa: BLE001 - 单个指令失败不该拖垮设备
                logger.exception("处理指令失败: %s", envelope.type)

    async def _dispatch(self, envelope: protocol.Envelope) -> None:
        """按消息类型分派。"""
        msg_type = envelope.msg_type

        if msg_type is MsgType.HELLO_ACK:
            if not envelope.ok:
                logger.error("服务端拒绝了握手: %s", envelope.raw.get("error"))
                self._stop.set()
                return
            logger.info(
                "握手成功 session=%s 心跳=%sms",
                envelope.raw.get("session"),
                envelope.raw.get("heartbeat_ms"),
            )
            return

        if msg_type is MsgType.PING:
            await self._send(
                {"v": protocol.PROTOCOL_VERSION, "type": MsgType.PONG.value,
                 "id": envelope.id, "ts": protocol.now_ms()}
            )
            return

        if msg_type is MsgType.INTENT:
            # 即发即忘：执行但不回复。
            await self._execute(envelope.action, envelope.params)
            return

        if msg_type is MsgType.COMMAND:
            data = await self._execute(envelope.action, envelope.params)
            if isinstance(data, dict) and data.get("__error__"):
                error = data.pop("__error__")
                await self._send_result(
                    envelope.id, ok=False, error={"code": error.get("code", "internal"),
                                                  "message": error.get("message", "失败")}
                )
            else:
                await self._send_result(envelope.id, ok=True, data=data or {})
                # 通知测试/观察者：这条指令确实到达并执行了。
                with contextlib.suppress(asyncio.QueueFull):
                    self.results.put_nowait({"action": envelope.action, "data": data or {}})
            return

        logger.debug("忽略消息类型 %s", envelope.type)

    # ------------------------------------------------------------------ #
    # 指令执行（模拟真实硬件）
    # ------------------------------------------------------------------ #
    async def _execute(self, action: str | None, params: dict[str, Any]) -> dict[str, Any]:
        """执行一条动作，返回 result 的 data 体。

        返回带 ``__error__`` 的字典表示失败。
        """
        self.command_log.append({"action": action, "params": params, "ts": time.time()})
        if action is None:
            return {"__error__": {"code": "bad_params", "message": "缺少 action"}}

        handler = getattr(self, f"_do_{action}", None)
        if handler is None:
            logger.warning("不支持的指令: %s", action)
            return {"__error__": {"code": "unsupported_action", "message": f"未实现 {action}"}}

        return await handler(params)

    # -- 底盘 ---------------------------------------------------------- #
    async def _do_drive(self, params: dict[str, Any]) -> dict[str, Any]:
        """执行 drive，并模拟开环定时自动停止。"""
        linear = float(params.get("linear", 0.0))
        angular = float(params.get("angular", 0.0))
        duration_ms = int(params.get("duration_ms", 0))

        self.motion = {"linear": linear, "angular": angular}
        # 角速度积分成朝向，抓帧时会体现在视角变化上。
        if angular:
            self.rotation_deg = (self.rotation_deg + angular * (duration_ms / 1000.0) * 57.2958) % 360.0

        if self._motion_task is not None and not self._motion_task.done():
            self._motion_task.cancel()

        if duration_ms > 0:
            self._motion_task = asyncio.create_task(self._auto_stop(duration_ms), name="mock-motion")

        return {"applied": {"linear": linear, "angular": angular}, "duration_ms": duration_ms}

    async def _auto_stop(self, duration_ms: int) -> None:
        """到点自动停下并发 motion_done 事件，模拟固件的开环定时。"""
        try:
            await asyncio.sleep(duration_ms / 1000.0)
        except asyncio.CancelledError:
            return
        self.motion = {"linear": 0.0, "angular": 0.0}
        await self._send_event(EventName.MOTION_DONE, action=Action.DRIVE.value)
        logger.debug("开环定时结束，底盘已停")

    async def _do_stop(self, params: dict[str, Any]) -> dict[str, Any]:
        """立即停止。"""
        if self._motion_task is not None and not self._motion_task.done():
            self._motion_task.cancel()
        self.motion = {"linear": 0.0, "angular": 0.0}
        return {"stopped": True, "emergency": bool(params.get("emergency"))}

    async def _do_set_motion_limits(self, params: dict[str, Any]) -> dict[str, Any]:
        """接受软限幅（模拟设备侧不额外做限制）。"""
        return {"limits": {k: v for k, v in params.items() if k.startswith("max_")}}

    # -- 显示 ---------------------------------------------------------- #
    async def _do_set_face(self, params: dict[str, Any]) -> dict[str, Any]:
        """切换表情。"""
        self.face = str(params.get("emotion", "neutral"))
        logger.info("😀 表情 → %s", self.face)
        return {"emotion": self.face}

    async def _do_set_text(self, params: dict[str, Any]) -> dict[str, Any]:
        """显示文字。"""
        logger.info("📝 屏幕文字 → %s", params.get("text"))
        return {"text": params.get("text", "")}

    async def _do_display_frame(self, params: dict[str, Any]) -> dict[str, Any]:
        """接收一整帧图片并「显示」。"""
        import base64

        raw = params.get("data_b64") or ""
        try:
            size = len(base64.b64decode(raw, validate=True))
        except Exception:  # noqa: BLE001
            return {"__error__": {"code": "bad_params", "message": "data_b64 不是合法 base64"}}
        logger.info("🖼  屏幕收到一帧图片 %d 字节", size)
        return {"shown_bytes": size}

    async def _do_set_backlight(self, params: dict[str, Any]) -> dict[str, Any]:
        """调整背光。"""
        self.backlight = int(params.get("percent", 100))
        return {"percent": self.backlight}

    async def _do_clear_display(self, params: dict[str, Any]) -> dict[str, Any]:
        """清屏。"""
        self.face = "neutral"
        return {"cleared": True}

    # -- 视觉 ---------------------------------------------------------- #
    async def _do_snapshot(self, params: dict[str, Any]) -> dict[str, Any]:
        """抓一帧并作为独立的 frame 消息回传。

        注意这里**先**回 frame 再回 result——协议允许两者顺序任意，
        PC 侧会用 id 配对并把帧缓存起来。
        """
        width = int(params.get("width") or self.config.frame_width)
        height = int(params.get("height") or self.config.frame_height)
        quality = int(params.get("quality") or self.config.frame_quality)
        await self._capture(width=width, height=height, quality=quality, msg_id=None)
        return {"format": "jpeg", "width": width, "height": height, "seq": self._frame_seq}

    async def _do_set_stream(self, params: dict[str, Any]) -> dict[str, Any]:
        """启停连续推流。"""
        enabled = bool(params.get("enabled"))
        fps = float(params.get("fps", 5.0))

        if self._stream_task is not None and not self._stream_task.done():
            self._stream_task.cancel()
            self._stream_task = None

        if enabled:
            self._stream_task = asyncio.create_task(self._stream_loop(fps), name="mock-stream")
        logger.info("📹 推流 %s (%.1f fps)", "开启" if enabled else "关闭", fps)
        return {"enabled": enabled, "fps": fps}

    async def _do_set_camera_params(self, params: dict[str, Any]) -> dict[str, Any]:
        """接受摄像头参数（模拟设备不真正应用）。"""
        return {"camera": {k: v for k, v in params.items()}}

    async def _do_face_identify(self, params: dict[str, Any]) -> dict[str, Any]:
        """人脸识别（**模拟设备端本地推理**）。

        真实固件是在芯片上跑 esp-dl（检测 + 提特征），只回 512 维特征与框；
        这里用 :func:`mock_face_feature` 造出同样形状的数据，
        于是"设备回特征 → PC 认名字"这条链路不需要硬件也能跑通。

        ``config`` 指令可以随时改 ``face_people``，用来模拟"有人走过来/走开"。
        """
        people = [str(n) for n in self.config.face_people]
        faces: list[dict[str, Any]] = []
        for index, name in enumerate(people[:4]):   # 与固件一样最多 4 张
            feat = mock_face_feature(name, noise=max(0.0, float(self.config.face_noise)))
            x1 = 40 + index * 150
            entry: dict[str, Any] = {
                "x1": x1,
                "y1": 80,
                "x2": x1 + 130,
                "y2": 260,
                "score": 0.93,
                "feat_len": len(feat),
            }
            if not self.config.face_strip_feat:
                entry["feat_b64"] = encode_face_feat(feat)
            faces.append(entry)

        logger.info("🙂 人脸识别（模拟）：%d 张 %s", len(faces), people[:4] or "—")
        return {
            "count": len(faces),
            "width": self.config.frame_width,
            "height": self.config.frame_height,
            "mean_luma": int(self.config.face_luma),
            "feat_format": "float32",
            "faces": faces,
        }

    async def _capture(self, *, width: int, height: int, quality: int, msg_id: str | None) -> None:
        """渲染并发送一帧。"""
        self._frame_seq += 1
        spec = SceneSpec(
            width=width,
            height=height,
            rotation_deg=self.rotation_deg,
        )
        jpeg = self.renderer.render(spec, quality=quality)
        payload = {
            "v": protocol.PROTOCOL_VERSION,
            "type": MsgType.FRAME.value,
            "id": msg_id,
            "ts": protocol.now_ms(),
            "format": "jpeg",
            "width": spec.width,
            "height": spec.height,
            "seq": self._frame_seq,
            "data_b64": encode_b64(jpeg),
        }
        if self.config.verbose:
            logger.debug(
                "→ frame seq=%d %dx%d jpeg=%d 字节", self._frame_seq, spec.width, spec.height, len(jpeg)
            )
        await self._send(payload)

    async def _stream_loop(self, fps: float) -> None:
        """按帧率持续推流。"""
        interval = 1.0 / max(0.2, fps)
        try:
            while not self._stop.is_set():
                # 推流用较小分辨率，模拟真实设备的带宽妥协。
                await self._capture(
                    width=min(320, self.config.frame_width),
                    height=min(240, self.config.frame_height),
                    quality=60,
                    msg_id=None,
                )
                await asyncio.sleep(interval)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001
            logger.warning("推流结束: %s", exc)

    # -- 音频 ---------------------------------------------------------- #
    async def _do_play_audio(self, params: dict[str, Any]) -> dict[str, Any]:
        """接收并「播放」音频。"""
        import base64

        raw = params.get("data_b64") or ""
        try:
            data = base64.b64decode(raw, validate=True)
        except Exception:  # noqa: BLE001
            return {"__error__": {"code": "bad_params", "message": "data_b64 不是合法 base64"}}

        fmt = params.get("format", "wav")
        logger.info("🔊 播放音频 %d 字节 (%s)", len(data), fmt)

        # 估算播放耗时并在结束时报 audio_done，让上层能等到播报完成。
        seconds = self._estimate_play_seconds(data, fmt)
        if seconds > 0:
            asyncio.create_task(self._announce_audio_done(seconds), name="mock-audio-done")
        return {"played_bytes": len(data), "format": fmt, "estimated_s": round(seconds, 2)}

    @staticmethod
    def _estimate_play_seconds(data: bytes, fmt: str) -> float:
        """粗略估算播放时长，用于模拟播报结束事件。"""
        if fmt in ("wav", "mp3"):
            # 16 kHz 单声道 16-bit 约 32 KB/s；压缩格式按一半估。
            rate = 32_000 if fmt == "wav" else 16_000
            return min(30.0, len(data) / rate)
        return min(30.0, len(data) / 32_000)

    async def _announce_audio_done(self, seconds: float) -> None:
        """延迟上报 audio_done。"""
        try:
            await asyncio.sleep(min(30.0, seconds))
        except asyncio.CancelledError:
            return
        await self._send_event(EventName.AUDIO_DONE)

    async def _do_tts_speak(self, params: dict[str, Any]) -> dict[str, Any]:
        """板载 TTS（模拟设备只记录文本）。"""
        logger.info("🗣  板载 TTS → %s", params.get("text"))
        return {"spoken": params.get("text", "")}

    async def _do_start_listen(self, params: dict[str, Any]) -> dict[str, Any]:
        """开始采集麦克风：分片上传模拟语音。"""
        if self._listen_task is not None and not self._listen_task.done():
            self._listen_task.cancel()
        self._listen_task = asyncio.create_task(
            self._listen_loop(params), name="mock-listen"
        )
        return {"listening": True}

    async def _do_stop_listen(self, params: dict[str, Any]) -> dict[str, Any]:
        """停止采集。"""
        if self._listen_task is not None and not self._listen_task.done():
            self._listen_task.cancel()
            self._listen_task = None
        return {"listening": False}

    async def _listen_loop(self, params: dict[str, Any]) -> None:
        """按分片上传一段合成的「语音」。

        每个采集会话**只发一个** ``end`` 标记。这一点很重要：
        如果正常结束和被取消各发一次，上一个会话的第二个 end 会残留在
        PC 侧队列里，让下一次采集刚开始就立刻「结束」并拿到空音频。
        """
        sample_rate = 16_000
        pcm = speech_like_pcm(duration_s=self.config.audio_seconds, sample_rate=sample_rate)
        # 每片 20 ms，与真实固件常见的 I2S 分片大小一致。
        chunk_bytes = int(sample_rate * 0.02) * 2
        chunks = [pcm[i : i + chunk_bytes] for i in range(0, len(pcm), chunk_bytes)]

        await self._send(
            {
                "v": protocol.PROTOCOL_VERSION,
                "type": MsgType.AUDIO.value,
                "ts": protocol.now_ms(),
                "phase": "start",
                "format": "pcm_s16le",
                "sample_rate": sample_rate,
                "channels": 1,
            }
        )
        logger.info("🎤 开始上传模拟音频（%.1fs / %d 片）", self.config.audio_seconds, len(chunks))

        ended = False
        try:
            for index, chunk in enumerate(chunks):
                if self._stop.is_set():
                    return
                await self._send(
                    {
                        "v": protocol.PROTOCOL_VERSION,
                        "type": MsgType.AUDIO.value,
                        "ts": protocol.now_ms(),
                        "phase": "chunk",
                        "format": "pcm_s16le",
                        "sample_rate": sample_rate,
                        "channels": 1,
                        "seq": index,
                        "data_b64": encode_b64(chunk),
                    }
                )
                # 按真实时间推送，让 PC 侧的静音检测与超时逻辑得到真实验证。
                await asyncio.sleep(0.02)

            ended = True
            logger.info("🎤 音频上传完成")
        except asyncio.CancelledError:
            # 被 stop_listen 打断：只有在还没正常收尾时才补发 end，保证只发一次。
            if not ended:
                ended = True
                with contextlib.suppress(Exception):
                    await self._send(
                        {
                            "v": protocol.PROTOCOL_VERSION,
                            "type": MsgType.AUDIO.value,
                            "ts": protocol.now_ms(),
                            "phase": "end",
                        }
                    )
            raise

        if ended:
            await self._send(
                {
                    "v": protocol.PROTOCOL_VERSION,
                    "type": MsgType.AUDIO.value,
                    "ts": protocol.now_ms(),
                    "phase": "end",
                    "format": "pcm_s16le",
                    "sample_rate": sample_rate,
                    "channels": 1,
                }
            )

    async def _do_set_volume(self, params: dict[str, Any]) -> dict[str, Any]:
        """调整音量。"""
        self.volume = int(params.get("percent", 70))
        return {"percent": self.volume}

    async def _do_play_tone(self, params: dict[str, Any]) -> dict[str, Any]:
        """播放提示音。"""
        logger.info("🔔 提示音 %.0f Hz / %s ms",
                    float(params.get("frequency_hz", 880.0)), params.get("duration_ms", 120))
        return {"played": True}

    # -- 系统 ---------------------------------------------------------- #
    async def _do_set_led(self, params: dict[str, Any]) -> dict[str, Any]:
        """设置 RGB 灯。"""
        self.led = (int(params.get("r", 0)), int(params.get("g", 0)), int(params.get("b", 0)))
        return {"rgb": list(self.led)}

    async def _do_config(self, params: dict[str, Any]) -> dict[str, Any]:
        """运行时配置。"""
        # face_people：模拟"谁走到了摄像头前/谁走开了"，
        # 这样不用重启模拟设备就能测"换个人会不会认错"。
        if "face_people" in params:
            raw = params.get("face_people") or []
            if isinstance(raw, str):
                raw = [raw]
            self.config.face_people = [str(item) for item in raw]
            logger.info("🙂 模拟场景里的人换成：%s", self.config.face_people or "—")
        # face_luma：模拟"把灯关了"，用来验证「太暗」这条分支。
        if "face_luma" in params:
            self.config.face_luma = int(params["face_luma"])
            logger.info("💡 模拟画面亮度 = %d", self.config.face_luma)
        # face_strip_feat：模拟"设备回包缺 feat_b64"这种半截数据。
        if "face_strip_feat" in params:
            self.config.face_strip_feat = bool(params["face_strip_feat"])
            logger.info("🧪 模拟特征缺失 = %s", self.config.face_strip_feat)
        return {"applied": {k: v for k, v in params.items()}}

    async def _do_reboot(self, params: dict[str, Any]) -> dict[str, Any]:
        """模拟重启：回 result 后断开连接。"""
        logger.warning("♻️  模拟重启")
        asyncio.create_task(self._reboot_later(), name="mock-reboot")
        return {"rebooting": True}

    async def _reboot_later(self) -> None:
        """稍后断开，确保 result 已发出。"""
        await asyncio.sleep(0.5)
        self._stop.set()

    # ------------------------------------------------------------------ #
    # 后台循环
    # ------------------------------------------------------------------ #
    async def _telemetry_loop(self) -> None:
        """周期上报遥测，并让电量缓慢下降以逼近真实表现。"""
        interval = max(0.5, self.config.telemetry_ms / 1000.0)
        while not self._stop.is_set():
            try:
                await asyncio.sleep(interval)
                # 移动时耗电更快。
                drain = 0.02 if (self.motion["linear"] or self.motion["angular"]) else 0.005
                self.battery_percent = max(5.0, self.battery_percent - drain)
                self.battery_voltage = 6.4 + self.battery_percent / 100.0 * 2.0

                await self._send(
                    {
                        "v": protocol.PROTOCOL_VERSION,
                        "type": MsgType.TELEMETRY.value,
                        "ts": protocol.now_ms(),
                        "battery": {
                            "voltage": round(self.battery_voltage, 2),
                            "percent": round(self.battery_percent, 1),
                        },
                        "imu": {
                            "yaw": round(self.rotation_deg, 1),
                            "pitch": 0.0,
                            "roll": 0.0,
                        },
                        "motion": dict(self.motion),
                        "display": {"face": self.face, "backlight": self.backlight},
                        "audio": {"volume": self.volume, "listening": self._listen_task is not None},
                        "led": list(self.led),
                        "rssi": -52,
                        "uptime_ms": int((time.time() - self.uptime_start) * 1000),
                    }
                )
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001
                logger.debug("遥测发送失败: %s", exc)

    async def _trigger_loop(self) -> None:
        """周期性模拟唤醒词，用于自动验证语音闭环。"""
        while not self._stop.is_set():
            try:
                await asyncio.sleep(self.config.trigger_interval_s)
                logger.info("👋 模拟唤醒词触发")
                await self._send_event(EventName.WAKE_WORD, phrase="你好小星", source="mock")
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001
                logger.debug("触发失败: %s", exc)


# --------------------------------------------------------------------------- #
# 命令行入口
# --------------------------------------------------------------------------- #
def build_arg_parser() -> argparse.ArgumentParser:
    """构造命令行参数解析器。"""
    parser = argparse.ArgumentParser(
        prog="python -m sparkbot.mock_device",
        description="模拟一台 ESP32-S3 机器人，用于在没有硬件时验证 PC 端框架",
    )
    parser.add_argument("--url", default="ws://127.0.0.1:8765/robot", help="PC 端 WebSocket 地址")
    parser.add_argument("--id", dest="device_id", default="esp32s3-mock01", help="设备 id")
    parser.add_argument("--name", default="小星（模拟）", help="设备名字")
    parser.add_argument("--trigger", type=float, default=0.0,
                        help="每 N 秒模拟一次唤醒词（0 表示不模拟）")
    parser.add_argument("--telemetry-ms", type=int, default=3_000,
                        help="遥测上报周期毫秒（默认 3000；调小可让联调更快看到数据）")
    parser.add_argument("--stream-fps", type=float, default=0.0, help="启动即开启推流的帧率")
    parser.add_argument("--audio-seconds", type=float, default=1.8, help="每次采集模拟的语音时长")
    parser.add_argument("--face", action="append", default=None,
                        dest="faces",
                        help="模拟站在摄像头前的人（可重复，最多 4 个）；用于离线验证人脸绑定")
    parser.add_argument("--no-mic", action="store_true", help="不声明麦克风能力")
    parser.add_argument("--no-camera", action="store_true", help="不声明摄像头能力")
    parser.add_argument("--no-display", action="store_true", help="不声明显示屏能力")
    parser.add_argument("-v", "--verbose", action="store_true", help="打印每条收发的信封")
    return parser


def config_from_args(args: argparse.Namespace) -> MockDeviceConfig:
    """把命令行参数转成配置。"""
    capabilities = [
        cap
        for cap in DEFAULT_CAPABILITIES
        if not (
            (cap == "microphone" and args.no_mic)
            or (cap == "camera" and args.no_camera)
            or (cap == "display" and args.no_display)
        )
    ]
    return MockDeviceConfig(
        url=args.url,
        device_id=args.device_id,
        name=args.name,
        capabilities=tuple(capabilities),
        telemetry_ms=args.telemetry_ms,
        trigger_interval_s=args.trigger,
        stream_fps=args.stream_fps,
        audio_seconds=args.audio_seconds,
        face_people=list(args.faces or []),
        verbose=args.verbose,
    )


async def _amain(config: MockDeviceConfig) -> int:
    """异步主函数：连接、运行、优雅退出。"""
    device = MockDevice(config)

    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        with contextlib.suppress(NotImplementedError, AttributeError):
            loop.add_signal_handler(sig, lambda: asyncio.create_task(device.stop()))

    try:
        await device.run()
    except OSError as exc:
        logger.error("无法连接 %s: %s", config.url, exc)
        return 1
    return 0


def main(argv: list[str] | None = None) -> int:
    """命令行入口。"""
    parser = build_arg_parser()
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )
    try:
        return asyncio.run(_amain(config_from_args(args)))
    except KeyboardInterrupt:  # pragma: no cover - 交互式中断
        return 0
