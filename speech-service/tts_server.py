"""本地 TTS 服务：sherpa-onnx（默认）/ Kokoro（备选）/ edge-tts（兜底）。

三个引擎的关系：
    * **sherpa-onnx** —— 默认。VITS 中文模型，原生 16kHz，多说话人。
    * **Kokoro** —— 备选。中文音色更多，但 24kHz 需重采样、加载更慢。
    * **edge-tts** —— 仅兜底。在线服务，不稳定。

    请求体里的 ``model`` 字段可以直接选引擎（``sherpa`` / ``kokoro`` /
    ``edge``），所以 PC 端改 ``speech.tts_model`` 就能热切换，不用重启。

为什么默认是 sherpa-onnx：
    * **原生输出 16kHz**，与板子 I2S 一致 —— 整条链路不需要重采样。
      这点很关键：固件里的 ``resample_s16_mono`` 是线性插值、没有抗混叠
      滤波，24k→16k 会把 8kHz 以上的成分折回来，齿音发毛、听感不自然
      （见 OUTPUT_RATE 的说明）。sherpa 直接绕开了这一环。
    * 冷启动快：模型加载约 0.9s（Kokoro 约 3.5s）；
    * 合成更快：RTF ≈ 0.30（Kokoro ≈ 0.40）；
    * 多说话人：本模型带 187 个中文说话人，声线名 ``sid_0`` ~ ``sid_186``；
    * 同样 Apache-2.0。

为什么不再拿 edge-tts 当主力：
    它是**在线**服务，走微软的免费接口。实测会返回 502/503
    （``Invalid response status`` / ``No audio was received``），
    机器人播报直接失败。对话机器人不能把"能不能出声"押在这种
    不保证可用的免费接口上。

为什么保留 Kokoro：
    * 开源（MIT），82M 参数，纯 ONNX，**不需要 GPU**；
    * 有专门的中文模型（Kokoro-82M-v1.1-zh）和成体系的中文声线；
    * 完全离线，不受网络和对方服务状态影响；
    * 实测本机（i5-11400，纯 CPU）RTF ≈ 0.4，比实时快 2.5 倍：
      3.3 秒的回答约 1.5 秒合成完，对话节奏可以接受。

**必须用 fp32 模型**（重要）：
    同一份模型的 int8 量化版在本机 RTF ≈ 3.8（比实时慢 4 倍），
    反而比 fp32 慢约 9 倍 —— x86 上 ORT 的量化卷积/矩阵乘内核
    在这么小的模型上不占优，还多了量化/反量化开销。
    所以这里固定加载 ``kokoro-v1.1-zh.fp32.onnx``。
    （int8 实测：4/6/12 线程分别 RTF 4.05 / 3.98 / 3.89，
      说明瓶颈不是线程调度，就是模型本身。）

中文文本前端：
    用 ``misaki`` 的 ``ZHG2P`` 做汉字 → 音素。注意上游包声明
    ``Python <3.13``，但它是纯 Python 包，实测在 3.13 上工作正常，
    安装时用 ``--ignore-requires-python`` 绕过版本上限。

关于输出格式（关键实现细节）：
    Kokoro 直接输出 **24kHz float32** PCM，这里转成
    24kHz / 单声道 / 16bit WAV。24kHz 不用降到 16kHz ——
    固件里的 ``bot_audio_play()`` 已实现线性重采样，会自动转成
    板子的 16kHz。edge-tts 只出 MP3，所以走兜底时要多一步解码。

接口契约（照抄自 sparkbot/perception/speech.py）：
    请求： JSON
        {"model": ..., "voice": "zh-CN-XiaoxiaoNeural",
         "input": "要合成的文本", "response_format": "wav"}
    响应：**音频字节**（不是 JSON）
        Content-Type: audio/wav

    客户端送的 ``voice`` 是 edge 风格的名字，这里做一次映射到
    Kokoro 中文声线；直接送 Kokoro 声线名（如 ``zf_001``）也可以。

用法::

    D:\\dsh\\sparkbot\\speech-service\\.venv\\Scripts\\python.exe tts_server.py
    ... tts_server.py --port 8761 --engine auto
    ... tts_server.py --engine edge            (只用 edge-tts，排障用)
    ... tts_server.py --voice zf_003           (换 Kokoro 声线)
"""

from __future__ import annotations

import argparse
import asyncio
import io
import logging
import math
import queue
import threading
import time
import wave
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import numpy as np
import soundfile as sf
import uvicorn
from fastapi import FastAPI, HTTPException
from fastapi.responses import Response, StreamingResponse
from pydantic import BaseModel, Field
from scipy.signal import resample_poly

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("tts")

# --------------------------------------------------------------------------- #
# 引擎选择
# --------------------------------------------------------------------------- #

#: 默认引擎。``sherpa`` = 用 sherpa-onnx；``auto`` = Kokoro 优先、失败回落
#: edge-tts；``kokoro`` / ``edge`` = 只用其一。
#: 客户端可以在单次请求里用 ``model`` 字段覆盖这个默认值。
DEFAULT_ENGINE = "sherpa"

