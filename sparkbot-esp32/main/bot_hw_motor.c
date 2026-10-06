/*
 * 底盘电机实现：通用双路 H 桥 + 差速运动学
 *
 * 线程安全：PWM 底层驱动自带互斥，速度状态用一个互斥锁保护，
 * 因为主循环（协议处理）与定时器轮询都会碰它。
 */

#include "bot_hw_motor.h"

#include <math.h>
#include <stdlib.h>
#include <string.h>

#include "driver/gpio.h"
#include "driver/ledc.h"
#include "esp_log.h"
#include "esp_timer.h"
#include "freertos/FreeRTOS.h"
#include "freertos/semphr.h"

static const char *TAG = "bot_motor";

/* LEDC 配置：14 位分辨率在 20kHz 下，占空比步进约 0.006%，
 * 对电机调速绰绰有余，同时对低速段也有足够分辨率。 */
#define MOTOR_LEDC_MODE       LEDC_LOW_SPEED_MODE
#define MOTOR_LEDC_TIMER      LEDC_TIMER_2   /* 0/1 留给背光与其它用途 */
#define MOTOR_LEDC_RES        LEDC_TIMER_14_BIT
#define MOTOR_LEDC_MAX_DUTY   ((1 << 14) - 1)

#define MOTOR_CH_LEFT  LEDC_CHANNEL_0
#define MOTOR_CH_RIGHT LEDC_CHANNEL_1

typedef struct {
    bool ready;
    bool moving;
    float linear;        /* 当前线速度 m/s */
    float angular;       /* 当前角速度 rad/s */
    float max_linear;
    float max_angular;
    float wheel_base;    /* 轮距 m */
    int deadzone_pct;    /* 占空比死区 % */

    int64_t stop_at_us;  /* 开环定时的停止时刻，0 = 不定时 */
    bot_motor_done_cb_t done_cb;

    SemaphoreHandle_t lock;
} motor_ctx_t;

static motor_ctx_t s_m;

/* ------------------------------------------------------------------ */
/* 方向控制                                                           */
/* ------------------------------------------------------------------ */

/*
 * 设置一路电机的转向。
 *
 * 单 PWM 模式（IN2 = -1）：用 IN1 电平定方向，PWM 调速。
 *   这是最简接法，但无法反向制动，且低速时扭矩差。
 * 双 PWM 模式（IN1/IN2 都给）：IN1 高 IN2 低 = 正转，反之反转，
 *   两个都低 = 滑行，两个都高 = 刹车。
 */
static void set_direction(int in1, int in2, bool forward, bool active)
{
    if (in1 < 0) {
        return;
    }

    if (in2 < 0) {
        /* 单 PWM 模式：方向靠 IN1 电平，PWM 恒为有效 */
        gpio_set_level((gpio_num_t)in1, forward ? 1 : 0);
        return;
    }

    if (!active) {
        /* 停：两脚都拉低 = 滑行（不刹车，避免急停甩尾） */
        gpio_set_level((gpio_num_t)in1, 0);
        gpio_set_level((gpio_num_t)in2, 0);
        return;
    }

    gpio_set_level((gpio_num_t)in1, forward ? 1 : 0);
    gpio_set_level((gpio_num_t)in2, forward ? 0 : 1);
}

/*
 * 把带符号的速度比例（-1.0 ~ +1.0）写进 LEDC。
 *
 * 死区补偿：静摩擦导致小占空比时电机不转。设定死区后把有效区间
 * 线性映射到 [deadzone, 100%]，这样最低速也能起步，
 * 代价是极低速不可达（对机器人无所谓）。
 */
static void write_channel(ledc_channel_t ch, int in1, int in2, float signed_ratio)
{
    float mag = fabsf(signed_ratio);
    if (mag > 1.0f) {
        mag = 1.0f;
    }

    bool active = mag > 0.001f;
    set_direction(in1, in2, signed_ratio >= 0.0f, active);

    if (!active) {
        ledc_set_duty(MOTOR_LEDC_MODE, ch, 0);
        ledc_update_duty(MOTOR_LEDC_MODE, ch);
        return;
    }

    float dead = (float)s_m.deadzone_pct / 100.0f;
    if (dead > 0.9f) {
        dead = 0.9f; /* 死区过大等于没有调速空间，钳一下 */
    }
    float ratio = dead + mag * (1.0f - dead);

    uint32_t duty = (uint32_t)(ratio * (float)MOTOR_LEDC_MAX_DUTY);
    ledc_set_duty(MOTOR_LEDC_MODE, ch, duty);
    ledc_update_duty(MOTOR_LEDC_MODE, ch);
}

