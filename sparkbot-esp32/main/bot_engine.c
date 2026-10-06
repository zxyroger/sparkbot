/*
 * 协议引擎实现：SparkBot 协议的设备侧
 *
 * 内存管理约定（重要）：
 *   cJSON 的对象树必须显式 Delete。本文件里所有构造出来的 JSON
 *   都在同一个函数内配对使用，任何提前 return 都可能漏 free。
 *   为此统一用 "goto done" 模式收尾，而不是在多个分支里重复 Delete。
 */

#include "bot_engine.h"

#include <inttypes.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

#include "cJSON.h"
#include "esp_log.h"
#include "esp_mac.h"
#include "esp_timer.h"
#include "freertos/FreeRTOS.h"
#include "freertos/semphr.h"
#include "mbedtls/base64.h"

#include "bot_hw_audio.h"
#include "bot_hw_camera.h"
#include "bot_hw_display.h"
#include "bot_hw_motor.h"
#include "bot_hw_power.h"
#include "bot_json.h"
#include "bot_net.h"
#include "bot_protocol.h"
#include "bot_wakeword.h"

static const char *TAG = "bot_proto";

/* 遥测周期：与 PC 端协商的心跳一致（协议默认 10s，这里取 5s 更密一点，
 * 电量变化本身不快，但频繁上报能让 PC 的面板看起来是活的） */
#define TELEMETRY_INTERVAL_MS 5000
#define PING_INTERVAL_MS 30000

typedef struct {
    char device_id[48];
    char device_name[64];
    bool handshake_ok;

    int64_t last_telemetry_us;
    int64_t last_ping_us;
    int64_t last_pong_us;

    /* 监听状态 */
    volatile bool listening;
    int listen_timeout_ms;
    int64_t listen_deadline_us;
    int audio_seq;

    int telemetry_seq;
    SemaphoreHandle_t lock;
} proto_ctx_t;

static proto_ctx_t s_p;

/* ------------------------------------------------------------------ */
/* 参数辅助                                                           */
/* ------------------------------------------------------------------ */

static const char *emotion_from_params(const cJSON *params)
{
    const char *e = bot_param_str(params, "emotion", BOT_FACE_NEUTRAL);
    return e != NULL ? e : BOT_FACE_NEUTRAL;
}

/* ------------------------------------------------------------------ */
/* 上行发送                                                           */
/* ------------------------------------------------------------------ */

/* 把 cJSON 对象序列化并发送，然后释放对象。 */
static esp_err_t send_json_and_free(cJSON *root)
{
    if (root == NULL) {
        return ESP_ERR_NO_MEM;
    }
    char *text = cJSON_PrintUnformatted(root);
    cJSON_Delete(root);
    if (text == NULL) {
        return ESP_ERR_NO_MEM;
    }

    size_t len = strlen(text);
    esp_err_t err = bot_net_send_text(text, len);
    free(text);
    return err;
}

static void add_ts(cJSON *root)
{
    cJSON_AddNumberToObject(root, "ts", (double)(esp_timer_get_time() / 1000));
}

esp_err_t bot_proto_send_event(const char *event, const char *json_data)
{
    if (!bot_net_is_connected_to_server()) {
        return ESP_ERR_INVALID_STATE;
    }

    cJSON *root = cJSON_CreateObject();
    if (root == NULL) {
        return ESP_ERR_NO_MEM;
    }
    cJSON_AddNumberToObject(root, "v", BOT_PROTOCOL_VERSION);
    cJSON_AddStringToObject(root, "type", BOT_MSG_EVENT);
    add_ts(root);
    cJSON_AddStringToObject(root, "event", event);

    if (json_data != NULL && json_data[0] != '\0') {
        cJSON *data = cJSON_Parse(json_data);
        if (data != NULL) {
            cJSON_AddItemToObject(root, "data", data);
        }
    }

    ESP_LOGD(TAG, "→ event %s", event);
    return send_json_and_free(root);
}

esp_err_t bot_proto_send_frame(const uint8_t *jpeg, size_t len,
                               uint16_t width, uint16_t height)
{
    if (!bot_net_is_connected_to_server()) {
        return ESP_ERR_INVALID_STATE;
    }
    if (jpeg == NULL || len == 0) {
        return ESP_ERR_INVALID_ARG;
    }

    /* base64 后长度约 4/3，加 1KB 余量给 JSON 外壳 */
    size_t b64_cap = ((len + 2) / 3) * 4 + 16;
    char *b64 = malloc(b64_cap);
    if (b64 == NULL) {
        ESP_LOGW(TAG, "帧 base64 缓冲分配失败 (%u 字节)", (unsigned)b64_cap);
        return ESP_ERR_NO_MEM;
    }

    size_t b64_len = 0;
    int rc = mbedtls_base64_encode((unsigned char *)b64, b64_cap, &b64_len, jpeg, len);
    if (rc != 0) {
        free(b64);
        return ESP_FAIL;
    }
    b64[b64_len] = '\0';

    cJSON *root = cJSON_CreateObject();
    if (root == NULL) {
        free(b64);
        return ESP_ERR_NO_MEM;
    }
    cJSON_AddNumberToObject(root, "v", BOT_PROTOCOL_VERSION);
    cJSON_AddStringToObject(root, "type", BOT_MSG_FRAME);
    cJSON_AddNullToObject(root, "id"); /* 非响应式帧：id 为 null */
    add_ts(root);
    cJSON_AddStringToObject(root, "format", BOT_FMT_JPEG);
    cJSON_AddNumberToObject(root, "width", width);
    cJSON_AddNumberToObject(root, "height", height);
    cJSON_AddNumberToObject(root, "seq", (double)(++s_p.telemetry_seq));
    cJSON_AddStringToObject(root, "data_b64", b64);

    free(b64); /* cJSON 已拷贝字符串 */
    return send_json_and_free(root);
}

