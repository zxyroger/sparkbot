/*
 * 表情绘制：黄色圆角方块 + 黑色五官的 emoji 风格。
 *
 * 为什么仍然是**几何绘制**而不是位图资源：
 *   * 不占 Flash —— 12 种表情的位图按 200x200 RGB565 算要 480KB；
 *   * intensity 可以直接映射成五官尺寸，做出"轻微/强烈"的差别；
 *   * 调参一眼能看懂，不需要重新导出素材。
 * 固件也确实没有图像解码器（bot_display_show_image 明确返回不支持），
 * 所以几何路线与现状一致。
 *
 * 视觉规范（对齐参考图）：
 *   * 外层背景：深灰，让黄色方块跳出来；
 *   * 脸部：圆角方块，黄色 0xFDC0（暖黄，接近参考图）；
 *   * 五官：纯黑 0x0000，实心；眼睛带左上白色高光，显得有神；
 *   * 方块尺寸随 intensity 轻微缩放，产生"情绪强弱"的观感。
 *
 * 屏幕坐标以正方形脸为中心，四周留边；只要屏幕不发生极端长宽比，
 * 这个布局都成立。
 */

#include <math.h>
#include <string.h>

#include "bot_hw_display.h"

/* ------------------------------------------------------------------ */
/* 配色                                                               */
/* ------------------------------------------------------------------ */

/*
 * 配色。
 *
 * 香蕉黄 = 高红 + 高绿 + 极低蓝，且绿色分量要够高才不会偏橙。
 * 换算：R=255/255 -> 31；G=222/255 -> 55；B=44/255 -> 5
 *       RGB565 = (31<<11) | (55<<5) | 5 = 0xFEE5
 * 屏幕反解回来约 (255, 222, 41)，是标准香蕉黄。
 * 之前用 0xFDC0（反解约 (255,186,0)）绿色分量偏低，所以偏橙。
 */
#define FACE_BG        0x1082  /* 外层背景：近黑深灰 */
#define FACE_YELLOW    0xFEE5  /* 脸的黄：香蕉黄 */
#define FACE_YELLOW_D  0xE4A0  /* 脸的暗边（当前未使用，保留备用） */
#define FACE_BLACK     0x0000  /* 五官：纯黑 */
#define FACE_WHITE     0xFFFF  /* 眼睛高光 */

/* ------------------------------------------------------------------ */
/* 基础图元                                                           */
/* ------------------------------------------------------------------ */

/* 实心圆（带半径保护，半径 <=0 时不画） */
static void f_circle(int cx, int cy, int r, uint16_t color)
{
    if (r <= 0) {
        return;
    }
    bot_display_fill_circle(cx, cy, r, color);
}

/*
 * 实心椭圆。
 *
 * bot_display_fill_circle 只能画正圆，而参考图里的眼睛有横向压扁的
 * 形状（例如眯眼），所以这里按扫描线自己算宽度。
 */
static void f_ellipse(int cx, int cy, int rx, int ry, uint16_t color)
{
    if (rx <= 0 || ry <= 0) {
        return;
    }
    for (int dy = -ry; dy <= ry; dy++) {
        float t = (float)(dy * dy) / (float)(ry * ry);
        if (t >= 1.0f) {
            continue;
        }
        int span = (int)((float)rx * sqrtf(1.0f - t));
        if (span < 0) {
            continue;
        }
        bot_display_fill_rect(cx - span, cy + dy, span * 2 + 1, 1, color);
    }
}

/*
 * 实心圆角矩形。
 *
 * 关键点：挖角只能用**四分之一圆**，不能用整圆。
 *
 * 踩过的坑：早先用整圆（`f_circle`）在四角挖，结果圆的内侧一半也把
 * 黄色吃掉了 —— 屏幕上看到的是"四角各一个明显的圆点"，而不是圆角。
 * 实测数据：洞在 y=15 时宽 x72~80，y=36（圆心处）最宽到 x97，
 * 这正是整圆剖面。
 *
 * 做法：逐行只清除该行**落在角的圆外**的那一小段（左/右各一段），
 * 等价于画一个四分之一圆，且不用判断象限。
 */
