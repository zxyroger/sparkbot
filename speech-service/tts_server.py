"""本地 TTS 服务：edge-tts，OpenAI 兼容接口。

为什么选 edge-tts 而不是 CosyVoice2：
    * **不需要 GPU、不需要下载模型**（CosyVoice2 模型 1.5GB，CPU 上合成
      一句话要 3~15 秒，对话会有明显停顿）；
    * 音质是微软的神经网络语音，中文很自然；
    * 延迟极低（实测首次约 1 秒，之后更快）。
    代价：它是**在线**服务（走微软接口），不是完全离线。

关于输出格式（关键实现细节）：
    edge-tts 7.x **只输出 MP3**（`MP3_BITRATE_BPS`，48kbps），没有 PCM 选项。
    而板子用的是 `esp_codec_dev`，**播不了 MP3**。
    所以这里在服务端把 MP3 解成 **24kHz / 单声道 / 16bit** 的 WAV 再返回。
    24kHz 不用降到 16kHz —— 固件里的 `bot_audio_play()` 已实现线性重采样，
    会自动转成板子的 16kHz。

    解码用 `soundfile`（libsndfile 自 1.1 起支持 MP3），无需 ffmpeg。

接口契约（照抄自 sparkbot/perception/speech.py）：
    请求： JSON
        {"model": ..., "voice": "zh-CN-XiaoxiaoNeural",
         "input": "要合成的文本", "response_format": "wav"}
    响应：**音频字节**（不是 JSON）
        Content-Type: audio/wav

用法::

    D:\\dsh\\sparkbot\\speech-service\\.venv\\Scripts\\python.exe tts_server.py
    ... tts_server.py --port 8761 --voice zh-CN-YunxiNeural
"""

from __future__ import annotations

import argparse
import asyncio
import io
import logging
import time
import wave
from typing import Any

import edge_tts
import numpy as np
import soundfile as sf
import uvicorn
from fastapi import FastAPI, HTTPException
from fastapi.responses import Response
from pydantic import BaseModel, Field

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("edge-tts")

#: 默认中文女声。中文语音清单：
#:   zh-CN-XiaoxiaoNeural(女)  zh-CN-XiaoyiNeural(女)
#:   zh-CN-YunjianNeural(男)   zh-CN-YunxiNeural(男)
#:   zh-CN-YunxiaNeural(男)    zh-CN-YunyangNeural(男)
#:   zh-CN-liaoning-XiaobeiNeural(东北女)  zh-CN-shaanxi-XiaoniNeural(陕西女)
DEFAULT_VOICE = "zh-CN-XiaoxiaoNeural"

#: 启动参数里 ``--voice`` 的默认值。
#: 之所以要有这个常量：``main()`` 里要用 ``global DEFAULT_VOICE`` 覆盖它，
#: 而 Python 不允许在模块级已经引用过同名变量后再声明 global
#: （会报 "name is used prior to global declaration"）。
#: 用一个初始化常量就能绕开，同时保持默认值只在源码里写一次。
_INITIAL_VOICE = DEFAULT_VOICE

app = FastAPI(title="SparkBot Local TTS (edge-tts)")


class SpeechRequest(BaseModel):
    """OpenAI `/v1/audio/speech` 的请求体。"""

    model: str = "edge-tts"
    input: str = Field(..., description="要合成的文本")  # noqa: A003 - 字段名沿用 OpenAI
    voice: str = DEFAULT_VOICE
    response_format: str = "wav"
    # 这些是 OpenAI 有但我们不用的字段，接受但不报错，免得客户端被拒
    speed: float | None = None
    rate: str | None = None
    volume: str | None = None
    pitch: str | None = None


def mp3_to_wav(mp3: bytes) -> tuple[bytes, int, float]:
    """把 edge-tts 输出的 MP3 解成 WAV。

    Returns:
        ``(wav 字节, 采样率, 时长秒)``
    """
    data, rate = sf.read(io.BytesIO(mp3), dtype="int16", always_2d=True)

    # 多声道就降成单声道（板子是单声道，多传没意义）
    if data.shape[1] > 1:
        data = data[:, :1]

    pcm = np.ascontiguousarray(data).tobytes()
    duration = data.shape[0] / float(rate)

    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(pcm)
    return buf.getvalue(), rate, duration