#: 模型目录（与源码同级的 models/kokoro/，不入库，见 .gitignore）
MODEL_DIR = Path(__file__).resolve().parent / "models" / "kokoro"
KOKORO_MODEL = MODEL_DIR / "kokoro-v1.1-zh.fp32.onnx"
KOKORO_VOICES = MODEL_DIR / "voices-zh.npz"
KOKORO_CONFIG = MODEL_DIR / "config.json"

#: sherpa-onnx 的 VITS 中文模型（同样不入库，见 README）。
#: 优点：**原生输出 16kHz**，与板子 I2S 一致，服务端不用重采样；
#: 这个模型还是多说话人（187 个），可以做音色选择。
SHERPA_DIR = Path(__file__).resolve().parent / "models" / "sherpa" / "vits-zh-hf-fanchen-C"
SHERPA_MODEL = SHERPA_DIR / "vits-zh-hf-fanchen-C.onnx"
SHERPA_LEXICON = SHERPA_DIR / "lexicon.txt"
SHERPA_TOKENS = SHERPA_DIR / "tokens.txt"
SHERPA_DICT = SHERPA_DIR / "dict"
SHERPA_RULE_FSTS = ["date.fst", "phone.fst", "number.fst", "new_heteronym.fst"]

#: sherpa 默认说话人编号（0 ~ num_speakers-1）。
#: 2 号是这个模型里语速与音色比较均衡的一个（实测 3.21s / RTF 0.30）。
DEFAULT_SHERPA_SID = 2

#: 默认 Kokoro 中文声线（zf = 女声，zm = 男声）。
#: 这是"客户端没指定/指定了不认识的名字"时的兜底。
DEFAULT_KOKORO_VOICE = "zf_005"

#: edge-tts 的默认中文女声。中文语音清单：
#:   zh-CN-XiaoxiaoNeural(女)  zh-CN-XiaoyiNeural(女)
#:   zh-CN-YunjianNeural(男)   zh-CN-YunxiNeural(男)
#:   zh-CN-YunxiaNeural(男)    zh-CN-YunyangNeural(男)
DEFAULT_EDGE_VOICE = "zh-CN-XiaoxiaoNeural"

#: 客户端（PC 端 .env）送的是 edge 风格的名字，映射到 Kokoro 声线。
#: 不在这里、也不在声线库里的名字一律回落到 DEFAULT_KOKORO_VOICE。
EDGE_TO_KOKORO = {
    "zh-CN-XiaoxiaoNeural": "zf_001",
    "zh-CN-XiaoyiNeural": "zf_002",
    "zh-CN-YunjianNeural": "zm_010",
    "zh-CN-YunxiNeural": "zm_009",
    "zh-CN-YunxiaNeural": "zm_012",
    "zh-CN-YunyangNeural": "zm_011",
}

#: ``--voice`` 的默认值。用初始化常量绕开 "name is used prior to global
#: declaration"：``main()`` 里要用 ``global`` 覆盖默认声线。
_INITIAL_VOICE = DEFAULT_KOKORO_VOICE

#: 实际生效的引擎，``main()`` 里按命令行参数覆盖。
ENGINE = DEFAULT_ENGINE

#: 实际生效的默认声线。
DEFAULT_VOICE = _INITIAL_VOICE

#: 合成输出的目标峰值（响度归一化）。Kokoro 原始输出峰值只有 ~0.3（约 -10dB），
#: 实测在蓝牙耳机上偏轻、听不清，所以统一拉到这里再送出去。
TARGET_PEAK = 0.95

#: 增益上限。静音/极轻的块峰值很小，不设上限会把底噪一起放大。
STREAM_MAX_GAIN = 8.0

#: 相邻两块之间允许的增益**上升**倍率。
#:
#: 增益的规则是"降可以立刻降、升只能慢慢升"：
#:   * 立刻降 —— 保证任何一块都不会削顶（削顶就是听感上的"滋滋"）；
#:   * 慢慢升 —— 避免一块一块忽大忽小（听起来像在抽气）。
#:
#: 为什么不是"整句一个增益"：实测同一句话的峰值能从 20000 涨到 32767，
#: 用开头估的固定增益会在后半句削顶。sherpa 的块约 1.5 秒，按块算
#: 既跟得上响度变化，又不会碎到影响听感。
STREAM_GAIN_RISE = 1.5

#: 增益变化时的过渡时长（秒）。
#:
#: 为什么必须过渡：增益只要**突变**，波形上就出现一个台阶 —— 听感就是
#: "咔"的一声。整段合成只有一个增益不会有这问题；流式是一块一块来的，
#: 每块各算增益就会在块边界留下台阶。10ms 的线性斜坡足以消掉它，
#: 又短到听不出音量渐变。
STREAM_GAIN_RAMP_S = 0.010

