/*
 * 摄像头：OV2640 DVP，抓帧与连续推流
 *
 * 帧缓冲放在 PSRAM（VGA JPEG 一帧可达 100KB+，内部 RAM 放不下）。
 *
 * 推流与抓帧的关系：
 *   - snapshot 动作：抓一帧，通过 frame 消息回给 PC（一次性）
 *   - set_stream 动作：按 fps 持续抓帧并推送（供 PC 端做持续视觉监控）
 *   两者共用同一个 esp_camera 实例，因此必须互斥：
 *   已经开始推流时再抓单帧会拿到推流中的帧（这没问题），
 *   但不能同时有两个任务去调 esp_camera_fb_get()（会阻塞）。
 *   所以抓帧与推流都在**主循环**里串行执行，见 bot_camera_poll()。
 */

#pragma once

#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>

#include "esp_err.h"

#ifdef __cplusplus
extern "C" {
#endif

/* 帧回调：每拿到一帧就调一次（用于发 frame 消息）。
 * jpeg 指针仅回调内有效。 */
typedef void (*bot_camera_frame_cb_t)(const uint8_t *jpeg, size_t len,
                                      uint16_t width, uint16_t height);

esp_err_t bot_camera_init(void);

bool bot_camera_ready(void);

void bot_camera_set_frame_cb(bot_camera_frame_cb_t cb);

/*
 * 抓一帧并通过回调送出。
 *
 * @param width/height 期望分辨率，0 表示用当前设置
 * @param quality      JPEG 质量 0~63（越小越清晰），<0 表示不改
 * @return ESP_OK 已抓取并送出
 *
 * 本函数会阻塞直到拿到一帧（VGA 通常几十毫秒）。
 */
esp_err_t bot_camera_snapshot(int width, int height, int quality);

/*
 * 启停连续推流。
 * @param fps 每秒帧数；<=0 或 enabled=false 表示停止
 */
esp_err_t bot_camera_set_stream(bool enabled, float fps, int width, int height);

bool bot_camera_streaming(void);

/* 运行期改分辨率/质量。返回实际生效的分辨率索引。 */
esp_err_t bot_camera_set_params(int width, int height, int quality);

/* 当前分辨率与质量（写进 hello 的 camera 字段） */
void bot_camera_get_info(uint16_t *width, uint16_t *height, int *quality);

/* 主循环定期调用：推流时按 fps 抓帧 */
void bot_camera_poll(void);

/*
 * 借一帧原始 JPEG —— 给**本地人脸推理**用（见 bot_face_rec.h）。
 *
 * 与 snapshot 的区别：snapshot 是"抓完就通过回调发出去"，这里只是把
 * 帧借给调用方用一下（推理完就还），不发网络。
 *
 * 用完**必须**调 bot_camera_frame_release()，否则帧缓冲会被占满，
 * 之后抓帧会一直失败。
 */
esp_err_t bot_camera_frame_borrow(const uint8_t **jpeg, size_t *len,
                                  uint16_t *width, uint16_t *height);
void bot_camera_frame_release(void);

#ifdef __cplusplus
}
#endif
