/*
 * 显示实现：ILI9341/ILI9342C + 表情与文字绘制
 *
 * 帧缓冲策略：整屏 RGB565 帧缓冲放在 **PSRAM**（320x240x2 = 150KB）。
 *
 *   设计时考虑过"行缓冲 + 直接推屏"以省内存，但表情绘制里有圆和
 *   折线，需要跨行读改写（例如给圆填充时的边界判断），纯行缓冲会
 *   让每个绘制函数都要处理分段回写，复杂度急剧上升。
 *   150KB 放 PSRAM 对 ESP32-S3-N16R8（8MB PSRAM）完全可接受，
 *   而 PSRAM 的带宽对 320x240@几十 Hz 的局部刷新也够用。
 *   代价是绘制时写 PSRAM 比写内部 RAM 慢，但对"画表情"这种
 *   低频（每秒几次）的负载无影响。
 *
 * 脏矩形：每次绘制都会扩大脏矩形，bot_display_poll() 只把脏矩形
 * 那一块推给面板。画一个小表情只传几 KB，而不是整屏 150KB。
 */

#include "bot_hw_display.h"

#include <math.h>
#include <stdlib.h>
#include <string.h>

#include "driver/gpio.h"
#include "driver/ledc.h"
#include "driver/spi_master.h"
#include "esp_heap_caps.h"
#include "esp_lcd_ili9341.h"
#include "esp_lcd_panel_io.h"
#include "esp_lcd_panel_ops.h"
#include "esp_lcd_panel_vendor.h"
#include "esp_log.h"
#include "esp_timer.h"
#include "freertos/FreeRTOS.h"
#include "freertos/semphr.h"

#include "bot_font.h"

static const char *TAG = "bot_disp";

/* 背光 LEDC（与电机的定时器分开，各占一路） */
#define BL_LEDC_MODE  LEDC_LOW_SPEED_MODE
#define BL_LEDC_TIMER LEDC_TIMER_0
#define BL_LEDC_CH    LEDC_CHANNEL_2
#define BL_LEDC_RES   LEDC_TIMER_10_BIT
#define BL_LEDC_MAX   ((1 << 10) - 1)

typedef struct {
    bool ready;
    int width;
    int height;
    uint16_t *fb;             /* RGB565 帧缓冲（PSRAM） */
    esp_lcd_panel_handle_t panel;
    esp_lcd_panel_io_handle_t io;
    int backlight;

    /* 脏矩形 */
    int dirty_x0, dirty_y0, dirty_x1, dirty_y1;
    bool dirty;

    /* 刷屏降级统计：内部 RAM 不足时跳过帧的次数，以及 SPI 报错次数。
     * 用于诊断"为什么屏幕偶尔不更新"——总比默默卡住或雪崩好。 */
    uint32_t flush_skipped;
    uint32_t flush_errors;
    /* 首次刷屏实际切成几块（启动时打一次，便于确认分块生效） */
    uint32_t flush_chunks;

    /* 颜色诊断状态：color_index < 0 表示未在诊断。
     * 用状态机而不是阻塞延时 —— 见 bot_display_color_test 的说明。 */
    int color_index;
    int color_hold_ms;
    int64_t color_next_us;

    /* 当前表情与叠加文字 */
    char face[24];
    float face_intensity;
    char text[64];
    int64_t text_until_us;    /* 0 = 不自动消失 */

    SemaphoreHandle_t lock;
} disp_ctx_t;

static disp_ctx_t s_d;

/* ------------------------------------------------------------------ */
/* 脏矩形                                                             */
/* ------------------------------------------------------------------ */

static void mark_dirty(int x0, int y0, int x1, int y1)
{
    if (x1 < x0 || y1 < y0) {
        return;
    }
    if (x0 < 0) {
        x0 = 0;
    }
    if (y0 < 0) {
        y0 = 0;
    }
    if (x1 > s_d.width - 1) {
        x1 = s_d.width - 1;
    }
    if (y1 > s_d.height - 1) {
        y1 = s_d.height - 1;
    }
    if (x1 < 0 || y1 < 0 || x0 > s_d.width - 1 || y0 > s_d.height - 1) {
        return;
    }

    if (!s_d.dirty) {
        s_d.dirty_x0 = x0;
        s_d.dirty_y0 = y0;
        s_d.dirty_x1 = x1;
        s_d.dirty_y1 = y1;
        s_d.dirty = true;
        return;
    }
    if (x0 < s_d.dirty_x0) {
        s_d.dirty_x0 = x0;
    }
    if (y0 < s_d.dirty_y0) {
        s_d.dirty_y0 = y0;
    }
    if (x1 > s_d.dirty_x1) {
        s_d.dirty_x1 = x1;
    }
    if (y1 > s_d.dirty_y1) {
        s_d.dirty_y1 = y1;
    }
}

/* 直接写帧缓冲里的一个像素（不裁剪，调用方保证范围） */
static inline void put_px(int x, int y, uint16_t color)
{
    s_d.fb[y * s_d.width + x] = color;
}

