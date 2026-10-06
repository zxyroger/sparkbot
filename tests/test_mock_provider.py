"""假模型（MockProvider）的规则与多工具链单元测试。

不需要服务、设备或网络，直接构造消息数组调用 ``provider.chat``。

重点验证三件事：

1. 关键词能正确映射到工具；
2. **连续多轮**里工具调用不会互相污染——曾经的 bug 是「只要历史里
   出现过 tool 消息，后面所有轮次都直接给最终回复」，表现为机器人
   永远只重复上一句话、再也不调工具；
3. 一句话包含多个意图时，能依次调用多个工具形成动作链。

运行::

    python tests/test_mock_provider.py
"""

from __future__ import annotations

import asyncio
import logging
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from sparkbot.paths import ensure_path  # noqa: E402

ensure_path()

from sparkbot.brain.agent import Agent  # noqa: E402
from sparkbot.brain.memory import Memory  # noqa: E402
from sparkbot.core.tools import ToolRegistry  # noqa: E402
from sparkbot.llm.base import (  # noqa: E402
    ChatMessage,
    LLMProvider,
    LLMResponse,
    ToolCallRequest,
)
from sparkbot.llm.mock_provider import MockProvider  # noqa: E402

logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(message)s")

_PASSED = 0
_FAILED: list[str] = []


class ScriptedProvider(LLMProvider):
    """按脚本返回预设的工具调用，用来确定性地测试 Agent 的链式执行。

    真实模型在一次响应里就可以并列返回多个 ``tool_calls``，
    ``Agent`` 必须把它们**全部**执行完再回喂。本类正是模拟这个行为。

    Args:
        script: 每次 ``chat`` 依次返回的响应；用完后返回固定的收尾文本。
    """

    name = "scripted"
    supports_vision = False

    def __init__(self, script: list[LLMResponse], *, fallback: str = "做完了。") -> None:
        self.script = list(script)
        self.fallback = fallback
        self.calls = 0

    async def chat(self, messages, *, tools=None, temperature=None, max_tokens=None) -> LLMResponse:
        """返回脚本里的下一条响应。"""
        self.calls += 1
        if self.script:
            return self.script.pop(0)
        return LLMResponse(content=self.fallback, model=self.name, finish_reason="stop")


def call(name: str, index: int, **arguments) -> ToolCallRequest:
    """构造一个工具调用请求。"""
    import json

    return ToolCallRequest(
        id=f"call_{index}_{name}",
        name=name,
        arguments=arguments,
        raw_arguments=json.dumps(arguments, ensure_ascii=False),
    )


def check(condition: bool, label: str, detail: str = "") -> None:
    """记录断言结果。"""
    global _PASSED
    if condition:
        _PASSED += 1
        print(f"  PASS  {label}" + (f" — {detail}" if detail else ""))
    else:
        _FAILED.append(label)
        print(f"  FAIL  {label}" + (f" — {detail}" if detail else ""))


class NullBus:
    """丢弃一切事件的总线替身，避免测试依赖真实 EventBus。"""

    def publish(self, topic: str, **payload) -> None:
        """什么都不做。"""
        return None


def minimal_settings():
    """构造 Agent 需要的最小配置。"""
    from sparkbot.config import Settings

    settings = Settings()
    settings.llm.provider = "mock"
    settings.llm.max_tool_rounds = 4
    settings.behavior.talking_animation = False
    return settings


def _noop_tool(name: str):
    """生成一个返回固定成功结果的异步工具函数。"""

    async def tool(**kwargs):
        """测试桩工具。"""
        return {"ok": True, "summary": f"{name} 已执行"}

    tool.__name__ = name
    return tool


def tools(*names: str) -> list[dict]:
    """构造 tools 数组。"""
    return [
        {"type": "function", "function": {"name": n, "description": "", "parameters": {}}}
        for n in names
    ]


ALL_TOOLS = tools(
    "look_around", "capture_photo", "move_forward", "move_backward", "turn_left",
    "turn_right", "stop_moving", "show_emotion", "show_text", "beep", "speak",
    "listen", "get_status",
)