esp_err_t bot_proto_send_audio(const char *phase, const uint8_t *pcm, size_t len, int seq)
{
    if (!bot_net_is_connected_to_server()) {
        return ESP_ERR_INVALID_STATE;
    }

    cJSON *root = cJSON_CreateObject();
    if (root == NULL) {
        return ESP_ERR_NO_MEM;
    }
    cJSON_AddNumberToObject(root, "v", BOT_PROTOCOL_VERSION);
    cJSON_AddStringToObject(root, "type", BOT_MSG_AUDIO);
    cJSON_AddNullToObject(root, "id");
    add_ts(root);
    cJSON_AddStringToObject(root, "phase", phase);
    cJSON_AddStringToObject(root, "format", BOT_FMT_PCM_S16LE);
    cJSON_AddNumberToObject(root, "sample_rate", bot_audio_sample_rate());
    cJSON_AddNumberToObject(root, "channels", 1);

    if (pcm != NULL && len > 0) {
        size_t b64_cap = ((len + 2) / 3) * 4 + 16;
        /*
         * 上行音频的 base64 缓冲同样放 PSRAM。
         * 这里每片只有 640 字节（base64 后约 856 字节），内部 RAM 其实够用；
         * 但上行是**高频**路径（音频采集期间每秒约 50 次分配/释放），
         * 放在 PSRAM 可以避免反复摩擦本就紧张的内部分配器。
         */
        char *b64 = heap_caps_malloc(b64_cap, MALLOC_CAP_SPIRAM);
        if (b64 == NULL) {
            b64 = malloc(b64_cap);
        }
        if (b64 == NULL) {
            cJSON_Delete(root);
            return ESP_ERR_NO_MEM;
        }
        size_t b64_len = 0;
        if (mbedtls_base64_encode((unsigned char *)b64, b64_cap, &b64_len, pcm, len) != 0) {
            free(b64);
            cJSON_Delete(root);
            return ESP_FAIL;
        }
        b64[b64_len] = '\0';
        cJSON_AddNumberToObject(root, "seq", seq);
        cJSON_AddStringToObject(root, "data_b64", b64);
        free(b64);
    }

    return send_json_and_free(root);
}

/* ------------------------------------------------------------------ */
/* 遥测                                                               */
/* ------------------------------------------------------------------ */

static void send_telemetry(void)
{
    cJSON *root = cJSON_CreateObject();
    if (root == NULL) {
        return;
    }
    cJSON_AddNumberToObject(root, "v", BOT_PROTOCOL_VERSION);
    cJSON_AddStringToObject(root, "type", BOT_MSG_TELEMETRY);
    add_ts(root);

    /* 电池 —— 协议里唯一带语义的遥测字段，PC 会读它并在提示词里
     * 告诉模型"还剩多少电"。
     *
     * 只有读到**合理**的电压才上报：没接电池时 PMIC 会给出约 400mV 的
     * 噪声值，上报它等于告诉 PC"电量 0.4V"，比不报更糟。
     * 见 bot_hw_power.c 里的合理性校验。 */
    bot_power_info_t pw;
    if (bot_power_poll(&pw)) {
        cJSON *bat = cJSON_CreateObject();
        if (bat != NULL) {
            cJSON_AddNumberToObject(bat, "voltage", pw.battery_mv / 1000.0);
            cJSON_AddNumberToObject(bat, "percent", pw.percent);
            cJSON_AddBoolToObject(bat, "charging", pw.charging);
            cJSON_AddBoolToObject(bat, "external_power", pw.external_power);
            cJSON_AddItemToObject(root, "battery", bat);
        }
    }

    /* 运动状态 */
    float lin = 0.0f, ang = 0.0f;
    bot_motor_get_motion(&lin, &ang);
    cJSON *motion = cJSON_CreateObject();
    if (motion != NULL) {
        cJSON_AddNumberToObject(motion, "linear", lin);
        cJSON_AddNumberToObject(motion, "angular", ang);
        cJSON_AddBoolToObject(motion, "moving", bot_motor_is_moving());
        cJSON_AddItemToObject(root, "motion", motion);
    }

    /* 音频/显示状态，便于 PC 端面板观察 */
    cJSON *audio = cJSON_CreateObject();
    if (audio != NULL) {
        cJSON_AddNumberToObject(audio, "volume", bot_audio_get_volume());
        cJSON_AddBoolToObject(audio, "listening", bot_audio_capture_active());
        cJSON_AddItemToObject(root, "audio", audio);
    }

    cJSON *display = cJSON_CreateObject();
    if (display != NULL) {
        cJSON_AddNumberToObject(display, "backlight", bot_display_get_backlight());
        cJSON_AddItemToObject(root, "display", display);
    }

    cJSON_AddStringToObject(root, "device_id", s_p.device_id);
    cJSON_AddNumberToObject(root, "uptime_ms",
                            (double)(esp_timer_get_time() / 1000));

    send_json_and_free(root);
}

/* ------------------------------------------------------------------ */
/* hello / 握手                                                       */
/* ------------------------------------------------------------------ */

static void build_device_id(void)
{
#ifdef CONFIG_SPARKBOT_DEVICE_ID
    /* Kconfig 字符串宏在 C 里就是字符串字面量，可以直接比较首字符。
     * 注意不能用 #if CONFIG_... [0] —— 预处理阶段拿不到字符串的内容，
     * 会报 'token """" is not valid in preprocessor expressions'。 */
    if (CONFIG_SPARKBOT_DEVICE_ID[0] != '\0') {
        strncpy(s_p.device_id, CONFIG_SPARKBOT_DEVICE_ID, sizeof(s_p.device_id) - 1);
        strncpy(s_p.device_name, CONFIG_SPARKBOT_DEVICE_NAME, sizeof(s_p.device_name) - 1);
        return;
    }
#endif

    /* 用 MAC 后 3 字节生成稳定 id：换网络、重启都不变，
     * 这样 PC 端的设备索引与对话历史能延续。 */
    uint8_t mac[6] = {0};
    esp_read_mac(mac, ESP_MAC_WIFI_STA);
    snprintf(s_p.device_id, sizeof(s_p.device_id), "esp32s3-%02x%02x%02x",
             mac[3], mac[4], mac[5]);
    strncpy(s_p.device_name, CONFIG_SPARKBOT_DEVICE_NAME, sizeof(s_p.device_name) - 1);
}

esp_err_t bot_proto_init(void)
{
    memset(&s_p, 0, sizeof(s_p));
    s_p.lock = xSemaphoreCreateMutex();
    if (s_p.lock == NULL) {
        return ESP_ERR_NO_MEM;
    }

    build_device_id();

    /* 建立调试用的关联：不阻塞，失败也不影响协议本身 */
    bot_net_start();

    ESP_LOGI(TAG, "协议引擎就绪, device_id=%s name=%s", s_p.device_id, s_p.device_name);
    return ESP_OK;
}