/* ------------------------------------------------------------------ */
/* 初始化                                                             */
/* ------------------------------------------------------------------ */

esp_err_t bot_motor_init(void)
{
    memset(&s_m, 0, sizeof(s_m));
    s_m.lock = xSemaphoreCreateMutex();
    if (s_m.lock == NULL) {
        return ESP_ERR_NO_MEM;
    }

    /* 限幅从 Kconfig 读（字符串是因为 Kconfig int 只支持整数，
     * 而线速度需要小数）。解析失败时用保守默认值。 */
    s_m.max_linear = strtof(CONFIG_SPARKBOT_MOTOR_MAX_LINEAR, NULL);
    if (s_m.max_linear <= 0.0f || s_m.max_linear > 5.0f) {
        s_m.max_linear = 0.6f;
    }
    s_m.max_angular = strtof(CONFIG_SPARKBOT_MOTOR_MAX_ANGULAR, NULL);
    if (s_m.max_angular <= 0.0f || s_m.max_angular > 10.0f) {
        s_m.max_angular = 2.0f;
    }
    s_m.wheel_base = strtof(CONFIG_SPARKBOT_MOTOR_WHEEL_BASE_M, NULL);
    if (s_m.wheel_base <= 0.01f || s_m.wheel_base > 2.0f) {
        s_m.wheel_base = 0.15f;
    }
    s_m.deadzone_pct = CONFIG_SPARKBOT_MOTOR_DEADZONE_PERCENT;

#if !CONFIG_SPARKBOT_MOTOR_ENABLE
    ESP_LOGI(TAG, "电机未启用（menuconfig -> 底盘电机）。drive 会回 unsupported_action。");
    return ESP_OK;
#endif

    /* 方向脚 */
    uint64_t dir_mask = 0;
    if (CONFIG_SPARKBOT_MOTOR_LEFT_IN1_PIN >= 0) {
        dir_mask |= (1ULL << CONFIG_SPARKBOT_MOTOR_LEFT_IN1_PIN);
    }
    if (CONFIG_SPARKBOT_MOTOR_LEFT_IN2_PIN >= 0) {
        dir_mask |= (1ULL << CONFIG_SPARKBOT_MOTOR_LEFT_IN2_PIN);
    }
    if (CONFIG_SPARKBOT_MOTOR_RIGHT_IN1_PIN >= 0) {
        dir_mask |= (1ULL << CONFIG_SPARKBOT_MOTOR_RIGHT_IN1_PIN);
    }
    if (CONFIG_SPARKBOT_MOTOR_RIGHT_IN2_PIN >= 0) {
        dir_mask |= (1ULL << CONFIG_SPARKBOT_MOTOR_RIGHT_IN2_PIN);
    }

    gpio_config_t io = {
        .pin_bit_mask = dir_mask,
        .mode = GPIO_MODE_OUTPUT,
        .pull_up_en = GPIO_PULLUP_DISABLE,
        .pull_down_en = GPIO_PULLDOWN_DISABLE,
        .intr_type = GPIO_INTR_DISABLE,
    };
    if (dir_mask != 0) {
        esp_err_t err = gpio_config(&io);
        if (err != ESP_OK) {
            ESP_LOGE(TAG, "方向脚配置失败: %s", esp_err_to_name(err));
            return err;
        }
    }

    /* PWM 定时器 */
    ledc_timer_config_t tcfg = {
        .speed_mode = MOTOR_LEDC_MODE,
        .timer_num = MOTOR_LEDC_TIMER,
        .duty_resolution = MOTOR_LEDC_RES,
        .freq_hz = CONFIG_SPARKBOT_MOTOR_PWM_FREQ_HZ,
        .clk_cfg = LEDC_AUTO_CLK,
    };
    esp_err_t err = ledc_timer_config(&tcfg);
    if (err != ESP_OK) {
        ESP_LOGE(TAG, "LEDC 定时器配置失败: %s", esp_err_to_name(err));
        return err;
    }

    /* 两个 PWM 通道 */
    const int pwm_pins[2] = {CONFIG_SPARKBOT_MOTOR_LEFT_PWM_PIN,
                             CONFIG_SPARKBOT_MOTOR_RIGHT_PWM_PIN};
    const ledc_channel_t chans[2] = {MOTOR_CH_LEFT, MOTOR_CH_RIGHT};
    for (int i = 0; i < 2; i++) {
        if (pwm_pins[i] < 0) {
            ESP_LOGE(TAG, "第 %d 路 PWM 引脚未配置", i);
            return ESP_ERR_INVALID_ARG;
        }
        ledc_channel_config_t ccfg = {
            .gpio_num = pwm_pins[i],
            .speed_mode = MOTOR_LEDC_MODE,
            .channel = chans[i],
            .intr_type = LEDC_INTR_DISABLE,
            .timer_sel = MOTOR_LEDC_TIMER,
            .duty = 0,
            .hpoint = 0,
        };
        err = ledc_channel_config(&ccfg);
        if (err != ESP_OK) {
            ESP_LOGE(TAG, "LEDC 通道 %d 配置失败: %s", i, esp_err_to_name(err));
            return err;
        }
    }

    s_m.ready = true;
    ESP_LOGI(TAG, "电机就绪: 左 PWM=%d IN=%d/%d, 右 PWM=%d IN=%d/%d, %dHz 轮距=%.3fm",
             CONFIG_SPARKBOT_MOTOR_LEFT_PWM_PIN, CONFIG_SPARKBOT_MOTOR_LEFT_IN1_PIN,
             CONFIG_SPARKBOT_MOTOR_LEFT_IN2_PIN, CONFIG_SPARKBOT_MOTOR_RIGHT_PWM_PIN,
             CONFIG_SPARKBOT_MOTOR_RIGHT_IN1_PIN, CONFIG_SPARKBOT_MOTOR_RIGHT_IN2_PIN,
             CONFIG_SPARKBOT_MOTOR_PWM_FREQ_HZ, s_m.wheel_base);
    return ESP_OK;
}

