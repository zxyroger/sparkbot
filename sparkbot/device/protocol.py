"""SparkBot 设备协议 —— 单一事实来源（Single Source of Truth）。

PC 与 ESP32-S3 之间的全部往来都使用 UTF-8 的 JSON 文本帧。
二进制帧**不**用于控制，只用于可选的原始音频透传（见 ``MEDIA_BINARY``）。

信封格式
========

``PC → 设备``（PC 下发指令，期望回复）::

    {"v": 1, "type": "command", "id": "<uuid>", "ts": 1735689600123,
     "action": "drive", "params": {"linear": 0.35, "angular": 0.0, "duration_ms": 1200}}

``PC → 设备``（不期望回复，尽力而为）::

    {"v": 1, "type": "intent", "id": "<uuid>", "ts": ..., "action": "stop", "params": {}}

``设备 → PC``（对某条 command 的回复）::

    {"v": 1, "type": "result", "id": "<同一 id>", "ok": true,
     "data": {...}, "error": null, "ts": ...}

``设备 → PC``（周期性遥测，不绑定任何 command）::

    {"v": 1, "type": "telemetry", "ts": ...,
     "battery": {"voltage": 7.9, "percent": 76},
     "imu": {"yaw": 12.5, "pitch": 0.3, "roll": -0.1},
     "motion": {"linear": 0.35, "angular": 0.0},
     "rssi": -57, "uptime_ms": 812345}

``设备 → PC``（异步事件，例如唤醒词）::

    {"v": 1, "type": "event", "ts": ..., "event": "wake_word",
     "data": {"phrase": "你好机器人"}}

``设备 → PC``（媒体，例如摄像头抓帧）::

    {"v": 1, "type": "frame", "id": "<可选，回答 snapshot command>",
     "format": "jpeg", "width": 640, "height": 480,
     "seq": 42, "data_b64": "..."}

``设备 → PC``（设备侧开始/结束一段语音输入）::

    {"v": 1, "type": "audio", "id": "<可选>", "phase": "start|chunk|end",
     "format": "pcm_s16le", "sample_rate": 16000, "channels": 1,
     "seq": 3, "data_b64": "..."}

握手
====

连接建立后设备**必须**立即（建议 3 秒内）发送 ``hello``，否则 PC 会断开它::

    {"v": 1, "type": "hello",
     "device": {"id": "esp32s3-a1b2c3", "model": "ESP32-S3-N16R8",
                "fw": "0.1.0", "name": "小星"},
     "capabilities": ["camera", "microphone", "speaker", "display", "motor"],
     "display": {"width": 240, "height": 240, "color": "rgb565"},
     "camera": {"max_width": 640, "max_height": 480, "formats": ["jpeg"]},
     "audio": {"input_rate": 16000, "output_rate": 16000, "channels": 1},
     "motor": {"kind": "differential", "max_linear": 1.0, "max_angular": 2.5}}

PC 回以 ``hello_ack``::

    {"v": 1, "type": "hello_ack", "ok": true, "server": "sparkbot/0.1.0",
     "session": "s-<hex>", "server_ts": ..., "heartbeat_ms": 10000}

心跳
====

设备侧每 ``heartbeat_ms`` 发一次 ``telemetry``；PC 侧每 30 秒发 ``ping``，
设备回 ``pong``。任一侧连续 3 个周期无消息即判定链路断开。

动作总表
========

见 ``Action`` 与 ``docs/protocol.md``。所有 ``params`` 字段都必须是
可 JSON 序列化的标量或数组；单位统一为 SI（米/秒、弧度/秒、毫秒、摄氏度）。
"""

from __future__ import annotations

import json
import time
import uuid
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Mapping

PROTOCOL_VERSION = 1

# 单个 WebSocket JSON 文本帧的软上限，超过则改用二进制帧或分块。
MAX_TEXT_FRAME_BYTES = 512 * 1024

# 设备必须在连接后该时间内完成 hello。
HELLO_TIMEOUT_S = 5.0

# PC 侧 ping 间隔与判定掉线的周期数。
PING_INTERVAL_S = 30.0
MISSED_HEARTBEATS_BEFORE_DEAD = 3


def now_ms() -> int:
    """当前 Unix 时间戳（毫秒），协议中所有 ``ts`` 字段都使用它。"""
    return int(time.time() * 1000)


def new_id() -> str:
    """生成一条消息的关联 id。"""
    return uuid.uuid4().hex[:16]


# --------------------------------------------------------------------------- #
# 消息类型
# --------------------------------------------------------------------------- #
class MsgType(str, Enum):
    """信封 ``type`` 字段的全部取值。"""

    # --- 设备 → PC ---
    HELLO = "hello"
    TELEMETRY = "telemetry"
    RESULT = "result"
    EVENT = "event"
    FRAME = "frame"
    AUDIO = "audio"
    PONG = "pong"

    # --- PC → 设备 ---
    HELLO_ACK = "hello_ack"
    COMMAND = "command"  # 期望 result 回复
    INTENT = "intent"  # 不期望回复
    PING = "ping"