static void f_rounded_rect(int x, int y, int w, int h, int r, uint16_t color,
                           uint16_t bg)
{
    if (w <= 0 || h <= 0) {
        return;
    }
    bot_display_fill_rect(x, y, w, h, color);
    if (r <= 0) {
        return; /* 直角 */
    }
    if (r > w / 2) {
        r = w / 2;
    }
    if (r > h / 2) {
        r = h / 2;
    }

    /*
     * 四个角各处理 r 行。对每一行，算出该行上圆的半宽 span，
     * 则"角外"的宽度是 r - span（只清这一段，不清圆内部分）。
     */
    for (int i = 0; i < r; i++) {
        int dy = r - i;                       /* 距圆心的行距，从 r 递减到 1 */
        float t = 1.0f - (float)(dy * dy) / (float)(r * r);
        int span = (t > 0.0f) ? (int)((float)r * sqrtf(t)) : 0;
        int cut = r - span;                   /* 该行需要清掉的宽度 */
        if (cut <= 0) {
            continue;
        }
        /* 上左、上右 */
        bot_display_fill_rect(x, y + i, cut, 1, bg);
        bot_display_fill_rect(x + w - cut, y + i, cut, 1, bg);
        /* 下左、下右 */
        bot_display_fill_rect(x, y + h - 1 - i, cut, 1, bg);
        bot_display_fill_rect(x + w - cut, y + h - 1 - i, cut, 1, bg);
    }
}

/* 上半圆（碗口朝上）——用于"大笑"的嘴 */
static void f_semicircle_up(int cx, int cy, int r, uint16_t color)
{
    if (r <= 0) {
        return;
    }
    for (int dy = 0; dy <= r; dy++) {
        float t = (float)(dy * dy) / (float)(r * r);
        if (t >= 1.0f) {
            continue;
        }
        int span = (int)((float)r * sqrtf(1.0f - t));
        if (span < 0) {
            continue;
        }
        bot_display_fill_rect(cx - span, cy + dy, span * 2 + 1, 1, color);
    }
}

/* 下半圆（碗口朝下）——用于"难过"的嘴 */
static void f_semicircle_down(int cx, int cy, int r, uint16_t color)
{
    if (r <= 0) {
        return;
    }
    for (int dy = 0; dy >= -r; dy--) {
        float t = (float)(dy * dy) / (float)(r * r);
        if (t >= 1.0f) {
            continue;
        }
        int span = (int)((float)r * sqrtf(1.0f - t));
        if (span < 0) {
            continue;
        }
        bot_display_fill_rect(cx - span, cy + dy, span * 2 + 1, 1, color);
    }
}

/* 粗线：把一条线画成有宽度的圆头线段（描边类五官用） */
static void f_thick_line(int x0, int y0, int x1, int y1, int thickness, uint16_t color)
{
    int steps = abs(x1 - x0);
    if (abs(y1 - y0) > steps) {
        steps = abs(y1 - y0);
    }
    if (steps == 0) {
        f_circle(x0, y0, thickness / 2, color);
        return;
    }
    int r = thickness / 2;
    for (int i = 0; i <= steps; i++) {
        int x = x0 + (x1 - x0) * i / steps;
        int y = y0 + (y1 - y0) * i / steps;
        f_circle(x, y, r, color);
    }
}

/* ------------------------------------------------------------------ */
/* 五官                                                               */
/* ------------------------------------------------------------------ */

/*
 * 眼睛：黑色实心 + 左上白色高光。
 *
 * 高光是"有神"的关键 —— 纯黑圆点看着呆滞，加一个小白点立刻活起来，
 * 成本只是一个额外的圆。
 */
static void f_eye_open(int cx, int cy, int r)
{
    f_ellipse(cx, cy, r, (int)(r * 1.1f), FACE_BLACK);
    int hr = r / 3;
    if (hr < 2) {
        hr = 2;
    }
    f_circle(cx - r / 3, cy - r / 3, hr, FACE_WHITE);
}

