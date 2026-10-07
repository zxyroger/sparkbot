/*
 * 摄像头实现：OV2640 DVP + esp32-camera
 *
 * 帧缓冲放 PSRAM：VGA JPEG 一帧通常 30~100KB，内部 RAM 放不下。
 *
 * 抓帧与推流都在主循环里串行执行（bot_camera_poll）：
 *   esp_camera_fb_get() 在拿到帧之前会阻塞，而帧缓冲只有 N 份。
 *   若有第二个任务同时抓帧，两者会互相等待并可能拿同一份 fb，
 *   导致"发出半帧"或死锁。串行化是最简单也最稳的做法。
 *
 * 与 PC 端的对应关系：
 *   snapshot 动作          → bot_camera_snapshot()  → frame 消息
 *   set_stream 动作        → 开推流               → 周期性 frame 消息
 */

#include "bot_hw_camera.h"

#include <stdlib.h>
#include <string.h>

#include "driver/i2c_master.h"
#include "esp_camera.h"
#include "esp_log.h"
#include "esp_timer.h"
#include "freertos/FreeRTOS.h"
#include "freertos/semphr.h"
#include "freertos/task.h"

#include "bot_hw_i2c.h"
#include "bot_hw_power.h"

static const char *TAG = "bot_cam";

typedef struct {
    bool ready;
    bool streaming;
    float fps;
    int64_t next_frame_us;  /* 推流下一帧的时间点 */
    int width;
    int height;
    int quality;
    bot_camera_frame_cb_t cb;
    uint32_t frames_sent;
    SemaphoreHandle_t lock;
} cam_ctx_t;

static cam_ctx_t s_c;

/* framesize 索引（esp32-camera 的 framesize_t） */
static framesize_t size_from_dims(int width, int height)
{
    /* 按最接近的标准档位选，不追求精确匹配 —— 
     * OV2640 只支持固定几档分辨率。 */
    if (width >= 1600 || height >= 1200) {
        return FRAMESIZE_UXGA; /* 1600x1200 */
    }
    if (width >= 1280 || height >= 1024) {
        return FRAMESIZE_SXGA; /* 1280x1024 */
    }
    if (width >= 1024 || height >= 768) {
        return FRAMESIZE_XGA;  /* 1024x768 */
    }
    if (width >= 800 || height >= 600) {
        return FRAMESIZE_SVGA; /* 800x600 */
    }
    if (width >= 640 || height >= 480) {
        return FRAMESIZE_VGA;  /* 640x480 */
    }
    if (width >= 320 || height >= 240) {
        return FRAMESIZE_QVGA; /* 320x240 */
    }
    return FRAMESIZE_QQVGA;    /* 160x120 */
}

static void dims_from_size(framesize_t fs, int *w, int *h)
{
    switch (fs) {
    case FRAMESIZE_UXGA: *w = 1600; *h = 1200; break;
    case FRAMESIZE_SXGA: *w = 1280; *h = 1024; break;
    case FRAMESIZE_XGA:  *w = 1024; *h = 768;  break;
    case FRAMESIZE_SVGA: *w = 800;  *h = 600;  break;
    case FRAMESIZE_VGA:  *w = 640;  *h = 480;  break;
    case FRAMESIZE_QVGA: *w = 320;  *h = 240;  break;
    case FRAMESIZE_QQVGA: *w = 160; *h = 120;  break;
    default:             *w = 640;  *h = 480;  break;
    }
}

/*: 真正改了分辨率后，为了等 AEC/AGC 重新收敛而丢掉的帧数。 */
#define CAM_RESET_DISCARD_FRAMES 6

/*
 * 只在**分辨率真的变了**时才写 sensor 寄存器；返回 true 表示确实改了。
 *
 * 为什么必须挡这一道：OV2640 的 set_framesize() 会重写时序/窗口寄存器，
 * 副作用是 **AEC/AGC/AWB 全部从头收敛**，之后若干帧都是"没收敛"的状态 ——
 * 实测在室内光线下表现为**近全黑**（平均亮度 8/255，收敛后是 30~48）。
 *
 * 这个坑很隐蔽：`snapshot` 每次抓图都会带上 PC 传来的 width/height，
 * 于是"抓一张看一眼"这种最普通的用法拿到的全是没收敛的暗帧；
 * 而推流路径只在开启时设置一次分辨率，帧就一直是亮的 ——
 * 表现成「预览正常，抓拍 / 识别却什么都看不到」，很难往分辨率上想。
 */
