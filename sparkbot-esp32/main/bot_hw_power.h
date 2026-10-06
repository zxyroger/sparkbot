/*
 * 电源遥测：AXP2101 PMIC 的电池电压 / 电量 / 充电状态。
 *
 * 对应 SparkBot 协议的 telemetry.battery 字段：
 *   {"voltage": 8.12, "percent": 86.0, "charging": false, "external": true}
 * 以及 low_battery 事件。
 *
 * 为什么值得单独做：机器人是移动设备，PC 端的模型需要知道电量
 * 才能决定"还能不能继续跑"。协议里 battery 是遥测里唯一带语义的字段
 * （PC 会读它并在提示词里告诉模型）。
 */

#pragma once

#include <stdbool.h>
#include <stdint.h>

#include "esp_err.h"

#ifdef __cplusplus
extern "C" {
#endif

typedef struct {
    bool battery_present;  /* 是否检测到电池 */
    bool external_power;   /* 是否由 USB/VBUS 供电 */
    bool charging;         /* 是否正在充电 */
    int battery_mv;        /* 电池电压 mV，0 = 读不到 */
    int vbus_mv;           /* VBUS 电压 mV，0 = 未插 */
    int percent;           /* 电量 0~100，-1 = 读不到 */
    bool read_error;       /* true = PMIC 无应答，以上字段都不可信 */
} bot_power_info_t;

/* 初始化 AXP2101（探测 + 使能电量计）。可重复调用。 */
esp_err_t bot_power_init(void);

bool bot_power_ready(void);

/* 读一次电源状态。未初始化或无 PMIC 时返回 false。 */
bool bot_power_poll(bot_power_info_t *out);

/* 低电阈值判定（读 Kconfig 常量），供主循环发 low_battery 事件 */
#define BOT_POWER_LOW_PERCENT 20
#define BOT_POWER_CRITICAL_PERCENT 5

/*
 * 打开摄像头供电。
 *
 * 本板 OV2640 有三路供电挂在 AXP2101 上：
 *
 *   ALDO2  VDDCAM_3V3  摄像头 I/O
 *   BLDO1  AVDD        模拟电源
 *   BLDO2  DVDD        数字内核
 *
 * 三路的**电压也必须设对**，不能只动使能位：
 * 实测这块板冷启动时 DVDD 的电压寄存器在 2.8V 档，而 OV2640 的 DVDD
 * 要求 1.2V 左右。若只把使能位置 1，DVDD 会以错误电压上电（可能损坏
 * 模组，或表现为"探不到传感器"）。
 *
 * 因此这里按原厂板级配置设置电压后再使能：
 *   ALDO2 = 2800mV, BLDO1 = 2800mV, BLDO2 = 1200mV
 *
 * @return ESP_OK 三路都已按要求使能
 */
esp_err_t bot_power_camera_on(void);

/* 摄像头供电的目标电压（mV），可用来核对 */
#define BOT_POWER_CAM_IO_MV    2800  /* ALDO2  VDDCAM_3V3 */
#define BOT_POWER_CAM_AVDD_MV  2800  /* BLDO1  AVDD */
#define BOT_POWER_CAM_DVDD_MV  1200  /* BLDO2  DVDD */

/*
 * 是否有可用的电池读数。
 *
 * 用来决定 hello 里要不要声明 `battery` 能力：
 * 没接电池时 PMIC 的 VBAT 寄存器会给一个约 400mV 的噪声值，
 * 声明了能力却上报不出有效电量，比不声明更糟（PC 端会显示一个
 * 看起来像真的假电量）。所以这里做一次实际读取 + 合理性判断。
 */
bool bot_power_has_battery(void);

#ifdef __cplusplus
}
#endif