#: 送出去的采样率。板子 I2S 固定 16kHz，而 Kokoro 出 24kHz。
#: **必须在这里重采样，不能交给固件做**：固件的 ``resample_s16_mono``
#: 是线性插值、没有抗混叠滤波，24k→16k 会把 8kHz 以上的成分折叠回来，
#: 齿音发毛、听感明显不自然。这里用多相滤波重采样成 16kHz 后，
#: 固件那句 ``if (src_rate != s_a.sample_rate)`` 不再成立，直接原样播放。
OUTPUT_RATE = 16000

app = FastAPI(title="SparkBot Local TTS (Kokoro + edge-tts)")


class SpeechRequest(BaseModel):
    """OpenAI `/v1/audio/speech` 的请求体。"""

    model: str = "kokoro"
    input: str = Field(..., description="要合成的文本")  # noqa: A003 - 字段名沿用 OpenAI
    voice: str = _INITIAL_VOICE
    response_format: str = "wav"
    # 这些是 OpenAI 有我们用不全的字段，接受但不报错，免得客户端被拒
    speed: float | None = None
    rate: str | None = None
    volume: str | None = None
    pitch: str | None = None


class StreamSpeechRequest(BaseModel):
    """`/v1/audio/speech/stream` 的请求体。

    不含 ``response_format``：流式端点**永远**返回裸 PCM
    （16kHz / 16bit / 单声道 little-endian），采样率由 ``sample_rate`` 指定。
    板子的 I2S 就是这个格式，PC 端拿到可以直接往板子推，不需要再解码。
    """

    model: str = "sherpa"
    input: str = Field(..., description="要合成的文本")  # noqa: A003 - 沿用 OpenAI
    voice: str = _INITIAL_VOICE
    speed: float | None = None
    sample_rate: int = OUTPUT_RATE


