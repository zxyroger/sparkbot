/*
 * 协议引擎：SparkBot 协议的设备侧实现
 *
 * 职责：
 *   - 连接建立后发 hello（能力协商），等 hello_ack
 *   - 按心跳周期发 telemetry（电量、运动状态等）
 *   - 收 command → 执行 → 回 result（同 id）
 *   - 收 intent → 执行但不回 result
 *   - 收 ping → 回 pong
 *   - 对外提供发 event / frame / audio 的接口
 *
 * 执行动作的实际硬件操作全部委托给 bot_hw_* 模块；
 * 本文件只做「解析参数 → 校验 → 调硬件 → 组织回复」。
 *
 * 与 bot_ws.c 的分工：bot_ws 是**服务端**（本工程用不到，
 * 保留给"板子当 AP、PC 主动连"的变体）；板子对接 SparkBot 时
 * 走的是 bot_net.c 的**客户端**路径。协议引擎通过 bot_net 收发。
 */

#pragma once

#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>

#include "esp_err.h"

#ifdef __cplusplus
extern "C" {
#endif

/* 初始化协议引擎（准备状态、生成设备 id，不建连接） */
esp_err_t bot_proto_init(void);

/*
 * 连接建立后调用：发 hello 并等待 hello_ack。
 * @return ESP_OK 握手成功；其它表示失败（调用方应断开重连）
 */
esp_err_t bot_proto_handshake(void);

/* 连接断开时调用：复位状态 */
void bot_proto_on_disconnect(void);

/* 处理一条收到的文本消息（同步执行） */
void bot_proto_handle_message(const char *json, size_t len);

/* 主循环定期调用：到点就发遥测 */
void bot_proto_poll(void);

/*
 * 音频播完了（由音频任务的回调调用）。
 *
 * **只置一个标志，不在调用方发 WebSocket** —— 音频任务 bot_spk 的栈只有
 * 4096 字节，在里面构造并发送一帧 JSON 会栈溢出直接重启。真正的发送由
 * bot_proto_poll() 在主循环里完成。
 */
void bot_proto_notify_audio_done(void);

/* ------------------------------------------------------------------ */
/* 上行消息                                                           */
/* ------------------------------------------------------------------ */

/* 发一条异步事件。json_data 可为 NULL 表示空 data。 */
esp_err_t bot_proto_send_event(const char *event, const char *json_data);

/* 发一帧图像（jpeg 会被 base64 编码后放进 frame 消息） */
esp_err_t bot_proto_send_frame(const uint8_t *jpeg, size_t len,
                               uint16_t width, uint16_t height);

/* 音频分片上行：phase = "start" / "chunk" / "end" */
esp_err_t bot_proto_send_audio(const char *phase, const uint8_t *pcm, size_t len, int seq);

/* 麦克风上行开关（start_listen / stop_listen 动作里用） */
esp_err_t bot_proto_listen_start(int timeout_ms, bool wake_word);
esp_err_t bot_proto_listen_stop(void);
bool bot_proto_listening(void);

/* 设备 id 与名字（hello 用） */
const char *bot_proto_device_id(void);
const char *bot_proto_device_name(void);

#ifdef __cplusplus
}
#endif
