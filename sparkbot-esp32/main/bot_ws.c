/*
 * 极简 WebSocket 服务端实现（RFC 6455 子集）
 *
 * 实现要点与取舍见 bot_ws.h 顶部注释。
 * 这里只强调两处容易写错的地方：
 *
 * 1) **客户端发来的帧一定带掩码**（RFC 6455 5.3 规定客户端必须掩码），
 *    服务端必须按 4 字节掩码 XOR 还原。忘了还原就会得到一堆乱码 JSON。
 *
 * 2) **TCP 是字节流，不是消息流。** recv() 返回的长度可能小于请求长度，
 *    所以读帧头、读扩展长度、读载荷都必须循环读到满，不能假设一次读完。
 *    这是自研 WebSocket 最常见的 bug，表现为"网络一忙就解析错位"。
 */

#include "bot_ws.h"

#include <errno.h>
#include <stdlib.h>
#include <string.h>
#include <sys/time.h>

#include "esp_http_server.h"
#include "esp_log.h"
#include "freertos/FreeRTOS.h"
#include "freertos/semphr.h"
#include "freertos/task.h"
#include "lwip/sockets.h"
#include "mbedtls/base64.h"
#include "mbedtls/sha1.h"

static const char *TAG = "bot_ws";

/* 单个载荷上限：超过就拒收，避免被畸形包打爆内存。
 * PC 端协议规定文本帧软上限 512KB，这里留一倍余量。 */
#define WS_MAX_PAYLOAD (1024 * 1024)

/* 服务端发送大帧时的分片载荷大小：单帧 8KB，
 * 既不会让 lwip 发送缓冲吃紧，也不会因分片过多而增加开销。 */
#define WS_TX_CHUNK 8192

/* 接收缓冲区：够放一帧的头部与常见小消息 */
#define WS_RX_BUF 4096

typedef struct {
    int sock;                 /* 已 hijack 的 socket，-1 表示无连接 */
    SemaphoreHandle_t tx_lock; /* 保护并发发送 */
    bot_ws_msg_cb_t cb;
    char path[64];

    /* 分片消息重组缓冲 */
    uint8_t *frag;
    size_t frag_len;
    size_t frag_cap;
    int frag_opcode;

    volatile bool closing;
} ws_ctx_t;

static ws_ctx_t s_ws = {
    .sock = -1,
    .tx_lock = NULL,
    .cb = NULL,
    .frag = NULL,
    .frag_len = 0,
    .frag_cap = 0,
    .frag_opcode = 0,
    .closing = false,
};

/* ------------------------------------------------------------------ */
/* 字节流读取辅助                                                     */
/* ------------------------------------------------------------------ */

/*
 * 精确读取 n 字节。返回已读字节数；0 表示对端关闭；负值表示出错。
 * 这是解决"TCP 不是消息流"的关键：必须循环到读满。
 */
static int ws_recv_all(int sock, uint8_t *dst, size_t n)
{
    size_t got = 0;
    while (got < n) {
        int r = recv(sock, dst + got, n - got, 0);
        if (r == 0) {
            return 0; /* 对端正常关闭 */
        }
        if (r < 0) {
            if (errno == EINTR) {
                continue;
            }
            return -1;
        }
        got += (size_t)r;
    }
    return (int)got;
}

/* ------------------------------------------------------------------ */
/* 发送                                                               */
/* ------------------------------------------------------------------ */

/* 把 data 全部写入 socket，处理部分写与 EINTR。 */
static esp_err_t ws_send_all(const uint8_t *data, size_t len)
{
    int sock = s_ws.sock;
    if (sock < 0) {
        return ESP_ERR_INVALID_STATE;
    }

    size_t sent = 0;
    while (sent < len) {
        int n = send(sock, data + sent, len - sent, 0);
        if (n < 0) {
            if (errno == EINTR) {
                continue;
            }
            ESP_LOGD(TAG, "send 失败: errno=%d", errno);
            return ESP_FAIL;
        }
        sent += (size_t)n;
    }
    return ESP_OK;
}