def _resample_to_output(data: np.ndarray, rate: int) -> tuple[np.ndarray, int]:
    """单声道 float32 → OUTPUT_RATE，带抗混叠滤波。

    用多相（polyphase）重采样，不是线性插值 —— 见 OUTPUT_RATE 的说明。
    """
    rate = int(rate)
    if data.size == 0 or rate == OUTPUT_RATE:
        return data, rate
    g = math.gcd(rate, OUTPUT_RATE)
    out = resample_poly(data, OUTPUT_RATE // g, rate // g)
    return np.asarray(out, dtype=np.float32), OUTPUT_RATE


def _encode_wav(pcm: np.ndarray, rate: int) -> tuple[bytes, int, float]:
    """16bit 单声道 PCM → WAV 字节。"""
    duration = pcm.size / float(rate)
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(pcm.tobytes())
    return buf.getvalue(), rate, duration


def pcm_to_wav(samples: np.ndarray, rate: int) -> tuple[bytes, int, float]:
    """float32 [-1, 1] 单声道 PCM → 16bit WAV 字节。

    **响度归一化**：Kokoro 原始输出的峰值只有 0.3 左右（约 -10dB），
    RMS 约 0.05，实测在蓝牙耳机上偏轻、听不清。所以这里统一拉到
    目标峰值再输出，让不同文本/声线的响度一致。
    （板子那侧还有自己的音量控制，这里只要保证送出去的信号够满。）

    Returns:
        ``(wav 字节, 采样率, 时长秒)``
    """
    data = np.asarray(samples, dtype=np.float32).reshape(-1)
    data, rate = _resample_to_output(data, rate)

    peak = float(np.max(np.abs(data))) if data.size else 0.0
    # 峰值太低就整体抬到目标值；太高则压回来，避免削顶爆音。
    # 静音段（peak 极小）不动，否则会把底噪也放大。
    if peak > 0.01:
        data = data * (TARGET_PEAK / peak)

    pcm = (np.clip(data, -1.0, 1.0) * 32767.0).astype(np.int16)
    return _encode_wav(pcm, rate)


def to_pcm16_bytes(samples: np.ndarray, gain: float = 1.0) -> bytes:
    """float32 [-1, 1] 单声道 → **裸 16bit little-endian PCM 字节**。

    流式接口用裸 PCM 而不是 WAV：WAV 的 44 字节头必须写在最前面，
    但流式合成在写完头之前并不知道总长度，也没必要让客户端再剥一层容器。
    """
    data = np.asarray(samples, dtype=np.float32).reshape(-1)
    scaled = np.clip(data * float(gain), -1.0, 1.0)
    return (scaled * 32767.0).astype(np.int16).tobytes()


def to_pcm16_bytes_ramped(
    samples: np.ndarray, prev_gain: float | None, gain: float, ramp_n: int
) -> bytes:
    """同上，但增益从 ``prev_gain`` **线性过渡**到 ``gain``（见 STREAM_GAIN_RAMP_S）。

    ``prev_gain is None`` 表示这是本段的开头：用一小段淡入（0 → gain）
    代替硬起头 —— 上一段若被强行打断，硬起头会"啪"一声。
    """
    data = np.asarray(samples, dtype=np.float32).reshape(-1)
    n = min(max(1, ramp_n), data.size)

    if prev_gain is None:
        env = np.ones(data.size, dtype=np.float32)
        env[:n] = np.linspace(0.0, 1.0, n, dtype=np.float32)
        scaled = data * (float(gain) * env)
    elif abs(prev_gain - gain) > 1e-6:
        env = np.full(data.size, float(gain), dtype=np.float32)
        env[:n] = np.linspace(float(prev_gain), float(gain), n, dtype=np.float32)
        scaled = data * env
    else:
        scaled = data * float(gain)

    return (np.clip(scaled, -1.0, 1.0) * 32767.0).astype(np.int16).tobytes()


def mp3_to_wav(mp3: bytes) -> tuple[bytes, int, float]:
    """把 edge-tts 输出的 MP3 解成 WAV（同样统一到 OUTPUT_RATE）。

    解码用 `soundfile`（libsndfile 自 1.1 起支持 MP3），无需 ffmpeg。
    """
    data, rate = sf.read(io.BytesIO(mp3), dtype="float32", always_2d=True)

    # 多声道就降成单声道（板子是单声道，多传没意义）
    mono = np.ascontiguousarray(data[:, 0], dtype=np.float32)
    mono, rate = _resample_to_output(mono, rate)

    pcm = (np.clip(mono, -1.0, 1.0) * 32767.0).astype(np.int16)
    return _encode_wav(pcm, rate)


# --------------------------------------------------------------------------- #
# Kokoro（离线主力）
# --------------------------------------------------------------------------- #


class KokoroEngine:
    """Kokoro 中文 TTS：模型 + 声线库 + 中文 G2P，首次用到时才加载。"""

    def __init__(self) -> None:
        self._kokoro: Any = None
        self._g2p: Any = None
        self._load_error: str | None = None
        self.load_seconds: float | None = None

    @property
    def files_ready(self) -> bool:
        return (
            KOKORO_MODEL.exists() and KOKORO_VOICES.exists() and KOKORO_CONFIG.exists()
        )

    def load(self) -> None:
        """加载模型与文本前端。已经加载过（或已失败）就直接返回。"""
        if self._kokoro is not None or self._load_error is not None:
            return
        if not self.files_ready:
            self._load_error = (
                f"模型文件缺失，期望在 {MODEL_DIR} 下找到 "
                f"{KOKORO_MODEL.name} / {KOKORO_VOICES.name} / {KOKORO_CONFIG.name}"
            )
            return

        t0 = time.perf_counter()
        try:
            from kokoro_onnx import Kokoro
            from misaki import zh

            self._kokoro = Kokoro(
                str(KOKORO_MODEL), str(KOKORO_VOICES), vocab_config=str(KOKORO_CONFIG)
            )
            self._g2p = zh.ZHG2P(version="1.1")
        except Exception as exc:  # noqa: BLE001 - 加载失败只降级，不致命
            self._load_error = f"{type(exc).__name__}: {exc}"
            logger.warning("Kokoro 加载失败，将回落到 edge-tts: %s", self._load_error)
            return

        self.load_seconds = time.perf_counter() - t0
        logger.info(
            "Kokoro 就绪: 加载 %.2fs, 声线 %d 个, 模型 %s",
            self.load_seconds, len(self.list_voices()), KOKORO_MODEL.name,
        )

    @property
    def ready(self) -> bool:
        return self._kokoro is not None

    @property
    def error(self) -> str | None:
        return self._load_error

    def list_voices(self) -> list[str]:
        return list(self._kokoro.get_voices()) if self._kokoro is not None else []

    def resolve_voice(self, voice: str) -> str:
        """把客户端送来的声线名解析成 Kokoro 声线名。"""
        available = self.list_voices()
        if voice in available:
            return voice
        mapped = EDGE_TO_KOKORO.get(voice)
        if mapped in available:
            return mapped
        return DEFAULT_KOKORO_VOICE

    def synthesize(self, text: str, voice: str, speed: float) -> tuple[bytes, int, float]:
        """合成一段文本，返回 ``(wav 字节, 采样率, 时长秒)``。"""
        self.load()
        if self._kokoro is None:
            raise RuntimeError(self._load_error or "Kokoro 未就绪")

        name = self.resolve_voice(voice)
        phonemes, _ = self._g2p(text)
        if not phonemes.strip():
            raise ValueError(f"文本没有可合成的音素: {text[:30]!r}")

        # create() 的 speed 只接受 0.5~2.0，超范围会被拒，这里先夹一下
        speed = min(2.0, max(0.5, float(speed or 1.0)))
        audio, rate = self._kokoro.create(
            phonemes, voice=name, speed=speed, is_phonemes=True
        )
        return pcm_to_wav(audio, rate)


KOKORO = KokoroEngine()


# --------------------------------------------------------------------------- #
# sherpa-onnx（离线，可选引擎；原生 16kHz）
# --------------------------------------------------------------------------- #


class SherpaEngine:
    """sherpa-onnx 的 VITS 中文 TTS，多说话人。首次用到时才加载。"""

    def __init__(self) -> None:
        self._tts: Any = None
        self._load_error: str | None = None
        self.load_seconds: float | None = None

    @property
    def files_ready(self) -> bool:
        return SHERPA_MODEL.exists() and SHERPA_LEXICON.exists() and SHERPA_TOKENS.exists()

    def load(self) -> None:
        if self._tts is not None or self._load_error is not None:
            return
        if not self.files_ready:
            self._load_error = f"模型文件缺失，期望在 {SHERPA_DIR} 下找到 onnx/lexicon/tokens"
            return

        t0 = time.perf_counter()
        try:
            import sherpa_onnx

            cfg = sherpa_onnx.OfflineTtsConfig(
                model=sherpa_onnx.OfflineTtsModelConfig(
                    vits=sherpa_onnx.OfflineTtsVitsModelConfig(
                        model=str(SHERPA_MODEL),
                        lexicon=str(SHERPA_LEXICON),
                        tokens=str(SHERPA_TOKENS),
                        dict_dir=str(SHERPA_DICT),
                    ),
                    num_threads=6,
                    provider="cpu",
                ),
                # 文本正则化：日期 / 电话号码 / 数字 / 多音字
                rule_fsts=",".join(str(SHERPA_DIR / f) for f in SHERPA_RULE_FSTS),
                max_num_sentences=1,
            )
            self._tts = sherpa_onnx.OfflineTts(cfg)
        except Exception as exc:  # noqa: BLE001
            self._load_error = f"{type(exc).__name__}: {exc}"
            logger.warning("sherpa 加载失败: %s", self._load_error)
            return

        self.load_seconds = time.perf_counter() - t0
        logger.info(
            "sherpa 就绪: 加载 %.2fs, 采样率 %d, 说话人 %d",
            self.load_seconds, self._tts.sample_rate, self._tts.num_speakers,
        )

    @property
    def ready(self) -> bool:
        return self._tts is not None

    @property
    def error(self) -> str | None:
        return self._load_error

    def resolve_sid(self, voice: str) -> int:
        """声线名 ``sid_12`` → 说话人 12；其它名字一律用默认说话人。"""
        if voice.lower().startswith("sid_"):
            try:
                return int(voice[4:])
            except ValueError:
                pass
        return DEFAULT_SHERPA_SID

    def synthesize(self, text: str, voice: str, speed: float) -> tuple[bytes, int, float]:
        self.load()
        if self._tts is None:
            raise RuntimeError(self._load_error or "sherpa 未就绪")

        sid = min(max(self.resolve_sid(voice), 0), self._tts.num_speakers - 1)
        speed = min(2.0, max(0.5, float(speed or 1.0)))
        audio = self._tts.generate(text, sid=sid, speed=speed)

        data = np.asarray(audio.samples, dtype=np.float32).reshape(-1)
        # sherpa 已是 16kHz，_resample_to_output 会原样返回
        data, rate = _resample_to_output(data, audio.sample_rate)
        peak = float(np.max(np.abs(data))) if data.size else 0.0
        if peak > 0.01:
            data = data * (TARGET_PEAK / peak)
        pcm = (np.clip(data, -1.0, 1.0) * 32767.0).astype(np.int16)
        return _encode_wav(pcm, rate)


    def stream(
        self,
        text: str,
        voice: str,
        speed: float,
        *,
        queue_size: int = 8,
        stop_event: threading.Event | None = None,
    ) -> Iterator[bytes]:
        """流式合成：**边生成边** yield 裸 16bit 单声道 PCM（16kHz）。

        原理：sherpa-onnx 的 ``OfflineTts.generate()`` 支持一个
        ``callback(samples, progress)``，生成过程中会**分块**回调。
        这里把它接到一个有界队列上，主线程负责取块 + 加增益 + 转 int16。

        与 ``synthesize()`` 的区别就是首字延迟：整段合成要等全部算完
        （几秒），这里第一块几百毫秒就能送出去。

        增益策略见 ``STREAM_GAIN_RISE``：按块算增益，**降立刻降、升慢慢升**。
        硬约束是"任何一块都不削顶"—— 削顶在听感上就是"滋滋"的失真。
        """
        self.load()
        if self._tts is None:
            raise RuntimeError(self._load_error or "sherpa 未就绪")

        sid = min(max(self.resolve_sid(voice), 0), self._tts.num_speakers - 1)
        speed = min(2.0, max(0.5, float(speed or 1.0)))

        chunks: queue.Queue = queue.Queue(maxsize=max(2, queue_size))

        def on_chunk(samples: np.ndarray, progress: float) -> int:
            # 这个回调跑在生成线程里。队列满时 put 会阻塞 —— 这是**有意的**：
            # 客户端消费不过来时反过来压住生成，避免在内存里堆一大段音频。
            # 但要用带超时的 put：否则客户端中途断开（消费端不再取块）时
            # 这个线程会永远卡死在这里，连 stop_event 都看不到。
            #
            # ⚠️ 返回值语义与文档相反（sherpa-onnx 1.13.8 实测）：
            #   * 返回 **非 0** → 继续生成；
            #   * 返回 **0**   → **提前停止**生成。
            # 文档写的是"非 0 停止"，照文档写会让整句只出第一块就结束
            # （实测 8.5 秒的句子只得到 0.9 秒）。下面是实测出来的正确写法。
            payload = ("audio", np.asarray(samples, dtype=np.float32).reshape(-1).copy())
            while True:
                if stop_event is not None and stop_event.is_set():
                    return 0  # 0 = 停止生成
                try:
                    chunks.put(payload, timeout=0.2)
                    return 1  # 非 0 = 继续生成
                except queue.Full:
                    continue

        def worker() -> None:
            try:
                self._tts.generate(text, sid=sid, speed=speed, callback=on_chunk)
                chunks.put(("done", None))
            except BaseException as exc:  # noqa: BLE001 - 原样交给消费端抛
                chunks.put(("error", exc))

        threading.Thread(target=worker, name="tts-stream", daemon=True).start()

        rate = int(getattr(self._tts, "sample_rate", 0) or OUTPUT_RATE)
        ramp_n = max(1, int(rate * STREAM_GAIN_RAMP_S))
        gain: float | None = None
        applied: float | None = None   # 上一块实际用的增益（用于斜坡）

        while True:
            kind, payload = chunks.get()
            if kind == "done":
                break
            if kind == "error":
                raise payload

            data = payload
            peak = float(np.max(np.abs(data))) if data.size else 0.0
            if peak > 0.01:
                want = min(STREAM_MAX_GAIN, TARGET_PEAK / peak)
                if gain is None or want <= gain:
                    gain = want          # 首块直接定；需要降就立刻降（防削顶）
                else:
                    gain = min(want, gain * STREAM_GAIN_RISE)  # 升只能慢慢升
            current = gain if gain is not None else 1.0
            yield to_pcm16_bytes_ramped(data, applied, current, ramp_n)
            applied = current


SHERPA = SherpaEngine()


# --------------------------------------------------------------------------- #
# edge-tts（在线兜底）
# --------------------------------------------------------------------------- #


async def edge_synthesize(text: str, voice: str, rate: str | None,
                          volume: str | None, pitch: str | None) -> bytes:
    """调用 edge-tts 拿 MP3 字节。"""
    import edge_tts

    kwargs: dict[str, Any] = {"voice": voice}
    if rate:
        kwargs["rate"] = rate
    if volume:
        kwargs["volume"] = volume
    if pitch:
        kwargs["pitch"] = pitch

    comm = edge_tts.Communicate(text, **kwargs)
    buf = bytearray()
    async for chunk in comm.stream():
        if chunk["type"] == "audio":
            buf.extend(chunk["data"])
    return bytes(buf)


# --------------------------------------------------------------------------- #
# HTTP 接口
# --------------------------------------------------------------------------- #


@app.get("/health")
async def health() -> dict[str, Any]:
    """就绪探针。"""
    return {
        "status": "ok",
        "engine": ENGINE,
        "kokoro_ready": KOKORO.ready,
        "kokoro_error": KOKORO.error,
        "kokoro_load_seconds": KOKORO.load_seconds,
        "sherpa_ready": SHERPA.ready,
        "sherpa_error": SHERPA.error,
        "default_voice": DEFAULT_VOICE,
        "voices": KOKORO.list_voices() if KOKORO.ready else None,
    }


@app.get("/v1/voices")
async def voices(locale: str = "zh-CN") -> dict[str, Any]:
    """列出可用语音（便于挑音色）。"""
    if KOKORO.ready:
        return {
            "object": "list",
            "engine": "kokoro",
            "data": [{"id": v, "voice": v} for v in KOKORO.list_voices()],
        }

    import edge_tts

    try:
        all_voices = await edge_tts.list_voices()
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=502, detail=f"获取语音列表失败: {exc}") from exc
    items = [{"id": v["ShortName"], "gender": v.get("Gender"), "locale": v.get("Locale")}
             for v in all_voices if str(v.get("Locale", "")).startswith(locale)]
    return {"object": "list", "engine": "edge-tts", "data": items}