class Action(str, Enum):
    """PC 可下发的动作。

    运动相关动作全部使用**开环定时**语义：设备收到后按参数执行，
    到 ``duration_ms`` 自动停止（``0`` 表示持续直到收到新的运动指令）。
    """

    # --- 底盘（差速轮）---
    DRIVE = "drive"  # linear(m/s) + angular(rad/s) + duration_ms
    STOP = "stop"  # 立即刹停，等价 duration_ms=0 的零速度
    SET_MOTION_LIMITS = "set_motion_limits"  # max_linear / max_angular 软限幅

    # --- 显示 ---
    SET_FACE = "set_face"  # emotion + intensity，查表得到表情动画
    SET_TEXT = "set_text"  # 在屏幕上叠加一行文字
    DISPLAY_FRAME = "display_frame"  # 直接把一帧图片推到 LCD
    SET_BACKLIGHT = "set_backlight"  # 0..100
    CLEAR_DISPLAY = "clear_display"

    # --- 视觉 ---
    SNAPSHOT = "snapshot"  # 抓一帧，回 frame 消息
    SET_STREAM = "set_stream"  # 启停连续推流 fps/分辨率
    SET_CAMERA_PARAMS = "set_camera_params"

    # --- 音频 ---
    PLAY_AUDIO = "play_audio"  # 播放 PC 侧生成的音频（url 或内联 base64）
    # --- 流式播放：begin → write × N → end，段落之间不断流 ---
    AUDIO_STREAM_BEGIN = "audio_stream_begin"
    AUDIO_STREAM_WRITE = "audio_stream_write"  # data_b64 = 16k 单声道 16bit 裸 PCM
    AUDIO_STREAM_END = "audio_stream_end"
    TTS_SPEAK = "tts_speak"  # 让设备用板载 TTS 说一句（可选能力）
    START_LISTEN = "start_listen"  # 开始采集麦克风并回传 audio 帧
    STOP_LISTEN = "stop_listen"
    SET_VOLUME = "set_volume"  # 0..100
    PLAY_TONE = "play_tone"  # 蜂鸣提示音，用于反馈

    # --- 系统 ---
    REBOOT = "reboot"
    SET_LED = "set_led"  # 板载 RGB / 状态灯
    CONFIG = "config"  # 运行时改参数（心跳周期、推流开关等）


class Emotion(str, Enum):
    """``SET_FACE`` 支持的表情名，设备侧为每张表情实现一段动画。

    设备可以只实现其中一部分；PC 侧会根据 hello 里的 ``display`` 能力降级。
    """

    NEUTRAL = "neutral"
    HAPPY = "happy"
    SAD = "sad"
    ANGRY = "angry"
    SURPRISED = "surprised"
    SLEEPY = "sleepy"
    CONFUSED = "confused"
    THINKING = "thinking"
    LOVE = "love"
    EXCITED = "excited"
    SCARED = "scared"
    BORED = "bored"


class EventName(str, Enum):
    """``EVENT`` 消息的取值。"""

    WAKE_WORD = "wake_word"  # 本地唤醒词命中，之后设备开始上传音频
    TOUCH = "touch"  # 触摸/按键
    BUTTON = "button"
    OBSTACLE = "obstacle"  # 前向测距触发
    CLIFF = "cliff"  # 掉落检测
    BUMP = "bump"  # 碰撞开关
    LOW_BATTERY = "low_battery"
    OVERHEAT = "overheat"
    ERROR = "error"
    MOTION_DONE = "motion_done"  # 开环定时运动走完
    AUDIO_DONE = "audio_done"  # 播报结束
    LISTEN_TIMEOUT = "listen_timeout"  # 静音超时，音频流自动结束


class ErrorCode(str, Enum):
    """``RESULT.error.code`` 的取值，便于 PC 侧做分支处理。"""

    UNSUPPORTED_ACTION = "unsupported_action"
    UNSUPPORTED_PARAM = "unsupported_param"
    BAD_PARAMS = "bad_params"
    BUSY = "busy"
    HARDWARE_FAULT = "hardware_fault"
    TIMEOUT = "timeout"
    LOW_BATTERY = "low_battery"
    INTERNAL = "internal"


# --------------------------------------------------------------------------- #
# 构造辅助
# --------------------------------------------------------------------------- #
def make_command(
    action: Action | str,
    params: Mapping[str, Any] | None = None,
    *,
    msg_id: str | None = None,
    timeout_ms: int | None = None,
) -> dict[str, Any]:
    """构造一条 ``COMMAND`` 信封（期望设备回 ``RESULT``）。"""
    action_value = action.value if isinstance(action, Action) else str(action)
    envelope: dict[str, Any] = {
        "v": PROTOCOL_VERSION,
        "type": MsgType.COMMAND.value,
        "id": msg_id or new_id(),
        "ts": now_ms(),
        "action": action_value,
        "params": dict(params or {}),
    }
    if timeout_ms is not None:
        envelope["timeout_ms"] = int(timeout_ms)
    return envelope