/* ------------------------------------------------------------------ */
/* 基础绘制                                                           */
/* ------------------------------------------------------------------ */

esp_err_t bot_display_fill(uint16_t color)
{
    if (!s_d.ready) {
        return ESP_ERR_INVALID_STATE;
    }
    xSemaphoreTake(s_d.lock, portMAX_DELAY);
    for (int i = 0; i < s_d.width * s_d.height; i++) {
        s_d.fb[i] = color;
    }
    mark_dirty(0, 0, s_d.width - 1, s_d.height - 1);
    xSemaphoreGive(s_d.lock);
    return ESP_OK;
}

void bot_display_hline(int x, int y, int w, uint16_t color)
{
    if (!s_d.ready || y < 0 || y >= s_d.height) {
        return;
    }
    int x0 = x;
    int x1 = x + w - 1;
    if (x1 < 0 || x0 >= s_d.width) {
        return;
    }
    if (x0 < 0) {
        x0 = 0;
    }
    if (x1 > s_d.width - 1) {
        x1 = s_d.width - 1;
    }

    xSemaphoreTake(s_d.lock, portMAX_DELAY);
    for (int i = x0; i <= x1; i++) {
        put_px(i, y, color);
    }
    mark_dirty(x0, y, x1, y);
    xSemaphoreGive(s_d.lock);
}

void bot_display_fill_rect(int x, int y, int w, int h, uint16_t color)
{
    if (!s_d.ready || w <= 0 || h <= 0) {
        return;
    }

    int x0 = x, y0 = y, x1 = x + w - 1, y1 = y + h - 1;
    if (x0 < 0) {
        x0 = 0;
    }
    if (y0 < 0) {
        y0 = 0;
    }
    if (x1 > s_d.width - 1) {
        x1 = s_d.width - 1;
    }
    if (y1 > s_d.height - 1) {
        y1 = s_d.height - 1;
    }
    if (x0 > x1 || y0 > y1) {
        return;
    }

    xSemaphoreTake(s_d.lock, portMAX_DELAY);
    for (int yy = y0; yy <= y1; yy++) {
        uint16_t *row = s_d.fb + yy * s_d.width;
        for (int xx = x0; xx <= x1; xx++) {
            row[xx] = color;
        }
    }
    mark_dirty(x0, y0, x1, y1);
    xSemaphoreGive(s_d.lock);
}

void bot_display_fill_circle(int cx, int cy, int radius, uint16_t color)
{
    if (!s_d.ready || radius <= 0) {
        return;
    }

    int y0 = cy - radius;
    int y1 = cy + radius;
    if (y0 < 0) {
        y0 = 0;
    }
    if (y1 > s_d.height - 1) {
        y1 = s_d.height - 1;
    }

    xSemaphoreTake(s_d.lock, portMAX_DELAY);
    for (int y = y0; y <= y1; y++) {
        int dy = y - cy;
        /* 用勾股求该行的半宽；整数开方用近似即可，
         * 圆边缘差一像素肉眼看不出。 */
        int rr = radius * radius - dy * dy;
        if (rr < 0) {
            continue;
        }
        int dx = 0;
        while ((dx + 1) * (dx + 1) <= rr) {
            dx++;
        }

        int xa = cx - dx;
        int xb = cx + dx;
        if (xa < 0) {
            xa = 0;
        }
        if (xb > s_d.width - 1) {
            xb = s_d.width - 1;
        }
        if (xa > xb) {
            continue;
        }
        uint16_t *row = s_d.fb + y * s_d.width;
        for (int x = xa; x <= xb; x++) {
            row[x] = color;
        }
        mark_dirty(xa, y, xb, y);
    }
    xSemaphoreGive(s_d.lock);
}

void bot_display_line(int x0, int y0, int x1, int y1, uint16_t color)
{
    if (!s_d.ready) {
        return;
    }

    xSemaphoreTake(s_d.lock, portMAX_DELAY);

    /* Bresenham */
    int dx = abs(x1 - x0);
    int dy = -abs(y1 - y0);
    int sx = x0 < x1 ? 1 : -1;
    int sy = y0 < y1 ? 1 : -1;
    int err = dx + dy;

    while (true) {
        if (x0 >= 0 && x0 < s_d.width && y0 >= 0 && y0 < s_d.height) {
            put_px(x0, y0, color);
            mark_dirty(x0, y0, x0, y0);
        }
        if (x0 == x1 && y0 == y1) {
            break;
        }
        int e2 = 2 * err;
        if (e2 >= dy) {
            err += dy;
            x0 += sx;
        }
        if (e2 <= dx) {
            err += dx;
            y0 += sy;
        }
    }

    xSemaphoreGive(s_d.lock);
}

void bot_display_polyline(const int *xs, const int *ys, int count, uint16_t color)
{
    if (xs == NULL || ys == NULL || count < 2) {
        return;
    }
    for (int i = 0; i + 1 < count; i++) {
        bot_display_line(xs[i], ys[i], xs[i + 1], ys[i + 1], color);
    }
}

/* ------------------------------------------------------------------ */
/* 文字                                                               */
/* ------------------------------------------------------------------ */

