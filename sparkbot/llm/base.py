"""Provider 基类与统一数据结构。

``LLMProvider`` 是整个框架唯一的模型入口。之所以自己定义消息结构而不是
直接透传各家 SDK 的 dict，是为了：

* **可替换**：换模型只改一个工厂函数，agent 循环一行不动；
* **可序列化**：会话历史能直接存成 JSON，方便调试与回放；
* **可测试**：``MockProvider`` 不联网就能驱动完整工具调用循环。
"""

from __future__ import annotations

import enum
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, Literal

from ..config import LLMSettings, VisionSettings
from ..core.errors import ProviderError


class MessageRole(str, enum.Enum):
    """对话角色。"""

    SYSTEM = "system"
    USER = "user"
    ASSISTANT = "assistant"
    TOOL = "tool"


@dataclass(slots=True)
class ToolCallRequest:
    """模型请求调用某个工具。"""

    id: str
    name: str
    arguments: dict[str, Any] = field(default_factory=dict)
    #: 模型给出的原始参数字符串；解析失败时保留下来便于排查。
    raw_arguments: str = ""

    def to_openai_dict(self) -> dict[str, Any]:
        """转成 OpenAI 协议里 assistant.tool_calls 的一项。"""
        return {
            "id": self.id,
            "type": "function",
            "function": {"name": self.name, "arguments": self.raw_arguments or "{}"},
        }


@dataclass(slots=True)
class ChatMessage:
    """一条对话消息。

    ``images`` 用于多模态输入：元素是 ``data:image/jpeg;base64,...`` 或公网 URL。
    ``tool_call_id`` / ``tool_calls`` 用于工具调用协议往返。
    """

    role: MessageRole | str
    content: str = ""
    images: list[str] = field(default_factory=list)
    tool_calls: list[ToolCallRequest] = field(default_factory=list)
    tool_call_id: str | None = None

    @property
    def role_value(self) -> str:
        """角色的字符串值。"""
        return self.role.value if isinstance(self.role, MessageRole) else str(self.role)

    def to_openai_dict(self) -> dict[str, Any]:
        """转成 OpenAI 兼容的 message 对象（含多模态 content 数组）。"""
        message: dict[str, Any] = {"role": self.role_value}

        if self.images:
            parts: list[dict[str, Any]] = []
            if self.content:
                parts.append({"type": "text", "text": self.content})
            parts.extend({"type": "image_url", "image_url": {"url": url}} for url in self.images)
            message["content"] = parts
        else:
            message["content"] = self.content

        if self.tool_calls:
            message["tool_calls"] = [call.to_openai_dict() for call in self.tool_calls]
            # 带工具调用的 assistant 消息 content 允许为空。
            message.setdefault("content", self.content or None)
        if self.tool_call_id:
            message["tool_call_id"] = self.tool_call_id
        return message

    def to_dict(self) -> dict[str, Any]:
        """转成可 JSON 持久化的字典（图片只记录数量，避免历史膨胀）。"""
        return {
            "role": self.role_value,
            "content": self.content,
            "images": len(self.images),
            "tool_calls": [
                {"id": c.id, "name": c.name, "arguments": c.arguments} for c in self.tool_calls
            ],
            "tool_call_id": self.tool_call_id,
        }

    @classmethod
    def system(cls, content: str) -> ChatMessage:
        """构造 system 消息。"""
        return cls(role=MessageRole.SYSTEM, content=content)

    @classmethod
    def user(cls, content: str, images: list[str] | None = None) -> ChatMessage:
        """构造 user 消息。"""
        return cls(role=MessageRole.USER, content=content, images=list(images or []))

    @classmethod
    def tool(cls, tool_call_id: str, content: str) -> ChatMessage:
        """构造工具结果消息。"""
        return cls(role=MessageRole.TOOL, content=content, tool_call_id=tool_call_id)


@dataclass(slots=True)
class Usage:
    """token 用量，便于做成本核算。"""

    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0

    def __add__(self, other: Usage) -> Usage:
        return Usage(
            prompt_tokens=self.prompt_tokens + other.prompt_tokens,
            completion_tokens=self.completion_tokens + other.completion_tokens,
            total_tokens=self.total_tokens + other.total_tokens,
        )


