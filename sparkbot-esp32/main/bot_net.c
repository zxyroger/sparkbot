/*
 * 网络实现：WiFi STA + WebSocket 客户端
 *
 * 关于线程模型的说明（重要）：
 *
 *   本模块**不自己起接收任务**。原因有两个：
 *     1) 设备侧收到消息后要做的事（抓帧、驱动电机、读音频）大多是
 *        阻塞式或独占外设的，放在一个独立任务里反而要处理与主循环的
 *        互斥；让主循环同步收更简单也更可控。
 *     2) 板子只需要服务一个 PC，消息量不大，没必要引入额外任务与队列。
 *
 *   因此调用约定是：主循环调 bot_net_recv_text() 带超时收消息，
 *   超时（ESP_ERR_TIMEOUT）不算错误，继续循环即可 —— 这样主循环
 *   既能及时处理消息，又能顺带跑周期性工作（遥测、摄像头推流）。
 *
 * 发送侧有互斥锁，因为音频/摄像头任务可能和主循环并发发消息。
 */

#include "bot_net.h"
#include "bot_discovery.h"

#include <errno.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/time.h>

#include "esp_event.h"
#include "esp_log.h"
#include "esp_random.h"
#include "esp_timer.h"
#include "esp_wifi.h"
#include "freertos/FreeRTOS.h"
#include "freertos/event_groups.h"
#include "freertos/semphr.h"
#include "lwip/netdb.h"
#include "lwip/sockets.h"
#include "mbedtls/base64.h"
#include "mbedtls/sha1.h"
#include "nvs_flash.h"

static const char *TAG = "bot_net";

#define WIFI_CONNECTED_BIT BIT0
#define WIFI_FAIL_BIT      BIT1

/* WebSocket 客户端接收缓冲：一次最多读 4KB 网络数据 */
#define WS_RX_CHUNK 4096

/*
 * 单条 WebSocket 消息的上限。
 *
 * 为什么是 1MB 而不是 512KB：`play_audio` 送的音频要 **base64** 编码，
 * 体积会膨胀到 4/3 倍。512KB 的上限意味着**原始音频只能到 384KB**
 * （约 12 秒 16kHz 单声道 WAV）—— 稍微长一点的 TTS 回应就会被
 * 固件判定"消息过大，断开"，表现为设备无故掉线。
 *
 * 1MB 上限对应约 768KB 原始音频（约 24 秒），够大多数播报使用。
 *
 * ⚠️ 真正的解法是**音频分片传输**（PC 端把长音频切成多条 messages
 * 发送、设备拼起来播），但那是协议扩展，需要两端一起改。
 * 在那之前，PC 端应当把单次 play_audio 控制在
 * `WS_MAX_MSG * 3/4 * 0.9` 字节以内。
 */
#define WS_MAX_MSG  (1024 * 1024)

static EventGroupHandle_t s_wifi_events;
static int s_retry_count = 0;
static volatile bool s_wifi_connected = false;
static char s_ip_str[16] = "0.0.0.0";

/* WebSocket 连接状态 */
static int s_sock = -1;
static SemaphoreHandle_t s_tx_lock = NULL;

/*
 * 发送健康度统计。
 *
 * s_send_dropped      —— 因拥塞被丢弃的消息数（链路仍好）
 * s_send_fail_streak  —— 连续真失败次数，达到阈值才判定断开
 *
 * 分开统计的目的：让"拥塞丢帧"和"链路真断"在日志上可区分。
 * 旧实现把两者混在一起，导致一次拥塞就让屏幕显示 LINK LOST。
 */
static volatile uint32_t s_send_dropped = 0;
static volatile uint32_t s_send_fail_streak = 0;

/*: 连续多少次真失败才判定链路断开。给足余量，避免误判。 */
#define SEND_FAIL_STREAK_LIMIT 5

/*
 * 接收一整帧的时间预算（毫秒）。
 *
 * 一帧 TTS 音频 base64 后有 250KB 以上，在 WiFi 上读完可能要好几百毫秒
 * 甚至更久。给足预算才能在网络抖动时把整帧收完，而不是中途放弃并误判
 * 成"对端关闭"（那会让设备自己掐断连接）。
 *
 * 15 秒远大于正常所需的几百毫秒，又远小于 PC 端 90 秒的判死阈值，
 * 所以既不会误断，也不会把真断的情况拖太久。
 */
