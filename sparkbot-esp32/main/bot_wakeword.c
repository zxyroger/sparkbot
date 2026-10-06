/*
 * 本地唤醒词：esp-sr AFE + WakeNet9（模型 wn9_hixiaoxing_tts = "Hi,小星"）
 *
 * ── 唤醒词到底是什么 ─────────────────────────────────────────────────
 *
 * 模型训练好的发音是 **"Hi,小星"**（Hi xiao xing）。叫它不是"你好小星" ——
 * 官方开放的唤醒词里**没有**"你好小星"。自定义训练要 ≥2 万条语料、
 * 2~3 周、并支付定制费用，不适合本项目。详见 README。
 *
 * ── 版本选择（关键教训）─────────────────────────────────────────────
 *
 * 本模块**必须**用 esp-sr **2.x**（本工程锁 2.4.7），不能用 1.9.x。
 *
 *   * 1.9.5 只能手工填 `AFE_CONFIG_DEFAULT()`。我按参考实现反复调整
 *     （优先级、ringbuf、VAD/SE、检测模式、通道数、帧长 160/512/1024、
 *     LOW_COST/HIGH_PERF）**全都无效**：`fetch()` 始终返回 ret=-1、
 *     size=0，内部流水线不产出任何结果。
 *   * 2.x 提供 `afe_config_init(input_format, models, type, mode)`，
 *     由它按输入通道格式（"M" / "MR"）**推导**出整套内部参数。
 *     同芯片（ESP32-S3）、同 IDF（5.5.4）的 xiaozhi-esp32 工程
 *     正是用这个 API 跑通唤醒词的。
 *
 * 结论：这不是参数问题，是**API 代际问题**。手工填配置走不通。
 *
 * ── 数据从哪来（架构）──────────────────────────────────────────────
 *
 * 唤醒引擎**不自己读 I2S**，也**不在采集任务里跑**：
 *
 *   采集任务 ──(片)──> 帧队列 ──> AFE worker 任务 ──> feed()/fetch()
 *      (只投递,     (满则丢帧)      (可以阻塞)
 *       绝不阻塞)
 *
 * 为什么要这样分层：`fetch()` 实测会阻塞数秒。当初直接在采集任务里调用，
 * 10ms 的音频帧要花数秒处理，采集被彻底饿死（10 秒只有 2 次 I2S 读）。
 * 解耦后采集恢复到 400~500 次/10 秒。
 *
 * ── 一个重要的限制：唤醒只在采集窗口内有效 ──────────────────────────
 *
 * 麦克风只在采集期间被打开（codec 用静音位控制），所以**只有设备正在
 * 采集音频时**唤醒词才会被检测。要做到"随时喊一声就响应"，需要麦克风
 * 常开（功耗上升）并把 AFE 的输入源改成常开采集流。这是明确的下一步。
 */

#include "bot_wakeword.h"

#include <stdlib.h>
#include <string.h>

#include "esp_afe_sr_iface.h"
#include "esp_afe_sr_models.h"
#include "esp_heap_caps.h"
#include "esp_log.h"
#include "esp_timer.h"
#include "freertos/idf_additions.h"
#include "freertos/ringbuf.h"
#include "freertos/task.h"
#include "model_path.h"

#include "bot_hw_audio.h"

static const char *TAG = "bot_wake";

/* 模型所在分区的标签。**必须和 partitions_16m.csv 里的名字一致**：
 * esp-sr 的构建脚本也按这个名字查找并自动烧写 srmodels.bin。 */
#define MODEL_PARTITION_LABEL "model"

/* 麦克风通道数。本板只有一个麦克风，没有回采参考通道，
 * 因此输入格式是 "M"（单麦）。 */
#define AFE_MIC_NUM 1

/*
 * 帧队列：采集任务 → AFE worker。
 *
 * 队列满时**丢帧**：丢几帧音频唤醒词照样能识别，但阻塞采集是不可接受的。
 * 深度按"一帧的时长"折算，16 帧足够吸收 fetch 的抖动。
 */