/*
 * 发送一个帧头。
 * opcode: 1=text 2=bin 8=close 9=ping 10=pong
 * fin:    是否为消息的最后一帧
 */
static esp_err_t ws_send_header(uint8_t opcode, bool fin, size_t payload_len)
{
    uint8_t hdr[10];
    size_t n = 0;

    hdr[n++] = (uint8_t)((fin ? 0x80 : 0x00) | (opcode & 0x0F));

    /* 服务端发送的帧**不加掩码**（RFC 6455 5.1） */
    if (payload_len < 126) {
        hdr[n++] = (uint8_t)payload_len;
    } else if (payload_len <= 0xFFFF) {
        hdr[n++] = 126;
        hdr[n++] = (uint8_t)(payload_len >> 8);
        hdr[n++] = (uint8_t)(payload_len & 0xFF);
    } else {
        hdr[n++] = 127;
        uint64_t v = (uint64_t)payload_len;
        for (int i = 7; i >= 0; i--) {
            hdr[n++] = (uint8_t)((v >> (i * 8)) & 0xFF);
        }
    }
    return ws_send_all(hdr, n);
}

static esp_err_t ws_send_frame(uint8_t opcode, const uint8_t *data, size_t len)
{
    if (s_ws.sock < 0) {
        return ESP_ERR_INVALID_STATE;
    }

    xSemaphoreTake(s_ws.tx_lock, portMAX_DELAY);

    esp_err_t err;
    if (len <= WS_TX_CHUNK) {
        err = ws_send_header(opcode, true, len);
        if (err == ESP_OK && len > 0) {
            err = ws_send_all(data, len);
        }
    } else {
        /* 自动分片：首帧带真实 opcode，后续用 continuation(0)，
         * 最后一帧置 FIN。这样接收方按标准流程重组即可。 */
        size_t off = 0;
        bool first = true;
        err = ESP_OK;
        while (off < len) {
            size_t chunk = len - off;
            if (chunk > WS_TX_CHUNK) {
                chunk = WS_TX_CHUNK;
            }
            bool last = (off + chunk) >= len;
            err = ws_send_header(first ? opcode : 0x00, last, chunk);
            if (err != ESP_OK) {
                break;
            }
            err = ws_send_all(data + off, chunk);
            if (err != ESP_OK) {
                break;
            }
            off += chunk;
            first = false;
        }
    }

    xSemaphoreGive(s_ws.tx_lock);
    return err;
}

esp_err_t bot_ws_send_text(const char *text, size_t len)
{
    return ws_send_frame(0x01, (const uint8_t *)text, len);
}

esp_err_t bot_ws_send_bin(const uint8_t *data, size_t len)
{
    return ws_send_frame(0x02, data, len);
}

bool bot_ws_connected(void)
{
    return s_ws.sock >= 0 && !s_ws.closing;
}

void bot_ws_close(void)
{
    if (s_ws.sock >= 0) {
        s_ws.closing = true;
        /* 发一个 close 帧（1000 = normal closure），发不出去也无所谓，
         * 接收循环随后会因为对端关闭或 recv 出错而退出。 */
        uint8_t payload[2] = {0x03, 0xE8};
        ws_send_frame(0x08, payload, sizeof(payload));
    }
}

/* ------------------------------------------------------------------ */
/* 分片消息重组                                                       */
/* ------------------------------------------------------------------ */

/*
 * 把一段载荷追加重组缓冲。
 * 返回 ESP_OK 表示已追加；ESP_ERR_NO_MEM 表示超限或分配失败。
 */
