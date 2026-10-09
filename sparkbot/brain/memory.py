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
        """返回可直接送进模型的完整消息数组。

        注意这里过了一道 :meth:`_sanitized`：交给模型的历史必须**工具调用
        严格配对**，否则会被 API 直接拒掉（见该方法的说明）。
        """
        return [self._system, *self._sanitized()]

    def _sanitized(self) -> list[ChatMessage]:
        """把历史整理成「tool_calls ↔ tool 回复」严格配对的序列。

        为什么必须有这一步：``_turns`` 是定长 deque，超出上限时它**一次只丢
        最老的一条**。只要丢掉的正好是「带 tool_calls 的 assistant 消息」，
        它的 tool 回复就变成了孤儿，OpenAI / DeepSeek 会直接回 400：

            Messages with role 'tool' must be a response to a preceding
            message with 'tool_calls'

        实测表现极具误导性：唤醒 → 说话 → 机器人答「我的大脑有点连不上」，
        看上去像"唤醒坏了 / 麦克风坏了"，其实是历史里躺着一条孤儿消息。
        反向的问题同样会被拒：assistant(tool_calls) 后面没有 tool 回复
        （工具执行中途抛异常时会留下这种半截状态）。

        所以这里按「组」重建：配不上的整组丢掉。宁可少一轮上下文，
        也不能让模型调用失败 —— 失败一次，用户听到的就是"机器人坏了"。
        """
        out: list[ChatMessage] = []
        index = 0
        total = len(self._turns)

        while index < total:
            message = self._turns[index]
            role = message.role_value

            # ① 孤儿 tool 回复：它的 assistant(tool_calls) 已被裁掉 → 丢掉
            if role == MessageRole.TOOL.value:
                index += 1
                continue

            # ② assistant 的 tool_calls：连同紧随其后的 tool 回复一起看
            if role == MessageRole.ASSISTANT.value and message.tool_calls:
                wanted = {call.id for call in message.tool_calls}
                group = [message]
                cursor = index + 1
                while cursor < total and self._turns[cursor].role_value == MessageRole.TOOL.value:
                    group.append(self._turns[cursor])
                    cursor += 1
                answered = {m.tool_call_id for m in group[1:]}
                if wanted <= answered:
                    out.extend(group)
                else:
                    logger.debug(
                        "会话历史里有一组未配对的 tool_calls（缺 %d 条回复），整组跳过",
                        len(wanted - answered),
                    )
                index = cursor
                continue

            out.append(message)
            index += 1

        return out

    def snapshot(self) -> list[dict[str, Any]]:
        """导出可 JSON 序列化的历史（图片只留数量）。

        这里刻意用**原始**历史而不是 :meth:`_sanitized` 的结果：
        排查"模型为什么拒答"时，需要看到历史里真实躺着什么，
        而不是被清理过的版本。
        """
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