async def synth_with_edge(req: SpeechRequest) -> tuple[bytes, str, int, float]:
    """走 edge-tts；返回 ``(音频字节, 媒体类型, 采样率, 时长)``。"""
    mp3 = await edge_synthesize(req.input, req.voice, req.rate, req.volume, req.pitch)
    if not mp3:
        raise RuntimeError("edge-tts 没有返回音频数据")

    if (req.response_format or "wav").lower() == "mp3":
        return mp3, "audio/mpeg", 0, 0.0
    wav, rate, dur = mp3_to_wav(mp3)
    return wav, "audio/wav", rate, dur


@app.post("/v1/audio/speech")
async def speech(req: SpeechRequest) -> Response:
    """OpenAI 兼容的语音合成端点，返回**音频字节**。"""
    text = (req.input or "").strip()
    if not text:
        raise HTTPException(status_code=400, detail="input 不能为空")

    t0 = time.perf_counter()
    errors: list[str] = []
    wav: bytes | None = None
    media = "audio/wav"
    rate = 0
    dur = 0.0
    used = ""

    # 引擎可以在单个请求里用 model 字段指定，这样 PC 端改 speech.tts_model
    # 就能热切换（kokoro / sherpa / edge），不用重启本服务。
    engine = req.model.lower()
    if engine not in ("kokoro", "sherpa", "edge"):
        engine = ENGINE

    # ---- 1. 主力：Kokoro（离线） -------------------------------------- #
    if engine in ("auto", "kokoro"):
        try:
            wav, rate, dur = await asyncio.to_thread(
                KOKORO.synthesize, text, req.voice, req.speed or 1.0
            )
            media, used = "audio/wav", "kokoro"
        except Exception as exc:  # noqa: BLE001 - 失败就降级
            errors.append(f"kokoro: {type(exc).__name__}: {exc}")
            logger.warning("Kokoro 合成失败: %s", exc)

    # ---- 2. 可选：sherpa-onnx（离线，原生 16kHz，多说话人） ------------- #
    if wav is None and engine == "sherpa":
        try:
            wav, rate, dur = await asyncio.to_thread(
                SHERPA.synthesize, text, req.voice, req.speed or 1.0
            )
            media, used = "audio/wav", "sherpa"
        except Exception as exc:  # noqa: BLE001
            errors.append(f"sherpa: {type(exc).__name__}: {exc}")
            logger.warning("sherpa 合成失败: %s", exc)

    # ---- 3. 兜底：edge-tts（在线） ------------------------------------- #
    if wav is None and engine in ("auto", "edge"):
        try:
            wav, media, rate, dur = await synth_with_edge(req)
            used = "edge-tts"
        except Exception as exc:  # noqa: BLE001
            errors.append(f"edge-tts: {type(exc).__name__}: {exc}")
            logger.exception("edge-tts 合成失败")

    if wav is None:
        raise HTTPException(status_code=502, detail="合成失败: " + " | ".join(errors))

    total_ms = (time.perf_counter() - t0) * 1000
    if media == "audio/wav" and dur > 0:
        # 这几行是部署时最想拿到的数据：延迟与实时率
        logger.info(
            "合成[%s]: %d 字 -> %.2fs 音频, 总计 %.0fms | RTF %.3f | %r",
            used, len(text), dur, total_ms, total_ms / 1000 / dur, text[:30],
        )
    else:
        logger.info("合成[%s]: %d 字 -> %d 字节, 总计 %.0fms",
                    used, len(text), len(wav), total_ms)

    return Response(
        content=wav,
        media_type=media,
        headers={
            # 诊断信息用响应头带出去，客户端可忽略
            "X-TTS-Engine": used,
            "X-Audio-Seconds": f"{dur:.3f}",
            "X-Synth-Ms": f"{total_ms:.1f}",
            "X-Sample-Rate": str(rate),
        },
    )


