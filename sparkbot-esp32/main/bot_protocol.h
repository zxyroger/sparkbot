/*
 * SparkBot 设备协议常量 —— 与 PC 端 sparkbot/device/protocol.py 一一对应。
 *
 * 本文件是固件侧的协议事实来源。任何字段名 / 动作名 / 错误码的改动
 * 都必须同步改 PC 端的 protocol.py 与 docs/protocol.md，
 * 三者保持一致才能互通。
 *
 * 信封格式（UTF-8 JSON 文本帧，WebSocket）：
 *
 *   PC → 设备（期望回复）
 *     {"v":1,"type":"command","id":"<uuid>","ts":<ms>,
 *      "action":"drive","params":{"linear":0.35,...}}
 *
 *   PC → 设备（不期望回复，即发即忘）
 *     {"v":1,"type":"intent","id":"...","ts":...,"action":"stop","params":{}}
 *
 *   设备 → PC（对某条 command 的回复）
 *     {"v":1,"type":"result","id":"<同一 id>","ok":true,"data":{...},"error":null}
 *
 *   设备 → PC（周期遥测）
 *     {"v":1,"type":"telemetry","ts":...,"battery":{...},"motion":{...},...}
 *
 *   设备 → PC（异步事件）
 *     {"v":1,"type":"event","ts":...,"event":"wake_word","data":{...}}
 *
 *   设备 → PC（图像）
 *     {"v":1,"type":"frame","id":null,"ts":...,"format":"jpeg",
 *      "width":640,"height":480,"seq":42,"data_b64":"..."}
 *
 *   设备 → PC（音频分片）
 *     {"v":1,"type":"audio","id":null,"ts":...,"phase":"start|chunk|end",
 *      "format":"pcm_s16le","sample_rate":16000,"channels":1,
 *      "seq":3,"data_b64":"..."}
 */

#pragma once

#include <stdbool.h>
#include <stdint.h>
#include <stddef.h>