#define FRAME_QUEUE_DEPTH 16
#define AFE_WORKER_STACK 8192
#define AFE_WORKER_PRIO 5

/*
 * 命中后的冷却时间（毫秒）。
 *
 * 唤醒检测在采集期间照常运行（麦克风常开），用户在对话中说出与唤醒词
 * 相近的音就会再次触发。实测"嗨小新"能触发「Hi,小星」，于是 PC 端收到
 * 第二个 wake 事件，在上一轮还没播报完时又开一次采集，两个采集会话互
 * 相打断 —— 表现为"只能对话一次"。
 *
 * 4 秒足够覆盖"采集 + 识别 + 播报"的全过程：这段时间本来也不需要再次
 * 唤醒。数值太小起不到作用，太大会让用户觉得喊了没反应。
 */
#define WAKE_COOLDOWN_MS 4000

typedef struct {
    bool ready;
    const esp_afe_sr_iface_t *afe;
    esp_afe_sr_data_t *data;
    srmodel_list_t *models; /* 从分区加载的模型列表，需持有到销毁 */

    bot_wakeword_cb_t cb;

    int feed_samples;         /* 每次 feed 的采样数（含所有输入通道） */
    int16_t *frame;           /* 整帧缓冲 */
    RingbufHandle_t frame_rb; /* 采集任务投递整帧的队列 */
    TaskHandle_t worker;
    int pending; /* 采集任务侧累积但还没凑满一帧的采样数 */

    uint32_t fed;     /* 累计 feed 次数 */
    uint32_t dropped; /* 队列满而丢弃的帧数 */
    uint32_t hits;    /* 命中次数 */
    /* 冷却期抑制掉的命中次数。这个值偏高通常意味着用户说话内容
     * 与唤醒词发音接近，或麦克风把扬声器的声音也收了进来。 */
    uint32_t hits_suppressed;
    int64_t last_hit_us;    /* 上次命中的时间戳，用于冷却判断 */
    int64_t max_cost_us;
    int64_t last_chunk_us;  /* 上一片音频的时间戳，用于判断采集会话边界 */
    uint32_t seg_start_fed; /* 本段采集开始时的 fed，用于统计本段帧数 */
} wake_ctx_t;

static wake_ctx_t s_w;

/*
 * AFE worker：从帧队列取整帧，feed 后立刻 fetch。
 *
 * `fetch()` 可能阻塞数秒，但它阻塞的是这个任务，不是采集任务 ——
 * 这正是分层的目的。
 */