bool bot_motor_ready(void)
{
    return s_m.ready;
}

/* ------------------------------------------------------------------ */
/* 速度控制                                                           */
/* ------------------------------------------------------------------ */

esp_err_t bot_motor_set_wheel_speed(float left_mps, float right_mps)
{
    if (!s_m.ready) {
        return ESP_ERR_INVALID_STATE;
    }

    /* m/s → 占空比比例。用 max_linear 作为满速参考：
     * 这是简化模型（没有编码器，无法闭环），
     * 效果是"0.6 m/s 请求 = 100% 占空比"，速度不完全线性但可用。 */
    float ref = s_m.max_linear > 0.01f ? s_m.max_linear : 0.6f;
    float lr = left_mps / ref;
    float rr = right_mps / ref;

    xSemaphoreTake(s_m.lock, portMAX_DELAY);
    write_channel(MOTOR_CH_LEFT, CONFIG_SPARKBOT_MOTOR_LEFT_IN1_PIN,
                  CONFIG_SPARKBOT_MOTOR_LEFT_IN2_PIN, lr);
    write_channel(MOTOR_CH_RIGHT, CONFIG_SPARKBOT_MOTOR_RIGHT_IN1_PIN,
                  CONFIG_SPARKBOT_MOTOR_RIGHT_IN2_PIN, rr);
    xSemaphoreGive(s_m.lock);
    return ESP_OK;
}