const char *bot_proto_device_id(void)
{
    return s_p.device_id;
}

const char *bot_proto_device_name(void)
{
    return s_p.device_name;
}

esp_err_t bot_proto_handshake(void)
{
    cJSON *root = cJSON_CreateObject();
    if (root == NULL) {
        return ESP_ERR_NO_MEM;
    }

    cJSON_AddNumberToObject(root, "v", BOT_PROTOCOL_VERSION);
    cJSON_AddStringToObject(root, "type", BOT_MSG_HELLO);
    add_ts(root);

    cJSON *dev = cJSON_CreateObject();
    cJSON_AddStringToObject(dev, "id", s_p.device_id);
    cJSON_AddStringToObject(dev, "model", "ESP32-S3");
    cJSON_AddStringToObject(dev, "fw", BOT_FW_VERSION);
    cJSON_AddStringToObject(dev, "name", s_p.device_name);
    cJSON_AddItemToObject(root, "device", dev);

    /* 能力列表：**只上报真实可用的**。
     * PC 端据此决定向模型暴露哪些工具；多报会导致模型调用
     * 一个注定失败的工具，用户体验很差。 */
    cJSON *caps = cJSON_CreateArray();
    if (bot_camera_ready()) {
        cJSON_AddItemToArray(caps, cJSON_CreateString(BOT_CAP_CAMERA));
    }
    if (bot_audio_ready()) {
        cJSON_AddItemToArray(caps, cJSON_CreateString(BOT_CAP_MICROPHONE));
        cJSON_AddItemToArray(caps, cJSON_CreateString(BOT_CAP_SPEAKER));
    }
    if (bot_display_ready()) {
        cJSON_AddItemToArray(caps, cJSON_CreateString(BOT_CAP_DISPLAY));
    }
    if (bot_motor_ready()) {
        cJSON_AddItemToArray(caps, cJSON_CreateString(BOT_CAP_MOTOR));
    }
    if (bot_power_has_battery()) {
        cJSON_AddItemToArray(caps, cJSON_CreateString(BOT_CAP_BATTERY));
    }
    cJSON_AddItemToObject(root, "capabilities", caps);

    if (bot_display_ready()) {
        cJSON *disp = cJSON_CreateObject();
        cJSON_AddNumberToObject(disp, "width", bot_display_width());
        cJSON_AddNumberToObject(disp, "height", bot_display_height());
        cJSON_AddStringToObject(disp, "color", "rgb565");
        /* 只要实现了全部 12 种表情就报 full，否则报 basic */
        cJSON_AddStringToObject(disp, "emotions", "full");
        cJSON_AddItemToObject(root, "display", disp);
    }

    if (bot_camera_ready()) {
        uint16_t w = 0, h = 0;
        int q = 0;
        bot_camera_get_info(&w, &h, &q);
        cJSON *cam = cJSON_CreateObject();
        cJSON_AddNumberToObject(cam, "max_width", 1600);
        cJSON_AddNumberToObject(cam, "max_height", 1200);
        cJSON_AddNumberToObject(cam, "width", w);
        cJSON_AddNumberToObject(cam, "height", h);
        cJSON_AddNumberToObject(cam, "quality", q);
        cJSON *fmts = cJSON_CreateArray();
        cJSON_AddItemToArray(fmts, cJSON_CreateString(BOT_FMT_JPEG));
        cJSON_AddItemToObject(cam, "formats", fmts);
        cJSON_AddItemToObject(root, "camera", cam);
    }

    if (bot_audio_ready()) {
        cJSON *au = cJSON_CreateObject();
        cJSON_AddNumberToObject(au, "input_rate", bot_audio_sample_rate());
        cJSON_AddNumberToObject(au, "output_rate", bot_audio_sample_rate());
        cJSON_AddNumberToObject(au, "channels", 1);
        cJSON_AddItemToObject(root, "audio", au);
    }

    /* motor 字段只在真的能驱动时才报，
     * 因为它会让 PC 端开放运动类工具。 */
    if (bot_motor_ready()) {
        float ml = 0.0f, ma = 0.0f;
        bot_motor_get_limits(&ml, &ma);
        cJSON *mo = cJSON_CreateObject();
        cJSON_AddStringToObject(mo, "kind", "differential");
        cJSON_AddNumberToObject(mo, "max_linear", ml);
        cJSON_AddNumberToObject(mo, "max_angular", ma);
        cJSON_AddItemToObject(root, "motor", mo);
    }

    ESP_LOGI(TAG, "发送 hello（能力: %s）",
             cJSON_PrintUnformatted(caps) ? "见日志" : "无");

    esp_err_t err = send_json_and_free(root);
    if (err != ESP_OK) {
        return err;
    }

    /* 等 hello_ack —— 带超时，避免 PC 不回时永久卡住启动流程 */
    int64_t deadline = esp_timer_get_time() + (int64_t)CONFIG_SPARKBOT_HELLO_TIMEOUT_MS * 1000;
    while (esp_timer_get_time() < deadline) {
        char *buf = NULL;
        size_t len = 0;
        esp_err_t rerr = bot_net_recv_text(&buf, &len, 500);
        if (rerr == ESP_ERR_TIMEOUT) {
            continue;
        }
        if (rerr != ESP_OK) {
            return ESP_FAIL;
        }

        cJSON *msg = cJSON_Parse(buf);
        if (msg == NULL) {
            continue;
        }
        const cJSON *type = cJSON_GetObjectItemCaseSensitive(msg, "type");
        if (cJSON_IsString(type) && strcmp(type->valuestring, BOT_MSG_HELLO_ACK) == 0) {
            const cJSON *ok = cJSON_GetObjectItemCaseSensitive(msg, "ok");
            bool accepted = cJSON_IsTrue(ok);
            const cJSON *hb = cJSON_GetObjectItemCaseSensitive(msg, "heartbeat_ms");
            int heartbeat = cJSON_IsNumber(hb) ? (int)hb->valuedouble : 0;

            if (accepted) {
                ESP_LOGI(TAG, "握手成功: session=%s 心跳=%dms",
                         bot_param_str(msg, "session", "?"), heartbeat);
                s_p.handshake_ok = true;
                s_p.last_telemetry_us = 0; /* 立刻发第一条遥测 */
                cJSON_Delete(msg);
                return ESP_OK;
            }

            ESP_LOGE(TAG, "服务端拒绝握手: %s", bot_param_str(msg, "error", "未知原因"));
            cJSON_Delete(msg);
            return ESP_FAIL;
        }

        /* 不是 hello_ack 就交给正常处理（可能是 ping 之类的早期消息） */
        bot_proto_handle_message(buf, len);
        cJSON_Delete(msg);
    }

    ESP_LOGE(TAG, "等待 hello_ack 超时");
    return ESP_ERR_TIMEOUT;
}