async def test_rule_mapping() -> None:
    """阶段 1：关键词 → 工具映射。"""
    print("\n阶段 1 · 关键词映射")
    provider = MockProvider()

    cases = [
        ("往前走", "move_forward"),
        ("往后退一点", "move_backward"),
        ("向左转", "turn_left"),
        ("向右转 90 度", "turn_right"),
        ("停下来", "stop_moving"),
        ("你前面有什么东西？", "look_around"),
        ("你现在状态怎么样", "get_status"),
        ("开心一点", "show_emotion"),
        ("叫一声", "beep"),
    ]

    for text, expected in cases:
        response = await provider.chat([ChatMessage.user(text)], tools=ALL_TOOLS)
        names = [c.name for c in response.tool_calls]
        check(expected in names, f"「{text}」→ {expected}", ", ".join(names) or "无调用")

    # 没有关键词时应直接回答，而不是乱调工具
    response = await provider.chat([ChatMessage.user("今天天气不错")], tools=ALL_TOOLS)
    check(not response.tool_calls, "无关键词时不调用工具")
    check(bool(response.content), "无关键词时给出文本回复", response.content[:30])


async def test_rules_reference_real_tools() -> None:
    """阶段 1b：规则表里的工具名必须真实存在（关键回归）。

    这条断言是有来历的：规则表曾经把 ``set_face``（协议层的 action 名）
    当成工具名写进去，而注册表里实际叫 ``show_emotion``。
    写错名字**不会报任何错**——规则只会被"工具不可用"静默跳过，
    表现为「说你好永远不换表情」。这种失效方式极难靠人工发现，
    所以必须由测试守着。
    """
    print("\n阶段 1b · 规则表与真实工具的命名一致性")
    from sparkbot.brain.tools import build_registry, ToolContext
    from sparkbot.config import Settings
    from sparkbot.core.events import EventBus
    from sparkbot.device.capabilities import RobotProvider
    from sparkbot.device.gateway import DeviceGateway
    from sparkbot.llm.mock_provider import _FOLLOWUP, _RULES, _STEP_LABEL

    settings = Settings()
    settings.llm.provider = "mock"
    settings.vision.provider = "mock"
    gateway = DeviceGateway(settings, EventBus())
    registry = build_registry(
        ToolContext(
            robots=RobotProvider(gateway, settings),
            vision=None, asr=None, tts=None, settings=settings,
        )
    )
    real_names = set(registry.names)

    rule_tools = {tool for _, tool, _ in _RULES}
    check(rule_tools <= real_names, "规则表引用的工具全部真实存在",
          f"不存在的: {sorted(rule_tools - real_names)}" or "全部存在")

    # 全能力设备下，每条规则都应能命中（>= 说明至少有个工具可用）
    full_caps = {"camera", "microphone", "speaker", "display", "motor"}
    selectable = {
        s["function"]["name"] for s in registry.openai_schemas(full_caps)
    }
    unusable = rule_tools - selectable
    check(not unusable, "全能力设备下所有规则的工具都可用",
          f"不可用: {sorted(unusable)}" or "全部可用")

    # 辅助文案表里的键也应与真实工具名一致
    for label, table in (("_FOLLOWUP", _FOLLOWUP), ("_STEP_LABEL", _STEP_LABEL)):
        unknown = set(table) - real_names
        check(not unknown, f"{label} 的键都是真实工具名",
              f"多余: {sorted(unknown)}" or "全部一致")

    # 规则表引用的工具必须都能被假模型解析出来（走真实 schema 路径）
    provider = MockProvider()
    parsed = provider._available_tool_names(registry.openai_schemas(full_caps))
    check(rule_tools <= parsed, "假模型能从真实 schema 解析出全部规则工具",
          f"缺: {sorted(rule_tools - parsed)}" or "全部可解析")

    # 端到端确认：问候语必须真的触发一个表情工具
    response = await provider.chat(
        [ChatMessage.user("你好")], tools=registry.openai_schemas(full_caps)
    )
    names = [c.name for c in response.tool_calls]
    check(names == ["show_emotion"], "「你好」触发了表情工具", ", ".join(names) or "无调用")


