# main 组件构建脚本
#
# 依赖说明：
#   esp_http_server / esp_wifi / nvs_flash / mbedtls / json 随 IDF 提供；
#   esp32-camera / esp_codec_dev / esp_lcd_ili9341 / esp-sr 由
#   idf_component.yml 拉取并自动加入构建。

idf_component_register(
    SRCS
        "app_main.c"
        "bot_engine.c"
        "bot_json.c"
        "bot_ws.c"
        "bot_net.c"
        "bot_discovery.c"
        "bot_hw_i2c.c"
        "bot_hw_power.c"
        "bot_hw_motor.c"
        "bot_hw_audio.c"
        "bot_hw_display.c"
        "bot_face.c"
        "bot_hw_camera.c"
        "bot_wakeword.c"
        "bot_face_rec.cpp"
    INCLUDE_DIRS "."
    REQUIRES
        esp_http_server
        esp_wifi
        esp_event
        esp_netif
        esp_timer
        nvs_flash
        mbedtls
        json
        driver
        esp_driver_i2c
        esp_driver_i2s
        esp_driver_ledc
        esp_driver_gpio
        esp_driver_spi
        esp_lcd
        esp32-camera
        esp_codec_dev
        # esp-dl 人脸检测/特征（本地推理）。human_face_recognition 会带出
        # human_face_detect + esp-dl + esp_jpeg。
        espressif__human_face_recognition
        espressif__human_face_detect
        esp-dl
        # esp-sr 提供 AFE + WakeNet（本地唤醒词）。
        # 它是可选能力：不在 menuconfig 里打开时，bot_wakeword.c 会
        # 直接返回 ESP_OK 而不引用任何 esp-sr 符号，但组件仍需能链接到。
        esp-sr
)