def make_intent(action: Action | str, params: Mapping[str, Any] | None = None) -> dict[str, Any]:
    """构造一条 ``INTENT`` 信封（即发即忘，用于高频遥操作与急停）。"""
    action_value = action.value if isinstance(action, Action) else str(action)
    return {
        "v": PROTOCOL_VERSION,
        "type": MsgType.INTENT.value,
        "id": new_id(),
        "ts": now_ms(),
        "action": action_value,
        "params": dict(params or {}),
    }


def make_hello_ack(
    *, server: str, session: str, heartbeat_ms: int, ok: bool = True, error: str | None = None
) -> dict[str, Any]:
    """构造 PC 侧对 ``HELLO`` 的应答。"""
    return {
        "v": PROTOCOL_VERSION,
        "type": MsgType.HELLO_ACK.value,
        "id": new_id(),
        "ts": now_ms(),
        "ok": ok,
        "error": error,
        "server": server,
        "session": session,
        "server_ts": now_ms(),
        "heartbeat_ms": int(heartbeat_ms),
    }


# --------------------------------------------------------------------------- #
# 解析
# --------------------------------------------------------------------------- #
class ProtocolError(Exception):
    """收到不符合协议的信封时抛出。"""

    def __init__(self, message: str, *, raw: Any = None) -> None:
        super().__init__(message)
        self.raw = raw


@dataclass(slots=True)
class Envelope:
    """解析后的信封。未知字段保留在 ``extra`` 中，保证前向兼容。"""

    type: str
    raw: dict[str, Any]
    v: int = PROTOCOL_VERSION
    id: str | None = None
    ts: int | None = None
    action: str | None = None
    params: dict[str, Any] = field(default_factory=dict)
    extra: dict[str, Any] = field(default_factory=dict)

    # -- RESULT 便捷访问 ------------------------------------------------ #
    @property
    def ok(self) -> bool:
        """``RESULT`` 是否成功；其它类型一律返回 True。"""
        return bool(self.raw.get("ok", True))

    @property
    def error(self) -> dict[str, Any] | None:
        """``RESULT`` 的错误对象 ``{"code": ..., "message": ...}``。"""
        err = self.raw.get("error")
        return err if isinstance(err, dict) else None

    @property
    def data(self) -> dict[str, Any]:
        """``RESULT`` 携带的数据体。"""
        data = self.raw.get("data")
        return data if isinstance(data, dict) else {}

    @property
    def msg_type(self) -> MsgType | None:
        """把 ``type`` 映射成枚举，未知类型返回 ``None``。"""
        try:
            return MsgType(self.type)
        except ValueError:
            return None

    @property
    def action_enum(self) -> Action | None:
        """把 ``action`` 映射成枚举，未知动作返回 ``None``。"""
        if self.action is None:
            return None
        try:
            return Action(self.action)
        except ValueError:
            return None


_KNOWN_FIELDS = {"v", "type", "id", "ts", "action", "params"}


def parse_envelope(text: str | bytes) -> Envelope:
    """把一段 JSON 文本解析成 :class:`Envelope`。

    Raises:
        ProtocolError: 不是合法 JSON、缺少 ``type``、或版本不兼容。
    """
    if isinstance(text, bytes):
        try:
            text = text.decode("utf-8")
        except UnicodeDecodeError as exc:  # pragma: no cover - 防御性
            raise ProtocolError(f"帧不是合法 UTF-8: {exc}") from exc

    if len(text) > MAX_TEXT_FRAME_BYTES:
        raise ProtocolError(f"文本帧过大: {len(text)} 字节")

    try:
        obj = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ProtocolError(f"不是合法 JSON: {exc}", raw=text[:200]) from exc

    if not isinstance(obj, dict):
        raise ProtocolError("信封根节点必须是 JSON 对象", raw=obj)
    if "type" not in obj:
        raise ProtocolError("信封缺少 type 字段", raw=obj)

    version = obj.get("v", PROTOCOL_VERSION)
    if not isinstance(version, int) or version > PROTOCOL_VERSION:
        raise ProtocolError(f"协议版本不兼容: v={version!r}", raw=obj)

    params = obj.get("params") or {}
    if not isinstance(params, dict):
        raise ProtocolError("params 必须是 JSON 对象", raw=obj)

    return Envelope(
        type=str(obj["type"]),
        raw=obj,
        v=version,
        id=obj.get("id"),
        ts=obj.get("ts"),
        action=obj.get("action"),
        params=params,
        extra={k: val for k, val in obj.items() if k not in _KNOWN_FIELDS},
    )


def dumps(payload: Mapping[str, Any]) -> str:
    """把信封序列化成紧凑 JSON（不保留空格，省带宽）。"""
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
