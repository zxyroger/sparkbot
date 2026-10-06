/*
 * 极简 WebSocket 服务端（RFC 6455 子集）
 *
 * 为什么自己写而不是用第三方组件：
 *   ESP-IDF 自带 esp_http_server，但它只到 HTTP 层，WebSocket 需要
 *   自己做 Upgrade 握手与帧编解码。官方没有内置 WebSocket 服务端，
 *   而第三方组件要么依赖 arduino、要么引入一堆不需要的功能。
 *   机器人只对接一个自研 PC 客户端，需要的其实是 RFC 6455 的一个
 *   很小的子集，自己实现反而更可控（也便于把每处边界都写清楚）。
 *
 * 支持范围：
 *   - HTTP GET + Upgrade 握手（Sec-WebSocket-Accept 用 mbedtls 算）
 *   - 文本帧收发、二进制帧接收
 *   - 分片消息重组（PC 端的 JSON 可能超过单个 TCP 段）
 *   - ping/pong 与 close
 *   - 发送侧自动分片（大帧拆成多个 continuation 帧）
 *
 * 不支持（本场景不需要）：
 *   - 扩展协商（permessage-deflate 等）
 *   - 服务端主动发起握手
 *   - 多客户端并发（板子一次只服务一个 PC）
 */

#pragma once

#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>

#include "esp_err.h"

#ifdef __cplusplus
extern "C" {
#endif

/* 收到的消息类型 */
typedef enum {
    BOT_WS_OP_TEXT = 1,  /* 完整文本消息（已重组） */
    BOT_WS_OP_BIN = 2,   /* 完整二进制消息（已重组） */
} bot_ws_op_t;

/* 收到一条完整消息时的回调。
 *
 * data 指向内部缓冲区，**回调返回后立即失效**，需要留存必须自己拷走。
 * 回调应尽量快：它在 WebSocket 任务里同步执行，
 * 长时间阻塞会拖慢 ping/pong 的响应，进而被 PC 判定掉线。
 */
typedef void (*bot_ws_msg_cb_t)(bot_ws_op_t op, const char *data, size_t len);

/*
 * 启动 WebSocket 服务端。
 *
 * @param port   监听端口
 * @param path   接受的 URL 路径，例如 "/robot"；其它路径返回 404
 * @param cb     收到完整消息时的回调
 * @return ESP_OK 成功
 */
esp_err_t bot_ws_start(uint16_t port, const char *path, bot_ws_msg_cb_t cb);

/* 当前是否有客户端已握手完成 */
bool bot_ws_connected(void);

/*
 * 发送一条文本消息（内部按需分片）。
 *
 * 线程安全：内部有互斥锁，可以从协议引擎、音频任务、摄像头任务并发调用。
 * 没有客户端连接时返回 ESP_ERR_INVALID_STATE，调用方应据此停止推流。
 */
esp_err_t bot_ws_send_text(const char *text, size_t len);

/* 发送一条二进制消息。目前仅用于可选的原始音频透传。 */
esp_err_t bot_ws_send_bin(const uint8_t *data, size_t len);

/* 主动关闭当前连接（例如 hello_ack 回了 ok=false） */
void bot_ws_close(void);

#ifdef __cplusplus
}
#endif
