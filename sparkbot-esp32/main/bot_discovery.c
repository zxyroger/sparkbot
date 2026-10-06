/*
 * 服务器自动发现：解决"PC 的 IP 变了就要重新编译烧录"的问题。
 *
 * 起因：板子和 PC 都在 DHCP 网段里，PC 的 IP 变过好几次
 * （192.168.0.103 -> .106），每次都要改 CONFIG_SPARKBOT_SERVER_HOST
 * 再重新编译烧录。这在调试期非常烦。
 *
 * 做法（标准且可靠）：**UDP 广播探测**
 *
 *   1. 板子把 "SPARKBOT-DISCOVER" 广播到 <广播地址>:8765；
 *   2. PC 端服务监听同一个 UDP 端口，收到探测后**原路回一个应答**，
 *      应答里带上自己的 IP；
 *   3. 板子用应答的源地址作为服务器地址，去建 WebSocket 连接。
 *
 * 为什么用 UDP 广播而不是别的：
 *   * 不需要 mDNS/Bonjour —— 那要引入额外组件，而且 Windows 上
 *     mDNS 支持不总是可靠；
 *   * 不需要固定 IP 或静态 DHCP 绑定；
 *   * PC 端实现极简（一个 UDP socket + 回包），不引入依赖。
 *
 * 失败时的行为很重要：**发现不到就回退到编译时配置的 IP**。
 * 这样即使 PC 端没起服务、或网络屏蔽了广播，行为也与改动前一致，
 * 不会把原本能用的部署弄坏。
 */

#include "bot_discovery.h"

#include <errno.h>
#include <fcntl.h>
#include <stdio.h>
#include <string.h>

#include "esp_log.h"
#include "esp_timer.h"
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"
#include "lwip/netdb.h"
#include "lwip/netif.h"
#include "lwip/sockets.h"

static const char *TAG = "bot_disc";

/*
 * 取本机（STA）IP，用来推子网广播地址。
 *
 * 实现上直接**按接口名匹配 "st"**（ESP-IDF 的 STA 接口名），而不是
 * 遍历所有 netif 挑第一个可用的。原因：
 *   * `netif_is_loopback()` 和 `NETIF_FLAG_LOOPBACK` 在这份 lwip 里
 *     都不存在（两个都试过，编译报错）；
 *   * 这块板子只有 STA 一个真实接口，按名字取最直接、也最不容易
 *     误取到回环或别的虚拟接口。
 *
 * 不缓存：调用次数很少，缓存反而会引入"IP 变了但缓存没更新"的隐患。
 * 返回 netif 内部的静态字符串，调用方不要释放。
 */
const char *bot_discovery_local_ip(void)
{
    struct netif *n = netif_list;
    while (n != NULL) {
        if (n->name[0] == 's' && n->name[1] == 't' && netif_is_up(n)
            && n->ip_addr.u_addr.ip4.addr != 0) {
            return ip4addr_ntoa(&n->ip_addr.u_addr.ip4);
        }
        n = n->next;
    }
    return NULL;
}

/* 探测magic：PC 端按这个字符串匹配，避免把无关的广播包当成探测。 */
#define DISCOVER_MAGIC "SPARKBOT-DISCOVER-V1"

/* 单次探测等待应答的时间。局域网内通常几毫秒就回，
 * 给 800ms 是留足余量（PC 端可能在忙着推理）。 */
#define DISCOVER_TIMEOUT_MS 800

/* 最多试几轮。每轮之间隔 400ms，总耗时上限约 8 秒。 */
#define DISCOVER_ATTEMPTS 8

/* 发现结果。长度按 IP 字符串留足（含结尾 NUL）。 */
static char s_host[32] = {0};
static bool s_found = false;

const char *bot_discovery_host(void)
{
    return s_found ? s_host : NULL;
}

bool bot_discovery_found(void)
{
    return s_found;
}

void bot_discovery_clear(void)
{
    s_found = false;
    s_host[0] = '\0';
}

/*
 * 发一轮广播探测，等应答。
 *
 * 返回 true 表示收到应答，且把 IP 写进了 s_host。
 */
