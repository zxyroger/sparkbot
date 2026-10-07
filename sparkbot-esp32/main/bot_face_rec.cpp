/*
 * 人脸检测 + 特征提取的实现（esp-dl）。接口见 bot_face_rec.h。
 *
 * 参考 esp-who / esp-dl 官方示例的用法：
 *   1) 检测：HumanFaceDetect(MSRMNP_S8_V1) —— MSR 出候选框，MNP 关键点精修；
 *   2) 特征：HumanFaceFeat(MFN_S8_V1)     —— 用关键点做对齐，输出 512 维。
 *
 * 两个模型都是 int8 量化版：S3 上 MSR+MNP 约 0.15s、MFN 约 0.25s，
 * 一张脸合计 ~0.4s。所以只在"需要知道是谁"的时候跑一次，不做逐帧识别。
 */

#include "bot_face_rec.h"

#include "dl_image_jpeg.hpp"
#include "esp_heap_caps.h"
#include "esp_log.h"
#include "esp_timer.h"
#include "human_face_detect.hpp"
#include "human_face_recognition.hpp"
#include "jpeg_decoder.h"

static const char *TAG = "bot_face";

static HumanFaceDetect *s_det = nullptr;
static HumanFaceFeat *s_feat = nullptr;

extern "C" int bot_face_rec_init(void)
{
    if (s_det != nullptr && s_feat != nullptr) {
        return 0;
    }

    int64_t t0 = esp_timer_get_time();
    /*
     * 构造参数是 (sdcard_model_dir, model_type)：模型也可以从 SD 卡加载，
     * 本板没有 SD 卡，第一个参数传 nullptr 表示用烧进固件的模型。
     *
     * 模型对象在构造时就加载权重，所以这里一次性建好常驻。
     * 用 MFN（1.2M 参数）而不是 MBF：精度差一点（TAR 90.0% vs 93.9%），
     * 但在 S3 上快 4 倍多（245ms vs 1117ms）—— 对话场景里响应速度更重要。
     */
    s_det = new HumanFaceDetect(nullptr, HumanFaceDetect::MSRMNP_S8_V1);
    s_feat = new HumanFaceFeat(nullptr, HumanFaceFeat::MFN_S8_V1);
    if (s_det == nullptr || s_feat == nullptr) {
        ESP_LOGE(TAG, "人脸模型分配失败");
        return -1;
    }

    ESP_LOGI(TAG, "人脸模型就绪: 检测 MSRMNP_S8_V1 + 特征 MFN_S8_V1，耗时 %lld ms",
             (long long)((esp_timer_get_time() - t0) / 1000));
    return 0;
}

extern "C" bool bot_face_rec_ready(void)
{
    return s_det != nullptr && s_feat != nullptr;
}

extern "C" int bot_face_rec_run(const uint8_t *jpeg, size_t len, bot_face_t *out, int max_faces)
{
    if (!bot_face_rec_ready() || jpeg == nullptr || len == 0 || out == nullptr || max_faces <= 0) {
        return -1;
    }
    if (max_faces > BOT_FACE_MAX) {
        max_faces = BOT_FACE_MAX;
    }

    int64_t t0 = esp_timer_get_time();

    /* ---- 1. JPEG 解码 ---- *
     * S3 没有硬件 JPEG 解码器，走 esp_jpeg 的软件解码。
     * 解码缓冲由 esp-dl 内部申请（PSRAM），用完必须还。
     *
     * ⚠️ 这里必须先填 jimg.width/height：
     * esp-dl 的 sw_decode_jpeg 是拿 **jpeg_img 自带的 height*width*3**
     * 去决定输出缓冲大小的（见 dl_image_jpeg.cpp），它自己并不解析 JPEG 头。
     * 两个字段留 0 → 输出缓冲 0 字节 → 解码器报
     * "Not enough size in output buffer!" 直接失败（踩过）。
     * 与其让调用方每处都记得传，不如在这里从 JPEG 头里读出真实尺寸，
     * 顺便也能挡掉"传进来的尺寸和实际 JPEG 不符"这类错误。 */
    esp_jpeg_image_cfg_t info_cfg = {};
    info_cfg.indata = (uint8_t *)jpeg;
    info_cfg.indata_size = (uint32_t)len;
    esp_jpeg_image_output_t info = {};
    if (esp_jpeg_get_image_info(&info_cfg, &info) != ESP_OK) {
        ESP_LOGW(TAG, "读不到 JPEG 头信息（%u 字节），放弃推理", (unsigned)len);
        return -1;
    }

    dl::image::jpeg_img_t jimg = {};
    jimg.data = (uint8_t *)jpeg;
    jimg.data_size = (uint32_t)len;
    jimg.width = info.width;
    jimg.height = info.height;

    dl::image::img_t img = {};
    img.pix_type = dl::image::DL_IMAGE_PIX_TYPE_RGB888;
    /* 第三个参数 swap_color_bytes=true：RGB888 下等价于"不交换字节"，
     * 与官方 human_face_recognition 示例一致（该函数对 RGB888 会取反）。 */
    esp_err_t derr = dl::image::sw_decode_jpeg(jimg, img, true);
    if (derr != ESP_OK || img.data == nullptr) {
        ESP_LOGW(TAG, "JPEG 解码失败（%u 字节，%dx%d）", (unsigned)len, info.width, info.height);
        return -1;
    }

    /* ---- 2. 人脸检测 ---- */
    int64_t t_det = esp_timer_get_time();
    auto &det_res = s_det->run(img);
    int64_t det_ms = (esp_timer_get_time() - t_det) / 1000;

    /* ---- 3. 逐脸提特征 ---- */
    int n = 0;
    int64_t t_feat = esp_timer_get_time();
    for (auto &d : det_res) {
        if (n >= max_faces) {
            break;
        }
        if (d.box.size() < 4 || d.keypoint.empty()) {
            continue;
        }

        dl::TensorBase *feat = s_feat->run(img, d.keypoint);
        if (feat == nullptr) {
            ESP_LOGW(TAG, "特征提取失败（跳过这张脸）");
            continue;
        }

        bot_face_t &f = out[n];
        f.x1 = d.box[0];
        f.y1 = d.box[1];
        f.x2 = d.box[2];
        f.y2 = d.box[3];
        f.score = d.score;

        /* 特征维度从张量形状拿（这一版 esp-dl 的 Feat 没有 get_feat_len）；
         * MFN/MBF 都是 512 维，取最后一维最稳。 */
        std::vector<int> shape = feat->get_shape();
        int L = shape.empty() ? BOT_FACE_FEAT_LEN : shape[shape.size() - 1];
        if (L > BOT_FACE_FEAT_LEN) {
            L = BOT_FACE_FEAT_LEN;
        }
        f.feat_len = L;

        /* 特征是 **float32 且已 L2 归一化**（FeatPostprocessor 干的），
         * 不是 int8 量化值 —— 必须按 float 读，否则拿到的是 float 数据的
         * 前若干个字节重解释出来的垃圾。PC 侧直接点积即为余弦相似度。 */
        const float *src = (const float *)feat->data;
        for (int i = 0; i < L; i++) {
            f.feat[i] = src[i];
        }
        n++;
    }
    int64_t feat_ms = (esp_timer_get_time() - t_feat) / 1000;

    heap_caps_free(img.data);

    ESP_LOGI(TAG, "人脸识别: 检测到 %d 张（%dx%d 解码+检测 %lld ms，特征 %lld ms，总 %lld ms）",
             n, img.width, img.height, (long long)det_ms, (long long)feat_ms,
             (long long)((esp_timer_get_time() - t0) / 1000));
    return n;
}
