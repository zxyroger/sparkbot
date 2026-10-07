/*
 * 电源遥测实现：AXP2101 PMIC
 *
 * 只读必要的寄存器，不配置 PMIC —— 板子的初始电压/充电参数由出厂
 * 固件或硬件设定决定，从固件里乱改充电电流是危险的（可能烧电池）。
 * 本模块的定位就是"读取并上报"，不做电源策略。
 *
 * 电量估算：AXP2101 有库仑计（fuel gauge），但要用它需要先做一轮
 * 完整的充放电标定并把参数写进 NVS，对"让 PC 知道还剩多少电"这个
 * 需求而言过重。这里用电压法线性映射（3.30V=0%，4.15V=100%），
 * 单节锂电的电压-容量曲线中段接近线性，误差在 ±15% 左右，
 * 对"能不能继续跑"的判断足够。函数里明确标注了这是估算值。
 */

#include "bot_hw_power.h"

#include <string.h>

#include "driver/i2c_master.h"
#include "esp_log.h"
#include "freertos/FreeRTOS.h"
#include "freertos/semphr.h"

#include "bot_hw_i2c.h"

static const char *TAG = "bot_power";

/* AXP2101 寄存器（仅用到这几个） */
#define AXP_REG_STATUS1      0x00  /* bit5 = VBUS 好, bit3 = 电池存在 */
#define AXP_REG_STATUS2      0x01  /* bit6 = 充电中, bit5 = 放电中 */
#define AXP_REG_VBAT_H       0x34
#define AXP_REG_VBAT_L       0x35
#define AXP_REG_VBUS_H       0x36
#define AXP_REG_VBUS_L       0x37
#define AXP_REG_TS_PIN_H     0x38
#define AXP_REG_TS_PIN_L     0x39
#define AXP_REG_LDO_ONOFF0   0x90  /* LDO 使能：bit0~3=ALDO1~4, bit4=BLDO1, bit5=BLDO2 */
#define AXP_REG_ALDO1_VOL    0x92
#define AXP_REG_ALDO2_VOL    0x93  /* 摄像头 I/O 供电 VDDCAM_3V3 */
#define AXP_REG_ALDO3_VOL    0x94
#define AXP_REG_ALDO4_VOL    0x95
#define AXP_REG_BLDO1_VOL    0x96  /* BLDO1 电压 = 500mV + N*100mV (N 取低 5 位) */
#define AXP_REG_BLDO2_VOL    0x97

/* LDO 使能位 */
/*
 * AXP2101 寄存器 0x90 的位定义：bit0~3 = ALDO1~ALDO4，bit4 = BLDO1，bit5 = BLDO2。
 *
 * ⚠️ ALDO2 是 **bit1 (0x02)**，不是 bit2。
 * 这里原来写成 0x04（那是 ALDO3），后果很隐蔽：摄像头的 I/O 供电
 * （VDDCAM_3V3）**一直没打开**，DVP 数据线的高电平只有漏电电压，
 * 低于 ESP32-S3 的判高门限 → SCCB 还能通（开漏+上拉），但拍不出图，
 * 报错是 "Detected camera not supported"，很容易误判成排线/模组坏了。
 * 同板 onegpio 工程的注释写得很明确：ALDO2 默认是关的，必须显式打开。
 */
#define AXP_ALDO2_BIT 0x02  /* bit1 → 摄像头 I/O 供电 VDDCAM_3V3 */
#define AXP_BLDO1_BIT 0x10  /* bit4 → OV2640 AVDD */
#define AXP_BLDO2_BIT 0x20  /* bit5 → OV2640 DVDD */

/* 电压法估算电量的端点（单节锂电，mV） */
#define VBAT_EMPTY_MV 3300
#define VBAT_FULL_MV  4150

static i2c_master_dev_handle_t s_dev = NULL;
static SemaphoreHandle_t s_lock = NULL;
static volatile bool s_ready = false;

/* ------------------------------------------------------------------ */
/* I2C 读写                                                           */
/* ------------------------------------------------------------------ */

