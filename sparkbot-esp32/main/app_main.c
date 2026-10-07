/*
 * SparkBot 固件主程序
 *
 * 启动顺序（有依赖关系，不能随意调换）：
 *   1. I2C 总线     —— 音频 codec 与电源管理都要用它
 *   2. 显示         —— 最早的视觉反馈，启动慢也能先给人看到
 *   3. 音频 / 电池
 *   4. 摄像头       —— 最占资源，放后面避免拖累前面
 *   5. 电机         —— 最后初始化，确保开机时轮子是停的
 *   6. 网络 + 连接 PC + hello 握手
 *   7. 进主循环
 *
 * 主循环做什么：
 *   - 收一条 PC 消息（带超时，超时就继续做周期工作）
 *   - 协议周期任务（遥测、超时检查）
 *   - 摄像头推流
 *   - 电机开环定时
 *   - 屏幕刷新（脏矩形）
 *
 * 为什么不用独立的接收任务：设备侧的"处理消息"会调用摄像头抓帧、
 * 驱动电机这类阻塞且独占外设的操作。放主循环里串行执行最简单也最稳；
 * 音频采集因为 I2S 读是长阻塞的，才单独起任务（见 bot_hw_audio.c）。
 */

#include <stdio.h>
#include <string.h>

#include "esp_log.h"
#include "esp_system.h"
#include "esp_timer.h"
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"

#include "bot_discovery.h"
#include "bot_engine.h"
#include "bot_hw_audio.h"
#include "bot_hw_camera.h"
#include "bot_hw_display.h"
#include "bot_hw_i2c.h"
#include "bot_hw_motor.h"
#include "bot_hw_power.h"
#include "bot_net.h"
#include "bot_protocol.h"
#include "bot_wakeword.h"

static const char *TAG = "app";

/* ------------------------------------------------------------------ */
/* 子模块回调                                                         */
/* ------------------------------------------------------------------ */

/* 摄像头抓到一帧 → 发 frame 消息给 PC */
static void on_camera_frame(const uint8_t *jpeg, size_t len,
                            uint16_t width, uint16_t height)
{
    bot_proto_send_frame(jpeg, len, width, height);
}

/*
 * 音频播完 → 通知主循环去发 audio_done 事件
 * （PC 端用它做"说完再听"的时序，分段播报也靠它衔接）。
 *
 * 这里**只置标志**，不能直接调 bot_proto_send_event：本回调跑在音频任务
 * bot_spk 里，那个任务的栈只有 4096 字节（见 bot_hw_audio.c 的
 * xTaskCreate(play_task, "bot_spk", 4096, ...)），构造并发送一帧 JSON 会
 * 栈溢出、板子当场重启。发送改由主循环的 bot_proto_poll() 完成。
 */
static void on_audio_done(void)
{
    bot_proto_notify_audio_done();
}

/* 开环定时走完 → 发 motion_done 事件 */
static void on_motion_done(void)
{
    bot_proto_send_event(BOT_EVT_MOTION_DONE, NULL);
}

/*
 * 本地唤醒词命中 → 告诉 PC「用户叫我了」。
 *
 * PC 侧的语音闭环正是监听这个事件：收到后它会先播一声"叮"作提示，
 * 然后下发 start_listen 开始采集，走完 识别 → 决策 → 播报 一整轮。
 * 也就是说**这一条事件就是整条语音交互的入口**。
 *
 * 这是在采集任务的上下文里被调用的，而 send_event 会写 WebSocket。
 * 写操作本身是短小的（只有几十字节），不会明显阻塞采集；
 * 但如果将来要在这里加更重的逻辑，必须挪到独立任务去。
 */