static void afe_worker(void *arg)
{
    const size_t frame_bytes = (size_t)s_w.feed_samples * sizeof(int16_t);

    while (true) {
        size_t got = 0;
        void *item = xRingbufferReceive(s_w.frame_rb, &got, portMAX_DELAY);
        if (item == NULL) {
            continue;
        }
        if (got != frame_bytes) {
            vRingbufferReturnItem(s_w.frame_rb, item);
            continue;
        }

        s_w.afe->feed(s_w.data, (const int16_t *)item);
        afe_fetch_result_t *res = s_w.afe->fetch(s_w.data);

        vRingbufferReturnItem(s_w.frame_rb, item);
        s_w.fed++;

        if (res == NULL) {
            continue;
        }
        if (res->ret_value == ESP_FAIL) {
            /* 前几次失败打出来，便于确认是否真的没产出 */
            if (s_w.fed <= 5) {
                ESP_LOGW(TAG, "AFE fetch 失败: ret=%d size=%d", res->ret_value,
                         res->data_size);
            }
            continue;
        }

        /* 进度日志的间隔。
         *
         * 麦克风常开后 AFE 一直在跑，每 200 帧（约 2 秒）打一条会刷屏，
         * 把真正有用的信息冲掉。改成每 2000 帧（约 20 秒）一条，
         * 既能看出"检测器还活着"，又不淹没日志。 */
        if (s_w.fed % 2000 == 1) {
            ESP_LOGI(TAG, "唤醒检测运行中: feed=%u 丢=%u size=%d wakeup_state=%d vad=%d",
                     (unsigned)s_w.fed, (unsigned)s_w.dropped, res->data_size,
                     (int)res->wakeup_state, (int)res->vad_state);
        }

        if (res->wakeup_state == WAKENET_DETECTED) {
            /*
             * 冷却期：命中后一段时间内忽略后续命中。
             *
             * 为什么必须要有：唤醒词在**采集期间**也一直在检测（麦克风常开）。
             * 用户接着说话时，内容里若出现与唤醒词相近的音（实测"嗨小新"
             * 就会触发），AFE 会再报一次命中。于是 PC 端收到第二个 wake
             * 事件，在上一轮还没播报完时又开一次采集，两个采集会话互相
             * 打断 —— 表现为"只能对话一次"、连接断开、播报失败。
             *
             * 这也是回声抑制的常规做法：自己刚"说话"或正在采集时，
             * 不应把残留音频当成新的唤醒。
             */
            int64_t now_us = esp_timer_get_time();
            if (s_w.last_hit_us != 0 &&
                now_us - s_w.last_hit_us < (int64_t)WAKE_COOLDOWN_MS * 1000) {
                s_w.hits_suppressed++;
                if (s_w.hits_suppressed == 1 || s_w.hits_suppressed % 20 == 0) {
                    ESP_LOGD(TAG, "冷却期内忽略命中（累计抑制 %u 次，距上次 %d ms）",
                             (unsigned)s_w.hits_suppressed,
                             (int)((now_us - s_w.last_hit_us) / 1000));
                }
                continue;
            }
            s_w.last_hit_us = now_us;

            s_w.hits++;
            ESP_LOGI(TAG, "唤醒词命中！（第 %u 次，模型序号=%d，唤醒词长度=%d 采样）",
                     (unsigned)s_w.hits, res->wakenet_model_index, res->wake_word_length);
            if (s_w.cb != NULL) {
                s_w.cb();
            }
        }
    }
}

/*
 * 采集旁路：把采集任务的 PCM 攒够一帧后投进队列。
 *
 * 这个函数跑在**采集任务**里，必须极快、绝不阻塞 ——
 * 队列满就直接丢帧。
 *
 * 输入格式是 "M"（单麦），所以直接把单声道采样填进去即可，
 * 不需要像多通道那样交错。
 */
static void on_audio_monitor(const uint8_t *pcm, size_t len)
{
    if (!s_w.ready || pcm == NULL || len < 2 || s_w.frame_rb == NULL) {
        return;
    }

    /*
     * 麦克风现在是**常开**的，所以不再有"采集开始/结束"的概念 ——
     * 唤醒检测一直在跑。这里只按数据流的间歇刷一条活跃提示，
     * 让串口能看出"确实在听"，而不是死寂一片。
     */
    int64_t now = esp_timer_get_time();
    if (now - s_w.last_chunk_us > 10000000) { /* 距上次提示超过 10 秒 */
        ESP_LOGI(TAG, "唤醒检测运行中（已 feed %u 帧，命中 %u）—— 可随时喊「Hi 小星」",
                 (unsigned)s_w.fed, (unsigned)s_w.hits);
    }
    s_w.last_chunk_us = now;

    const int16_t *mono = (const int16_t *)pcm;
    size_t count = len / 2;
    const size_t frame_samples = (size_t)s_w.feed_samples;

    for (size_t i = 0; i < count; i++) {
        s_w.frame[s_w.pending++] = mono[i];

        if ((size_t)s_w.pending >= frame_samples) {
            if (xRingbufferSend(s_w.frame_rb, s_w.frame, frame_samples * sizeof(int16_t),
                                0) != pdTRUE) {
                s_w.dropped++;
            }
            s_w.pending = 0;
        }
    }
}

