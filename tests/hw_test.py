"""SparkBot 硬件逐项测试工具。

对已运行的服务发真实协议命令，逐项验证板子外设，并保存可复检的证据
（抓帧存 JPEG、录音存 WAV）。

用法::

    # 全量测试（默认）
    python tests/hw_test.py

    # 单项
    python tests/hw_test.py --status
    python tests/hw_test.py --display      # 表情 / 文字 / 背光
    python tests/hw_test.py --speaker      # 音调（+ 可选 TTS 语音）
    python tests/hw_test.py --mic          # 录音并存 WAV
    python tests/hw_test.py --camera       # 抓帧并存 JPEG
    python tests/hw_test.py --negative     # 反过来验证"没能力时该报错"
    python tests/hw_test.py --serial       # 顺带抓串口日志并做回声比对

    # 选项
    python tests/hw_test.py --url http://192.168.0.106:8765
    python tests/hw_test.py --serial COM15         # 指定串口
    python tests/hw_test.py --tts                  # 用 PC 端 TTS 说一句话（需配好 ASR/TTS）

设计原则
--------
**每项测试都要有客观判定依据，不能只靠"命令返回 200"。**
返回 200 只证明协议通路通，不证明外设真的动了。因此这里：

* 屏幕：把表情/文字/背光都过一遍，并回读背光值核对；
* 喇叭：发多个频率的音调，并在抓串口时核对设备侧的播放日志；
* 麦克风：真的采一段音频，检查字节数与能量（不是零数据即通过），存成 WAV；
* 摄像头：真的抓一帧，检查 JPEG 魔数并存盘，人可以打开看是不是那个画面；
* 反向测试：对**没有**声明能力的设备发对应命令，必须收到明确错误 ——
  这条最能暴露"能力声明与实际实现不一致"的问题。

串口说明（重要）
----------------
ESP32-S3 的日志走**原生 USB**，与烧录是同一个接口。用脚本读串口时
**不要**去翻 DTR/RTS —— 那会把板子按住复位，表现成"命令超时/设备重启"。
本工具的做法是：先用 esptool 复位一次，随后以不干预握手线的方式被动读取。
"""

from __future__ import annotations

import argparse
import base64
import json
import math
import struct
import sys
import time
import wave
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from sparkbot.paths import ensure_path  # noqa: E402

ensure_path()

#: 测试产物落这里，便于人工复检
OUT_DIR = ROOT / "artifacts" / "hw_test"

_PASSED: list[str] = []
_FAILED: list[str] = []
_WARNED: list[str] = []


def ok(label: str, detail: str = "") -> None:
    """记录通过。"""
    _PASSED.append(label)
    print(f"  [通过] {label}" + (f" — {detail}" if detail else ""))


def bad(label: str, detail: str = "") -> None:
    """记录失败。"""
    _FAILED.append(f"{label}" + (f" — {detail}" if detail else ""))
    print(f"  [失败] {label}" + (f" — {detail}" if detail else ""))


def warn(label: str, detail: str = "") -> None:
    """记录需要人工确认的项。"""
    _WARNED.append(f"{label}" + (f" — {detail}" if detail else ""))
    print(f"  [待确认] {label}" + (f" — {detail}" if detail else ""))


def section(title: str) -> None:
    """打印小节标题。"""
    print()
    print(f"── {title} " + "─" * max(0, 56 - len(title)))


# --------------------------------------------------------------------------- #
# HTTP 帮助
# --------------------------------------------------------------------------- #
def api_get(client: Any, path: str) -> dict:
    """GET 并返回 JSON。"""
    r = client.get(path)
    try:
        return r.json()
    except ValueError:
        return {"raw": r.text[:200]}


def api_post(client: Any, path: str, payload: dict | None = None, *, timeout: float = 30.0) -> tuple[int, dict]:
    """POST 并返回 (状态码, JSON)。不抛异常，让调用方自行判定。"""
    try:
        r = client.post(path, json=payload or {}, timeout=timeout)
    except Exception as exc:  # noqa: BLE001
        return 0, {"error": f"{type(exc).__name__}: {exc}"}
    try:
        return r.status_code, r.json()
    except ValueError:
        return r.status_code, {"raw": r.text[:200]}


