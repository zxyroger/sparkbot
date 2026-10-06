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
import io
import json
import logging
import time
import wave
from typing import Any

import numpy as np
import uvicorn
from fastapi import FastAPI, File, Form, HTTPException, UploadFile
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


def main() -> int:
    """命令行入口：加载模型后启动 HTTP 服务。"""
    global _model, _model_name, _load_seconds

    p = argparse.ArgumentParser(description="本地 SenseVoice ASR 服务（OpenAI 兼容）")
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", type=int, default=8000)
    p.add_argument("--device", default="cpu", help="cpu 或 cuda:0")
    p.add_argument("--model", default="iic/SenseVoiceSmall", help="ModelScope 模型 id")
    p.add_argument("--revision", default=None, help="可选：固定模型版本")
    p.add_argument("--warmup", action="store_true", default=True, help="启动时用静音预热")
    args = p.parse_args()

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

    logger.info("服务启动: http://%s:%d/v1  (health: /health)", args.host, args.port)
    uvicorn.run(app, host=args.host, port=args.port, log_level="warning")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