#define WS_RECV_FRAME_BUDGET_MS 15000

/* 接收侧：分片重组缓冲 */
static uint8_t *s_rx;
static size_t s_rx_len;
static size_t s_rx_cap;
static int s_rx_opcode;

/* ------------------------------------------------------------------ */
/* WiFi                                                               */
/* ------------------------------------------------------------------ */

static void wifi_event_handler(void *arg, esp_event_base_t base, int32_t id, void *data)
{
    if (base == WIFI_EVENT && id == WIFI_EVENT_STA_START) {
        esp_wifi_connect();
    } else if (base == WIFI_EVENT && id == WIFI_EVENT_STA_DISCONNECTED) {
        s_wifi_connected = false;
        wifi_event_sta_disconnected_t *ev = (wifi_event_sta_disconnected_t *)data;
        if (s_retry_count < CONFIG_SPARKBOT_WIFI_MAX_RETRY) {
            s_retry_count++;
            ESP_LOGW(TAG, "WiFi 断开 (reason=%d)，第 %d 次重连", ev->reason, s_retry_count);
            /* 断开后旧的发现结果可能已经失效（PC 或本机换了网段），清掉重来 */
            bot_discovery_clear();
            esp_wifi_connect();
        } else {
            ESP_LOGE(TAG, "WiFi 重连次数用尽，等待手动恢复");
            xEventGroupSetBits(s_wifi_events, WIFI_FAIL_BIT);
        }
    } else if (base == IP_EVENT && id == IP_EVENT_STA_GOT_IP) {
        ip_event_got_ip_t *ev = (ip_event_got_ip_t *)data;
        snprintf(s_ip_str, sizeof(s_ip_str), IPSTR, IP2STR(&ev->ip_info.ip));
        s_retry_count = 0;
        s_wifi_connected = true;
        ESP_LOGI(TAG, "WiFi 已连接，IP = %s", s_ip_str);
        xEventGroupSetBits(s_wifi_events, WIFI_CONNECTED_BIT);
    }
}

esp_err_t bot_net_start(void)
{
    if (CONFIG_SPARKBOT_WIFI_SSID[0] == '\0') {
        ESP_LOGE(TAG, "未配置 WiFi SSID —— 请在 menuconfig -> SparkBot 固件配置 -> 网络 里填写");
        return ESP_ERR_INVALID_STATE;
    }

    /* NVS 是 WiFi 驱动存放校准数据的地方，必须先初始化 */
    esp_err_t err = nvs_flash_init();
    if (err == ESP_ERR_NVS_NO_FREE_PAGES || err == ESP_ERR_NVS_NEW_VERSION_FOUND) {
        ESP_ERROR_CHECK(nvs_flash_erase());
        err = nvs_flash_init();
    }
    if (err != ESP_OK) {
        return err;
    }

    s_wifi_events = xEventGroupCreate();
    s_tx_lock = xSemaphoreCreateMutex();
    if (s_wifi_events == NULL || s_tx_lock == NULL) {
        return ESP_ERR_NO_MEM;
    }

    ESP_ERROR_CHECK(esp_netif_init());
    ESP_ERROR_CHECK(esp_event_loop_create_default());
    esp_netif_create_default_wifi_sta();

    wifi_init_config_t cfg = WIFI_INIT_CONFIG_DEFAULT();
    ESP_ERROR_CHECK(esp_wifi_init(&cfg));

    ESP_ERROR_CHECK(esp_event_handler_instance_register(WIFI_EVENT, ESP_EVENT_ANY_ID,
                                                        wifi_event_handler, NULL, NULL));
    ESP_ERROR_CHECK(esp_event_handler_instance_register(IP_EVENT, IP_EVENT_STA_GOT_IP,
                                                        wifi_event_handler, NULL, NULL));

    wifi_config_t wifi_cfg = {0};
    strncpy((char *)wifi_cfg.sta.ssid, CONFIG_SPARKBOT_WIFI_SSID, sizeof(wifi_cfg.sta.ssid) - 1);
    strncpy((char *)wifi_cfg.sta.password, CONFIG_SPARKBOT_WIFI_PASSWORD,
            sizeof(wifi_cfg.sta.password) - 1);
    wifi_cfg.sta.threshold.authmode = WIFI_AUTH_WPA2_PSK;

    ESP_ERROR_CHECK(esp_wifi_set_mode(WIFI_MODE_STA));
    ESP_ERROR_CHECK(esp_wifi_set_config(WIFI_IF_STA, &wifi_cfg));
    ESP_ERROR_CHECK(esp_wifi_start());

    /* 发射功率：摄像头 + WiFi 同时工作时电流尖峰很大，
     * 调低一点能明显减少小电池供电时的掉电重启。 */
    ESP_ERROR_CHECK(esp_wifi_set_max_tx_power(CONFIG_SPARKBOT_WIFI_TX_POWER_DBM * 4));

#if CONFIG_SPARKBOT_WIFI_POWER_SAVE
    ESP_ERROR_CHECK(esp_wifi_set_ps(WIFI_PS_MIN_MODEM));
#else
    ESP_ERROR_CHECK(esp_wifi_set_ps(WIFI_PS_NONE));
#endif

    ESP_LOGI(TAG, "WiFi 启动中，SSID=%s 发射功率=%ddBm", CONFIG_SPARKBOT_WIFI_SSID,
             CONFIG_SPARKBOT_WIFI_TX_POWER_DBM);

    /* 等第一次连上（最多 20 秒），之后交给事件回调处理重连 */
    EventBits_t bits = xEventGroupWaitBits(s_wifi_events,
                                           WIFI_CONNECTED_BIT | WIFI_FAIL_BIT,
                                           pdFALSE, pdFALSE, pdMS_TO_TICKS(20000));
    if (bits & WIFI_CONNECTED_BIT) {
        return ESP_OK;
    }

    ESP_LOGW(TAG, "启动阶段未连上 WiFi（会在后台继续重试）");
    return ESP_OK; /* 不算致命错误：后台还会重连 */
}

