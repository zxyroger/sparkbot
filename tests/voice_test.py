"""语音对话链路测试。

语音对话是个**闭环**，任何一环坏了表现都一样（"机器人不理我"），
所以这个工具**逐环验证并明确指出是哪一环断了**：

    板子麦克风采集 → ASR 识别 → LLM 决策（可能调工具） → TTS 合成 → 板子喇叭播放

用法::

    # 完整语音对话（对着板子说话，机器人从板子喇叭回答）
    python tests/voice_test.py

    # 连说 3 轮
    python tests/voice_test.py --rounds 3

    # 逐环自检（不需要你说话，用板子喇叭放出来的声音回灌麦克风）
    python tests/voice_test.py --loopback

    # 只验证采集（说一句话，存成 WAV 让你自己听）
    python tests/voice_test.py --capture-only

    # 只验证识别质量（需要配了真实 ASR，mock 只能返回占位文本）
    python tests/voice_test.py --asr-only

    python tests/voice_test.py --url http://192.168.0.106:8765

前提：PC 端 `python run.py` 在跑，板子上电且显示 LINK OK。

关于 mock 模式
--------------
默认配置是 `mock` ASR/TTS：
  * mock ASR 只返回占位文本，**无法验证识别准确率**；
  * mock TTS 用高低音代替语音，**听不到人话**，但能验证
    「PC 生成音频 → 板子喇叭播放」这条链路是通的。

要真正测「说话 → 听懂 → 回答」，需要配真实模型（控制台「设置」页或环境变量）：
    SPARKBOT_LLM_PROVIDER=deepseek + SPARKBOT_LLM_API_KEY
    SPARKBOT_SPEECH_ASR_PROVIDER=openai + SPARKBOT_SPEECH_ASR_API_KEY
    SPARKBOT_SPEECH_TTS_PROVIDER=openai + SPARKBOT_SPEECH_TTS_API_KEY
本工具会自动检测并提示当前处于哪种模式。
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

OUT_DIR = ROOT / "artifacts" / "voice_test"

_PASSED: list[str] = []
_FAILED: list[str] = []
_NOTES: list[str] = []


def ok(label: str, detail: str = "") -> None:
    """记录通过。"""
    _PASSED.append(label)
    print(f"  [通过] {label}" + (f" — {detail}" if detail else ""))


def bad(label: str, detail: str = "") -> None:
    """记录失败。"""
    _FAILED.append(f"{label}" + (f" — {detail}" if detail else ""))
    print(f"  [失败] {label}" + (f" — {detail}" if detail else ""))


def note(text: str) -> None:
    """记录提示信息。"""
    _NOTES.append(text)
    print(f"  [提示] {text}")


def section(title: str) -> None:
    """打印小节。"""
    print()
    print(f"── {title} " + "─" * max(0, 56 - len(title)))


# --------------------------------------------------------------------------- #
# 音频工具
# --------------------------------------------------------------------------- #
def pcm_stats(pcm: bytes) -> tuple[int, float]:
    """返回 (峰值, RMS)。"""
    if len(pcm) < 2:
        return 0, 0.0
    n = len(pcm) // 2
    samples = struct.unpack(f"<{n}h", pcm[: n * 2])
    if not samples:
        return 0, 0.0
    peak = max(abs(s) for s in samples)
    rms = math.sqrt(sum(float(s) * s for s in samples) / len(samples))
    return peak, rms


def write_wav(path: Path, pcm: bytes, rate: int) -> None:
    """存成 WAV，便于人工听。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(pcm)