static bool apply_framesize_locked(sensor_t *sensor, framesize_t want)
{
    if (sensor == NULL) {
        return false;
    }
    int w = 0, h = 0;
    dims_from_size(want, &w, &h);
    if (w == s_c.width && h == s_c.height) {
        return false;   /* 没变：一个寄存器都不碰 */
    }
    if (sensor->set_framesize(sensor, want) != 0) {
        return false;
    }
    s_c.width = w;
    s_c.height = h;
    return true;
}

/* 丢若干帧，给 AEC/AGC 留出重新收敛的时间（调用方必须已持锁）。 */
static void discard_frames(int count)
{
    for (int i = 0; i < count; i++) {
        camera_fb_t *fb = esp_camera_fb_get();
        if (fb != NULL) {
            esp_camera_fb_return(fb);
        }
        vTaskDelay(pdMS_TO_TICKS(20));
    }
}

/*
 * 扫一遍 I2C 总线并打印有应答的地址。
 *
 * 这是排查"摄像头探测失败"最有效的一步：它能区分两类完全不同的原因 ——
 *   * 总线上一片死寂（只有 PMIC/codec 应答）→ 传感器没上电 / 排线没插 /
 *     模组坏；
 *   * 0x30 有应答但驱动仍报 not supported → 是型号识别或寄存器读的问题，
 *     方向完全不同。
 * 没有这一步，两种情况都只表现为一句 "Detected camera not supported"。
 */
static void i2c_scan(const char *tag)
{
    i2c_master_bus_handle_t bus = bot_i2c_bus();
    if (bus == NULL) {
        ESP_LOGW(TAG, "I2C 总线不可用，跳过扫描");
        return;
    }

    int count = 0;
    for (uint8_t addr = 0x08; addr < 0x78; addr++) {
        if (i2c_master_probe(bus, addr, 50) == ESP_OK) {
            ESP_LOGI(TAG, "  I2C 应答: 0x%02X", addr);
            count++;
        }
    }

    if (count == 0) {
        ESP_LOGW(TAG, "%s: I2C 总线上没有任何设备应答 —— 传感器很可能没上电 / 排线没插好 / 模组故障", tag);
    } else {
        ESP_LOGI(TAG, "%s: I2C 总线共 %d 个设备应答（正常应含 0x30 摄像头、0x34 PMIC、0x18 codec）",
                 tag, count);
    }
}