void bot_proto_on_disconnect(void)
{
    s_p.handshake_ok = false;
    s_p.listening = false;
    /*
     * 只关上行，**不关麦克风**。
     *
     * 麦克风是为唤醒词常开的；断连时关掉它，重连后就再也听不到唤醒词了
     * —— 那正是"喊了没反应"的一类成因。上行必须停（没有人接收了），
     * 但本地唤醒检测应当继续工作。
     */
    bot_audio_set_uplink(false);
}

/* ------------------------------------------------------------------ */
/* 命令处理                                                           */
/* ------------------------------------------------------------------ */

/* 回复一条 result */
static void send_result(const char *id, bool ok, cJSON *data,
                        const char *err_code, const char *err_msg)
{
    cJSON *root = cJSON_CreateObject();
    if (root == NULL) {
        if (data != NULL) {
            cJSON_Delete(data);
        }
        return;
    }
    cJSON_AddNumberToObject(root, "v", BOT_PROTOCOL_VERSION);
    cJSON_AddStringToObject(root, "type", BOT_MSG_RESULT);
    if (id != NULL) {
        cJSON_AddStringToObject(root, "id", id);
    } else {
        cJSON_AddNullToObject(root, "id");
    }
    add_ts(root);
    cJSON_AddBoolToObject(root, "ok", ok);
    if (data != NULL) {
        cJSON_AddItemToObject(root, "data", data);
    } else {
        cJSON_AddItemToObject(root, "data", cJSON_CreateObject());
    }

    if (!ok) {
        cJSON *err = cJSON_CreateObject();
        cJSON_AddStringToObject(err, "code", err_code != NULL ? err_code : BOT_ERR_INTERNAL);
        cJSON_AddStringToObject(err, "message", err_msg != NULL ? err_msg : "未知错误");
        cJSON_AddItemToObject(root, "error", err);
    } else {
        cJSON_AddNullToObject(root, "error");
    }

    if (!ok) {
        ESP_LOGW(TAG, "结果: %s → 失败 [%s] %s", id != NULL ? id : "-", err_code, err_msg);
    }
    send_json_and_free(root);
}

/* 便捷宏：失败回复 */
#define REPLY_ERR(code, msg) send_result(id, false, NULL, code, msg)

