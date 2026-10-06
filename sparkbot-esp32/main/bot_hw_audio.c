/*
 * 音频实现：ES8311 codec + I2S（麦克风采集 / 喇叭播放 / 音调）
 *
 * ── 三个关键设计，都是踩过坑之后定下来的 ─────────────────────────────
 *
 * 1) **全双工：建通道时同时提供 tx 与 rx 句柄**
 *
 *    这里连着踩了两个坑：
 *
 *    坑一：最初用 `i2s_new_channel(&cfg, &tx, &rx)` 建通道然后同时启用
 *    两个方向 —— 看起来没错，但播放任务持续写 TX 时会把 RX 的读取严重
 *    饿死（实测 8 秒采集窗口只读到 2 片 20ms 数据），在 PC 侧表现为
 *    "麦克风没有声音"。
 *
 *    坑二：为了"共享 DMA"改成 `i2s_new_channel(&cfg, &handle, NULL)`，
 *    以为能拿到一个双功能句柄。**这是错的** —— IDF 的签名是
 *    `i2s_new_channel(config, tx_handle, rx_handle)`，第二个参数是 TX、
 *    第三个是 RX。给第三个传 NULL 等于根本没建接收方向，
 *    `i2s_channel_read()` 永远读不到数据（实测 0 片）。
 *
 *    正确做法就是 IDF 文档里的全双工用法：建通道时**两个句柄都给**，
 *    之后写用 tx、读用 rx。
 *
 * 2) **codec 只在启动时打开一次，之后靠静音位控制**
 *
 *    esp_codec_dev_open() 会重新配置 codec 的时钟与通路，耗时可达数百毫秒。
 *    若把它放在命令处理路径上（start_listen / play_audio），
 *    PC 端等 result 的 5 秒很容易被吃满而报超时（实测 409）。
 *    分开 open 还会让两个方向互相干扰：开麦克风时重新 open 会把正在播放的
 *    喇叭通路一起复位，听起来就是"一说话音乐就断"。
 *
 *    代价是 ADC/DAC 一直有时钟（约十几 mA）。对插着 USB 的调试场景完全
 *    可接受；真要做电池续航优化，可以改成"按需 open + 状态机"，
 *    但那会把复杂度明显推高。
 *
 * 3) **采集任务的栈必须够大**
 *
 *    给 4096 字节时会把板子打重启（串口日志：
 *    `A stack overflow in task bot_mic has been detected`）。
 *    因为调用链很深：采集回调 → bot_proto_send_audio → cJSON 建对象树
 *    → mbedtls_base64_encode → WebSocket 发送。见 CAPTURE_TASK_STACK。
 */

#include "bot_hw_audio.h"

#include <math.h>
#include <stdlib.h>
#include <string.h>

#include "driver/gpio.h"
#include "driver/i2s_std.h"
#include "esp_codec_dev.h"
#include "esp_codec_dev_defaults.h"
#include "esp_log.h"
#include "esp_timer.h"
#include "esp_heap_caps.h"
#include "freertos/FreeRTOS.h"
#include "freertos/idf_additions.h"
#include "freertos/ringbuf.h"
#include "freertos/task.h"

#include "bot_hw_i2c.h"

static const char *TAG = "bot_audio";

/* 播放缓冲：够放约 2 秒 16kHz 单声道 PCM。
 * 太小会导致长语音被截断，太大浪费内存。 */
#define PLAY_RING_BYTES (64 * 1024)

/* I2S DMA 描述符数量与每帧采样数。
 * 采样数取小一点让 DMA 更"碎"，降低采集延迟；描述符多几个保证不断流。 */
#define I2S_DMA_DESC_NUM 6
#define I2S_DMA_FRAME_NUM 240

/* 采集任务栈大小。见文件头说明 3 —— 4096 会 stack overflow。 */
#define CAPTURE_TASK_STACK 8192

/*
 * 采集任务栈放在哪。
 *
 * 这块板子是 ESP32-S3-**N16R8**：16MB Flash + **8MB PSRAM**。
 * 但 FreeRTOS 的任务栈默认只能从**内部 RAM** 分配，而内部 RAM 很紧张：
 * 打开本地唤醒词后，AFE 初始化会占掉大部分内部 RAM，实测只剩
 *
 *     内部RAM可用: 17235 字节, 最大连续块: 7680 字节
 *     PSRAM可用: 7641676 字节
 *
 * 最大连续块 7680 < 栈所需的 8192，于是 xTaskCreate 直接失败，
 * 表现为 `start_listen` 返回 "麦克风不可用"（hardware_fault）——
 * 现象是"麦克风坏了"，实际是**内存放不下栈**。
 *
 * 所以这里显式把栈放到 PSRAM（`xTaskCreateWithCaps` + MALLOC_CAP_SPIRAM）。
 * 代价是栈访问要走 PSRAM，比内部 RAM 稍慢；对采集任务无所谓 ——
 * 它绝大部分时间阻塞在 I2S 读上。
 *
 * ⚠️ 用 WithCaps 创建的任务**必须**用 `vTaskDeleteWithCaps()` 退出，
 * 用普通的 `vTaskDelete()` 会按内部 RAM 去释放 PSRAM 的栈，
 * 导致堆损坏（这类崩溃往往在很久之后才暴露，极难定位）。
 */
