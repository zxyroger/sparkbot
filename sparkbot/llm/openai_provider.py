"""OpenAI / DeepSeek / 任意 OpenAI 兼容网关的 provider。

只用 ``httpx`` 直接打 ``/chat/completions``，不依赖官方 SDK：
* 少一层依赖，ESP32 项目常在公司内网跑，装包越少越省事；
* 兼容自建网关（vLLM / Ollama / one-api / 各类中转）只要形状对就能用。

``deepseek`` 与 ``openai`` 走的是同一套协议，差别只在默认 base_url 与
是否支持视觉输入，因此共用本实现。
"""

from __future__ import annotations

import json
import logging
from typing import Any

import httpx

from ..core.errors import ProviderError
from .base import ChatMessage, LLMProvider, LLMResponse, ToolCallRequest, Usage

logger = logging.getLogger(__name__)


class OpenAIProvider(LLMProvider):
    """OpenAI 兼容协议的 provider。"""

    def __init__(
        self,
        *,
        api_key: str,
        model: str,
        base_url: str = "https://api.openai.com/v1",
        timeout_s: float = 60.0,
        name: str = "openai",
        supports_vision: bool = True,
        extra_headers: dict[str, str] | None = None,
    ) -> None:
        self.name = name
        self.model = model
        self.base_url = base_url.rstrip("/")
        self.timeout_s = timeout_s
        self.supports_vision = supports_vision
        self._api_key = api_key
        self._extra_headers = dict(extra_headers or {})
        self._client: httpx.AsyncClient | None = None

    # ------------------------------------------------------------------ #
    # 传输
    # ------------------------------------------------------------------ #
    def _headers(self) -> dict[str, str]:
        """组装请求头。"""
        headers = {"Content-Type": "application/json"}
        if self._api_key:
            headers["Authorization"] = f"Bearer {self._api_key}"
        headers.update(self._extra_headers)
        return headers

    async def _get_client(self) -> httpx.AsyncClient:
        """惰性创建复用的 HTTP 连接池。"""
        if self._client is None or self._client.is_closed:
            self._client = httpx.AsyncClient(
                timeout=httpx.Timeout(self.timeout_s, connect=15.0),
                limits=httpx.Limits(max_connections=8, max_keepalive_connections=4),
            )
        return self._client

    async def aclose(self) -> None:
        """关闭连接池。"""
        if self._client is not None and not self._client.is_closed:
            await self._client.aclose()
        self._client = None

    # ------------------------------------------------------------------ #
    # 对话
    # ------------------------------------------------------------------ #
    async def chat(
        self,
        messages: list[ChatMessage],
        *,
        tools: list[dict[str, Any]] | None = None,
        temperature: float | None = None,
        max_tokens: int | None = None,
    ) -> LLMResponse:
        """调用 ``/chat/completions``。

        Raises:
            ProviderError: 网络失败或返回体不符合预期。
        """
        payload: dict[str, Any] = {
            "model": self.model,
            "messages": [m.to_openai_dict() for m in messages],
        }
        if temperature is not None:
            payload["temperature"] = temperature
        if max_tokens is not None:
            payload["max_tokens"] = max_tokens
        if tools:
            payload["tools"] = tools
            payload["tool_choice"] = "auto"

        client = await self._get_client()
        url = f"{self.base_url}/chat/completions"

        try:
            response = await client.post(url, headers=self._headers(), json=payload)
        except httpx.TimeoutException as exc:
            raise ProviderError(
                f"{self.name} 请求超时（{self.timeout_s:.0f}s）", model=self.model
            ) from exc
        except httpx.HTTPError as exc:
            raise ProviderError(f"{self.name} 网络错误: {exc}", model=self.model) from exc

        if response.status_code >= 400:
            raise ProviderError(
                f"{self.name} 返回 HTTP {response.status_code}: {response.text[:400]}",
                model=self.model,
            )

        try:
            body = response.json()
        except json.JSONDecodeError as exc:
            raise ProviderError(f"{self.name} 返回非 JSON: {response.text[:200]}") from exc

        return self._parse_response(body)

    # ------------------------------------------------------------------ #
    # 解析
    # ------------------------------------------------------------------ #
    def _parse_response(self, body: dict[str, Any]) -> LLMResponse:
        """把 OpenAI 形状的响应体转成 :class:`LLMResponse`。"""
        choices = body.get("choices") or []
        if not choices:
            raise ProviderError(f"{self.name} 返回体缺少 choices", body_keys=list(body)[:10])

        choice = choices[0]
        message = choice.get("message") or {}

        content = message.get("content") or ""
        if isinstance(content, list):
            # 少数网关会返回分段 content，这里拼回纯文本。
            content = "".join(
                part.get("text", "") for part in content if isinstance(part, dict)
            )

        tool_calls: list[ToolCallRequest] = []
        for index, raw_call in enumerate(message.get("tool_calls") or []):
            function = raw_call.get("function") or {}
            raw_args = function.get("arguments") or "{}"
            try:
                arguments = json.loads(raw_args) if isinstance(raw_args, str) else dict(raw_args)
            except json.JSONDecodeError:
                logger.warning("工具参数字符串不是合法 JSON: %r", raw_args[:200])
                arguments = {}
            tool_calls.append(
                ToolCallRequest(
                    id=raw_call.get("id") or f"call_{index}",
                    name=function.get("name", ""),
                    arguments=arguments if isinstance(arguments, dict) else {},
                    raw_arguments=raw_args if isinstance(raw_args, str) else json.dumps(raw_args),
                )
            )

        raw_usage = body.get("usage") or {}
        usage = Usage(
            prompt_tokens=int(raw_usage.get("prompt_tokens", 0) or 0),
            completion_tokens=int(raw_usage.get("completion_tokens", 0) or 0),
            total_tokens=int(raw_usage.get("total_tokens", 0) or 0),
            # 上下文缓存命中量：DeepSeek 用 prompt_cache_hit_tokens，
            # OpenAI 用 prompt_tokens_details.cached_tokens，两个都认。
            cache_hit_tokens=int(
                raw_usage.get("prompt_cache_hit_tokens")
                or (raw_usage.get("prompt_tokens_details") or {}).get("cached_tokens")
                or 0
            ),
            cache_miss_tokens=int(raw_usage.get("prompt_cache_miss_tokens") or 0),
        )

        return LLMResponse(
            content=content,
            tool_calls=tool_calls,
            usage=usage,
            model=body.get("model", self.model),
            finish_reason=choice.get("finish_reason", "") or "",
            raw=body,
        )
