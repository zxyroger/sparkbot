"""预览固件表情效果（不烧录也能看）。

原理：用 PIL 按 ``main/bot_face.c`` 里的**同一套几何参数**重画一遍，
输出一张 12 种表情的对照图。这样调参时不必反复编译烧录。

**它只是预览**：真正的显示由固件绘制，两者如果调参不一致会看不出差别，
所以改 ``bot_face.c`` 的比例后要同步改这里的常量。

用法::

    python tools/preview_faces.py                 # 输出到 artifacts/faces_preview.png
    python tools/preview_faces.py -o out.png
"""

from __future__ import annotations

import argparse
from pathlib import Path

from PIL import Image, ImageDraw

# --- 与 bot_face.c 保持一致的颜色 --------------------------------------- #
FACE_BG = (16, 16, 16)
FACE_YELLOW = (255, 222, 44)
FACE_YELLOW_D = (228, 160, 0)
FACE_BLACK = (0, 0, 0)
FACE_WHITE = (255, 255, 255)

#: 屏幕尺寸（与固件一致：ILI9341 320x240）
SCREEN_W, SCREEN_H = 320, 240

#: 固件支持的全部表情（见 bot_hw_display.c 的 FACE_STYLES）
EMOTIONS = [
    "neutral", "happy", "sad", "angry",
    "surprised", "sleepy", "confused", "thinking",
    "love", "excited", "scared", "bored",
]


# --------------------------------------------------------------------------- #
# 基础图元（对应 bot_face.c 里的 f_* 函数）
# --------------------------------------------------------------------------- #
def f_circle(d: ImageDraw.ImageDraw, cx: int, cy: int, r: int, color) -> None:
    """实心圆。"""
    if r <= 0:
        return
    d.ellipse([cx - r, cy - r, cx + r, cy + r], fill=color)


def f_ellipse(d: ImageDraw.ImageDraw, cx: int, cy: int, rx: int, ry: int, color) -> None:
    """实心椭圆。"""
    if rx <= 0 or ry <= 0:
        return
    d.ellipse([cx - rx, cy - ry, cx + rx, cy + ry], fill=color)