#define CAPTURE_TASK_NAME "bot_mic"
#define CAPTURE_TASK_PRIO 4
#define CAPTURE_TASK_CAPS MALLOC_CAP_SPIRAM

typedef struct {
    bool ready;
    /* 全双工的收发句柄，由同一次 i2s_new_channel() 创建（见文件头说明 1） */
    i2s_chan_handle_t tx;
    i2s_chan_handle_t rx;
    esp_codec_dev_handle_t codec;

    bot_audio_capture_cb_t capture_cb;
    bot_audio_done_cb_t done_cb;
    bot_audio_monitor_cb_t monitor_cb;

    /*
     * 采集任务是否在跑。
     *
     * **常开** —— 启动后就一直为 true，不再随 listen 会话启停。
     * 原因见 bot_hw_audio.h 里 bot_audio_start_monitor() 的说明：
     * 唤醒词要求麦克风一直有数据，否则"喊一声唤醒"根本无从谈起。
     */
    volatile bool capture_active;

    /*
     * 是否把 PCM 上行给 PC。
     *
     * 与 capture_active 分开是**关键**：采集任务常开，但只有 PC 明确
     * 要求采集（listen 会话）时才上传音频。否则设备会把环境声音
     * 一直推给 PC，既浪费带宽又浪费 PC 的算力。
     */
    volatile bool uplink_active;

    TaskHandle_t capture_task;
    TaskHandle_t play_task;

    RingbufHandle_t play_rb;
    volatile bool playing;
    volatile bool stop_requested;

    int volume;
    int sample_rate;
} audio_ctx_t;

static audio_ctx_t s_a;

/* ------------------------------------------------------------------ */
/* 采集任务                                                           */
/* ------------------------------------------------------------------ */

static void capture_task(void *arg)
{
    const int rate = s_a.sample_rate;
    /* 分片时长来自 Kconfig（默认 20ms）。16kHz 单声道 16bit = 32 字节/ms。 */
    const int chunk_ms = CONFIG_SPARKBOT_AUDIO_CAPTURE_CHUNK_MS;
    int chunk_bytes = rate * 2 * chunk_ms / 1000;
    if (chunk_bytes < 64) {
        chunk_bytes = 64;
    }

    /* 每次从 I2S 读的采样数：正好一个分片的量。
     * 读太少会增加循环开销，读太多则分片会被切开。 */
    size_t read_samples = (size_t)(rate * chunk_ms / 1000);
    if (read_samples < 128) {
        read_samples = 128;
    }

    int16_t *raw = malloc(read_samples * sizeof(int16_t));
    uint8_t *pcm = malloc((size_t)chunk_bytes);
    if (raw == NULL || pcm == NULL) {
        ESP_LOGE(TAG, "采集缓冲分配失败");
        free(raw);
        free(pcm);
        s_a.capture_task = NULL;
        vTaskDeleteWithCaps(NULL); /* 见 CAPTURE_TASK_CAPS 的说明 */
        return;
    }

    ESP_LOGI(TAG, "麦克风采集开始: %dHz 分片 %dms (%d 字节)", rate, chunk_ms, chunk_bytes);

    uint32_t reads = 0;    /* 成功读次数 */
    uint32_t timeouts = 0; /* 超时次数 */
    size_t fill = 0;

    while (s_a.capture_active) {
        size_t bytes_read = 0;
        esp_err_t err = i2s_channel_read(s_a.rx, raw, read_samples * sizeof(int16_t),
                                        &bytes_read, pdMS_TO_TICKS(200));
        if (err == ESP_ERR_TIMEOUT) {
            timeouts++;
            continue; /* 正常：暂时没数据 */
        }
        if (err != ESP_OK) {
            ESP_LOGW(TAG, "I2S 读失败: %s", esp_err_to_name(err));
            vTaskDelay(pdMS_TO_TICKS(10));
            continue;
        }
        if (bytes_read == 0) {
            continue;
        }
        reads++;

        /* 把读到的字节按分片长度切开送出 */
        size_t off = 0;
        while (off < bytes_read) {
            size_t room = (size_t)chunk_bytes - fill;
            size_t take = bytes_read - off;
            if (take > room) {
                take = room;
            }
            memcpy(pcm + fill, (const uint8_t *)raw + off, take);
            fill += take;
            off += take;

            if (fill == (size_t)chunk_bytes) {
                /* 顺序有讲究（沿用改造前的结论）：
                 * 先喂唤醒引擎，再送上行回调。唤醒引擎需要的是"最新"
                 * 数据，而上行回调会写网络、可能阻塞；先喂唤醒能保证
                 * 它拿到的是低延迟的实时数据。
                 *
                 * 两者的**开关是独立的**：
                 *   monitor_cb —— 常开（唤醒词要一直听）
                 *   capture_cb —— 只在 PC 要求采集时（uplink_active）
                 * 这样采集任务可以常驻，而不会把环境声音一直推给 PC。 */
                if (s_a.monitor_cb != NULL) {
                    s_a.monitor_cb(pcm, (size_t)chunk_bytes);
                }
                if (s_a.uplink_active && s_a.capture_cb != NULL) {
                    s_a.capture_cb(pcm, (size_t)chunk_bytes);
                }
                fill = 0;
            }
        }
    }

    /*
     * 收尾诊断：这两个数字是排查"麦克风没声音"的第一手依据。
     *   reads 远小于预期（8 秒应有约 400 次）→ I2S 根本没读到数据
     *   reads 正常但 PC 只收到很少 → 上行链路或任务被饿死
     */
    ESP_LOGI(TAG, "麦克风采集结束: 成功读 %u 次, 超时 %u 次", (unsigned)reads, (unsigned)timeouts);

    free(raw);
    free(pcm);
    s_a.capture_task = NULL;
    vTaskDeleteWithCaps(NULL); /* 栈在 PSRAM，必须用带 Caps 的版本释放 */
}