static bool discover_once(uint16_t port, int timeout_ms)
{
    int sock = socket(AF_INET, SOCK_DGRAM, IPPROTO_UDP);
    if (sock < 0) {
        ESP_LOGW(TAG, "创建 UDP socket 失败: errno=%d", errno);
        return false;
    }

    /* 允许广播，否则 sendto 到广播地址会返回 EACCES */
    int yes = 1;
    setsockopt(sock, SOL_SOCKET, SO_BROADCAST, &yes, sizeof(yes));
    /* 绑定任意本地端口即可；这是客户端行为，不需要固定端口 */
    struct sockaddr_in local = {
        .sin_family = AF_INET,
        .sin_port = htons(0),
        .sin_addr.s_addr = htonl(INADDR_ANY),
    };
    if (bind(sock, (struct sockaddr *)&local, sizeof(local)) < 0) {
        ESP_LOGW(TAG, "绑定本地端口失败: errno=%d", errno);
        close(sock);
        return false;
    }

    /*
     * 用 O_NONBLOCK + 轮询，而不是 SO_RCVTIMEO。
     *
     * 踩过的坑：带 SO_RCVTIMEO 的阻塞 recvfrom 在发完广播后**必然崩溃**：
     *     Guru Meditation Error: LoadProhibited
     *     EXCVADDR: 0x000000c0
     * 崩在两个 sendto 之后、recvfrom 那一步。加大主任务栈（8K -> 16K）
     * 无效，说明不是栈问题；lwIP 的超时阻塞接收在这条路径上不可靠。
     * 改成非阻塞轮询后行为可预测：能收就收，收不到就等下一轮。
     */
    int flags = fcntl(sock, F_GETFL, 0);
    fcntl(sock, F_SETFL, flags | O_NONBLOCK);

    /*
     * 要发往的广播地址。
     *
     * 这里用"先填好缓冲区、再按计数遍历"。**不要**用
     *   const char *targets[] = {"255.255.255.255", NULL};
     * 再在循环里按 targets[t] != NULL 判断 —— 那种写法在这里会崩：
     * 子网地址缓冲区若声明在 targets 之后，targets[1] 指向的局部数组
     * 在 inet_addr 读它时已经不是合法内容，实测每次必然崩溃。
     *
     * 崩溃信息（用 addr2line 解析 backtrace 得到）：
     *   ip4addr_aton  at lwip/ip4_addr.c:153
     *   ipaddr_addr   (即 inet_addr)
     *   discover_once at bot_discovery.c:162
     * 症状是 LoadProhibited、EXCVADDR=0x00000200。
     * 一开始误判成 recvfrom 问题和栈溢出，加大栈（8K->16K）无效；
     * 真正定位靠的是 addr2line，而不是继续读日志猜。
     */
    char bcast_any[32];
    char bcast_subnet[32];
    snprintf(bcast_any, sizeof(bcast_any), "255.255.255.255");

    /* 从自己的 IP 推子网广播地址（假设 /24，家庭网络几乎都是）。
     * 不追求精确子网掩码 —— 发现不到还有回退，不值得为此引入
     * 读取 netif 掩码的复杂度。 */
    int n_targets = 1;
    bcast_subnet[0] = '\0';
    const char *myip = bot_discovery_local_ip();
    if (myip != NULL) {
        int a = 0, b = 0, c = 0, d = 0;
        if (sscanf(myip, "%d.%d.%d.%d", &a, &b, &c, &d) == 4) {
            snprintf(bcast_subnet, sizeof(bcast_subnet), "%d.%d.%d.255", a, b, c);
            n_targets = 2;
        }
    }

    for (int t = 0; t < n_targets; t++) {
        const char *target = (t == 0) ? bcast_any : bcast_subnet;
        struct sockaddr_in dst = {
            .sin_family = AF_INET,
            .sin_port = htons(port),
            .sin_addr.s_addr = inet_addr(target),
        };
        sendto(sock, DISCOVER_MAGIC, strlen(DISCOVER_MAGIC), 0,
               (struct sockaddr *)&dst, sizeof(dst));
        ESP_LOGD(TAG, "广播探测 -> %s:%u", target, (unsigned)port);
    }

    /*
     * 轮询等应答。总等待时间 ≈ timeout_ms，每 20ms 查一次。
     * 用 20ms 而不是更小：这块板子上 WiFi 中断较频繁，太密的轮询
     * 收益很小但会白占 CPU。
     */
    char buf[128] = {0};
    struct sockaddr_in from = {0};
    socklen_t from_len = sizeof(from);
    int n = -1;
    const int poll_step_ms = 20;
    const int max_rounds = (timeout_ms > 0) ? (timeout_ms / poll_step_ms) : 1;

    for (int r = 0; r < max_rounds; r++) {
        n = recvfrom(sock, buf, sizeof(buf) - 1, 0,
                     (struct sockaddr *)&from, &from_len);
        if (n > 0) {
            break;
        }
        vTaskDelay(pdMS_TO_TICKS(poll_step_ms));
    }
    close(sock);

    if (n <= 0) {
        return false; /* 超时或错误 —— 交给上层重试 */
    }
    buf[n] = '\0';

    if (strncmp(buf, "SPARKBOT-HERE", 13) != 0) {
        ESP_LOGW(TAG, "收到非预期应答，忽略: %.40s", buf);
        return false;
    }

    /*
     * 优先用应答**源地址**，而不是应答体里的字符串。
     * 源地址是 TCP/IP 层的事实，应答体可能被配置写错；
     * 而且我们最终要连的就是这个源地址。
     */
    char ip[32] = {0};
    if (inet_ntop(AF_INET, &from.sin_addr, ip, sizeof(ip)) == NULL) {
        return false;
    }

    strncpy(s_host, ip, sizeof(s_host) - 1);
    s_host[sizeof(s_host) - 1] = '\0';
    s_found = true;
    ESP_LOGI(TAG, "发现服务器: %s:%u", s_host, port);
    return true;
}

bool bot_discovery_find_server(uint16_t port, int max_attempts)
{
    if (port == 0) {
        port = 8765;
    }
    if (max_attempts <= 0) {
        max_attempts = DISCOVER_ATTEMPTS;
    }

    bot_discovery_clear();

    for (int i = 0; i < max_attempts; i++) {
        if (discover_once(port, DISCOVER_TIMEOUT_MS)) {
            return true;
        }
        ESP_LOGI(TAG, "第 %d/%d 次探测无应答，重试…", i + 1, max_attempts);
        vTaskDelay(pdMS_TO_TICKS(400));
    }

    ESP_LOGW(TAG, "广播发现失败（%d 次），将回退到编译时配置的地址。"
                  "请确认 PC 端服务在运行，且防火墙放行了 UDP %u",
             max_attempts, (unsigned)port);
    return false;
}