static esp_err_t axp_read(uint8_t reg, uint8_t *buf, size_t len)
{
    if (s_dev == NULL) {
        return ESP_ERR_INVALID_STATE;
    }
    /* AXP 的读时序：先写寄存器地址，再读数据。
     * 用 transmit_receive 一次完成，避免两次调用之间被别的任务插入
     * 一次同类访问（总线是共享的）。 */
    return i2c_master_transmit_receive(s_dev, &reg, 1, buf, len, 100);
}

/* AXP 寄存器写。读改写时序：写 [reg, value]。 */
static esp_err_t axp_write(uint8_t reg, uint8_t value)
{
    if (s_dev == NULL) {
        return ESP_ERR_INVALID_STATE;
    }
    uint8_t buf[2] = {reg, value};
    return i2c_master_transmit(s_dev, buf, sizeof(buf), 100);
}

/*
 * 设置一路 LDO 的电压（mV）。
 *
 * 寄存器编码：低 5 位 N，电压 = 500mV + N*100mV，即 N = (mV - 500) / 100。
 * 高 3 位是别的标志，必须保留，所以用读改写。
 */
static esp_err_t axp_set_ldo_mv(uint8_t vol_reg, int millivolt)
{
    if (millivolt < 500 || millivolt > 3500) {
        ESP_LOGW(TAG, "LDO 电压 %d mV 超出范围 (500~3500)", millivolt);
        return ESP_ERR_INVALID_ARG;
    }

    uint8_t cur = 0;
    esp_err_t err = axp_read(vol_reg, &cur, 1);
    if (err != ESP_OK) {
        ESP_LOGW(TAG, "读电压寄存器 0x%02X 失败", vol_reg);
        return err;
    }

    int n = (millivolt - 500) / 100;
    uint8_t target = (uint8_t)((cur & 0xE0) | (n & 0x1F));
    if (target == cur) {
        return ESP_OK; /* 已经是目标值 */
    }

    err = axp_write(vol_reg, target);
    if (err != ESP_OK) {
        ESP_LOGW(TAG, "写电压寄存器 0x%02X 失败", vol_reg);
        return err;
    }
    return ESP_OK;
}

/*
 * 读取某路 LDO 当前配置的电压（mV），用于日志核对。
 * 返回 -1 表示读失败。
 */
static int axp_get_ldo_mv(uint8_t vol_reg)
{
    uint8_t v = 0;
    if (axp_read(vol_reg, &v, 1) != ESP_OK) {
        return -1;
    }
    return 500 + (v & 0x1F) * 100;
}