bool bot_net_is_connected(void)
{
    return s_wifi_connected;
}

const char *bot_net_ip_string(void)
{
    return s_ip_str;
}

const char *bot_net_server_host(void)
{
    /*
     * 优先用广播发现到的地址，发现不到才回退到编译时配置。
     *
     * 为什么要有回退而不是"发现不到就报错"：PC 端没起服务、或网络
     * 屏蔽了广播时，行为应当与改动前一致 —— 不能因为引入了自动发现
     * 就把原本可用的部署弄坏。
     */
    const char *found = bot_discovery_host();
    if (found != NULL) {
        return found;
    }
    return CONFIG_SPARKBOT_SERVER_HOST;
}

uint16_t bot_net_server_port(void)
{
    return (uint16_t)CONFIG_SPARKBOT_SERVER_PORT;
}

const char *bot_net_server_path(void)
{
    return CONFIG_SPARKBOT_SERVER_PATH;
}

/* ------------------------------------------------------------------ */
/* WebSocket 客户端                                                   */
/* ------------------------------------------------------------------ */

/* 精确读 n 字节。
 *
 * 返回值约定（这里区分"超时"和"断开"非常关键）：
 *   > 0  实际读到的字节数（等于 n）
 *   -1   出错或对端关闭 —— 调用方应断开重连
 *   -2   接收超时（EAGAIN/EWOULDBLOCK）且一个字节都没读到 —— 连接仍正常
 *
 * 为什么必须区分：socket 上设了 SO_RCVTIMEO，空闲时 recv 会返回 EAGAIN。
 * 如果把它当成错误，主循环就会在没有任何消息的时候不停判定"连接断开"
 * 并重连 —— 表现为设备每几秒重连一次，而实际上链路一直是好的。
 * 这正是本项目踩过的坑。
 */