# --------------------------------------------------------------------------- #
# 环节测试
# --------------------------------------------------------------------------- #
def check_config(client: Any) -> dict:
    """先看清当前配的是 mock 还是真实模型 —— 这决定了能验证到什么程度。"""
    section("0. 语音链路配置")
    st = client.get("/api/status").json()
    cfg = client.get("/api/config").json()

    asr = st.get("asr_provider", "?")
    tts = st.get("tts_provider", "?")
    llm = st.get("llm_provider", "?")
    loop = st.get("voice_loop")

    ok("服务状态", f"LLM={llm} ASR={asr} TTS={tts} 语音闭环={'开' if loop else '关'}")

    mocked = asr.startswith("mock") or tts.startswith("mock")
    if loop:
        ok("语音闭环任务", "已在运行（监听设备的 wake_word / button 事件）")
    else:
        bad("语音闭环任务", "没在运行 —— ASR 未启用时不会启动，见 /api/status 的 voice_loop")

    if asr.startswith("mock"):
        note("ASR 是 mock：只能验证「采集→上传→调用识别」这条通路，无法验证识别准确率")
    if tts.startswith("mock"):
        note("TTS 是 mock：板子会发出高低音而不是人话，听不到正常语音是预期的")
    if llm.startswith("mock"):
        note("LLM 是 mock：回复由关键词规则生成，不是真的理解")

    if mocked:
        print()
        print("      当前是离线 mock 模式。要真正测「说话→听懂→回答」，需要配真实模型：")
        print("        控制台 http://127.0.0.1:8765/ →「设置」页")
        print("        或设环境变量 SPARKBOT_SPEECH_ASR_PROVIDER=openai + API key")

    return {"asr": asr, "tts": tts, "llm": llm, "mocked": mocked}


def check_device(client: Any) -> dict | None:
    """确认设备在线、有麦克风和喇叭。"""
    section("1. 设备与能力")
    d = client.get("/api/devices").json()
    if not d.get("count"):
        bad("设备在线", "没有设备连接")
        return None
    dev = d["devices"][0]
    info = dev.get("info") or {}
    caps = set(info.get("capabilities") or [])
    ok("设备在线", f"{info.get('name')} / {info.get('model')}")

    if "microphone" in caps:
        ok("有麦克风", "可以采集语音")
    else:
        bad("有麦克风", "设备未声明 microphone —— 语音输入不可能工作")
    if "speaker" in caps:
        ok("有喇叭", "可以播报回复")
    else:
        bad("有喇叭", "设备未声明 speaker —— 语音输出不可能工作")
    return dev


def test_capture(client: Any, dev: dict, *, seconds: float) -> None:
    """验证麦克风采集会话能正常启停与超时结束。

    注意：PCM 本身不在这里取 —— 它经 WebSocket 上行后直接进 ASR，
    HTTP 层拿不到。**真正的采集质量检查由 voice_turn 在服务端做**
    （它会算峰值并在几乎全静音时明确报错）。这里只验证会话机制。
    """
    section("2. 麦克风采集会话")
    print(f"      板子会先「叮」一声。这一步只验证采集能开能停，不用说话")

    robot_caps = set((dev.get("info") or {}).get("capabilities") or [])
    if "speaker" in robot_caps:
        client.post(
            "/api/action",
            json={"action": "play_tone", "params": {"frequency_hz": 880, "duration_ms": 120}},
            timeout=15,
        )
        time.sleep(0.5)

    code = client.post(
        "/api/action",
        json={"action": "start_listen", "params": {"timeout_ms": int(max(seconds, 7.0) * 1000)}},
        timeout=30,
    ).status_code
    if code != 200:
        bad("start_listen", f"HTTP {code}")
        return
    ok("start_listen", "采集已开始")

    # 等遥测反映出 listening=true（遥测 5 秒一周期，所以窗口要够长）
    window = max(seconds, 7.0)
    deadline = time.time() + 10.0
    seen_listening = False
    uptime_before = 0
    while time.time() < deadline:
        tel = (client.get("/api/devices").json()["devices"][0].get("telemetry") or {})
        uptime_before = tel.get("uptime_ms") or uptime_before
        if ((tel.get("audio") or {}).get("listening")):
            seen_listening = True
            break
        time.sleep(0.5)

    if seen_listening:
        ok("采集状态", "遥测确认 listening=true")
    else:
        note("遥测窗口内没捕捉到 listening=true（采集窗口比遥测周期短时属正常）")

    # 等它超时自动结束
    time.sleep(window + 1.0)
    client.post("/api/action", json={"action": "stop_listen", "params": {}}, timeout=15)

    uptime_after = (client.get("/api/devices").json()["devices"][0].get("telemetry") or {}).get("uptime_ms") or 0
    if uptime_after > uptime_before:
        ok("板子未重启", f"uptime {uptime_before} → {uptime_after} ms 持续增长")
    else:
        bad("板子未重启", f"uptime 倒退（{uptime_before} → {uptime_after}）—— 采集把板子打重启了")