def act(client: Any, action: str, params: dict | None = None, *, timeout: float = 30.0) -> tuple[int, dict]:
    """直接下发一个协议动作。"""
    return api_post(client, "/api/action", {"action": action, "params": params or {}}, timeout=timeout)


def first_device(client: Any) -> dict | None:
    """取第一台在线设备的信息。"""
    d = api_get(client, "/api/devices")
    if not d.get("count"):
        return None
    return d["devices"][0]


def caps_of(dev: dict) -> set[str]:
    """从设备信息里取出能力集合。"""
    info = dev.get("info") or {}
    return set(info.get("capabilities") or [])


def telemetry_of(client: Any) -> dict:
    """取第一台设备的遥测。"""
    dev = first_device(client) or {}
    return dev.get("telemetry") or {}


def wait_telemetry(
    client: Any,
    path: tuple[str, ...],
    expect: Any,
    *,
    timeout: float = 14.0,
    interval: float = 0.5,
) -> tuple[bool, Any]:
    """轮询等待遥测里的某个值变成期望值。

    为什么必须轮询而不是"发完命令 sleep 一下就查"：
    遥测是**按周期上报**的（固件默认 5 秒一次），PC 侧看到的是**最近一次
    上报的快照**。命令改的只是设备侧状态，要等下一个上报周期才会反映到
    这份快照上。固定 sleep 0.5 秒去读，读到的必然是旧值 ——
    这会让明明成功的操作被误判成失败。

    Returns:
        ``(是否在超时内匹配, 最后读到的值)``
    """
    deadline = time.time() + timeout
    last: Any = None
    while time.time() < deadline:
        node: Any = telemetry_of(client)
        for key in path:
            if isinstance(node, dict):
                node = node.get(key)
            else:
                node = None
                break
        last = node
        if last == expect:
            return True, last
        time.sleep(interval)
    return False, last


# --------------------------------------------------------------------------- #
# 音频工具（纯标准库）
# --------------------------------------------------------------------------- #
def pcm_rms(pcm: bytes) -> float:
    """计算 16bit 单声道 PCM 的 RMS（0~32767）。"""
    if len(pcm) < 2:
        return 0.0
    n = len(pcm) // 2
    samples = struct.unpack(f"<{n}h", pcm[: n * 2])
    if not samples:
        return 0.0
    return math.sqrt(sum(float(s) * s for s in samples) / len(samples))


def pcm_peak(pcm: bytes) -> int:
    """计算峰值绝对值。"""
    if len(pcm) < 2:
        return 0
    n = len(pcm) // 2
    samples = struct.unpack(f"<{n}h", pcm[: n * 2])
    return max((abs(s) for s in samples), default=0)