/* 执行一个动作。返回 cJSON 数据体（成功）或 NULL（失败时已回 result）。 */
static cJSON *exec_action(const char *action, const cJSON *params,
                          bool want_reply, const char *id)
{
    bool terr = false;

    /* ---------------- 底盘 ---------------- */
    if (strcmp(action, BOT_ACT_DRIVE) == 0) {
        if (!bot_motor_ready()) {
            if (want_reply) {
                REPLY_ERR(BOT_ERR_HARDWARE_FAULT, "本机未启用电机");
            }
            return NULL;
        }
        float linear = bot_param_float(params, "linear", 0.0f, &terr);
        float angular = bot_param_float(params, "angular", 0.0f, &terr);
        int duration = bot_param_int(params, "duration_ms", 0, &terr);
        if (terr) {
            if (want_reply) {
                REPLY_ERR(BOT_ERR_BAD_PARAMS, "linear/angular/duration_ms 必须是数字");
            }
            return NULL;
        }
        esp_err_t err = bot_motor_drive(linear, angular, duration);
        if (err != ESP_OK) {
            if (want_reply) {
                REPLY_ERR(BOT_ERR_INTERNAL, "电机驱动失败");
            }
            return NULL;
        }
        cJSON *d = cJSON_CreateObject();
        cJSON_AddNumberToObject(d, "linear", linear);
        cJSON_AddNumberToObject(d, "angular", angular);
        cJSON_AddNumberToObject(d, "duration_ms", duration);
        return d;
    }

    if (strcmp(action, BOT_ACT_STOP) == 0) {
        bot_motor_stop();
        cJSON *d = cJSON_CreateObject();
        cJSON_AddBoolToObject(d, "stopped", true);
        return d;
    }

    if (strcmp(action, BOT_ACT_SET_MOTION_LIMITS) == 0) {
        float ml = bot_param_float(params, "max_linear", -1.0f, &terr);
        float ma = bot_param_float(params, "max_angular", -1.0f, &terr);
        if (bot_motor_ready()) {
            bot_motor_set_limits(ml, ma);
        }
        cJSON *d = cJSON_CreateObject();
        cJSON_AddNumberToObject(d, "max_linear", ml);
        cJSON_AddNumberToObject(d, "max_angular", ma);
        return d;
    }

    /* ---------------- 显示 ---------------- */
    if (strcmp(action, BOT_ACT_SET_FACE) == 0) {
        if (!bot_display_ready()) {
            if (want_reply) {
                REPLY_ERR(BOT_ERR_HARDWARE_FAULT, "本机未启用显示屏");
            }
            return NULL;
        }
        const char *emotion = emotion_from_params(params);
        float intensity = bot_param_float(params, "intensity", 1.0f, &terr);
        if (terr) {
            intensity = 1.0f;
        }
        esp_err_t err = bot_display_show_face(emotion, intensity);
        if (err != ESP_OK) {
            if (want_reply) {
                REPLY_ERR(BOT_ERR_INTERNAL, "显示失败");
            }
            return NULL;
        }
        cJSON *d = cJSON_CreateObject();
        cJSON_AddStringToObject(d, "emotion", emotion);
        return d;
    }

    if (strcmp(action, BOT_ACT_SET_TEXT) == 0) {
        if (!bot_display_ready()) {
            if (want_reply) {
                REPLY_ERR(BOT_ERR_HARDWARE_FAULT, "本机未启用显示屏");
            }
            return NULL;
        }
        const char *text = bot_param_str(params, "text", "");
        int duration = bot_param_int(params, "duration_ms", 2000, &terr);
        bot_display_show_text(text, duration);
        cJSON *d = cJSON_CreateObject();
        cJSON_AddStringToObject(d, "text", text);
        return d;
    }

    if (strcmp(action, BOT_ACT_CLEAR_DISPLAY) == 0) {
        bot_display_clear();
        cJSON *d = cJSON_CreateObject();
        cJSON_AddBoolToObject(d, "cleared", true);
        return d;
    }

    if (strcmp(action, BOT_ACT_SET_BACKLIGHT) == 0) {
        int pct = bot_param_int(params, "percent", 100, &terr);
        if (!bot_display_ready()) {
            if (want_reply) {
                REPLY_ERR(BOT_ERR_HARDWARE_FAULT, "本机未启用显示屏");
            }
            return NULL;
        }
        bot_display_set_backlight(pct);
        cJSON *d = cJSON_CreateObject();
        cJSON_AddNumberToObject(d, "percent", pct);
        return d;
    }

    if (strcmp(action, BOT_ACT_DISPLAY_FRAME) == 0) {
        /* 固件未内置图像解码器，明确回不支持。
         * 比静默成功要好：PC 端能立刻知道失败原因。 */
        if (want_reply) {
            REPLY_ERR(BOT_ERR_UNSUPPORTED_ACTION,
                      "固件未内置图像解码器，display_frame 暂不支持");
        }
        return NULL;
    }

    /* ---------------- 视觉 ---------------- */
    if (strcmp(action, BOT_ACT_SNAPSHOT) == 0) {
        if (!bot_camera_ready()) {
            if (want_reply) {
                REPLY_ERR(BOT_ERR_HARDWARE_FAULT, "本机未启用摄像头");
            }
            return NULL;
        }
        int w = bot_param_int(params, "width", 640, &terr);
        int h = bot_param_int(params, "height", 480, &terr);
        int q = bot_param_int(params, "quality", -1, &terr);
        if (terr) {
            if (want_reply) {
                REPLY_ERR(BOT_ERR_BAD_PARAMS, "width/height/quality 必须是数字");
            }
            return NULL;
        }
        /* 抓帧会通过回调发出 frame 消息；
         * 先发 frame 再回 result，PC 端两种顺序都支持。 */
        esp_err_t err = bot_camera_snapshot(w, h, q);
        if (err != ESP_OK) {
            if (want_reply) {
                REPLY_ERR(BOT_ERR_HARDWARE_FAULT, "抓帧失败");
            }
            return NULL;
        }
        uint16_t aw = 0, ah = 0;
        int aq = 0;
        bot_camera_get_info(&aw, &ah, &aq);
        cJSON *d = cJSON_CreateObject();
        cJSON_AddStringToObject(d, "format", BOT_FMT_JPEG);
        cJSON_AddNumberToObject(d, "width", aw);
        cJSON_AddNumberToObject(d, "height", ah);
        cJSON_AddNumberToObject(d, "quality", aq);
        return d;
    }

    if (strcmp(action, BOT_ACT_SET_STREAM) == 0) {
        if (!bot_camera_ready()) {
            if (want_reply) {
                REPLY_ERR(BOT_ERR_HARDWARE_FAULT, "本机未启用摄像头");
            }
            return NULL;
        }
        bool enabled = bot_param_bool(params, "enabled", false, &terr);
        float fps = bot_param_float(params, "fps", 5.0f, &terr);
        int w = bot_param_int(params, "width", 320, &terr);
        int h = bot_param_int(params, "height", 240, &terr);
        bot_camera_set_stream(enabled, fps, w, h);
        cJSON *d = cJSON_CreateObject();
        cJSON_AddBoolToObject(d, "enabled", enabled);
        cJSON_AddNumberToObject(d, "fps", fps);
        return d;
    }

    if (strcmp(action, BOT_ACT_SET_CAMERA_PARAMS) == 0) {
        if (!bot_camera_ready()) {
            if (want_reply) {
                REPLY_ERR(BOT_ERR_HARDWARE_FAULT, "本机未启用摄像头");
            }
            return NULL;
        }
        int w = bot_param_int(params, "width", -1, &terr);
        int h = bot_param_int(params, "height", -1, &terr);
        int q = bot_param_int(params, "quality", -1, &terr);
        bot_camera_set_params(w, h, q);
        cJSON *d = cJSON_CreateObject();
        cJSON_AddBoolToObject(d, "applied", true);
        return d;
    }

    /* ---------------- 音频 ---------------- */
    if (strcmp(action, BOT_ACT_PLAY_AUDIO) == 0) {
        if (!bot_audio_ready()) {
            if (want_reply) {
                REPLY_ERR(BOT_ERR_HARDWARE_FAULT, "本机未启用音频");
            }
            return NULL;
        }
        const char *fmt = bot_param_str(params, "format", BOT_FMT_WAV);
        const char *b64 = bot_param_str(params, "data_b64", NULL);
        if (b64 == NULL || b64[0] == '\0') {
            if (want_reply) {
                REPLY_ERR(BOT_ERR_BAD_PARAMS, "缺少 data_b64");
            }
            return NULL;
        }
        if (strcmp(fmt, BOT_FMT_MP3) == 0) {
            if (want_reply) {
                REPLY_ERR(BOT_ERR_UNSUPPORTED_PARAM, "固件不支持 mp3，请用 wav 或 pcm_s16le");
            }
            return NULL;
        }

        size_t b64_len = strlen(b64);
        size_t cap = (b64_len / 4 + 1) * 3 + 4;

        /*
         * 解码缓冲必须优先放 **PSRAM**。
         *
         * 为什么：一段 10 秒的 16kHz 单声道 WAV 约 320KB，base64 之后
         * 是 427KB。而本机内部 RAM 只剩约 137KB（即使空闲时也不到 150KB），
         * PSRAM 则有 7MB 以上。用 malloc() 默认走内部 RAM，必然失败，
         * 而且失败方式很糟 —— 表现为设备直接掉线（"link lost"），
         * 因为分配失败往往发生在网络收发路径上，会连带把连接拖垮。
         *
         * 实测：一句较长回复（约 70 字 → 音频约 320KB）就能触发。
         */
        uint8_t *raw = heap_caps_malloc(cap, MALLOC_CAP_SPIRAM);
        if (raw == NULL) {
            /* 退回内部 RAM：短音频（几秒）仍能走通 */
            ESP_LOGW(TAG, "PSRAM 分配 %u 字节失败，退回内部 RAM", (unsigned)cap);
            raw = malloc(cap);
        }
        if (raw == NULL) {
            ESP_LOGE(TAG, "解码缓冲分配失败 (%u 字节): 内部可用=%u 内部最大块=%u PSRAM可用=%u",
                     (unsigned)cap,
                     (unsigned)heap_caps_get_free_size(MALLOC_CAP_INTERNAL),
                     (unsigned)heap_caps_get_largest_free_block(MALLOC_CAP_INTERNAL),
                     (unsigned)heap_caps_get_free_size(MALLOC_CAP_SPIRAM));
            if (want_reply) {
                REPLY_ERR(BOT_ERR_INTERNAL, "内存不足");
            }
            return NULL;
        }
        size_t raw_len = 0;
        int rc = mbedtls_base64_decode(raw, cap, &raw_len, (const unsigned char *)b64, b64_len);
        if (rc != 0) {
            free(raw);
            if (want_reply) {
                REPLY_ERR(BOT_ERR_BAD_PARAMS, "data_b64 不是合法 base64");
            }
            return NULL;
        }

        bool is_wav = (strcmp(fmt, BOT_FMT_WAV) == 0);
        int rate = bot_param_int(params, "sample_rate", bot_audio_sample_rate(), &terr);
        esp_err_t err = bot_audio_play(raw, raw_len, is_wav, rate);
        free(raw);

        if (err != ESP_OK) {
            if (want_reply) {
                REPLY_ERR(BOT_ERR_INTERNAL, "播放失败");
            }
            return NULL;
        }
        cJSON *d = cJSON_CreateObject();
        cJSON_AddNumberToObject(d, "bytes", (double)raw_len);
        cJSON_AddStringToObject(d, "format", fmt);
        return d;
    }

    if (strcmp(action, BOT_ACT_TTS_SPEAK) == 0) {
        /* 板载 TTS 需要 esp-sr 的 esp-tts 与中文语音数据分区，
         * 本工程为保持依赖精简没有引入。PC 端有 TTS，
         * 走 play_audio 即可，效果更好。 */
        if (want_reply) {
            REPLY_ERR(BOT_ERR_UNSUPPORTED_ACTION,
                      "固件未内置板载 TTS，请用 PC 端 TTS + play_audio");
        }
        return NULL;
    }

    if (strcmp(action, BOT_ACT_PLAY_TONE) == 0) {
        if (!bot_audio_ready()) {
            if (want_reply) {
                REPLY_ERR(BOT_ERR_HARDWARE_FAULT, "本机未启用音频");
            }
            return NULL;
        }
        float freq = bot_param_float(params, "frequency_hz", 880.0f, &terr);
        int dur = bot_param_int(params, "duration_ms", 120, &terr);
        bot_audio_play_tone((int)freq, dur, bot_audio_get_volume());
        cJSON *d = cJSON_CreateObject();
        cJSON_AddNumberToObject(d, "frequency_hz", freq);
        cJSON_AddNumberToObject(d, "duration_ms", dur);
        return d;
    }

    if (strcmp(action, BOT_ACT_SET_VOLUME) == 0) {
        int pct = bot_param_int(params, "percent", 70, &terr);
        bot_audio_set_volume(pct);
        cJSON *d = cJSON_CreateObject();
        cJSON_AddNumberToObject(d, "percent", pct);
        return d;
    }

    if (strcmp(action, BOT_ACT_START_LISTEN) == 0) {
        int timeout = bot_param_int(params, "timeout_ms", 8000, &terr);
        bool wake = bot_param_bool(params, "wake_word", false, &terr);
        if (bot_proto_listen_start(timeout, wake) != ESP_OK) {
            if (want_reply) {
                REPLY_ERR(BOT_ERR_HARDWARE_FAULT, "麦克风不可用");
            }
            return NULL;
        }
        cJSON *d = cJSON_CreateObject();
        cJSON_AddBoolToObject(d, "listening", true);
        return d;
    }

    if (strcmp(action, BOT_ACT_STOP_LISTEN) == 0) {
        bot_proto_listen_stop();
        cJSON *d = cJSON_CreateObject();
        cJSON_AddBoolToObject(d, "listening", false);
        return d;
    }

    /* ---------------- 系统 ---------------- */
    if (strcmp(action, BOT_ACT_SET_LED) == 0) {
        /* 本板没有独立 RGB 灯（屏幕与背光就是状态指示）。
         * 回成功但不做事，避免 PC 端因为一个无关紧要的动作报错。 */
        cJSON *d = cJSON_CreateObject();
        cJSON_AddBoolToObject(d, "applied", false);
        cJSON_AddStringToObject(d, "note", "本板无独立 RGB 灯");
        return d;
    }

    if (strcmp(action, BOT_ACT_REBOOT) == 0) {
        /* 先回 result 再重启，否则 PC 只会看到连接断开。
         * 注意这里不能在 exec_action 里 return —— 需要把 result 发出去。
         * 所以标记一个待重启标志，由主循环在回复之后执行。 */
        if (want_reply) {
            cJSON *d = cJSON_CreateObject();
            cJSON_AddBoolToObject(d, "rebooting", true);
            send_result(id, true, d, NULL, NULL);
            ESP_LOGW(TAG, "收到重启指令，2 秒后重启");
            vTaskDelay(pdMS_TO_TICKS(2000));
            esp_restart();
        }
        return NULL;
    }

    if (strcmp(action, BOT_ACT_CONFIG) == 0) {
        cJSON *d = cJSON_CreateObject();
        cJSON_AddBoolToObject(d, "applied", true);
        return d;
    }

    /*
     * 诊断专用动作（非协议标准动作，按需使用）：
     * 把唤醒词/AFE 状态与系统任务表打到串口。
     * 排查 "fetch 一直不产出" 时用它确认 AFE 内部任务是否存在/在跑。
     */
    if (strcmp(action, "wakeword_dump") == 0) {
        bot_wakeword_dump();
        cJSON *d = cJSON_CreateObject();
        cJSON_AddBoolToObject(d, "dumped", true);
        return d;
    }

    /* 帧缓冲颜色统计：确认软件实际画了什么颜色 */
    if (strcmp(action, "fbcolors") == 0) {
        if (!bot_display_ready()) {
            if (want_reply) {
                REPLY_ERR(BOT_ERR_HARDWARE_FAULT, "本机未启用显示");
            }
            return NULL;
        }
        bot_display_dump_colors(6);
        cJSON *d = cJSON_CreateObject();
        cJSON_AddBoolToObject(d, "dumped", true);
        cJSON_AddStringToObject(d, "note", "颜色统计已打到串口");
        return d;
    }

    /*
     * 颜色诊断：整屏依次显示 红/绿/蓝/黄/白/黑。
     * 颜色不对时用它判断是通道顺序错还是反色开关错 ——
     * 看"黄色变粉红"分不清，看纯色就一目了然。
     */
    if (strcmp(action, "colors") == 0) {
        if (!bot_display_ready()) {
            if (want_reply) {
                REPLY_ERR(BOT_ERR_HARDWARE_FAULT, "本机未启用显示");
            }
            return NULL;
        }
        /* 默认每色 1.2 秒；调用方可以传 hold_ms 覆盖 */
        bool terr = false;
        int hold_ms = bot_param_int(params, "hold_ms", 1200, &terr);
        if (terr || hold_ms <= 0) {
            hold_ms = 1200;
        }
        bot_display_color_test(hold_ms);
        /* 诊断完把表情恢复回去 */
        bot_display_redraw();
        cJSON *d = cJSON_CreateObject();
        cJSON_AddStringToObject(d, "order",
#if CONFIG_SPARKBOT_LCD_COLOR_RGB
                                "RGB"
#else
                                "BGR"
#endif
        );
#if CONFIG_SPARKBOT_LCD_INVERT
        cJSON_AddBoolToObject(d, "invert", true);
#else
        cJSON_AddBoolToObject(d, "invert", false);
#endif
        cJSON_AddStringToObject(d, "sequence", "红 绿 蓝 黄 白 黑");
        return d;
    }

    /* 未知动作 */
    if (want_reply) {
        REPLY_ERR(BOT_ERR_UNSUPPORTED_ACTION, "固件未实现该动作");
    } else {
        ESP_LOGW(TAG, "收到未实现的 intent: %s", action);
    }
    return NULL;
}