/*
 * 把 UTF-8 流里的下一个码点解出来。
 * 返回码点，并推进 *p。非法字节按单字节推进，避免死循环。
 */
static uint32_t utf8_next(const char **p)
{
    const unsigned char *s = (const unsigned char *)*p;
    if (*s == '\0') {
        return 0;
    }

    uint32_t cp = 0;
    int extra = 0;
    if (s[0] < 0x80) {
        cp = s[0];
        extra = 0;
    } else if ((s[0] & 0xE0) == 0xC0) {
        cp = s[0] & 0x1F;
        extra = 1;
    } else if ((s[0] & 0xF0) == 0xE0) {
        cp = s[0] & 0x0F;
        extra = 2;
    } else if ((s[0] & 0xF8) == 0xF0) {
        cp = s[0] & 0x07;
        extra = 3;
    } else {
        (*p)++;
        return '?';
    }

    for (int i = 1; i <= extra; i++) {
        if ((s[i] & 0xC0) != 0x80) {
            (*p)++;
            return '?';
        }
        cp = (cp << 6) | (s[i] & 0x3F);
    }
    *p += extra + 1;
    return cp;
}

int bot_display_text(int x, int y, const char *utf8, uint16_t color, int scale)
{
    if (!s_d.ready || utf8 == NULL) {
        return 0;
    }
    if (scale < 1) {
        scale = 1;
    }

    int drawn = 0;
    int cursor_x = x;
    const char *p = utf8;

    while (true) {
        uint32_t cp = utf8_next(&p);
        if (cp == 0) {
            break;
        }

        /* 非 ASCII 一律画 '?'：内置字库只有 ASCII。
         * 中文要走 display_frame 推图的路子（见 README）。 */
        if (cp < 0x20 || cp > 0x7E) {
            cp = '?';
        }

        const uint8_t *glyph = bot_font_8x16[cp - 0x20];

        int gx0 = cursor_x;
        int gy0 = y;
        int gw = BOT_FONT_W * scale;
        int gh = BOT_FONT_H * scale;

        if (gx0 < s_d.width && gy0 < s_d.height) {
            xSemaphoreTake(s_d.lock, portMAX_DELAY);
            for (int row = 0; row < BOT_FONT_H; row++) {
                uint8_t bits = glyph[row];
                if (bits == 0) {
                    continue;
                }
                for (int col = 0; col < BOT_FONT_W; col++) {
                    if ((bits & (0x80 >> col)) == 0) {
                        continue;
                    }
                    /* 放大：一个点画成 scale×scale 的方块 */
                    for (int sy = 0; sy < scale; sy++) {
                        int py = gy0 + row * scale + sy;
                        if (py < 0 || py >= s_d.height) {
                            continue;
                        }
                        uint16_t *r = s_d.fb + py * s_d.width;
                        for (int sx = 0; sx < scale; sx++) {
                            int px = gx0 + col * scale + sx;
                            if (px < 0 || px >= s_d.width) {
                                continue;
                            }
                            r[px] = color;
                        }
                    }
                }
            }
            mark_dirty(gx0, gy0, gx0 + gw - 1, gy0 + gh - 1);
            xSemaphoreGive(s_d.lock);
        }

        cursor_x += gw;
        drawn++;

        if (cursor_x >= s_d.width) {
            break; /* 出屏即停，省时间 */
        }
    }

    return drawn;
}

int bot_display_text_width(const char *utf8, int scale)
{
    if (utf8 == NULL) {
        return 0;
    }
    if (scale < 1) {
        scale = 1;
    }
    int n = 0;
    const char *p = utf8;
    while (utf8_next(&p) != 0) {
        n++;
    }
    return n * BOT_FONT_W * scale;
}

/* ------------------------------------------------------------------ */
/* 表情                                                               */
/* ------------------------------------------------------------------ */

/* 表情配色：仅供叠加文字与"当前表情"记录使用。
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

esp_err_t bot_display_show_face(const char *emotion, float intensity)
{
    if (!s_d.ready) {
        return ESP_ERR_INVALID_STATE;
    }

    const face_style_t *style = find_style(emotion);

    strncpy(s_d.face, style->name, sizeof(s_d.face) - 1);
    s_d.face[sizeof(s_d.face) - 1] = '\0';
    s_d.face_intensity = intensity;

    bot_face_render(style->name, intensity);

    /* 如果之前有文字还在显示期内，重画一遍（换表情不该把字吃掉） */
    if (s_d.text[0] != '\0' && s_d.text_until_us > esp_timer_get_time()) {
        int tw = bot_display_text_width(s_d.text, 2);
        bot_display_text((s_d.width - tw) / 2, s_d.height - 40, s_d.text, style->fg, 2);
    }

    ESP_LOGI(TAG, "表情: %s (强度 %.2f)", style->name, intensity);
    return ESP_OK;
}