esp_err_t bot_audio_capture_start(void)
{
    if (!s_a.ready) {
        return ESP_ERR_INVALID_STATE;
    }
    if (s_a.capture_active) {
        return ESP_OK; /* 已在采集 */
    }
    if (s_a.rx == NULL) {
        ESP_LOGW(TAG, "I2S RX 通道不可用，无法采集");
        return ESP_ERR_INVALID_STATE;
    }

    /* 不再调用 esp_codec_dev_open()：见文件头说明 2。
     * 采集的启停只靠**输入静音位**控制。 */
    esp_codec_dev_set_in_mute(s_a.codec, false);
    esp_codec_dev_set_in_gain(s_a.codec, (float)CONFIG_SPARKBOT_AUDIO_MIC_GAIN_DB);

    s_a.capture_active = true;    if (s_a.capture_task == NULL) {
        /*
         * 优先把栈放到 PSRAM（见 CAPTURE_TASK_CAPS 的详细说明）。
         *
         * 优先级 4：略低于主循环任务。采集只要不丢样本就行，
         * 不该和协议处理抢 CPU —— 高优先级会拖慢命令响应，
         * 反而更容易触发 PC 端超时。
         */
        BaseType_t created = xTaskCreateWithCaps(capture_task, CAPTURE_TASK_NAME,
                                                CAPTURE_TASK_STACK, NULL,
                                                CAPTURE_TASK_PRIO, &s_a.capture_task,
                                                CAPTURE_TASK_CAPS);

        if (created != pdPASS) {
            /* PSRAM 也失败（极少见）时退回内部 RAM —— 内部 RAM 够的话
             * 仍能工作，只是和 AFE 抢内存。这条回退让"唤醒词吃满内存"
             * 不至于把麦克风彻底打死。 */
            ESP_LOGW(TAG, "PSRAM 栈创建失败，回退到内部 RAM");
            created = xTaskCreate(capture_task, CAPTURE_TASK_NAME,
                                  CAPTURE_TASK_STACK, NULL, CAPTURE_TASK_PRIO,
                                  &s_a.capture_task);
        }

        if (created != pdPASS) {
            s_a.capture_active = false;
            /* 把内存状况一起打出来 —— "创建任务失败"几乎总是内存不够，
             * 但光看这句话没法判断还差多少、PSRAM 是否还有余量。 */
            ESP_LOGE(TAG, "创建采集任务失败（需要 %d 字节栈）", CAPTURE_TASK_STACK);
            ESP_LOGE(TAG, "  内部RAM可用: %u 字节, 最大连续块: %u 字节",
                     (unsigned)heap_caps_get_free_size(MALLOC_CAP_INTERNAL),
                     (unsigned)heap_caps_get_largest_free_block(MALLOC_CAP_INTERNAL | MALLOC_CAP_8BIT));
            ESP_LOGE(TAG, "  PSRAM可用: %u 字节, 最大连续块: %u 字节",
                     (unsigned)heap_caps_get_free_size(MALLOC_CAP_SPIRAM),
                     (unsigned)heap_caps_get_largest_free_block(MALLOC_CAP_SPIRAM | MALLOC_CAP_8BIT));
            return ESP_FAIL;
        }
        ESP_LOGI(TAG, "采集任务已创建（栈 %d 字节，来自 %s）", CAPTURE_TASK_STACK,
                 CAPTURE_TASK_CAPS == MALLOC_CAP_SPIRAM ? "PSRAM" : "内部RAM");
    }
    return ESP_OK;
}