/* ------------------------------------------------------------------ */
/* 消息分发                                                           */
/* ------------------------------------------------------------------ */

void bot_proto_handle_message(const char *json, size_t len)
{
    cJSON *msg = cJSON_ParseWithLength(json, len + 1);
    if (msg == NULL) {
        ESP_LOGW(TAG, "收到无法解析的 JSON（%u 字节）", (unsigned)len);
        return;
    }

    const cJSON *type = cJSON_GetObjectItemCaseSensitive(msg, "type");
    if (!cJSON_IsString(type)) {
        ESP_LOGW(TAG, "消息缺少 type 字段");
        cJSON_Delete(msg);
        return;
    }

    /* ping → pong */
    if (strcmp(type->valuestring, BOT_MSG_PING) == 0) {
        cJSON *pong = cJSON_CreateObject();
        cJSON_AddNumberToObject(pong, "v", BOT_PROTOCOL_VERSION);
        cJSON_AddStringToObject(pong, "type", BOT_MSG_PONG);
        const cJSON *id = cJSON_GetObjectItemCaseSensitive(msg, "id");
        if (cJSON_IsString(id)) {
            cJSON_AddStringToObject(pong, "id", id->valuestring);
        }
        add_ts(pong);
        send_json_and_free(pong);
        cJSON_Delete(msg);
        return;
    }

    if (strcmp(type->valuestring, BOT_MSG_PONG) == 0) {
        s_p.last_pong_us = esp_timer_get_time();
        cJSON_Delete(msg);
        return;
    }

    bool is_command = strcmp(type->valuestring, BOT_MSG_COMMAND) == 0;
    bool is_intent = strcmp(type->valuestring, BOT_MSG_INTENT) == 0;

    if (!is_command && !is_intent) {
        ESP_LOGD(TAG, "忽略消息类型: %s", type->valuestring);
        cJSON_Delete(msg);
        return;
    }

    const cJSON *action_item = cJSON_GetObjectItemCaseSensitive(msg, "action");
    if (!cJSON_IsString(action_item)) {
        if (is_command) {
            const cJSON *id = cJSON_GetObjectItemCaseSensitive(msg, "id");
            send_result(cJSON_IsString(id) ? id->valuestring : NULL, false, NULL,
                        BOT_ERR_BAD_PARAMS, "缺少 action");
        }
        cJSON_Delete(msg);
        return;
    }

    const char *action = action_item->valuestring;
    const cJSON *params = cJSON_GetObjectItemCaseSensitive(msg, "params");
    const cJSON *id_item = cJSON_GetObjectItemCaseSensitive(msg, "id");
    const char *id = cJSON_IsString(id_item) ? id_item->valuestring : NULL;

    if (is_command) {
        ESP_LOGI(TAG, "← command %s", action);
    } else {
        ESP_LOGD(TAG, "← intent %s", action);
    }

    /* exec_action 内部在失败时已经回过 result，成功时返回数据体 */
    cJSON *data = exec_action(action, params, is_command, id);

    if (is_command && data != NULL) {
        send_result(id, true, data, NULL, NULL);
    }

    cJSON_Delete(msg);
}