def f_rounded_rect(d: ImageDraw.ImageDraw, x: int, y: int, w: int, h: int,
                   r: int, color, bg) -> None:
    """实心圆角矩形（与固件一致的"逐行清角"实现）。

    关键：挖角只能用**四分之一圆**。早先用整圆挖，圆的内侧一半也把颜色
    吃掉了，屏幕上就是"四角各一个圆点"而不是圆角。
    """
    if w <= 0 or h <= 0:
        return
    d.rectangle([x, y, x + w - 1, y + h - 1], fill=color)
    r = max(0, r)
    r = min(r, w // 2, h // 2)
    if r <= 0:
        return

    for i in range(r):
        dy = r - i
        t = 1.0 - (dy * dy) / (r * r)
        span = int(r * (t ** 0.5)) if t > 0 else 0
        cut = r - span
        if cut <= 0:
            continue
        d.rectangle([x, y + i, x + cut - 1, y + i], fill=bg)
        d.rectangle([x + w - cut, y + i, x + w - 1, y + i], fill=bg)
        d.rectangle([x, y + h - 1 - i, x + cut - 1, y + h - 1 - i], fill=bg)
        d.rectangle([x + w - cut, y + h - 1 - i, x + w - 1, y + h - 1 - i], fill=bg)


def f_thick_line(d: ImageDraw.ImageDraw, x0: int, y0: int, x1: int, y1: int,
                 thickness: int, color) -> None:
    """有宽度的圆头线段。"""
    d.line([x0, y0, x1, y1], fill=color, width=max(1, thickness))
    r = thickness // 2
    f_circle(d, x0, y0, r, color)
    f_circle(d, x1, y1, r, color)


def f_semicircle_up(d: ImageDraw.ImageDraw, cx: int, cy: int, r: int, color) -> None:
    """上半圆（碗口朝上）。"""
    if r <= 0:
        return
    d.pieslice([cx - r, cy - r, cx + r, cy + r], start=0, end=180, fill=color)


def f_semicircle_down(d: ImageDraw.ImageDraw, cx: int, cy: int, r: int, color) -> None:
    """下半圆（碗口朝下）。"""
    if r <= 0:
        return
    d.pieslice([cx - r, cy - r, cx + r, cy + r], start=180, end=360, fill=color)


# --------------------------------------------------------------------------- #
# 五官
# --------------------------------------------------------------------------- #
def f_eye_open(d: ImageDraw.ImageDraw, cx: int, cy: int, r: int) -> None:
    """黑眼珠 + 左上白色高光。"""
    f_ellipse(d, cx, cy, r, int(r * 1.1), FACE_BLACK)
    hr = max(2, r // 3)
    f_circle(d, cx - r // 3, cy - r // 3, hr, FACE_WHITE)


def f_eye_arc(d: ImageDraw.ImageDraw, cx: int, cy: int, r: int) -> None:
    """笑眼（向上弯的弧）。"""
    t = r // 2 + 1
    f_thick_line(d, cx - r, cy, cx, cy - int(r * 0.95), t, FACE_BLACK)
    f_thick_line(d, cx, cy - int(r * 0.95), cx + r, cy, t, FACE_BLACK)


def f_eye_closed(d: ImageDraw.ImageDraw, cx: int, cy: int, r: int) -> None:
    """闭眼（横线）。"""
    f_thick_line(d, cx - r, cy, cx + r, cy, r // 2 + 1, FACE_BLACK)


def f_eye_star(d: ImageDraw.ImageDraw, cx: int, cy: int, r: int) -> None:
    """四角星眼。"""
    f_thick_line(d, cx - r, cy, cx + r, cy, r // 3 + 1, FACE_BLACK)
    f_thick_line(d, cx, cy - r, cx, cy + r, r // 3 + 1, FACE_BLACK)
    dd = int(r * 0.62)
    f_thick_line(d, cx - dd, cy - dd, cx + dd, cy + dd, r // 4 + 1, FACE_BLACK)
    f_thick_line(d, cx - dd, cy + dd, cx + dd, cy - dd, r // 4 + 1, FACE_BLACK)


def f_eye_heart(d: ImageDraw.ImageDraw, cx: int, cy: int, r: int) -> None:
    """爱心眼。"""
    rr = max(2, int(r * 0.52))
    f_circle(d, cx - rr // 2, cy - rr // 4, rr, FACE_BLACK)
    f_circle(d, cx + rr // 2, cy - rr // 4, rr, FACE_BLACK)
    for i in range(rr + r // 2 + 1):
        w = r - i // 2
        if w <= 0:
            break
        d.rectangle([cx - w // 2, cy + rr // 4 + i,
                     cx - w // 2 + w - 1, cy + rr // 4 + i], fill=FACE_BLACK)


def f_brow_angry(d: ImageDraw.ImageDraw, cx: int, cy: int, dx: int,
                 length: int, drop: int) -> None:
    """生气的八字眉。"""
    f_thick_line(d, cx - dx - length, cy - drop, cx - dx + length, cy + drop, 5, FACE_BLACK)
    f_thick_line(d, cx + dx + length, cy - drop, cx + dx - length, cy + drop, 5, FACE_BLACK)


# --------------------------------------------------------------------------- #
# 单个表情
# --------------------------------------------------------------------------- #
def draw_emotion(d: ImageDraw.ImageDraw, emotion: str, intensity: float = 1.0) -> None:
    """在 320x240 的画布上画一个表情（与固件 draw_emotion 对应）。

    脸是**矩形**，铺满屏幕（拉伸），与参考图的正方形不同 —— 见 bot_face.c
    的说明：屏幕是 4:3，正方形与"铺满"不可兼得，这里按铺满来做。
    """
    cx, cy = SCREEN_W // 2, SCREEN_H // 2

    d.rectangle([0, 0, SCREEN_W - 1, SCREEN_H - 1], fill=FACE_BG)

    # intensity 只做很小的缩放，否则"铺满"就名不副实
    k = 0.97 + 0.03 * intensity
    fw = min(SCREEN_W, int(SCREEN_W * k))
    fh = min(SCREEN_H, int(SCREEN_H * k))
    x, y = cx - fw // 2, cy - fh // 2
    # 圆角按短边取，否则长边上的角会显得过大
    corner = min(fw, fh) // 8

    f_rounded_rect(d, x, y, fw, fh, corner, FACE_YELLOW, FACE_BG)

    # 水平量按宽、垂直量按高、半径按短边 —— 拉伸后布局才均匀，
    # 且眼睛仍是正圆而不是被压扁
    ref = min(fw, fh)
    eye_dx = int(fw * 0.235)
    eye_cy = int(fh * -0.17)
    r = int(ref * 0.115)
    mouth_cy = int(fh * 0.19)
    mouth_w = int(ref * 0.32)

    lx, rx = cx - eye_dx, cx + eye_dx
    ecy = cy + eye_cy
    mcy = cy + mouth_cy

    if emotion in ("sleepy", "bored"):
        f_eye_closed(d, lx, ecy, r)
        f_eye_closed(d, rx, ecy, r)
        f_ellipse(d, cx, mcy, mouth_w // 3, mouth_w // 2, FACE_BLACK)
        return

    if emotion in ("surprised", "scared"):
        f_eye_open(d, lx, ecy, int(r * 1.15))
        f_eye_open(d, rx, ecy, int(r * 1.15))
        f_ellipse(d, cx, mcy, int(mouth_w * 0.42), int(mouth_w * 0.62), FACE_BLACK)
        return

    if emotion == "angry":
        f_brow_angry(d, cx, ecy - r - 8, eye_dx, int(r * 0.95), 6)
        f_eye_open(d, lx, ecy, int(r * 0.95))
        f_eye_open(d, rx, ecy, int(r * 0.95))
        f_semicircle_down(d, cx, mcy + int(mouth_w * 0.30), int(mouth_w * 0.72), FACE_BLACK)
        return

    if emotion == "sad":
        f_eye_open(d, lx, ecy, r)
        f_eye_open(d, rx, ecy, r)
        f_semicircle_down(d, cx, mcy + int(mouth_w * 0.26), int(mouth_w * 0.62), FACE_BLACK)
        return

    if emotion == "love":
        f_eye_heart(d, lx, ecy, r)
        f_eye_heart(d, rx, ecy, r)
        f_semicircle_up(d, cx, mcy - int(mouth_w * 0.2), int(mouth_w * 0.55), FACE_BLACK)
        return

    if emotion == "excited":
        f_eye_star(d, lx, ecy, int(r * 1.1))
        f_eye_star(d, rx, ecy, int(r * 1.1))
        f_semicircle_up(d, cx, mcy - int(mouth_w * 0.25), int(mouth_w * 0.78), FACE_BLACK)
        return

    if emotion == "thinking":
        f_eye_open(d, lx, ecy + 3, r)
        f_eye_closed(d, rx, ecy - 2, r)
        f_thick_line(d, cx - mouth_w // 2, mcy + 4, cx + mouth_w // 4, mcy + 4, 5, FACE_BLACK)
        return

    if emotion == "confused":
        f_eye_open(d, lx, ecy, r)
        f_eye_open(d, rx, ecy, r)
        f_thick_line(d, cx - mouth_w // 2, mcy + 6, cx + mouth_w // 2, mcy - 2, 5, FACE_BLACK)
        return

    if emotion == "happy":
        f_eye_arc(d, lx, ecy + r // 2, r)
        f_eye_arc(d, rx, ecy + r // 2, r)
        f_semicircle_up(d, cx, mcy, int(mouth_w * 0.72), FACE_BLACK)
        return

    # neutral 及未列出
    f_eye_open(d, lx, ecy, r)
    f_eye_open(d, rx, ecy, r)
    f_thick_line(d, cx - mouth_w // 2, mcy, cx + mouth_w // 2, mcy, 5, FACE_BLACK)


# --------------------------------------------------------------------------- #
# 出图
# --------------------------------------------------------------------------- #
def main() -> int:
    """生成对照图。"""
    p = argparse.ArgumentParser(description="预览固件表情")
    p.add_argument("-o", "--out", default="artifacts/faces_preview.png")
    p.add_argument("--scale", type=float, default=1.0, help="整体缩放")
    p.add_argument("--cols", type=int, default=4)
    args = p.parse_args()

    cols = args.cols
    rows = (len(EMOTIONS) + cols - 1) // cols
    gap = 12
    tile_w, tile_h = SCREEN_W, SCREEN_H
    scale = args.scale

    out_w = int(cols * tile_w + (cols + 1) * gap)
    out_h = int(rows * (tile_h + 26) + (rows + 1) * gap)
    canvas = Image.new("RGB", (out_w, out_h), (30, 30, 34))
    draw = ImageDraw.Draw(canvas)

    for i, emo in enumerate(EMOTIONS):
        r, c = divmod(i, cols)
        ox = int(gap + c * (tile_w + gap))
        oy = int(gap + r * (tile_h + 26 + gap))

        tile = Image.new("RGB", (tile_w, tile_h), FACE_BG)
        td = ImageDraw.Draw(tile)
        draw_emotion(td, emo, 1.0)
        canvas.paste(tile, (ox, oy))
        draw.text((ox + 6, oy + tile_h + 4), emo, fill=(200, 200, 210))

    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    if scale != 1.0:
        canvas = canvas.resize(
            (int(out_w * scale), int(out_h * scale)), Image.LANCZOS
        )
    canvas.save(out)
    print(f"已生成 {out}  ({canvas.width}x{canvas.height}, {len(EMOTIONS)} 种表情)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