def wav_to_pcm16_bytes(wav: bytes) -> bytes:
    """整段 WAV → 归一化后的裸 PCM16（OUTPUT_RATE，单声道）。

    只有"兜底"路径用得到：sherpa 能真流式，Kokoro/edge 不能 —— 它们
    整段合成完再一次性转成 PCM 发出去，接口形态保持一致。
    """
    with wave.open(io.BytesIO(wav), "rb") as w:
        frames = w.readframes(w.getnframes())
        rate = w.getframerate()
        channels = w.getnchannels()
        width = w.getsampwidth()
    if width != 2:
        raise ValueError(f"只支持 16bit WAV，收到 {width * 8}bit")

    data = np.frombuffer(frames, dtype=np.int16).astype(np.float32) / 32768.0
    if channels > 1:
        data = data.reshape(-1, channels)[:, 0]
    data, _ = _resample_to_output(data, rate)

    peak = float(np.max(np.abs(data))) if data.size else 0.0
    gain = min(STREAM_MAX_GAIN, TARGET_PEAK / peak) if peak > 0.01 else 1.0
    return to_pcm16_bytes(data, gain)


async def synthesize_full_pcm16(
    text: str, engine: str, voice: str, speed: float
) -> bytes:
    """非流式兜底：整段合成 → 裸 PCM16。"""
    errors: list[str] = []

    if engine != "edge":
        order = ("sherpa", "kokoro") if engine != "kokoro" else ("kokoro", "sherpa")
        for name in order:
            fn = SHERPA.synthesize if name == "sherpa" else KOKORO.synthesize
            try:
                wav, _rate, _dur = await asyncio.to_thread(fn, text, voice, speed)
                return wav_to_pcm16_bytes(wav)
            except Exception as exc:  # noqa: BLE001
                errors.append(f"{name}: {type(exc).__name__}: {exc}")

    try:
        mp3 = await edge_synthesize(text, voice, None, None, None)
        wav, _rate, _dur = mp3_to_wav(mp3)
        return wav_to_pcm16_bytes(wav)
    except Exception as exc:  # noqa: BLE001
        errors.append(f"edge-tts: {type(exc).__name__}: {exc}")

    raise HTTPException(status_code=502, detail="合成失败: " + " | ".join(errors))


