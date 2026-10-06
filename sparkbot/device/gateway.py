"""设备网关：管理与 ESP32-S3 的 WebSocket 连接、RPC、遥测与媒体流。

职责边界
--------
本模块**只**负责链路层：连接生命周期、请求-响应配对、超时、上行分发。
它不理解「机器人该做什么」，因此被更高层的 capabilities / agent 复用时
不需要任何改动。新增一种设备或换掉传输层，只需替换本模块。

关键设计
--------
* 一条 command 一个 ``Future``：发出去的每条指令都登记在 ``_pending`` 里，
  用 id 配对设备回的 ``result``，超时即让调用方拿到
  :class:`DeviceTimeoutError`，而不是无限等待。
* 发送串行化：所有写都过一把锁，避免并发任务把 JSON 帧交叉写坏。
* 慢消费者隔离：摄像头/音频这类高频媒体不塞进 Future，而是进有界缓冲，
  满了就丢最旧的——机器人动作永远不该因为 UI 跟不上而卡住。
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Iterable, Mapping

from ..config import Settings
from ..core.errors import (
    DeviceError,
    DeviceOfflineError,
    DeviceTimeoutError,
    ProtocolViolationError,
)
from ..core.events import EventBus
from . import protocol
from .protocol import Action, Envelope, EventName, MsgType, ProtocolError

logger = logging.getLogger(__name__)


# --------------------------------------------------------------------------- #
# 媒体容器
# --------------------------------------------------------------------------- #
@dataclass(slots=True)
class Frame:
    """一帧摄像头图像（已解码为 bytes 便于直接送去 VLM / 存盘）。"""

    device_id: str
    data: bytes
    fmt: str = "jpeg"
    width: int | None = None
    height: int | None = None
    seq: int | None = None
    ts: float = field(default_factory=time.time)

    @property
    def approx_kb(self) -> float:
        """帧大小（KB），用于日志与面板展示。"""
        return round(len(self.data) / 1024, 1)


@dataclass(slots=True)
class AudioChunk:
    """一段麦克风音频。"""

    device_id: str
    data: bytes
    fmt: str = "pcm_s16le"
    sample_rate: int = 16_000
    channels: int = 1
    seq: int | None = None
    ts: float = field(default_factory=time.time)


# --------------------------------------------------------------------------- #
# 连接
# --------------------------------------------------------------------------- #
@dataclass(slots=True)
class DeviceInfo:
    """设备在 ``hello`` 中上报的自述信息。"""

    id: str
    model: str = "unknown"
    fw: str = "unknown"
    name: str = ""
    capabilities: frozenset[str] = frozenset()
    display: dict[str, Any] = field(default_factory=dict)
    camera: dict[str, Any] = field(default_factory=dict)
    audio: dict[str, Any] = field(default_factory=dict)
    motor: dict[str, Any] = field(default_factory=dict)
    raw: dict[str, Any] = field(default_factory=dict)

    def supports(self, capability: str) -> bool:
        """该设备是否声明支持某能力。"""
        return capability in self.capabilities


class DeviceConnection:
    """一条活跃的设备会话。

    上层（capabilities / agent）通过 :meth:`command` 下发指令，
    通过 :attr:`telemetry` / :attr:`frames` / :attr:`audio` 读取上行数据。
    """

    def __init__(
        self,
        *,
        device_id: str,
        websocket: Any,
        settings: Settings,
        bus: EventBus,
        session: str,
    ) -> None:
        self.device_id = device_id
        self.websocket = websocket
        self.settings = settings
        self.bus = bus
        self.session = session

        self.info: DeviceInfo | None = None
        self.connected_at = time.time()
        self.last_seen = time.time()

        #: 最近一次遥测（battery / imu / motion / rssi ...）
        self.telemetry: dict[str, Any] = {}
        self.motion: dict[str, float] = {"linear": 0.0, "angular": 0.0}

        #: 最近若干帧，供视觉工具与面板复用。
        self.frames: deque[Frame] = deque(maxlen=settings.device.frame_buffer_size)

        #: 音频分片队列；纯内存流式消费，不落盘除非上层要求。
        self.audio: asyncio.Queue[AudioChunk] = asyncio.Queue(maxsize=512)

        #: 最近一次 collect_audio 收到的分片数，供诊断。
        #: 麦克风链路坏了有两种完全不同的原因——"一片都没收到"（上行断了）
        #: 和"收到了但被静音判定提前结束"（阈值/时序问题）。有这个计数才能分清。
        self.last_audio_chunks: int | None = None

        #: 设备上行事件（唤醒词、碰撞…）的广播队列。
        self.events: asyncio.Queue[Envelope] = asyncio.Queue(maxsize=256)

        self._pending: dict[str, asyncio.Future[Envelope]] = {}
        self._send_lock = asyncio.Lock()
        self._closed = False

    # ------------------------------------------------------------------ #
    # 生命周期
    # ------------------------------------------------------------------ #
    @property
    def alive(self) -> bool:
        """连接是否仍然可用。"""
        return not self._closed

    def mark_seen(self) -> None:
        """记录一次收到上行消息，用于心跳判活。"""
        self.last_seen = time.time()

    async def close(self, *, reason: str = "closed") -> None:
        """关闭连接并让所有在途请求立刻失败。

        这里打日志是为了区分**主动关闭**与**被动感知断开**：设备自己关连接时
        这边只会看到 receive 结束，而本方法被调用说明是 PC 侧先动手。
        排查断连时，"谁先关的"是最关键的一条信息。
        """
        if self._closed:
            return
        self._closed = True
        logger.info("主动关闭连接 %s: %s", self.device_id, reason)

        for future in list(self._pending.values()):
            if not future.done():
                future.set_exception(DeviceOfflineError(f"设备 {self.device_id} 已断开: {reason}"))
        self._pending.clear()

        with contextlib.suppress(Exception):
            await self.websocket.close()

        self.bus.publish("device.disconnected", device_id=self.device_id, reason=reason)
        logger.info("设备 %s 断开: %s", self.device_id, reason)

    # ------------------------------------------------------------------ #
    # 发送
    # ------------------------------------------------------------------ #
    async def send(self, payload: Mapping[str, Any]) -> None:
        """发送一条信封。并发调用会串行化，保证帧完整。"""
        if self._closed:
            raise DeviceOfflineError(f"设备 {self.device_id} 已断开")
        text = protocol.dumps(payload)
        async with self._send_lock:
            try:
                await self.websocket.send_text(text)
            except Exception as exc:  # noqa: BLE001 - 底层实现各异，统一包装
                await self.close(reason=f"发送失败: {exc}")
                raise DeviceOfflineError(f"向设备 {self.device_id} 发送失败: {exc}") from exc

    async def send_bytes(self, data: bytes) -> None:
        """发送二进制帧（仅用于可选的高吞吐音频透传）。"""
        if self._closed:
            raise DeviceOfflineError(f"设备 {self.device_id} 已断开")
        async with self._send_lock:
            await self.websocket.send_bytes(data)

    # ------------------------------------------------------------------ #
    # RPC
    # ------------------------------------------------------------------ #
    async def command(
        self,
        action: Action | str,
        params: Mapping[str, Any] | None = None,
        *,
        timeout_ms: int | None = None,
    ) -> Envelope:
        """下发一条 command 并等待设备的 result。

        Raises:
            DeviceOfflineError: 连接已断。
            DeviceTimeoutError: 超过 ``timeout_ms`` 仍未收到回复。
        """
        if self._closed:
            raise DeviceOfflineError(f"设备 {self.device_id} 已断开")

        envelope = protocol.make_command(action, params, timeout_ms=timeout_ms)
        msg_id = envelope["id"]
        loop = asyncio.get_running_loop()
        future: asyncio.Future[Envelope] = loop.create_future()
        self._pending[msg_id] = future

        timeout_s = (timeout_ms or self.settings.device.command_timeout_ms) / 1000.0
        action_value = envelope["action"]
        try:
            await self.send(envelope)
            envelope_out = await asyncio.wait_for(future, timeout=timeout_s)
        except asyncio.TimeoutError as exc:
            raise DeviceTimeoutError(
                f"设备 {self.device_id} 未在 {timeout_s:.1f}s 内响应 {action_value}",
                action=action_value,
                msg_id=msg_id,
            ) from exc
        finally:
            self._pending.pop(msg_id, None)

        if not envelope_out.ok:
            error = envelope_out.error or {}
            raise DeviceError(
                f"设备 {self.device_id} 执行 {action_value} 失败: {error.get('message', '未知错误')}",
                action=action_value,
                code=error.get("code"),
            )
        return envelope_out

    async def intent(self, action: Action | str, params: Mapping[str, Any] | None = None) -> None:
        """下发即发即忘的 intent（用于急停与高频遥操作）。"""
        await self.send(protocol.make_intent(action, params))

    # ------------------------------------------------------------------ #
    # 上行分发（由 DeviceGateway 调用）
    # ------------------------------------------------------------------ #
    async def handle(self, envelope: Envelope) -> None:
        """把一条已解析的上行信封路由到对应的等待者或缓冲。"""
        self.mark_seen()
        msg_type = envelope.msg_type

        if msg_type is MsgType.RESULT:
            future = self._pending.get(envelope.id or "")
            if future is not None and not future.done():
                future.set_result(envelope)
            else:
                logger.debug("收到无人认领的 result: id=%s", envelope.id)
            return

        if msg_type is MsgType.TELEMETRY:
            self.telemetry = envelope.raw
            motion = envelope.raw.get("motion")
            if isinstance(motion, dict):
                self.motion = {
                    "linear": float(motion.get("linear", 0.0)),
                    "angular": float(motion.get("angular", 0.0)),
                }
            self.bus.publish("device.telemetry", device_id=self.device_id, data=envelope.raw)
            return

        if msg_type is MsgType.EVENT:
            self._offer(self.events, envelope)
            self.bus.publish(
                "device.event",
                device_id=self.device_id,
                event=envelope.raw.get("event"),
                data=envelope.raw.get("data") or {},
            )
            # 一个 result 的等待者也可能在等某个事件（如 audio_done），这里不动它。
            return

        if msg_type is MsgType.FRAME:
            await self._handle_frame(envelope)
            return

        if msg_type is MsgType.AUDIO:
            self._handle_audio(envelope)
            return

        if msg_type is MsgType.PONG:
            return

        if msg_type is MsgType.PING:
            # **必须回 pong**。
            #
            # 设备也会主动 ping（固件 PING_INTERVAL_MS=30s）。若这边只把
            # 它当成"未处理的类型"记一条警告而不回应，设备侧就认为上行
            # 无人应答。协议里 ping/pong 是对等的，任何一端收到 ping 都
            # 应当回 pong —— 这也是设备判断"PC 还在不在"的唯一依据。
            #
            # 注意这里的方法属于 DeviceConnection，用的是 self，**不是 conn**
            # （本方法早先是模块级分发的写法，改到类里后遗留了这个名字）。
            # 写成 conn 会抛 NameError，被 _receive_loop 的异常捕获后
            # **直接把连接关掉** —— 表现为每 30 秒一次 LINK LOST：
            #     WARNING gateway: 设备 ... 会话异常结束: name 'conn' is not defined
            try:
                await self.send(
                    {
                        "v": protocol.PROTOCOL_VERSION,
                        "type": MsgType.PONG.value,
                        "id": envelope.raw.get("id") or protocol.new_id(),
                        "ts": protocol.now_ms(),
                    }
                )
            except DeviceError as exc:
                logger.debug("回复 pong 失败: %s", exc)
            return

        logger.warning("设备 %s 发来未处理的类型: %s", self.device_id, envelope.type)

    # -- 内部 ---------------------------------------------------------- #
    @staticmethod
    def _offer(queue: asyncio.Queue[Any], item: Any) -> None:
        """往有界队列塞值，满了就丢最旧的一条。"""
        try:
            queue.put_nowait(item)
        except asyncio.QueueFull:
            with contextlib.suppress(asyncio.QueueEmpty):
                queue.get_nowait()
            with contextlib.suppress(asyncio.QueueFull):
                queue.put_nowait(item)

    async def _handle_frame(self, envelope: Envelope) -> None:
        import base64
        import binascii

        raw_b64 = envelope.raw.get("data_b64") or ""
        try:
            data = base64.b64decode(raw_b64, validate=True)
        except (binascii.Error, ValueError) as exc:
            logger.warning("设备 %s 发来损坏的帧: %s", self.device_id, exc)
            return

        frame = Frame(
            device_id=self.device_id,
            data=data,
            fmt=envelope.raw.get("format", "jpeg"),
            width=envelope.raw.get("width"),
            height=envelope.raw.get("height"),
            seq=envelope.raw.get("seq"),
        )
        self.frames.append(frame)
        self.bus.publish(
            "device.frame",
            device_id=self.device_id,
            seq=frame.seq,
            width=frame.width,
            height=frame.height,
            kb=frame.approx_kb,
        )

        # 如果这帧是对某条 snapshot 命令的回复，配对成 result 语义。
        future = self._pending.get(envelope.id or "")
        if future is not None and not future.done():
            future.set_result(
                Envelope(
                    type=MsgType.FRAME.value,
                    raw={
                        "v": envelope.v,
                        "type": MsgType.FRAME.value,
                        "id": envelope.id,
                        "ok": True,
                        "data": {
                            "format": frame.fmt,
                            "width": frame.width,
                            "height": frame.height,
                        },
                    },
                )
            )

    def _handle_audio(self, envelope: Envelope) -> None:
        import base64
        import binascii

        phase = envelope.raw.get("phase", "chunk")
        if phase == "start":
            self.bus.publish("device.audio_start", device_id=self.device_id)
            return
        if phase == "end":
            self._offer(self.audio, AudioChunk(device_id=self.device_id, data=b"", fmt="__end__"))
            self.bus.publish("device.audio_end", device_id=self.device_id)
            return

        raw_b64 = envelope.raw.get("data_b64") or ""
        try:
            data = base64.b64decode(raw_b64, validate=True)
        except (binascii.Error, ValueError) as exc:
            logger.warning("设备 %s 发来损坏的音频分片: %s", self.device_id, exc)
            return

        self._offer(
            self.audio,
            AudioChunk(
                device_id=self.device_id,
                data=data,
                fmt=envelope.raw.get("format", "pcm_s16le"),
                sample_rate=int(envelope.raw.get("sample_rate", 16_000)),
                channels=int(envelope.raw.get("channels", 1)),
                seq=envelope.raw.get("seq"),
            ),
        )

    # ------------------------------------------------------------------ #
    # 视图
    # ------------------------------------------------------------------ #
    def snapshot(self) -> dict[str, Any]:
        """给 API / 控制台用的连接摘要（不含二进制数据）。"""
        info = self.info
        return {
            "device_id": self.device_id,
            "session": self.session,
            "connected_at": self.connected_at,
            "last_seen": self.last_seen,
            "alive": self.alive,
            "info": {
                "model": info.model,
                "fw": info.fw,
                "name": info.name,
                "capabilities": sorted(info.capabilities),
                "display": info.display,
                "camera": info.camera,
                "audio": info.audio,
                "motor": info.motor,
            }
            if info
            else None,
            "motion": dict(self.motion),
            "telemetry": {
                k: v for k, v in self.telemetry.items() if k not in {"v", "type", "ts"}
            },
            "frames_buffered": len(self.frames),
            "pending_commands": len(self._pending),
        }


# --------------------------------------------------------------------------- #
# 网关
# --------------------------------------------------------------------------- #
class DeviceGateway:
    """所有设备连接的注册中心，并暴露给 FastAPI 使用的 WebSocket 入口。"""

    def __init__(self, settings: Settings, bus: EventBus, *, server_name: str = "sparkbot") -> None:
        self.settings = settings
        self.bus = bus
        self.server_name = server_name
        self._devices: dict[str, DeviceConnection] = {}
        #: 首帧之前用连接对象本身作临时键。
        self._limbo: dict[int, DeviceConnection] = {}
        self._watchdog: asyncio.Task[None] | None = None

    # ------------------------------------------------------------------ #
    # 查询
    # ------------------------------------------------------------------ #
    def get(self, device_id: str) -> DeviceConnection:
        """按 id 取连接。

        Raises:
            DeviceOfflineError: 设备未连接。
        """
        conn = self._devices.get(device_id)
        if conn is None or not conn.alive:
            raise DeviceOfflineError(f"设备 {device_id} 不在线")
        return conn

    def try_get(self, device_id: str) -> DeviceConnection | None:
        """按 id 取连接，取不到返回 ``None``。"""
        conn = self._devices.get(device_id)
        return conn if conn is not None and conn.alive else None

    def all(self) -> list[DeviceConnection]:
        """全部在线连接。"""
        return [c for c in self._devices.values() if c.alive]

    def first(self) -> DeviceConnection:
        """取任意一台在线设备（单机器人场景的便捷入口）。"""
        devices = self.all()
        if not devices:
            raise DeviceOfflineError("当前没有在线设备")
        return devices[0]

    def resolve(self, device_id: str | None = None) -> DeviceConnection:
        """解析目标设备：给了 id 就按 id，否则取第一台。"""
        return self.get(device_id) if device_id else self.first()

    @property
    def connected_count(self) -> int:
        """在线设备数量。"""
        return len(self.all())

    def describe(self) -> list[dict[str, Any]]:
        """所有在线连接的摘要，供 ``/api/devices`` 使用。"""
        return [c.snapshot() for c in self.all()]

    # ------------------------------------------------------------------ #
    # 连接生命周期
    # ------------------------------------------------------------------ #
    async def serve(self, websocket: Any) -> None:
        """``/robot`` 端点的连接处理函数。

        流程：等待 ``hello`` → 回 ``hello_ack`` → 进入消息循环 → 断开清理。
        """
        import secrets

        await websocket.accept()
        session = f"s-{secrets.token_hex(6)}"
        conn = DeviceConnection(
            device_id="<pending>",
            websocket=websocket,
            settings=self.settings,
            bus=self.bus,
            session=session,
        )
        self._limbo[id(conn)] = conn
        logger.info("新连接建立 session=%s", session)

        try:
            if not await self._handshake(conn):
                return
            await self._receive_loop(conn)
        except ProtocolViolationError as exc:
            logger.warning("协议违规，断开 %s: %s", conn.device_id, exc)
        except Exception as exc:  # noqa: BLE001 - 收尾必须清理注册表
            if conn.alive:
                logger.warning("设备 %s 会话异常结束: %s", conn.device_id, exc)
        finally:
            self._limbo.pop(id(conn), None)
            await conn.close(reason="会话结束")
            if self._devices.get(conn.device_id) is conn:
                del self._devices[conn.device_id]

    async def _handshake(self, conn: DeviceConnection) -> bool:
        """等待并校验 ``hello``，成功后把连接登记进注册表。"""
        deadline = self.settings.device.hello_timeout_s
        try:
            raw = await asyncio.wait_for(conn.websocket.receive_text(), timeout=deadline)
        except asyncio.TimeoutError:
            logger.warning("连接在 %.1fs 内未发送 hello，断开", deadline)
            with contextlib.suppress(Exception):
                await conn.websocket.close()
            return False

        try:
            envelope = protocol.parse_envelope(raw)
        except ProtocolError as exc:
            raise ProtocolViolationError(f"首帧不是合法信封: {exc}") from exc

        if envelope.msg_type is not MsgType.HELLO:
            raise ProtocolViolationError(f"首帧必须是 hello，实际是 {envelope.type}")

        info = self._parse_hello(envelope)
        conn.info = info
        conn.device_id = info.id

        # 同 id 重连：按配置踢掉旧连接。
        previous = self._devices.get(info.id)
        if previous is not None and previous.alive:
            if self.settings.device.replace_existing_connection:
                logger.info("设备 %s 重连，关闭旧连接", info.id)
                await previous.close(reason="被新连接替代")
            else:
                await conn.send(
                    protocol.make_hello_ack(
                        server=self.server_name,
                        session=conn.session,
                        heartbeat_ms=self.settings.device.heartbeat_ms,
                        ok=False,
                        error="device_id 已在线",
                    )
                )
                await conn.close(reason="duplicate device_id")
                return False

        self._devices[info.id] = conn
        await conn.send(
            protocol.make_hello_ack(
                server=f"{self.server_name}/{protocol.PROTOCOL_VERSION}",
                session=conn.session,
                heartbeat_ms=self.settings.device.heartbeat_ms,
            )
        )
        logger.info(
            "设备上线 id=%s name=%s model=%s 能力=%s",
            info.id,
            info.name,
            info.model,
            sorted(info.capabilities),
        )
        self.bus.publish(
            "device.connected",
            device_id=info.id,
            name=info.name,
            capabilities=sorted(info.capabilities),
        )
        return True

    @staticmethod
    def _parse_hello(envelope: Envelope) -> DeviceInfo:
        """把 ``hello`` 正文解析成 :class:`DeviceInfo`，缺字段时给安全默认值。"""
        raw_device = envelope.raw.get("device") or {}
        if not isinstance(raw_device, dict) or not raw_device.get("id"):
            raise ProtocolViolationError("hello.device.id 缺失")

        caps = envelope.raw.get("capabilities") or []
        if not isinstance(caps, list):
            raise ProtocolViolationError("hello.capabilities 必须是数组")

        return DeviceInfo(
            id=str(raw_device["id"]),
            model=str(raw_device.get("model", "unknown")),
            fw=str(raw_device.get("fw", "unknown")),
            name=str(raw_device.get("name", "")),
            capabilities=frozenset(str(c) for c in caps),
            display=dict(envelope.raw.get("display") or {}),
            camera=dict(envelope.raw.get("camera") or {}),
            audio=dict(envelope.raw.get("audio") or {}),
            motor=dict(envelope.raw.get("motor") or {}),
            raw=envelope.raw,
        )

    async def _receive_loop(self, conn: DeviceConnection) -> None:
        """主消息循环：解析每一帧并交给连接对象分发。

        这个循环的**退出原因是排查断连的关键信息**，所以每条退出路径都
        明确打日志。此前只写了 ``logger.debug``，INFO 级别下完全看不见，
        只能看到外层那句笼统的 ``断开: 会话结束`` —— 无法区分是设备主动
        关了连接，还是这边读取出错，导致定位迟迟没有进展。
        """
        while conn.alive:
            try:
                message = await conn.websocket.receive()
            except Exception as exc:  # noqa: BLE001 - 客户端断开的各种实现差异
                logger.info("设备 %s 接收循环退出: receive 异常 %s: %s",
                            conn.device_id, type(exc).__name__, exc)
                return

            msg_type = message.get("type") if isinstance(message, dict) else None
            if msg_type == "websocket.disconnect":
                # 设备（或中间设备）主动发了 close 帧
                code = message.get("code") if isinstance(message, dict) else None
                reason = message.get("reason") if isinstance(message, dict) else None
                logger.warning("设备 %s 接收循环退出: 对端发送 close 帧 "
                               "(code=%s reason=%s)",
                               conn.device_id, code, reason or "无")
                return
            if msg_type not in (None, "websocket.receive"):
                logger.debug("设备 %s 收到非数据帧: %s", conn.device_id, msg_type)
                continue

            text = message.get("text") if isinstance(message, dict) else None
            data = message.get("bytes") if isinstance(message, dict) else None

            if text is not None:
                try:
                    envelope = protocol.parse_envelope(text)
                except ProtocolError as exc:
                    logger.warning("设备 %s 发来非法信封，已忽略: %s", conn.device_id, exc)
                    continue
                await conn.handle(envelope)
            elif data is not None:
                # 二进制仅用于可选音频透传；这里按原始 PCM 入队。
                conn._offer(  # noqa: SLF001 - 同模块内的窄接口
                    conn.audio,
                    AudioChunk(device_id=conn.device_id, data=data),
                )
        # -- 循环结束 ---------------------------------------------------- #

    # ------------------------------------------------------------------ #
    # 后台任务
    # ------------------------------------------------------------------ #
    async def start(self) -> None:
        """启动心跳看护任务（在 FastAPI lifespan 中调用）。"""
        if self._watchdog is None:
            self._watchdog = asyncio.create_task(self._watchdog_loop(), name="device-watchdog")

    async def stop(self) -> None:
        """关闭全部连接并停止看护任务。"""
        if self._watchdog is not None:
            self._watchdog.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._watchdog
            self._watchdog = None
        for conn in list(self._devices.values()):
            await conn.close(reason="服务关闭")
        self._devices.clear()

    async def _watchdog_loop(self) -> None:
        """周期性 ping，并淘汰连续多个周期无上行的僵尸连接。"""
        interval = protocol.PING_INTERVAL_S
        miss_limit = protocol.MISSED_HEARTBEATS_BEFORE_DEAD
        grace = max(self.settings.device.heartbeat_ms / 1000.0, interval)
        while True:
            await asyncio.sleep(interval)
            now = time.time()
            for conn in self.all():
                try:
                    await conn.send({"v": protocol.PROTOCOL_VERSION, "type": MsgType.PING.value,
                                     "id": protocol.new_id(), "ts": protocol.now_ms()})
                except DeviceError:
                    continue
                if now - conn.last_seen > grace * miss_limit:
                    logger.warning("设备 %s 连续 %d 个周期无响应，判定离线", conn.device_id, miss_limit)
                    await conn.close(reason="心跳超时")

    # ------------------------------------------------------------------ #
    # 广播
    # ------------------------------------------------------------------ #
    async def broadcast(
        self,
        action: Action | str,
        params: Mapping[str, Any] | None = None,
        *,
        device_ids: Iterable[str] | None = None,
    ) -> dict[str, Any]:
        """向多台设备下发同一条 intent，返回各设备的成功/失败。"""
        targets = (
            [self.get(did) for did in device_ids] if device_ids is not None else self.all()
        )
        results: dict[str, Any] = {}
        for conn in targets:
            try:
                await conn.intent(action, params)
                results[conn.device_id] = "sent"
            except DeviceError as exc:
                results[conn.device_id] = f"failed: {exc.message}"
        return results
