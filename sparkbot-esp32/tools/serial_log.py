"""被动读 ESP32-S3 串口日志（默认不干预 DTR/RTS，不会复位板子）。

用法::

    C:\\Espressif\\python_env\\idf5.5_py3.13_env\\Scripts\\python.exe tools\\serial_log.py COM15 20
    C:\\Espressif\\python_env\\idf5.5_py3.13_env\\Scripts\\python.exe tools\\serial_log.py COM15 20 --reset

参数：``<端口> <读取秒数> [--reset]``

``--reset`` 会先通过 DTR/RTS 复位一次，用于抓**完整启动日志**。

为什么必须用 pyserial 而不是 PowerShell 的 SerialPort：
    PowerShell 打开串口时会翻转 DTR/RTS，而 ESP32-S3 的原生 USB 串口
    用这两根线做复位/下载模式控制 —— 结果就是"一读日志板子就重启"。
    pyserial 可以明确把 dtr/rts 设为 False，保持板子正常运行。

为什么要内置 ``--reset``：
    用 esptool 单独复位、再用另一个进程读串口时，两个进程会抢串口，
    实测经常丢掉整个启动段（只能读到 0~700 字节）。
    把复位和读取放在**同一个进程**里、中间不释放串口，就不会丢。
"""

from __future__ import annotations

import sys
import time


def reset_via_dtr(port: str, baud: int = 115200) -> None:
    """通过 DTR/RTS 复位一次，用于抓完整启动日志。

    ESP32-S3 的复位时序：RTS 拉低 → 松开 → 再复位一次。
    之后立刻打开串口读取，即可看到从第一行开始的启动日志。
    """
    import serial

    sp = serial.Serial()
    sp.port = port
    sp.baudrate = baud
    sp.dtr = False
    sp.rts = False
    sp.open()
    time.sleep(0.1)

    # 复位时序（与 esptool 的硬复位等价）
    sp.rts = True
    time.sleep(0.1)
    sp.rts = False
    time.sleep(0.05)
    sp.close()


def main() -> int:
    """读指定秒数的串口日志并打印。"""
    if len(sys.argv) < 2:
        print(__doc__)
        return 1

    port = sys.argv[1]
    seconds = float(sys.argv[2]) if len(sys.argv) > 2 else 15.0
    do_reset = "--reset" in sys.argv

    try:
        import serial
    except ImportError:
        print("需要 pyserial。请用 IDF 的 python 环境跑：")
        print(r"  C:\Espressif\python_env\idf5.5_py3.13_env\Scripts\python.exe tools\serial_log.py COM15 20")
        return 2

    if do_reset:
        print(f"# 先复位 {port}（DTR/RTS）…", file=sys.stderr)
        try:
            reset_via_dtr(port)
        except Exception as exc:  # noqa: BLE001
            print(f"# 复位失败（继续尝试读取）: {exc}", file=sys.stderr)

    sp = serial.Serial()
    sp.port = port
    sp.baudrate = 115200
    sp.timeout = 0.2
    # 关键：这两行都不能少。设为 False 才不会触发复位/下载模式。
    sp.dtr = False
    sp.rts = False
    try:
        sp.open()
    except Exception as exc:  # noqa: BLE001
        # 最常见的原因是**别的进程还占着串口**（例如上一次 Ctrl+C 没退干净、
        # 或者同项目里另有个串口工具在跑）。把可能占用者提示出来，
        # 否则用户只会看到一句"拒绝访问"，无从下手。
        print(f"# 打不开 {port}: {exc}", file=sys.stderr)
        print("# 常见原因：另一个程序占着串口。可先执行：", file=sys.stderr)
        print(f"#   taskkill /F /IM esp32gw.exe        (项目自带的网关工具)", file=sys.stderr)
        print(f"#   或关掉其它串口助手 / 上一次没退干净的 script", file=sys.stderr)
        return 3

    print(f"# 正在被动读取 {port} {seconds:.0f} 秒（不干预握手线）…", file=sys.stderr)
    if do_reset:
        print("# 注意：先复位再读，能抓到完整启动日志", file=sys.stderr)

    deadline = time.time() + seconds
    total = 0
    try:
        while time.time() < deadline:
            data = sp.read(4096)
            if data:
                total += len(data)
                # 替换非法字符，尽量保住可读内容
                sys.stdout.write(data.decode("utf-8", errors="replace"))
                sys.stdout.flush()
    except KeyboardInterrupt:
        pass
    finally:
        sp.close()

    # 明确报告"读到了多少"，让"串口没输出"和"工具没打开"能区分开
    print(f"\n# 读取结束：共收到 {total} 字节", file=sys.stderr)
    if total == 0:
        print("# 一个字节都没收到。这说明**设备当前没有往串口打印**，"
              "而不是工具没打开。", file=sys.stderr)
        print("# 该固件只在有事件时打印；采集期间每 200 帧才输出一行进度。",
              file=sys.stderr)
        print("# 想确认串口通不通，可以加 --reset 抓启动日志（启动时必然有输出）。",
              file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