/* 眯眼/笑眼：向上弯的一道粗弧 */
static void f_eye_arc(int cx, int cy, int r)
{
    f_thick_line(cx - r, cy, cx, cy - (int)(r * 0.95f), r / 2 + 1, FACE_BLACK);
    f_thick_line(cx, cy - (int)(r * 0.95f), cx + r, cy, r / 2 + 1, FACE_BLACK);
}

/* 闭眼：一条横线（困倦、无聊） */
static void f_eye_closed(int cx, int cy, int r)
{
    f_thick_line(cx - r, cy, cx + r, cy, r / 2 + 1, FACE_BLACK);
}

/* 星形眼：用四个方向的尖角拼一个四角星（兴奋） */
static void f_eye_star(int cx, int cy, int r)
{
    f_thick_line(cx - r, cy, cx + r, cy, r / 3 + 1, FACE_BLACK);
    f_thick_line(cx, cy - r, cx, cy + r, r / 3 + 1, FACE_BLACK);
    int d = (int)(r * 0.62f);
    f_thick_line(cx - d, cy - d, cx + d, cy + d, r / 4 + 1, FACE_BLACK);
    f_thick_line(cx - d, cy + d, cx + d, cy - d, r / 4 + 1, FACE_BLACK);
}

/* 爱心眼：两个圆 + 一个三角，用粗线近似 */
static void f_eye_heart(int cx, int cy, int r)
{
    int rr = (int)(r * 0.52f);
    f_circle(cx - rr / 2, cy - rr / 4, rr, FACE_BLACK);
    f_circle(cx + rr / 2, cy - rr / 4, rr, FACE_BLACK);
    /* 下半部用逐渐收窄的横线拼成尖角 */
    for (int i = 0; i <= rr + r / 2; i++) {
        int w = r - i / 2;
        if (w <= 0) {
            break;
        }
        bot_display_fill_rect(cx - w / 2, cy + rr / 4 + i, w, 1, FACE_BLACK);
    }
}

/* 恼怒的眉：向内下压的两道粗线 */
static void f_brow_angry(int cx, int cy, int dx, int len, int drop)
{
    /* 左眉：从左下往右上（内端高）——改成内端低，形成"八"字压迫感 */
    f_thick_line(cx - dx - len, cy - drop, cx - dx + len, cy + drop, 5, FACE_BLACK);
    f_thick_line(cx + dx + len, cy - drop, cx + dx - len, cy + drop, 5, FACE_BLACK);
}

/* ------------------------------------------------------------------ */
/* 单个表情的绘制                                                     */
/* ------------------------------------------------------------------ */

/* 几何参数：以脸的中心为原点算，尺寸随 intensity 缩放。
 *
 * 宽高**分开**：脸是矩形（铺满屏幕），五官的水平位置按宽度算、
 * 垂直位置按高度算，这样拉伸时布局是均匀的，而不是把圆眼压成扁椭圆。
 * 数值型的尺寸（眼睛半径、嘴宽）统一按 ref 取，保证等比。 */
typedef struct {
    int cx;
    int cy;
    int fw;         /* 脸宽 */
    int fh;         /* 脸高 */
    int ref;        /* 特征尺寸的基准 = min(fw, fh) */
    int eye_dx;     /* 两眼中心到脸中心水平距离 */
    int eye_cy;     /* 眼睛中心相对脸中心的垂直偏移（负=偏上） */
    int eye_r;      /* 眼睛基础半径 */
    int mouth_cy;   /* 嘴中心相对脸中心的垂直偏移 */
    int mouth_w;    /* 嘴宽度半值 */
    int corner;     /* 圆角半径 */
} face_geo_t;

