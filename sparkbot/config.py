"""配置层：全部可选项都可通过 ``SPARKBOT_`` 前缀的环境变量或 ``.env`` 覆盖。"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Any, Literal

from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class ServerSettings(BaseSettings):
    """HTTP / WebSocket 服务端监听参数。"""

    model_config = SettingsConfigDict(env_prefix="SPARKBOT_SERVER_", extra="ignore")

    host: str = "0.0.0.0"
    port: int = 8765
    ws_path: str = "/robot"
    log_level: Literal["debug", "info", "warning", "error"] = "info"
    # 允许跨域访问控制台/接口的来源；``*`` 仅建议在可信内网使用。
    cors_origins: list[str] = Field(default_factory=lambda: ["*"])


class DeviceSettings(BaseSettings):
    """设备网关参数。"""

    model_config = SettingsConfigDict(env_prefix="SPARKBOT_DEVICE_", extra="ignore")

    heartbeat_ms: int = 10_000
    """下发给设备的心跳/遥测周期，写进 ``hello_ack``。"""

    command_timeout_ms: int = 5_000
    """单条 command 等待 result 的默认超时。"""

    hello_timeout_s: float = 5.0
    """连接后等待 hello 的时限，超时即断开。"""

    # 推流/抓帧在内存里保留的最近帧数，供视觉工具与面板复用。
    frame_buffer_size: int = 8
    # 音频分片在这台 PC 上缓存的秒数，防止长时间监听吃满内存。
    audio_buffer_seconds: float = 30.0
    # 同一 device_id 重连时是否踢掉旧连接。
    replace_existing_connection: bool = True


class LLMSettings(BaseSettings):
    """大模型 provider 参数。

    ``provider`` 决定走哪套适配器：
      * ``openai``     —— 官方 OpenAI（也兼容任何 OpenAI 形状的网关）
      * ``deepseek``   —— DeepSeek 官方，走 OpenAI 兼容协议
      * ``openai_compat`` —— 自建/第三方兼容网关，需显式给 ``base_url``
      * ``mock``       —— 离线假模型，用于不联网跑通全链路
    """

    model_config = SettingsConfigDict(
        env_prefix="SPARKBOT_LLM_",
        extra="ignore",
        env_file=".env",
        env_file_encoding="utf-8",
    )

    provider: Literal["openai", "deepseek", "openai_compat", "mock"] = "mock"
    model: str = "gpt-4o-mini"
    base_url: str | None = None
    api_key: SecretStr | None = None
    temperature: float = 0.6
    max_tokens: int = 1024
    request_timeout_s: float = 60.0
    max_tool_rounds: int = 6
    """单轮对话里最多允许几轮「模型思考 → 调用工具」循环，防止死循环。"""


class VisionSettings(BaseSettings):
    """视觉理解参数。"""

    model_config = SettingsConfigDict(
        env_prefix="SPARKBOT_VISION_",
        extra="ignore",
        env_file=".env",
        env_file_encoding="utf-8",
    )

    enabled: bool = True
    # 抓帧时向设备请求的分辨率，越小越快。
    request_width: int = 640
    request_height: int = 480
    jpeg_quality: int = 80
    # 送进 VLM 前把图缩到该最长边，控制 token 成本。
    downscale_max_edge: int = 768
    # 独立的视觉模型；留空则复用 LLM 的 provider 与 key。
    provider: Literal["openai", "deepseek", "openai_compat", "mock"] | None = None
    model: str | None = None
    base_url: str | None = None
    api_key: SecretStr | None = None
    timeout_s: float = 45.0


class SpeechSettings(BaseSettings):
    """语音链路参数。"""

    model_config = SettingsConfigDict(
        env_prefix="SPARKBOT_SPEECH_",
        extra="ignore",
        # 必须显式指定 env_file：嵌套的 BaseSettings 子模型**不会**继承
        # 根 Settings 的 env_file 设置。不写这一行的后果是"控制台改的配置
        # 重启后全部丢失" —— 只读环境变量，完全不看 .env 文件。
        # 实测：SpeechSettings() 读到默认值 mock，而
        #       SpeechSettings(_env_file=".env") 才正确读到 openai。
        env_file=".env",
        env_file_encoding="utf-8",
    )

    asr_provider: Literal["openai", "mock", "disabled"] = "mock"
    asr_model: str = "whisper-1"
    asr_base_url: str | None = None
    asr_api_key: SecretStr | None = None

    tts_provider: Literal["openai", "mock", "disabled"] = "mock"
    tts_model: str = "tts-1"
    tts_voice: str = "alloy"
    tts_base_url: str | None = None
    tts_api_key: SecretStr | None = None
    tts_format: Literal["wav", "mp3"] = "wav"

    # 板载唤醒词命中后，PC 侧等待音频段的静音超时（秒）。
    listen_timeout_s: float = 8.0
    # 麦克风采样率，必须与设备 hello.audio.input_rate 一致。
    input_sample_rate: int = 16_000

    # 云端返回的音频是否缓存到 artifacts/audio。
    cache_audio: bool = True

    #: 播报走"流式下发"（边合成边推、设备边收边播）还是老的"整段下发"。
    #:
    #: 默认 **False**：整段下发是老路径，实测音频最干净。
    #: 流式下发首字延迟更低（0.5s vs 2.9s），但在本板上出现过用户可闻的
    #: 杂音（播放期间持续收发 + 被打断），所以默认关掉、作为可选实验项。
    #: 打开：SPARKBOT_SPEECH_TTS_STREAM_PLAYBACK=true
    tts_stream_playback: bool = False


class LongTermMemorySettings(BaseSettings):
    """长期记忆参数（跨会话保留的事实）。"""

    model_config = SettingsConfigDict(
        env_prefix="SPARKBOT_MEMORY_",
        extra="ignore",
        env_file=".env",
        env_file_encoding="utf-8",
    )

    # 总开关。关掉后既不读取也不写入记忆文件。
    enabled: bool = True
    #: 记忆文件路径（相对项目根目录）。
    path: str = "artifacts/memory/facts.jsonl"
    #: 最多保留多少条事实；超出后按"重要性 + 使用频次 - 时间衰减"淘汰。
    capacity: int = 500
    #: 每轮对话最多把几条相关记忆注入 system prompt。
    #: 这个数直接吃 token，也直接影响模型会不会被无关信息带偏，别调太高。
    max_injected: int = 5
    #: 是否自动从用户话语里抽取事实（无需模型显式调用 remember 工具）。
    auto_extract: bool = True


class FaceSettings(BaseSettings):
    """人脸识别参数（设备端本地推理 + PC 端名字绑定）。

    推理在 ESP32-S3 上跑（esp-dl 的 MSR+MNP 检测 + MFN 提特征），设备只回
    512 维特征向量；"这是谁"由 PC 侧的人脸库回答 —— 见 perception/face.py。
    """

    model_config = SettingsConfigDict(
        env_prefix="SPARKBOT_FACE_",
        extra="ignore",
        env_file=".env",
        env_file_encoding="utf-8",
    )

    # 总开关。关掉后不做摄像头扫描，人脸相关工具会明确回答"未启用"。
    enabled: bool = True
    #: 人脸库文件路径（相对项目根目录）。
    path: str = "artifacts/faces/face_db.json"
    #: 判定"是同一个人"的余弦相似度阈值。
    #: 特征在设备端已 L2 归一化，点积即余弦；0.5 沿用 esp-dl
    #: HumanFaceRecognizer 的默认阈值（同一个模型，判据一致）。
    threshold: float = 0.5
    #: 同一个人最多保留几条特征（不同角度各一条，匹配时取最高分）。
    max_samples: int = 8
    #: 每轮对话开始前是否自动扫一次脸，把"现在面前是谁"注入上下文。
    #: 单次约 0.4~0.8 秒（设备端推理），在意响应速度可以关掉。
    auto_scan: bool = True
    #: 用户自我介绍（"我叫…""我是…"）时，是否自动把当前这张脸绑到那个名字。
    auto_enroll: bool = True
    #: 每轮最多把几个人的身份注入 system prompt。
    max_injected: int = 2


class BehaviorSettings(BaseSettings):
    """Agent 行为策略参数。"""

    model_config = SettingsConfigDict(
        env_prefix="SPARKBOT_BEHAVIOR_",
        extra="ignore",
        env_file=".env",
        env_file_encoding="utf-8",
    )

    # 是否在工具执行前做安全审查（速度钳制、命令频率限制）。
    safety_enabled: bool = True
    max_linear_mps: float = 0.6
    max_angular_rps: float = 2.0
    # 单次运动指令的最长持续时间，防止「一直往前冲」。
    max_duration_ms: int = 5_000
    # 两次运动指令之间的最小间隔，抑制抖动。
    min_command_interval_ms: int = 120
    # 说话时是否自动张嘴/切换表情，让机器人显得在互动。
    talking_animation: bool = True
    # 会话历史保留条数。
    history_limit: int = 40

    # 一次唤醒后连续对话的轮数。
    #
    # 为什么需要这个参数：设备只在收到 ``start_listen`` 时才上传音频，
    # 所以"采集一次 → 识别 → 回复 → 播报"这套流程走完就**停止录音**了。
    # 用户如果想接着说第二句，麦克风根本没在采集，自然没有回应 ——
    # 表现为"唤醒后只能对话一句"。
    #
    # 设为 1 表示只回一句就停（旧行为）；大于 1 会在一轮结束后自动再采集，
    # 让用户可以连续说几句而不用反复喊唤醒词。
    voice_session_turns: int = 3
    # 连续对话中，两轮之间的等待时间（秒）。
    #
    # 必须留出这段时间：一是让设备的扬声器把话说完（否则麦克风会把
    # 机器人自己的声音当成用户输入），二是给用户一点反应时间。
    voice_session_gap_s: float = 1.2


class Settings(BaseSettings):
    """聚合配置根对象。"""

    model_config = SettingsConfigDict(
        env_prefix="SPARKBOT_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    server: ServerSettings = Field(default_factory=ServerSettings)
    device: DeviceSettings = Field(default_factory=DeviceSettings)
    llm: LLMSettings = Field(default_factory=LLMSettings)
    vision: VisionSettings = Field(default_factory=VisionSettings)
    speech: SpeechSettings = Field(default_factory=SpeechSettings)
    behavior: BehaviorSettings = Field(default_factory=BehaviorSettings)
    memory: LongTermMemorySettings = Field(default_factory=LongTermMemorySettings)
    face: FaceSettings = Field(default_factory=FaceSettings)

    # 运行期产物目录（帧、音频、日志）。
    artifacts_dir: str = "artifacts"
    # 机器人人格设定，会注入 system prompt。
    persona: str = (
        "你是「小星」，一台可爱的履带式桌面机器人。"
        "你能看到眼前的东西、能听见别人说话、会转动身体、会用屏幕表达情绪。"
        "说话简短、口语化、有温度，每次回复不超过三句话。"
        "需要了解周围环境时主动调用看图的工具，不要凭空猜测。"
    )


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """返回进程级单例配置。测试中可 ``get_settings.cache_clear()`` 重置。"""
    return Settings()


# --------------------------------------------------------------------------- #
# 运行期配置读写
# --------------------------------------------------------------------------- #
#: 可以在运行期通过 Web 控制台查看与修改的配置项。
#:
#: 格式：``"分组.字段": "SPARKBOT_ 环境变量名"``。
#: 只有在这里登记过的字段才会被 ``/api/config`` 暴露与接受——
#: 这是一道明确的护栏，避免把监听地址、CORS 之类的启动期设置
#: 在运行中被改掉（那些改了也不会生效，只会让人困惑）。
EDITABLE_FIELDS: dict[str, str] = {
    "llm.provider": "SPARKBOT_LLM_PROVIDER",
    "llm.model": "SPARKBOT_LLM_MODEL",
    "llm.base_url": "SPARKBOT_LLM_BASE_URL",
    "llm.api_key": "SPARKBOT_LLM_API_KEY",
    "llm.temperature": "SPARKBOT_LLM_TEMPERATURE",
    "llm.max_tokens": "SPARKBOT_LLM_MAX_TOKENS",
    "llm.max_tool_rounds": "SPARKBOT_LLM_MAX_TOOL_ROUNDS",
    "vision.enabled": "SPARKBOT_VISION_ENABLED",
    "vision.provider": "SPARKBOT_VISION_PROVIDER",
    "vision.model": "SPARKBOT_VISION_MODEL",
    "vision.base_url": "SPARKBOT_VISION_BASE_URL",
    "vision.api_key": "SPARKBOT_VISION_API_KEY",
    "speech.asr_provider": "SPARKBOT_SPEECH_ASR_PROVIDER",
    "speech.asr_api_key": "SPARKBOT_SPEECH_ASR_API_KEY",
    # base_url 与 model 必须可配，否则只能用 OpenAI 官方：
    # 很多国内服务（如硅基流动）提供 OpenAI 兼容的
    # /audio/transcriptions 与 /audio/speech，只要改这两项就能用。
    "speech.asr_base_url": "SPARKBOT_SPEECH_ASR_BASE_URL",
    "speech.asr_model": "SPARKBOT_SPEECH_ASR_MODEL",
    "speech.tts_provider": "SPARKBOT_SPEECH_TTS_PROVIDER",
    "speech.tts_api_key": "SPARKBOT_SPEECH_TTS_API_KEY",
    "speech.tts_base_url": "SPARKBOT_SPEECH_TTS_BASE_URL",
    "speech.tts_model": "SPARKBOT_SPEECH_TTS_MODEL",
    "speech.tts_voice": "SPARKBOT_SPEECH_TTS_VOICE",
    # tts_format 必须是可配的：本地 TTS（如 edge-tts 包装服务）通常只出
    # WAV，而云端 OpenAI 用 mp3 更省流量。写死任一种都会让另一类服务不可用。
    "speech.tts_format": "SPARKBOT_SPEECH_TTS_FORMAT",
    "speech.tts_stream_playback": "SPARKBOT_SPEECH_TTS_STREAM_PLAYBACK",
    "speech.listen_timeout_s": "SPARKBOT_SPEECH_LISTEN_TIMEOUT_S",
    "behavior.max_linear_mps": "SPARKBOT_BEHAVIOR_MAX_LINEAR_MPS",
    "behavior.max_angular_rps": "SPARKBOT_BEHAVIOR_MAX_ANGULAR_RPS",
    "behavior.max_duration_ms": "SPARKBOT_BEHAVIOR_MAX_DURATION_MS",
    "behavior.talking_animation": "SPARKBOT_BEHAVIOR_TALKING_ANIMATION",
    "behavior.history_limit": "SPARKBOT_BEHAVIOR_HISTORY_LIMIT",
    "behavior.voice_session_turns": "SPARKBOT_BEHAVIOR_VOICE_SESSION_TURNS",
    "behavior.voice_session_gap_s": "SPARKBOT_BEHAVIOR_VOICE_SESSION_GAP_S",
    "memory.enabled": "SPARKBOT_MEMORY_ENABLED",
    "memory.max_injected": "SPARKBOT_MEMORY_MAX_INJECTED",
    "memory.auto_extract": "SPARKBOT_MEMORY_AUTO_EXTRACT",
    "memory.capacity": "SPARKBOT_MEMORY_CAPACITY",
    "face.enabled": "SPARKBOT_FACE_ENABLED",
    "face.threshold": "SPARKBOT_FACE_THRESHOLD",
    "face.auto_scan": "SPARKBOT_FACE_AUTO_SCAN",
    "face.auto_enroll": "SPARKBOT_FACE_AUTO_ENROLL",
    "face.max_injected": "SPARKBOT_FACE_MAX_INJECTED",
    "face.max_samples": "SPARKBOT_FACE_MAX_SAMPLES",
    "persona": "SPARKBOT_PERSONA",
}

#: 字段名里含这些字样的一律当作密钥，读取时打码、写回时"留空即不变"。
SECRET_HINTS = ("api_key", "secret", "token", "password")

#: ``provider`` 字段的合法取值，供控制台渲染下拉框并做服务端校验。
PROVIDER_CHOICES: dict[str, list[str]] = {
    "llm.provider": ["mock", "openai", "deepseek", "openai_compat"],
    "vision.provider": ["", "mock", "openai", "deepseek", "openai_compat"],
    "speech.asr_provider": ["mock", "openai", "disabled"],
    "speech.tts_provider": ["mock", "openai", "disabled"],
}


def is_secret_field(field: str) -> bool:
    """该字段是否为密钥（需要打码）。"""
    return any(hint in field for hint in SECRET_HINTS)


def get_field(settings: Settings, field: str) -> Any:
    """按 ``"分组.字段"`` 路径读取配置值；根级字段直接写字段名。

    Raises:
        KeyError: 路径不存在。
    """
    if "." not in field:
        return getattr(settings, field)
    group, _, name = field.partition(".")
    target = getattr(settings, group)
    return getattr(target, name)


def set_field(settings: Settings, field: str, value: Any) -> None:
    """按 ``"分组.字段"`` 路径写入配置值。

    ``llm.api_key`` 这类字段的值是 pydantic 的 ``SecretStr``，
    写入时需要包一层，否则后续 ``.get_secret_value()`` 会失败。
    """
    group, _, name = field.partition(".")
    target = settings if not name else getattr(settings, group)
    attr = name or field

    if is_secret_field(field) and isinstance(value, str):
        from pydantic import SecretStr

        target_value = SecretStr(value) if value else None
    else:
        target_value = value
    setattr(target, attr, target_value)


def secret_plain(settings: Settings, field: str) -> str:
    """取出密钥的明文（用于判断"是否已配置"），取不到返回空串。"""
    try:
        value = get_field(settings, field)
    except (AttributeError, KeyError):
        return ""
    if value is None:
        return ""
    if hasattr(value, "get_secret_value"):
        return value.get_secret_value()
    return str(value)


def config_snapshot(settings: Settings) -> dict[str, Any]:
    """导出可安全下发给前端的配置快照。

    密钥**绝不**回传明文，只回 ``api_key_set`` 布尔量——
    否则控制台页面、浏览器缓存、代理日志里都会留下 key。
    """
    payload: dict[str, Any] = {}
    for field in EDITABLE_FIELDS:
        try:
            raw = get_field(settings, field)
        except (AttributeError, KeyError):
            continue
        if is_secret_field(field):
            payload[f"{field}_set"] = bool(secret_plain(settings, field))
            payload[field] = ""
        elif hasattr(raw, "value") and not isinstance(raw, str):  # Enum
            payload[field] = raw.value
        else:
            payload[field] = raw
    return payload


def persist_env(path: str | Path, updates: dict[str, str]) -> int:
    """把配置写回 ``.env``，返回写入的条目数。

    刻意做成"读原文 → 逐行替换或追加"而不是重新生成整个文件：
    ``.env`` 里通常还有注释和手工调整的项，重写会全部丢掉。

    值为空字符串表示**删除该项**（用于清空 API key）。
    """
    import os

    env_path = Path(path)
    existing: dict[str, str] = {}
    lines: list[str] = []

    if env_path.exists():
        lines = env_path.read_text(encoding="utf-8").splitlines()

    for line in lines:
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        key, _, value = stripped.partition("=")
        existing[key.strip()] = value.strip()

    written = 0
    for key, value in updates.items():
        if existing.get(key) == value:
            continue
        existing[key] = value
        written += 1

    # 用最新状态重建，但保留注释行与原有顺序。
    output: list[str] = []
    emitted: set[str] = set()
    for line in lines:
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            output.append(line)
            continue
        key = stripped.partition("=")[0].strip()
        if key in existing:
            output.append(f"{key}={existing[key]}")
            emitted.add(key)
        else:
            output.append(line)

    remaining = {k: v for k, v in existing.items() if k not in emitted}
    if remaining:
        if output and output[-1].strip():
            output.append("")
        output.append("# ---- 由 Web 控制台写入 ----")
        output.extend(f"{key}={value}" for key, value in sorted(remaining.items()))

    temp_path = env_path.with_suffix(env_path.suffix + ".tmp")
    temp_path.write_text("\n".join(output) + "\n", encoding="utf-8")
    os.replace(temp_path, env_path)
    return written