@dataclass(slots=True)
class LLMResponse:
    """一次模型调用的结果。"""

    content: str = ""
    tool_calls: list[ToolCallRequest] = field(default_factory=list)
    usage: Usage = field(default_factory=Usage)
    model: str = ""
    finish_reason: str = ""
    raw: dict[str, Any] | None = None

    @property
    def wants_tools(self) -> bool:
        """模型是否要求调用工具。"""
        return bool(self.tool_calls)


class LLMProvider(ABC):
    """模型 provider 的统一接口。"""

    #: 人类可读的 provider 名，用于日志与 ``/api/status``。
    name: str = "base"

    #: 是否支持图像输入。
    supports_vision: bool = False

    @abstractmethod
    async def chat(
        self,
        messages: list[ChatMessage],
        *,
        tools: list[dict[str, Any]] | None = None,
        temperature: float | None = None,
        max_tokens: int | None = None,
    ) -> LLMResponse:
        """执行一次对话补全。

        Args:
            messages: 完整对话历史（已含 system prompt）。
            tools: OpenAI 形状的工具定义数组；``None`` 表示本次不开放工具。
            temperature: 覆盖默认温度。
            max_tokens: 覆盖默认最大输出长度。
        """

    async def aclose(self) -> None:
        """释放底层网络资源；默认无操作。"""
        return None


# --------------------------------------------------------------------------- #
# 工厂
# --------------------------------------------------------------------------- #
def create_provider(
    settings: LLMSettings | VisionSettings,
    *,
    purpose: Literal["chat", "vision"] = "chat",
) -> LLMProvider:
    """按配置构造 provider。

    Args:
        settings: ``LLMSettings`` 或 ``VisionSettings``。
        purpose: 用于日志与错误提示，区分是主对话还是视觉理解。

    Raises:
        ProviderError: 配置不完整或 provider 名未知。
    """
    provider_name = getattr(settings, "provider", None) or "mock"

    # 视觉配置可以把 provider 留空以复用主 LLM 的设置。
    if purpose == "vision" and isinstance(settings, VisionSettings) and settings.provider is None:
        raise ProviderError("视觉 provider 未配置，且未提供回退的 LLM 设置")

    api_key = getattr(settings, "api_key", None)
    secret = api_key.get_secret_value() if hasattr(api_key, "get_secret_value") else api_key

    base_url = getattr(settings, "base_url", None)
    model = getattr(settings, "model", None) or "gpt-4o-mini"
    timeout = float(
        getattr(settings, "timeout_s", None) or getattr(settings, "request_timeout_s", 60.0)
    )

    if provider_name == "mock":
        from .mock_provider import MockProvider

        return MockProvider(model=model or "mock-model")

    if provider_name == "openai":
        from .openai_provider import OpenAIProvider

        return OpenAIProvider(
            api_key=secret or "",
            model=model,
            base_url=base_url or "https://api.openai.com/v1",
            timeout_s=timeout,
            name="openai",
        )

    if provider_name == "deepseek":
        from .openai_provider import OpenAIProvider

        return OpenAIProvider(
            api_key=secret or "",
            model=model if model != "gpt-4o-mini" else "deepseek-chat",
            base_url=base_url or "https://api.deepseek.com/v1",
            timeout_s=timeout,
            name="deepseek",
            supports_vision=False,
        )

    if provider_name == "openai_compat":
        if not base_url:
            raise ProviderError("provider=openai_compat 必须提供 base_url")
        from .openai_provider import OpenAIProvider

        return OpenAIProvider(
            api_key=secret or "not-needed",
            model=model,
            base_url=base_url,
            timeout_s=timeout,
            name="openai_compat",
        )

    raise ProviderError(f"未知的 provider: {provider_name!r}", purpose=purpose)


def resolve_vision_config(llm: LLMSettings, vision: VisionSettings) -> VisionSettings:
    """补全视觉配置：未显式设置时复用主 LLM 的 provider / key / base_url。

    这样用户只配一套 ``SPARKBOT_LLM_*`` 就能同时跑通对话与看图。
    """
    if vision.provider is not None:
        return vision

    resolved = vision.model_copy()
    resolved.provider = llm.provider
    resolved.model = vision.model or llm.model
    resolved.base_url = vision.base_url or llm.base_url
    resolved.api_key = vision.api_key or llm.api_key
    return resolved