/* ------------------------------------------------------------------ */
/* 音频采集上行                                                       */
/* ------------------------------------------------------------------ */

/* bot_hw_audio 的采集回调：每 20ms 一片 PCM 送进来 */
static void on_audio_capture(const uint8_t *pcm, size_t len)
{
    if (!s_p.listening || !bot_net_is_connected_to_server()) {
        return;
    }

    /* 音频上行是高频路径：不能阻塞主循环太久。
     * bot_net_send_text 内部有锁与串行写入，20ms 一片（约 860 字节
     * base64 后的 JSON）在 WiFi 上完全跟得上。 */
    bot_proto_send_audio("chunk", pcm, len, s_p.audio_seq++);
}

esp_err_t bot_proto_listen_start(int timeout_ms, bool wake_word)
{
    if (!bot_audio_ready()) {
        return ESP_ERR_INVALID_STATE;
    }
    if (s_p.listening) {
        return ESP_OK;
    }
    if (timeout_ms <= 0) {
        timeout_ms = 8000;
    }

    s_p.audio_seq = 0;
    s_p.listen_timeout_ms = timeout_ms;
    /* wake_word 参数目前忽略：唤醒词由设备本地的 WakeNet 常驻检测，
     * 不依赖 PC 通过这个参数指定。 */
    (void)wake_word;

    bot_audio_set_capture_cb(on_audio_capture);

    /*
     * 麦克风本来就是常开的（bot_audio_start_monitor()，为了唤醒词），
     * 这里只需要**打开上行**，把 PCM 送出去。
     *
     * 早期实现在这里做 bot_audio_capture_start() —— 那等于"为了录音才开
     * 麦克风"，唤醒词就只能在录音期间生效。现在两者解耦：
     *   麦克风/唤醒检测：常开
     *   音频上行：        仅本次会话
     */
    bot_audio_set_uplink(true);

    s_p.listening = true;
    s_p.listen_deadline_us = esp_timer_get_time() + (int64_t)timeout_ms * 1000;

    /* 协议规定：一个采集会话发且仅发一个 start 与一个 end */
    bot_proto_send_audio("start", NULL, 0, 0);
    ESP_LOGI(TAG, "开始采集音频（超时 %dms）", timeout_ms);
    return ESP_OK;
}