esp_err_t bot_motor_drive(float linear, float angular, int duration_ms)
{
    if (!s_m.ready) {
        return ESP_ERR_INVALID_STATE;
    }

    /* 限幅：固件侧必须自己再做一次。
     * 不能只依赖 PC 端护栏 —— PC 可能来自任意客户端，
     * 而且这是会真的撞到东西的设备。 */
    if (linear > s_m.max_linear) {
        linear = s_m.max_linear;
    }
    if (linear < -s_m.max_linear) {
        linear = -s_m.max_linear;
    }
    if (angular > s_m.max_angular) {
        angular = s_m.max_angular;
    }
    if (angular < -s_m.max_angular) {
        angular = -s_m.max_angular;
    }

    /* 差速：正 angular = 左转 = 右轮快、左轮慢 */
    float half = s_m.wheel_base * 0.5f;
    float v_left = linear - angular * half;
    float v_right = linear + angular * half;

    esp_err_t err = bot_motor_set_wheel_speed(v_left, v_right);
    if (err != ESP_OK) {
        return err;
    }

    xSemaphoreTake(s_m.lock, portMAX_DELAY);
    s_m.linear = linear;
    s_m.angular = angular;
    s_m.moving = (fabsf(linear) > 0.001f || fabsf(angular) > 0.001f);
    if (duration_ms > 0) {
        s_m.stop_at_us = esp_timer_get_time() + (int64_t)duration_ms * 1000;
    } else {
        s_m.stop_at_us = 0; /* 持续运动 */
    }
    xSemaphoreGive(s_m.lock);

    ESP_LOGD(TAG, "drive linear=%.3f angular=%.3f duration=%dms → L=%.3f R=%.3f m/s",
             linear, angular, duration_ms, v_left, v_right);
    return ESP_OK;
}

void bot_motor_stop(void)
{
    if (!s_m.ready) {
        return;
    }

    xSemaphoreTake(s_m.lock, portMAX_DELAY);
    write_channel(MOTOR_CH_LEFT, CONFIG_SPARKBOT_MOTOR_LEFT_IN1_PIN,
                  CONFIG_SPARKBOT_MOTOR_LEFT_IN2_PIN, 0.0f);
    write_channel(MOTOR_CH_RIGHT, CONFIG_SPARKBOT_MOTOR_RIGHT_IN1_PIN,
                  CONFIG_SPARKBOT_MOTOR_RIGHT_IN2_PIN, 0.0f);
    s_m.linear = 0.0f;
    s_m.angular = 0.0f;
    s_m.moving = false;
    s_m.stop_at_us = 0;
    xSemaphoreGive(s_m.lock);
}

bool bot_motor_is_moving(void)
{
    return s_m.ready && s_m.moving;
}

void bot_motor_get_motion(float *linear, float *angular)
{
    xSemaphoreTake(s_m.lock, portMAX_DELAY);
    if (linear != NULL) {
        *linear = s_m.linear;
    }
    if (angular != NULL) {
        *angular = s_m.angular;
    }
    xSemaphoreGive(s_m.lock);
}

esp_err_t bot_motor_set_limits(float max_linear, float max_angular)
{
    if (!s_m.ready) {
        return ESP_ERR_INVALID_STATE;
    }
    xSemaphoreTake(s_m.lock, portMAX_DELAY);
    if (max_linear > 0.0f) {
        s_m.max_linear = max_linear;
    }
    if (max_angular > 0.0f) {
        s_m.max_angular = max_angular;
    }
    xSemaphoreGive(s_m.lock);
    ESP_LOGI(TAG, "限幅更新: max_linear=%.3f max_angular=%.3f", s_m.max_linear, s_m.max_angular);
    return ESP_OK;
}

void bot_motor_get_limits(float *max_linear, float *max_angular)
{
    xSemaphoreTake(s_m.lock, portMAX_DELAY);
    if (max_linear != NULL) {
        *max_linear = s_m.max_linear;
    }
    if (max_angular != NULL) {
        *max_angular = s_m.max_angular;
    }
    xSemaphoreGive(s_m.lock);
}

void bot_motor_set_done_cb(bot_motor_done_cb_t cb)
{
    s_m.done_cb = cb;
}

void bot_motor_poll(void)
{
    if (!s_m.ready) {
        return;
    }

    int64_t stop_at;
    xSemaphoreTake(s_m.lock, portMAX_DELAY);
    stop_at = s_m.stop_at_us;
    xSemaphoreGive(s_m.lock);

    if (stop_at == 0 || esp_timer_get_time() < stop_at) {
        return;
    }

    /* 开环定时到点：自己停下。
     * 这一点是关键的安全设计 —— 网络断了、PC 崩了，
     * 机器人也不会一直往前冲。 */
    bot_motor_stop();
    ESP_LOGI(TAG, "开环定时结束，已自动停车");

    if (s_m.done_cb != NULL) {
        s_m.done_cb();
    }
}