esp_err_t bot_power_camera_on(void)
{
#if !CONFIG_SPARKBOT_POWER_ENABLE
    return ESP_OK; /* 电源管理未启用：假设摄像头供电由硬件常开 */
#else
    if (s_dev == NULL) {
        ESP_LOGW(TAG, "PMIC 不可用，跳过摄像头供电配置");
        return ESP_OK;
    }

    xSemaphoreTake(s_lock, portMAX_DELAY);

    /*
     * 顺序很重要：**先设电压，再使能**。
     *
     * 反过来（先使能再改电压）会让 LDO 以一个未知的电压先给模组上电 ——
     * DVDD 那一路尤其危险。本板冷启动实测 DVDD 的电压寄存器停在 2.8V 档，
     * 而 OV2640 的 DVDD 只要 1.2V。先设好再开。
     */
    esp_err_t err = axp_set_ldo_mv(AXP_REG_ALDO2_VOL, BOT_POWER_CAM_IO_MV);
    if (err == ESP_OK) {
        err = axp_set_ldo_mv(AXP_REG_BLDO1_VOL, BOT_POWER_CAM_AVDD_MV);
    }
    if (err == ESP_OK) {
        err = axp_set_ldo_mv(AXP_REG_BLDO2_VOL, BOT_POWER_CAM_DVDD_MV);
    }
    if (err != ESP_OK) {
        xSemaphoreGive(s_lock);
        return err;
    }

    /* 读回三路电压用于核对，出错时能一眼看出是哪一路没配对 */
    int io_mv = axp_get_ldo_mv(AXP_REG_ALDO2_VOL);
    int avdd_mv = axp_get_ldo_mv(AXP_REG_BLDO1_VOL);
    int dvdd_mv = axp_get_ldo_mv(AXP_REG_BLDO2_VOL);

    /* 再使能三路 */
    uint8_t onoff = 0;
    err = axp_read(AXP_REG_LDO_ONOFF0, &onoff, 1);
    if (err != ESP_OK) {
        xSemaphoreGive(s_lock);
        ESP_LOGW(TAG, "读 LDO 使能寄存器失败");
        return err;
    }

    const uint8_t need = AXP_ALDO2_BIT | AXP_BLDO1_BIT | AXP_BLDO2_BIT;
    uint8_t want = (uint8_t)(onoff | need);
    if (want != onoff) {
        err = axp_write(AXP_REG_LDO_ONOFF0, want);
        if (err != ESP_OK) {
            xSemaphoreGive(s_lock);
            ESP_LOGW(TAG, "写 LDO 使能寄存器失败");
            return err;
        }
        /* LDO 稳定需要时间；不等待的话 SCCB 探测会随机会失败 */
        vTaskDelay(pdMS_TO_TICKS(80));
    }

    /* 回读确认。写"成功"不等于生效，而供电没起来会以
     * "Detected camera not supported" 的形式失败，很容易被误判成排线问题。 */
    uint8_t after = 0;
    axp_read(AXP_REG_LDO_ONOFF0, &after, 1);
    xSemaphoreGive(s_lock);

    bool io_on = (after & AXP_ALDO2_BIT) != 0;
    bool avdd_on = (after & AXP_BLDO1_BIT) != 0;
    bool dvdd_on = (after & AXP_BLDO2_BIT) != 0;

    ESP_LOGI(TAG, "摄像头供电: 0x90=0x%02X  IO(ALDO2)=%s@%dmV  AVDD(BLDO1)=%s@%dmV  DVDD(BLDO2)=%s@%dmV",
             after, io_on ? "on" : "off", io_mv, avdd_on ? "on" : "off", avdd_mv,
             dvdd_on ? "on" : "off", dvdd_mv);

    if (!io_on || !avdd_on || !dvdd_on) {
        ESP_LOGW(TAG, "摄像头供电未全部打开，传感器可能探测不到");
        return ESP_FAIL;
    }
    if (dvdd_mv != BOT_POWER_CAM_DVDD_MV) {
        ESP_LOGW(TAG, "DVDD 实际 %dmV，期望 %dmV —— 电压不对会导致探测失败或损坏模组",
                 dvdd_mv, BOT_POWER_CAM_DVDD_MV);
        return ESP_FAIL;
    }
    return ESP_OK;
#endif
}

static esp_err_t axp_read_u16(uint8_t reg_h, uint8_t reg_l, int *out_mv){
    uint8_t h = 0, l = 0;
    esp_err_t err = axp_read(reg_h, &h, 1);
    if (err != ESP_OK) {
        return err;
    }
    err = axp_read(reg_l, &l, 1);
    if (err != ESP_OK) {
        return err;
    }
    /* AXP2101 的电压寄存器是 14 位有效（高位在前），
     * 单位 mV。电压值 = (h << 8 | l)，高 2 位无效。 */
    int raw = ((int)h << 8) | l;
    raw &= 0x3FFF;
    *out_mv = raw;
    return ESP_OK;
}

/* ------------------------------------------------------------------ */
/* 初始化                                                             */
/* ------------------------------------------------------------------ */