esp_err_t bot_display_show_text(const char *utf8, int duration_ms)
{
    if (!s_d.ready) {
        return ESP_ERR_INVALID_STATE;
    }

    strncpy(s_d.text, utf8 != NULL ? utf8 : "", sizeof(s_d.text) - 1);
    s_d.text[sizeof(s_d.text) - 1] = '\0';

    const face_style_t *style = find_style(s_d.face);
    int tw = bot_display_text_width(s_d.text, 2);
    int tx = (s_d.width - tw) / 2;
    if (tx < 0) {
        tx = 0;
    }
    int ty = s_d.height - 40;

    /* 文字用底色反衬，避免压在图形上看不清 */
    bot_display_fill_rect(0, ty - 4, s_d.width, BOT_FONT_H * 2 + 8, style->bg);
    bot_display_text(tx, ty, s_d.text, style->fg, 2);

    s_d.text_until_us = (duration_ms > 0)
                            ? esp_timer_get_time() + (int64_t)duration_ms * 1000
                            : 0;
    return ESP_OK;
}

esp_err_t bot_display_show_image(const uint8_t *data, size_t len, const char *format)
{
    /*
     * 本工程没有引入 JPEG 解码器（省 Flash 与依赖）。
     * PC 端要显示图片时，建议改成自己把图缩到屏幕尺寸后
     * 以 RGB565 裸数据推过来，或者给固件加 esp_jpeg 组件。
     *
     * 这里明确返回"不支持"，让协议层回 unsupported_param —— 
     * 比静默什么都不做要好：PC 端能立刻知道失败原因。
     */
    (void)data;
    (void)len;
    (void)format;
    ESP_LOGW(TAG, "display_frame 未实现（固件未内置图像解码器）");
    return ESP_ERR_NOT_SUPPORTED;
}

esp_err_t bot_display_clear(void)
{
    if (!s_d.ready) {
        return ESP_ERR_INVALID_STATE;
    }
    s_d.text[0] = '\0';
    s_d.text_until_us = 0;
    return bot_display_show_face(s_d.face[0] ? s_d.face : "neutral", 1.0f);
}

/*
 * 颜色诊断：依次整屏显示纯色，用来定位通道错误。
 *
 * 为什么需要它：颜色不对时，光看"黄色变粉红"很难判断到底是
 * 颜色顺序（RGB/BGR）错了、还是反色开关错了 —— 两者都会让颜色变样。
 * 依次显示纯色就能一眼看出：
 *
 *   * 显示的红色实际是蓝色  -> 颜色顺序反了（RGB/BGR 要互换）
 *   * 全部颜色都"反相"（白显示成黑）-> 反色开关反了
 *   * 红绿蓝都正确但黄不对  -> 那是单个通道位数（5/6/5）的问题
 *
 * **非阻塞实现**：这里只登记状态，颜色由 bot_display_poll() 逐帧推进。
 * 早先是阻塞版（每色 vTaskDelay 1.2 秒），六色要 7 秒以上，超过了 PC 端
 * 5 秒的命令超时，调用方只会拿到 "未在 5.0s 内响应" —— 诊断动作必须
 * 立刻返回，否则根本用不了。
 */
esp_err_t bot_display_color_test(int hold_ms)
{
    if (!s_d.ready) {
        return ESP_ERR_INVALID_STATE;
    }
    if (hold_ms <= 0) {
        hold_ms = 1200;
    }
    s_d.color_hold_ms = hold_ms;
    s_d.color_index = 0;
    s_d.color_next_us = 0; /* 让 poll 立刻画第一色 */
    return ESP_OK;
}

/*
 * 把帧缓冲里出现最多的几种颜色统计出来打到串口。
 *
 * 为什么需要它：排查颜色问题时，"屏幕上是什么颜色"靠人眼描述容易有偏差
 * （同一块屏在不同光线下、不同人眼里说法不同）。直接读出帧缓冲的实际
 * 像素值，就能确认**软件到底画了什么**，把"软件画错"和"屏幕显示错"
 * 这两件事分开。配合颜色诊断动作，就能定位到具体哪一环。
 */
