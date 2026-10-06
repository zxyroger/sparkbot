"""合成画面生成：让模拟设备「看见」一个可描述的场景。

目标是产出**多模态模型能真正识别出物体**的图像，而不是纯噪声。
做法是在渐变背景上画几个高对比度的几何物体（杯子、书本、球、箱子），
并附上位置关系——这样接上真实 VLM 时能验证「视觉工具确实拿到了有意义的图」。
"""

from __future__ import annotations

import base64
import colorsys
import io
import logging
import math
import struct
import zlib
from dataclasses import dataclass
from typing import Any

logger = logging.getLogger(__name__)


#: 场景里可能出现的物体：``(名称, 归一化 x, 归一化 y, 归一化宽, 归一化高, 颜色)``
SCENE_OBJECTS: list[tuple[str, float, float, float, float, tuple[int, int, int]]] = [
    ("红色马克杯", 0.18, 0.62, 0.11, 0.20, (196, 58, 48)),
    ("蓝色书本", 0.44, 0.72, 0.18, 0.07, (48, 96, 196)),
    ("绿色小球", 0.72, 0.70, 0.10, 0.16, (56, 168, 88)),
    ("棕色纸箱", 0.85, 0.55, 0.22, 0.34, (150, 112, 66)),
]


@dataclass(slots=True)
class SceneSpec:
    """一个可复现的场景描述。"""

    width: int = 640
    height: int = 480
    rotation_deg: float = 0.0
    """模拟机器人转头后视角的变化，会整体平移画面中的物体。"""

    label: str = "desk"
    """场景名，仅用于日志。"""


