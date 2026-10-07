/*
 * 人脸检测 + 人脸特征（**在芯片上本地推理**，用 esp-dl，参考 esp-who 的做法）
 *
 * 为什么不把图传回 PC 去认：
 *   * VGA JPEG 一帧 30~100KB，来回传一次就吃掉好几帧视频的带宽；
 *   * PC 侧要额外装视觉模型、解码库，链路长、依赖重；
 *   * esp-dl 的 MFN 人脸模型只有 1.2M 参数，ESP32-S3 上单张脸约 0.25 秒，
 *     算力和内存都扛得住。
 *
 * 分工：**设备侧出"特征向量"，PC 侧负责"名字"**。
 *   设备：JPEG → 解码 RGB888 → 人脸检测 → 对齐裁剪 → 512 维特征（float32）
 *   PC  ：拿特征和已登记的人做余弦相似度 → 得到姓名 → 写进对话上下文/长期记忆
 * 这样名字的归属完全在 Agent 这边，设备不需要存任何隐私数据。
 */

#pragma once

#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>

#ifdef __cplusplus
extern "C" {
#endif

/*: 人脸特征维度（MFN_S8_V1 / MBF_S8_V1 都是 512）。 */
#define BOT_FACE_FEAT_LEN 512
/*: 单帧最多处理几张脸（多了既慢又没用）。 */
#define BOT_FACE_MAX 4

/*
 * 一张脸的结果。
 *
 * 特征不是量化值，而是 **float32 且已做 L2 归一化**（见 esp-dl 的
 * FeatPostprocessor::l2_norm）。所以 PC 侧只要两个向量做点积就是
 * 余弦相似度 —— 和设备端 HumanFaceRecognizer 用同一个判据，阈值也能直接
 * 沿用它的默认值 0.5。
 *
 * 曾误以为特征跟模型一样是 int8 量化值，按 `int8_t` 去读张量，读到的
 * 其实是 float 数据的前 512 个字节 —— 全是垃圾，相似度自然没有意义。
 * 张量的 dtype 就是 DATA_TYPE_FLOAT，必须按 float 读。
 */
typedef struct {
    int x1, y1, x2, y2;   /* 人脸框（原图坐标） */
    float score;          /* 检测置信度 */
    int feat_len;         /* 实际特征长度，正常 = BOT_FACE_FEAT_LEN */
    float feat[BOT_FACE_FEAT_LEN];
} bot_face_t;

/*: 加载检测 + 特征模型（首次调用较慢，约 1 秒）。返回 0 表示成功。 */
int bot_face_rec_init(void);

/*: 模型是否就绪。 */
bool bot_face_rec_ready(void);

/*
 * 对一张 JPEG 跑"检测 + 特征提取"。
 *
 * Args:
 *   jpeg/len: JPEG 字节（就是摄像头抓的那一帧，不用转格式）。
 *   out:      输出数组，至少能放 max_faces 个 bot_face_t。
 *   max_faces: 最多返回几张脸（<= BOT_FACE_MAX）。
 *
 * Returns:
 *   识别到的人脸数（>=0）；<0 表示失败（解码失败/模型未就绪）。
 */
int bot_face_rec_run(const uint8_t *jpeg, size_t len, bot_face_t *out, int max_faces);

#ifdef __cplusplus
}
#endif
