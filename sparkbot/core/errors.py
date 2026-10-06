"""框架异常层次。所有对外抛出的错误都应继承 :class:`SparkBotError`。"""

from __future__ import annotations

from typing import Any


class SparkBotError(Exception):
    """框架内所有异常的基类。"""

    def __init__(self, message: str, **context: Any) -> None:
        super().__init__(message)
        self.message = message
        self.context = context

    def __str__(self) -> str:  # pragma: no cover - 仅用于日志可读性
        if not self.context:
            return self.message
        detail = " ".join(f"{k}={v!r}" for k, v in self.context.items())
        return f"{self.message} ({detail})"


class DeviceError(SparkBotError):
    """设备侧返回失败，或设备不可用。"""


class DeviceOfflineError(DeviceError):
    """目标设备当前没有活跃连接。"""


class DeviceTimeoutError(DeviceError):
    """等待设备回复超时。"""


class ProtocolViolationError(SparkBotError):
    """对端发来了不符合协议的内容。"""


class ToolError(SparkBotError):
    """工具执行失败；该错误会被回喂给模型，让模型自行纠正。"""


class ProviderError(SparkBotError):
    """LLM / ASR / TTS 等外部 provider 调用失败。"""


class SafetyError(ToolError):
    """动作被安全策略拦截。"""