@app.post("/v1/audio/speech/stream")
async def speech_stream(req: StreamSpeechRequest) -> StreamingResponse:
    """流式语音合成：**边合成边下发**裸 PCM（16kHz / 16bit / 单声道 LE）。

    与 `/v1/audio/speech` 的区别：

    * 后者等整句算完才返回，首字延迟 = 整句合成时间；
    * 这里 sherpa-onnx 每生成一小块就立刻推出去，首块通常几百毫秒，
      而且**不与文本长度线性相关**——长句也能很快开口。

    客户端把收到的字节直接喂给板子的 ``audio_stream_write`` 即可，
    不需要解码容器、也不需要重采样。
    """
    text = (req.input or "").strip()
    if not text:
        raise HTTPException(status_code=400, detail="input 不能为空")

    engine = req.model.lower()
    if engine not in ("kokoro", "sherpa", "edge"):
        engine = ENGINE

    # sherpa 是唯一支持真流式的引擎；其余引擎整段合成后一次性发出，
    # 保持接口一致（客户端不用区分）。
    if engine == "sherpa" and SHERPA.files_ready:
        stop_event = threading.Event()
        iterator = SHERPA.stream(text, req.voice, req.speed or 1.0, stop_event=stop_event)

        def _pull_next():
            # 生成是阻塞的 torch 推理，必须放到线程里，否则会卡住事件循环
            # （注意是普通函数：asyncio.to_thread 只接受同步可调用对象）
            try:
                return next(iterator)
            except StopIteration:
                return _STREAM_DONE
            except BaseException as exc:  # noqa: BLE001 - 原样抛给客户端
                return exc

        async def _gen():
            t0 = time.perf_counter()
            total = 0
            first = True
            try:
                while True:
                    item = await asyncio.to_thread(_pull_next)
                    if item is _STREAM_DONE:
                        break
                    if isinstance(item, BaseException):
                        raise item
                    total += len(item)
                    if first:
                        first = False
                        logger.info(
                            "流式合成[首块]: %d 字 -> %.0fms 出第一块（%d 字节）| %r",
                            len(text), (time.perf_counter() - t0) * 1000, len(item), text[:30],
                        )
                    yield item
            finally:
                # 客户端中途断开时，消费端不再取块 —— 必须让生成线程停下来
                stop_event.set()
                logger.info(
                    "流式合成完成: %d 字 -> %d 字节 / %.2fs 音频 | 总耗时 %.0fms",
                    len(text), total, total / 2 / OUTPUT_RATE,
                    (time.perf_counter() - t0) * 1000,
                )

        return StreamingResponse(
            _gen(),
            media_type="audio/L16",
            headers={
                "X-TTS-Engine": "sherpa-stream",
                "X-Sample-Rate": str(OUTPUT_RATE),
                "X-Channels": "1",
                "X-Sample-Format": "s16le",
            },
        )

    pcm = await synthesize_full_pcm16(text, engine, req.voice, req.speed or 1.0)
    logger.info("流式合成[兜底整段]: %d 字 -> %d 字节（engine=%s）", len(text), len(pcm), engine)
    return StreamingResponse(
        iter((pcm,)),
        media_type="audio/L16",
        headers={
            "X-TTS-Engine": f"{engine}-whole",
            "X-Sample-Rate": str(OUTPUT_RATE),
            "X-Channels": "1",
            "X-Sample-Format": "s16le",
        },
    )