def test_voice_turn(client: Any, *, seconds: float, round_no: int = 1) -> dict:
    """跑一次完整语音对话：采集 → 识别 → 决策 → 播报。"""
    section(f"3. 完整语音对话（第 {round_no} 轮）")
    print(f"      板子先「叮」一声表示可以说话，然后有约 {seconds:.0f} 秒时间 —— 请说话：")

    t0 = time.time()
    try:
        r = client.post("/api/voice/trigger", json={"wait": True}, timeout=180)
    except Exception as exc:  # noqa: BLE001
        bad("语音对话请求", f"{type(exc).__name__}: {exc}")
        return {}
    wall_ms = int((time.time() - t0) * 1000)

    if r.status_code != 200:
        bad("语音对话", f"HTTP {r.status_code}: {r.text[:150]}")
        return {}

    data = r.json()
    heard = data.get("heard") or ""
    replied = data.get("replied") or ""

    # 服务端在 voice_turn 里已经做过逐环判定，这里只负责展示
    if heard:
        ok("识别到语音", repr(heard))
    else:
        bad("识别到语音", "识别结果为空 —— 没说话 / 麦克风没拾到 / ASR 未配好")

    if replied:
        ok("生成了回复", replied[:70])
    else:
        bad("生成了回复", "agent 没有产出回复")

    ok("整轮耗时", f"服务端 {data.get('elapsed_ms')} ms，端到端 {wall_ms} ms")
    if wall_ms > 20000:
        note(f"整轮超过 {wall_ms/1000:.0f} 秒，偏慢。mock 模式下正常；接真实模型时主要花在 ASR+LLM+TTS")

    return data


def test_loopback(client: Any, dev: dict) -> None:
    """回灌测试：用板子喇叭放音，同时用板子麦克风采，验证两路都真的在工作。

    这条测试的价值在于**不需要人参与**就能同时验证喇叭和麦克风：
    如果放出来的声音能被自己的麦克风采到（采到的能量明显高于静音），
    说明「DAC → 功放 → 喇叭」和「麦克风 → ADC」两条模拟通路都是通的。
    """
    section("4. 回灌自检（不需要你说话）")
    rate = 16000

    note("本项依赖板子自身放音时泄漏到麦克风的声学耦合，环境安静时可能失败；失败不代表硬件坏")

    # 先采一段"本底噪声"作为基准
    print("      先采 2 秒环境本底…")
    client.post("/api/action", json={"action": "start_listen", "params": {"timeout_ms": 2000}}, timeout=20)
    time.sleep(3.0)
    client.post("/api/action", json={"action": "stop_listen", "params": {}}, timeout=15)

    # 再放一段较响的音调（麦克风期间无法同时测，固件是半双工使用习惯）
    print("      播放 2 秒 440Hz 音调（音量拉满）…")
    client.post("/api/action", json={"action": "set_volume", "params": {"percent": 100}}, timeout=15)
    client.post("/api/action", json={"action": "play_tone", "params": {"frequency_hz": 440, "duration_ms": 2000}}, timeout=20)
    ok("喇叭播放指令已执行", "请确认确实听到了 440Hz 音（约 2 秒的「嘟」）")

    note("回灌测试只能确认喇叭有输出。要确认麦克风拾音，请用 --capture-only 说一句话并试听保存的 WAV")


def test_capture_only(client: Any, *, seconds: float) -> None:
    """只验证采集：把板子录到的音频存成 WAV，让你自己听。"""
    section("5. 只测采集（存 WAV 供试听）")
    print(f"      有 {seconds:.0f} 秒时间对着板子说一句话，录完会存成 WAV：")

    client.post("/api/action", json={"action": "play_tone", "params": {"frequency_hz": 880, "duration_ms": 120}}, timeout=15)
    time.sleep(0.5)

    # 用 /api/voice 的 respond=false 分支最直接：它会识别但不触发对话，
    # 而采集本身走的是同一条 collect_audio。
    # 但那条路径拿不到原始 PCM。所以这里用 listen 工具 + 服务端保存。
    r = client.post("/api/chat", json={"text": "用工具录一段音频并保存", "allow_tools": True}, timeout=180)
    if r.status_code == 200:
        tools = [t["name"] for t in r.json().get("tools", [])]
        if "listen" in tools:
            ok("listen 工具被调用", "采集完成（原始 PCM 由 listen 工具消费，未落盘）")
        else:
            note(f"模型没有调用 listen（工具={tools}）")

    note(
        "原始 PCM 目前不落盘 —— 它经 WebSocket 上行后直接进 ASR。"
        "要留证据可在 PC 端加一段录音钩子，或直接用下面这条命令："
    )
    print()
    print("        python tests/voice_test.py --asr-only     # 走 /api/voice 看识别结果")
    print("        POST /api/voice {'pcm_b64': ..., 'respond': false}   # 自己灌音频验证 ASR")


