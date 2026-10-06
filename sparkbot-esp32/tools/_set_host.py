"""把 sdkconfig 里的服务器地址改成指定值（用于验证自动发现）。

用法::

    python tools/_set_host.py 10.99.99.99
    python tools/_set_host.py 192.168.0.103

为什么需要这个脚本：PowerShell 会把命令行里的引号吃掉，直接
`python -c "..."` 写正则很容易踩坑（引号丢失导致 SyntaxError）。
写成文件就没有这个问题。
"""

from __future__ import annotations

import io
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SDKCONFIG = ROOT / "sdkconfig"
PATTERN = 'CONFIG_SPARKBOT_SERVER_HOST="[^"]*"'


def main() -> int:
    """改写 sdkconfig 里的主机地址。"""
    if len(sys.argv) < 2:
        print("用法: python tools/_set_host.py <IP>")
        return 1
    host = sys.argv[1]

    if not SDKCONFIG.exists():
        print(f"找不到 {SDKCONFIG}")
        return 1

    text = io.open(SDKCONFIG, encoding="utf-8").read()
    found = re.findall(PATTERN, text)
    if not found:
        print("sdkconfig 里没有 CONFIG_SPARKBOT_SERVER_HOST")
        return 1

    print(f"  改前: {found[0]}")
    new_line = f'CONFIG_SPARKBOT_SERVER_HOST="{host}"'
    text = re.sub(PATTERN, new_line, text)
    io.open(SDKCONFIG, "w", encoding="utf-8", newline="\n").write(text)
    print(f"  改后: {new_line}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
