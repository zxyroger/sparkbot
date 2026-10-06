/*
 * 显示：ILI9341/ILI9342C SPI 面板 + 表情与文字绘制
 *
 * 设计取舍：**不做整屏帧缓冲**。
 *
 *   320x240 RGB565 一帧是 150KB。ESP32-S3 的内部 RAM 只有 512KB，
 *   其中还要放 WiFi/蓝牙协议栈、摄像头帧缓冲、音频缓冲。
 *   把 150KB 常驻在内部 RAM 里会挤爆其它模块；放 PSRAM 又会让
 *   每次绘制都走慢速总线。
 *
 *   所以这里用**行缓冲**策略：内部维护一块几行的 RGB565 缓冲，
 *   绘制函数往行缓冲里写，flush 时按行推给面板。
 *   对"画表情 + 显示一行字"这种场景，行缓冲完全够用，
 *   而且显存占用从 150KB 降到几 KB。
 *
 * 表情用几何图形画（圆/椭圆/直线/折线），不依赖字库；
 * 文字用内置 8x16 点阵字库（ASCII）。中文暂不支持 —— 见 README 的说明。
 */

#pragma once

#include <stdbool.h>
#include <stdint.h>

#include "esp_err.h"

#ifdef __cplusplus
extern "C" {
#endif

/* RGB565 常用颜色 */
#define BOT_COLOR_BLACK   0x0000
#define BOT_COLOR_WHITE   0xFFFF
#define BOT_COLOR_RED     0xF800
#define BOT_COLOR_GREEN   0x07E0
#define BOT_COLOR_BLUE    0x001F
#define BOT_COLOR_YELLOW  0xFFE0
#define BOT_COLOR_CYAN    0x07FF
#define BOT_COLOR_MAGENTA 0xF81F
#define BOT_COLOR_GRAY    0x8410
#define BOT_COLOR_ORANGE  0xFD20

/* 初始化 SPI + 面板 + 背光 PWM */
esp_err_t bot_display_init(void);

bool bot_display_ready(void);

int bot_display_width(void);
int bot_display_height(void);

/* 背光 0~100 */
void bot_display_set_backlight(int percent);
int bot_display_get_backlight(void);

/* ------------------------------------------------------------------ */
/* 基础绘制（写进行缓冲，需 flush 才真正显示）                        */
/* ------------------------------------------------------------------ */

/* 用整屏填充色覆盖内容（也会清掉表情） */
esp_err_t bot_display_fill(uint16_t rgb565);

/* 画一条水平线 */
void bot_display_hline(int x, int y, int w, uint16_t color);

/* 画一个填充矩形 */
void bot_display_fill_rect(int x, int y, int w, int h, uint16_t color);

/* 画填充圆（表情的腮红、眼睛高光等） */
void bot_display_fill_circle(int cx, int cy, int radius, uint16_t color);

/*
 * 画一条线段（Bresenham）。
 * 表情里的"嘴"用折线拼：折线由调用方拆成若干线段。
 */
void bot_display_line(int x0, int y0, int x1, int y1, uint16_t color);

/* 画一条折线（用于各种嘴形） */
void bot_display_polyline(const int *xs, const int *ys, int count, uint16_t color);

/*
 * 画 ASCII 文字。
 *
 * 注意：中文（多字节 UTF-8）会被替换成 '?'。
 * 需要显示中文时请改用 set_text 协议动作 + PC 端把文字画成图片推过来
 * （display_frame 动作），或者扩展字库。
 *
 * @param scale 放大倍数，1 = 8x16 原始大小
 * @return 实际绘制的字符数
 */
int bot_display_text(int x, int y, const char *utf8, uint16_t color, int scale);

/* 实测一段文字画出来有多宽（用于居中） */
int bot_display_text_width(const char *utf8, int scale);

/* ------------------------------------------------------------------ */
/* 表情                                                               */
/* ------------------------------------------------------------------ */

/* 按表情名绘制。未知名字画 neutral。 */
esp_err_t bot_display_show_face(const char *emotion, float intensity);

/*
 * 直接渲染一个表情（黄色圆角方块 + 黑色五官的 emoji 风格）。
 *
 * 由 bot_display_show_face() 内部调用；单独暴露出来是为了能在不改变
 * "当前表情"记录的情况下重绘（例如刷屏后恢复画面）。
 * 实现见 bot_face.c。
 */
void bot_face_render(const char *emotion, float intensity);

/* 在表情之上叠加一行文字（例如识别结果），短暂显示后自动消失。
 * duration_ms <= 0 表示不自动消失。 */
esp_err_t bot_display_show_text(const char *utf8, int duration_ms);

/*
 * 把一整帧 JPEG/PNG 解码后显示到面板。
 * 需要解码器（esp_jpeg），失败时返回 ESP_ERR_NOT_SUPPORTED，
 * 此时协议层应回 unsupported_param 而不是当作成功。
 */
esp_err_t bot_display_show_image(const uint8_t *data, size_t len, const char *format);

/* 清屏到默认背景色 */
esp_err_t bot_display_clear(void);

/*
 * 颜色诊断：依次整屏显示 红/绿/蓝/黄/白/黑 各 hold_ms 毫秒。
 *
 * 用于定位颜色通道配置（RGB/BGR 顺序、反色开关）是否正确 ——
 * 光看"黄色变粉红"分不清是顺序错还是反色错，看纯色就一目了然。
 * 实现见 bot_hw_display.c。
 */
esp_err_t bot_display_color_test(int hold_ms);

/*
 * 颜色诊断的推进函数，由 bot_display_poll() 调用。
 *
 * 它**不是**给外部直接调用的；写成非 static 只是为了能和 poll 分开维护。
 * 返回 true 表示诊断仍在进行。
 */
bool bot_display_color_step(void);

/* 把帧缓冲里占比最高的几种颜色打到串口，用于确认软件到底画了什么颜色。
 * 排查颜色问题时把软件画错和屏幕显示错分开。 */
void bot_display_dump_colors(int top_n);

/* 重绘当前表情（供文字消失后恢复） */
void bot_display_redraw(void);

/* 主循环定期调用：处理文字自动消失 */
void bot_display_poll(void);

#ifdef __cplusplus
}
#endif
