/*
 * 底盘电机：通用双路 H 桥（TB6612 / DRV8833 / L298N 之类）
 *
 * 运动学约定（与 SparkBot 协议一致）：
 *   linear  : 前进线速度 m/s，负数后退
 *   angular : 转向角速度 rad/s，**正数左转**（逆时针）
 *
 * 差速换算：
 *   v_left  = linear - angular * wheel_base / 2
 *   v_right = linear + angular * wheel_base / 2
 * 这是标准差速模型。正 angular（左转）意味着右轮更快，两者一致。
 *
 * 开环定时：drive 带 duration_ms，到点固件**自己**停下并发 motion_done 事件。
 * 不能等 PC 再发 stop —— 网络断了机器人就会一直跑，这是安全问题。
 */

#pragma once

#include <stdbool.h>
#include <stdint.h>

#include "esp_err.h"

#ifdef __cplusplus
extern "C" {
#endif

/* 初始化 GPIO + LEDC PWM。未启用电机时是空实现，返回 ESP_OK。 */
esp_err_t bot_motor_init(void);

bool bot_motor_ready(void);

/*
 * 设置左右轮速度（m/s）。
 * 内部做死区补偿与限幅，然后写 PWM。
 */
esp_err_t bot_motor_set_wheel_speed(float left_mps, float right_mps);

/*
 * 按差速模型设置机器人速度。
 * duration_ms > 0 时启动开环定时器，到点自动停下；
 * duration_ms == 0 表示持续，直到收到新的指令或 stop。
 */
esp_err_t bot_motor_drive(float linear, float angular, int duration_ms);

/* 立刻停止（滑行） */
void bot_motor_stop(void);

/* 是否正在运动 */
bool bot_motor_is_moving(void);

/* 当前设定的速度（用于遥测回报） */
void bot_motor_get_motion(float *linear, float *angular);

/*
 * 运行期修改限幅（对应协议的 set_motion_limits 动作）。
 * 任一项为负表示保持不变。
 */
esp_err_t bot_motor_set_limits(float max_linear, float max_angular);

/* 把当前限幅读出来（写进 hello 的 motor 字段） */
void bot_motor_get_limits(float *max_linear, float *max_angular);

/*
 * 主循环里定期调用：检查开环定时是否到点。
 * 到点会自动停车并调用注册的回调（用于发 motion_done 事件）。
 */
void bot_motor_poll(void);

/* 注册"运动完成"回调（在 bot_motor_poll 里被调用）。 */
typedef void (*bot_motor_done_cb_t)(void);
void bot_motor_set_done_cb(bot_motor_done_cb_t cb);

#ifdef __cplusplus
}
#endif