esp_err_t bot_camera_init(void)
{
    memset(&s_c, 0, sizeof(s_c));
    s_c.quality = CONFIG_SPARKBOT_CAMERA_JPEG_QUALITY;
    s_c.lock = xSemaphoreCreateMutex();

#if !CONFIG_SPARKBOT_CAMERA_ENABLE
    ESP_LOGI(TAG, "摄像头未启用");
    return ESP_OK;
#endif

    if (s_c.lock == NULL) {
        return ESP_ERR_NO_MEM;
    }

    /*
     * 先给摄像头上电。
     *
     * 本板 OV2640 的 AVDD/DVDD 挂在 AXP2101 的 BLDO1/BLDO2 上，默认关闭。
     * 不开电的话 SCCB 可能写得进去（IO 电源是另一路），但传感器不出像素
     * 时钟，esp_camera_init 会以 "Detected camera not supported" 失败 —— 
     * 这个错误信息很容易让人误以为是排线或模组坏了。
     */
    esp_err_t power_err = bot_power_camera_on();
    if (power_err != ESP_OK) {
        ESP_LOGW(TAG, "摄像头供电使能未完全成功，仍继续尝试初始化");
    }

    /* 把实际使用的 SCCB 配置打出来。esp32-camera 在"自建总线"和
     * "用已有总线"两条路径下的日志完全不对称（后者不打任何日志），
     * 现场排查时很难判断到底走了哪条，所以这里自己记一条。 */
    ESP_LOGI(TAG, "SCCB 配置: 复用已有 I2C 端口 %d (pin_sccb_sda=-1)", I2C_NUM_0);

    camera_config_t cfg = {
        .pin_pwdn = CONFIG_SPARKBOT_CAMERA_PWDN_PIN,
        .pin_reset = CONFIG_SPARKBOT_CAMERA_RESET_PIN,
        .pin_xclk = CONFIG_SPARKBOT_CAMERA_XCLK_PIN,
        /*
         * SCCB 控制总线**复用板载已有 I2C**（与 ES8311 / AXP2101 同一条）。
         *
         * 为什么这样做：本板把摄像头 SIOD/SIOC 与音频 codec 接在同一对
         * SDA=1 / SCL=2 上。如果让摄像头驱动自己再初始化一次这条总线，
         * 会和 i2c_master 驱动抢同一组 GPIO，日志里会看到
         * "GPIO 1 is not usable, maybe conflict with others"，
         * 而且实测会导致 SCCB 通信不可靠。
         *
         * 按 esp32-camera 的约定：pin_sccb_sda = -1 表示"不自建总线，
         * 用 sccb_i2c_port 指定的那条已配置好的 I2C"。
         * 我们的总线建在 I2C_NUM_0（见 bot_hw_i2c.c）。
         */
        .pin_sccb_sda = -1,
        .pin_sccb_scl = -1,
        .sccb_i2c_port = I2C_NUM_0,
        .pin_d7 = CONFIG_SPARKBOT_CAMERA_D7_PIN,
        .pin_d6 = CONFIG_SPARKBOT_CAMERA_D6_PIN,
        .pin_d5 = CONFIG_SPARKBOT_CAMERA_D5_PIN,
        .pin_d4 = CONFIG_SPARKBOT_CAMERA_D4_PIN,
        .pin_d3 = CONFIG_SPARKBOT_CAMERA_D3_PIN,
        .pin_d2 = CONFIG_SPARKBOT_CAMERA_D2_PIN,
        .pin_d1 = CONFIG_SPARKBOT_CAMERA_D1_PIN,
        .pin_d0 = CONFIG_SPARKBOT_CAMERA_D0_PIN,
        .pin_vsync = CONFIG_SPARKBOT_CAMERA_VSYNC_PIN,
        .pin_href = CONFIG_SPARKBOT_CAMERA_HREF_PIN,
        .pin_pclk = CONFIG_SPARKBOT_CAMERA_PCLK_PIN,

        .xclk_freq_hz = CONFIG_SPARKBOT_CAMERA_XCLK_FREQ_HZ,
        .ledc_timer = LEDC_TIMER_1,
        .ledc_channel = LEDC_CHANNEL_6,
        .pixel_format = PIXFORMAT_JPEG,
        .frame_size = FRAMESIZE_VGA,
        .jpeg_quality = s_c.quality,
        .fb_count = CONFIG_SPARKBOT_CAMERA_FB_COUNT,
        /* GRAB_WHEN_EMPTY：没有新帧时返回旧帧，而不是一直等。
         * 对机器人视觉够用，也能避免推流时卡住主循环。 */
        .fb_location = CAMERA_FB_IN_PSRAM,
        .grab_mode = CAMERA_GRAB_LATEST,
    };

    esp_err_t err = esp_camera_init(&cfg);
    if (err != ESP_OK) {
        ESP_LOGE(TAG, "摄像头初始化失败: %s", esp_err_to_name(err));
        ESP_LOGE(TAG, "  排查顺序：1) 扫下面的 I2C 结果看传感器有没有应答 "
                      "2) 摄像头排线是否插紧 3) 模组是否插反");
        /* 失败后扫一遍总线，把"是供电/接线问题"还是"是驱动识别问题"
         * 一次性区分开 —— 否则只有一句 not supported，无从下手。 */
        i2c_scan("摄像头初始化失败后");
        return err;
    }

    /*
     * 预热：丢掉前若干帧再返回。
     *
     * OV2640 上电后 AEC/AGC/AWB 是从默认值**逐帧收敛**的，头几帧增益拉满、
     * 白平衡没收敛 —— 直接出图就是"很多噪点 + 彩色横纹"。这条经验来自同板
     * 的 onegpio 工程：那边实测未收敛帧的逐行噪声是收敛后的 4.5~8 倍。
     *
     * 按"时间 + 帧数"双条件等待：收敛是按帧推进的，夜间/低帧率时时间够了
     * 帧数可能还不够。期间 vTaskDelay 让出 CPU，别把主循环卡住。
     */
#define CAM_WARMUP_MS     2000
#define CAM_WARMUP_FRAMES 20
    {
        int64_t deadline = esp_timer_get_time() + (int64_t)CAM_WARMUP_MS * 1000;
        int discarded = 0;
        while (discarded < CAM_WARMUP_FRAMES || esp_timer_get_time() < deadline) {
            camera_fb_t *fb = esp_camera_fb_get();
            if (fb != NULL) {
                esp_camera_fb_return(fb);
            }
            discarded++;
            vTaskDelay(pdMS_TO_TICKS(20));
        }
        ESP_LOGI(TAG, "预热完成：丢弃 %d 帧（等待 AEC/AGC/AWB 收敛）", discarded);
    }

    /* 板子上的模组装反了，需要水平镜像 */
    sensor_t *sensor = esp_camera_sensor_get();
    if (sensor != NULL) {
        sensor->set_hmirror(sensor, 1);
        sensor->set_vflip(sensor, 0);
        s_c.width = 640;
        s_c.height = 480;
    }

    s_c.ready = true;
    ESP_LOGI(TAG, "摄像头就绪: OV2640 DVP, 默认 VGA, JPEG 质量 %d, %d 个 PSRAM 帧缓冲",
             s_c.quality, CONFIG_SPARKBOT_CAMERA_FB_COUNT);
    return ESP_OK;
}