void bot_audio_capture_stop(void)
{
    if (!s_a.capture_active) {
        return;
    }
    s_a.capture_active = false;
    /* 等任务自己退出（它最多阻塞 200ms 在 I2S 读上） */
    for (int i = 0; i < 30 && s_a.capture_task != NULL; i++) {
        vTaskDelay(pdMS_TO_TICKS(20));
    }
    if (s_a.codec != NULL) {
        /* 只静音输入，不 close —— 见文件头说明 2 */
        esp_codec_dev_set_in_mute(s_a.codec, true);
    }
    ESP_LOGI(TAG, "麦克风已关闭");
}

esp_err_t bot_audio_start_monitor(void)
{
    /* 常开麦克风：采集任务一直跑，monitor_cb（唤醒词）一直有数据。
     * 上行由 bot_audio_set_uplink() 单独控制。 */
    s_a.uplink_active = false;
    esp_err_t err = bot_audio_capture_start();
    if (err == ESP_OK) {
        ESP_LOGI(TAG, "麦克风常开（唤醒检测持续工作），音频上行待命");
    }
    return err;
}

void bot_audio_set_uplink(bool enable)
{
    if (s_a.uplink_active == enable) {
        return;
    }
    s_a.uplink_active = enable;
    ESP_LOGI(TAG, "音频上行 %s", enable ? "开启" : "关闭");
}

bool bot_audio_uplink_active(void)
{
    return s_a.uplink_active;
}

bool bot_audio_capture_active(void)
{
    return s_a.capture_active;
}

void bot_audio_set_capture_cb(bot_audio_capture_cb_t cb)
{
    s_a.capture_cb = cb;
}

void bot_audio_set_done_cb(bot_audio_done_cb_t cb)
{
    s_a.done_cb = cb;
}

void bot_audio_set_monitor_cb(bot_audio_monitor_cb_t cb)
{
    s_a.monitor_cb = cb;
}

/* ------------------------------------------------------------------ */
/* 播放任务                                                           */
/* ------------------------------------------------------------------ */

static void play_task(void *arg)
{
    ESP_LOGI(TAG, "播放任务启动");

    /* 空闲时先静音，避免底噪；有数据时再解除 */
    esp_codec_dev_set_out_mute(s_a.codec, true);
    esp_codec_dev_set_out_vol(s_a.codec, s_a.volume);

    while (true) {
        size_t got = 0;
        /* 阻塞等数据；超时后回到循环检查退出条件 */
        uint8_t *chunk = xRingbufferReceiveUpTo(s_a.play_rb, &got, pdMS_TO_TICKS(100), 4096);
        if (chunk == NULL) {
            /* 没数据。如果之前正在播，说明播完了。 */
            if (s_a.playing) {
                s_a.playing = false;
                esp_codec_dev_set_out_mute(s_a.codec, true);
                ESP_LOGI(TAG, "播放结束");
                if (s_a.done_cb != NULL) {
                    s_a.done_cb();
                }
            }
            continue;
        }

        s_a.playing = true;
        esp_codec_dev_set_out_mute(s_a.codec, false);

        if (s_a.stop_requested) {
            vRingbufferReturnItem(s_a.play_rb, chunk);
            /* 把缓冲里剩下的也倒掉 */
            while ((chunk = xRingbufferReceiveUpTo(s_a.play_rb, &got, 0, 4096)) != NULL) {
                vRingbufferReturnItem(s_a.play_rb, chunk);
            }
            s_a.stop_requested = false;
            s_a.playing = false;
            esp_codec_dev_set_out_mute(s_a.codec, true);
            continue;
        }

        /* 数据在推入缓冲前已由 bot_audio_play() 重采样到板子采样率，
         * 所以这里直接写即可。 */
        size_t written = 0;
        esp_err_t werr = i2s_channel_write(s_a.tx, chunk, got, &written, pdMS_TO_TICKS(1000));
        vRingbufferReturnItem(s_a.play_rb, chunk);

        if (werr != ESP_OK) {
            ESP_LOGW(TAG, "I2S 写失败: %s (写入 %u/%u)", esp_err_to_name(werr),
                     (unsigned)written, (unsigned)got);
        }
    }
}