static int ws_recv_all(int sock, uint8_t *dst, size_t n)
{
    size_t got = 0;

    /*
     * 整帧的接收截止时间。
     *
     * 这是修掉"播放时连接被自己掐断"的关键。旧实现在**读到一半**又遇到
     * socket 超时（EAGAIN）时直接返回 -1，调用方把 -1 当成"对端关闭"，
     * 于是主动 shutdown + close —— 屏幕显示 LINK LOST。
     *
     * 而 PC 的 TTS 音频一帧就有 250KB 以上（base64 后），WiFi 上要读好几秒；
     * SO_RCVTIMEO 经常在中途触发，于是**每次播报都断连一次**。
     * 帧越大越容易命中，这解释了为什么症状总出现在播放阶段。
     *
     * 正确做法：只要**还有进展**就继续等，直到整帧读完或超过总预算。
     * 只有"一个字节都没读到"才是真正的空闲（返回 -2）；
     * 只有"有进展但迟迟读不完"才可能是链路卡死（返回 -1）。
     */
    const int64_t deadline_us = esp_timer_get_time() + WS_RECV_FRAME_BUDGET_MS * 1000;

    while (got < n) {
        int r = recv(sock, dst + got, n - got, 0);
        if (r == 0) {
            return -1; /* 对端正常关闭 */
        }
        if (r < 0) {
            if (errno == EINTR) {
                continue;
            }
            if (errno == EAGAIN || errno == EWOULDBLOCK) {
                if (got == 0) {
                    return -2; /* 完全没数据：空闲超时，不是错误 */
                }
                /* 读到一半超时：只要还没超总预算就继续等这一帧剩下的部分 */
                if (esp_timer_get_time() < deadline_us) {
                    continue;
                }
                ESP_LOGW(TAG, "整帧接收超时（已收 %u/%u 字节，预算 %dms）",
                         (unsigned)got, (unsigned)n, WS_RECV_FRAME_BUDGET_MS);
                return -1;
            }
            return -1;
        }
        got += (size_t)r;
    }
    return (int)got;
}

/*
 * 发送全部字节。
 *
 * 返回值的区分**非常关键**（这里是"播放时 LINK LOST"的修复点）：
 *   ESP_OK            —— 全部发出
 *   ESP_ERR_TIMEOUT   —— 发送超时/缓冲满（可恢复：丢这一帧即可，别断链）
 *   ESP_FAIL          —— 真错误（对端关闭、连接重置），应当断开重连
 *
 * 旧实现把两者混为一谈，一律返回 ESP_FAIL，于是**一次拥塞就判链路死亡**。
 * 音频上行（麦克风常开）会在 PC 忙于合成/播报时把发送缓冲填满，
 * 结果每次播报都触发一次断连 —— 屏幕显示 LINK LOST。
 */
static esp_err_t ws_send_all(int sock, const uint8_t *data, size_t len)
{
    size_t sent = 0;
    while (sent < len) {
        int n = send(sock, data + sent, len - sent, 0);
        if (n < 0) {
            if (errno == EINTR) {
                continue;
            }
            if (errno == EAGAIN || errno == EWOULDBLOCK) {
                /* 缓冲满或超时：链路本身没问题，丢掉这一帧就行 */
                return ESP_ERR_TIMEOUT;
            }
            /* ECONNRESET / EPIPE / ENOTCONN 等才是真的断了 */
            return ESP_FAIL;
        }
        sent += (size_t)n;
    }
    return ESP_OK;
}

/*
 * 发送一个客户端帧。
 *
 * 与 bot_ws.c（服务端）的关键区别：**客户端发的帧必须加掩码**
 * （RFC 6455 5.3）。服务端要求客户端掩码，否则会关闭连接。
 * 掩码是 4 字节随机数，越随机越好（这里用 esp_random）。
 */
static esp_err_t ws_send_frame(int sock, uint8_t opcode, const uint8_t *data, size_t len)
{
    uint8_t hdr[14];
    size_t n = 0;

    hdr[n++] = 0x80 | (opcode & 0x0F); /* FIN=1 */

    /* 客户端帧：掩码位必须置 1 */
    if (len < 126) {
        hdr[n++] = 0x80 | (uint8_t)len;
    } else if (len <= 0xFFFF) {
        hdr[n++] = 0x80 | 126;
        hdr[n++] = (uint8_t)(len >> 8);
        hdr[n++] = (uint8_t)(len & 0xFF);
    } else {
        hdr[n++] = 0x80 | 127;
        uint64_t v = (uint64_t)len;
        for (int i = 7; i >= 0; i--) {
            hdr[n++] = (uint8_t)((v >> (i * 8)) & 0xFF);
        }
    }

    uint8_t mask[4];
    uint32_t r = esp_random();
    memcpy(mask, &r, 4);
    memcpy(hdr + n, mask, 4);
    n += 4;

    /* 帧头 + 掩码 */
    esp_err_t err = ws_send_all(sock, hdr, n);
    if (err != ESP_OK) {
        return err;
    }

    /* 载荷——必须就地掩码。为了不额外分配大缓冲，
     * 分块掩码后发送。 */
    size_t off = 0;
    uint8_t tmp[WS_RX_CHUNK];
    while (off < len) {
        size_t chunk = len - off;
        if (chunk > sizeof(tmp)) {
            chunk = sizeof(tmp);
        }
        for (size_t i = 0; i < chunk; i++) {
            tmp[i] = data[off + i] ^ mask[(off + i) & 3];
        }
        err = ws_send_all(sock, tmp, chunk);
        if (err != ESP_OK) {
            return err;
        }
        off += chunk;
    }
    return ESP_OK;
}