void bot_display_dump_colors(int top_n)
{
    if (!s_d.ready || s_d.fb == NULL) {
        ESP_LOGW(TAG, "帧缓冲不可用，无法转储");
        return;
    }
    if (top_n <= 0 || top_n > 8) {
        top_n = 6;
    }

    /* 用一个小的直方图统计：RGB565 只有 65536 种可能，但每帧实际只用几种。
     * 这里只统计全屏比例，用"扫描 + 计数"的简化做法，避免开大数组。 */
    typedef struct {
        uint16_t color;
        uint32_t count;
    } entry_t;
    entry_t top[8] = {0};
    const uint32_t total = (uint32_t)s_d.width * (uint32_t)s_d.height;

    for (uint32_t i = 0; i < total; i++) {
        uint16_t c = s_d.fb[i];
        int slot = -1;
        for (int k = 0; k < top_n; k++) {
            if (top[k].count > 0 && top[k].color == c) {
                slot = k;
                break;
            }
            if (top[k].count == 0) {
                top[k].color = c;
                slot = k;
                break;
            }
        }
        if (slot < 0) {
            /* 表已满：找当前最少的替换（近似 Top-N） */
            int min_k = 0;
            for (int k = 1; k < top_n; k++) {
                if (top[k].count < top[min_k].count) {
                    min_k = k;
                }
            }
            top[min_k].color = c;
            top[min_k].count = 0;
            slot = min_k;
        }
        top[slot].count++;
    }

    /* 按占比排序（简单选择排序，N 很小） */
    for (int i = 0; i < top_n; i++) {
        for (int j = i + 1; j < top_n; j++) {
            if (top[j].count > top[i].count) {
                entry_t t = top[i];
                top[i] = top[j];
                top[j] = t;
            }
        }
    }

    ESP_LOGI(TAG, "帧缓冲颜色统计（全屏共 %u 像素，当前表情=%s）",
             (unsigned)total, s_d.face);
    for (int i = 0; i < top_n; i++) {
        if (top[i].count == 0) {
            continue;
        }
        uint8_t r5 = (top[i].color >> 11) & 0x1F;
        uint8_t g6 = (top[i].color >> 5) & 0x3F;
        uint8_t b5 = top[i].color & 0x1F;
        ESP_LOGI(TAG, "  0x%04X = RGB(%3u,%3u,%3u)  占用 %6.2f%%  (%u 像素)",
                 top[i].color,
                 (unsigned)(r5 * 255 / 31),
                 (unsigned)(g6 * 255 / 63),
                 (unsigned)(b5 * 255 / 31),
                 100.0f * (float)top[i].count / (float)total,
                 (unsigned)top[i].count);
    }
}

/* 供 bot_display_poll() 调用的推进逻辑；返回 true 表示正在诊断 */
bool bot_display_color_step(void)
{
    if (s_d.color_index < 0) {
        return false;
    }

    static const uint16_t colors[] = {
        0xF800, /* RED */
        0x07E0, /* GREEN */
        0x001F, /* BLUE */
        0xFFE0, /* YELLOW —— 就是表情脸的黄 */
        0xFFFF, /* WHITE */
        0x0000, /* BLACK */
    };
    const int n = (int)(sizeof(colors) / sizeof(colors[0]));
    int64_t now = esp_timer_get_time();

    if (s_d.color_index > 0 && now < s_d.color_next_us) {
        return true; /* 当前色还没到时间 */
    }

    if (s_d.color_index >= n) {
        /* 诊断结束，回到正常画面 */
        s_d.color_index = -1;
        bot_display_redraw();
        ESP_LOGI(TAG, "颜色诊断结束（顺序应为 红 绿 蓝 黄 白 黑，"
                      "诊断前配置见启动日志的面板配置那一行）");
        return false;
    }

    bot_display_fill(colors[s_d.color_index]);
    ESP_LOGI(TAG, "颜色诊断 %d/%d: 现在应显示 %s (0x%04X)",
             s_d.color_index + 1, n,
             (const char *[]){"RED", "GREEN", "BLUE", "YELLOW", "WHITE", "BLACK"}[s_d.color_index],
             colors[s_d.color_index]);
    s_d.color_next_us = now + (int64_t)s_d.color_hold_ms * 1000;
    s_d.color_index++;
    return true;
}

void bot_display_redraw(void)
{
    if (!s_d.ready) {
        return;
    }
    bot_display_show_face(s_d.face[0] ? s_d.face : "neutral", s_d.face_intensity);
}

/* ------------------------------------------------------------------ */
/* 背光                                                               */
/* ------------------------------------------------------------------ */

void bot_display_set_backlight(int percent)
{
    if (percent < 0) {
        percent = 0;
    }
    if (percent > 100) {
        percent = 100;
    }
    s_d.backlight = percent;

    if (CONFIG_SPARKBOT_LCD_BACKLIGHT_PIN < 0) {
        return;
    }
    uint32_t duty = (uint32_t)((float)percent / 100.0f * (float)BL_LEDC_MAX);
    ledc_set_duty(BL_LEDC_MODE, BL_LEDC_CH, duty);
    ledc_update_duty(BL_LEDC_MODE, BL_LEDC_CH);
}

int bot_display_get_backlight(void)
{
    return s_d.backlight;
}

/* ------------------------------------------------------------------ */
/* 初始化                                                             */
/* ------------------------------------------------------------------ */