#ifdef __cplusplus
extern "C" {
#endif

/* ------------------------------------------------------------------ */
/* 版本                                                               */
/* ------------------------------------------------------------------ */
#define BOT_PROTOCOL_VERSION 1

/* 固件版本，随 hello 上报，方便 PC 端日志区分板子上跑的是哪一版 */
#define BOT_FW_VERSION "1.0.0"

/* 单条 WebSocket 文本帧的软上限：超过就分片发送 */
#define BOT_MAX_TEXT_FRAME (256 * 1024)

/* ------------------------------------------------------------------ */
/* 消息类型 (type)                                                    */
/* ------------------------------------------------------------------ */
/* 设备 → PC */
#define BOT_MSG_HELLO       "hello"
#define BOT_MSG_RESULT      "result"
#define BOT_MSG_TELEMETRY   "telemetry"
#define BOT_MSG_EVENT       "event"
#define BOT_MSG_FRAME       "frame"
#define BOT_MSG_AUDIO       "audio"
#define BOT_MSG_PONG        "pong"
/* PC → 设备 */
#define BOT_MSG_HELLO_ACK   "hello_ack"
#define BOT_MSG_COMMAND     "command"
#define BOT_MSG_INTENT      "intent"
#define BOT_MSG_PING        "ping"

/* ------------------------------------------------------------------ */
/* 动作 (action)                                                      */
/* ------------------------------------------------------------------ */
/* 底盘 */
#define BOT_ACT_DRIVE               "drive"
#define BOT_ACT_STOP                "stop"
#define BOT_ACT_SET_MOTION_LIMITS   "set_motion_limits"
/* 显示 */
#define BOT_ACT_SET_FACE            "set_face"
#define BOT_ACT_SET_TEXT            "set_text"
#define BOT_ACT_DISPLAY_FRAME       "display_frame"
#define BOT_ACT_SET_BACKLIGHT       "set_backlight"
#define BOT_ACT_CLEAR_DISPLAY       "clear_display"
/* 视觉 */
#define BOT_ACT_SNAPSHOT            "snapshot"
#define BOT_ACT_SET_STREAM          "set_stream"
#define BOT_ACT_SET_CAMERA_PARAMS   "set_camera_params"
/* 音频 */
#define BOT_ACT_PLAY_AUDIO          "play_audio"
/* 流式播放三件套：begin → write × N → end。
 * 与 play_audio 的区别是段落之间**不断流**（边收边播），
 * 适合长文本按句合成后连续推送。write 的 data_b64 是板子采样率的
 * 单声道 16bit 裸 PCM。 */
#define BOT_ACT_AUDIO_STREAM_BEGIN  "audio_stream_begin"
#define BOT_ACT_AUDIO_STREAM_WRITE  "audio_stream_write"
#define BOT_ACT_AUDIO_STREAM_END    "audio_stream_end"
#define BOT_ACT_TTS_SPEAK           "tts_speak"
#define BOT_ACT_PLAY_TONE           "play_tone"
#define BOT_ACT_SET_VOLUME          "set_volume"
#define BOT_ACT_START_LISTEN        "start_listen"
#define BOT_ACT_STOP_LISTEN         "stop_listen"
/* 系统 */
#define BOT_ACT_SET_LED             "set_led"
#define BOT_ACT_REBOOT              "reboot"
#define BOT_ACT_CONFIG              "config"

/* ------------------------------------------------------------------ */
/* 能力 (capabilities) —— 决定 PC 端向模型暴露哪些工具                */
/* ------------------------------------------------------------------ */
#define BOT_CAP_CAMERA      "camera"
#define BOT_CAP_MICROPHONE  "microphone"
#define BOT_CAP_SPEAKER     "speaker"
#define BOT_CAP_DISPLAY     "display"
#define BOT_CAP_MOTOR       "motor"
#define BOT_CAP_BATTERY     "battery"

/* ------------------------------------------------------------------ */
/* 错误码 (result.error.code)                                         */
/* ------------------------------------------------------------------ */
#define BOT_ERR_UNSUPPORTED_ACTION  "unsupported_action"
#define BOT_ERR_UNSUPPORTED_PARAM   "unsupported_param"
#define BOT_ERR_BAD_PARAMS          "bad_params"
#define BOT_ERR_BUSY                "busy"
#define BOT_ERR_HARDWARE_FAULT      "hardware_fault"
#define BOT_ERR_TIMEOUT             "timeout"
#define BOT_ERR_LOW_BATTERY         "low_battery"
#define BOT_ERR_INTERNAL            "internal"

/* ------------------------------------------------------------------ */
/* 事件名 (event)                                                     */
/* ------------------------------------------------------------------ */
#define BOT_EVT_WAKE_WORD       "wake_word"
#define BOT_EVT_BUTTON          "button"
#define BOT_EVT_TOUCH           "touch"
#define BOT_EVT_OBSTACLE        "obstacle"
#define BOT_EVT_CLIFF           "cliff"
#define BOT_EVT_BUMP            "bump"
#define BOT_EVT_LOW_BATTERY     "low_battery"
#define BOT_EVT_OVERHEAT        "overheat"
#define BOT_EVT_ERROR           "error"
#define BOT_EVT_MOTION_DONE     "motion_done"
#define BOT_EVT_AUDIO_DONE      "audio_done"
#define BOT_EVT_LISTEN_TIMEOUT  "listen_timeout"

/* ------------------------------------------------------------------ */
/* 表情名 (set_face.emotion)                                          */
/* ------------------------------------------------------------------ */
#define BOT_FACE_NEUTRAL    "neutral"
#define BOT_FACE_HAPPY      "happy"
#define BOT_FACE_SAD        "sad"
#define BOT_FACE_ANGRY      "angry"
#define BOT_FACE_SURPRISED  "surprised"
#define BOT_FACE_SLEEPY     "sleepy"
#define BOT_FACE_CONFUSED   "confused"
#define BOT_FACE_THINKING   "thinking"
#define BOT_FACE_LOVE       "love"
#define BOT_FACE_EXCITED    "excited"
#define BOT_FACE_SCARED     "scared"
#define BOT_FACE_BORED      "bored"

/* ------------------------------------------------------------------ */
/* 音频格式 (play_audio.format / audio.format)                        */
/* ------------------------------------------------------------------ */
#define BOT_FMT_PCM_S16LE   "pcm_s16le"
#define BOT_FMT_WAV         "wav"
#define BOT_FMT_MP3         "mp3"   /* 固件不支持，收到回 unsupported_param */
#define BOT_FMT_JPEG        "jpeg"

#ifdef __cplusplus
}
#endif