esp_err_t bot_wakeword_init(void)
{
    /*
     * 先保住已经注册的回调，再清零结构体。
     *
     * 这里踩过一个很隐蔽的坑：调用方通常是
     *     bot_wakeword_set_cb(on_wake_word);   // 先把回调存进 s_w.cb
     *     bot_wakeword_init();                 // 这里 memset 又把它清成 NULL
     * 于是**回调被自己的初始化抹掉了**。
     * 表现极具迷惑性：固件能打印"唤醒词命中"（说明检测完全正常），
     * 但 `if (s_w.cb != NULL)` 永远为假，事件一个都发不出去 ——
     * 从 PC 侧看就是"喊醒了但没反应"。
     *
     * 所以这里先把回调存到栈上，memset 之后再恢复。
     */
    bot_wakeword_cb_t saved_cb = s_w.cb;
    memset(&s_w, 0, sizeof(s_w));
    s_w.cb = saved_cb;

#if !CONFIG_SPARKBOT_WAKE_WORD_ENABLE
    ESP_LOGI(TAG, "本地唤醒词未启用（menuconfig）");
    return ESP_OK;
#endif

    if (!bot_audio_ready()) {
        ESP_LOGW(TAG, "音频未就绪，唤醒词不可用");
        return ESP_OK;
    }

    /*
     * 第一步：从 `model` 分区读出模型列表，查出唤醒词模型的**真实名字**。
     *
     * 模型名不在代码里、也不在预编译库里 —— 它是打包进 srmodels.bin 的
     * 分区内容（每个模型带一个 `_MODEL_INFO_` 文件），只能运行时从分区查。
     * 好处是换唤醒词只需改 menuconfig，不用动代码。
     */
    s_w.models = esp_srmodel_init(MODEL_PARTITION_LABEL);
    if (s_w.models == NULL || s_w.models->num <= 0) {
        ESP_LOGE(TAG, "未能从 `%s` 分区读到模型 —— 检查分区表与 srmodels.bin 烧写",
                 MODEL_PARTITION_LABEL);
        return ESP_FAIL;
    }

    ESP_LOGI(TAG, "分区里共有 %d 个模型:", s_w.models->num);
    for (int i = 0; i < s_w.models->num; i++) {
        ESP_LOGI(TAG, "  [%d] %s", i, s_w.models->model_name[i]);
    }

    char *wn_name = esp_srmodel_filter(s_w.models, ESP_WN_PREFIX, NULL);
    if (wn_name == NULL) {
        ESP_LOGE(TAG, "分区里没有唤醒词模型 —— 检查 menuconfig 的 "
                      "\"ESP Speech Recognition -> Select wake words\"");
        return ESP_FAIL;
    }
    ESP_LOGI(TAG, "使用唤醒词模型: %s", wn_name);

    char *phrases = esp_srmodel_get_wake_words(s_w.models, wn_name);
    if (phrases != NULL) {
        ESP_LOGI(TAG, "该模型的唤醒词: %s", phrases);
        free(phrases);
    }

    /*
     * 第二步：用 afe_config_init() 生成配置。
     *
     * **这是 2.x 与 1.9.x 最大的区别**，也是之前所有手工配置都失败的原因：
     * 由 esp-sr 自己按 `input_format` 推导整套内部参数（各帧长、ringbuf、
     * 任务栈等）。手工填 AFE_CONFIG_DEFAULT() 会漏掉这些推导。
     *
     * input_format = "M"：只有一个麦克风通道。
     *   （有多通道时按 "MR" 这样写：M=麦克风，R=回采参考）
     *
     * AFE_TYPE_VC + AFE_MODE_HIGH_PERF：与已验证可用的
     * xiaozhi-esp32 工程一致。
     */
    afe_config_t *cfg = afe_config_init("M", s_w.models, AFE_TYPE_VC, AFE_MODE_HIGH_PERF);
    if (cfg == NULL) {
        ESP_LOGE(TAG, "afe_config_init 失败");
        return ESP_FAIL;
    }

    /* 本板只有一个麦克风、没有回采通道 → 不做 AEC、不做参考通道 */
    cfg->aec_init = false;
    cfg->pcm_config.total_ch_num = AFE_MIC_NUM;
    cfg->pcm_config.mic_num = AFE_MIC_NUM;
    cfg->pcm_config.ref_num = 0;
    cfg->pcm_config.sample_rate = 16000;

    /* 唤醒词是核心 */
    cfg->wakenet_init = true;
    cfg->wakenet_model_name = wn_name;
    cfg->wakenet_model_name_2 = NULL;

    /*
     * VAD：打开，但**不指定 vad_model_name** ——
     * 传 NULL 时 esp-sr 用内置的 WebRTC VAD（不需要额外的模型文件）。
     * 之所以要开：fetch 的结果里带 vad_state，后续做"说话结束检测"
     * 用得上；而且这也与已验证可用的工程一致。
     */
    cfg->vad_init = true;
    cfg->vad_mode = VAD_MODE_0;
    cfg->vad_min_noise_ms = 100;

    cfg->ns_init = false;
    cfg->agc_init = false;
    cfg->memory_alloc_mode = AFE_MEMORY_ALLOC_MORE_PSRAM;

    ESP_LOGI(TAG, "创建 AFE 前: 内部RAM=%u (最大块 %u), PSRAM=%u",
             (unsigned)heap_caps_get_free_size(MALLOC_CAP_INTERNAL),
             (unsigned)heap_caps_get_largest_free_block(MALLOC_CAP_INTERNAL | MALLOC_CAP_8BIT),
             (unsigned)heap_caps_get_free_size(MALLOC_CAP_SPIRAM));

    /* 用配置选出正确的 AFE 实现（S3 上可能是 1mic/2mic 变体） */
    s_w.afe = esp_afe_handle_from_config(cfg);
    if (s_w.afe == NULL) {
        ESP_LOGE(TAG, "esp_afe_handle_from_config 返回 NULL");
        afe_config_free(cfg);
        return ESP_FAIL;
    }

    s_w.data = s_w.afe->create_from_config(cfg);
    afe_config_free(cfg); /* AFE 内部已拷贝所需字段 */
    if (s_w.data == NULL) {
        ESP_LOGE(TAG, "AFE 创建失败");
        return ESP_FAIL;
    }

    ESP_LOGI(TAG, "创建 AFE 后: 内部RAM=%u (最大块 %u), PSRAM=%u",
             (unsigned)heap_caps_get_free_size(MALLOC_CAP_INTERNAL),
             (unsigned)heap_caps_get_largest_free_block(MALLOC_CAP_INTERNAL | MALLOC_CAP_8BIT),
             (unsigned)heap_caps_get_free_size(MALLOC_CAP_SPIRAM));

    /* 打印内部流水线，排查时极有用 */
    if (s_w.afe->print_pipeline != NULL) {
        s_w.afe->print_pipeline(s_w.data);
    }

    /*
     * 每次 feed 的采样数 = get_feed_chunksize() × 输入通道数。
     *
     * 注意要乘通道数：该函数返回的是"每通道采样数"，
     * 与已验证工程里的写法一致（它也乘了 codec_->input_channels()）。
     */
    int per_channel = s_w.afe->get_feed_chunksize(s_w.data);
    if (per_channel <= 0) {
        ESP_LOGE(TAG, "AFE feed 帧长异常: %d", per_channel);
        return ESP_FAIL;
    }
    s_w.feed_samples = per_channel * AFE_MIC_NUM;

    s_w.frame = heap_caps_malloc((size_t)s_w.feed_samples * sizeof(int16_t), MALLOC_CAP_SPIRAM);
    if (s_w.frame == NULL) {
        s_w.frame = malloc((size_t)s_w.feed_samples * sizeof(int16_t));
    }
    if (s_w.frame == NULL) {
        ESP_LOGE(TAG, "帧缓冲分配失败");
        return ESP_ERR_NO_MEM;
    }
    s_w.pending = 0;

    s_w.frame_rb = xRingbufferCreate((size_t)s_w.feed_samples * sizeof(int16_t) * FRAME_QUEUE_DEPTH,
                                     RINGBUF_TYPE_NOSPLIT);
    if (s_w.frame_rb == NULL) {
        ESP_LOGE(TAG, "帧队列创建失败");
        return ESP_ERR_NO_MEM;
    }

    /* worker 栈放 PSRAM：内部 RAM 留给 AFE */
    BaseType_t created = xTaskCreateWithCaps(afe_worker, "bot_afe", AFE_WORKER_STACK,
                                            NULL, AFE_WORKER_PRIO, &s_w.worker,
                                            MALLOC_CAP_SPIRAM);
    if (created != pdPASS) {
        ESP_LOGW(TAG, "PSRAM 栈创建 worker 失败，回退内部 RAM");
        created = xTaskCreate(afe_worker, "bot_afe", AFE_WORKER_STACK, NULL,
                              AFE_WORKER_PRIO, &s_w.worker);
    }
    if (created != pdPASS) {
        ESP_LOGE(TAG, "AFE worker 创建失败");
        return ESP_FAIL;
    }

    bot_audio_set_monitor_cb(on_audio_monitor);
    s_w.ready = true;

    ESP_LOGI(TAG, "唤醒词就绪: 模型=%s 采样率=%d feed帧长=%d 采样(%d通道) fetch帧长=%d",
             wn_name, s_w.afe->get_samp_rate(s_w.data), s_w.feed_samples,
             AFE_MIC_NUM, s_w.afe->get_fetch_chunksize(s_w.data));
    ESP_LOGI(TAG, "唤醒检测**持续运行**（麦克风常开）：随时可喊「Hi 小星」");
    return ESP_OK;
}