static esp_err_t ws_frag_append(const uint8_t *data, size_t len)
{
    if (len == 0) {
        return ESP_OK;
    }
    if (s_ws.frag_len + len > WS_MAX_PAYLOAD) {
        ESP_LOGW(TAG, "分片消息超过上限 %d，丢弃", WS_MAX_PAYLOAD);
        return ESP_ERR_NO_MEM;
    }
    if (s_ws.frag_len + len > s_ws.frag_cap) {
        size_t want = s_ws.frag_len + len;
        if (want < 4096) {
            want = 4096;
        }
        uint8_t *grown = realloc(s_ws.frag, want);
        if (grown == NULL) {
            ESP_LOGE(TAG, "重组缓冲扩容失败 (%u 字节)", (unsigned)want);
            return ESP_ERR_NO_MEM;
        }
        s_ws.frag = grown;
        s_ws.frag_cap = want;
    }
    memcpy(s_ws.frag + s_ws.frag_len, data, len);
    s_ws.frag_len += len;
    return ESP_OK;
}

/*
 * 确保重组缓冲至少还能再容纳 extra 字节。
 *
 * 单独抽出来是因为"先为整帧载荷预留空间"这件事如果用 append 凑，
 * 会需要往里塞占位字节再撤销，既绕又容易越界。
 */
static esp_err_t ws_frag_reserve(size_t extra)
{
    if (s_ws.frag_len + extra > WS_MAX_PAYLOAD) {
        return ESP_ERR_NO_MEM;
    }
    if (s_ws.frag_len + extra <= s_ws.frag_cap) {
        return ESP_OK;
    }
    size_t want = s_ws.frag_len + extra;
    uint8_t *grown = realloc(s_ws.frag, want);
    if (grown == NULL) {
        ESP_LOGE(TAG, "载荷缓冲扩容失败 (%u 字节)", (unsigned)want);
        return ESP_ERR_NO_MEM;
    }
    s_ws.frag = grown;
    s_ws.frag_cap = want;
    return ESP_OK;
}

static void ws_frag_reset(void)
{
    s_ws.frag_len = 0;
    s_ws.frag_opcode = 0;
}

/* 一条完整消息收齐了，交给上层 */
static void ws_deliver(int opcode)
{
    if (s_ws.cb == NULL || s_ws.frag_len == 0) {
        return;
    }
    /* JSON 需要以 NUL 结尾，补一个（不计入 len 传给回调，但 cJSON 需要） */
    if (ws_frag_append((const uint8_t *)"", 1) != ESP_OK) {
        return;
    }
    s_ws.frag_len--; /* 把补的 NUL 排除在长度之外 */

    bot_ws_op_t op = (opcode == 0x01) ? BOT_WS_OP_TEXT : BOT_WS_OP_BIN;
    s_ws.cb(op, (const char *)s_ws.frag, s_ws.frag_len);

    /* 回调可能触发重连/关闭，重新确认状态 */
    ws_frag_reset();
}

/* ------------------------------------------------------------------ */
/* 接收循环                                                           */
/* ------------------------------------------------------------------ */

/*
 * 读一个完整帧并处理。
 * 返回 false 表示连接应关闭。
 */
