"""语音链路：ASR（麦克风 → 文本）与 TTS（文本 → 喇叭音频）。

与 LLM 层同样采用「接口 + 可插拔实现」：

* :class:`ASRProvider` / :class:`TTSProvider` —— 抽象接口；
* ``OpenAISpeech`` —— 云端实现（``/audio/transcriptions``、``/audio/speech``）；
* ``MockASR`` / ``MockTTS`` —— 离线实现，保证不联网也能跑通语音闭环。

音频约定：设备侧上行统一为 **16 kHz / 单声道 / 16-bit PCM**（ESP32 麦克风默认），
因此在送入 ASR 之前必须包成 WAV 容器——裸 PCM 没有采样率信息，云端会拒收。
"""

from __future__ import annotations

import array
import io
import logging
import math
import struct
import wave
from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Any

import httpx

from ..config import SpeechSettings
from ..core.errors import ProviderError

logger = logging.getLogger(__name__)

#: 设备上报的默认音频参数，与 ``config.SpeechSettings`` 保持一致。
DEFAULT_SAMPLE_RATE = 16_000
DEFAULT_CHANNELS = 1
DEFAULT_SAMPLE_WIDTH = 2


# --------------------------------------------------------------------------- #
# WAV 工具
# --------------------------------------------------------------------------- #
def pcm_to_wav(
    pcm: bytes,
    *,
    sample_rate: int = DEFAULT_SAMPLE_RATE,
    channels: int = DEFAULT_CHANNELS,
    sample_width: int = DEFAULT_SAMPLE_WIDTH,
) -> bytes:
    """把裸 PCM 包成 WAV，供 ASR/播放器使用。

    Args:
        pcm: 交错排列的原始采样数据。
        sample_rate: 采样率 Hz。
        channels: 声道数。
        sample_width: 每采样字节数，16-bit 即 2。
    """
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as handle:
        handle.setnchannels(channels)
        handle.setsampwidth(sample_width)
        handle.setframerate(sample_rate)
        handle.writeframes(pcm)
    return buffer.getvalue()


def wav_to_pcm(data: bytes) -> tuple[bytes, int, int]:
    """把 WAV 解成 ``(pcm, sample_rate, channels)``；不是 WAV 则按配置原样返回。

    设备可能只支持裸 PCM 播放，所以 TTS 的 WAV 输出需要这条转换路径。
    """
    try:
        with wave.open(io.BytesIO(data), "rb") as handle:
            return handle.readframes(handle.getnframes()), handle.getframerate(), handle.getnchannels()
    except wave.Error:
        # 已经是裸 PCM 或 mp3，交给上层按格式字段处理。
        return data, DEFAULT_SAMPLE_RATE, DEFAULT_CHANNELS


def pcm_duration_s(pcm: bytes, *, sample_rate: int = DEFAULT_SAMPLE_RATE,
                   channels: int = DEFAULT_CHANNELS, sample_width: int = DEFAULT_SAMPLE_WIDTH) -> float:
    """估算 PCM 时长（秒）。"""
    bytes_per_second = max(1, sample_rate * channels * sample_width)
    return len(pcm) / bytes_per_second


def silence_pcm(
    seconds: float, *, sample_rate: int = DEFAULT_SAMPLE_RATE, channels: int = DEFAULT_CHANNELS
) -> bytes:
    """生成一段静音 PCM，用于占位或做音频拼接间隔。"""
    frames = int(max(0.0, seconds) * sample_rate)
    return b"\x00\x00" * frames * channels