_STREAM_DONE = object()


def main() -> int:
    """命令行入口。"""
    global DEFAULT_VOICE, ENGINE  # noqa: PLW0603 - 允许命令行覆盖

    p = argparse.ArgumentParser(description="本地 TTS 服务（Kokoro + edge-tts）")
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", type=int, default=8761)
    p.add_argument("--voice", default=_INITIAL_VOICE,
                   help="默认声线（Kokoro 如 zf_001，或 edge 名字）")
    p.add_argument("--engine", default=DEFAULT_ENGINE,
                   choices=["auto", "kokoro", "sherpa", "edge"],
                   help="auto=Kokoro 优先、失败回落 edge-tts；也可指定 sherpa")
    args = p.parse_args()

    DEFAULT_VOICE = args.voice
    ENGINE = args.engine

    logger.info("TTS 服务启动: http://%s:%d/v1  (engine=%s, voice=%s)",
                args.host, args.port, args.engine, args.voice)

    if args.engine in ("auto", "kokoro"):
        KOKORO.load()
        if KOKORO.ready:
            logger.info("Kokoro 离线合成可用（默认声线 %s）", DEFAULT_KOKORO_VOICE)
        else:
            logger.warning("Kokoro 不可用（%s），将由 edge-tts 承担；"
                           "edge-tts 是在线服务，需要能访问微软接口", KOKORO.error)
    elif args.engine == "sherpa":
        SHERPA.load()
        if SHERPA.ready:
            logger.info("sherpa 离线合成可用（默认说话人 %d）", DEFAULT_SHERPA_SID)
        else:
            logger.warning("sherpa 不可用: %s", SHERPA.error)

    uvicorn.run(app, host=args.host, port=args.port, log_level="warning")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