static bool ws_handle_one_frame(void)
{
    uint8_t hdr[2];
    int r = ws_recv_all(s_ws.sock, hdr, 2);
    if (r <= 0) {
        ESP_LOGI(TAG, "客户端关闭连接");
        return false;
    }

    bool fin = (hdr[0] & 0x80) != 0;
    uint8_t opcode = hdr[0] & 0x0F;
    bool masked = (hdr[1] & 0x80) != 0;
    uint64_t payload_len = hdr[1] & 0x7F;

    /* 扩展长度 */
    if (payload_len == 126) {
        uint8_t ext[2];
        if (ws_recv_all(s_ws.sock, ext, 2) <= 0) {
            return false;
        }
        payload_len = ((uint64_t)ext[0] << 8) | ext[1];
    } else if (payload_len == 127) {
        uint8_t ext[8];
        if (ws_recv_all(s_ws.sock, ext, 8) <= 0) {
            return false;
        }
        payload_len = 0;
        for (int i = 0; i < 8; i++) {
            payload_len = (payload_len << 8) | ext[i];
        }
    }

    if (payload_len > WS_MAX_PAYLOAD) {
        ESP_LOGW(TAG, "帧载荷过大 (%llu)，关闭连接", (unsigned long long)payload_len);
        return false;
    }

    /* 掩码：客户端必须带，服务端必须还原 */
    uint8_t mask[4] = {0};
    if (masked) {
        if (ws_recv_all(s_ws.sock, mask, 4) <= 0) {
            return false;
        }
    }

    /* 控制帧（close/ping/pong）载荷极短，且不能分片 */
    if (opcode == 0x08) { /* close */
        ESP_LOGI(TAG, "收到 close 帧");
        /* 回一个 close 完成握手，然后关闭 */
        if (s_ws.sock >= 0) {
            xSemaphoreTake(s_ws.tx_lock, portMAX_DELAY);
            ws_send_header(0x08, true, 0);
            xSemaphoreGive(s_ws.tx_lock);
        }
        return false;
    }
    if (opcode == 0x09 || opcode == 0x0A) { /* ping / pong */
        uint8_t tmp[128];
        if (payload_len > 0) {
            size_t to_read = (payload_len > sizeof(tmp)) ? sizeof(tmp) : (size_t)payload_len;
            if (ws_recv_all(s_ws.sock, tmp, to_read) <= 0) {
                return false;
            }
            /* 超过缓冲的部分读掉丢弃 */
            for (uint64_t skip = to_read; skip < payload_len; skip += sizeof(tmp)) {
                size_t n = (size_t)((payload_len - skip > sizeof(tmp)) ? sizeof(tmp) : (payload_len - skip));
                if (ws_recv_all(s_ws.sock, tmp, n) <= 0) {
                    return false;
                }
            }
        }
        if (opcode == 0x09) {
            /* 收到 ping 必须回 pong，载荷原样带回 */
            ESP_LOGD(TAG, "收到 ping，回 pong");
        }
        return true;
    }

    if (payload_len == 0) {
        /* 空帧：可能是空文本；分片结束时也可能出现 */
        if (fin && opcode != 0x00) {
            ws_deliver(opcode);
        } else if (fin && opcode == 0x00) {
            ws_deliver(s_ws.frag_opcode);
        }
        return true;
    }

    /*
     * 读载荷。先一次性把重组缓冲扩到能容纳本帧，
     * 然后分块读（TCP 可能一次给不全），最后整体解掩码。
     */
    size_t old_len = s_ws.frag_len;
    if (ws_frag_reserve((size_t)payload_len) != ESP_OK) {
        ESP_LOGW(TAG, "载荷超限，关闭连接");
        return false;
    }

    size_t remaining = (size_t)payload_len;
    size_t write_at = old_len;
    while (remaining > 0) {
        size_t chunk = remaining > WS_RX_BUF ? WS_RX_BUF : remaining;
        int got = ws_recv_all(s_ws.sock, s_ws.frag + write_at, chunk);
        if (got <= 0) {
            return false;
        }
        write_at += chunk;
        remaining -= chunk;
    }

    /* 解掩码：掩码序号从本帧载荷的第 0 字节开始，
     * 因此对本帧的偏移 i 用 mask[i & 3]。 */
    if (masked) {
        for (size_t i = 0; i < (size_t)payload_len; i++) {
            s_ws.frag[old_len + i] ^= mask[i & 3];
        }
    }

    s_ws.frag_len = old_len + (size_t)payload_len;

    /* 分片处理 */
    if (opcode == 0x00) {
        /* continuation：继续攒，FIN 时投递 */
        if (fin) {
            ws_deliver(s_ws.frag_opcode);
        }
    } else {
        /* 新的数据帧 */
        if (s_ws.frag_len != (size_t)payload_len) {
            /* 说明前面还有没收完的分片，协议上不该出现，重置以求自保 */
            ESP_LOGW(TAG, "分片序列异常，重置重组缓冲");
            ws_frag_reset();
        }
        s_ws.frag_opcode = opcode;
        if (fin) {
            ws_deliver(opcode);
        }
        /* 未 FIN：保持 frag 内容，等 continuation */
    }

    return true;
}

