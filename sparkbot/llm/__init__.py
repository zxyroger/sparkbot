"""LLM 抽象层：统一 provider 接口，便于在云端 API / 本地模型 / 假模型间切换。"""

from .base import (
    ChatMessage,
    LLMProvider,
    LLMResponse,
    MessageRole,
    ToolCallRequest,
    Usage,
    create_provider,
    resolve_vision_config,
)

__all__ = [
    "ChatMessage",
    "LLMProvider",
    "LLMResponse",
    "MessageRole",
    "ToolCallRequest",
    "Usage",
    "create_provider",
    "resolve_vision_config",
]