/* ------------------------------------------------------------------ */
/* 初始化                                                             */
/* ------------------------------------------------------------------ */

esp_err_t bot_audio_init(void)
{
    memset(&s_a, 0, sizeof(s_a));
    s_a.sample_rate = CONFIG_SPARKBOT_AUDIO_SAMPLE_RATE;
    s_a.volume = CONFIG_SPARKBOT_AUDIO_VOLUME;

#if !CONFIG_SPARKBOT_AUDIO_ENABLE
    ESP_LOGI(TAG, "音频未启用（menuconfig）");
    return ESP_OK;
#endif

    if (!bot_i2c_ready()) {
        ESP_LOGW(TAG, "I2C 未就绪，codec 无法配置，音频不可用");
        return ESP_OK;
    }

    /* ---- I2S 全双工通道 ---- */
    i2s_chan_config_t chan_cfg = I2S_CHANNEL_DEFAULT_CONFIG(I2S_NUM_0, I2S_ROLE_MASTER);
    chan_cfg.dma_desc_num = I2S_DMA_DESC_NUM;
    chan_cfg.dma_frame_num = I2S_DMA_FRAME_NUM;
    /* 自动清空 DMA 缓冲：避免停止/重开后残留旧音频造成爆音 */
    chan_cfg.auto_clear = true;

    /* 一次调用同时建出收发两个方向。
     * IDF 的参数顺序是 (config, tx_handle, rx_handle) —— 两个都要给，
     * 只给 tx 的话接收方向根本没建起来（见文件头说明 1 的坑二）。 */
    esp_err_t err = i2s_new_channel(&chan_cfg, &s_a.tx, &s_a.rx);
    if (err != ESP_OK) {
        ESP_LOGE(TAG, "I2S 通道创建失败: %s", esp_err_to_name(err));
        return err;
    }

    i2s_std_config_t std_cfg = {
        .clk_cfg = I2S_STD_CLK_DEFAULT_CONFIG(s_a.sample_rate),
        .slot_cfg = I2S_STD_PHILIPS_SLOT_DEFAULT_CONFIG(I2S_DATA_BIT_WIDTH_16BIT,
                                                       I2S_SLOT_MODE_MONO),
        .gpio_cfg = {
            .mclk = CONFIG_SPARKBOT_AUDIO_MCLK_PIN,
            .bclk = CONFIG_SPARKBOT_AUDIO_BCLK_PIN,
            .ws = CONFIG_SPARKBOT_AUDIO_WS_PIN,
            .dout = CONFIG_SPARKBOT_AUDIO_DOUT_PIN,
            .din = CONFIG_SPARKBOT_AUDIO_DIN_PIN,
            .invert_flags = {
                .mclk_inv = false,
                .bclk_inv = false,
                .ws_inv = false,
            },
        },
    };
    /* MCLK 倍数：ES8311 要求采样率 × 256 */
    std_cfg.clk_cfg.mclk_multiple = I2S_MCLK_MULTIPLE_256;

    err = i2s_channel_init_std_mode(s_a.tx, &std_cfg);
    if (err != ESP_OK) {
        ESP_LOGE(TAG, "I2S TX 初始化失败: %s", esp_err_to_name(err));
        return err;
    }
    err = i2s_channel_init_std_mode(s_a.rx, &std_cfg);
    if (err != ESP_OK) {
        ESP_LOGE(TAG, "I2S RX 初始化失败: %s", esp_err_to_name(err));
        return err;
    }
    err = i2s_channel_enable(s_a.tx);
    if (err != ESP_OK) {
        ESP_LOGE(TAG, "I2S TX 使能失败: %s", esp_err_to_name(err));
        return err;
    }
    err = i2s_channel_enable(s_a.rx);
    if (err != ESP_OK) {
        ESP_LOGE(TAG, "I2S RX 使能失败: %s", esp_err_to_name(err));
        return err;
    }

    /* ---- ES8311 codec ---- */
    i2c_master_dev_handle_t codec_i2c = NULL;
    err = bot_i2c_add_device(CONFIG_SPARKBOT_AUDIO_CODEC_ADDR, 400000, &codec_i2c);
    if (err != ESP_OK) {
        ESP_LOGE(TAG, "挂载 ES8311 失败");
        return err;
    }

    audio_codec_i2c_cfg_t i2c_cfg = {
        .addr = CONFIG_SPARKBOT_AUDIO_CODEC_ADDR,
        .bus_handle = bot_i2c_bus(),
    };
    const audio_codec_ctrl_if_t *ctrl_if = audio_codec_new_i2c_ctrl(&i2c_cfg);
    if (ctrl_if == NULL) {
        ESP_LOGE(TAG, "codec 控制接口创建失败");
        return ESP_FAIL;
    }

    const audio_codec_gpio_if_t *gpio_if = audio_codec_new_gpio();
    audio_codec_i2s_cfg_t i2s_cfg = {
        .port = I2S_NUM_0,
        .rx_handle = s_a.rx,
        .tx_handle = s_a.tx,
    };
    const audio_codec_data_if_t *data_if = audio_codec_new_i2s_data(&i2s_cfg);
    if (data_if == NULL) {
        ESP_LOGE(TAG, "codec 数据接口创建失败");
        return ESP_FAIL;
    }

    esp_codec_dev_hw_gain_t hw_gain = {
        .pa_voltage = 5.0,
        .codec_dac_voltage = 3.3,
    };
    es8311_codec_cfg_t es_cfg = {
        .ctrl_if = ctrl_if,
        .gpio_if = gpio_if,
        .codec_mode = ESP_CODEC_DEV_WORK_MODE_BOTH, /* DAC + ADC 同时开 */
        .pa_pin = CONFIG_SPARKBOT_AUDIO_PA_PIN,
        .pa_reverted = false,
        .master_mode = false, /* ESP32 做 I2S 主机，codec 从机 */
        .use_mclk = true,
        .digital_mic = false,
        .hw_gain = hw_gain,
    };
    const audio_codec_if_t *codec_if = es8311_codec_new(&es_cfg);
    if (codec_if == NULL) {
        ESP_LOGE(TAG, "ES8311 驱动创建失败（芯片没应答？检查 I2C 与供电）");
        return ESP_FAIL;
    }

    esp_codec_dev_cfg_t dev_cfg = {
        .codec_if = codec_if,
        .data_if = data_if,
        .dev_type = ESP_CODEC_DEV_TYPE_IN_OUT,
    };
    s_a.codec = esp_codec_dev_new(&dev_cfg);
    if (s_a.codec == NULL) {
        ESP_LOGE(TAG, "codec 设备创建失败");
        return ESP_FAIL;
    }

    /* ---- 一次性打开 codec（收发同时）---- 见文件头说明 2 */
    esp_codec_dev_sample_info_t fs = {
        .sample_rate = s_a.sample_rate,
        .channel = 1,
        .bits_per_sample = 16,
    };
    err = esp_codec_dev_open(s_a.codec, &fs);
    if (err != ESP_OK) {
        ESP_LOGE(TAG, "codec 打开失败: %s", esp_err_to_name(err));
        return err;
    }
    esp_codec_dev_set_out_vol(s_a.codec, s_a.volume);
    esp_codec_dev_set_in_gain(s_a.codec, (float)CONFIG_SPARKBOT_AUDIO_MIC_GAIN_DB);
    /* 空闲时两个方向都静音，避免底噪，也避免误采 */
    esp_codec_dev_set_out_mute(s_a.codec, true);
    esp_codec_dev_set_in_mute(s_a.codec, true);

    /* ---- 播放环形缓冲与任务 ---- */
    s_a.play_rb = xRingbufferCreate(PLAY_RING_BYTES, RINGBUF_TYPE_BYTEBUF);
    if (s_a.play_rb == NULL) {
        ESP_LOGE(TAG, "播放缓冲创建失败");
        return ESP_ERR_NO_MEM;
    }
    if (xTaskCreate(play_task, "bot_spk", 4096, NULL, 5, &s_a.play_task) != pdPASS) {
        ESP_LOGE(TAG, "创建播放任务失败");
        return ESP_FAIL;
    }

    s_a.ready = true;
    ESP_LOGI(TAG, "音频就绪: %dHz 单声道全双工, 音量 %d%%, MCLK=%d BCLK=%d WS=%d DOUT=%d DIN=%d PA=%d",
             s_a.sample_rate, s_a.volume, CONFIG_SPARKBOT_AUDIO_MCLK_PIN,
             CONFIG_SPARKBOT_AUDIO_BCLK_PIN, CONFIG_SPARKBOT_AUDIO_WS_PIN,
             CONFIG_SPARKBOT_AUDIO_DOUT_PIN, CONFIG_SPARKBOT_AUDIO_DIN_PIN,
             CONFIG_SPARKBOT_AUDIO_PA_PIN);
    return ESP_OK;
}

