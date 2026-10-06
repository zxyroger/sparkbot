/*
 * 板载 I2C 总线：ES8311 音频 codec 与 AXP2101 电源管理共用一条总线。
 *
 * 为什么把总线单独抽出来：这两个外设地址不同（0x30 / 0x34）但物理上
 * 是同一对 SDA/SCL。如果各自初始化一遍会互相覆盖驱动状态，
 * 所以统一在这里初始化一次，其余模块通过句柄借用。
 */

#pragma once

#include <stdbool.h>
#include <stdint.h>

#include "driver/i2c_master.h"
#include "esp_err.h"

#ifdef __cplusplus
extern "C" {
#endif

/* 初始化板载 I2C 总线。可重复调用，只有第一次真正建总线。 */
esp_err_t bot_i2c_init(void);

/* 总线是否可用 */
bool bot_i2c_ready(void);

/* 总线句柄（给 esp_codec_dev 之类的组件用）；未初始化返回 NULL */
i2c_master_bus_handle_t bot_i2c_bus(void);

/*
 * 绑定一个 I2C 从设备。
 *
 * @param addr        7 位地址（如 ES8311 = 0x30，AXP2101 = 0x34）
 * @param scl_speed   总线频率 Hz
 * @param out_dev     输出句柄
 */
esp_err_t bot_i2c_add_device(uint8_t addr, uint32_t scl_speed,
                             i2c_master_dev_handle_t *out_dev);

#ifdef __cplusplus
}
#endif
