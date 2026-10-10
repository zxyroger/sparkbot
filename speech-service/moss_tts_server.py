"""本地 TTS 服务：MOSS-TTS-Nano（文本 → 语音，纯 CPU）。

为什么换成 MOSS-TTS-Nano：
    * 只有 0.1B 参数，纯 CPU 就能跑，不需要 GPU；
    * 完全离线，不依赖任何云端接口（edge-tts 是微软在线接口，实测会 502）；
    * 原生 48kHz 立体声，音质比 sherpa 的小 VITS 明显好；
    * 声音克隆：给一段参考音频就能定音色，换音色不用重新训练。

与原 tts_server.py（sherpa / Kokoro / edge-tts）的关系：
    两个服务实现**同一套** OpenAI 兼容接口，PC 端只改
    ``speech.tts_model`` / ``speech.tts_base_url`` 就能换引擎，不用动代码。
    本文件是现在的默认 TTS；``tts_server.py`` 保留作对照与兜底。

为什么单独一个 venv（``.venv-moss``）：
    MOSS-TTS-Nano 的官方代码依赖 ``torchaudio``，而 torchaudio 在 2.9 之后
    停止发版，2.8.0 是最后一个还能配上 torch 2.8.x 的版本。ASR 那边装的是
    torch 2.14.1，没有对应的 torchaudio，两边版本无法共存。所以这里用作者
    验证过的组合：torch 2.8.0 + torchaudio 2.8.0 + transformers 4.57.1，
    与 ASR 的 .venv 完全隔离，互不影响。

采样率：
    模型原生输出 48kHz / 立体声，板子 I2S 固定 16kHz / 单声道。这里在服务端
    用 torchaudio 的多相滤波重采样到 16kHz（不是线性插值），避免高频折叠带来
    的「齿音发毛」。见 tts_server.py 里 OUTPUT_RATE 的同一段说明。

接口（照抄自 sparkbot/perception/speech.py，与 tts_server.py 一致）：
    请求: JSON
        {"model": "moss", "voice": "Junhao", "input": "要合成的文本",
         "response_format": "wav"}
    响应: **音频字节**（不是 JSON），Content-Type: audio/wav

    ``voice`` 用内置音色名（Junhao / Xiaoyu / Yuewen / Lingyu），也可以直接
    给参考音频的绝对路径；留空则用服务端默认音色。``speed`` 字段接受但忽略
    —— 模型本身不支持变速。

用法::

    .venv-moss\\Scripts\\python.exe moss_tts_server.py
    ... moss_tts_server.py --port 8761 --voice Xiaoyu
    ... moss_tts_server.py --no-preload        (启动不加载模型，首请求再加载)
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import io
import logging
import os
import threading
import time
import wave
from pathlib import Path
from typing import Any

# --------------------------------------------------------------------------- #
# 路径与常量
# --------------------------------------------------------------------------- #

BASE_DIR = Path(__file__).resolve().parent
MODEL_ROOT = BASE_DIR / "models" / "moss"
TTS_DIR = MODEL_ROOT / "tts"
CODEC_DIR = MODEL_ROOT / "codec"
VOICE_DIR = MODEL_ROOT / "voices"
OUTPUT_DIR = BASE_DIR / "generated_audio"

# transformers 用 trust_remote_code 加载模型自带的代码时，会把那几个 .py 复制到
# HF 缓存目录。本机家目录（C:\Users\Administrator）不可写，默认缓存路径会直接
# 报 WinError 5 拒绝访问 —— 和 README 里 ModelScope 那条坑同源。这里把 HF 缓存
# 指到服务目录下，不依赖外部环境变量。
# 必须在 import transformers 之前设好，所以 transformers 是延迟导入的。
os.environ.setdefault("HF_HOME", str(BASE_DIR / "hf-home"))

import numpy as np
import torch
from fastapi import FastAPI, HTTPException
from fastapi.responses import Response, StreamingResponse
from pydantic import BaseModel, Field
from torchaudio.functional import resample as _torch_resample

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("moss-tts")

#: 送给板子的采样率。板子 I2S 固定 16kHz，这里统一重采样好再出去。
OUTPUT_RATE = 16000

#: 响度归一化的目标峰值。模型输出峰值不固定，统一拉齐后再送出去。
TARGET_PEAK = 0.95

#: 内置音色：名字 -> (参考音频文件名, 说明)。
#: 参考音频来自官方 Space（spaces/OpenMOSS-Team/MOSS-TTS-Nano），不入库。
VOICE_PRESETS: dict[str, tuple[str, str]] = {
    "junhao": ("zh_1.wav", "中文男声 A"),
    "xiaoyu": ("zh_3.wav", "中文女声 A"),
    "yuewen": ("zh_4.wav", "中文女声 B"),
    "lingyu": ("zh_6.wav", "中文女声 C"),
}

#: 默认音色。PC 端一般会自己带 voice，这里只是兜底。main() 里可被命令行覆盖。
DEFAULT_VOICE = "Lingyu"

#: 生成参数，取官方 Space 运行时的默认值。
GEN_DEFAULTS: dict[str, Any] = {
    "do_sample": True,
    "text_temperature": 1.0,
    "text_top_p": 1.0,
    "text_top_k": 50,
    "audio_temperature": 0.8,
    "audio_top_p": 0.95,
    "audio_top_k": 25,
    "audio_repetition_penalty": 1.2,
}

#: 单次生成的最长帧数。帧率 12.5Hz，375 帧 ≈ 30 秒音频。
MAX_NEW_FRAMES = 375

#: 声音克隆模式下每段文本的 token 上限（超出由模型自动切句）。
VOICE_CLONE_MAX_TEXT_TOKENS = 75

#: 参考音频最多用到这么长（秒）。
#: 模型每合成一句都要重新编码一遍参考音频，参考越长固定开销越大：实测同一句
#: 话，7.9s 的参考比 5s 的参考每次多花 ~2.5s。5 秒足够克隆出音色。
REF_MAX_SECONDS = 5.0


# --------------------------------------------------------------------------- #
# 请求体
# --------------------------------------------------------------------------- #


class SpeechRequest(BaseModel):
    """OpenAI `/v1/audio/speech` 的请求体。"""

    model: str = "moss"
    input: str = Field(..., description="要合成的文本")  # noqa: A003 - 沿用 OpenAI
    voice: str = ""
    response_format: str = "wav"
    # OpenAI 有、我们用不全的字段：接受但不报错，免得客户端被拒。
    speed: float | None = None
    rate: str | None = None
    volume: str | None = None
    pitch: str | None = None


class StreamSpeechRequest(BaseModel):
    """`/v1/audio/speech/stream` 的请求体。

    不含 `response_format`：流式端点**永远**返回裸 PCM
    （16kHz / 16bit / 单声道 / little-endian）。板子 I2S 就是这个格式。
    """

    model: str = "moss"
    input: str = Field(..., description="要合成的文本")  # noqa: A003 - 沿用 OpenAI
    voice: str = ""
    speed: float | None = None
    sample_rate: int = OUTPUT_RATE


# --------------------------------------------------------------------------- #
# 音频工具
# --------------------------------------------------------------------------- #


def waveform_to_mono_16k(waveform: Any, sample_rate: int) -> np.ndarray:
    """模型输出波形（torch，48kHz，可能是立体声）→ 16kHz 单声道 float32。"""
    tensor = waveform
    if not torch.is_tensor(tensor):
        tensor = torch.as_tensor(np.asarray(tensor))
    tensor = tensor.to(torch.float32).cpu()

    if tensor.dim() == 1:
        tensor = tensor.unsqueeze(0)
    elif tensor.dim() != 2:
        raise ValueError(f"不支持的波形维度: {tuple(tensor.shape)}")

    # 立体声（或多声道）降成单声道：板子只有一个喇叭，多声道没意义。
    if tensor.shape[0] > 1:
        tensor = tensor.mean(dim=0, keepdim=True)

    rate = int(sample_rate or 0)
    if rate <= 0:
        raise ValueError("模型没有返回采样率")
    if rate != OUTPUT_RATE:
        # 多相滤波重采样：比线性插值干净得多，见模块开头的说明。
        tensor = _torch_resample(tensor, orig_freq=rate, new_freq=OUTPUT_RATE)

    return tensor.squeeze(0).contiguous().numpy().astype(np.float32, copy=False)


def encode_wav(samples: np.ndarray, rate: int = OUTPUT_RATE) -> tuple[bytes, float]:
    """float32 [-1, 1] 单声道 PCM → 16bit WAV 字节。

    顺带把响度统一拉到 ``TARGET_PEAK``：不同参考音色、不同文本的原始峰值差
    很多，不归一化的话在蓝牙耳机上会忽大忽小。极轻的段落（peak 很小）不动，
    免得把底噪一起放大。

    Returns:
        ``(wav 字节, 音频秒数)``
    """
    data = np.asarray(samples, dtype=np.float32).reshape(-1)
    duration = data.size / float(rate) if rate else 0.0

    peak = float(np.max(np.abs(data))) if data.size else 0.0
    if peak > 0.01:
        data = data * (TARGET_PEAK / peak)
    pcm = (np.clip(data, -1.0, 1.0) * 32767.0).astype(np.int16)

    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(pcm.tobytes())
    return buf.getvalue(), duration


def wav_to_pcm16(wav: bytes) -> bytes:
    """16bit 单声道 WAV → 裸 PCM16（流式端点的兜底路径用）。"""
    with wave.open(io.BytesIO(wav), "rb") as w:
        frames = w.readframes(w.getnframes())
        channels = w.getnchannels()
        width = w.getsampwidth()
        rate = w.getframerate()
    if width != 2:
        raise ValueError(f"只支持 16bit WAV，收到 {width * 8}bit")
    if channels != 1 or rate != OUTPUT_RATE:
        raise ValueError(f"WAV 格式不符合预期: channels={channels} rate={rate}")
    return frames


# --------------------------------------------------------------------------- #
# 引擎
# --------------------------------------------------------------------------- #


class MossNanoEngine:
    """MOSS-TTS-Nano：AutoModelForCausalLM + 神经音频编解码器。

    首次用到时才加载（也可以启动时预热）。模型不是线程安全的（内部有 KV cache
    等可变状态），所以 ``synthesize`` 全程持锁串行执行 —— 语音链路本来就是
    一句一句来的。
    """

    name = "moss-tts-nano"

    def __init__(self, *, nq: int | None = None, max_frames: int = MAX_NEW_FRAMES) -> None:
        self._model: Any = None
        self._codec: Any = None
        self._load_error: str | None = None
        #: 可重入锁：load() 与 synthesize() 都要拿，避免两个并发请求各加载一份。
        self._lock = threading.RLock()
        self.load_seconds: float | None = None
        self.nq = nq
        self.max_frames = max_frames
        self._device = torch.device("cpu")

    # ---- 加载 ----------------------------------------------------------- #
    @property
    def files_ready(self) -> bool:
        return (TTS_DIR / "config.json").exists() and (CODEC_DIR / "config.json").exists()

    @property
    def missing_hint(self) -> str:
        return (
            f"模型文件缺失，期望在 {MODEL_ROOT} 下找到 tts/ 与 codec/ 两个目录"
            "（见 speech-service/README.md 的下载说明）"
        )

    def load(self) -> None:
        """加载模型与编解码器。已加载或已失败则直接返回（幂等）。"""
        with self._lock:
            if self._model is not None or self._load_error is not None:
                return
            if not self.files_ready:
                self._load_error = self.missing_hint
                return

            t0 = time.perf_counter()
            try:
                # 延迟导入：HF_HOME 必须先设好，见文件开头的说明。
                from transformers import AutoModel, AutoModelForCausalLM

                codec = AutoModel.from_pretrained(
                    str(CODEC_DIR), trust_remote_code=True, local_files_only=True
                )
                codec.eval()
                model = AutoModelForCausalLM.from_pretrained(
                    str(TTS_DIR), trust_remote_code=True, local_files_only=True
                )
                model.to(device=self._device, dtype=torch.float32)
                model.eval()

                # 这个 checkpoint 的 config 里写死了 flash_attention_2，而 flash-attn
                # 在 Windows / CPU 上没有可用的构建，直接跑会 ImportError。官方 Space
                # 的做法是显式切到 sdpa（CPU 上走 PyTorch 自带的 sdpa 内核），编解码器
                # 同样切 sdpa + fp32。两处的 setter 都用 hasattr 兜一下，免得换 checkpoint
                # 时因为接口改名直接崩。
                if hasattr(codec, "set_attention_implementation"):
                    codec.set_attention_implementation("sdpa")
                if hasattr(codec, "set_compute_dtype"):
                    codec.set_compute_dtype("fp32")
                if hasattr(model, "_set_attention_implementation"):
                    model._set_attention_implementation(  # noqa: SLF001 - 官方 runtime 同名私有接口
                        "sdpa", local_attn_implementation="sdpa"
                    )
            except Exception as exc:  # noqa: BLE001 - 加载失败只降级，不致命
                self._load_error = f"{type(exc).__name__}: {exc}"
                logger.warning("MOSS-TTS-Nano 加载失败: %s", self._load_error)
                return

            self._codec = codec
            self._model = model
            self.load_seconds = time.perf_counter() - t0
            logger.info(
                "MOSS-TTS-Nano 就绪: 加载 %.2fs, 音色 %s, 输出 %dHz",
                self.load_seconds, self.list_voices(), OUTPUT_RATE,
            )

    @property
    def ready(self) -> bool:
        return self._model is not None

    @property
    def error(self) -> str | None:
        return self._load_error

    # ---- 音色 ----------------------------------------------------------- #
    def list_voices(self) -> list[str]:
        """可用音色名（只列参考音频真的在磁盘上的）。"""
        names = []
        for name, (file_name, _desc) in VOICE_PRESETS.items():
            if (VOICE_DIR / file_name).exists():
                names.append(name.capitalize())
        return names

    def resolve_reference(self, voice: str) -> Path:
        """把客户端送来的 voice 解析成参考音频路径。

        支持三种写法：内置音色名、参考音频的绝对路径、留空。认不出来就回落到
        默认音色（和 tts_server.py 的兜底策略一致）。
        """
        raw = (voice or "").strip()

        preset = VOICE_PRESETS.get(raw.lower())
        if preset is not None:
            path = VOICE_DIR / preset[0]
            if path.exists():
                return path

        if raw:
            candidate = Path(raw)
            if candidate.is_file():
                return candidate

        default_file = VOICE_PRESETS[DEFAULT_VOICE.lower()][0]
        default_path = VOICE_DIR / default_file
        if not default_path.exists():
            raise RuntimeError(
                f"默认参考音频缺失: {default_path}（见 speech-service/README.md）"
            )
        return default_path

    def prepare_reference(self, reference: Path) -> Path:
        """把过长的参考音频截到前 ``REF_MAX_SECONDS`` 秒，结果缓存复用。

        不修改原文件：裁剪结果写到 ``generated_audio/ref/`` 下，按源文件路径
        做名字，下次直接命中。只在 ``synthesize`` 的锁里调用，不用额外加锁。
        """
        try:
            import torchaudio  # noqa: PLC0415 - 只有这条路径需要

            info = torchaudio.info(str(reference))
            max_frames = int(info.sample_rate * REF_MAX_SECONDS)
            if info.num_frames <= max_frames:
                return reference
        except Exception:  # noqa: BLE001 - 读不出信息就不裁，交给模型自己处理
            return reference

        cache_dir = OUTPUT_DIR / "ref"
        cache_dir.mkdir(parents=True, exist_ok=True)
        key = hashlib.md5(str(reference.resolve()).encode("utf-8")).hexdigest()[:8]
        target = cache_dir / f"{reference.stem}-{key}.wav"
        if not target.exists():
            waveform, rate = torchaudio.load(str(reference))
            trimmed = waveform[..., : int(rate * REF_MAX_SECONDS)]
            torchaudio.save(str(target), trimmed, rate)
            logger.info(
                "参考音频裁剪缓存: %s (%.1fs -> %.1fs)",
                reference.name, info.num_frames / info.sample_rate, REF_MAX_SECONDS,
            )
        return target

    # ---- 合成 ----------------------------------------------------------- #
    def synthesize(self, text: str, voice: str) -> tuple[bytes, int, float]:
        """合成一段文本，返回 ``(wav 字节, 采样率, 音频秒数)``。"""
        self.load()
        if self._model is None:
            raise RuntimeError(self._load_error or "MOSS-TTS-Nano 未就绪")

        reference = self.resolve_reference(voice)

        with self._lock:
            reference = self.prepare_reference(reference)
            OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
            out_path = OUTPUT_DIR / f"moss_{int(time.time() * 1000)}.wav"
            try:
                with torch.no_grad():
                    result = self._model.inference(
                        text=text,
                        output_audio_path=str(out_path),
                        mode="voice_clone",
                        prompt_audio_path=str(reference),
                        audio_tokenizer=self._codec,
                        device=self._device,
                        nq=self.nq,
                        max_new_frames=self.max_frames,
                        voice_clone_max_text_tokens=VOICE_CLONE_MAX_TEXT_TOKENS,
                        **GEN_DEFAULTS,
                    )
            finally:
                # 模型自己会把 wav 落盘；我们只要内存里的波形。
                out_path.unlink(missing_ok=True)

        waveform = result.get("waveform")
        if waveform is None:
            raise RuntimeError("模型没有返回波形")
        samples = waveform_to_mono_16k(waveform, int(result.get("sample_rate") or 0))
        if samples.size == 0:
            raise RuntimeError("模型返回了空音频")

        wav, duration = encode_wav(samples, OUTPUT_RATE)
        return wav, OUTPUT_RATE, duration


MOSS = MossNanoEngine()

app = FastAPI(title="SparkBot Local TTS (MOSS-TTS-Nano)")


# --------------------------------------------------------------------------- #
# HTTP 接口
# --------------------------------------------------------------------------- #


@app.get("/health")
async def health() -> dict[str, Any]:
    """就绪探针。"""
    return {
        "status": "ok",
        "engine": "moss-tts-nano",
        "ready": MOSS.ready,
        "error": MOSS.error,
        "load_seconds": MOSS.load_seconds,
        "voices": MOSS.list_voices(),
        "default_voice": DEFAULT_VOICE,
        "sample_rate": OUTPUT_RATE,
    }


@app.get("/v1/voices")
async def voices() -> dict[str, Any]:
    """列出可用音色（便于挑音色）。"""
    data = []
    for name, (file_name, desc) in VOICE_PRESETS.items():
        if (VOICE_DIR / file_name).exists():
            data.append(
                {"id": name.capitalize(), "voice": name.capitalize(), "description": desc}
            )
    return {"object": "list", "engine": "moss-tts-nano", "data": data}


def _clean_text(raw: str) -> str:
    text = (raw or "").strip()
    if not text:
        raise HTTPException(status_code=400, detail="input 不能为空")
    return text


@app.post("/v1/audio/speech")
async def speech(req: SpeechRequest) -> Response:
    """OpenAI 兼容的语音合成端点，返回**音频字节**。"""
    text = _clean_text(req.input)

    t0 = time.perf_counter()
    try:
        wav, rate, dur = await asyncio.to_thread(MOSS.synthesize, text, req.voice)
    except HTTPException:
        raise
    except Exception as exc:  # noqa: BLE001 - 统一转成 502 给客户端
        logger.exception("合成失败")
        raise HTTPException(status_code=502, detail=f"合成失败: {type(exc).__name__}: {exc}") from exc

    total_ms = (time.perf_counter() - t0) * 1000
    # 延迟与实时率是部署时最想看到的数据。
    logger.info(
        "合成: %d 字 -> %.2fs 音频, 总计 %.0fms | RTF %.3f | %r",
        len(text), dur, total_ms, total_ms / 1000 / dur if dur else 0.0, text[:30],
    )

    return Response(
        content=wav,
        media_type="audio/wav",
        headers={
            "X-TTS-Engine": MOSS.name,
            "X-Audio-Seconds": f"{dur:.3f}",
            "X-Synth-Ms": f"{total_ms:.1f}",
            "X-Sample-Rate": str(rate),
        },
    )


@app.post("/v1/audio/speech/stream")
async def speech_stream(req: StreamSpeechRequest) -> StreamingResponse:
    """流式语音合成：**整段合成后一次性下发**裸 PCM。

    和 tts_server.py 的兜底路径一样，这里不做真流式：真流式要模型边生成边解码，
    而板子实测连续小块喂 I2S 会「呲呲」（见提交「播报不再走流式」），所以改成
    整段合成好再发。接口形态保持一致，客户端不用区分。
    """
    text = _clean_text(req.input)

    try:
        wav, _rate, dur = await asyncio.to_thread(MOSS.synthesize, text, req.voice)
        pcm = wav_to_pcm16(wav)
    except HTTPException:
        raise
    except Exception as exc:  # noqa: BLE001
        logger.exception("流式合成失败")
        raise HTTPException(status_code=502, detail=f"合成失败: {type(exc).__name__}: {exc}") from exc

    logger.info("流式(整段): %d 字 -> %.2fs 音频, %d 字节 PCM", len(text), dur, len(pcm))
    return StreamingResponse(
        iter((pcm,)),
        media_type="audio/L16",
        headers={
            "X-TTS-Engine": f"{MOSS.name}-whole",
            "X-Sample-Rate": str(OUTPUT_RATE),
            "X-Channels": "1",
            "X-Sample-Format": "s16le",
        },
    )


def main() -> int:
    """命令行入口。"""
    global DEFAULT_VOICE  # noqa: PLW0603 - 允许命令行覆盖默认音色

    p = argparse.ArgumentParser(description="本地 TTS 服务：MOSS-TTS-Nano")
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", type=int, default=8761)
    p.add_argument("--voice", default=DEFAULT_VOICE, help="默认音色名，如 Junhao / Xiaoyu")
    p.add_argument("--threads", type=int, default=0, help="torch 线程数，0 = 用默认")
    p.add_argument("--nq", type=int, default=0, help="音频码本数（越小越快、音质越低），0 = 用满")
    p.add_argument("--max-frames", type=int, default=MAX_NEW_FRAMES, help="单次最长帧数")
    p.add_argument("--no-preload", action="store_true", help="启动不加载模型，首个请求再加载")
    args = p.parse_args()

    if args.threads > 0:
        torch.set_num_threads(args.threads)

    MOSS.nq = args.nq or None
    MOSS.max_frames = args.max_frames

    if args.voice.strip().capitalize() in {v.capitalize() for v in VOICE_PRESETS}:
        DEFAULT_VOICE = args.voice.strip().capitalize()
    elif args.voice.strip():
        logger.warning("未知音色 %r，继续用默认音色 %s", args.voice, DEFAULT_VOICE)

    logger.info(
        "MOSS-TTS-Nano 服务启动: http://%s:%d/v1  (voice=%s, 输出 %dHz)",
        args.host, args.port, DEFAULT_VOICE, OUTPUT_RATE,
    )

    if not args.no_preload:
        MOSS.load()
        if MOSS.ready:
            logger.info("模型预热完成，首句不用再等加载。")
        else:
            logger.warning(
                "模型未就绪（%s）；服务照常启动，/health 会报 ready=false。", MOSS.error
            )

    import uvicorn

    uvicorn.run(app, host=args.host, port=args.port, log_level="warning")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