bool bot_wakeword_ready(void)
{
    return s_w.ready;
}

void bot_wakeword_set_cb(bot_wakeword_cb_t cb)
{
    s_w.cb = cb;
}

void bot_wakeword_stats(uint32_t *fed, uint32_t *hits, uint32_t *dropped, int64_t *max_cost_us)
{
    if (fed != NULL) {
        *fed = s_w.fed;
    }
    if (hits != NULL) {
        *hits = s_w.hits;
    }
    if (dropped != NULL) {
        *dropped = s_w.dropped;
    }
    if (max_cost_us != NULL) {
        *max_cost_us = s_w.max_cost_us;
    }
}

/*
 * Dump 唤醒词/AFE 状态与系统任务表，供诊断。
 * 用法：向设备发动作 `wakeword_dump`。
 */
void bot_wakeword_dump(void)
{
    ESP_LOGI(TAG, "===== 唤醒词状态 =====");
    ESP_LOGI(TAG, "ready=%d feed_samples=%d worker=%p afe=%p",
             (int)s_w.ready, s_w.feed_samples, (void *)s_w.worker, (void *)s_w.data);
    ESP_LOGI(TAG, "feed=%u 命中=%u 丢弃=%u 最坏耗时=%lld us",
             (unsigned)s_w.fed, (unsigned)s_w.hits, (unsigned)s_w.dropped,
             (long long)s_w.max_cost_us);
    if (s_w.frame_rb != NULL) {
        ESP_LOGI(TAG, "帧队列剩余=%u 字节",
                 (unsigned)xRingbufferGetCurFreeSize(s_w.frame_rb));
    }

    ESP_LOGI(TAG, "===== 任务表 =====");
    static TaskStatus_t tasks[28];
    UBaseType_t n = uxTaskGetSystemState(tasks, sizeof(tasks) / sizeof(tasks[0]), NULL);
    for (UBaseType_t i = 0; i < n; i++) {
        ESP_LOGI(TAG, "  %-14s prio=%-3u state=%-2u 栈余=%u",
                 tasks[i].pcTaskName, (unsigned)tasks[i].uxCurrentPriority,
                 (unsigned)tasks[i].eCurrentState,
                 (unsigned)tasks[i].usStackHighWaterMark);
    }
    ESP_LOGI(TAG, "===== 共 %u 个任务 =====", (unsigned)n);
}
