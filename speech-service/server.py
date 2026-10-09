"""本地 ASR 服务：SenseVoice-Small，OpenAI 兼容接口。

为什么要有这个服务：
    主项目（sparkbot）的语音链路被设计成调用**云端 OpenAI 兼容接口**
    （`POST /v1/audio/transcriptions`）。本地部署模型有两条路：
      A. 起一个兼容服务，主项目只改 base_url —— **本文件走这条**；
      B. 在主项目里直接 import 模型 —— 会把 torch 等重依赖塞进主项目。
    路线 A 的好处：依赖完全隔离在这个 venv 里，主项目零改动，
    而且模型加载只发生一次（服务常驻），不会每次请求都重新加载。

与主项目的契约（照抄自 sparkbot/perception/speech.py）：
    请求：multipart/form-data
        file  = WAV（16kHz / 16bit / 单声道）
        model = 模型名（本服务实际上忽略它，固定用已加载的模型）
    响应：JSON {"text": "识别出的文字"}

用法::

    D:\\dsh\\sparkbot\\speech-service\\.venv\\Scripts\\python.exe server.py
    D:\\dsh\\sparkbot\\speech-service\\.venv\\Scripts\\python.exe server.py --port 8000 --device cpu

首次运行会从 ModelScope 下载模型（SenseVoice-Small 约 500MB），
并且要预热 —— 服务起来后的**第一次请求**可能明显偏慢，属正常现象。
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import io
import json
import logging
import time
import wave
from pathlib import Path
from typing import Any

import numpy as np
import uvicorn
from fastapi import FastAPI, File, Form, HTTPException, UploadFile, WebSocket, WebSocketDisconnect
from fastapi.responses import JSONResponse

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("sensevoice")

#: SenseVoice 支持的语言标记。auto 让它自己判断。
_LANG_MAP = {
    "auto": "auto",
    "zh": "zh",
    "en": "en",
    "yue": "yue",
    "ja": "ja",
    "ko": "ko",
}

app = FastAPI(title="SparkBot Local ASR (SenseVoice)")

#: 模型句柄，启动时加载一次。
_model: Any = None
_model_name: str = ""
_load_seconds: float = 0.0

# --------------------------------------------------------------------------- #
# 流式识别（FunASR Paraformer streaming）
# --------------------------------------------------------------------------- #
#:
#: 为什么用 Paraformer 而不是继续用 SenseVoice：
#: SenseVoice 是**离线**模型 —— 必须拿到整段音频才能出结果，做不到"边说边出字"。
#: Paraformer-online 是 FunASR 的流式模型（同一个 venv、同一套依赖），
#: 按 600ms 一块增量解码，每块都能给出**到目前为止**的文本。
#:
#: 代价：识别精度略低于 SenseVoice-large。批处理接口 `/v1/audio/transcriptions`
#: 仍然用 SenseVoice，两条链路互相独立，哪个更合适由调用方选。
#: 用 **large** 版本：小版本（speech_paraformer_asr_nat-...-online，280MB）实测
#: 会把"一台履带式"听成"你台你大师的"，误差大到会影响大模型理解。large 版
#: 是 848MB，同一条音频基本能读对。这条实测结论值得记下来，别再换回小版本。
STREAM_MODEL_ID = "iic/speech_paraformer-large_asr_nat-zh-cn-16k-common-vocab8404-online"

#: 600ms 一块（FunASR 约定：单位为 60ms 帧，[0, 10, 5] = 左看 0 / 当前 10 / 右看 5）。
STREAM_CHUNK_SIZE = [0, 10, 5]
#: 每块 600ms 的采样数（960 = 60ms @ 16kHz）。
STREAM_STRIDE = STREAM_CHUNK_SIZE[1] * 960
STREAM_SAMPLE_RATE = 16000

#: 流式模型句柄与它的互斥锁。
#: 模型本身是共享的（一份权重），但 torch 前向不适合并发调同一实例 ——
#: 单机器人场景下串行化最简单也最稳。每个连接有**自己的 cache**（解码状态）。
_stream_model: Any = None
_stream_ready = False
_stream_error: str | None = None
_stream_load_seconds = 0.0
_stream_lock = asyncio.Lock()

# --------------------------------------------------------------------------- #
# 说话人声纹（speaker embedding）
# --------------------------------------------------------------------------- #
#:
#: 用 CAM++ 中文说话人模型（3D-Speaker，16kHz）：把一段语音压成 192 维向量，
#: 同一个人的两段话向量接近、不同人相差大。PC 侧据此判断"现在是谁在说话"，
#: 从而把长期记忆里的名字对到人头上。
#:
#: 模型是**离线**的、单次推理约 10~30ms（2 秒音频），可以每条语音都算。
_SPEAKER_MODEL = (
    Path(__file__).resolve().parent
    / "models" / "speaker" / "3dspeaker_speech_campplus_sv_zh-cn_16k-common.onnx"
)
_speaker_extractor: Any = None
_speaker_error: str | None = None
_speaker_load_seconds = 0.0

#: 最终文本用哪个模型出：
#:   * ``batch`` —— 说完后用 SenseVoice 对整段重识别一次（默认）。
#:     流式的 partial 照常实时展示，但**最终文本取精度更高的那个**。
#:     代价：句末多花约 0.3~0.5 秒（与 SenseVoice 的 RTF 一致）。
#:   * ``stream`` —— 直接用流式模型累积出来的文本，句末零额外延迟，
#:     但精度略低于 SenseVoice（实测会把"一台履带式"听成"你凯旅带式"）。
#:
#: 为什么默认 batch：对这台机器人来说，**回复正确**比省 0.4 秒更重要 ——
#: 识别错一个字，大模型可能整句答偏。要极致的低延迟就设 stream。
STREAM_FINAL_MODE = "batch"


def load_wav_to_float32(data: bytes) -> np.ndarray:
    """把 WAV 字节解成 float32 单声道波形（范围 ±1）。

    SenseVoice 期望的是 **float32 波形**，不是 WAV 容器。
    这里不用 soundfile/librosa，只用标准库 wave + numpy：
    依赖越少，装环境和排错越简单。
    """
    with wave.open(io.BytesIO(data), "rb") as w:
        channels = w.getnchannels()
        width = w.getsampwidth()
        rate = w.getframerate()
        frames = w.readframes(w.getnframes())

    if width != 2:
        raise ValueError(f"只支持 16bit WAV，收到 {width * 8}bit")

    pcm = np.frombuffer(frames, dtype=np.int16).astype(np.float32) / 32768.0

    # 多声道就取第一声道（我们的设备永远是单声道，这里是防御）
    if channels > 1:
        pcm = pcm.reshape(-1, channels)[:, 0]

    # SenseVoice 内部按 16kHz 训练；采样率不符时给出明确警告而不是静默出错
    if rate != 16000:
        logger.warning("输入采样率 %d Hz，模型期望 16000 Hz —— 识别质量可能下降", rate)

    return pcm


def extract_text(result: Any) -> str:
    """从 FunASR 的返回里取出纯文本。

    ``generate()`` 返回 ``[{"key": ..., "text": ...}]``，但 text 里可能带
    SenseVoice 的特殊标记（如情绪/事件标记 ``<|...|>``），需要清掉。
    """
    if isinstance(result, list) and result:
        item = result[0]
        if isinstance(item, dict):
            text = str(item.get("text", ""))
        else:
            text = str(item)
    elif isinstance(result, dict):
        text = str(result.get("text", ""))
    else:
        text = str(result or "")

    # 去掉 SenseVoice 的富文本标记 <|zh|><|NEUTRAL|><|Speech|> 等
    out_chars: list[str] = []
    depth = 0
    for ch in text:
        if ch == "<":
            depth += 1
        elif ch == ">":
            if depth > 0:
                depth -= 1
        elif depth == 0:
            out_chars.append(ch)
    return "".join(out_chars).strip()


@app.get("/health")
async def health() -> dict[str, Any]:
    """就绪探针（FunASR 官方服务也用这个路径）。"""
    return {
        "status": "ok" if _model is not None else "loading",
        "model": _model_name,
        "load_seconds": round(_load_seconds, 1),
        # 流式链路的状态单独报：批处理能用、流式没起来，是两种不同的可用性
        "stream": {
            "ready": _stream_ready,
            "model": STREAM_MODEL_ID,
            "error": _stream_error,
            "load_seconds": round(_stream_load_seconds, 1),
            "chunk_ms": 600,
        },
        # 声纹（说话人识别）
        "speaker": {
            "ready": _speaker_extractor is not None,
            "dim": _speaker_extractor.dim if _speaker_extractor is not None else None,
            "model": _SPEAKER_MODEL.name,
            "error": _speaker_error,
            "load_seconds": round(_speaker_load_seconds, 1),
        },
    }


@app.get("/v1/models")
async def models() -> dict[str, Any]:
    """列出可用模型 —— 客户端常用它验证服务是否真的加载好了。"""
    return {
        "object": "list",
        "data": [{"id": _model_name or "sensevoice", "object": "model", "owned_by": "local"}],
    }


@app.post("/v1/audio/transcriptions")
async def transcriptions(
    file: UploadFile = File(...),  # noqa: B008 - FastAPI 的依赖注入写法
    model: str = Form("sensevoice"),  # noqa: A002 - 字段名必须叫 model
    language: str = Form("auto"),
    response_format: str = Form("json"),
) -> JSONResponse:
    """OpenAI 兼容的转写端点。

    ``model`` 参数被接受但忽略（服务只加载一个模型）—— 保留它是为了
    兼容客户端的调用习惯，而不是为了多模型路由。
    """
    if _model is None:
        raise HTTPException(status_code=503, detail="模型尚未加载完成")

    raw = await file.read()
    if not raw:
        raise HTTPException(status_code=400, detail="空的音频文件")

    t0 = time.perf_counter()
    try:
        pcm = load_wav_to_float32(raw)
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=400, detail=f"音频解析失败: {exc}") from exc
    decode_ms = (time.perf_counter() - t0) * 1000

    lang = _LANG_MAP.get(language.lower(), "auto")

    t1 = time.perf_counter()
    try:
        result = _model.generate(
            input=pcm,
            cache={},
            language=lang,
            use_itn=True,          # 逆文本正则化：把"一二三"转成数字等，更适合对话
            batch_size_s=60,
            merge_vad=True,
            merge_length_s=15,
        )
    except Exception as exc:  # noqa: BLE001
        logger.exception("识别失败")
        raise HTTPException(status_code=500, detail=f"识别失败: {exc}") from exc
    infer_ms = (time.perf_counter() - t1) * 1000

    text = extract_text(result)
    audio_s = len(pcm) / 16000.0
    rtf = (infer_ms / 1000) / audio_s if audio_s > 0 else 0.0

    # 这几行是本次部署最想拿到的数据：延迟与实时率
    logger.info(
        "识别: 音频 %.2fs -> %.2fs 推理 (RTF %.3f, 解码 %.0fms) | %r",
        audio_s, infer_ms / 1000, rtf, decode_ms, text[:60],
    )

    if response_format == "text":
        return JSONResponse(content={"text": text})
    return JSONResponse(
        content={
            "text": text,
            "model": _model_name,
            # 额外的诊断字段：OpenAI 客户端会忽略未知字段，不影响兼容性
            "audio_seconds": round(audio_s, 3),
            "infer_ms": round(infer_ms, 1),
            "rtf": round(rtf, 4),
        }
    )


# --------------------------------------------------------------------------- #
# 流式识别
# --------------------------------------------------------------------------- #
@app.post("/v1/speaker/embed")
async def speaker_embed(
    file: UploadFile = File(...),  # noqa: B008 - FastAPI 依赖注入写法
) -> JSONResponse:
    """说话人声纹：上传一段 WAV，返回 192 维 embedding（已归一化）。

    用法上跟 ``/v1/audio/transcriptions`` 完全对称 —— 采集到一段语音就
    顺手算一次声纹，PC 侧拿它去和已登记的人比对（余弦相似度）。

    返回的向量**已做 L2 归一化**，所以比较时直接点积就是余弦相似度。
    """
    if _speaker_extractor is None:
        raise HTTPException(status_code=503, detail=_speaker_error or "声纹模型未就绪")

    raw = await file.read()
    if not raw:
        raise HTTPException(status_code=400, detail="空的音频文件")
    try:
        pcm = load_wav_to_float32(raw)
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=400, detail=f"音频解析失败: {exc}") from exc

    # 太短的音频算不出稳定声纹（CAM++ 至少需要几百毫秒）
    if pcm.size < 16000 * 0.4:
        raise HTTPException(status_code=400, detail="音频太短，至少需要 0.4 秒")

    t0 = time.perf_counter()
    try:
        emb = await asyncio.to_thread(_speaker_embedding, pcm, 16000)
    except Exception as exc:  # noqa: BLE001
        logger.exception("声纹提取失败")
        raise HTTPException(status_code=500, detail=f"声纹提取失败: {exc}") from exc
    infer_ms = (time.perf_counter() - t0) * 1000

    logger.info(
        "声纹: 音频 %.2fs -> %d 维 (%.0fms)",
        pcm.size / 16000.0, len(emb), infer_ms,
    )
    return JSONResponse(content={
        "embedding": emb,
        "dim": len(emb),
        "audio_seconds": round(pcm.size / 16000.0, 3),
        "infer_ms": round(infer_ms, 1),
    })


def _speaker_embedding(pcm: np.ndarray, sample_rate: int) -> list[float]:
    """同步算一段语音的声纹向量（L2 归一化后返回）。"""
    stream = _speaker_extractor.create_stream()
    stream.accept_waveform(sample_rate=sample_rate, waveform=pcm)
    stream.input_finished()
    vec = np.asarray(_speaker_extractor.compute(stream), dtype=np.float32).reshape(-1)
    norm = float(np.linalg.norm(vec))
    if norm > 1e-9:
        vec = vec / norm
    return [round(float(x), 6) for x in vec]


def _stream_decode(chunk: np.ndarray, cache: dict[str, Any], is_final: bool) -> str:
    """同步解一块流式音频，返回**到目前为止**的整段文本。

    FunASR 的流式 API 靠 ``cache`` 维护解码状态：每喂一块，返回的是
    从会话开始累积到现在的文本（不是这一块的增量），所以调用方直接
    用返回值替换即可，不需要自己拼字符串。
    """
    result = _stream_model.generate(
        input=chunk,
        cache=cache,
        is_final=is_final,
        chunk_size=STREAM_CHUNK_SIZE,
        encoder_chunk_look_back=4,
        decoder_chunk_look_back=1,
    )
    return extract_text(result)


async def _stream_step(chunk: np.ndarray, cache: dict[str, Any], is_final: bool) -> str:
    """把阻塞的流式前向放到线程里，并用全局锁串行化。"""
    async with _stream_lock:
        return await asyncio.to_thread(_stream_decode, chunk, cache, is_final)


def _batch_decode(pcm: np.ndarray) -> str:
    """用批处理模型（SenseVoice）重识别整段，作为最终文本。"""
    result = _model.generate(
        input=pcm, cache={}, language="auto", use_itn=True,
        batch_size_s=60, merge_vad=True, merge_length_s=15,
    )
    return extract_text(result)


@app.websocket("/v1/audio/stream")
async def audio_stream(ws: WebSocket) -> None:
    """流式转写：客户端边推 PCM，服务端边回 partial 文本。

    协议（文本帧 JSON / 二进制帧裸 PCM 混用）::

        服务端 → {"type": "ready", "sample_rate": 16000}
        客户端 → 二进制帧：16kHz / 16bit / 单声道 little-endian PCM（任意长度）
        服务端 → {"type": "partial", "text": "到目前为止的文本"}   (可多次)
        客户端 → {"type": "end"}
        服务端 → {"type": "final", "text": "...", "audio_seconds": 3.2, ...}

    为什么每块回的是"整段文本"而不是增量：模型本身就是这么给的
    （见 ``_stream_decode``），而且尾部几个字会随下文修正 —— 让客户端
    直接替换比让它拼增量更不容易错。
    """
    await ws.accept()

    if not _stream_ready:
        await ws.send_json({"type": "error", "message": _stream_error or "流式模型未就绪"})
        await ws.close()
        return

    await ws.send_json({"type": "ready", "sample_rate": STREAM_SAMPLE_RATE})

    cache: dict[str, Any] = {}
    buffer = np.zeros(0, dtype=np.float32)
    heard: list[np.ndarray] = []
    text = ""
    total = 0
    t0 = time.perf_counter()
    infer_s = 0.0

    async def decode(chunk: np.ndarray, is_final: bool) -> None:
        nonlocal text, infer_s
        step0 = time.perf_counter()
        fragment = await _stream_step(chunk, cache, is_final)
        infer_s += time.perf_counter() - step0
        if not fragment:
            return

        # FunASR 的流式接口返回的是**本块新增的片段**（实测：整句由
        # '你' '好呀' '我是' '小星' … 逐块拼出来），所以要累加。
        # 同时兼容"返回累积文本"的实现：新片段若以已有文本开头，直接替换。
        if fragment.startswith(text):
            merged = fragment
        elif text.endswith(fragment):
            merged = text
        else:
            merged = text + fragment

        if merged != text:
            text = merged
            await ws.send_json({"type": "partial", "text": text})

    try:
        while True:
            message = await ws.receive()
            if message.get("type") == "websocket.disconnect":
                break

            raw = message.get("bytes")
            if raw is None:
                payload = message.get("text")
                if payload:
                    with contextlib.suppress(ValueError):
                        if json.loads(payload).get("type") == "end":
                            break
                continue

            pcm = np.frombuffer(raw, dtype=np.int16).astype(np.float32) / 32768.0
            total += int(pcm.size)
            heard.append(pcm)
            buffer = pcm if buffer.size == 0 else np.concatenate([buffer, pcm])

            # 留最后一块不立刻解：它的下文还没到，早解出来反而会在
            # 下一块到达时整体改写。等够两块再解，partial 更稳定。
            while buffer.size >= 2 * STREAM_STRIDE:
                await decode(buffer[:STREAM_STRIDE], False)
                buffer = buffer[STREAM_STRIDE:]

        # 收尾：把剩下的（不足一块就补静音）用 is_final 解一次，
        # 让模型把最后一个字的声学上下文看完，否则尾字常被吃掉。
        tail = buffer
        if tail.size < STREAM_STRIDE:
            tail = np.concatenate([tail, np.zeros(STREAM_STRIDE - tail.size, dtype=np.float32)])
        await decode(tail, True)

        # 收尾：默认用批处理模型（SenseVoice）复核整段。
        # 流式的 partial 已经实时给过了，最终文本取更准的那个。
        if STREAM_FINAL_MODE == "batch" and _model is not None and heard:
            try:
                audio = np.concatenate(heard)
                step0 = time.perf_counter()
                async with _stream_lock:
                    refined = await asyncio.to_thread(_batch_decode, audio)
                infer_s += time.perf_counter() - step0
                if refined:
                    text = refined
                    logger.info("流式收尾: SenseVoice 复核完成（%.0fms）",
                                (time.perf_counter() - step0) * 1000)
            except Exception as exc:  # noqa: BLE001 - 复核失败就用流式结果
                logger.warning("流式收尾复核失败，沿用流式结果: %s", exc)

        elapsed_ms = (time.perf_counter() - t0) * 1000
        audio_s = total / STREAM_SAMPLE_RATE
        logger.info(
            "流式识别: 音频 %.2fs -> 端到端 %.0fms（解码 %.0fms, RTF %.3f）| %r",
            audio_s, elapsed_ms, infer_s * 1000,
            (infer_s / audio_s) if audio_s > 0 else 0.0, text[:60],
        )
        await ws.send_json({
            "type": "final",
            "text": text,
            "audio_seconds": round(audio_s, 3),
            "elapsed_ms": round(elapsed_ms, 1),
            "rtf": round((infer_s / audio_s) if audio_s > 0 else 0.0, 4),
        })
    except WebSocketDisconnect:
        logger.info("流式识别连接断开（已收到 %.2fs 音频）", total / STREAM_SAMPLE_RATE)
    except Exception as exc:  # noqa: BLE001 - 不能让单个会话把服务带崩
        logger.exception("流式识别失败")
        with contextlib.suppress(Exception):
            await ws.send_json({"type": "error", "message": f"{type(exc).__name__}: {exc}"})
    finally:
        with contextlib.suppress(Exception):
            await ws.close()


def main() -> int:
    """命令行入口：加载模型后启动 HTTP 服务。"""
    global _model, _model_name, _load_seconds
    global _stream_model, _stream_ready, _stream_error, _stream_load_seconds
    global STREAM_FINAL_MODE
    global _speaker_extractor, _speaker_error, _speaker_load_seconds

    p = argparse.ArgumentParser(description="本地 SenseVoice ASR 服务（OpenAI 兼容）")
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", type=int, default=8000)
    p.add_argument("--device", default="cpu", help="cpu 或 cuda:0")
    p.add_argument("--model", default="iic/SenseVoiceSmall", help="ModelScope 模型 id")
    p.add_argument("--revision", default=None, help="可选：固定模型版本")
    p.add_argument("--warmup", action="store_true", default=True, help="启动时用静音预热")
    p.add_argument("--stream-model", default=STREAM_MODEL_ID,
                   help="流式识别用的 FunASR 模型 id")
    p.add_argument("--no-stream", action="store_true",
                   help="不加载流式模型（只保留批处理接口）")
    p.add_argument("--no-speaker", action="store_true",
                   help="不加载说话人声纹模型")
    p.add_argument("--stream-final", choices=("batch", "stream"), default=STREAM_FINAL_MODE,
                   help="最终文本来源：batch=用 SenseVoice 复核（准，句末 +0.4s）；"
                        "stream=直接用流式结果（快，精度略低）")
    args = p.parse_args()
    STREAM_FINAL_MODE = args.stream_final

    logger.info("正在加载模型 %s (device=%s) …", args.model, args.device)
    t0 = time.perf_counter()

    from funasr import AutoModel

    _model = AutoModel(
        model=args.model,
        device=args.device,
        # 关掉不需要的功能，减少加载时间与依赖：
        disable_update=True,      # 不联网检查更新
        disable_pbar=True,
        disable_log=False,
    )
    _model_name = args.model
    _load_seconds = time.perf_counter() - t0
    logger.info("模型加载完成，耗时 %.1f 秒", _load_seconds)

    if args.warmup:
        # 预热：第一次 generate 会做图优化/分配缓存，耗时可观。
        # 不预热的话把它算到用户第一次请求上，会误以为服务很慢。
        logger.info("预热中（用 1 秒静音）…")
        t1 = time.perf_counter()
        try:
            _model.generate(input=np.zeros(16000, dtype=np.float32), cache={},
                            language="auto", use_itn=True, batch_size_s=60)
        except Exception:  # noqa: BLE001
            logger.warning("预热失败（不影响后续请求）", exc_info=True)
        logger.info("预热完成，耗时 %.1f 秒", time.perf_counter() - t1)

    # ---- 流式模型（Paraformer online） --------------------------------- #
    if args.no_stream:
        _stream_error = "已通过 --no-stream 关闭"
        logger.info("流式识别已关闭（--no-stream）")
    else:
        st0 = time.perf_counter()
        logger.info("正在加载流式模型 %s …", args.stream_model)
        try:
            from funasr import AutoModel

            _stream_model = AutoModel(
                model=args.stream_model,
                device=args.device,
                disable_update=True,
                disable_pbar=True,
                disable_log=False,
            )
            # 预热：跑一次空会话，把图建好。否则第一轮对话会多等好几秒。
            _stream_model.generate(
                input=np.zeros(STREAM_STRIDE, dtype=np.float32),
                cache={},
                is_final=True,
                chunk_size=STREAM_CHUNK_SIZE,
                encoder_chunk_look_back=4,
                decoder_chunk_look_back=1,
            )
            _stream_ready = True
        except Exception as exc:  # noqa: BLE001 - 流式起不来不影响批处理
            _stream_error = f"{type(exc).__name__}: {exc}"
            logger.warning("流式模型加载失败（批处理仍然可用）: %s", _stream_error, exc_info=True)
        _stream_load_seconds = time.perf_counter() - st0
        if _stream_ready:
            logger.info("流式模型就绪，耗时 %.1f 秒（600ms 一块）", _stream_load_seconds)

    # ---- 说话人声纹（CAM++） ------------------------------------------- #
    if args.no_speaker:
        _speaker_error = "已通过 --no-speaker 关闭"
        logger.info("声纹识别已关闭（--no-speaker）")
    elif not _SPEAKER_MODEL.exists():
        _speaker_error = f"模型文件不存在: {_SPEAKER_MODEL}"
        logger.warning("声纹模型缺失，说话人识别不可用: %s", _SPEAKER_MODEL)
    else:
        sp0 = time.perf_counter()
        logger.info("正在加载声纹模型 %s …", _SPEAKER_MODEL.name)
        try:
            import sherpa_onnx

            cfg = sherpa_onnx.SpeakerEmbeddingExtractorConfig(
                model=str(_SPEAKER_MODEL), num_threads=2, provider="cpu"
            )
            if not cfg.validate():
                raise RuntimeError("配置校验失败")
            _speaker_extractor = sherpa_onnx.SpeakerEmbeddingExtractor(cfg)
        except Exception as exc:  # noqa: BLE001 - 声纹坏了不影响识别/合成
            _speaker_error = f"{type(exc).__name__}: {exc}"
            logger.warning("声纹模型加载失败: %s", _speaker_error, exc_info=True)
        _speaker_load_seconds = time.perf_counter() - sp0
        if _speaker_extractor is not None:
            logger.info(
                "声纹模型就绪，耗时 %.1f 秒（%d 维）",
                _speaker_load_seconds, _speaker_extractor.dim,
            )

    logger.info("服务启动: http://%s:%d/v1  (health: /health)", args.host, args.port)
    uvicorn.run(app, host=args.host, port=args.port, log_level="warning")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
