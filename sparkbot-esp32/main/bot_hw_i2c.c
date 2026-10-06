/*
 * 板载 I2C 总线实现
 *
 * ES8311（0x30）与 AXP2101（0x34）共用 SDA=GPIO1 / SCL=GPIO2。
 * 统一在这里建总线，其余模块通过 bot_i2c_add_device 挂从设备。
 */

#include "bot_hw_i2c.h"

#include "esp_log.h"

static const char *TAG = "bot_i2c";

static i2c_master_bus_handle_t s_bus = NULL;

esp_err_t bot_i2c_init(void)
{
    if (s_bus != NULL) {
        return ESP_OK; /* 已初始化，幂等 */
    }

    i2c_master_bus_config_t cfg = {
        .i2c_port = I2C_NUM_0,
        .sda_io_num = CONFIG_SPARKBOT_AUDIO_I2C_SDA_PIN,
        .scl_io_num = CONFIG_SPARKBOT_AUDIO_I2C_SCL_PIN,
        .clk_source = I2C_CLK_SRC_DEFAULT,
        .glitch_ignore_cnt = 7,
        /* 板载总线有上拉电阻，这里不再开内部上拉（内部上拉偏弱） */
        .flags.enable_internal_pullup = false,
    };

    esp_err_t err = i2c_new_master_bus(&cfg, &s_bus);
    if (err != ESP_OK) {
        ESP_LOGE(TAG, "I2C 总线初始化失败: %s (SDA=%d SCL=%d)", esp_err_to_name(err),
                 CONFIG_SPARKBOT_AUDIO_I2C_SDA_PIN, CONFIG_SPARKBOT_AUDIO_I2C_SCL_PIN);
        s_bus = NULL;
        return err;
    }

    ESP_LOGI(TAG, "I2C 总线就绪: SDA=%d SCL=%d", CONFIG_SPARKBOT_AUDIO_I2C_SDA_PIN,
             CONFIG_SPARKBOT_AUDIO_I2C_SCL_PIN);
    return ESP_OK;
}

bool bot_i2c_ready(void)
{
    return s_bus != NULL;
}

i2c_master_bus_handle_t bot_i2c_bus(void)
{
    return s_bus;
}

esp_err_t bot_i2c_add_device(uint8_t addr, uint32_t scl_speed,
                             i2c_master_dev_handle_t *out_dev)
{
    if (s_bus == NULL || out_dev == NULL) {
        return ESP_ERR_INVALID_STATE;
    }

    i2c_device_config_t dev_cfg = {
        .dev_addr_length = I2C_ADDR_BIT_LEN_7,
        .device_address = addr,
        .scl_speed_hz = scl_speed,
    };

    esp_err_t err = i2c_master_bus_add_device(s_bus, &dev_cfg, out_dev);
    if (err != ESP_OK) {
        ESP_LOGW(TAG, "挂载 I2C 从设备 0x%02X 失败: %s", addr, esp_err_to_name(err));
    }
    return err;
}
