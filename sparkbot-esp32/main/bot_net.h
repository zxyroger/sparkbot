/*
 * 网络：WiFi STA 连接 + WebSocket 客户端（连 PC 端 SparkBot 服务）
 *
 * 这里的板子是**客户端**：
 * PC 起服务端，板子主动连上去（SparkBot 协议如此设计，
 * 这样 PC 不需要知道板子 IP，换网络也不用改配置）。
 */

#pragma once

#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>

#include "esp_err.h"

#ifdef __cplusplus
extern "C" {
#endif

/*
 * 启动 WiFi（STA）。使用 Kconfig 里的 SSID/密码。
 * 本函数非阻塞：连接在后台进行，通过 bot_net_is_connected() 查询状态。
 */
esp_err_t bot_net_start(void);

/* WiFi 是否已连上 AP 并拿到 IP */
bool bot_net_is_connected(void);

/* 当前 IP 字符串；未连接时返回 "0.0.0.0" */
const char *bot_net_ip_string(void);

/* ------------------------------------------------------------------ */
/* WebSocket 客户端                                                   */
/* ------------------------------------------------------------------ */

/*
 * 连接到 PC 端的 SparkBot 服务。
 *
 * 本函数会阻塞直到握手完成或超时（约 5 秒）。
 * 失败时返回错误，调用方负责延时后重试。
 */
esp_err_t bot_net_connect_server(void);

/* 断开当前 WebSocket 连接 */
void bot_net_disconnect(void);

/* 是否处于已连接状态 */
bool bot_net_is_connected_to_server(void);

/*
 * 发送一条文本消息（一次完整的 WebSocket 文本帧，客户端侧会加掩码）。
 *
 * 线程安全。没有连接时返回 ESP_ERR_INVALID_STATE。
 */
esp_err_t bot_net_send_text(const char *text, size_t len);

/*
 * 从连接上读取一条完整消息（阻塞，带超时）。
 *
 * 成功后 *out_buf 指向内部缓冲，长度写入 *out_len；
 * 缓冲区在下次调用本函数前有效。
 *
 * @param timeout_ms 超时毫秒数
 * @return ESP_OK 收到消息；ESP_ERR_TIMEOUT 超时（连接仍正常）；
 *         ESP_FAIL 连接已断开。
 */
esp_err_t bot_net_recv_text(char **out_buf, size_t *out_len, int timeout_ms);

/* ------------------------------------------------------------------ */
/* 服务器地址（供日志与 hello 上报）                                  */
/* ------------------------------------------------------------------ */
const char *bot_net_server_host(void);
uint16_t bot_net_server_port(void);
const char *bot_net_server_path(void);

#ifdef __cplusplus
}
#endif
