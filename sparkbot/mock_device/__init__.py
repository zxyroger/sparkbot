"""模拟 ESP32-S3 设备端：在没有硬件时验证 PC 端全链路。

它实现与真实固件**完全相同**的协议行为：
握手、心跳遥测、命令响应、事件上报、摄像头抓帧、麦克风采集。

因此 PC 侧任何链路问题（协议字段、超时、媒体配对、工具循环）
都能在这里被发现，而不必先烧录固件。
"""

from .device import MockDevice, MockDeviceConfig

__all__ = ["MockDevice", "MockDeviceConfig"]