esp_err_t bot_net_send_text(const char *text, size_t len)
{
    int sock = s_sock;
    if (sock < 0) {
        return ESP_ERR_INVALID_STATE;
    }
    xSemaphoreTake(s_tx_lock, portMAX_DELAY);
    esp_err_t err = ws_send_frame(sock, 0x01, (const uint8_t *)text, len);
    xSemaphoreGive(s_tx_lock);

    if (err == ESP_OK) {
        s_send_fail_streak = 0;
        return ESP_OK;
    }

    if (err == ESP_ERR_TIMEOUT) {
        /*
         * 拥塞：**不要断开**。
         *
         * 丢掉这一帧即可 —— 对遥测/音频这类周期性数据毫无影响；
         * 对 result 这类关键消息，PC 端有超时重试机制兜底。
         * 一次拥塞就断链是过去"播放时 LINK LOST"的直接原因。
         */
        s_send_dropped++;
        if (s_send_dropped == 1 || s_send_dropped % 50 == 0) {
            ESP_LOGW(TAG, "发送拥塞，丢弃本次消息（累计 %u 次，链路保持）",
                     (unsigned)s_send_dropped);
        }
        return err;
    }

    /*
     * 真错误。即使如此也不立刻断开：TCP 偶发错误后往往还能继续用，
     * 连续多次失败才说明链路真的坏了。
     */
    s_send_fail_streak++;
    ESP_LOGW(TAG, "发送失败（连续第 %u 次）", (unsigned)s_send_fail_streak);
    if (s_send_fail_streak >= SEND_FAIL_STREAK_LIMIT) {
        ESP_LOGW(TAG, "连续 %u 次发送失败，判定链路断开", (unsigned)s_send_fail_streak);
        s_send_fail_streak = 0;
        bot_net_disconnect();
    }
    return err;
}

bool bot_net_is_connected_to_server(void)
{
    return s_sock >= 0;
}

void bot_net_disconnect(void)
{
    if (s_sock >= 0) {
        /*
         * 在这里自己打一条日志，而不是依赖各调用点先打。
         *
         * 排查"设备主动关连接"时，PC 端只能看到 close 帧 (code=1005)，
         * 无法知道是哪条路径触发的；而各调用点的日志在实测中都没出现，
         * 定位不了。把日志放在这个必经之路，就能从串口直接确认是谁关的。
         *
         * 注意：`shutdown()` 只发 FIN、**不发 WebSocket close 帧**，
         * 所以 PC 端看到的是 code=1005（无状态码）。这个细节曾让我
         * 误以为是对端正常关闭。
         */
        ESP_LOGW(TAG, "关闭连接（sock=%d，原因见上一行日志）", s_sock);
        shutdown(s_sock, SHUT_RDWR);
        close(s_sock);
        s_sock = -1;
        ESP_LOGI(TAG, "与 PC 的连接已断开");
    }
    s_rx_len = 0;
    s_rx_opcode = 0;
}