esp_err_t bot_display_init(void)
{
    memset(&s_d, 0, sizeof(s_d));
    s_d.width = CONFIG_SPARKBOT_LCD_WIDTH;
    s_d.height = CONFIG_SPARKBOT_LCD_HEIGHT;
    s_d.backlight = CONFIG_SPARKBOT_LCD_BACKLIGHT_PERCENT;
    /* -1 = 未在跑颜色诊断。memset 会给 0，那是"正在诊断第一色"，
     * 不显式设成 -1 的话开机就会自动播一遍颜色测试。 */
    s_d.color_index = -1;
    s_d.lock = xSemaphoreCreateMutex();
    strncpy(s_d.face, "neutral", sizeof(s_d.face) - 1);

#if !CONFIG_SPARKBOT_LCD_ENABLE
    ESP_LOGI(TAG, "LCD 未启用");
    return ESP_OK;
#endif

    if (s_d.lock == NULL) {
        return ESP_ERR_NO_MEM;
    }

    /*
     * 帧缓冲放 PSRAM，并且**要求它自身可被 DMA**。
     *
     * 为什么要带 MALLOC_CAP_DMA：ESP-IDF 的 SPI 驱动（spi_master.c 的
     * setup_dma_priv_buffer）这样判断是否需要额外拷贝：
     *
     *     is_ptr_ext  = esp_ptr_external_ram(buffer)      // 帧缓冲在 PSRAM → true
     *     use_psram   = is_ptr_ext && (flags & SPI_TRANS_DMA_USE_PSRAM)
     *     need_malloc = is_ptr_ext ? (!use_psram || !esp_ptr_dma_ext_capable(buffer))
     *                              : !esp_ptr_dma_capable(buffer)
     *
     * 帧缓冲若只是普通 PSRAM（不可 DMA），need_malloc 为真 → 驱动每次刷屏
     * 都要**临时申请一块内部 DMA 缓冲再拷贝**，于是出现：
     *     E spi_master: setup_dma_priv_buffer(1208): Failed to allocate priv TX buffer
     *     E lcd_panel.io.spi: spi_transmit (queue) color failed
     *
     * 实测过一件反直觉的事：**失败时内存其实是够的** ——
     *     DMA 可用=25383  最大连续块=13824（只要 1208 字节）
     * 所以这不是容量问题，而是那条"临时缓冲"路径本身走不通。
     * 让帧缓冲自身可 DMA，就能彻底绕开这次额外分配。
     *
     * 对齐用 64 字节：PSRAM 的 DMA 对缓存行对齐有要求，不对齐会被
     * 驱动再拷一次（`need_malloc |= ((uint32_t)buffer | len) & (alignment-1)`）。
     */
    size_t fb_bytes = (size_t)s_d.width * s_d.height * sizeof(uint16_t);
    s_d.fb = heap_caps_aligned_alloc(64, fb_bytes,
                                     MALLOC_CAP_SPIRAM | MALLOC_CAP_DMA | MALLOC_CAP_8BIT);
    if (s_d.fb != NULL) {
        ESP_LOGI(TAG, "帧缓冲已按 DMA 能力分配（PSRAM，64 字节对齐）");
    }
    if (s_d.fb == NULL) {
        /* 退一步：普通 PSRAM。仍可工作，只是每次刷屏驱动要多拷一块，
         * 内存紧张时可能出现 "Failed to allocate priv TX buffer"。 */
        ESP_LOGW(TAG, "PSRAM DMA 分配失败，退回普通 PSRAM（刷屏会多一次拷贝）");
        s_d.fb = heap_caps_malloc(fb_bytes, MALLOC_CAP_SPIRAM | MALLOC_CAP_8BIT);
    }
    if (s_d.fb == NULL) {
        /* 最后退到内部 RAM：小屏或 PSRAM 未启用时还能跑 */
        ESP_LOGW(TAG, "PSRAM 分配失败，退回内部 RAM");
        s_d.fb = malloc(fb_bytes);
    }
    if (s_d.fb == NULL) {
        ESP_LOGE(TAG, "帧缓冲分配失败 (%u 字节)", (unsigned)fb_bytes);
        return ESP_ERR_NO_MEM;
    }
    ESP_LOGI(TAG, "帧缓冲 %dx%d = %u KB（在 PSRAM=%d，可 DMA=%d）",
             s_d.width, s_d.height, (unsigned)(fb_bytes / 1024),
             (int)esp_ptr_external_ram(s_d.fb),
             (int)esp_ptr_dma_ext_capable(s_d.fb));

    /* SPI 总线 */
    spi_bus_config_t buscfg = {
        .sclk_io_num = CONFIG_SPARKBOT_LCD_SCK_PIN,
        .mosi_io_num = CONFIG_SPARKBOT_LCD_MOSI_PIN,
        .miso_io_num = -1,
        .quadwp_io_num = -1,
        .quadhd_io_num = -1,
        .max_transfer_sz = s_d.width * 40 * sizeof(uint16_t),
    };
    esp_err_t err = spi_bus_initialize(SPI2_HOST, &buscfg, SPI_DMA_CH_AUTO);
    if (err != ESP_OK) {
        ESP_LOGE(TAG, "SPI 总线初始化失败: %s", esp_err_to_name(err));
        return err;
    }

    esp_lcd_panel_io_spi_config_t io_config = {
        .dc_gpio_num = CONFIG_SPARKBOT_LCD_DC_PIN,
        .cs_gpio_num = CONFIG_SPARKBOT_LCD_CS_PIN,
        .pclk_hz = CONFIG_SPARKBOT_LCD_SPI_CLOCK_HZ,
        .lcd_cmd_bits = 8,
        .lcd_param_bits = 8,
        .spi_mode = 0,
        .trans_queue_depth = 10,
    };
    err = esp_lcd_new_panel_io_spi((esp_lcd_spi_bus_handle_t)SPI2_HOST, &io_config, &s_d.io);
    if (err != ESP_OK) {
        ESP_LOGE(TAG, "面板 IO 创建失败: %s", esp_err_to_name(err));
        return err;
    }

    esp_lcd_panel_dev_config_t panel_config = {
        .reset_gpio_num = -1, /* 本板未接 RESET，走软件复位 */
        /*
         * 颜色通道顺序可配。
         *
         * "黄色显示成粉红"是通道错位的典型症状，所以把颜色顺序与反色
         * 都做成 Kconfig 选项，便于用 menuconfig 直接试出正确组合 ——
         * 比反复改代码重新编译快得多。
         */
#if CONFIG_SPARKBOT_LCD_COLOR_RGB
        .rgb_ele_order = LCD_RGB_ELEMENT_ORDER_RGB,
#else
        .rgb_ele_order = LCD_RGB_ELEMENT_ORDER_BGR,
#endif
        .bits_per_pixel = 16,
    };
    err = esp_lcd_new_panel_ili9341(s_d.io, &panel_config, &s_d.panel);
    if (err != ESP_OK) {
        ESP_LOGE(TAG, "ILI9341 面板创建失败: %s", esp_err_to_name(err));
        return err;
    }

    esp_lcd_panel_reset(s_d.panel);
    esp_lcd_panel_init(s_d.panel);

    /* 这块屏的接线决定了要镜像 */
    esp_lcd_panel_mirror(s_d.panel, true, true);
    esp_lcd_panel_swap_xy(s_d.panel, false);
    /*
     * 反色可配（见 Kconfig 说明）。ILI9341 通常需要、ILI9342C 通常不需要。
     * esp_lcd_ili9341 驱动本身**不会**自动发 INVON，所以这里是唯一的
     * 反色控制点，不会出现"驱动和代码各反一次"的叠加问题。
     */
#if CONFIG_SPARKBOT_LCD_INVERT
    esp_lcd_panel_invert_color(s_d.panel, true);
#else
    esp_lcd_panel_invert_color(s_d.panel, false);
#endif
    esp_lcd_panel_set_gap(s_d.panel, 0, 0);
    esp_lcd_panel_disp_on_off(s_d.panel, true);

#if CONFIG_SPARKBOT_LCD_COLOR_RGB
    const char *order_name = "RGB";
#else
    const char *order_name = "BGR";
#endif
#if CONFIG_SPARKBOT_LCD_INVERT
    ESP_LOGI(TAG, "面板配置: 颜色顺序=%s 反色=开", order_name);
#else
    ESP_LOGI(TAG, "面板配置: 颜色顺序=%s 反色=关", order_name);
#endif

    /* 背光 PWM */
    if (CONFIG_SPARKBOT_LCD_BACKLIGHT_PIN >= 0) {
        ledc_timer_config_t tcfg = {
            .speed_mode = BL_LEDC_MODE,
            .timer_num = BL_LEDC_TIMER,
            .duty_resolution = BL_LEDC_RES,
            .freq_hz = 5000,
            .clk_cfg = LEDC_AUTO_CLK,
        };
        ledc_timer_config(&tcfg);

        ledc_channel_config_t ccfg = {
            .gpio_num = CONFIG_SPARKBOT_LCD_BACKLIGHT_PIN,
            .speed_mode = BL_LEDC_MODE,
            .channel = BL_LEDC_CH,
            .intr_type = LEDC_INTR_DISABLE,
            .timer_sel = BL_LEDC_TIMER,
            .duty = 0,
            .hpoint = 0,
        };
        ledc_channel_config(&ccfg);

        /* 本板背光是反相的：占空比 100% = 最暗。
         * 所以这里先把亮度映射反过来再写。 */
        bot_display_set_backlight(s_d.backlight);
    }

    s_d.ready = true;
    ESP_LOGI(TAG, "LCD 就绪: %dx%d SPI=%dHz SCK=%d MOSI=%d DC=%d CS=%d BL=%d",
             s_d.width, s_d.height, CONFIG_SPARKBOT_LCD_SPI_CLOCK_HZ,
             CONFIG_SPARKBOT_LCD_SCK_PIN, CONFIG_SPARKBOT_LCD_MOSI_PIN,
             CONFIG_SPARKBOT_LCD_DC_PIN, CONFIG_SPARKBOT_LCD_CS_PIN,
             CONFIG_SPARKBOT_LCD_BACKLIGHT_PIN);

    bot_display_show_face("neutral", 1.0f);
    return ESP_OK;
}