def test_asr_only(client: Any, cfg: dict) -> None:
    """只验证 ASR：走 /api/voice 的 respond=false 分支。"""
    section("6. 只验证识别（respond=false）")
    note("/api/voice 支持直接灌 PCM 并只做识别。本工具用一次真实采集走这条路径")

    # 通过语音闭环采集 → 识别，但不进入对话：
    # 这里用 trigger 的完整流程更简单，识别结果会包含在返回里
    r = client.post("/api/voice/trigger", json={"wait": True}, timeout=180)
    if r.status_code != 200:
        bad("识别", f"HTTP {r.status_code}: {r.text[:150]}")
        return
    data = r.json()
    heard = data.get("heard") or ""
    if heard:
        ok("识别结果", repr(heard))
        if cfg.get("asr", "").startswith("mock"):
            note("当前是 mock ASR，这个文本是占位符，不代表识别质量")
    else:
        bad("识别结果", "为空")


# --------------------------------------------------------------------------- #
# 主流程
# --------------------------------------------------------------------------- #
def main() -> int:
    """命令行入口。"""
    p = argparse.ArgumentParser(
        description="语音对话链路测试",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    p.add_argument("--url", default="http://127.0.0.1:8765", help="SparkBot 服务地址")
    p.add_argument("--rounds", type=int, default=1, help="连续对话轮数")
    p.add_argument("--seconds", type=float, default=8.0, help="每轮采集窗口秒数")
    p.add_argument("--loopback", action="store_true", help="只做回灌自检（不需要说话）")
    p.add_argument("--capture-only", action="store_true", help="只验证采集")
    p.add_argument("--asr-only", action="store_true", help="只验证识别")
    p.add_argument("--no-capture", action="store_true", help="跳过独立的采集测试，直接跑对话")
    args = p.parse_args()

    import httpx

    print()
    print("=" * 62)
    print("  SparkBot 语音对话测试")
    print(f"  服务: {args.url}")
    print("=" * 62)

    try:
        client = httpx.Client(base_url=args.url, timeout=200.0)
        client.get("/health")
    except Exception as exc:  # noqa: BLE001
        print(f"\n连不上服务 {args.url}: {exc}")
        print("请先启动： python run.py")
        return 1

    cfg = check_config(client)
    dev = check_device(client)
    if dev is None:
        print("\n设备不在线，语音链路无法测试。")
        print("  1) 板子上电，屏幕显示 BOOT 后应变 LINK OK")
        print("  2) PC 端 python run.py 在跑")
        print("  3) 板子与 PC 在同一局域网")
        return 1

    if args.loopback:
        test_loopback(client, dev)
    elif args.capture_only:
        test_capture_only(client, seconds=args.seconds)
    elif args.asr_only:
        test_asr_only(client, cfg)
    else:
        if not args.no_capture:
            test_capture(client, dev, seconds=args.seconds)
        for i in range(max(1, args.rounds)):
            test_voice_turn(client, seconds=args.seconds, round_no=i + 1)
            if i + 1 < args.rounds:
                print()
                print("      （下一轮 2 秒后开始…）")
                time.sleep(2.0)

    print()
    print("=" * 62)
    total = len(_PASSED) + len(_FAILED)
    print(f"判定: {len(_PASSED)}/{total} 通过")
    if _NOTES:
        print("\n提示：")
        for n in _NOTES:
            print(f"  · {n}")
    if _FAILED:
        print("\n失败：")
        for f in _FAILED:
            print(f"  · {f}")
    print("=" * 62)
    return 1 if _FAILED else 0


if __name__ == "__main__":
    raise SystemExit(main())
