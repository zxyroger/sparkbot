"""进程内异步事件总线。

用途：把「设备上行事件」「agent 决策」「语音链路状态」解耦广播出去，
供 Web 控制台、日志、以及后续的行为层订阅，而不让它们彼此直接依赖。
"""

from __future__ import annotations

import asyncio
import contextlib
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Any, AsyncIterator


@dataclass(slots=True)
class Event:
    """一条总线事件。"""

    topic: str
    payload: dict[str, Any] = field(default_factory=dict)
    ts: float = field(default_factory=time.time)

    def to_dict(self) -> dict[str, Any]:
        """序列化成可 JSON 化的字典（供 SSE / WebSocket 面板使用）。"""
        return {"topic": self.topic, "payload": self.payload, "ts": self.ts}


class EventBus:
    """极简发布/订阅。

    每个订阅者拿到一个独立的 ``asyncio.Queue``；队列满时**丢弃最旧**事件，
    保证慢订阅者不会拖垮发布方（UI 卡顿不该影响机器人动作）。
    """

    def __init__(self, *, history_size: int = 200) -> None:
        self._subscribers: set[asyncio.Queue[Event]] = set()
        self._history: deque[Event] = deque(maxlen=history_size)
        self._closed = False

    # ------------------------------------------------------------------ #
    # 发布
    # ------------------------------------------------------------------ #
    def publish(self, topic: str, **payload: Any) -> Event:
        """同步发布一个事件。永不阻塞、永不抛异常。"""
        event = Event(topic=topic, payload=payload)
        self._history.append(event)
        if self._closed:
            return event
        for queue in list(self._subscribers):
            try:
                queue.put_nowait(event)
            except asyncio.QueueFull:
                # 丢掉最旧的一条再塞，避免订阅者永久落后。
                with contextlib.suppress(asyncio.QueueEmpty):
                    queue.get_nowait()
                with contextlib.suppress(asyncio.QueueFull):
                    queue.put_nowait(event)
        return event

    # ------------------------------------------------------------------ #
    # 订阅
    # ------------------------------------------------------------------ #
    @contextlib.asynccontextmanager
    async def subscribe(self, *, maxsize: int = 256) -> AsyncIterator[asyncio.Queue[Event]]:
        """``async with bus.subscribe() as q:`` 取得一个独立队列。"""
        queue: asyncio.Queue[Event] = asyncio.Queue(maxsize=maxsize)
        self._subscribers.add(queue)
        try:
            yield queue
        finally:
            self._subscribers.discard(queue)

    async def stream(self, *, maxsize: int = 256) -> AsyncIterator[Event]:
        """持续产出事件，直到调用方退出循环。"""
        async with self.subscribe(maxsize=maxsize) as queue:
            while True:
                yield await queue.get()

    # ------------------------------------------------------------------ #
    # 历史与生命周期
    # ------------------------------------------------------------------ #
    def recent(self, *, limit: int = 50, topic_prefix: str | None = None) -> list[Event]:
        """读取最近事件，可选按 topic 前缀过滤。"""
        items = list(self._history)
        if topic_prefix:
            items = [e for e in items if e.topic.startswith(topic_prefix)]
        return items[-limit:]

    def close(self) -> None:
        """关闭总线，之后 :meth:`publish` 只写历史不再分发。"""
        self._closed = True
        self._subscribers.clear()

    @property
    def subscriber_count(self) -> int:
        """当前订阅者数量，用于健康检查。"""
        return len(self._subscribers)