def tone_wav(
    *,
    frequency_hz: float = 660.0,
    duration_ms: int = 200,
    sample_rate: int = DEFAULT_SAMPLE_RATE,
    volume: float = 0.25,
) -> bytes:
    """合成一段正弦提示音的 WAV。

    用途：没有配置云端 TTS 时，让机器人至少能「出声回应」，
    避免调试时误判成喇叭坏了。
    """
    total = int(max(1, duration_ms) * sample_rate / 1000)
    amplitude = int(max(0.0, min(1.0, volume)) * 32767)
    samples = array.array("h")
    fade = max(1, total // 20)  # 两端各做 5% 淡入淡出，消除爆音
    for index in range(total):
        envelope = min(1.0, index / fade, (total - index) / fade)
        value = int(amplitude * envelope * math.sin(2 * math.pi * frequency_hz * index / sample_rate))
        samples.append(value)

    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(sample_rate)
        handle.writeframes(samples.tobytes())
    return buffer.getvalue()


def speech_wav(text: str, *, sample_rate: int = DEFAULT_SAMPLE_RATE) -> bytes:
    """把文本「唱」成一段可听的提示音序列（离线 TTS 占位实现）。

    不是真语音合成，但会让每个字/词对应一声不同音高的短音，
    听感上能反映文本长度，便于验证「PC → 设备喇叭」链路是通的。
    """
    body = [s for s in text.strip() if not s.isspace()]
    if not body:
        return tone_wav(frequency_hz=520.0, duration_ms=160, sample_rate=sample_rate)

    chunks: list[bytes] = []
    for index, char in enumerate(body[:60]):
        # 用字符码映射到 440~880 Hz，句子有起伏但不刺耳。
        frequency = 440.0 + (ord(char) % 24) * 18.0
        chunks.append(tone_wav(frequency_hz=frequency, duration_ms=90, sample_rate=sample_rate))
        if char in "，。！？,.!?":
            chunks.append(silence_pcm(0.18, sample_rate=sample_rate))
        elif index % 4 == 3:
            chunks.append(silence_pcm(0.08, sample_rate=sample_rate))
    return pcm_to_wav(b"".join(chunks), sample_rate=sample_rate)


# --------------------------------------------------------------------------- #
# 接口
# --------------------------------------------------------------------------- #
@dataclass(slots=True)
class Transcript:
    """一次语音识别结果。"""

    text: str
    confidence: float | None = None
    language: str | None = None
    duration_s: float | None = None
    provider: str = ""

    def to_dict(self) -> dict[str, Any]:
        """转成可序列化字典。"""
        return {
            "text": self.text,
            "confidence": self.confidence,
            "language": self.language,
            "duration_s": self.duration_s,
            "provider": self.provider,
        }


class ASRProvider(ABC):
    """语音识别接口。"""

    name: str = "asr"

    @abstractmethod
    async def transcribe(self, pcm: bytes, *, sample_rate: int = DEFAULT_SAMPLE_RATE) -> Transcript:
        """把 PCM 音频转成文本。

        Args:
            pcm: 裸 PCM（16-bit）。
            sample_rate: 采样率 Hz。
        """

    async def aclose(self) -> None:
        """释放资源。"""
        return None


class TTSProvider(ABC):
    """语音合成接口。"""

    name: str = "tts"

    @abstractmethod
    async def synthesize(self, text: str) -> tuple[bytes, str]:
        """把文本合成音频，返回 ``(字节, 格式)``。

        格式为 ``wav`` / ``mp3`` / ``pcm_s16le``，会原样作为协议的
        ``PLAY_AUDIO.format`` 下发给设备。
        """

    async def aclose(self) -> None:
        """释放资源。"""
        return None


# --------------------------------------------------------------------------- #
# 云端实现
# --------------------------------------------------------------------------- #
class OpenAISpeech(ASRProvider, TTSProvider):
    """OpenAI 兼容的语音服务，同时实现 ASR 与 TTS。

    ``name`` 会被实例级覆盖（``openai-asr`` / ``openai-tts``），
    便于日志里一眼看出是哪条链路。
    """

    def __init__(
        self,
        *,
        api_key: str,
        asr_model: str = "whisper-1",
        tts_model: str = "tts-1",
        tts_voice: str = "alloy",
        tts_format: str = "wav",
        base_url: str = "https://api.openai.com/v1",
        timeout_s: float = 60.0,
    ) -> None:
        self._api_key = api_key
        self.asr_model = asr_model
        self.tts_model = tts_model
        self.tts_voice = tts_voice
        self.tts_format = tts_format
        self.base_url = base_url.rstrip("/")
        self.timeout_s = timeout_s
        self.name = "openai"
        self._client: httpx.AsyncClient | None = None

    async def _get_client(self) -> httpx.AsyncClient:
        """惰性创建连接池。"""
        if self._client is None or self._client.is_closed:
            self._client = httpx.AsyncClient(timeout=httpx.Timeout(self.timeout_s, connect=15.0))
        return self._client

    async def aclose(self) -> None:
        """关闭连接池。"""
        if self._client is not None and not self._client.is_closed:
            await self._client.aclose()
        self._client = None

    def _headers(self, *, json_body: bool = False) -> dict[str, str]:
        """组装请求头。"""
        headers = {"Authorization": f"Bearer {self._api_key}"}
        if json_body:
            headers["Content-Type"] = "application/json"
        return headers

    # ------------------------------------------------------------------ #
    # ASR
    # ------------------------------------------------------------------ #
    async def transcribe(self, pcm: bytes, *, sample_rate: int = DEFAULT_SAMPLE_RATE) -> Transcript:
        """上传 WAV 到 ``/audio/transcriptions``。

        Raises:
            ProviderError: 网络失败或服务端报错。
        """
        if not pcm:
            return Transcript(text="", provider="openai-asr", duration_s=0.0)

        wav = pcm_to_wav(pcm, sample_rate=sample_rate)
        client = await self._get_client()
        files = {"file": ("speech.wav", wav, "audio/wav")}
        data = {"model": self.asr_model}

        try:
            response = await client.post(
                f"{self.base_url}/audio/transcriptions",
                headers=self._headers(),
                files=files,
                data=data,
            )
        except httpx.HTTPError as exc:
            raise ProviderError(f"ASR 网络错误: {exc}") from exc

        if response.status_code >= 400:
            raise ProviderError(f"ASR 返回 HTTP {response.status_code}: {response.text[:300]}")

        text = response.text.strip()
        try:
            body = response.json()
        except ValueError:
            body = None
        if isinstance(body, dict):
            text = str(body.get("text", "")).strip()

        return Transcript(
            text=text,
            duration_s=pcm_duration_s(pcm, sample_rate=sample_rate),
            provider="openai-asr",
        )

    # ------------------------------------------------------------------ #
    # TTS
    # ------------------------------------------------------------------ #
    async def synthesize(self, text: str) -> tuple[bytes, str]:
        """请求 ``/audio/speech`` 并返回音频字节。

        Raises:
            ProviderError: 网络失败或服务端报错。
        """
        cleaned = text.strip()
        if not cleaned:
            return b"", "wav"

        payload = {
            "model": self.tts_model,
            "voice": self.tts_voice,
            "input": cleaned,
            "response_format": self.tts_format,
        }
        client = await self._get_client()
        try:
            response = await client.post(
                f"{self.base_url}/audio/speech",
                headers=self._headers(json_body=True),
                json=payload,
            )
        except httpx.HTTPError as exc:
            raise ProviderError(f"TTS 网络错误: {exc}") from exc

        if response.status_code >= 400:
            raise ProviderError(f"TTS 返回 HTTP {response.status_code}: {response.text[:300]}")

        return response.content, self.tts_format


# --------------------------------------------------------------------------- #
# 离线实现
# --------------------------------------------------------------------------- #
class MockASR(ASRProvider):
    """离线 ASR：不识别内容，只报回时长与占位文本。

    存在的价值是让语音闭环的**时序**可验证（采集 → 上传 → 转写 → 回复），
    音频内容本身交给真实 provider。
    """

    name = "mock-asr"

    def __init__(self, *, placeholder: str = "（离线识别：这里是一句话）") -> None:
        self.placeholder = placeholder
        #: 记录收到的音频总时长，便于调试采集是否正常。
        self.total_seconds = 0.0

    async def transcribe(self, pcm: bytes, *, sample_rate: int = DEFAULT_SAMPLE_RATE) -> Transcript:
        """返回占位文本。"""
        duration = pcm_duration_s(pcm, sample_rate=sample_rate)
        self.total_seconds += duration
        if duration < 0.25:
            return Transcript(text="", provider=self.name, duration_s=duration)
        return Transcript(
            text=self.placeholder,
            confidence=None,
            duration_s=round(duration, 2),
            provider=self.name,
        )


class MockTTS(TTSProvider):
    """离线 TTS：用音高序列代替真人语音，返回合法 WAV。"""

    name = "mock-tts"

    def __init__(self, *, sample_rate: int = DEFAULT_SAMPLE_RATE) -> None:
        self.sample_rate = sample_rate
        #: 最近一次合成的文本，便于测试断言。
        self.last_text = ""

    async def synthesize(self, text: str) -> tuple[bytes, str]:
        """合成提示音 WAV。"""
        self.last_text = text
        return speech_wav(text, sample_rate=self.sample_rate), "wav"


# --------------------------------------------------------------------------- #
# 工厂
# --------------------------------------------------------------------------- #
def create_asr(settings: SpeechSettings) -> ASRProvider:
    """按配置构造 ASR provider。"""
    provider = settings.asr_provider
    if provider == "disabled":
        raise ProviderError("ASR 已被配置禁用（SPARKBOT_SPEECH_ASR_PROVIDER=disabled）")
    if provider == "mock":
        return MockASR()

    api_key = settings.asr_api_key
    secret = api_key.get_secret_value() if api_key is not None else None
    if not secret:
        logger.warning("ASR 配置为 openai 但未提供 api_key，退回离线实现")
        return MockASR()

    return OpenAISpeech(
        api_key=secret,
        asr_model=settings.asr_model,
        base_url=_speech_base_url(settings.asr_base_url),
    )


def create_tts(settings: SpeechSettings) -> TTSProvider:
    """按配置构造 TTS provider。"""
    provider = settings.tts_provider
    if provider == "disabled":
        raise ProviderError("TTS 已被配置禁用（SPARKBOT_SPEECH_TTS_PROVIDER=disabled）")
    if provider == "mock":
        return MockTTS(sample_rate=settings.input_sample_rate)

    api_key = settings.tts_api_key
    secret = api_key.get_secret_value() if api_key is not None else None
    if not secret:
        logger.warning("TTS 配置为 openai 但未提供 api_key，退回离线实现")
        return MockTTS(sample_rate=settings.input_sample_rate)

    return OpenAISpeech(
        api_key=secret,
        tts_model=settings.tts_model,
        tts_voice=settings.tts_voice,
        tts_format=settings.tts_format,
        base_url=_speech_base_url(settings.tts_base_url),
    )


def _speech_base_url(configured: str | None) -> str:
    """语音接口的 base_url，默认走 OpenAI 官方。"""
    return (configured or "https://api.openai.com/v1").rstrip("/")