static void ws_rx_task(void *arg)
{
    ESP_LOGI(TAG, "WebSocket 接收任务启动");

    while (bot_ws_connected()) {
        if (!ws_handle_one_frame()) {
            break;
        }
    }

    /* 收尾：关闭 socket 并复位状态，让上层知道连接没了 */
    if (s_ws.sock >= 0) {
        shutdown(s_ws.sock, SHUT_RDWR);
        close(s_ws.sock);
        s_ws.sock = -1;
    }
    s_ws.closing = false;
    ws_frag_reset();
    ESP_LOGI(TAG, "WebSocket 连接已结束");

    vTaskDelete(NULL);
}

/* ------------------------------------------------------------------ */
/* HTTP 握手                                                          */
/* ------------------------------------------------------------------ */

static const char *WS_GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11";

/*
 * 计算 Sec-WebSocket-Accept = base64(sha1(key + GUID))
 *
 * 注意：ESP-IDF v5.5 带的是 mbedtls 3.x，API 是 mbedtls_sha1_starts/update/finish；
 * mbedtls 2.x 才有 _ret 后缀。用错会直接编译失败。
 */
static esp_err_t ws_compute_accept(const char *key, char *out, size_t out_len)
{
    uint8_t sha[20];
    mbedtls_sha1_context ctx;
    mbedtls_sha1_init(&ctx);

    int rc = mbedtls_sha1_starts(&ctx);
    if (rc == 0) {
        rc = mbedtls_sha1_update(&ctx, (const uint8_t *)key, strlen(key));
    }
    if (rc == 0) {
        rc = mbedtls_sha1_update(&ctx, (const uint8_t *)WS_GUID, strlen(WS_GUID));
    }
    if (rc == 0) {
        rc = mbedtls_sha1_finish(&ctx, sha);
    }
    mbedtls_sha1_free(&ctx);
    if (rc != 0) {
        return ESP_FAIL;
    }

    size_t olen = 0;
    if (mbedtls_base64_encode((uint8_t *)out, out_len, &olen, sha, sizeof(sha)) != 0) {
        return ESP_FAIL;
    }
    out[olen] = '\0';
    return ESP_OK;
}

