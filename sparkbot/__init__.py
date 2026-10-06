"""SparkBot —— PC 端机器人 Agent 框架。

架构分层::

    ┌─────────────────────────────────────────────────────────────┐
    │  brain (Agent 循环)  ←→  llm (可插拔 provider)               │
    │        ↑ 工具调用                                            │
    │  brain/tools  →  device/capabilities  →  device/gateway      │
    │        ↑ 感知                                                  │
    │  perception (vision / speech)                                 │
    └─────────────────────────────────────────────────────────────┘
                              ↕ WebSocket + JSON 信封
    ┌─────────────────────────────────────────────────────────────┐
    │  ESP32-S3 固件：摄像头 / 麦克风 / 喇叭 / LCD / 底盘电机        │
    └─────────────────────────────────────────────────────────────┘

对外接口：
    * ``sparkbot.app:create_app``  —— FastAPI 应用工厂
    * ``python -m sparkbot``       —— 启动服务
    * ``python -m sparkbot.mock_device`` —— 启动模拟设备端
"""

__version__ = "0.1.0"

__all__ = ["__version__"]