esp_err_t bot_power_init(void)
{
    if (s_lock == NULL) {
        s_lock = xSemaphoreCreateMutex();
        if (s_lock == NULL) {
            return ESP_ERR_NO_MEM;
        }
    }

    if (s_ready) {
        return ESP_OK;
    }

#if !CONFIG_SPARKBOT_POWER_ENABLE
    ESP_LOGI(TAG, "电源遥测未启用");
    return ESP_OK;
#endif

    if (!bot_i2c_ready()) {
        /* I2C 没起来：不报错，只是没有电池信息。
         * 电池遥测缺失不该阻止机器人的其它功能可用。 */
        ESP_LOGW(TAG, "I2C 总线未就绪，跳过电源管理初始化");
        return ESP_OK;
    }

    esp_err_t err = bot_i2c_add_device(CONFIG_SPARKBOT_POWER_I2C_ADDR, 100000, &s_dev);
    if (err != ESP_OK) {
        return err;
    }

    /* 探测：读 STATUS1，能读通说明芯片在 */
    uint8_t status = 0;
    if (axp_read(AXP_REG_STATUS1, &status, 1) != ESP_OK) {
        ESP_LOGW(TAG, "AXP2101 (0x%02X) 无应答，电池遥测不可用",
                 CONFIG_SPARKBOT_POWER_I2C_ADDR);
        i2c_master_bus_rm_device(s_dev);
        s_dev = NULL;
        return ESP_OK; /* 不算致命 */
    }

    s_ready = true;
    ESP_LOGI(TAG, "AXP2101 就绪 (0x%02X), STATUS1=0x%02X", CONFIG_SPARKBOT_POWER_I2C_ADDR, status);
    return ESP_OK;
}

bool bot_power_ready(void)
{
    return s_ready;
}

bool bot_power_has_battery(void)
{
    bot_power_info_t info;
    if (!bot_power_poll(&info)) {
        return false;
    }
    return info.battery_present;
}

/* ------------------------------------------------------------------ */
/* 读取                                                               */
/* ------------------------------------------------------------------ */

bool bot_power_poll(bot_power_info_t *out)
{
    if (out == NULL) {
        return false;
    }
    memset(out, 0, sizeof(*out));
    out->percent = -1;

    if (!s_ready || s_dev == NULL) {
        out->read_error = true;
        return false;
    }

    xSemaphoreTake(s_lock, portMAX_DELAY);

    uint8_t st1 = 0, st2 = 0;
    bool ok = (axp_read(AXP_REG_STATUS1, &st1, 1) == ESP_OK)
              && (axp_read(AXP_REG_STATUS2, &st2, 1) == ESP_OK);

    if (ok) {
        /* STATUS1 bit5: VBUS 好（插着 USB）
         * STATUS1 bit3: 电池存在 */
        out->external_power = (st1 & 0x20) != 0;
        out->battery_present = (st1 & 0x08) != 0;

        /* STATUS2 bit6: 充电中；bit5: 放电中 */
        out->charging = (st2 & 0x40) != 0;
    }

    int mv = 0;
    if (ok && axp_read_u16(AXP_REG_VBAT_H, AXP_REG_VBAT_L, &mv) == ESP_OK) {
        out->battery_mv = mv;
    } else {
        ok = false;
    }

    int vbus = 0;
    if (axp_read_u16(AXP_REG_VBUS_H, AXP_REG_VBUS_L, &vbus) == ESP_OK) {
        out->vbus_mv = vbus;
    }

    xSemaphoreGive(s_lock);

    if (!ok) {
        out->read_error = true;
        return false;
    }

    /*
     * 合理性校验。
     *
     * 没有接电池时（或者电池被拔掉后），AXP2101 的 VBAT 寄存器会给出
     * 一个很小的噪声值（实测约 400 mV），而不是 0。如果直接上报，
     * PC 端会把它当成真实的电池电压 —— 0.4 V 的"电量"既无意义，
     * 还会让模型据此做出错误判断。
     *
     * 单节锂电的工作区间是 3.0~4.3 V，留一点余量取 2.5~4.6 V。
     * 超出这个范围就认为"读不到有效电压"，宁可报 read_error
     * 也不报一个看起来像真的假值。
     */
    if (out->battery_mv < 2500 || out->battery_mv > 4600) {
        out->read_error = true;
        out->percent = -1;
        return false;
    }

    /* 电压法估算电量（见文件头说明：这是估算，不是库仑计读数） */
    if (out->battery_mv > 1000) {
        int span = VBAT_FULL_MV - VBAT_EMPTY_MV;
        int pct = (out->battery_mv - VBAT_EMPTY_MV) * 100 / span;
        if (pct < 0) {
            pct = 0;
        }
        if (pct > 100) {
            pct = 100;
        }
        out->percent = pct;
    }

    return true;
}