bool bot_camera_ready(void)
{
    return s_c.ready;
}

void bot_camera_set_frame_cb(bot_camera_frame_cb_t cb)
{
    s_c.cb = cb;
}

void bot_camera_get_info(uint16_t *width, uint16_t *height, int *quality)
{
    if (width != NULL) {
        *width = (uint16_t)s_c.width;
    }
    if (height != NULL) {
        *height = (uint16_t)s_c.height;
    }
    if (quality != NULL) {
        *quality = s_c.quality;
    }
}

/* 抓一帧并交给回调。内部使用，调用方需保证串行。 */
static esp_err_t grab_and_deliver(void)
{
    camera_fb_t *fb = esp_camera_fb_get();
    if (fb == NULL) {
        ESP_LOGW(TAG, "抓帧失败（fb_get 返回 NULL）");
        return ESP_FAIL;
    }

    esp_err_t err = ESP_OK;
    if (s_c.cb != NULL) {
        s_c.cb(fb->buf, fb->len, (uint16_t)fb->width, (uint16_t)fb->height);
    } else {
        ESP_LOGW(TAG, "没有注册帧回调，帧被丢弃");
    }

    s_c.frames_sent++;
    esp_camera_fb_return(fb);
    return err;
}

/*: 借出去的帧（同一时刻最多一份）。 */
static camera_fb_t *s_borrowed = NULL;

esp_err_t bot_camera_frame_borrow(const uint8_t **jpeg, size_t *len,
                                  uint16_t *width, uint16_t *height)
{
    if (!s_c.ready || jpeg == NULL || len == NULL) {
        return ESP_ERR_INVALID_STATE;
    }
    if (s_borrowed != NULL) {
        return ESP_ERR_INVALID_STATE; /* 上一份还没还 */
    }

    xSemaphoreTake(s_c.lock, portMAX_DELAY);
    camera_fb_t *fb = esp_camera_fb_get();
    if (fb == NULL) {
        xSemaphoreGive(s_c.lock);
        ESP_LOGW(TAG, "借帧失败（fb_get 返回 NULL）");
        return ESP_FAIL;
    }
    s_borrowed = fb;
    *jpeg = fb->buf;
    *len = fb->len;
    if (width) {
        *width = (uint16_t)fb->width;
    }
    if (height) {
        *height = (uint16_t)fb->height;
    }
    return ESP_OK;
}

void bot_camera_frame_release(void)
{
    if (s_borrowed == NULL) {
        return;
    }
    esp_camera_fb_return(s_borrowed);
    s_borrowed = NULL;
    xSemaphoreGive(s_c.lock);
}

esp_err_t bot_camera_snapshot(int width, int height, int quality)
{
    if (!s_c.ready) {
        return ESP_ERR_INVALID_STATE;
    }

    xSemaphoreTake(s_c.lock, portMAX_DELAY);

    /* 需要改分辨率/质量时先设置 */
    sensor_t *sensor = esp_camera_sensor_get();
    if (sensor != NULL) {
        if (quality >= 0 && quality != s_c.quality) {
            sensor->set_quality(sensor, quality);
            s_c.quality = quality;
        }
        if (width > 0 && height > 0) {
            if (apply_framesize_locked(sensor, size_from_dims(width, height))) {
                discard_frames(CAM_RESET_DISCARD_FRAMES);
            }
        }
    }

    esp_err_t err = grab_and_deliver();

    xSemaphoreGive(s_c.lock);
    return err;
}

