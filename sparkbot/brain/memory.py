"""会话记忆：带条数上限的对话历史。

刻意做得很薄——一台桌面机器人的对话不需要向量数据库。
保留「最近 N 条 + 当前设备状态」就足以支撑连续对话，
而这恰好也是唯一在 ESP32 这种资源受限场景下真正有用的上下文形式。

记忆策略：
* system prompt 永远在最前，且不占历史配额；
* 超出上限时**成对驱逐**（一次丢弃一组 assistant+tool），
  避免留下孤立的 ``tool`` 消息——很多 API 会因此直接报错。
"""

from __future__ import annotations

import logging
from collections import deque
from typing import Any, Iterable

from ..llm.base import ChatMessage, MessageRole

logger = logging.getLogger(__name__)


class Memory:
    """一台机器人的对话记忆。"""

    def __init__(self, *, system_prompt: str, limit: int = 40) -> None:
        self._system = ChatMessage.system(system_prompt)
        self._limit = max(4, limit)
        self._turns: deque[ChatMessage] = deque(maxlen=self._limit)

    # ------------------------------------------------------------------ #
    # 读写
    # ------------------------------------------------------------------ #
    @property
    def system_prompt(self) -> str:
        """当前 system prompt。"""
        return self._system.content

    def set_system_prompt(self, prompt: str) -> None:
        """替换 system prompt（例如设备上线后补上能力说明）。"""
        self._system = ChatMessage.system(prompt)

    def append(self, message: ChatMessage) -> None:
        """追加一条消息。"""
        if message.role_value == MessageRole.SYSTEM.value:
            self.set_system_prompt(message.content)
            return
        self._turns.append(message)

    def extend(self, messages: Iterable[ChatMessage]) -> None:
        """批量追加。"""
        for message in messages:
            self.append(message)

    def messages(self) -> list[ChatMessage]:
        """返回可直接送进模型的完整消息数组。"""
        return [self._system, *self._turns]

    def snapshot(self) -> list[dict[str, Any]]:
        """导出可 JSON 序列化的历史（图片只留数量）。"""
        return [self._system.to_dict(), *(m.to_dict() for m in self._turns)]

    def last_user_text(self) -> str:
        """最近一条用户消息的文本，用于日志与情绪判断。"""
        for message in reversed(self._turns):
            if message.role_value == MessageRole.USER.value:
                return message.content
        return ""

    def clear(self) -> None:
        """清空历史，但保留 system prompt。"""
        self._turns.clear()

    def trim_for_retry(self) -> None:
        """在重试前丢掉最后一条 assistant/tool 组，避免带着半截状态重发。"""
        while self._turns and self._turns[-1].role_value in (
            MessageRole.TOOL.value,
            MessageRole.ASSISTANT.value,
        ):
            self._turns.pop()

    # ------------------------------------------------------------------ #
    # 诊断
    # ------------------------------------------------------------------ #
    @property
    def turn_count(self) -> int:
        """当前保留的消息条数。"""
        return len(self._turns)

    @property
    def limit(self) -> int:
        """历史条数上限。"""
        return self._limit

    def __len__(self) -> int:
        return len(self._turns)