static void on_wake_word(void)
{
    /* 先在本地打一条日志再上报 —— 这一环过去是"黑盒"：
     * 固件打印了"唤醒词命中"却没有任何事件到达 PC，无法判断
     * 到底是回调没被调用、事件没发出去、还是 PC 侧没收。
     * 有了这行，看串口就能分清：
     *   * 有"命中"但没有"上报" → 回调链断了；
     *   * 有"上报 ok"但 PC 没收到 → 传输/PC 侧问题；
     *   * 有"上报失败(code)" → 发送失败，code 说明原因。 */
    ESP_LOGI(TAG, "唤醒词命中 → 上报 wake_word 事件");

    /* phrase 字段填实际唤醒词，PC 端会把它记录到事件里，
     * 便于排查"是不是听错了词"。 */
    esp_err_t err = bot_proto_send_event(BOT_EVT_WAKE_WORD, "{\"phrase\":\"Hi,小星\"}");
    if (err != ESP_OK) {
        ESP_LOGW(TAG, "wake_word 事件上报失败: %s", esp_err_to_name(err));
    } else {
        ESP_LOGI(TAG, "wake_word 事件上报 ok");
    }
}

/* ------------------------------------------------------------------ */
/* 模块初始化                                                         */
/* ------------------------------------------------------------------ */

static void init_hardware(void)
{
    /* 1. I2C 必须最先：音频 codec（ES8311）与电源管理（AXP2101）
     *    都挂在这条总线上。 */
    if (bot_i2c_init() != ESP_OK) {
        ESP_LOGW(TAG, "I2C 初始化失败 —— 音频与电池遥测将不可用");
    }

    /* 2. 显示 */
    if (bot_display_init() == ESP_OK && bot_display_ready()) {
        /* 开机画面：让人一眼看出固件起来了 */
        bot_display_show_face("neutral", 1.0f);
        bot_display_show_text("BOOT", 1500);
    }

    /* 3. 音频
     *
     * ⚠️ 回调必须在 bot_audio_init() **之后**注册。
     * bot_audio_init() 第一句是 memset(&s_a, 0, sizeof(s_a))，
     * 先注册会被当场清成 NULL —— 表现是串口能看到"播放结束"，
     * 但 audio_done 事件永远发不出去，PC 侧只能靠估算时长衔接分段播报。 */
    if (bot_audio_init() != ESP_OK) {
        ESP_LOGW(TAG, "音频初始化失败");
    }
    bot_audio_set_done_cb(on_audio_done);

    /* 3a. **常开麦克风** —— 让唤醒词能一直听。
     *
     * 必须在 wakeword_init 之前：唤醒引擎通过 monitor 旁路拿数据，
     * 而旁路只有在采集任务跑起来之后才会有数据。
     *
     * 注意这里只开麦克风，**不开上行** —— 上行由 listen 会话控制。
     * 否则设备会把环境声音一直推给 PC。 */
    if (bot_audio_start_monitor() != ESP_OK) {
        ESP_LOGW(TAG, "麦克风常开失败，唤醒词将无法工作");
    }

    /* 3b. 本地唤醒词 —— 必须在 audio_init / start_monitor 之后：
     *     它依赖音频就绪，并会通过 bot_audio_set_monitor_cb() 挂上
     *     采集旁路来拿麦克风数据（见 bot_wakeword.c 顶部说明）。 */
    bot_wakeword_set_cb(on_wake_word);
    if (bot_wakeword_init() != ESP_OK) {
        ESP_LOGW(TAG, "唤醒词初始化失败（其它功能继续）");
    }

    /* 4. 电池 */
    if (bot_power_init() != ESP_OK) {
        ESP_LOGW(TAG, "电源管理初始化失败");
    }

    /* 5. 摄像头 */
    bot_camera_set_frame_cb(on_camera_frame);
    if (bot_camera_init() != ESP_OK) {
        ESP_LOGW(TAG, "摄像头不可用（其它功能继续）");
    }

    /* 6. 电机（最后：确保前面出问题时轮子始终是停的） */
    bot_motor_set_done_cb(on_motion_done);
    if (bot_motor_init() != ESP_OK) {
        ESP_LOGW(TAG, "电机初始化失败");
    }
}

/* 把各模块的能力汇总成一行日志，便于一眼确认板子状态 */
static void log_capabilities(void)
{
    char buf[160];
    int n = 0;
    n += snprintf(buf + n, sizeof(buf) - n, "能力: ");
    if (bot_camera_ready()) {
        n += snprintf(buf + n, sizeof(buf) - n, "camera ");
    }
    if (bot_audio_ready()) {
        n += snprintf(buf + n, sizeof(buf) - n, "mic+speaker ");
    }
    if (bot_wakeword_ready()) {
        n += snprintf(buf + n, sizeof(buf) - n, "wakeword ");
    }
    if (bot_display_ready()) {
        n += snprintf(buf + n, sizeof(buf) - n, "display ");
    }
    if (bot_motor_ready()) {
        n += snprintf(buf + n, sizeof(buf) - n, "motor ");
    }
    if (bot_power_ready()) {
        n += snprintf(buf + n, sizeof(buf) - n, "battery ");
    }
    ESP_LOGI(TAG, "%s", buf);
}

