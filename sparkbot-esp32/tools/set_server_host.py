"""把固件里的 PC IP 改掉并重新生成 sdkconfig。

背景：``CONFIG_SPARKBOT_SERVER_HOST`` 是硬编码的 PC IP。PC 走 DHCP，
路由器重新分配地址后（例如 .106 -> .103）设备就再也连不上，串口里会一直
刷 ``连接失败: 104``（ECONNRESET）。改这个值比重烧整份配置快得多。

用法::

    python tools/set_server_host.py 192.168.0.103
"""

from __future__ import annotations

import io
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SDKCONFIG = ROOT / "sdkconfig"


def main() -> int:
    """替换 sdkconfig 里的服务器地址。"""
    if len(sys.argv) < 2:
        print("用法: python tools/set_server_host.py <PC的IP>")
        return 2

    host = sys.argv[1].strip()
    if not re.fullmatch(r"\d{1,3}(\.\d{1,3}){3}", host):
        print(f"看起来不是合法 IPv4 地址: {host!r}")
        return 2

    if not SDKCONFIG.exists():
        print(f"找不到 {SDKCONFIG}，请先构建一次固件（build.bat build）")
        return 1

    src = io.open(SDKCONFIG, encoding="utf-8").read()
    pattern = re.compile(r'CONFIG_SPARKBOT_SERVER_HOST="[^"]*"')
    new, n = pattern.subn(f'CONFIG_SPARKBOT_SERVER_HOST="{host}"', src)
    if n == 0:
        print("sdkconfig 里没有 CONFIG_SPARKBOT_SERVER_HOST，请检查配置项是否存在")
        return 1

    io.open(SDKCONFIG, "w", encoding="utf-8", newline="\n").write(new)
    print(f"已更新 {n} 处: CONFIG_SPARKBOT_SERVER_HOST=\"{host}\"")
    print("接下来: build.bat build  &&  build.bat flash COM15")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