async def test_history_isolation() -> None:
    """阶段 2：多轮对话之间不互相污染（回归测试）。"""
    print("\n阶段 2 · 历史隔离（关键回归）")
    provider = MockProvider()

    # 第 1 轮：看
    messages = [ChatMessage.system("sys"), ChatMessage.user("你前面有什么东西？")]
    first = await provider.chat(messages, tools=ALL_TOOLS)
    check([c.name for c in first.tool_calls] == ["look_around"], "第 1 轮调用 look_around")

    # 把第 1 轮完整落进历史
    messages.append(
        ChatMessage(role="assistant", content="", tool_calls=first.tool_calls)
    )
    messages.append(
        ChatMessage.tool(first.tool_calls[0].id, '{"ok": true, "result": {"summary": "看到一张桌子"}}')
    )
    final = await provider.chat(messages, tools=ALL_TOOLS)
    check(not final.tool_calls, "第 1 轮收尾不再调工具")
    check("看到一张桌子" in final.content, "第 1 轮收尾引用了工具结果", final.content[:40])
    messages.append(ChatMessage(role="assistant", content=final.content))

    # 第 2 轮：历史里已经有 tool 消息，也必须能调新工具
    messages.append(ChatMessage.user("往前走"))
    second = await provider.chat(messages, tools=ALL_TOOLS)
    names = [c.name for c in second.tool_calls]
    check(
        names == ["move_forward"],
        "第 2 轮不受上一轮 tool 消息影响，仍能调用新工具",
        ", ".join(names) or "无调用",
    )
    check("桌子" not in second.content, "第 2 轮没有复读上一轮的回复", second.content[:30] or "(空)")

    # 第 3 轮：再换一个工具
    messages.append(ChatMessage(role="assistant", content="", tool_calls=second.tool_calls))
    messages.append(
        ChatMessage.tool(second.tool_calls[0].id, '{"ok": true, "result": {"summary": "前进 0.3 米"}}')
    )
    third_final = await provider.chat(messages, tools=ALL_TOOLS)
    check(not third_final.tool_calls, "第 2 轮收尾正常结束")
    messages.append(ChatMessage(role="assistant", content=third_final.content))

    messages.append(ChatMessage.user("向左转"))
    third = await provider.chat(messages, tools=ALL_TOOLS)
    check(
        [c.name for c in third.tool_calls] == ["turn_left"],
        "第 3 轮继续正常选择工具",
        ", ".join(c.name for c in third.tool_calls) or "无调用",
    )