bool bot_display_ready(void)
{
    return s_d.ready;
}

int bot_display_width(void)
{
    return s_d.width;
}

int bot_display_height(void)
{
    return s_d.height;
}

/* ------------------------------------------------------------------ */
/* 刷新                                                               */
/* ------------------------------------------------------------------ */

void bot_display_poll(void)
{
    if (!s_d.ready) {
        return;
    }

    /* 颜色诊断优先：它在跑的时候不要被表情/文字的刷新逻辑打断 */
    if (s_d.color_index >= 0) {
        bot_display_color_step();
        return;
    }

    /* 文字到点自动消失 */
    if (s_d.text[0] != '\0' && s_d.text_until_us > 0
        && esp_timer_get_time() > s_d.text_until_us) {
        s_d.text[0] = '\0';
        s_d.text_until_us = 0;
        bot_display_redraw();
    }

    if (!s_d.dirty) {
        return;
    }

    int x0 = s_d.dirty_x0;
    int y0 = s_d.dirty_y0;
    int x1 = s_d.dirty_x1;
    int y1 = s_d.dirty_y1;
    s_d.dirty = false;

    /* 往外扩一点：SPI 按整块 DMA 传输，边缘对齐能减少一次传输的开销，
     * 也让文字抗锯齿边缘的相邻像素一起更新，避免残影。 */
    x0 = (x0 - 1) & ~1;
    if (x0 < 0) {
        x0 = 0;
    }
    y0 = y0 - 1;
    if (y0 < 0) {
        y0 = 0;
    }
    x1 = x1 + 1;
    if (x1 > s_d.width - 1) {
        x1 = s_d.width - 1;
    }
    y1 = y1 + 1;
    if (y1 > s_d.height - 1) {
        y1 = s_d.height - 1;
    }

    /*
     * 分块刷屏 —— 让每次 DMA 的临时缓冲需求足够小。
     *
     * 背景（读 IDF 源码 + 实测确认的完整链条）：
     *   * `esp_lcd` 的 SPI 驱动**从不**设置 `SPI_TRANS_DMA_USE_PSRAM`
     *     （全 IDF 只有 SPI 自测代码用它，配置结构里也没有对应开关）。
     *   * 因此只要传输缓冲在 PSRAM，spi_master.c 的 setup_dma_priv_buffer
     *     就会判定 need_malloc=true，去申请一块**内部 DMA 缓冲**并拷贝：
     *         MALLOC_CAP_DMA | MALLOC_CAP_INTERNAL
     *   * 本板启动日志写着 `Reserving pool of 32K of internal memory for
     *     DMA/internal allocations` —— DMA 池很小。整屏 320x240x2=150KB
     *     一次分配必然失败：
     *         E spi_master: setup_dma_priv_buffer(1208): Failed to allocate
     *         E lcd_panel.io.spi: spi_transmit (queue) color failed
     *
     * 实测过的反直觉事实：失败时 DMA 其实还剩 25KB、最大连续块 13.8KB，
     * 而它只要 1208 字节 —— 所以**不是简单的大小不够**，更可能是那块
     * 池子的对齐/碎片约束。既然无法让驱动走 PSRAM-DMA，就把每次请求
     * 压到最小：按行分块，单块只有几百字节。
     *
     * 代价只是多几次 SPI 事务，对 240 行的小屏完全可接受。
     */
    const int kMaxRowsPerChunk = 16;

    xSemaphoreTake(s_d.lock, portMAX_DELAY);

    int y = y0;
    int chunks = 0;
    int failures = 0;
    while (y <= y1) {
        int y_end = y + kMaxRowsPerChunk - 1;
        if (y_end > y1) {
            y_end = y1;
        }
        /* draw_bitmap 要的是该区域首行首像素的指针 */
        esp_err_t err = esp_lcd_panel_draw_bitmap(s_d.panel, x0, y, x1 + 1, y_end + 1,
                                                  s_d.fb + y * s_d.width + x0);
        chunks++;
        if (err != ESP_OK) {
            failures++;
            s_d.flush_errors++;
            if (s_d.flush_errors == 1 || s_d.flush_errors % 50 == 0) {
                ESP_LOGW(TAG, "刷屏失败 %s（累计 %u 次）: DMA可用=%u DMA最大块=%u "
                              "内部可用=%u 内部最大块=%u 本块 %d 行=%u 字节",
                         esp_err_to_name(err), (unsigned)s_d.flush_errors,
                         (unsigned)heap_caps_get_free_size(MALLOC_CAP_DMA),
                         (unsigned)heap_caps_get_largest_free_block(MALLOC_CAP_DMA),
                         (unsigned)heap_caps_get_free_size(MALLOC_CAP_INTERNAL),
                         (unsigned)heap_caps_get_largest_free_block(MALLOC_CAP_INTERNAL),
                         y_end - y + 1,
                         (unsigned)((x1 - x0 + 1) * (y_end - y + 1) * sizeof(uint16_t)));
            }
        }
        y = y_end + 1;
    }
    xSemaphoreGive(s_d.lock);

    if (failures > 0) {
        /* 保留脏标记，下一轮重试（内存通常很快恢复） */
        mark_dirty(x0, y0 - 1, x1 - 1, y1 - 1);
    }
    if (s_d.flush_chunks == 0) {
        s_d.flush_chunks = (uint32_t)chunks;
        ESP_LOGI(TAG, "刷屏分块: 每次 %d 行，本次共 %d 块（失败 %d）",
                 kMaxRowsPerChunk, chunks, failures);
    }
}
