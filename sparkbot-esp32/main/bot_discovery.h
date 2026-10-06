/*
 * 服务器自动发现（见 bot_discovery.c 顶部的设计说明）。
 *
 * 对外只暴露四件事：
 *   * 找一次（会阻塞几秒，每轮约 1.2 秒）
 *   * 问"找到了吗"
 *   * 问"找到的 IP 是什么"
 *   * 清掉结果（WiFi 重连后要重新发现）
 */

#pragma once

#include <stdbool.h>
#include <stdint.h>

#ifdef __cplusplus
extern "C" {
#endif

/*
 * 用 UDP 广播找 PC 端服务，成功则把 IP 记下来。
 *
 * 这是**阻塞**调用：最多 max_attempts 轮，每轮约 0.8 秒等待 + 0.4 秒间隔。
 * 默认 8 轮，最坏约 9.6 秒。放在 WiFi 刚连上时调用一次是合适的；
 * 不要在消息循环里调用。
 *
 * port 传 0 用默认 8765；max_attempts 传 0 用默认 8 次。
 *
 * 返回 true 表示找到。
 */
bool bot_discovery_find_server(uint16_t port, int max_attempts);

/* 是否已经发现过服务器 */
bool bot_discovery_found(void);

/* 返回发现到的 IP 字符串；没发现时返回 NULL（调用方应回退到配置的 IP） */
const char *bot_discovery_host(void);

/* 清空发现结果（WiFi 断开/重连时调用，避免用已经失效的旧 IP） */
void bot_discovery_clear(void);

/* 本机 IP（用于推算子网广播地址）；失败返回 NULL */
const char *bot_discovery_local_ip(void);

#ifdef __cplusplus
}
#endif