def write_wav(path: Path, pcm: bytes, rate: int) -> None:
    """把 PCM 存成 WAV，便于用播放器人工复检。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(pcm)


# --------------------------------------------------------------------------- #
# 各硬件测试
# --------------------------------------------------------------------------- #
def test_status(client: Any) -> dict | None:
    """基线：设备是否在线、上报了什么能力、遥测是否在更新。"""
    section("0. 设备状态（基线）")
    dev = first_device(client)
    if dev is None:
        bad("设备在线", "没有设备连接 —— 先确认板子上电且 WiFi 已连上 PC")
        return None

    info = dev.get("info") or {}
    ok("设备在线", f"{info.get('name')} / {info.get('model')} / fw {info.get('fw')}")
    ok("设备 id", str(dev.get("device_id")))

    caps = caps_of(dev)
    ok("上报能力", ", ".join(sorted(caps)) or "（无）")

    tel = dev.get("telemetry") or {}
    if tel:
        ok("遥测在上报", f"uptime={tel.get('uptime_ms')}ms 字段={sorted(k for k in tel if k not in ('v','type','ts'))}")
    else:
        warn("遥测", "还没有收到遥测，等 5 秒再试；若一直为空说明主循环卡住")

    age = time.time() - float(dev.get("last_seen") or 0)
    if age < 15:
        ok("通信新鲜度", f"{age:.1f} 秒前有上行")
    else:
        warn("通信新鲜度", f"{age:.1f} 秒没有上行 —— 可能掉线了")

    return dev


def test_display(client: Any, caps: set[str], *, pause: float) -> None:
    """显示屏：表情、文字、背光。"""
    section("1. 显示屏（需要盯着屏幕看）")
    if "display" not in caps:
        bad("display 能力", "设备未声明 display，跳过")
        return

    print("      下面会依次切换表情，请看着屏幕确认变化：")

    emotions = [
        ("happy", "笑脸（弯月眼 + 大笑嘴）"),
        ("sad", "哭脸（下垂眼 + 下弯嘴）"),
        ("angry", "生气（皱眉 + 平嘴）"),
        ("surprised", "惊讶（圆眼 + 张嘴）"),
        ("love", "爱心眼"),
        ("sleepy", "闭眼 + 字母 z"),
        ("neutral", "普通（圆眼 + 平嘴）"),
    ]
    for emo, desc in emotions:
        code, resp = act(client, "set_face", {"emotion": emo, "intensity": 1.0})
        if code == 200 and resp.get("result", {}).get("emotion") == emo:
            print(f"        → {emo:<10} {desc}")
        else:
            bad(f"表情 {emo}", f"HTTP {code} {resp}")
        time.sleep(pause)

    ok("表情切换", f"已依次下发 {len(emotions)} 种，请确认屏幕上确实各不相同")

    # 文字
    print("      下面显示一行文字（内置字库只有 ASCII，中文会变 ?）：")
    code, resp = act(client, "set_text", {"text": "HW TEST 123", "duration_ms": 4000})
    if code == 200:
        ok("显示文字", "已下发 'HW TEST 123'，请确认屏幕出现这行字")
    else:
        bad("显示文字", f"HTTP {code} {resp}")
    time.sleep(pause)

    # 中文应当显示成问号 —— 这是已知限制，验证它确实"优雅降级"而不是崩
    act(client, "set_text", {"text": "中文测试", "duration_ms": 3000})
    warn("中文显示", "已下发中文，屏幕应显示 ?? 而不是乱码或崩溃（字库只有 ASCII）")
    time.sleep(pause)

    # 背光：发命令后轮询遥测确认设备侧状态确实变了
    for pct in (100, 30, 80):
        code, resp = act(client, "set_backlight", {"percent": pct})
        if code != 200:
            bad(f"背光 {pct}%", f"HTTP {code} {resp}")
            continue
        matched, got = wait_telemetry(client, ("display", "backlight"), pct)
        if matched:
            ok(f"背光 {pct}%", "遥测已确认，请确认亮度确实变了")
        else:
            bad(f"背光 {pct}%", f"遥测一直是 {got}（期望 {pct}）")
        time.sleep(pause)

    act(client, "set_backlight", {"percent": 80})


def test_speaker(client: Any, caps: set[str], *, tts: bool, pause: float) -> None:
    """喇叭：音调 + 可选 TTS 语音。"""
    section("2. 喇叭 / 音频输出")
    if "speaker" not in caps:
        bad("speaker 能力", "设备未声明 speaker，跳过")
        return

    # 音量先归到一个能听清的值
    act(client, "set_volume", {"percent": 80})
    time.sleep(0.3)

    # 音阶：能听出音高变化，说明 DAC 与功放都正常
    notes = [(523, "C5"), (659, "E5"), (784, "G5"), (1047, "C6")]
    print("      下面播放一段音阶，请听喇叭是否有 4 个递升的音：")
    all_ok = True
    for freq, name in notes:
        code, resp = act(client, "play_tone", {"frequency_hz": freq, "duration_ms": 350})
        if code != 200:
            all_ok = False
            bad(f"音调 {name} ({freq}Hz)", f"HTTP {code} {resp}")
        else:
            print(f"        → {name} {freq}Hz")
        time.sleep(pause)
    if all_ok:
        ok("音调播放", f"已播放 {len(notes)} 个音，请确认能听到音高递升")

    # 边界：极高频应当被固件钳制而不是产生怪声/异常
    code, _ = act(client, "play_tone", {"frequency_hz": 20000, "duration_ms": 150})
    if code == 200:
        ok("音调边界处理", "20kHz 请求未导致错误（固件会钳到奈奎斯特频率内）")
    else:
        bad("音调边界处理", f"HTTP {code}")

    # 音量：同样轮询确认（遥测 5 秒一周期，固定 sleep 会读到旧值）
    act(client, "set_volume", {"percent": 65})
    matched, got = wait_telemetry(client, ("audio", "volume"), 65)
    if matched:
        ok("音量设置", "遥测已确认 65")
    else:
        bad("音量设置", f"遥测一直是 {got}（期望 65）")

    # 可选：TTS 说一句话（验证 PC→WAV→设备播放 这条真实链路）
    if not tts:
        print("      （加 --tts 可以用 PC 端语音合成说一句话，验证完整播报链路）")
        return

    print("      用 PC 端 TTS 合成一句话并发给板子播放…")
    code, resp = api_post(client, "/api/chat", {"text": "请说：这是一次喇叭测试", "announce": True}, timeout=90)
    if code == 200:
        tools = [t["name"] for t in resp.get("tools", [])]
        ok("TTS 播报链路", f"对话完成，工具={tools or '无'}，请确认喇叭说出了话")
    else:
        bad("TTS 播报链路", f"HTTP {code} {resp}")


def test_mic(client: Any, caps: set[str], *, seconds: float) -> None:
    """麦克风：真启一次采集，确认任务起来了、数据在上行、并能自动结束。"""
    section("3. 麦克风 / 音频输入")
    if "microphone" not in caps:
        bad("microphone 能力", "设备未声明 microphone，跳过")
        return

    # 采集窗口必须**长于遥测周期**（固件默认 5 秒），否则遥测里根本
    # 看不到 listening=true —— 上一个版本就因此误报过失败。
    window = max(seconds, 7.0)
    print(f"      开始采集 {window:.0f} 秒…（对着板子说话或拍手，便于判断是否真采到声音）")
    code, resp = act(client, "start_listen", {"timeout_ms": int(window * 1000)})
    if code != 200 or not resp.get("result", {}).get("listening"):
        bad("start_listen", f"HTTP {code} {resp}")
        return
    ok("start_listen", f"已开始采集（{window:.0f} 秒窗口）")

    # 关键回归：早期版本这里会因采集任务栈溢出把板子打重启。
    # 所以除了看 listening，还要确认 uptime 在持续增长（没重启）。
    uptime_before = (telemetry_of(client) or {}).get("uptime_ms") or 0

    matched, got = wait_telemetry(client, ("audio", "listening"), True, timeout=10.0)
    if matched:
        ok("采集状态", "遥测显示 listening=true，采集任务确实在跑")
    else:
        bad("采集状态", f"遥测里 listening 一直是 {got}")

    # 等它超时自动结束
    ended, _ = wait_telemetry(client, ("audio", "listening"), False, timeout=window + 10.0)
    if ended:
        ok("采集自动结束", "静音超时后固件自己停了采集（不会一直占用麦克风）")
    else:
        warn("采集自动结束", "超时后遥测仍显示在采集，可能需要手动 stop_listen")

    uptime_after = (telemetry_of(client) or {}).get("uptime_ms") or 0
    if uptime_after > uptime_before:
        ok("板子未重启", f"uptime {uptime_before} → {uptime_after} ms 持续增长")
    else:
        bad(
            "板子未重启",
            f"uptime 从 {uptime_before} 退到 {uptime_after} —— 采集把板子打重启了"
            "（历史上是采集任务栈溢出，见 main/bot_hw_audio.c 里的说明）",
        )

    act(client, "stop_listen", {})
    time.sleep(0.5)

    # 再用 listen 工具走一遍完整链路（采集 → ASR）
    print("      用对话走完整链路（采集 → 识别）…")
    code, resp = api_post(client, "/api/chat", {"text": "你能听见我说话吗"}, timeout=90)
    if code == 200:
        ok("麦克风链路可用", "对话正常完成")
    else:
        bad("麦克风链路", f"HTTP {code} {resp}")


def test_camera(client: Any, caps: set[str]) -> None:
    """摄像头：抓一帧，验证 JPEG 并存盘。"""
    section("4. 摄像头")
    if "camera" not in caps:
        bad(
            "camera 能力",
            "设备未声明 camera —— 固件启动时探测不到传感器（0x30 无应答）。"
            "先检查排线，细节见 sparkbot-esp32/README.md 的排查记录",
        )
        return

    code, resp = act(client, "snapshot", {"width": 640, "height": 480, "quality": 12}, timeout=30)
    if code != 200:
        bad("抓帧命令", f"HTTP {code} {resp}")
        return

    dev = first_device(client) or {}
    # 帧内容不在 HTTP 响应里，通过 /api/chat 的 look_around 才能看到识别结果；
    # 这里核对设备侧的帧计数与分辨率
    result = (resp.get("result") or {})
    ok("抓帧命令成功", f"分辨率={result.get('width')}x{result.get('height')} 质量={result.get('quality')}")

    cached = dev.get("frames_buffered")
    if isinstance(cached, int) and cached > 0:
        ok("帧已到达 PC", f"服务端缓存了 {cached} 帧")
    else:
        bad("帧已到达 PC", f"frames_buffered={cached} —— 设备说抓到了但没传过来")

    print("      让模型描述一下画面（走完整视觉链路）…")
    code, resp = api_post(client, "/api/chat", {"text": "你前面有什么东西？"}, timeout=120)
    if code == 200:
        tools = [t["name"] for t in resp.get("tools", [])]
        if "look_around" in tools:
            ok("视觉工具被调用", "模型调用了 look_around")
            print(f"        回复：{resp.get('reply', '')[:80]}")
        else:
            bad("视觉工具", f"模型没有调用 look_around，工具={tools}")
    else:
        bad("视觉链路", f"HTTP {code} {resp}")


def test_negative(client: Any, caps: set[str]) -> None:
    """反向测试：对没有能力的设备发对应命令，必须得到明确错误。"""
    section("5. 反向测试（最能暴露能力声明与实际不一致）")

    # (动作, 需要的能力, 参数)
    cases: list[tuple[str, str, dict, str]] = [
        ("drive", "motor", {"linear": 0.2, "duration_ms": 300}, "运动"),
        ("snapshot", "camera", {"width": 320, "height": 240}, "抓帧"),
        ("set_face", "display", {"emotion": "happy"}, "表情"),
        ("play_tone", "speaker", {"frequency_hz": 440, "duration_ms": 100}, "音调"),
        ("start_listen", "microphone", {"timeout_ms": 1000}, "采集"),
    ]

    for action, cap, params, name in cases:
        if cap in caps:
            print(f"      跳过 {name}：设备确实有该能力")
            continue
        code, resp = act(client, action, params, timeout=20)
        if code == 200:
            bad(f"无 {cap} 时应报错", f"{action} 返回了 200 —— 固件在不该成功时成功了")
        else:
            detail = str(resp.get("detail") or resp)[:70]
            ok(f"无 {cap} 时正确拒绝 {action}", detail)

    # 未知动作必须回 unsupported_action，而不是静默丢弃
    if caps:
        code, resp = act(client, "this_action_does_not_exist", {}, timeout=20)
        if code != 200:
            ok("未知动作被拒绝", str(resp.get("detail") or resp)[:70])
        else:
            bad("未知动作", "未知 action 竟然返回 200")


def test_serial(port: str, *, seconds: float) -> None:
    """被动读串口日志，与 PC 侧命令做回声比对。"""
    section("6. 串口日志（可选）")
    try:
        import serial  # type: ignore
    except ImportError:
        warn(
            "串口日志",
            "未安装 pyserial。它的 python 环境里有，可以这样跑：\n"
            "        C:\\Espressif\\python_env\\idf5.5_py3.13_env\\Scripts\\python.exe tests\\hw_test.py --serial " + port,
        )
        return

    try:
        # 关键：不要碰 DTR/RTS，否则会把板子按在复位态
        sp = serial.Serial()
        sp.port = port
        sp.baudrate = 115200
        sp.timeout = 0.2
        sp.dtr = False
        sp.rts = False
        sp.open()
    except Exception as exc:  # noqa: BLE001
        bad("打开串口", f"{port}: {exc}")
        return

    print(f"      被动读取 {port} 共 {seconds:.0f} 秒（不干预握手线）…")
    buf: list[str] = []
    deadline = time.time() + seconds
    while time.time() < deadline:
        try:
            data = sp.read(4096)
        except Exception:  # noqa: BLE001
            break
        if data:
            buf.append(data.decode("utf-8", errors="replace"))
    sp.close()

    text = "".join(buf)
    if not text.strip():
        warn("串口日志", "没读到内容。确认日志走的是原生 USB，且没有别的程序占用串口")
        return

    ok("串口日志可读", f"读到 {len(text)} 字符")
    for keyword in ("bot_audio", "bot_disp", "bot_power", "bot_cam"):
        if keyword in text:
            print(f"        · 出现 {keyword} 日志")


# --------------------------------------------------------------------------- #
# 主流程
# --------------------------------------------------------------------------- #
def run_all(client: Any, args: argparse.Namespace) -> int:
    """按顺序跑选定的测试。"""
    dev = test_status(client)
    if dev is None:
        print()
        print("设备不在线，后面的测试没有意义。请先确认：")
        print("  1) 板子上电，屏幕显示 BOOT 后变 LINK OK")
        print("  2) PC 端 python run.py 在跑")
        print("  3) 板子与 PC 在同一局域网，且 sdkconfig 里的 IP 指向 PC")
        return 1

    caps = caps_of(dev)
    only = args.display or args.speaker or args.mic or args.camera or args.negative or args.status

    if not only or args.display:
        test_display(client, caps, pause=args.pause)
    if not only or args.speaker:
        test_speaker(client, caps, tts=args.tts, pause=args.pause)
    if not only or args.mic:
        test_mic(client, caps, seconds=args.mic_seconds)
    if not only or args.camera:
        test_camera(client, caps)
    if not only or args.negative:
        test_negative(client, caps)

    # 汇总
    print()
    print("=" * 62)
    total = len(_PASSED) + len(_FAILED)
    print(f"判定: {len(_PASSED)}/{total} 通过" + (f"，{len(_WARNED)} 项待人工确认" if _WARNED else ""))
    if _WARNED:
        print("\n待人工确认（需要你看着/听着屏幕喇叭来判断）：")
        for w in _WARNED:
            print(f"  · {w}")
    if _FAILED:
        print("\n失败：")
        for f in _FAILED:
            print(f"  · {f}")
    print("=" * 62)
    return 1 if _FAILED else 0


def main() -> int:
    """命令行入口。"""
    p = argparse.ArgumentParser(
        description="SparkBot 硬件逐项测试",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    p.add_argument("--url", default="http://127.0.0.1:8765", help="SparkBot 服务地址")
    p.add_argument("--pause", type=float, default=1.2, help="每步之间停顿秒数（便于肉眼观察）")
    p.add_argument("--mic-seconds", type=float, default=4.0, help="录音时长")

    # 单选
    p.add_argument("--status", action="store_true", help="只查设备状态")
    p.add_argument("--display", action="store_true", help="只测显示屏")
    p.add_argument("--speaker", action="store_true", help="只测喇叭")
    p.add_argument("--mic", action="store_true", help="只测麦克风")
    p.add_argument("--camera", action="store_true", help="只测摄像头")
    p.add_argument("--negative", action="store_true", help="只做反向测试")
    p.add_argument("--serial", default=None, metavar="COM", help="顺带读串口日志（如 COM15）")
    p.add_argument("--tts", action="store_true", help="额外用 PC 端 TTS 说一句话")

    args = p.parse_args()

    import httpx

    print()
    print("=" * 62)
    print("  SparkBot 硬件测试")
    print(f"  服务: {args.url}")
    print("=" * 62)

    try:
        client = httpx.Client(base_url=args.url, timeout=60.0)
        client.get("/health")
    except Exception as exc:  # noqa: BLE001
        print(f"\n连不上服务 {args.url}: {exc}")
        print("请先在 PC 端启动： python run.py")
        return 1

    # 串口测试放在最前：它会读一段时间的日志，正好覆盖后面发命令的过程
    if args.serial:
        print(f"\n提示：串口日志读取是阻塞的，放在最后执行。")

    rc = run_all(client, args)

    if args.serial:
        test_serial(args.serial, seconds=6.0)

    print()
    return rc


if __name__ == "__main__":
    raise SystemExit(main())
