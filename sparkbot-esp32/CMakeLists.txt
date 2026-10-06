# SparkBot 固件（ESP32-S3）—— 顶层 CMake 工程文件
#
# 这是一个独立的 ESP-IDF 工程。
# 它复用官方组件（esp32-camera / esp_codec_dev / esp_lcd_ili9341）实现硬件访问，
# 协议层（WebSocket + JSON）为全新实现，对接 PC 端 SparkBot 框架。
#
# 构建：
#   idf.py set-target esp32s3
#   idf.py build

cmake_minimum_required(VERSION 3.16)

include($ENV{IDF_PATH}/tools/cmake/project.cmake)
project(sparkbot_firmware)