/* 简单的 TCP 连接，带超时 */
static int tcp_connect(const char *host, uint16_t port, int timeout_ms)
{
    struct addrinfo hints = {0};
    hints.ai_family = AF_INET;
    hints.ai_socktype = SOCK_STREAM;

    char port_str[8];
    snprintf(port_str, sizeof(port_str), "%u", port);

    struct addrinfo *res = NULL;
    if (getaddrinfo(host, port_str, &hints, &res) != 0 || res == NULL) {
        ESP_LOGE(TAG, "解析主机失败: %s", host);
        return -1;
    }

    int sock = socket(res->ai_family, res->ai_socktype, 0);
    if (sock < 0) {
        freeaddrinfo(res);
        return -1;
    }

    /* 非阻塞连接 + select 实现超时：直接阻塞 connect 可能卡很久 */
    int flags = fcntl(sock, F_GETFL, 0);
    fcntl(sock, F_SETFL, flags | O_NONBLOCK);

    int rc = connect(sock, res->ai_addr, res->ai_addrlen);
    freeaddrinfo(res);

    if (rc < 0 && errno != EINPROGRESS) {
        close(sock);
        return -1;
    }

    if (rc < 0) {
        fd_set wfds;
        FD_ZERO(&wfds);
        FD_SET(sock, &wfds);
        struct timeval tv = {.tv_sec = timeout_ms / 1000,
                             .tv_usec = (timeout_ms % 1000) * 1000};
        rc = select(sock + 1, NULL, &wfds, NULL, &tv);
        if (rc <= 0) {
            ESP_LOGW(TAG, "连接 %s:%u 超时", host, port);
            close(sock);
            return -1;
        }
        int soerr = 0;
        socklen_t l = sizeof(soerr);
        getsockopt(sock, SOL_SOCKET, SO_ERROR, &soerr, &l);
        if (soerr != 0) {
            ESP_LOGW(TAG, "连接失败: %d", soerr);
            close(sock);
            return -1;
        }
    }

    /* 恢复阻塞模式 */
    fcntl(sock, F_SETFL, flags);

    int one = 1;
    setsockopt(sock, IPPROTO_TCP, TCP_NODELAY, &one, sizeof(one));

    /*
     * **发送超时** —— 这个必须设，否则会出现"播放时 LINK LOST"。
     *
     * 原因：音频上行（麦克风常开 + 采集）会在 PC 忙于合成/播报时把
     * TCP 发送缓冲填满。阻塞式 send() 此时会**长时间卡住**，而返回
     * EAGAIN 时旧代码又把它当成致命错误直接断开连接，屏幕显示 LINK LOST。
     *
     * 设上超时后，send 会及时返回 EAGAIN，配合 ws_send_all 的容错与
     * "连续多次失败才断开"的策略，链路不会因为一次拥塞就被判死。
     *
     * 3 秒足够长（正常情况下单帧微秒级完成），又不会让主循环卡死。
     */
    struct timeval snd_tv = {.tv_sec = 3, .tv_usec = 0};
    setsockopt(sock, SOL_SOCKET, SO_SNDTIMEO, &snd_tv, sizeof(snd_tv));

    return sock;
}

/* 计算 Sec-WebSocket-Accept（与服务端同样的算法） */
static bool ws_make_accept_key(const char *client_key, char *out, size_t out_len)
{
    static const char *GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11";
    uint8_t sha[20];
    mbedtls_sha1_context ctx;
    mbedtls_sha1_init(&ctx);

    int rc = mbedtls_sha1_starts(&ctx);
    if (rc == 0) {
        rc = mbedtls_sha1_update(&ctx, (const uint8_t *)client_key, strlen(client_key));
    }
    if (rc == 0) {
        rc = mbedtls_sha1_update(&ctx, (const uint8_t *)GUID, strlen(GUID));
    }
    if (rc == 0) {
        rc = mbedtls_sha1_finish(&ctx, sha);
    }
    mbedtls_sha1_free(&ctx);
    if (rc != 0) {
        return false;
    }

    size_t olen = 0;
    if (mbedtls_base64_encode((uint8_t *)out, out_len, &olen, sha, sizeof(sha)) != 0) {
        return false;
    }
    out[olen] = '\0';
    return true;
}

