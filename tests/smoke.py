"""手动联调脚本：对着一个**正在运行**的 SparkBot 服务发指令。

与 ``tests/`` 下的自动化测试不同，本脚本不启动服务、不启动模拟设备，
它只是像人一样操作 HTTP 接口——所以既适用于模拟设备，也适用于真板子。

用法::

    # 终端 1：启动服务
    python run.py --provider mock

    # 终端 2：启动模拟设备（或换成真 ESP32-S3）
    python -m sparkbot.mock_device

    # 终端 3：跑联调
    python tests/smoke.py                        # 全部检查
    python tests/smoke.py --chat "你前面有什么"    # 只聊一句
    python tests/smoke.py --action drive --params '{"linear":0.2,"duration_ms":500}'
    python tests/smoke.py --url http://127.0.0.1:8765
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from sparkbot.paths import ensure_path  # noqa: E402

ensure_path()


async def api(client: Any, method: str, path: str, **kwargs: Any) -> dict[str, Any]:
    """调用一个 HTTP 接口并返回 JSON，失败时抛出带正文的异常。"""
    response = await client.request(method, path, **kwargs)
    try:
        payload = response.json()
    except ValueError:
        payload = {"raw": response.text[:200]}
    if response.status_code >= 400:
        raise RuntimeError(f"{method} {path} -> HTTP {response.status_code}: {payload}")
    return payload


async def run_checks(base_url: str) -> int:
    """按顺序跑一遍全链路检查。"""
    import httpx

    failures: list[str] = []

    def report(label: str, ok: bool, detail: str = "") -> None:
        """打印一行检查结果。"""
        mark = "OK  " if ok else "FAIL"
        print(f"  [{mark}] {label}" + (f" — {detail}" if detail else ""))
        if not ok:
            failures.append(label)

    async with httpx.AsyncClient(base_url=base_url, timeout=60.0) as client:
        print("\n1) 服务健康")
        health = await api(client, "GET", "/health")
        report("健康检查", health.get("ok") is True, str(health.get("version")))

        print("\n2) 设备连接")
        devices = await api(client, "GET", "/api/devices")
        count = devices.get("count", 0)
        report("至少一台设备在线", count > 0, f"{count} 台")
        if count == 0:
            print("\n没有设备在线。请先在另一个终端启动模拟设备：")
            print(f"    python -m sparkbot.mock_device --url ws://127.0.0.1:{_port(base_url)}/robot")
            return 1

        info = devices["devices"][0]
        caps = (info.get("info") or {}).get("capabilities") or []
        report("设备上报了能力", bool(caps), ", ".join(caps))

        # 遥测是按周期上报的，设备刚上线时可能还没收到第一条，因此轮询等待。
        battery: dict[str, Any] = {}
        for _ in range(20):
            battery = ((info.get("telemetry") or {}).get("battery")) or {}
            if battery:
                break
            await asyncio.sleep(0.5)
            devices = await api(client, "GET", "/api/devices")
            info = devices["devices"][0]
        report("收到遥测", bool(battery), str(battery))

        print("\n3) 工具注册")
        tools = await api(client, "GET", "/api/tools")
        names = {t["name"] for t in tools.get("tools", [])}
        report("工具数量合理", len(names) >= 10, f"{len(names)} 个")

        print("\n4) 表情与显示")
        face = await api(client, "POST", "/api/face", json={"emotion": "happy"})
        report("切换表情", face.get("emotion") == "happy", str(face.get("emotion")))

        print("\n5) 视觉抓帧")
        shot = await api(client, "POST", "/api/action",
                         json={"action": "snapshot", "params": {"width": 320, "height": 240}})
        report("抓帧成功", shot.get("ok") is True, str(shot.get("result")))

        print("\n6) 对话（含工具调用）")
        turn = await api(client, "POST", "/api/chat", json={"text": "你前面有什么东西？"})
        used = [t["name"] for t in turn.get("tools", [])]
        report("获得了回复", bool(turn.get("reply")), str(turn.get("reply"))[:60])
        report("调用了工具", bool(used), ", ".join(used))
        print(f"        token 用量: {turn.get('usage')}")

        print("\n7) 底盘动作")
        drive = await api(client, "POST", "/api/action",
                          json={"action": "drive",
                                "params": {"linear": 0.2, "angular": 0.0, "duration_ms": 400}})
        report("下发运动指令", drive.get("ok") is True)
        stop = await api(client, "POST", "/api/stop")
        report("急停生效", stop.get("ok") is True, str(stop.get("stopped")))

    print()
    if failures:
        print(f"失败 {len(failures)} 项: {', '.join(failures)}")
        return 1
    print("全部检查通过")
    return 0


def _port(base_url: str) -> int:
    """从 base_url 里取出端口号。"""
    try:
        return int(base_url.rstrip("/").split(":")[-1])
    except ValueError:
        return 8765


async def main() -> int:
    """命令行入口。"""
    parser = argparse.ArgumentParser(description="SparkBot 手动联调")
    parser.add_argument("--url", default="http://127.0.0.1:8765", help="服务地址")
    parser.add_argument("--chat", help="只发一句话并打印完整结果")
    parser.add_argument("--action", help="只下发一个动作")
    parser.add_argument("--params", default="{}", help="动作参数 JSON")
    parser.add_argument("--face", help="只设置一个表情")
    parser.add_argument("--announce", action="store_true", help="让机器人把回复读出来")
    args = parser.parse_args()

    import httpx

    async with httpx.AsyncClient(base_url=args.url, timeout=120.0) as client:
        if args.chat:
            payload = await api(client, "POST", "/api/chat",
                                json={"text": args.chat, "announce": args.announce})
            print(json.dumps(payload, ensure_ascii=False, indent=2))
            return 0

        if args.action:
            params = json.loads(args.params)
            payload = await api(client, "POST", "/api/action",
                                json={"action": args.action, "params": params})
            print(json.dumps(payload, ensure_ascii=False, indent=2))
            return 0

        if args.face:
            payload = await api(client, "POST", "/api/face", json={"emotion": args.face})
            print(json.dumps(payload, ensure_ascii=False, indent=2))
            return 0

    return await run_checks(args.url)


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