bool bot_audio_ready(void)
{
    return s_a.ready;
}

/* ------------------------------------------------------------------ */
/* 播放接口                                                           */
/* ------------------------------------------------------------------ */

/*
 * 解析 WAV 头，取出采样率与数据偏移。
 *
 * 只支持最常见的 PCM WAV（format 1）。PC 端 TTS 输出通常是这种。
 * 返回 false 表示不是能识别的 WAV。
 */
static bool parse_wav(const uint8_t *data, size_t len, int *out_rate, size_t *out_off)
{
    if (len < 44) {
        return false;
    }
    if (memcmp(data, "RIFF", 4) != 0 || memcmp(data + 8, "WAVE", 4) != 0) {
        return false;
    }

    size_t pos = 12;
    int rate = 0;
    size_t data_off = 0;

    while (pos + 8 <= len) {
        const uint8_t *id = data + pos;
        uint32_t chunk_len = (uint32_t)data[pos + 4] | ((uint32_t)data[pos + 5] << 8)
                             | ((uint32_t)data[pos + 6] << 16) | ((uint32_t)data[pos + 7] << 24);
        size_t body = pos + 8;

        if (memcmp(id, "fmt ", 4) == 0 && chunk_len >= 16 && body + 16 <= len) {
            uint16_t fmt = (uint16_t)(data[body] | (data[body + 1] << 8));
            if (fmt != 1) {
                ESP_LOGW(TAG, "WAV 不是 PCM 格式 (fmt=%u)，不支持", fmt);
                return false;
            }
            rate = (int)((uint32_t)data[body + 4] | ((uint32_t)data[body + 5] << 8)
                         | ((uint32_t)data[body + 6] << 16) | ((uint32_t)data[body + 7] << 24));
        } else if (memcmp(id, "data", 4) == 0) {
            data_off = body;
            break;
        }

        pos = body + chunk_len + (chunk_len & 1); /* chunk 按偶数字节对齐 */
    }

    if (rate <= 0 || data_off == 0 || data_off >= len) {
        return false;
    }
    *out_rate = rate;
    *out_off = data_off;
    return true;
}