async def test_multi_tool_chain() -> None:
    """阶段 3：一次响应并列多个工具 → Agent 必须全部执行。

    这里用 :class:`ScriptedProvider` 而不是 :class:`MockProvider`：
    假模型基于关键词逐轮挑工具，而**真实模型可以在一条 assistant 消息里
    并列返回多个 tool_call**。链式执行是 Agent 的职责，用脚本化 provider
    才能确定性地验证它。
    """
    print("\n阶段 3 · 多工具链（Agent 执行并列工具调用）")

    script = [
        LLMResponse(
            content="",
            tool_calls=[
                call("move_forward", 1, distance_m=0.2),
                call("turn_left", 2, degrees=30),
            ],
            model="scripted",
            finish_reason="tool_calls",
        ),
        LLMResponse(
            content="",
            tool_calls=[call("show_emotion", 3, emotion="happy")],
            model="scripted",
            finish_reason="tool_calls",
        ),
    ]

    provider = ScriptedProvider(script, fallback="我会先往前走一点，然后向左转，还换个开心的表情。")
    executed: list[str] = []

    class RecordingRegistry:
        """记录调用顺序，同时转发给真实注册表执行。

        转发到真实工具是有意的：这样链上每个工具的参数校验、错误处理、
        返回结构都会被一并验证，而不只是「调用被记录了」。
        """

        def __init__(self, inner) -> None:
            self._inner = inner

        @property
        def names(self) -> list[str]:
            return self._inner.names

        def openai_schemas(self, capabilities=None):
            return self._inner.openai_schemas(capabilities)

        async def execute(self, name, arguments=None):
            executed.append(name)
            return await self._inner.execute(name, arguments)

    underlying = ToolRegistry()
    for name in ("move_forward", "turn_left", "show_emotion", "stop_moving"):
        underlying.register(_noop_tool(name), name=name, description=f"{name} 测试桩")

    class StubRobot:
        """不做任何事、只记录调用的机器人替身。"""

        device_id = "stub"
        capabilities = frozenset({"motor", "display", "camera", "speaker"})

        def has(self, capability: str) -> bool:
            return capability in self.capabilities

        def status(self) -> dict:
            return {
                "device_id": self.device_id,
                "name": "stub",
                "capabilities": sorted(self.capabilities),
                "battery_percent": 90,
                "motion": {"linear": 0.0, "angular": 0.0},
            }

        async def forward(self, distance_m: float = 0.0, speed_mps: float = 0.25):
            from sparkbot.device.capabilities import MotionResult

            return MotionResult(linear=speed_mps, angular=0.0, duration_ms=800)

        async def drive(self, linear: float, angular: float = 0.0, duration_ms: int = 800, **kwargs):
            from sparkbot.device.capabilities import MotionResult

            return MotionResult(linear=linear, angular=angular, duration_ms=duration_ms)

        async def set_face(self, emotion, intensity: float = 1.0):
            return {"ok": True, "emotion": getattr(emotion, "value", str(emotion))}

    class StubRobots:
        """RobotProvider 替身。"""

        def get(self, device_id=None):  # noqa: ANN001
            return StubRobot()

    agent = Agent(
        settings=minimal_settings(),
        provider=provider,
        registry=RecordingRegistry(underlying),  # type: ignore[arg-type]
        context=None,  # type: ignore[arg-type]
        robots=StubRobots(),  # type: ignore[arg-type]
        bus=NullBus(),  # type: ignore[arg-type]
        device_id=None,
    )
    agent.memory = Memory(system_prompt="测试用", limit=20)

    turn = await agent.run("往前走，然后向左转")

    check(executed == ["move_forward", "turn_left", "show_emotion"],
          "并列与后续工具按顺序全部执行", ", ".join(executed))
    # 第 1 轮请求并列工具 → 第 2 轮再请求 → 第 3 轮给出收尾文本
    check(turn.rounds == 3, "并列工具 + 后续工具共 3 轮模型调用", f"{turn.rounds} 轮")
    check(all(t.ok for t in turn.tool_invocations), "每个工具结果都被记录为成功")
    check(bool(turn.reply) and turn.error is None, "最终给出了收尾回复", turn.reply[:50])
    check(provider.calls == 3, "provider 被调用了 3 次（2 轮工具 + 1 轮收尾）",
          str(provider.calls))


async def test_capability_filtering() -> None:
    """阶段 4：设备没有的能力不应被调用。"""
    print("\n阶段 4 · 能力过滤")
    provider = MockProvider()

    display_only = tools("show_emotion", "show_text", "get_status")
    response = await provider.chat([ChatMessage.user("往前走")], tools=display_only)
    check(not response.tool_calls, "没有电机能力时不调用运动工具")

    response = await provider.chat([ChatMessage.user("开心一点")], tools=display_only)
    check([c.name for c in response.tool_calls] == ["show_emotion"], "有显示能力时正常调用表情")


async def test_image_input() -> None:
    """阶段 5：带图片的输入走视觉分支。"""
    print("\n阶段 5 · 图像输入")
    provider = MockProvider()
    message = ChatMessage.user("这是什么？", images=["data:image/jpeg;base64,AAAA"])
    response = await provider.chat([message], tools=ALL_TOOLS)
    check(not response.tool_calls, "带图输入不额外调工具")
    check("离线" in response.content or "画面" in response.content,
          "带图输入给出视觉相关回复", response.content[:40])


async def main() -> int:
    """跑完全部阶段。"""
    await test_rule_mapping()
    await test_rules_reference_real_tools()
    await test_history_isolation()
    await test_multi_tool_chain()
    await test_capability_filtering()
    await test_image_input()

    total = _PASSED + len(_FAILED)
    print()
    print("=" * 60)
    print(f"断言汇总: {_PASSED}/{total} 通过")
    if _FAILED:
        print("失败项:")
        for item in _FAILED:
            print(f"  · {item}")
        print("=" * 60)
        return 1
    print("全部通过")
    print("=" * 60)
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
