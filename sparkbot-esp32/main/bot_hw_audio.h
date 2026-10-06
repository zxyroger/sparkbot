/*
 * 音频：ES8311 codec + I2S，麦克风采集与喇叭播放
 *
 * 本工程需要真正的音频流：
 *
 *   - **采集**：16kHz / 16bit / 单声道 PCM，按 20ms 分片上行给 PC 做 ASR
 *   - **播放**：接收 PC 送来的 WAV 或裸 PCM 并播出来（TTS 的语音）
 *   - 音调：保留，用于"叮"这类即时反馈
 *
 * 采样率固定 16kHz：语音识别对它支持最好，20ms 分片正好 640 字节，
 * 与 SparkBot 协议推荐值一致。
 */

#pragma once

#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>

#include "esp_err.h"

#ifdef __cplusplus
extern "C" {
#endif

/* 采集回调：每凑够一片 PCM 就调一次。
 * data 生命周期仅限回调内，需要留存必须自己拷走。 */
typedef void (*bot_audio_capture_cb_t)(const uint8_t *pcm, size_t len);

/* 播放结束回调（用于发 audio_done 事件） */
typedef void (*bot_audio_done_cb_t)(void);

/*
 * 每次采到一片 PCM 时的旁路钩子（在采集回调**之前**调用）。
 *
 * 用途：把同一份麦克风数据同时喂给本地唤醒词引擎（AFE/WakeNet）。
 *
 * 为什么用"旁路"而不是让唤醒词引擎自己读 I2S：
 *   I2S 的读取已经由采集任务独占，再开一个任务读同一个 RX 句柄会互相
 *   抢数据（一块音频只会被其中一方拿到，两边都工作不正常）。
 *   所以这里不引入第二个读者，而是让采集任务顺手把数据分发给唤醒引擎。
 *
 * 代价：麦克风只在采集期间被打开，所以**唤醒词只在采集窗口内有效**。
 *   见 main/bot_wakeword.c 顶部关于"持续唤醒"的说明 —— 那是另一条
 *   需要常开 ADC 的路径，会明显增加功耗与改动面。
 */
typedef void (*bot_audio_monitor_cb_t)(const uint8_t *pcm, size_t len);

/* 初始化 I2S + ES8311 + PA。可重复调用；失败只影响音频，其它功能照常。 */
esp_err_t bot_audio_init(void);

bool bot_audio_ready(void);

/* 注册回调 */
void bot_audio_set_capture_cb(bot_audio_capture_cb_t cb);
void bot_audio_set_done_cb(bot_audio_done_cb_t cb);

/* 注册采集旁路钩子（唤醒词引擎用）。传 NULL 取消。 */
void bot_audio_set_monitor_cb(bot_audio_monitor_cb_t cb);

/* ------------------------------------------------------------------ */
/* 麦克风采集                                                         */
/* ------------------------------------------------------------------ */

/*
 * 开始/停止采集。
 *
 * 采集在**独立任务**里跑（I2S 读是阻塞的，放主循环会拖慢协议响应）。
 * 采集到的 PCM 按 Kconfig 里的分片时长切片后通过回调送出。
 */
esp_err_t bot_audio_capture_start(void);
void bot_audio_capture_stop(void);
bool bot_audio_capture_active(void);

/*
 * 常开麦克风：让采集任务一直跑，从而让唤醒词引擎（monitor 旁路）
 * **持续**拿到音频。
 *
 * 为什么需要它：唤醒词的意义是"平时安静地听，喊一声就醒"。
 * 早期实现把采集任务绑在 listen 会话的启停上（麦克风平时静音），
 * 结果唤醒词**只在已经处于采集状态时**才生效 —— 那时用户早就
 * 主动按了按钮，唤醒词毫无用处。实测：设备空闲时对着它喊
 * 「Hi 小星」，串口毫无反应。
 *
 * 调用后麦克风一直开，但**不会**把音频上传给 PC ——
 * 上行由 bot_audio_set_uplink() 单独控制。
 */
esp_err_t bot_audio_start_monitor(void);

/*
 * 音频上行开关：只有它为 true 时，PCM 才会通过 capture 回调送给上层
 * （进而经 WebSocket 上传给 PC）。
 *
 * 必须与"麦克风常开"分开：常开是为了唤醒词，上行是为了采集会话。
 * 若混为一谈，设备会把环境声音一直推给 PC，白耗带宽与 PC 算力。
 */
void bot_audio_set_uplink(bool enable);
bool bot_audio_uplink_active(void);

/* ------------------------------------------------------------------ */
/* 播放                                                               */
/* ------------------------------------------------------------------ */

/*
 * 播放一段音频。
 *
 * @param data         音频数据
 * @param len          字节数
 * @param is_wav       true 表示是 WAV 容器（会解析头部取采样率/声道）；
 *                     false 表示是 16bit 小端裸 PCM，用 sample_rate 参数
 * @param sample_rate  裸 PCM 时的采样率；WAV 时忽略
 * @return ESP_OK 已开始播放（异步，播完触发 done 回调）
 *
 * 播放是**非阻塞**的：数据被拷进内部缓冲，由播放任务消费。
 * 这样协议层收到 play_audio 后能立刻回 result，而不是
 * 等整段音频播完（那段可能好几秒，PC 侧会超时）。
 */
esp_err_t bot_audio_play(const uint8_t *data, size_t len, bool is_wav, int sample_rate);

/*
 * 直接播放 16bit 单声道 PCM 采样（已按板子采样率对齐，不做重采样）。
 * bot_audio_play 内部重采样后会调它；上层一般不需要直接用。
 */
esp_err_t bot_audio_play_raw_i16(const int16_t *samples, size_t count);

/* 立刻停止播放 */
void bot_audio_stop_playback(void);

bool bot_audio_playing(void);

/* 播放一段正弦音调（非阻塞）。用于"叮"这类即时反馈。 */
esp_err_t bot_audio_play_tone(int freq_hz, int duration_ms, int volume_percent);

/* 设置音量 0~100 */
void bot_audio_set_volume(int percent);
int bot_audio_get_volume(void);

/* 采样率（写进 hello 的 audio 字段） */
int bot_audio_sample_rate(void);

#ifdef __cplusplus
}
#endif