/*
 * 简单线性重采样：把 src_rate 的 16bit 单声道 PCM 转到 dst_rate。
 *
 * 为什么需要：PC 端 TTS 常见输出 24kHz，而板子 I2S 固定 16kHz。
 * 不重采样的话语速会变成 1.5 倍（听起来像快进）。
 * 线性插值对语音清晰度足够，且不需要额外依赖。
 */
static size_t resample_s16_mono(const int16_t *src, size_t src_samples,
                                int src_rate, int dst_rate, int16_t *dst, size_t dst_cap)
{
    if (src_rate == dst_rate) {
        size_t n = src_samples < dst_cap ? src_samples : dst_cap;
        memcpy(dst, src, n * sizeof(int16_t));
        return n;
    }

    size_t out_n = (size_t)((uint64_t)src_samples * (uint32_t)dst_rate / (uint32_t)src_rate);
    if (out_n > dst_cap) {
        out_n = dst_cap;
    }

    for (size_t i = 0; i < out_n; i++) {
        double pos = (double)i * (double)src_rate / (double)dst_rate;
        size_t i0 = (size_t)pos;
        double frac = pos - (double)i0;
        size_t i1 = i0 + 1 < src_samples ? i0 + 1 : i0;
        double v = (double)src[i0] * (1.0 - frac) + (double)src[i1] * frac;
        if (v > 32767.0) {
            v = 32767.0;
        }
        if (v < -32768.0) {
            v = -32768.0;
        }
        dst[i] = (int16_t)v;
    }
    return out_n;
}

esp_err_t bot_audio_play_raw_i16(const int16_t *samples, size_t count)
{
    if (!s_a.ready || samples == NULL || count == 0) {
        return ESP_ERR_INVALID_STATE;
    }

    /* 分块推入环形缓冲。满时等待播放任务消费。 */
    const uint8_t *p = (const uint8_t *)samples;
    size_t remaining = count * sizeof(int16_t);
    int64_t deadline = esp_timer_get_time() + 15000 * 1000; /* 15s 上限，防止永久卡住 */

    while (remaining > 0) {
        size_t chunk = remaining > 2048 ? 2048 : remaining;
        if (xRingbufferSend(s_a.play_rb, p, chunk, pdMS_TO_TICKS(200)) != pdTRUE) {
            if (esp_timer_get_time() > deadline) {
                ESP_LOGW(TAG, "播放缓冲长时间写不进去，放弃");
                return ESP_ERR_TIMEOUT;
            }
            continue; /* 缓冲满，等播放任务消费 */
        }
        p += chunk;
        remaining -= chunk;
    }
    return ESP_OK;
}

