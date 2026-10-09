"""端到端集成测试：既不需要硬件，也不需要 API key。

覆盖范围
--------
1. 真实 HTTP/WebSocket 服务启动与协议握手、能力协商；
2. 遥测上行与设备状态同步；
3. 假模型的完整工具调用循环（看 → 移动 → 表情）；
4. 摄像头抓帧的「command/frame 配对 + base64 解码」；
5. 播报链路（TTS → play_audio → 设备端确认）；
6. 麦克风采集 + ASR + 自动回复的语音闭环；
7. ``listen`` 工具与设备唤醒事件的端到端触发；
8. 安全护栏：速度钳制、时长钳制、绕过模型的急停；
9. 能力裁剪与工具 schema 自动生成；
10. HTTP API（/api/status、/api/chat、/api/stop、/api/tools）。

运行::

    python tests/test_end_to_end.py
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from sparkbot.paths import ensure_path  # noqa: E402

ensure_path()

from sparkbot.app import create_app  # noqa: E402
from sparkbot.config import Settings  # noqa: E402
from sparkbot.device.protocol import Emotion, EventName  # noqa: E402
from sparkbot.mock_device import MockDevice, MockDeviceConfig  # noqa: E402
from sparkbot.runtime import SparkBotRuntime  # noqa: E402
from sparkbot.testing import ServedApp, serve_app  # noqa: E402

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-6s %(name)s: %(message)s",
    datefmt="%H:%M:%S",
)
logging.getLogger("httpx").setLevel(logging.WARNING)
logger = logging.getLogger("e2e")

_PASSED: list[str] = []
_FAILED: list[str] = []


def check(condition: bool, label: str, detail: str = "") -> None:
    """记录一次断言结果。"""
    suffix = f" — {detail}" if detail else ""
    if condition:
        _PASSED.append(label)
        logger.info("  PASS  %s%s", label, suffix)
    else:
        _FAILED.append(f"{label}{suffix}")
        logger.error("  FAIL  %s%s", label, suffix)


async def wait_until(predicate, *, timeout: float = 15.0, interval: float = 0.05) -> bool:
    """轮询等待条件成立。"""
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while loop.time() < deadline:
        if predicate():
            return True
        await asyncio.sleep(interval)
    return False


async def wait_for_action(
    device: MockDevice, action: str, *, timeout: float = 6.0
) -> dict[str, Any] | None:
    """等待设备收到并执行了某个动作，返回其 result 数据。"""
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while True:
        remaining = deadline - loop.time()
        if remaining <= 0:
            return None
        try:
            item = await asyncio.wait_for(device.results.get(), timeout=remaining)
        except asyncio.TimeoutError:
            return None
        if item.get("action") == action:
            return item.get("data")


def build_settings() -> Settings:
    """构造一套完全离线的测试配置。"""
    settings = Settings()
    settings.server.host = "127.0.0.1"
    # create_app 会按该级别重设 root logger，因此必须保持 info 才能看到断言输出。
    settings.server.log_level = "info"

    # 离线假模型 + 假语音，确保测试不联网、完全可复现。
    settings.llm.provider = "mock"
    settings.llm.model = "mock-model"
    settings.vision.provider = "mock"
    settings.speech.asr_provider = "mock"
    settings.speech.tts_provider = "mock"
    settings.speech.listen_timeout_s = 1.2

    settings.device.command_timeout_ms = 5_000
    settings.device.heartbeat_ms = 2_000
    settings.behavior.safety_enabled = True
    settings.behavior.max_linear_mps = 0.5
    settings.behavior.max_angular_rps = 1.5
    settings.behavior.max_duration_ms = 4_000
    settings.behavior.min_command_interval_ms = 0
    # 语音会话：端到端测试不跑"保持 5 分钟"，一轮就收工（等价于老行为），
    # 否则唤醒事件会拉起一个几分钟的后台会话，跟后面的听音工具抢麦克风。
    settings.behavior.voice_session_idle_timeout_s = 0.0
    settings.behavior.voice_session_turns = 1
    return settings


# --------------------------------------------------------------------------- #
# 阶段
# --------------------------------------------------------------------------- #
async def test_http_basics(served: ServedApp) -> None:
    """阶段 0：HTTP 层可用性。"""
    logger.info("阶段 0 · HTTP 基础接口")
    import httpx

    async with httpx.AsyncClient(base_url=served.base_url, timeout=10.0) as client:
        health = (await client.get("/health")).json()
        check(health.get("ok") is True, "健康检查可用", f"version={health.get('version')}")

        status = (await client.get("/api/status")).json()
        check(status.get("llm_provider") == "mock", "状态接口报告了 provider",
              f"llm={status.get('llm_provider')} tools={status.get('tools')}")
        check(status.get("tools", 0) >= 10, "工具已注册", f"{status.get('tools')} 个")

        tools = (await client.get("/api/tools")).json()
        names = {t["name"] for t in tools.get("tools", [])}
        expected = {"look_around", "move_forward", "turn_left", "stop_moving",
                    "show_emotion", "speak", "listen", "get_status"}
        check(expected <= names, "核心工具齐备", ", ".join(sorted(expected - names)) or "全部存在")

        console = await client.get("/")
        check("<title>SparkBot" in console.text, "控制台页面可访问")


async def test_handshake(rt: SparkBotRuntime, device: MockDevice) -> None:
    """阶段 1：握手与能力协商。"""
    logger.info("阶段 1 · 握手与能力协商")

    ok = await wait_until(lambda: rt.gateway.connected_count == 1, timeout=20.0)
    check(ok, "设备通过 WebSocket 成功接入")
    if not ok:
        return

    conn = rt.gateway.first()
    check(conn.device_id == device.config.device_id, "device_id 正确", conn.device_id)

    info = conn.info
    check(info is not None, "hello 信息已解析")
    if info is not None:
        check(set(info.capabilities) == set(device.config.capabilities), "能力集合一致",
              ", ".join(sorted(info.capabilities)))
        check(bool(info.name), "设备名已上报", info.name)

    robot = rt.robots.get()
    check(robot.has("camera") and robot.has("motor") and robot.has("speaker"),
          "能力门面识别正确")


async def test_telemetry(served: ServedApp, rt: SparkBotRuntime) -> None:
    """阶段 2：遥测上行。"""
    logger.info("阶段 2 · 遥测上行")
    robot = rt.robots.get()

    ok = await wait_until(lambda: bool(robot.battery), timeout=10.0)
    check(ok, "收到电量遥测", str(robot.battery))
    check(robot.battery.get("percent", 0) > 0, "电量数值合理")

    # HTTP 层也应能读到同一份遥测
    import httpx

    async with httpx.AsyncClient(base_url=served.base_url, timeout=10.0) as client:
        devices = (await client.get("/api/devices")).json()
    check(devices.get("count") == 1, "设备列表包含该设备")
    entry = devices["devices"][0]
    check(bool(entry.get("telemetry")), "遥测透出到 API", str(entry["telemetry"].get("battery")))


async def test_vision_tool(rt: SparkBotRuntime) -> None:
    """阶段 3：摄像头抓帧与 command/frame 配对。"""
    logger.info("阶段 3 · 视觉工具")
    robot = rt.robots.get()

    look = await robot.look()
    check(look.frame.data[:2] == b"\xff\xd8", "抓到的图像是合法 JPEG",
          f"{look.frame.approx_kb} KB")
    check(look.data_uri.startswith("data:image/jpeg;base64,"), "data URI 已生成")
    check(look.width == 640 and look.height == 480, "分辨率与请求一致",
          f"{look.width}x{look.height}")

    turn = await rt.chat("你前面有什么东西？")
    names = [t.name for t in turn.tool_invocations]
    check("look_around" in names, "假模型正确选择了 look_around", ", ".join(names))
    check(turn.ok, "对话无致命错误", turn.error or "正常")
    look_tool = next((t for t in turn.tool_invocations if t.name == "look_around"), None)
    check(look_tool is not None and look_tool.ok, "视觉工具执行成功",
          str((look_tool.result or {}).get("summary", ""))[:60] if look_tool else "")
    check(bool(turn.reply), "给出了语言回复", turn.reply[:60])
    check(turn.rounds >= 2, "工具调用经历了多轮交互", f"{turn.rounds} 轮")


async def test_motion_tools(rt: SparkBotRuntime) -> None:
    """阶段 4：运动工具与安全钳制。"""
    logger.info("阶段 4 · 运动与安全")
    robot = rt.robots.get()
    behavior = rt.settings.behavior

    turn = await rt.chat("往前走")
    names = [t.name for t in turn.tool_invocations]
    check("move_forward" in names, "假模型选择了 move_forward", ", ".join(names))
    check(turn.ok and bool(turn.reply), "运动类对话有回复", turn.reply[:40])

    result = await robot.drive(linear=5.0, angular=99.0, duration_ms=999_999)
    check(abs(result.linear) <= behavior.max_linear_mps + 1e-6, "线速度被钳制",
          f"{result.linear} <= {behavior.max_linear_mps}")
    check(abs(result.angular) <= behavior.max_angular_rps + 1e-6, "角速度被钳制",
          f"{result.angular} <= {behavior.max_angular_rps}")
    check(result.duration_ms <= behavior.max_duration_ms, "时长被钳制",
          f"{result.duration_ms} <= {behavior.max_duration_ms}")

    await robot.stop()
    check(robot.motion == {"linear": 0.0, "angular": 0.0}, "停止后运动状态归零")

    forward = await robot.forward(distance_m=0.2, speed_mps=0.25)
    check(abs(forward.duration_ms - 800) <= 1, "距离→时长换算正确", f"{forward.duration_ms} ms")
    await robot.stop()

    turn = await rt.chat("向左转")
    check("turn_left" in [t.name for t in turn.tool_invocations], "转向工具被调用")


async def test_tool_chaining(rt: SparkBotRuntime) -> None:
    """阶段 4b：多工具链式调用。

    这里直接走工具注册表而不是 ``rt.chat``，因为对话记忆是**有状态**的：
    前面的阶段已经往历史里塞过 ``look_around``，再让假模型自由发挥就会
    受历史影响而不稳定。工具链本身（逐个执行、结果回喂、状态累积）
    与用哪个模型无关，所以用确定性的直调来验证。
    """
    logger.info("阶段 4b · 多工具链式调用")
    robot = rt.robots.get()

    chain = [
        ("move_forward", {"distance_m": 0.2}),
        ("turn_left", {"degrees": 30}),
        ("show_emotion", {"emotion": "happy"}),
        ("stop_moving", {}),
    ]

    results = []
    for name, arguments in chain:
        results.append(await rt.registry.execute(name, arguments))

    check(all(r.ok for r in results), "链上每个工具都执行成功",
          ", ".join(f"{r.name}={'ok' if r.ok else r.error}" for r in results))
    check(len(rt.registry.names) >= 13, "注册表工具数量稳定", f"{len(rt.registry.names)}")

    # 链执行完之后底盘必须是停住的——这是安全底线
    await rt.registry.execute("stop_moving", {})
    check(robot.motion == {"linear": 0.0, "angular": 0.0}, "工具链结束后底盘已停止")


async def test_display(rt: SparkBotRuntime) -> None:
    """阶段 5：表情与显示。"""
    logger.info("阶段 5 · 表情与显示")
    robot = rt.robots.get()

    result = await robot.set_face(Emotion.HAPPY)
    check(result["emotion"] == "happy", "表情设置成功")

    fallback = await robot.set_face("这不是一个表情")
    check(fallback["emotion"] == "neutral", "未知表情降级为 neutral", fallback["emotion"])

    await robot.set_text("测试文字")
    check(True, "屏幕文字下发成功")


async def test_tts_playback(rt: SparkBotRuntime, device: MockDevice) -> None:
    """阶段 6：播报链路。"""
    logger.info("阶段 6 · 语音播报链路")
    robot = rt.robots.get()

    assert rt.tts is not None
    audio, fmt = await rt.tts.synthesize("你好，我是一台机器人。")
    check(len(audio) > 44, "TTS 生成了音频", f"{len(audio)} 字节 / {fmt}")
    check(audio[:4] == b"RIFF", "音频是合法 WAV 容器")

    await robot.say(audio, fmt=fmt)
    received = await wait_for_action(device, "play_audio")
    check(received is not None, "play_audio 到达设备并被执行",
          str(received)[:70] if received else "未收到")

    turn = await rt.chat("你好", announce=True)
    check(turn.ok and bool(turn.reply), "带播报的对话完成", turn.reply[:40])


async def test_voice_loop(rt: SparkBotRuntime, device: MockDevice) -> None:
    """阶段 7：语音闭环。"""
    logger.info("阶段 7 · 语音闭环")
    robot = rt.robots.get()

    pcm = await robot.collect_audio(max_seconds=4.0, silence_timeout_s=1.0)
    check(len(pcm) > 0, "从设备采集到音频", f"{len(pcm)} 字节 ≈ {len(pcm) / 32000:.2f}s")

    assert rt.asr is not None
    transcript = await rt.asr.transcribe(pcm, sample_rate=16_000)
    check(bool(transcript.text), "ASR 产出了文本", transcript.text[:40])
    check((transcript.duration_s or 0) > 0.5, "音频时长被正确估算", f"{transcript.duration_s}s")

    rt.agents.reset()
    text, turn = await rt.transcribe_and_chat(pcm, device_id=robot.device_id)
    check(bool(text), "transcribe_and_chat 返回识别文本")
    check(turn is not None and bool(turn.reply), "语音输入触发了回复",
          turn.reply[:50] if turn else "无")

    # 设备主动上报唤醒词应通过事件总线真正驱动闭环
    triggered: list[str] = []
    original = rt._handle_voice_turn  # noqa: SLF001

    async def spy(device_id: str | None) -> None:
        triggered.append(str(device_id))
        await original(device_id)

    rt._handle_voice_turn = spy  # type: ignore[method-assign]  # noqa: SLF001
    try:
        await device._send_event(EventName.WAKE_WORD, phrase="你好小星")  # noqa: SLF001
        ok = await wait_until(lambda: bool(triggered), timeout=10.0)
        check(ok, "唤醒词事件驱动了语音闭环", triggered[0] if triggered else "未触发")
    finally:
        rt._handle_voice_turn = original  # type: ignore[method-assign]


async def test_listen_tool(rt: SparkBotRuntime) -> None:
    """阶段 8：listen 工具的完整链条。"""
    logger.info("阶段 8 · listen 工具")
    result = await asyncio.wait_for(
        rt.registry.execute("listen", {"max_seconds": 4.0}), timeout=25.0
    )
    check(result.ok, "listen 工具执行成功", result.error or str(result.data)[:60])
    payload = result.data if isinstance(result.data, dict) else {}
    check(bool(payload.get("text")), "listen 拿到了识别文本", str(payload.get("text"))[:40])


async def test_emergency_stop(rt: SparkBotRuntime, device: MockDevice) -> None:
    """阶段 9：急停绕过模型立即生效。"""
    logger.info("阶段 9 · 急停")
    robot = rt.robots.get()

    # 通过 intents 通道下发持续运动（duration_ms=0 = 一直走），
    # 这样不会因为开环定时到点自动停止而让断言变得不确定。
    await robot.conn.intent("drive", {"linear": 0.3, "angular": 0.0, "duration_ms": 0})
    started = await wait_until(
        lambda: abs(robot.conn.motion.get("linear", 0.0) - 0.3) < 1e-6, timeout=3.0
    )
    check(started, "底盘已进入运动状态", str(robot.conn.motion))

    stopped = await rt.emergency_stop()
    check(robot.device_id in stopped, "急停返回了被停止的设备", str(stopped))
    check(robot.conn.motion == {"linear": 0.0, "angular": 0.0}, "急停后底盘速度归零")


async def test_capability_filtering(rt: SparkBotRuntime) -> None:
    """阶段 10：能力裁剪、错误处理与 schema 生成。"""
    logger.info("阶段 10 · 能力过滤与错误处理")
    registry = rt.registry

    names = {s["function"]["name"] for s in registry.openai_schemas({"display"})}
    check("look_around" not in names, "无摄像头时不暴露视觉工具")
    check("move_forward" not in names, "无电机时不暴露运动工具")
    check("show_emotion" in names, "有显示能力时暴露表情工具")
    check("get_status" in names, "无设备依赖的工具始终可用")

    bad = await registry.execute("move_forward", {"distance_m": "不是数字"})
    check(not bad.ok, "非法参数被捕获为失败结果", str(bad.error)[:60])

    unknown = await registry.execute("完全不存在的工具", {})
    check(not unknown.ok and unknown.error_code == "unknown_tool", "未知工具返回明确错误")

    spec = registry.get("show_emotion")
    props = spec.parameters["properties"]
    check("emotion" in props and props["emotion"]["type"] == "string", "schema 自动生成参数")
    check("description" in props["emotion"], "docstring 被解析成参数说明")
    # 有默认值的参数不应出现在 required 里（字段可能整个不存在）
    check(not spec.parameters.get("required"), "有默认值的参数不进 required")
    check(props["intensity"]["type"] == "number", "float 注解映射为 number",
          props["intensity"]["type"])
    check(props["emotion"]["default"] == "happy", "默认值被写入 schema",
          str(props["emotion"].get("default")))
    check(bool(spec.description) and len(spec.description) > 10, "工具描述足够详细",
          spec.description[:50])


async def test_http_chat_and_stop(served: ServedApp, rt: SparkBotRuntime) -> None:
    """阶段 11：HTTP 对话与控制接口。"""
    logger.info("阶段 11 · HTTP 对话与控制接口")
    import httpx

    async with httpx.AsyncClient(base_url=served.base_url, timeout=30.0) as client:
        response = await client.post("/api/chat", json={"text": "你好呀"})
        payload = response.json()
        check(response.status_code == 200, "POST /api/chat 返回 200", str(response.status_code))
        check(bool(payload.get("reply")), "HTTP 对话拿到了回复", str(payload.get("reply"))[:50])

        face = await client.post("/api/face", json={"emotion": "happy"})
        check(face.status_code == 200 and face.json().get("emotion") == "happy",
              "POST /api/face 生效")

        action = await client.post("/api/action",
                                   json={"action": "set_backlight", "params": {"percent": 42}})
        check(action.status_code == 200, "POST /api/action 生效", str(action.json())[:60])

        stop = await client.post("/api/stop")
        check(stop.status_code == 200, "POST /api/stop 生效", str(stop.json())[:60])

        empty = await client.post("/api/chat", json={"text": ""})
        check(empty.status_code == 422, "空输入被拒绝为 422", str(empty.status_code))

        reset = await client.post("/api/agent/reset", json={})
        check(reset.status_code == 200, "对话重置接口可用")


# --------------------------------------------------------------------------- #
# 主流程
# --------------------------------------------------------------------------- #
async def main() -> int:
    """启动真实服务与模拟设备，跑完全部阶段。"""
    settings = build_settings()
    app = create_app(settings)

    logger.info("启动服务（真实 HTTP/WebSocket 层，自动分配端口）…")
    served = await serve_app(app)

    rt: SparkBotRuntime = app.state.runtime
    device = MockDevice(
        MockDeviceConfig(
            url=served.ws_url(),
            device_id="esp32s3-e2e",
            name="小星（端到端测试）",
            telemetry_ms=1_000,
            frame_width=640,
            frame_height=480,
        )
    )
    device_task = asyncio.create_task(device.run(), name="mock-device")

    try:
        await test_http_basics(served)
        await test_handshake(rt, device)
        if rt.gateway.connected_count == 0:
            logger.error("握手失败，后续阶段无法进行")
            return 1

        await test_telemetry(served, rt)
        await test_vision_tool(rt)
        await test_motion_tools(rt)
        await test_tool_chaining(rt)
        await test_display(rt)
        await test_tts_playback(rt, device)
        await test_voice_loop(rt, device)
        await test_listen_tool(rt)
        await test_emergency_stop(rt, device)
        await test_capability_filtering(rt)
        await test_http_chat_and_stop(served, rt)
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
        logger.info("=" * 64)
        return 1
    logger.info("全部通过")
    logger.info("=" * 64)
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
