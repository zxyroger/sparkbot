"""一键测试唤醒词「Hi,小星」。

用法（**在你自己的终端里跑**，不要和我同时读串口）::

    C:\\Espressif\\python_env\\idf5.5_py3.13_env\\Scripts\\python.exe tools\\wake_test.py COM15 25

参数：``<串口> <监听秒数>``

它会：
  1. 打开串口（**只读，不干预 DTR/RTS**）
  2. 通过 PC 服务接口让设备进入采集（唤醒检测只在采集期间生效）
  3. 一边读串口一边等你说话
  4. 结束时给出明确结论：**是否检测到唤醒词**，以及没检测到时是哪一环断了

为什么要有这个脚本：
  * 手动"开采集 + 读串口"要两个步骤配合，很容易错过窗口；
  * 该固件平时静默，只有事件才打印，不看提示会以为串口坏了；
  * **串口同一时刻只能被一个进程打开** —— 如果别的工具（含我之前
    遗留的读取进程）还占着，会直接报
    "拒绝访问"，看起来也像"串口没打印"。本脚本会明确报出这种情况。
"""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def read_serial_into(port: str, seconds: float, sink: list[str], stop_flag: list[bool]) -> None:
    """在后台线程里持续读串口，把内容追加到 sink。"""
    import serial

    sp = serial.Serial()
    sp.port = port
    sp.baudrate = 115200
    sp.timeout = 0.2
    sp.dtr = False  # 关键：不干预这两根线，否则会把板子按进复位
    sp.rts = False
    sp.open()
    try:
        deadline = time.time() + seconds
        while time.time() < deadline and not stop_flag[0]:
            data = sp.read(4096)
            if data:
                sink.append(data.decode("utf-8", errors="replace"))
    finally:
        sp.close()


def main() -> int:
    """命令行入口。"""
    p = argparse.ArgumentParser(description="一键测试唤醒词 Hi,小星")
    p.add_argument("port", help="串口，例如 COM15")
    p.add_argument("seconds", nargs="?", type=float, default=25.0, help="监听秒数")
    p.add_argument("--url", default="http://127.0.0.1:8765", help="PC 服务地址")
    args = p.parse_args()

    try:
        import serial  # noqa: F401
    except ImportError:
        print("需要 pyserial，请用 IDF 的 python 环境运行本脚本")
        return 2

    try:
        import httpx
    except ImportError:
        sys.path.insert(0, str(ROOT.parent / "sparkbot" / ".vendor"))
        import httpx  # type: ignore

    import threading

    print()
    print("=" * 62)
    print("  唤醒词测试： Hi,小星")
    print("=" * 62)

    # 1) 先确认设备在线
    try:
        c = httpx.Client(base_url=args.url, timeout=20)
        d = c.get("/api/devices").json()
        if not d.get("count"):
            print("设备不在线 —— 先确认板子已连上 PC（屏幕显示 LINK OK）")
            return 1
        dev = d["devices"][0]
        caps = (dev.get("info") or {}).get("capabilities") or []
        print(f"设备在线: {dev['device_id']}  能力: {', '.join(caps)}")
    except Exception as exc:  # noqa: BLE001
        print(f"连不上 PC 服务 {args.url}: {exc}")
        print("请先启动： cd D:\\dsh\\sparkbot && python run.py")
        return 1

    # 2) 开串口（先开，避免错过开头的提示）
    sink: list[str] = []
    stop_flag = [False]
    t = threading.Thread(target=read_serial_into,
                         args=(args.port, args.seconds + 3, sink, stop_flag), daemon=True)
    try:
        t.start()
        time.sleep(1.0)  # 给线程一点时间真正打开串口
    except Exception as exc:  # noqa: BLE001
        print(f"打不开串口 {args.port}: {exc}")
        print("最常见原因：**别的进程还占着串口**。请先关掉其它串口工具，")
        print("或执行 taskkill /F /IM esp32gw.exe")
        return 3

    # 3) 让设备进入采集（唤醒检测只在采集期间生效）
    print()
    print(">>> 现在开始对着板子（20~30cm）清晰地说：「Hi 小星」")
    print(">>> 可以多说几遍。")
    print()
    try:
        r = c.post("/api/action",
                   json={"action": "start_listen", "params": {"timeout_ms": int(args.seconds * 1000)}},
                   timeout=30)
        print(f"已让设备进入采集: HTTP {r.status_code}")
    except Exception as exc:  # noqa: BLE001
        print(f"下发 start_listen 失败: {exc}")

    # 4) 边读边显示
    shown = 0
    deadline = time.time() + args.seconds
    while time.time() < deadline:
        time.sleep(0.5)
        text = "".join(sink)
        lines = [ln for ln in text.splitlines() if ln.strip()]
        while shown < len(lines):
            print("   " + lines[shown].strip())
            shown += 1

    stop_flag[0] = True
    time.sleep(0.5)

    try:
        c.post("/api/action", json={"action": "stop_listen", "params": {}}, timeout=20)
    except Exception:  # noqa: BLE001
        pass

    # 5) 结论
    text = "".join(sink)
    print()
    print("=" * 62)
    if "唤醒词命中" in text:
        print("  ✅ 检测到唤醒词！")
        print("=" * 62)
        print()
        print("  接下来确认语音闭环：PC 控制台应自动开始一轮对话")
        print("  （浏览器打开 http://127.0.0.1:8765/ 看「👂 听到」）")
        return 0

    print("  ❌ 没有检测到唤醒词")
    print("=" * 62)
    print()
    print("  按下面几条自查（对应不同的原因）：")
    if "开始采集，唤醒检测已启用" not in text:
        print("  1. 串口里没有「开始采集，唤醒检测已启用」——")
        print("     说明设备没进入采集，或串口没读到数据。")
        print("     请确认没有别的程序占着串口（尤其 esp32gw.exe）。")
    if "AFE 进度" not in text:
        print("  2. 串口里没有「AFE 进度」——")
        print("     说明音频没喂进 AFE，请把完整串口输出发我看。")
    else:
        print("  3. 有「AFE 进度」但没命中，通常是这几类：")
        print("     · 距离太远 / 环境太吵 → 靠近到 20cm、安静环境再试")
        print("     · 发音不对 → 是「Hi 小星」（Hi 在前），不是「你好小星」")
        print("     · 麦克风增益偏低 → 可调 CONFIG_SPARKBOT_AUDIO_MIC_GAIN_DB")
        print("     · 检测阈值偏严 → 模型是 DET_MODE_90，可试着放宽")
        print()
        print("     请把上面打印的「AFE 进度」几行发我，我据此调阈值/增益。")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