esp_err_t bot_camera_set_params(int width, int height, int quality)
{
    if (!s_c.ready) {
        return ESP_ERR_INVALID_STATE;
    }

    xSemaphoreTake(s_c.lock, portMAX_DELAY);
    sensor_t *sensor = esp_camera_sensor_get();
    if (sensor == NULL) {
        xSemaphoreGive(s_c.lock);
        return ESP_FAIL;
    }

    if (quality >= 0 && quality <= 63) {
        sensor->set_quality(sensor, quality);
        s_c.quality = quality;
    }
    if (width > 0 && height > 0) {
        /* 同 snapshot：只在真的变了才写，写完丢几帧等 AEC 收敛。 */
        if (apply_framesize_locked(sensor, size_from_dims(width, height))) {
            discard_frames(CAM_RESET_DISCARD_FRAMES);
        }
    }
    xSemaphoreGive(s_c.lock);

    ESP_LOGI(TAG, "摄像头参数更新: %dx%d 质量=%d", s_c.width, s_c.height, s_c.quality);
    return ESP_OK;
}

esp_err_t bot_camera_set_stream(bool enabled, float fps, int width, int height)
{
    if (!s_c.ready) {
        return ESP_ERR_INVALID_STATE;
    }

    xSemaphoreTake(s_c.lock, portMAX_DELAY);
    if (enabled) {
        if (fps < 0.2f) {
            fps = 0.2f;
        }
        if (fps > 30.0f) {
            fps = 30.0f;
        }
        s_c.fps = fps;
        s_c.next_frame_us = esp_timer_get_time();

        /* 推流时自动降分辨率与画质：
         * 连续推流下 PC 端只做监控/预览，不需要 VGA 质量，
         * 降下来能显著减少 WiFi 带宽与 CPU 占用。 */
        sensor_t *sensor = esp_camera_sensor_get();
        if (sensor != NULL && (width > 0 || height > 0)) {
            framesize_t want = size_from_dims(width > 0 ? width : 320,
                                              height > 0 ? height : 240);
            /* 同一道保护：反复开预览不该每次都把 AEC 打回起点。 */
            if (apply_framesize_locked(sensor, want)) {
                discard_frames(CAM_RESET_DISCARD_FRAMES);
            }
        }
        s_c.streaming = true;
        ESP_LOGI(TAG, "推流开启: %.1f fps, %dx%d", s_c.fps, s_c.width, s_c.height);
    } else {
        s_c.streaming = false;
        ESP_LOGI(TAG, "推流关闭（共发出 %u 帧）", (unsigned)s_c.frames_sent);
    }
    xSemaphoreGive(s_c.lock);
    return ESP_OK;
}

bool bot_camera_streaming(void)
{
    return s_c.streaming;
}

void bot_camera_poll(void)
{
    if (!s_c.ready || !s_c.streaming) {
        return;
    }

    int64_t now = esp_timer_get_time();
    if (now < s_c.next_frame_us) {
        return;
    }

    xSemaphoreTake(s_c.lock, portMAX_DELAY);

    /* 再检查一次：拿锁期间可能被 set_stream(false) 关掉 */
    if (!s_c.streaming) {
        xSemaphoreGive(s_c.lock);
        return;
    }

    camera_fb_t *fb = esp_camera_fb_get();
    if (fb != NULL) {
        if (s_c.cb != NULL) {
            s_c.cb(fb->buf, fb->len, (uint16_t)fb->width, (uint16_t)fb->height);
        }
        s_c.frames_sent++;
        esp_camera_fb_return(fb);
    }

    /* 排下一帧。用"上次计划时刻 + 周期"而不是"现在 + 周期"，
     * 避免抓帧耗时累积成越来越慢的漂移。 */
    int64_t period = (int64_t)(1000000.0f / s_c.fps);
    s_c.next_frame_us += period;
    if (s_c.next_frame_us < now) {
        s_c.next_frame_us = now + period; /* 落后太多就重新对齐 */
    }

    xSemaphoreGive(s_c.lock);
}
