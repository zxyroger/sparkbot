/*
 * 本地唤醒词检测（esp-sr AFE + WakeNet9）
 *
 * 模型：wn9_hixiaoxing_tts —— 实际发音是 **"Hi,小星"**。
 * 官方开放词里没有"你好小星"，自定义训练代价很大，见 bot_wakeword.c 说明。
 */

#pragma once

#include <stdbool.h>
#include <stdint.h>

#include "esp_err.h"

#ifdef __cplusplus
extern "C" {
#endif

/* 唤醒词命中回调。在采集任务上下文里被调用，**不要在这里做耗时操作**。 */
typedef void (*bot_wakeword_cb_t)(void);

/*
 * 初始化唤醒词引擎。
 *
 * 必须在 `bot_audio_init()` 之后调用（它依赖音频就绪，并会挂上采集旁路）。
 * 失败只影响唤醒词，音频与其它功能照常。
 */
esp_err_t bot_wakeword_init(void);

bool bot_wakeword_ready(void);

/* 注册命中回调（一般用来发 wake_word 事件） */
void bot_wakeword_set_cb(bot_wakeword_cb_t cb);

/* 取统计值（feed 次数 / 命中次数 / 丢弃帧数 / feed+fetch 最坏耗时 us），
 * 用于诊断"喊了没反应"。任一指针可为 NULL。 */
void bot_wakeword_stats(uint32_t *fed, uint32_t *hits, uint32_t *dropped, int64_t *max_cost_us);

/*
 * 打印唤醒词/AFE 状态与**系统中所有任务**（含 AFE 内部任务）。
 *
 * 排查 `fetch()` 不产出时最有用：能看出 AFE 的内部任务是否存在、
 * 优先级多少、运行计数有没有增长。
 */
void bot_wakeword_dump(void);

#ifdef __cplusplus
}
#endif