def _fallback_jpeg() -> bytes:
    """纯标准库生成的 8x8 JPEG。

    Pillow 不可用时的兜底：协议与工具链仍能跑通（帧能被抓到、能配对、
    能被回喂），只是图像本身没有内容。
    """
    width = height = 8
    quant = bytes([16] * 64)
    # 亮度分量的 DC 系数设为中等灰度，其余为 0。
    def block(dc: int) -> bytes:
        return bytes([dc]) + b"\x00" * 63

    entropy = block(0x28) + block(0x00) * (width * height // 64 - 1)

    out = io.BytesIO()
    out.write(b"\xff\xd8")  # SOI
    out.write(b"\xff\xdb" + struct.pack(">H", 67) + b"\x00" + quant)  # DQT
    out.write(
        b"\xff\xc0"
        + struct.pack(">H", 17)
        + bytes([8])
        + struct.pack(">HH", height, width)
        + bytes([3])
        + bytes([1, 0x11, 0]) + bytes([2, 0x11, 1]) + bytes([3, 0x11, 1])
    )  # SOF0
    out.write(b"\xff\xc4" + struct.pack(">H", 20) + bytes([0]) + bytes(range(16)))  # DHT (占位)
    out.write(b"\xff\xda" + struct.pack(">H", 8) + bytes([3, 1, 0, 2, 0x11, 3, 0x11]) + bytes([0, 63, 0]))
    out.write(entropy)
    out.write(b"\xff\xd9")  # EOI
    return out.getvalue()


class SceneRenderer:
    """把 :class:`SceneSpec` 渲染成 JPEG 字节。"""

    def __init__(self) -> None:
        self._pillow = self._try_import_pillow()
        if self._pillow is None:
            logger.warning("未安装 Pillow，模拟摄像头将输出占位图像（无法被视觉模型识别）")

    @staticmethod
    def _try_import_pillow() -> Any:
        """尝试导入 Pillow，失败返回 ``None``。"""
        try:
            from PIL import Image, ImageDraw

            return (Image, ImageDraw)
        except ImportError:
            return None

    def render(self, spec: SceneSpec, *, quality: int = 80) -> bytes:
        """渲染一帧。

        Args:
            spec: 场景参数。
            quality: JPEG 质量 1..100。
        """
        if self._pillow is None:
            return _fallback_jpeg()

        Image, ImageDraw = self._pillow
        width, height = max(64, spec.width), max(48, spec.height)
        image = Image.new("RGB", (width, height))
        draw = ImageDraw.Draw(image)

        # 1) 上深下浅的渐变背景，模拟桌面 + 墙面。
        for y in range(height):
            ratio = y / height
            # 上半部墙面偏冷，下半部桌面偏暖。
            if ratio < 0.65:
                color = (
                    int(52 + 18 * ratio),
                    int(62 + 20 * ratio),
                    int(78 + 22 * ratio),
                )
            else:
                t = (ratio - 0.65) / 0.35
                color = (
                    int(96 + 60 * t),
                    int(78 + 48 * t),
                    int(58 + 34 * t),
                )
            draw.line([(0, y), (width, y)], fill=color)

        # 2) 桌面边缘线，给场景一点结构感。
        horizon = int(height * 0.65)
        draw.line([(0, horizon), (width, horizon)], fill=(120, 104, 82), width=2)

        # 3) 物体：按转头角度整体水平平移，制造「视角变化」。
        shift = int(spec.rotation_deg / 360.0 * width)
        for name, nx, ny, nw, nh, rgb in SCENE_OBJECTS:
            cx = int((nx * width + shift) % (width + 200)) - 100
            cy = int(ny * height)
            half_w = max(3, int(nw * width / 2))
            half_h = max(3, int(nh * height / 2))

            box = [cx - half_w, cy - half_h, cx + half_w, cy + half_h]
            # 越界太多的物体直接跳过，避免画到画面边缘产生误导。
            if box[2] < 4 or box[0] > width - 4:
                continue

            draw.rectangle(box, fill=rgb, outline=tuple(min(255, c + 55) for c in rgb), width=2)
            # 高光，让物体更「立体」，模型更容易识别。
            highlight = [box[0] + 3, box[1] + 3, box[0] + half_w // 2 + 3, box[1] + half_h // 2 + 3]
            draw.rectangle(highlight, fill=tuple(min(255, c + 70) for c in rgb))

            # 在物体下方标注名称：真实机器人不需要，但调试时可读性极好。
            if width >= 320:
                text_y = min(height - 12, box[3] + 2)
                if text_y > 0:
                    draw.text((max(2, cx - half_w), text_y), name, fill=(236, 240, 245))

        # 4) 时间戳水印，便于确认抓到的不是缓存帧。
        import time

        stamp = time.strftime("%H:%M:%S")
        draw.text((6, 6), f"MOCK CAM  {width}x{height}  {stamp}", fill=(180, 200, 220))

        buffer = io.BytesIO()
        image.save(buffer, format="JPEG", quality=max(1, min(100, int(quality))), optimize=True)
        return buffer.getvalue()


def gradient_png(width: int = 32, height: int = 32) -> bytes:
    """纯标准库生成一张渐变 PNG，用于不需要内容的自检场景。"""
    raw = bytearray()
    for y in range(height):
        raw.append(0)  # filter type 0
        for x in range(width):
            hue = (x / width) * 0.8
            r, g, b = colorsys.hsv_to_rgb(hue, 0.55, 0.95 - 0.3 * (y / height))
            raw.extend((int(r * 255), int(g * 255), int(b * 255)))

    def chunk(tag: bytes, payload: bytes) -> bytes:
        return (
            struct.pack(">I", len(payload))
            + tag
            + payload
            + struct.pack(">I", zlib.crc32(tag + payload) & 0xFFFFFFFF)
        )

    header = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", header)
        + chunk(b"IDAT", zlib.compress(bytes(raw), 6))
        + chunk(b"IEND", b"")
    )


def encode_b64(data: bytes) -> str:
    """按协议要求做 base64 编码。"""
    return base64.b64encode(data).decode("ascii")


def speech_like_pcm(
    *,
    duration_s: float = 1.6,
    sample_rate: int = 16_000,
    seed: int = 7,
) -> bytes:
    """合成一段「像语音」的 16-bit PCM。

    真实麦克风波形不可复现，而测试需要**确定性的**输入。这里用若干个
    随时间变化的共振峰叠加，做出有能量起伏、有停顿的波形：
    足以让 ASR 走完真实流程，也足以让静音检测逻辑被正确触发。
    """
    import array

    total = int(max(0.1, duration_s) * sample_rate)
    samples = array.array("h")
    rng = seed

    for index in range(total):
        t = index / sample_rate

        # 用简单 LCG 做可复现的「随机」扰动，避免依赖 random 的全局状态。
        rng = (1103515245 * rng + 12345) & 0x7FFFFFFF
        jitter = ((rng >> 16) & 0xFF) / 255.0 - 0.5

        # 三个共振峰：基频 + 两个泛音，模拟人声频谱。
        value = (
            0.55 * math.sin(2 * math.pi * 128 * t)
            + 0.28 * math.sin(2 * math.pi * 320 * t)
            + 0.16 * math.sin(2 * math.pi * 780 * t)
            + 0.06 * jitter
        )

        # 音节包络：每 0.22 秒一个音节，之间有短停顿。
        syllable = (t % 0.22) / 0.22
        envelope = math.sin(math.pi * syllable) ** 1.5

        # 开头 0.15 秒渐进，结尾 0.25 秒渐出，模拟自然起止。
        if t < 0.15:
            envelope *= t / 0.15
        remaining = duration_s - t
        if remaining < 0.25:
            envelope *= max(0.0, remaining / 0.25)

        samples.append(int(max(-1.0, min(1.0, value * envelope)) * 12000))

    return samples.tobytes()