/* ------------------------------------------------------------------ */
/* 连接管理                                                           */
/* ------------------------------------------------------------------ */

/*
 * 连上 PC 并完成握手。返回 true 表示可用。
 *
 * 失败原因会打日志：这一步是现场调试最常见的卡点，
 * 日志必须能直接指出是"没连上"还是"握手被拒"。
 */
static bool connect_and_handshake(void)
{
    if (!bot_net_is_connected()) {
        ESP_LOGD(TAG, "WiFi 尚未连接，等待中");
        return false;
    }

    /*
     * 连不上就重新广播发现一次服务器。
     *
     * 为什么需要这个：PC 端 IP 由 DHCP 分配、会变（实测从 .103 变到 .106），
     * 而配置里写死的地址在烧录后就固定了。发现不到时 bot_net_server_host()
     * 会回退到配置值，所以这里失败不影响原本的行为。
     *
     * 每 30 秒最多发现一次：发现本身要占用 socket 且是阻塞的，
     * 而重连间隔只有几秒 —— 不限制的话会在连不上时空转。
     */
    /*
     * 广播发现服务器地址。
     *
     * 设计要点：
     *   * **首次必须立即发现**，不能等 30 秒。原先把发现放在"距上次
     *     发现超过 30 秒"的条件里，而首次的 last=0、now 只有 1.5 秒，
     *     条件为假 —— 于是发现一次都没跑，直接去连写死的地址。
     *     实测串口日志：`DISC-GATE: now=1520372 elapsed=... gate=0`。
     *   * 之后每 30 秒最多重试一次：发现是阻塞的（最坏约 10 秒），
     *     而重连间隔只有 3 秒，不限制会在连不上时空转。
     *
     * 为什么只在"尚未发现"时反复试：一旦发现成功，
     * bot_net_server_host() 就会一直用那个地址，没必要再探。
     */
    static int64_t s_last_discovery_us = 0;
    const int64_t now_us = esp_timer_get_time();
    const bool never_tried = (s_last_discovery_us == 0);
    const bool ready_for_retry = (now_us - s_last_discovery_us > 30000000LL);
    const bool should_discover =
        !bot_discovery_found() && (never_tried || ready_for_retry);

    if (should_discover) {
        s_last_discovery_us = now_us;
        if (bot_discovery_find_server(bot_net_server_port(), 0)) {
            ESP_LOGI(TAG, "服务器地址已更新为 %s（广播发现）",
                     bot_net_server_host());
        }
    }

    if (bot_net_connect_server() != ESP_OK) {
        ESP_LOGW(TAG, "连接 PC (%s:%u) 失败，%d ms 后重试",
                 bot_net_server_host(), bot_net_server_port(),
                 CONFIG_SPARKBOT_RECONNECT_INTERVAL_MS);
        return false;
    }

    if (bot_proto_handshake() != ESP_OK) {
        ESP_LOGW(TAG, "握手失败，断开后重试");
        bot_net_disconnect();
        bot_proto_on_disconnect();
        return false;
    }

    /* 握手成功：屏幕给个反馈，让人知道已经连上了 */
    if (bot_display_ready()) {
        bot_display_show_face("happy", 1.0f);
        bot_display_show_text("LINK OK", 2000);
    }

    ESP_LOGI(TAG, "=========== 已接入 SparkBot 服务端 ===========");
    ESP_LOGI(TAG, "  设备: %s (%s)", bot_proto_device_name(), bot_proto_device_id());
    ESP_LOGI(TAG, "  地址: %s:%u%s", bot_net_server_host(), bot_net_server_port(),
             bot_net_server_path());
    return true;
}

/* ------------------------------------------------------------------ */
/* 主循环                                                             */
/* ------------------------------------------------------------------ */

