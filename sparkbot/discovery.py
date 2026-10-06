"""服务器自动发现：回应 ESP32 的 UDP 广播探测。

背景
----
板子和 PC 都在 DHCP 网段里，PC 的 IP 会变（实测从 ``.192.168.0.103``
变成 ``.192.168.0.106``）。固件里写死的地址一旦失效，设备就连不上，
而改地址要重新编译烧录 —— 调试期非常痛苦。

做法
----
板子往 ``<广播地址>:<port>`` 发一个 UDP 包::

    SPARKBOT-DISCOVER-V1

本模块监听同一个 UDP 端口，收到后**原路回一个**::

    SPARKBOT-HERE <ip>

板子用**应答的源地址**作为服务器地址（源地址是网络层事实，比应答体
里的字符串更可信）。

设计取舍
--------
* **不用 mDNS**：要引入额外组件，且 Windows 上支持不总是可靠；
* **不用固定 IP / 静态 DHCP 绑定**：需要改路由器，部署不友好；
* UDP 单包一问一答，PC 端无依赖、无状态。

失败很安全：板子发现不到就回退到编译时配置的地址，行为与改动前一致。
"""

from __future__ import annotations

import asyncio
import logging
import socket

logger = logging.getLogger(__name__)

#: 探测包的 magic。板端按这个字符串匹配，避免把无关广播当成探测。
DISCOVER_MAGIC = b"SPARKBOT-DISCOVER-V1"

#: 应答前缀。板端也用这个做校验。
REPLY_PREFIX = b"SPARKBOT-HERE "


class DiscoveryResponder:
    """监听 UDP 广播探测并应答本机 IP。"""

    def __init__(self, port: int, host: str = "0.0.0.0") -> None:
        self.port = port
        self.host = host
        self._transport: asyncio.DatagramTransport | None = None
        self._protocol: _ResponderProtocol | None = None
        self.received = 0
        self.replied = 0
        self.errors = 0

    async def start(self) -> bool:
        """绑定端口并开始应答。

        返回是否成功。**失败不算致命**：端口被占（例如开了两个实例）
        或系统不允许绑定 UDP 时，服务其余部分仍应正常工作，只是设备
        无法自动发现、需要走编译时配置的地址。
        """
        loop = asyncio.get_running_loop()
        try:
            transport, protocol = await loop.create_datagram_endpoint(
                lambda: _ResponderProtocol(self),
                local_addr=(self.host, self.port),
                # 关键：允许把包发到广播地址，且允许地址复用，
                # 否则同一台机器上重启服务会因 TIME_WAIT 绑不上。
                reuse_port=False,
                allow_broadcast=True,
            )
        except OSError as exc:
            logger.warning(
                "自动发现服务未能监听 UDP %s:%d（%s）。"
                "设备将回退到固件里配置的 IP，功能不受影响。",
                self.host,
                self.port,
                exc,
            )
            self.errors += 1
            return False

        self._transport = transport
        self._protocol = protocol  # type: ignore[assignment]
        logger.info("自动发现已就绪：UDP %s:%d（回应 SPARKBOT 探测）", self.host, self.port)
        return True

    async def stop(self) -> None:
        """关闭监听。"""
        if self._transport is not None:
            self._transport.close()
            self._transport = None
            self._protocol = None


class _ResponderProtocol(asyncio.DatagramProtocol):
    """收到探测就回一个带本机 IP 的应答。"""

    def __init__(self, owner: DiscoveryResponder) -> None:
        self.owner = owner
        self.transport: asyncio.DatagramTransport | None = None

    def connection_made(self, transport: asyncio.BaseTransport) -> None:
        self.transport = transport  # type: ignore[assignment]

    def datagram_received(self, data: bytes, addr: tuple[str, int]) -> None:
        """处理一个探测包。"""
        self.owner.received += 1
        if data.strip() != DISCOVER_MAGIC:
            # 不是我们的探测包：安静忽略。局域网上各种广播都有。
            logger.debug("忽略非 SparkBot 探测包（来自 %s，%d 字节）", addr[0], len(data))
            return

        if self.transport is None:
            return

        # 应答里带上本机 IP；板端会优先用源地址，这个字符串只是冗余信息，
        # 便于串口日志里直接看出服务器是谁。
        ip = _local_ip_for(addr[0])
        reply = REPLY_PREFIX + ip.encode("utf-8")
        try:
            self.transport.sendto(reply, addr)
        except OSError as exc:
            self.owner.errors += 1
            logger.warning("回应发现请求失败（%s -> %s）：%s", ip, addr[0], exc)
            return

        self.owner.replied += 1
        logger.info("已回应设备发现请求：%s -> %s", addr[0], ip)


def _local_ip_for(peer: str) -> str:
    """取本机面向 ``peer`` 的出口 IP。

    用 UDP connect 的技巧：connect 一个 UDP socket 不会真的发包，
    但内核会据此填好本地地址。这样在多网卡（有线/无线/虚拟网卡）
    时能拿到**真正能到达设备**的那个地址 —— 直接返回
    ``gethostbyname(gethostname())`` 在虚拟网卡存在时经常拿到错误的那个。
    """
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        sock.connect((peer, 9))  # 9 = discard 端口，不会真的通信
        return sock.getsockname()[0]
    except OSError:
        return "0.0.0.0"
    finally:
        sock.close()