esp_err_t bot_audio_play(const uint8_t *data, size_t len, bool is_wav, int sample_rate)
{
    if (!s_a.ready || data == NULL || len == 0) {
        return ESP_ERR_INVALID_STATE;
    }

    const uint8_t *pcm = data;
    size_t pcm_len = len;
    int src_rate = sample_rate > 0 ? sample_rate : s_a.sample_rate;

    if (is_wav) {
        size_t off = 0;
        int wav_rate = 0;
        if (!parse_wav(data, len, &wav_rate, &off)) {
            ESP_LOGW(TAG, "WAV 解析失败");
            return ESP_ERR_INVALID_ARG;
        }
        pcm = data + off;
        pcm_len = len - off;
        src_rate = wav_rate;
    }

    /* 先停掉正在播的内容：新语音应该打断旧的，而不是排队等 */
    bot_audio_stop_playback();

    /* 重采样到板子的采样率 */
    if (src_rate != s_a.sample_rate) {
        size_t src_samples = pcm_len / 2;
        size_t dst_cap = (size_t)((uint64_t)src_samples * (uint32_t)s_a.sample_rate
                                  / (uint32_t)src_rate) + 64;
        int16_t *dst = malloc(dst_cap * sizeof(int16_t));
        if (dst == NULL) {
            return ESP_ERR_NO_MEM;
        }
        size_t n = resample_s16_mono((const int16_t *)pcm, src_samples, src_rate,
                                     s_a.sample_rate, dst, dst_cap);
        esp_err_t err = bot_audio_play_raw_i16(dst, n);
        free(dst);
        return err;
    }

    return bot_audio_play_raw_i16((const int16_t *)pcm, pcm_len / 2);
}

void bot_audio_stop_playback(void)
{
    if (!s_a.ready) {
        return;
    }
    /*
     * 只要缓冲区里还有数据或正在播，就请求停止。
     * 播放任务看到标志后会把缓冲倒空。
     *
     * 注意这里**不直接 mute**：如果立刻静音，正在写 I2S 的那一小段
     * 会被硬切掉，听感上是一个明显的"咔"。让播放任务自己清空缓冲、
     * 在循环末尾静音，过渡更自然。
     */
    if (s_a.playing || xRingbufferGetCurFreeSize(s_a.play_rb) < PLAY_RING_BYTES) {
        s_a.stop_requested = true;
    }
}

bool bot_audio_playing(void)
{
    return s_a.playing;
}

/* ------------------------------------------------------------------ */
/* 音调                                                               */
/* ------------------------------------------------------------------ */

esp_err_t bot_audio_play_tone(int freq_hz, int duration_ms, int volume_percent)
{
    if (!s_a.ready) {
        return ESP_ERR_INVALID_STATE;
    }
    if (freq_hz < 20) {
        freq_hz = 20;
    }
    /* 不超过奈奎斯特频率的 45%，避免混叠成怪声 */
    int max_freq = s_a.sample_rate * 45 / 100;
    if (freq_hz > max_freq) {
        freq_hz = max_freq;
    }
    if (duration_ms <= 0) {
        duration_ms = 100;
    }
    if (duration_ms > 10000) {
        duration_ms = 10000;
    }
    if (volume_percent < 0) {
        volume_percent = 0;
    }
    if (volume_percent > 100) {
        volume_percent = 100;
    }

    int rate = s_a.sample_rate;
    size_t total = (size_t)rate * (size_t)duration_ms / 1000;
    int16_t *buf = malloc(total * sizeof(int16_t));
    if (buf == NULL) {
        return ESP_ERR_NO_MEM;
    }

    /* 正弦 + 两端 5% 淡入淡出：不加淡变会有明显爆音 */
    double amp = 32767.0 * (double)volume_percent / 100.0 * 0.6;
    size_t fade = total / 20;
    if (fade < 1) {
        fade = 1;
    }
    for (size_t i = 0; i < total; i++) {
        double env = 1.0;
        if (i < fade) {
            env = (double)i / (double)fade;
        } else if (i + fade >= total) {
            env = (double)(total - i) / (double)fade;
        }
        double v = amp * env * sin(2.0 * M_PI * (double)freq_hz * (double)i / (double)rate);
        buf[i] = (int16_t)v;
    }

    esp_err_t err = bot_audio_play_raw_i16(buf, total);
    free(buf);
    return err;
}

void bot_audio_set_volume(int percent)
{
    if (percent < 0) {
        percent = 0;
    }
    if (percent > 100) {
        percent = 100;
    }
    s_a.volume = percent;
    if (s_a.codec != NULL) {
        esp_codec_dev_set_out_vol(s_a.codec, percent);
    }
}

int bot_audio_get_volume(void)
{
    return s_a.volume;
}

int bot_audio_sample_rate(void)
{
    return s_a.sample_rate;
}