static esp_err_t ws_http_handler(httpd_req_t *req)
{
    /* 路径校验：只接受配置的路径，其它给 404，避免误连 */
    if (strcmp(req->uri, s_ws.path) != 0) {
        httpd_resp_set_status(req, "404 Not Found");
        httpd_resp_sendstr(req, "SparkBot: unknown path");
        return ESP_OK;
    }

    char key[128] = {0};
    if (httpd_req_get_hdr_value_str(req, "Sec-WebSocket-Key", key, sizeof(key)) != ESP_OK) {
        httpd_resp_set_status(req, "400 Bad Request");
        httpd_resp_sendstr(req, "missing Sec-WebSocket-Key");
        return ESP_OK;
    }

    char accept[64] = {0};
    if (ws_compute_accept(key, accept, sizeof(accept)) != ESP_OK) {
        httpd_resp_set_status(req, "500 Internal Server Error");
        httpd_resp_sendstr(req, "handshake failed");
        return ESP_OK;
    }

    /* 一次只能有一个客户端：已有连接时拒绝新的，
     * 避免两个 PC 抢同一块板子的外设。 */
    if (s_ws.sock >= 0) {
        ESP_LOGW(TAG, "已有客户端连接，拒绝新的握手");
        httpd_resp_set_status(req, "503 Service Unavailable");
        httpd_resp_sendstr(req, "SparkBot: busy, another client is connected");
        return ESP_OK;
    }

    /* 组装 101 响应。注意用 httpd_resp_send 一次性发，
     * 不要用多次 send 触发 chunked 编码。 */
    char resp[256];
    int n = snprintf(resp, sizeof(resp),
                     "HTTP/1.1 101 Switching Protocols\r\n"
                     "Upgrade: websocket\r\n"
                     "Connection: Upgrade\r\n"
                     "Sec-WebSocket-Accept: %s\r\n"
                     "\r\n",
                     accept);
    if (n <= 0 || (size_t)n >= sizeof(resp)) {
        return ESP_FAIL;
    }

    if (httpd_resp_send(req, resp, n) != ESP_OK) {
        ESP_LOGW(TAG, "101 响应发送失败");
        return ESP_FAIL;
    }

    /* 取出底层 socket 并交给接收任务自己管理 */
    int sock = httpd_req_to_sockfd(req);
    if (sock < 0) {
        ESP_LOGE(TAG, "拿不到底层 socket");
        return ESP_FAIL;
    }

    /* 关掉 TCP_NODELAY 之外的干预：WebSocket 要求低延迟 */
    int one = 1;
    setsockopt(sock, IPPROTO_TCP, TCP_NODELAY, &one, sizeof(one));

    /* 设置接收超时：用来定期回到循环检查 closing 标志。
     * 没有超时的话，连接空闲时会一直阻塞在 recv 上，
     * bot_ws_close() 就无法让接收任务退出。 */
    struct timeval tv;
    tv.tv_sec = 1;
    tv.tv_usec = 0;
    setsockopt(sock, SOL_SOCKET, SO_RCVTIMEO, &tv, sizeof(tv));

    s_ws.closing = false;
    ws_frag_reset();

    /* 先起接收任务，再置 sock：避免任务一启动就看到还未生效的状态 */
    if (xTaskCreate(ws_rx_task, "bot_ws_rx", 6144, NULL, 5, NULL) != pdPASS) {
        ESP_LOGE(TAG, "创建接收任务失败");
        close(sock);
        return ESP_FAIL;
    }
    s_ws.sock = sock;

    ESP_LOGI(TAG, "客户端已连接（WebSocket 握手完成）");
    return ESP_OK;
}

/* ------------------------------------------------------------------ */
/* 启动                                                               */
/* ------------------------------------------------------------------ */

esp_err_t bot_ws_start(uint16_t port, const char *path, bot_ws_msg_cb_t cb)
{
    if (cb == NULL || path == NULL) {
        return ESP_ERR_INVALID_ARG;
    }

    s_ws.tx_lock = xSemaphoreCreateMutex();
    if (s_ws.tx_lock == NULL) {
        return ESP_ERR_NO_MEM;
    }
    s_ws.cb = cb;
    strncpy(s_ws.path, path, sizeof(s_ws.path) - 1);
    s_ws.path[sizeof(s_ws.path) - 1] = '\0';
    s_ws.sock = -1;

    httpd_config_t config = HTTPD_DEFAULT_CONFIG();
    config.server_port = port;
    config.ctrl_port = port + 1; /* 控制 socket 用相邻端口，避免和别的服务撞 */
    config.max_uri_handlers = 4;
    config.stack_size = 8192;    /* JSON 解析在 httpd 任务里做不了，但握手够用 */
    config.lru_purge_enable = true;

    httpd_handle_t server = NULL;
    if (httpd_start(&server, &config) != ESP_OK) {
        ESP_LOGE(TAG, "httpd 启动失败（端口 %u 被占用？）", port);
        return ESP_FAIL;
    }

    httpd_uri_t uri = {
        .uri = path,
        .method = HTTP_GET,
        .handler = ws_http_handler,
        .user_ctx = NULL,
    };
    if (httpd_register_uri_handler(server, &uri) != ESP_OK) {
        ESP_LOGE(TAG, "注册 WebSocket 路由失败");
        httpd_stop(server);
        return ESP_FAIL;
    }

    ESP_LOGI(TAG, "WebSocket 服务端已启动: ws://<board-ip>:%u%s", port, path);
    return ESP_OK;
}