esp_err_t bot_proto_listen_stop(void)
{
    if (!s_p.listening) {
        return ESP_OK;
    }

    s_p.listening = false;
    /* 只关上行，**不关麦克风** —— 唤醒词要继续听 */
    bot_audio_set_uplink(false);
    bot_proto_send_audio("end", NULL, 0, 0);
    ESP_LOGI(TAG, "音频采集结束（共 %d 片）", s_p.audio_seq);

    /*
     * 采集结束后打一次内存实况。
     *
     * 目的是抓住"刷屏失败"的现场：ESP-IDF 的 SPI 驱动在传输小块数据时
     * 需要申请 **MALLOC_CAP_DMA** 的内部缓冲，而语音会话期间这块内存
     * 可能被吃紧。把几个关键数字记下来，才能判断该在哪一侧腾内存，
     * 而不是靠猜（我已经猜错两版守卫了）。
     *
     * 关注 **最大连续块**：DMA 要的是连续内存，总量够但碎片化一样会失败。
     */
    ESP_LOGI(TAG, "内存实况: DMA可用=%u DMA最大块=%u | 内部可用=%u 内部最大块=%u | PSRAM可用=%u",
             (unsigned)heap_caps_get_free_size(MALLOC_CAP_DMA),
             (unsigned)heap_caps_get_largest_free_block(MALLOC_CAP_DMA),
             (unsigned)heap_caps_get_free_size(MALLOC_CAP_INTERNAL),
             (unsigned)heap_caps_get_largest_free_block(MALLOC_CAP_INTERNAL),
             (unsigned)heap_caps_get_free_size(MALLOC_CAP_SPIRAM));
    return ESP_OK;
}

bool bot_proto_listening(void)
{
    return s_p.listening;
}

/* ------------------------------------------------------------------ */
/* 周期性工作                                                         */
/* ------------------------------------------------------------------ */

void bot_proto_poll(void)
{
    if (!bot_net_is_connected_to_server() || !s_p.handshake_ok) {
        return;
    }

    int64_t now = esp_timer_get_time();

    /* 遥测 */
    if (now - s_p.last_telemetry_us >= (int64_t)TELEMETRY_INTERVAL_MS * 1000) {
        s_p.last_telemetry_us = now;
        send_telemetry();
    }

    /* ping 保活：PC 端 30s 没收到任何消息会判定掉线，
     * 而遥测是 5s 一次，所以实际上 ping 很少有机会发出去 —— 
     * 保留它是为了在遥测被关闭的情况下链路也能保活。 */
    if (now - s_p.last_ping_us >= (int64_t)PING_INTERVAL_MS * 1000) {
        s_p.last_ping_us = now;

        cJSON *ping = cJSON_CreateObject();
        cJSON_AddNumberToObject(ping, "v", BOT_PROTOCOL_VERSION);
        cJSON_AddStringToObject(ping, "type", BOT_MSG_PING);
        char idbuf[24];
        snprintf(idbuf, sizeof(idbuf), "p%lld", (long long)(now / 1000));
        cJSON_AddStringToObject(ping, "id", idbuf);
        add_ts(ping);
        send_json_and_free(ping);

        if (s_p.last_pong_us == 0) {
            ESP_LOGD(TAG, "首次 ping（尚未收到 pong）");
        }
    }

    /* 采集超时：到点自动结束并发 listen_timeout 事件 */
    if (s_p.listening && now > s_p.listen_deadline_us) {
        ESP_LOGI(TAG, "采集超时");
        bot_proto_listen_stop();
        bot_proto_send_event(BOT_EVT_LISTEN_TIMEOUT, NULL);
    }

    /* 低电事件：只在跨越阈值时发一次，避免每 5 秒刷一条 */
    static int last_low_reported = -1;
    bot_power_info_t pw;
    if (bot_power_poll(&pw) && pw.percent >= 0) {
        int level = 0; /* 0=正常 1=低 2=危险 */
        if (pw.percent <= BOT_POWER_CRITICAL_PERCENT) {
            level = 2;
        } else if (pw.percent <= BOT_POWER_LOW_PERCENT) {
            level = 1;
        }
        if (level > 0 && level != last_low_reported && !pw.external_power) {
            char data[64];
            snprintf(data, sizeof(data), "{\"percent\":%d}", pw.percent);
            bot_proto_send_event(level == 2 ? BOT_EVT_ERROR : BOT_EVT_LOW_BATTERY, data);
        }
        last_low_reported = level;
    }
}
