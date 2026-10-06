"""测试用的 HTTP/WebSocket 服务引导。

``SparkBotRuntime`` 只负责装配，真正**监听端口**的是 uvicorn。
因此纯运行时的测试无法接受设备连接——本模块补上这一层：

* 在后台任务里启动 uvicorn；
* 支持 ``port=0`` 让操作系统分配空闲端口，彻底避免测试间端口冲突；
* 提供 ``httpx`` 异步客户端直接打真实 HTTP 接口。

这样测试既覆盖了协议与 Agent 逻辑，也覆盖了真实的 HTTP/WS 路由。
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import socket
from dataclasses import dataclass
from typing import Any

logger = logging.getLogger(__name__)


@dataclass
class ServedApp:
    """一个已启动的服务实例。"""

    port: int
    server: Any
    task: asyncio.Task[None]

    @property
    def base_url(self) -> str:
        """HTTP 基地址。"""
        return f"http://127.0.0.1:{self.port}"

    def ws_url(self, path: str = "/robot") -> str:
        """设备接入的 WebSocket 地址。"""
        return f"ws://127.0.0.1:{self.port}{path}"

    async def close(self) -> None:
        """停止服务并等待端口释放。"""
        self.server.should_exit = True
        with contextlib.suppress(asyncio.CancelledError, Exception):
            await asyncio.wait_for(self.task, timeout=10.0)


def _free_port() -> int:
    """向操作系统要一个空闲端口（用于不支持 port=0 的场景）。"""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


async def serve_app(app: Any, *, host: str = "127.0.0.1", port: int = 0) -> ServedApp:
    """在后台启动 uvicorn 并返回可用的服务句柄。

    Args:
        app: FastAPI 应用实例。
        host: 监听地址。
        port: 监听端口；``0`` 表示自动分配。

    Raises:
        RuntimeError: uvicorn 未能在超时内完成启动。
    """
    import uvicorn

    requested = port or _free_port()
    config = uvicorn.Config(
        app,
        host=host,
        port=requested,
        log_level="warning",
        lifespan="on",
        access_log=False,
    )
    server = uvicorn.Server(config)
    task = asyncio.create_task(server.serve(), name="test-uvicorn")

    # 等待 started 标志，而不是盲等固定时长——既快又稳。
    for _ in range(300):  # 最多 30 秒
        if server.started:
            break
        if task.done():
            exc = task.exception()
            raise RuntimeError(f"uvicorn 启动失败: {exc}") from exc
        await asyncio.sleep(0.1)
    else:
        server.should_exit = True
        raise RuntimeError("uvicorn 启动超时")

    # 以真实绑定端口为准（配置里写了具体端口时两者一致）。
    actual = requested
    for sock in getattr(server, "servers", []) or []:
        for family_socket in getattr(sock, "sockets", []) or []:
            with contextlib.suppress(OSError, AttributeError):
                actual = int(family_socket.getsockname()[1])
    logger.info("测试服务已启动: http://%s:%d", host, actual)
    return ServedApp(port=actual, server=server, task=task)


@contextlib.asynccontextmanager
async def running_service(settings: Any, **kwargs: Any):
    """异步上下文管理器：启动服务、给出句柄、退出时自动关闭。"""
    from ..app import create_app

    app = create_app(settings)
    served = await serve_app(app, **kwargs)
    try:
        yield served
    finally:
        await served.close()