async def edge_synthesize(text: str, voice: str, rate: str | None,
                          volume: str | None, pitch: str | None) -> bytes:
    """调用 edge-tts 拿 MP3 字节。"""
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


@app.get("/health")
async def health() -> dict[str, Any]:
    """就绪探针。"""
    return {"status": "ok", "engine": "edge-tts", "default_voice": DEFAULT_VOICE,
            "version": getattr(edge_tts, "__version__", "?")}


@app.get("/v1/voices")
async def voices(locale: str = "zh-CN") -> dict[str, Any]:
    """列出可用语音（便于挑音色）。"""
    try:
        all_voices = await edge_tts.list_voices()
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=502, detail=f"获取语音列表失败: {exc}") from exc
    items = [{"id": v["ShortName"], "gender": v.get("Gender"), "locale": v.get("Locale")}
             for v in all_voices if str(v.get("Locale", "")).startswith(locale)]
    return {"object": "list", "data": items}


@app.post("/v1/audio/speech")
async def speech(req: SpeechRequest) -> Response:
    """OpenAI 兼容的语音合成端点，返回**音频字节**。"""
    text = (req.input or "").strip()
    if not text:
        raise HTTPException(status_code=400, detail="input 不能为空")

    t0 = time.perf_counter()
    try:
        mp3 = await edge_synthesize(text, req.voice, req.rate, req.volume, req.pitch)
    except Exception as exc:  # noqa: BLE001
        logger.exception("合成失败")
        raise HTTPException(status_code=502, detail=f"合成失败: {exc}") from exc
    t_tts = time.perf_counter()

    if not mp3:
        raise HTTPException(status_code=502, detail="edge-tts 没有返回音频数据")

    fmt = (req.response_format or "wav").lower()
    if fmt == "mp3":
        # 客户端要 MP3 就原样返回（注意：板子播不了 MP3，主项目默认用 wav）
        logger.info("合成: %d 字 -> mp3 %d 字节, %.0fms", len(text), len(mp3),
                    (t_tts - t0) * 1000)
        return Response(content=mp3, media_type="audio/mpeg")

    try:
        wav, rate, dur = mp3_to_wav(mp3)
    except Exception as exc:  # noqa: BLE001
        logger.exception("MP3 解码失败")
        raise HTTPException(status_code=500, detail=f"MP3 解码失败: {exc}") from exc

    total_ms = (time.perf_counter() - t0) * 1000
    # 这几行是部署时最想拿到的数据：延迟与实时率
    logger.info(
        "合成: %d 字 -> %.2fs 音频 | 网络+合成 %.0fms, 总计 %.0fms | RTF %.3f | %r",
        len(text), dur, (t_tts - t0) * 1000, total_ms,
        total_ms / 1000 / dur if dur > 0 else 0, text[:30],
    )

    return Response(
        content=wav,
        media_type="audio/wav",
        headers={
            # 诊断信息用响应头带出去，客户端可忽略
            "X-Audio-Seconds": f"{dur:.3f}",
            "X-Synth-Ms": f"{total_ms:.1f}",
            "X-Sample-Rate": str(rate),
        },
    )


def main() -> int:
    """命令行入口。"""
    global DEFAULT_VOICE  # noqa: PLW0603 - 允许用 --voice 覆盖默认音色

    p = argparse.ArgumentParser(description="本地 edge-tts 服务（OpenAI 兼容）")
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", type=int, default=8761)
    p.add_argument("--voice", default=_INITIAL_VOICE, help="默认语音（可被请求覆盖）")
    args = p.parse_args()

    DEFAULT_VOICE = args.voice

    logger.info("TTS 服务启动: http://%s:%d/v1  (engine=edge-tts, voice=%s)",
                args.host, args.port, args.voice)
    logger.info("注意：edge-tts 是**在线**服务，需要能访问微软接口")
    uvicorn.run(app, host=args.host, port=args.port, log_level="warning")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
