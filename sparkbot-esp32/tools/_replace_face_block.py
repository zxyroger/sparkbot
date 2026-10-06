"""把 bot_hw_display.c 里的旧表情实现替换为调用 bot_face.c。

旧实现是"深色底 + 彩色描边五官"，新实现是"黄色圆角方块 + 黑色实心五官"。
替换范围：从 `/* 表情配色：...` 到 `draw_face_geometry` 函数结束（含）。

之所以写脚本而不是手改：这段有近 200 行，手抄容易漏。脚本按标记定位，
改完打印前后行号与校验信息。
"""

from __future__ import annotations

import io
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SRC = ROOT / "main" / "bot_hw_display.c"

NEW_BLOCK = '''/* 表情配色：仅供叠加文字与"当前表情"记录使用。
 *
 * 五官的绘制已移到 bot_face.c（黄色圆角方块风格），这里只保留
 * 文字颜色与默认背景 —— 换表情时背景由 bot_face_render 自己铺。 */
typedef struct {
    const char *name;
    uint16_t bg;
    uint16_t fg;
} face_style_t;

static const face_style_t FACE_STYLES[] = {
    {"neutral",   0x1082, 0xFFFF}, /* 文字用白色，底色由 bot_face 决定 */
    {"happy",     0x1082, 0xFFFF},
    {"sad",       0x1082, 0xFFFF},
    {"angry",     0x1082, 0xFFFF},
    {"surprised", 0x1082, 0xFFFF},
    {"sleepy",    0x1082, 0xFFFF},
    {"confused",  0x1082, 0xFFFF},
    {"thinking",  0x1082, 0xFFFF},
    {"love",      0x1082, 0xFFFF},
    {"excited",   0x1082, 0xFFFF},
    {"scared",    0x1082, 0xFFFF},
    {"bored",     0x1082, 0xFFFF},
};

static const face_style_t *find_style(const char *emotion)
{
    if (emotion == NULL) {
        return &FACE_STYLES[0];
    }
    for (size_t i = 0; i < sizeof(FACE_STYLES) / sizeof(FACE_STYLES[0]); i++) {
        if (strcmp(FACE_STYLES[i].name, emotion) == 0) {
            return &FACE_STYLES[i];
        }
    }
    return &FACE_STYLES[0]; /* 未知表情 → neutral，与 PC 端约定一致 */
}
'''


def main() -> int:
    """执行替换。"""
    src = io.open(SRC, encoding="utf-8").read()

    # 起点：表情配色注释
    start_marker = "/* 表情配色：每种情绪一组前景/背景色 */"
    # 终点：draw_face_geometry 结束（下一个空行 + esp_err_t bot_display_show_face）
    end_marker = "esp_err_t bot_display_show_face(const char *emotion, float intensity)"

    i = src.find(start_marker)
    j = src.find(end_marker)
    if i < 0 or j < 0:
        print(f"找不到标记: start={i} end={j}")
        return 1
    if j <= i:
        print("标记顺序不对")
        return 1

    before = src[:i]
    after = src[j:]
    out = before + NEW_BLOCK + "\n" + after

    io.open(SRC, "w", encoding="utf-8", newline="\n").write(out)

    # 校验
    n_old = len(src[i:j].split("\n"))
    n_new = len(NEW_BLOCK.split("\n"))
    print(f"已替换 {n_old} 行 → {n_new} 行")
    print(f"文件: {len(src.split(chr(10)))} 行 → {len(out.split(chr(10)))} 行")
    for probe in ("draw_face_geometry", "DRAW_EYE", "bot_face_render"):
        print(f"  含 {probe!r}: {probe in out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