/*
 * 主循环里的周期工作。
 * 与协议无关的硬件轮询都在这里，保证"无论有没有连上 PC"
 * 屏幕、电机超时、摄像头推流都在正常工作。
 */
static void housekeeping(void)
{
    bot_display_poll();  /* 脏矩形刷新 + 文字自动消失 */
    bot_camera_poll();   /* 推流时抓帧 */
    bot_motor_poll();    /* 开环定时到点自动停车 */
    bot_proto_poll();    /* 遥测、ping、采集超时、低电事件 */
}

static void main_loop(void)
{
    int64_t next_reconnect_us = 0;

    while (true) {
        if (!bot_net_is_connected_to_server()) {
            /* 断线状态：按间隔重连 */
            int64_t now = esp_timer_get_time();
            if (now >= next_reconnect_us) {
                next_reconnect_us = now + (int64_t)CONFIG_SPARKBOT_RECONNECT_INTERVAL_MS * 1000;
                connect_and_handshake();
            }
            housekeeping();
            vTaskDelay(pdMS_TO_TICKS(50));
            continue;
        }

        /* 已连接：收一条消息（最多等 50ms） */
        char *buf = NULL;
        size_t len = 0;
        esp_err_t err = bot_net_recv_text(&buf, &len, 50);

        if (err == ESP_OK) {
            bot_proto_handle_message(buf, len);
        } else if (err == ESP_FAIL) {
            ESP_LOGW(TAG, "与 PC 的连接断开");
            bot_net_disconnect();
            bot_proto_on_disconnect();
            /* 连接断了必须立刻停车：不能让机器人带着最后一条
             * 运动指令一直跑下去。 */
            bot_motor_stop();
            bot_camera_set_stream(false, 0, 0, 0);
            if (bot_display_ready()) {
                bot_display_show_face("sad", 1.0f);
                bot_display_show_text("LINK LOST", 3000);
            }
            next_reconnect_us = esp_timer_get_time()
                                + (int64_t)CONFIG_SPARKBOT_RECONNECT_INTERVAL_MS * 1000;
            continue;
        }
        /* ESP_ERR_TIMEOUT：正常，继续做周期工作 */

        housekeeping();
    }
}

/* ------------------------------------------------------------------ */
/* 入口                                                               */
/* ------------------------------------------------------------------ */

void app_main(void)
{
    esp_log_level_set("*", ESP_LOG_INFO);
    /* 摄像头/SCCB 的细节日志平时不打，但排查"探不到传感器"时必须打开：
     * 它能区分"总线没建起来"和"传感器不应答"这两种完全不同的原因。
     * 定位完可以删掉这两行。 */
    esp_log_level_set("camera", ESP_LOG_DEBUG);
    esp_log_level_set("sccb-ng", ESP_LOG_DEBUG);
    esp_log_level_set("sccb", ESP_LOG_DEBUG);
    /* esp-sr / AFE 的内部日志：排查"fetch 超时"时必须打开，
     * 它会打印实际的帧长、通道数、任务创建等关键信息。 */
    esp_log_level_set("AFE_SR", ESP_LOG_DEBUG);
    esp_log_level_set("MODEL_LOADER", ESP_LOG_DEBUG);
    esp_log_level_set("wakenet", ESP_LOG_DEBUG);

    ESP_LOGI(TAG, "");
    ESP_LOGI(TAG, "==================================================");
    ESP_LOGI(TAG, "  SparkBot 固件 v%s  (ESP32-S3)", BOT_FW_VERSION);
    ESP_LOGI(TAG, "  协议版本 v%d，对接 PC 端 SparkBot 框架", BOT_PROTOCOL_VERSION);
    ESP_LOGI(TAG, "==================================================");

    init_hardware();
    log_capabilities();

    if (bot_proto_init() != ESP_OK) {
        ESP_LOGE(TAG, "协议引擎初始化失败");
    }

    ESP_LOGI(TAG, "目标服务端: %s:%u%s", bot_net_server_host(), bot_net_server_port(),
             bot_net_server_path());
    ESP_LOGI(TAG, "进入主循环");

    main_loop();

    /* main_loop 不会返回 */
}
