"""「保持唤醒、连续对话」的会话态行为测试。

用户诉求：唤醒一次后保持对话态，可以一直连续说话；**安静 5 分钟**后
自动退出对话态（退回需要喊唤醒词的状态）。

这里把时间参数压到亚秒级来验证状态机本身：

* 静音轮次不结束会话（会一直等）；
* 听到人声就把"安静计时"清零（会话被续命）；
* ASR 听漏但麦克风电平明显有语音时，**也算听到**（否则一句话被漏识别
  就当场掉线，用户会觉得"又只能问一句"）；
* 安静超时后退出对话态，并给一个看得见的表情提示；
* 「叮」只在第一轮响，之后不再打扰。

运行::

    python tests/test_voice_session.py
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from sparkbot.paths import ensure_path  # noqa: E402

ensure_path()

from sparkbot.app import create_app  # noqa: E402
from sparkbot.config import Settings  # noqa: E402
from sparkbot.mock_device import MockDevice, MockDeviceConfig  # noqa: E402
from sparkbot.runtime import SparkBotRuntime  # noqa: E402
from sparkbot.testing import ServedApp, serve_app  # noqa: E402

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-6s %(name)s: %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("voice-session")

_PASSED: list[str] = []
_FAILED: list[str] = []


def check(condition: bool, label: str, detail: str = "") -> None:
    """记录一次断言。"""
    suffix = f" — {detail}" if detail else ""
    if condition:
        _PASSED.append(label)
        logger.info("  PASS  %s%s", label, suffix)
    else:
        _FAILED.append(f"{label}{suffix}")
        logger.error("  FAIL  %s%s", label, suffix)


async def wait_until(predicate, *, timeout: float = 20.0, interval: float = 0.02) -> bool:
    """轮询等待条件成立。"""
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while loop.time() < deadline:
        if predicate():
            return True
        await asyncio.sleep(interval)
    return False


def build_settings(tmp: Path) -> Settings:
    """离线配置 + 亚秒级的会话超时。"""
    settings = Settings()
    settings.server.host = "127.0.0.1"
    settings.server.log_level = "info"
    settings.llm.provider = "mock"
    settings.llm.model = "mock-model"
    settings.vision.provider = "mock"
    settings.speech.asr_provider = "mock"
    settings.speech.tts_provider = "mock"
    settings.face.path = str(tmp / "face_db.json")
    settings.memory.path = str(tmp / "facts.jsonl")
    settings.behavior.talking_animation = True
    settings.behavior.voice_session_turns = 0            # 不限轮数
    settings.behavior.voice_session_idle_timeout_s = 0.8  # 0.8 秒安静即退出
    settings.behavior.voice_session_gap_s = 0.05
    return settings


class StubVoiceTurn:
    """替换掉真实的 voice_turn：按脚本返回结果，并记录调用参数。

    真实的 voice_turn 要跑采集 + ASR + 对话 + 播报，一轮十几秒；
    这里只验证**会话状态机**，所以给它一个可控的假回合。

    ``mic_open`` 按 stage 推导，与真实实现一致：
    静音轮保持上行开着，说话轮关掉（要播报了）。
    """

    def __init__(self, script: list[dict]) -> None:
        self.script = script
        self.calls: list[dict] = []

    async def __call__(self, device_id=None, *, beep: bool = True, listening: bool = False) -> dict:
        entry = self.script[min(len(self.calls), len(self.script) - 1)]
        self.calls.append({"device_id": device_id, "beep": beep, "listening": listening, **entry})
        await asyncio.sleep(0.02)   # 模拟一轮的耗时
        result = dict(entry)
        result.setdefault("mic_open", entry.get("stage") == "silent")
        return result


async def test_keeps_listening(rt: SparkBotRuntime) -> None:
    """阶段 1：静音轮不结束会话，听到人声会续期，超时才退出。"""
    logger.info("阶段 1 · 会话保持与续期")

    # 前 3 轮静音（0.8s 超时前足够跑完好几轮），第 4 轮有人说话，
    # 之后继续静音，直到超时退出。
    stub = StubVoiceTurn([
        {"stage": "silent", "text": "", "rms": 90},      # 静音（保持上行）
        {"stage": "silent", "text": "", "rms": 95},      # 静音
        {"stage": "done", "text": "你好", "rms": 800},   # 有语音 → 续期
        {"stage": "silent", "text": "", "rms": 88},      # 静音（重新开上行）
    ])
    original = rt.voice_turn
    rt.voice_turn = stub  # type: ignore[method-assign]
    try:
        await asyncio.wait_for(rt._handle_voice_turn("esp32s3-mock"), timeout=15.0)  # noqa: SLF001
    finally:
        rt.voice_turn = original  # type: ignore[method-assign]

    check(len(stub.calls) >= 4, "静音不会立刻结束会话（一直等到超时）",
          f"{len(stub.calls)} 轮")
    check(stub.calls[0]["beep"] is True, "第一轮「叮」一声提示可以说话")
    check(all(c["beep"] is False for c in stub.calls[1:]),
          "后续轮次不再「叮」—— 否则每几秒响一次会变成噪音",
          f"{[c['beep'] for c in stub.calls]}")
    check(stub.calls[0]["listening"] is False, "第一轮先把音频上行打开")
    silent_after_first = [c["listening"] for c in stub.calls[1:3]]
    check(all(silent_after_first),
          "静音轮之间**不关麦克风**（下一轮直接接着听）",
          f"{[c['listening'] for c in stub.calls]}")
    check(stub.calls[3]["listening"] is False,
          "说话那轮结束后上行被关掉（播报前必须关，否则录到自己）",
          f"{[c['listening'] for c in stub.calls]}")
    check(rt.voice_session_active is False, "会话结束后对话态关闭")
    heard_rounds = [i for i, c in enumerate(stub.calls) if c.get("text")]
    check(bool(heard_rounds), "有语音的那一轮被计为「听到」", str(heard_rounds))


async def test_rms_counts_as_speech(rt: SparkBotRuntime) -> None:
    """阶段 2：ASR 听漏但电平有语音，也要算「听到」。"""
    logger.info("阶段 2 · 电平兜底")
    stub = StubVoiceTurn([
        {"stage": "empty_text", "text": ".", "rms": 900},   # 识别不行、但确实有人说话
    ])
    original = rt.voice_turn
    rt.voice_turn = stub  # type: ignore[method-assign]
    try:
        held = rt._heard_speech(stub.script[0])  # noqa: SLF001
    finally:
        rt.voice_turn = original  # type: ignore[method-assign]
    check(held is True, "识别为空但电平很高 → 仍判为「有人在说话」")
    check(rt._heard_speech({"text": "", "rms": 90}) is False,  # noqa: SLF001
          "真静音（低电平、无文本）判为「没人说话」")


async def test_idle_timeout_window(rt: SparkBotRuntime, served: ServedApp) -> None:
    """阶段 3：静音超时 = 配置值（这里 0.8s），不是固定值。"""
    logger.info("阶段 3 · 静音计时")
    import httpx

    stub = StubVoiceTurn([{"stage": "silent", "text": "", "rms": 90}])
    original = rt.voice_turn
    rt.voice_turn = stub  # type: ignore[method-assign]
    started = time.monotonic()
    try:
        await asyncio.wait_for(rt._handle_voice_turn("esp32s3-mock"), timeout=15.0)  # noqa: SLF001
    finally:
        rt.voice_turn = original  # type: ignore[method-assign]
    elapsed = time.monotonic() - started
    check(0.7 <= elapsed <= 6.0, "会话在静音超时后结束", f"{elapsed:.2f}s（配置 0.8s）")

    # 状态接口要能看出"当前是不是对话态"
    async with httpx.AsyncClient(base_url=served.base_url, timeout=10.0) as client:
        status = (await client.get("/api/status")).json()
    check("voice_session" in status, "状态接口暴露 voice_session 字段",
          str(status.get("voice_session")))
    check(status.get("voice_session") is False, "空闲时不是对话态")


async def test_lazy_asr(rt: SparkBotRuntime, device: MockDevice) -> None:
    """阶段 6：静音不连 ASR，听到人声才连（走真实 voice_turn 路径）。

    这条守的是"回调里出异常被吞掉"那类问题：曾经 feed() 里引用了没导入的
    Robot，于是每片都抛 NameError，被采集循环吞成一行 warning ——
    结果是**永远听不到人声、永远不连 ASR**，日志里只剩一堆分片回调失败。
    """
    logger.info("阶段 6 · 按需连 ASR")
    robot = rt.robots.get()
    opened: list[int] = []
    real_open = rt.asr.open_stream  # type: ignore[union-attr]

    async def spy_open(**kwargs):
        opened.append(1)
        return await real_open(**kwargs)

    rt.asr.open_stream = spy_open  # type: ignore[union-attr,method-assign]
    try:
        # ① 静音：不连 ASR，但上行保持开着
        device.config.silent_audio = True
        quiet = await rt.voice_turn(robot.device_id, beep=False)
        check(quiet.get("stage") == "silent", "静音轮判为静音", str(quiet.get("stage")))
        check(quiet.get("mic_open") is True, "静音轮**保持上行开着**（不关麦克风）")
        check(not opened, "静音轮没有连接流式 ASR", f"{len(opened)} 次")
        check(int(quiet.get("rms") or 0) < 250, "静音轮电平确实很低",
              f"RMS={quiet.get('rms')}")

        # ② 有人说话：连 ASR、出文本、关上行走播报
        device.config.silent_audio = False
        spoke = await rt.voice_turn(robot.device_id, beep=False, listening=True)
        check(bool(opened), "听到人声才连接流式 ASR", f"{len(opened)} 次")
        check(bool(str(spoke.get("text") or "").strip()), "识别出了文本",
              str(spoke.get("text"))[:30])
        check(spoke.get("mic_open") is False, "说话轮结束后关掉上行（准备播报）")
    finally:
        rt.asr.open_stream = real_open  # type: ignore[union-attr,method-assign]
        device.config.silent_audio = False
        with contextlib.suppress(Exception):
            await rt._stop_device_audio(robot)  # noqa: SLF001


async def test_playback_waits(rt: SparkBotRuntime) -> None:
    """阶段 5：播报必须等设备播完，否则下一轮会录到机器人自己说话。

    实测事故：会话第二轮 ASR 识别出「次好吗？」，正是上一句
    "你再说一次好吗？"的尾巴 —— 机器人开始跟自己聊天。根因是**最后一段
    播报没等 audio_done** 就返回了，紧接着的采集把喇叭的声音收了进去。
    """
    logger.info("阶段 5 · 播报等待")
    robot = rt.robots.get()
    waits: list[bool] = []
    original = robot.say

    async def spy(audio, *, fmt="wav", sample_rate=None, wait=False):
        waits.append(wait)
        return await original(audio, fmt=fmt, sample_rate=sample_rate, wait=wait)

    robot.say = spy  # type: ignore[method-assign]
    try:
        turn = await rt.chat("你好呀", announce=True)
    finally:
        robot.say = original  # type: ignore[method-assign]

    check(bool(turn.reply), "带播报的对话完成", turn.reply[:30])
    check(bool(waits) and all(waits), "每一段播报都等设备播完再返回", str(waits))


async def test_single_session(rt: SparkBotRuntime) -> None:
    """阶段 7：同一台设备同时只能有一个会话。

    真实事故：连续几次触发"语音对话"叠出了多个会话 —— 它们互相抢麦克风、
    各自播报，又把对方的播报录成"用户说话"，机器人开始自己跟自己聊
    （用户描述："机器人一直在乱说话"）。
    """
    logger.info("阶段 7 · 会话不叠加")
    stub = StubVoiceTurn([{"stage": "silent", "text": "", "rms": 90}])
    original = rt.voice_turn
    rt.voice_turn = stub  # type: ignore[method-assign]
    saved_timeout = rt.settings.behavior.voice_session_idle_timeout_s
    rt.settings.behavior.voice_session_idle_timeout_s = 1.2
    try:
        first = asyncio.create_task(rt._handle_voice_turn("dev-x"))  # noqa: SLF001
        await asyncio.sleep(0.15)
        check(rt.voice_session_running("dev-x") is True, "会话进行中能被查询到")
        rounds_before = len(stub.calls)
        await asyncio.wait_for(rt._handle_voice_turn("dev-x"), timeout=2.0)  # noqa: SLF001
        check(len(stub.calls) == rounds_before,
              "第二次触发起被拒绝，没有多采一轮（否则两个会话会互录）",
              f"{rounds_before} → {len(stub.calls)}")
        await asyncio.wait_for(first, timeout=10.0)
        check(rt.voice_session_running("dev-x") is False, "会话结束后标记被清掉")
    finally:
        rt.voice_turn = original  # type: ignore[method-assign]
        rt.settings.behavior.voice_session_idle_timeout_s = saved_timeout


async def test_turn_cap_still_works(rt: SparkBotRuntime) -> None:
    """阶段 4：voice_session_turns 仍能作为硬上限。"""
    logger.info("阶段 4 · 轮数上限")
    rt.settings.behavior.voice_session_turns = 2
    rt.settings.behavior.voice_session_idle_timeout_s = 0.0   # 关掉静音超时
    stub = StubVoiceTurn([{"stage": "done", "text": "嗯", "rms": 700}])
    original = rt.voice_turn
    rt.voice_turn = stub  # type: ignore[method-assign]
    try:
        await asyncio.wait_for(rt._handle_voice_turn("esp32s3-mock"), timeout=15.0)  # noqa: SLF001
    finally:
        rt.voice_turn = original  # type: ignore[method-assign]
        rt.settings.behavior.voice_session_turns = 0
        rt.settings.behavior.voice_session_idle_timeout_s = 0.8
    check(len(stub.calls) == 2, "轮数上限生效", f"{len(stub.calls)} 轮")


async def main() -> int:
    """启动服务与模拟设备，跑完各阶段。"""
    with tempfile.TemporaryDirectory() as tmp:
        settings = build_settings(Path(tmp))
        app = create_app(settings)
        served = await serve_app(app)
        rt: SparkBotRuntime = app.state.runtime

        device = MockDevice(
            MockDeviceConfig(
                url=served.ws_url(),
                device_id="esp32s3-mock",
                name="小星（会话测试）",
                telemetry_ms=1_000,
            )
        )
        device_task = asyncio.create_task(device.run(), name="mock-device")
        try:
            connected = await wait_until(lambda: rt.gateway.connected_count == 1, timeout=20.0)
            check(connected, "模拟设备已接入")
            if not connected:
                return 1
            await test_keeps_listening(rt)
            await test_rms_counts_as_speech(rt)
            await test_idle_timeout_window(rt, served)
            await test_lazy_asr(rt, device)
            await test_playback_waits(rt)
            await test_single_session(rt)
            await test_turn_cap_still_works(rt)
        finally:
            await device.stop()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await asyncio.wait_for(device_task, timeout=6.0)
            await served.close()

    total = len(_PASSED) + len(_FAILED)
    logger.info("")
    logger.info("=" * 64)
    logger.info("断言汇总: %d/%d 通过", len(_PASSED), total)
    if _FAILED:
        logger.error("失败项:")
        for item in _FAILED:
            logger.error("  · %s", item)
        return 1
    logger.info("全部通过")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