static void draw_emotion(const char *emotion, float intensity, const face_geo_t *g)
{
    const int ecx_l = g->cx - g->eye_dx;  /* 左眼中心 x */
    const int ecx_r = g->cx + g->eye_dx;
    const int ecy = g->cy + g->eye_cy;
    const int r = g->eye_r;
    const int mcy = g->cy + g->mouth_cy;

    /* ---- 无精打采 / 困 ---- */
    if (strcmp(emotion, "sleepy") == 0 || strcmp(emotion, "bored") == 0) {
        f_eye_closed(ecx_l, ecy, r);
        f_eye_closed(ecx_r, ecy, r);
        /* 小圆嘴（打哈欠的雏形） */
        f_ellipse(g->cx, mcy, g->mouth_w / 3, g->mouth_w / 2, FACE_BLACK);
        return;
    }

    /* ---- 惊讶 / 害怕 ---- */
    if (strcmp(emotion, "surprised") == 0 || strcmp(emotion, "scared") == 0) {
        f_eye_open(ecx_l, ecy, (int)(r * 1.15f));
        f_eye_open(ecx_r, ecy, (int)(r * 1.15f));
        /* 竖直椭圆嘴：张嘴 */
        f_ellipse(g->cx, mcy, (int)(g->mouth_w * 0.42f), (int)(g->mouth_w * 0.62f),
                  FACE_BLACK);
        return;
    }

    /* ---- 生气 ---- */
    if (strcmp(emotion, "angry") == 0) {
        f_brow_angry(g->cx, ecy - r - 8, g->eye_dx, (int)(r * 0.95f), 6);
        f_eye_open(ecx_l, ecy, (int)(r * 0.95f));
        f_eye_open(ecx_r, ecy, (int)(r * 0.95f));
        /* 向下弯的嘴 */
        f_semicircle_down(g->cx, mcy + (int)(g->mouth_w * 0.30f),
                          (int)(g->mouth_w * 0.72f), FACE_BLACK);
        return;
    }

    /* ---- 难过 ---- */
    if (strcmp(emotion, "sad") == 0) {
        f_eye_open(ecx_l, ecy, r);
        f_eye_open(ecx_r, ecy, r);
        f_semicircle_down(g->cx, mcy + (int)(g->mouth_w * 0.26f),
                          (int)(g->mouth_w * 0.62f), FACE_BLACK);
        return;
    }

    /* ---- 爱心 ---- */
    if (strcmp(emotion, "love") == 0) {
        f_eye_heart(ecx_l, ecy, r);
        f_eye_heart(ecx_r, ecy, r);
        /* 微笑 */
        f_semicircle_up(g->cx, mcy - (int)(g->mouth_w * 0.2f),
                        (int)(g->mouth_w * 0.55f), FACE_BLACK);
        return;
    }

    /* ---- 兴奋 ---- */
    if (strcmp(emotion, "excited") == 0) {
        f_eye_star(ecx_l, ecy, (int)(r * 1.1f));
        f_eye_star(ecx_r, ecy, (int)(r * 1.1f));
        f_semicircle_up(g->cx, mcy - (int)(g->mouth_w * 0.25f),
                        (int)(g->mouth_w * 0.78f), FACE_BLACK);
        return;
    }

    /* ---- 思考 ---- */
    if (strcmp(emotion, "thinking") == 0) {
        /* 一只正常眼、一只眯眼，一边高一边低，像在琢磨 */
        f_eye_open(ecx_l, ecy + 3, r);
        f_eye_closed(ecx_r, ecy - 2, r);
        /* 一条短直线代表抿着的嘴 */
        f_thick_line(g->cx - g->mouth_w / 2, mcy + 4, g->cx + g->mouth_w / 4, mcy + 4,
                     5, FACE_BLACK);
        return;
    }

    /* ---- 困惑 ---- */
    if (strcmp(emotion, "confused") == 0) {
        f_eye_open(ecx_l, ecy, r);
        f_eye_open(ecx_r, ecy, r);
        /* 斜嘴：表示"说不上来" */
        f_thick_line(g->cx - g->mouth_w / 2, mcy + 6, g->cx + g->mouth_w / 2, mcy - 2,
                     5, FACE_BLACK);
        return;
    }

    /* ---- 开心 ---- */
    if (strcmp(emotion, "happy") == 0) {
        /* 笑眼 + 碗形大笑嘴。
         *
         * 嘴用**向下**的半圆（`f_semicircle_down` 画的是圆心下方那半个圆，
         * 视觉上是碗口朝上的"碗形"），圆心就在 mouth_cy，弧朝下展开 ——
         * 这样嘴的顶部正好落在嘴中心附近，不会像向上半圆那样侵入眼睛。 */
        f_eye_arc(ecx_l, ecy + r / 2, r);
        f_eye_arc(ecx_r, ecy + r / 2, r);
        f_semicircle_up(g->cx, mcy, (int)(g->mouth_w * 0.72f), FACE_BLACK);
        return;
    }

    /* neutral 以及所有未列出的表情：圆眼 + 平嘴，最不容易画错 */
    f_eye_open(ecx_l, ecy, r);
    f_eye_open(ecx_r, ecy, r);
    f_thick_line(g->cx - g->mouth_w / 2, mcy, g->cx + g->mouth_w / 2, mcy, 5,
                 FACE_BLACK);
}