esp_err_t bot_net_connect_server(void)
{
    if (!s_wifi_connected) {
        return ESP_ERR_INVALID_STATE;
    }
    if (s_sock >= 0) {
        return ESP_OK; /* 已连接 */
    }

    const char *host = bot_net_server_host();
    uint16_t port = bot_net_server_port();
    const char *path = bot_net_server_path();

    ESP_LOGI(TAG, "连接 PC: %s:%u%s", host, port, path);
    int sock = tcp_connect(host, port, 5000);
    if (sock < 0) {
        return ESP_FAIL;
    }

    /* 发 HTTP Upgrade 请求。随机 Sec-WebSocket-Key 由 esp_random 生成。 */
    uint8_t nonce[16];
    uint32_t *n32 = (uint32_t *)nonce;
    for (int i = 0; i < 4; i++) {
        n32[i] = esp_random();
    }
    char key[32];
    size_t klen = 0;
    if (mbedtls_base64_encode((uint8_t *)key, sizeof(key), &klen, nonce, sizeof(nonce)) != 0) {
        close(sock);
        return ESP_FAIL;
    }
    key[klen] = '\0';

    char req[256];
    int rn = snprintf(req, sizeof(req),
                      "GET %s HTTP/1.1\r\n"
                      "Host: %s:%u\r\n"
                      "Upgrade: websocket\r\n"
                      "Connection: Upgrade\r\n"
                      "Sec-WebSocket-Key: %s\r\n"
                      "Sec-WebSocket-Version: 13\r\n"
                      "\r\n",
                      path, host, port, key);
    if (rn <= 0 || (size_t)rn >= sizeof(req)) {
        close(sock);
        return ESP_FAIL;
    }
    if (ws_send_all(sock, (const uint8_t *)req, (size_t)rn) != ESP_OK) {
        close(sock);
        return ESP_FAIL;
    }

    /* 读响应头，直到 \r\n\r\n */
    char resp[1024];
    size_t total = 0;
    struct timeval tv = {.tv_sec = 5, .tv_usec = 0};
    setsockopt(sock, SOL_SOCKET, SO_RCVTIMEO, &tv, sizeof(tv));

    while (total < sizeof(resp) - 1) {
        int r = recv(sock, resp + total, sizeof(resp) - 1 - total, 0);
        if (r <= 0) {
            ESP_LOGE(TAG, "读握手响应失败");
            close(sock);
            return ESP_FAIL;
        }
        total += (size_t)r;
        resp[total] = '\0';
        if (strstr(resp, "\r\n\r\n") != NULL) {
            break;
        }
    }

    if (strncmp(resp, "HTTP/1.1 101", 12) != 0 && strncmp(resp, "HTTP/1.0 101", 12) != 0) {
        /* 把状态行打出来，方便看出是被拒（503 已连接）还是路径错（404） */
        char status[64] = {0};
        for (size_t i = 0; i < sizeof(status) - 1 && resp[i] && resp[i] != '\r'; i++) {
            status[i] = resp[i];
        }
        ESP_LOGE(TAG, "握手被拒绝: %s", status);
        close(sock);
        return ESP_FAIL;
    }

    /*
     * 校验 Sec-WebSocket-Accept。
     * 标准要求客户端核对，跳过的话中间有代理时会静默连到一个非 WebSocket 端点。
     */
    char expect[64];
    if (!ws_make_accept_key(key, expect, sizeof(expect))) {
        close(sock);
        return ESP_FAIL;
    }
    if (strstr(resp, expect) == NULL) {
        ESP_LOGE(TAG, "Sec-WebSocket-Accept 不匹配，可能连到了非 WebSocket 端点");
        close(sock);
        return ESP_FAIL;
    }

    s_sock = sock;
    s_rx_len = 0;
    s_rx_opcode = 0;
    ESP_LOGI(TAG, "WebSocket 握手完成，已连接到 SparkBot 服务");
    return ESP_OK;
}

/*
 * 读一个完整 WebSocket 消息（可能由多个分片帧组成）。
 *
 * 返回 ESP_OK 表示拿到完整消息；ESP_ERR_TIMEOUT 表示在超时内没有完整消息
 * （连接仍正常）；ESP_FAIL 表示连接断开。
 */
