"""临时诊断：观察设备 uptime 是否重置（判断是否重启）。

用法::

    python tests/_probe_uptime.py [次数] [间隔秒]
"""

from __future__ import annotations

import sys
import time

sys.path.insert(0, r"D:\dsh\sparkbot\.vendor")

import httpx  # noqa: E402

BASE = "http://127.0.0.1:8765"


def main() -> int:
    """轮询设备 uptime。"""
    times = int(sys.argv[1]) if len(sys.argv) > 1 else 6
    gap = float(sys.argv[2]) if len(sys.argv) > 2 else 4.0

    client = httpx.Client(timeout=20)
    prev_uptime: float | None = None
    resets = 0

    for i in range(times):
        try:
            data = client.get(f"{BASE}/api/devices").json()
        except Exception as exc:  # noqa: BLE001
            print(f"  +{i * gap:4.1f}s  请求失败: {type(exc).__name__}")
            time.sleep(gap)
            continue

        if not data.get("count"):
            print(f"  +{i * gap:4.1f}s  设备离线")
            time.sleep(gap)
            continue

        dev = data["devices"][0]
        tel = dev.get("telemetry") or {}
        up = float(tel.get("uptime_ms", 0)) / 1000.0
        ago = time.time() - dev["last_seen"]

        reset_mark = ""
        if prev_uptime is not None and up < prev_uptime - 1.0:
            resets += 1
            reset_mark = "   <== uptime 变小，设备重启了！"
        prev_uptime = up

        print(f"  +{i * gap:4.1f}s  uptime={up:7.1f}s  通信={ago:5.1f}s前{reset_mark}")
        time.sleep(gap)

    print()
    print(f"  重启次数: {resets}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