/* ------------------------------------------------------------------ */
/* 对外入口                                                           */
/* ------------------------------------------------------------------ */

void bot_face_render(const char *emotion, float intensity)
{
    if (!bot_display_ready()) {
        return;
    }

    if (emotion == NULL || emotion[0] == '\0') {
        emotion = "neutral";
    }
    if (intensity < 0.0f) {
        intensity = 0.0f;
    }
    if (intensity > 1.0f) {
        intensity = 1.0f;
    }

    const int w = bot_display_width();
    const int h = bot_display_height();
    const int cx = w / 2;
    const int cy = h / 2;

    /* 外层背景先铺满（矩形脸留不出边时这层其实会被盖住，留着无害） */
    bot_display_fill(FACE_BG);

    /*
     * 脸是**矩形，铺满整个屏幕**。
     *
     * 屏幕 320x240（4:3），参考图的脸是正方形，两者不能同时满足。
     * 这里按"拉伸成矩形"来做：黄色方块铺满，五官的水平位置按宽度算、
     * 垂直位置按高度算，所以布局是均匀拉伸的，而不是把眼睛压扁 ——
     * 眼睛仍按 min(w,h) 取半径，保持正圆。
     *
     * intensity 只做很小的缩放（0.97~1.0），否则"铺满"就名不副实了。
     */
    float k = 0.97f + 0.03f * intensity;
    int fw = (int)(w * k);
    int fh = (int)(h * k);
    if (fw > w) {
        fw = w;
    }
    if (fh > h) {
        fh = h;
    }
    int x = cx - fw / 2;
    int y = cy - fh / 2;

    /* 圆角按**短边**取，否则长边上会显得角过大 */
    int corner = ((fw < fh) ? fw : fh) / 8;

    /*
     * 只画一层圆角矩形。
     *
     * 试过在顶部加一条"高光"（用略深的黄），但两色差太小，实际观感是
     * 一条突兀的暗色横线，而不是高光。参考图本身就是纯平色块，
     * 所以这里保持干净。
     */
    f_rounded_rect(x, y, fw, fh, corner, FACE_YELLOW, FACE_BG);

    /*
     * 五官参数。
     *
     * 所有**尺寸类**的量（半径、嘴宽）都按 ref = min(fw, fh) 算，
     * 不能按 fw 算 —— 踩过一次：脸拉成 320x240 后按宽度取半径，
     * 嘴变成 115px 宽，撑满整个下半张脸，"happy" 的弧甚至高过眼睛。
     *
     * 只有**位置类**的量按各自方向取：水平位置跟宽度走、垂直位置跟
     * 高度走，这样拉伸后五官分布才是均匀的，而不是挤在中间一小块。
     */
    int ref = (fw < fh) ? fw : fh;
    face_geo_t g;
    g.cx = cx;
    g.cy = cy;
    g.fw = fw;
    g.fh = fh;
    g.ref = ref;
    g.eye_dx = (int)(fw * 0.235f);    /* 水平位置：按宽 */
    g.eye_cy = (int)(fh * -0.17f);    /* 垂直位置：按高 */
    g.eye_r = (int)(ref * 0.115f);    /* 半径：按短边，保证正圆 */
    g.mouth_cy = (int)(fh * 0.19f);   /* 嘴中心垂直位置 */
    g.mouth_w = (int)(ref * 0.32f);   /* 嘴宽：按短边 */
    g.corner = corner;

    draw_emotion(emotion, intensity, &g);
}