esp_err_t bot_net_recv_text(char **out_buf, size_t *out_len, int timeout_ms)
{
    int sock = s_sock;
    if (sock < 0) {
        return ESP_FAIL;
    }

    /* 设置本次调用的接收超时 */
    struct timeval tv;
    tv.tv_sec = timeout_ms / 1000;
    tv.tv_usec = (timeout_ms % 1000) * 1000;
    setsockopt(sock, SOL_SOCKET, SO_RCVTIMEO, &tv, sizeof(tv));

    while (true) {
        uint8_t hdr[2];
        int r = ws_recv_all(sock, hdr, 2);
        if (r == -2) {
            /* 空闲超时：连接正常，只是没有消息。 */
            return ESP_ERR_TIMEOUT;
        }
        if (r < 0) {
            return ESP_FAIL;
        }

        bool fin = (hdr[0] & 0x80) != 0;
        uint8_t opcode = hdr[0] & 0x0F;
        bool masked = (hdr[1] & 0x80) != 0;
        uint64_t plen = hdr[1] & 0x7F;

        if (plen == 126) {
            uint8_t ext[2];
            if (ws_recv_all(sock, ext, 2) != 2) {
                return ESP_FAIL;
            }
            plen = ((uint64_t)ext[0] << 8) | ext[1];
        } else if (plen == 127) {
            uint8_t ext[8];
            if (ws_recv_all(sock, ext, 8) != 8) {
                return ESP_FAIL;
            }
            plen = 0;
            for (int i = 0; i < 8; i++) {
                plen = (plen << 8) | ext[i];
            }
        }

        uint8_t mask[4] = {0};
        if (masked) {
            if (ws_recv_all(sock, mask, 4) != 4) {
                return ESP_FAIL;
            }
        }

        /* 控制帧 */
        if (opcode == 0x08) {
            ESP_LOGI(TAG, "服务端关闭连接");
            return ESP_FAIL;
        }
        if (opcode == 0x09) {
            /* ping 必须回 pong，载荷原样返回 */
            uint8_t tmp[128];
            size_t to_read = (plen > sizeof(tmp)) ? sizeof(tmp) : (size_t)plen;
            if (to_read > 0 && ws_recv_all(sock, tmp, to_read) != (int)to_read) {
                return ESP_FAIL;
            }
            xSemaphoreTake(s_tx_lock, portMAX_DELAY);
            ws_send_frame(sock, 0x0A, tmp, to_read);
            xSemaphoreGive(s_tx_lock);
            continue;
        }
        if (opcode == 0x0A) {
            /* pong：读掉丢弃 */
            uint8_t tmp[128];
            while (plen > 0) {
                size_t n = (plen > sizeof(tmp)) ? sizeof(tmp) : (size_t)plen;
                if (ws_recv_all(sock, tmp, n) != (int)n) {
                    return ESP_FAIL;
                }
                plen -= n;
            }
            continue;
        }

        if (plen > WS_MAX_MSG) {
            ESP_LOGW(TAG, "消息过大 (%llu)，断开", (unsigned long long)plen);
            return ESP_FAIL;
        }

        /* 扩容重组缓冲（读之前一次性扩够） */
        if (s_rx_len + plen + 1 > s_rx_cap) {
            size_t want = s_rx_len + plen + 1;
            if (want < 4096) {
                want = 4096;
            }
            uint8_t *grown = realloc(s_rx, want);
            if (grown == NULL) {
                ESP_LOGE(TAG, "重组缓冲分配失败 (%u 字节)", (unsigned)want);
                return ESP_FAIL;
            }
            s_rx = grown;
            s_rx_cap = want;
        }

        /* 收载荷。服务端**不应**加掩码，但若加了也要能还原。 */
        int pr = ws_recv_all(sock, s_rx + s_rx_len, (size_t)plen);
        if (pr == -2) {
            /* 载荷读了一半就超时：连接还在，但这条消息不完整。
             * 无法恢复（帧边界已错位），只能断开重连。 */
            ESP_LOGW(TAG, "读载荷超时，连接状态不可恢复，断开");
            return ESP_FAIL;
        }
        if (pr < 0) {
            return ESP_FAIL;
        }

        if (masked) {
            for (size_t i = 0; i < (size_t)plen; i++) {
                s_rx[s_rx_len + i] ^= mask[i & 3];
            }
        }

        /* 分片语义 */
        if (opcode == 0x00) {
            s_rx_len += (size_t)plen;
            if (!fin) {
                continue;
            }
            /* 收齐了 */
        } else {
            if (s_rx_len != 0) {
                /* 上一条消息没读干净，重置 */
                ESP_LOGW(TAG, "分片序列异常，重置");
                s_rx_len = 0;
            }
            s_rx_len = (size_t)plen;
            s_rx_opcode = opcode;
            if (!fin) {
                continue;
            }
        }

        s_rx[s_rx_len] = '\0';
        *out_buf = (char *)s_rx;
        *out_len = s_rx_len;

        size_t consumed = s_rx_len;
        s_rx_len = 0;
        s_rx_opcode = 0;
        (void)consumed;
        return ESP_OK;
    }
}
