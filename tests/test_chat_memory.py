"""会话历史的「工具调用配对」回归测试。

守的是一个**真实踩过的坑**：定长 deque 逐条裁历史时，可能丢掉
「带 tool_calls 的 assistant 消息」而留下它的 tool 回复，
于是发给模型的请求被 API 直接拒掉：

    Messages with role 'tool' must be a response to a preceding message with 'tool_calls'

现象极具误导性 —— 唤醒、录音、识别全都正常，机器人却回一句
「我的大脑有点连不上」，用户只会认为"唤醒坏了"。

运行::

    python tests/test_chat_memory.py
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from sparkbot.paths import ensure_path  # noqa: E402

ensure_path()

from sparkbot.brain.memory import Memory  # noqa: E402
from sparkbot.llm.base import ChatMessage, ToolCallRequest  # noqa: E402

_PASSED: list[str] = []
_FAILED: list[str] = []


def check(condition: bool, label: str, detail: str = "") -> None:
    """记录一次断言。"""
    suffix = f" — {detail}" if detail else ""
    if condition:
        _PASSED.append(label)
        print(f"  PASS  {label}{suffix}")
    else:
        _FAILED.append(f"{label}{suffix}")
        print(f"  FAIL  {label}{suffix}")


def assistant_with_tool(call_id: str) -> ChatMessage:
    """造一条带 tool_calls 的 assistant 消息。"""
    return ChatMessage(
        role="assistant",
        content="",
        tool_calls=[ToolCallRequest(id=call_id, name="look_around", arguments={}, raw_arguments="{}")],
    )


def payload_is_valid(messages: list[ChatMessage]) -> tuple[bool, str]:
    """按 OpenAI 的协议检查：每条 tool 消息都能找到它的 tool_calls 父消息。"""
    open_ids: set[str] = set()
    for message in messages:
        role = message.role_value
        if role == "system":
            continue
        if role == "assistant" and message.tool_calls:
            open_ids |= {call.id for call in message.tool_calls}
            continue
        if role == "tool":
            if message.tool_call_id not in open_ids:
                return False, f"孤儿 tool 消息（id={message.tool_call_id}）"
            open_ids.discard(message.tool_call_id)
            continue
        # 普通 user/assistant 消息：如果还有没被回复的 tool_calls，说明上一组是半截的
        if open_ids:
            return False, f"tool_calls 未被回复就被打断（缺 {sorted(open_ids)}）"
    if open_ids:
        return False, f"结尾有未回复的 tool_calls（缺 {sorted(open_ids)}）"
    return True, ""


def test_orphan_tool_message() -> None:
    """阶段 1：被裁掉父消息的 tool 回复必须被剔除。"""
    print("\n阶段 1 · 孤儿 tool 回复")
    memory = Memory(system_prompt="你是小星", limit=4)
    memory.append(ChatMessage.user("你前面有什么"))
    memory.append(assistant_with_tool("call_1"))
    memory.append(ChatMessage.tool("call_1", '{"ok":true}'))
    memory.append(ChatMessage(role="assistant", content="我看到一张桌子。"))

    # 再塞两条，把最老的 user + assistant(tool_calls) 挤出去，只留下 tool 回复
    memory.append(ChatMessage.user("哦"))
    memory.append(ChatMessage(role="assistant", content="嗯。"))

    sent = memory.messages()
    roles = [m.role_value for m in sent]
    check("tool" not in roles, "被裁掉父消息后不再残留孤儿 tool 回复", str(roles))
    ok, why = payload_is_valid(sent)
    check(ok, "整理后的历史满足 OpenAI 的配对约束", why)


def test_dangling_tool_calls() -> None:
    """阶段 2：没有 tool 回复的 assistant(tool_calls) 必须整组丢掉。"""
    print("\n阶段 2 · 半截的 tool_calls")
    memory = Memory(system_prompt="你是小星", limit=20)
    memory.append(ChatMessage.user("往前走"))
    memory.append(assistant_with_tool("call_x"))      # 工具还没执行完就中断了
    memory.append(ChatMessage.user("算了"))

    sent = memory.messages()
    check(
        all(not m.tool_calls for m in sent),
        "未收到回复的 tool_calls 不再发给模型",
        str([m.role_value for m in sent]),
    )
    ok, why = payload_is_valid(sent)
    check(ok, "整理后的历史满足 OpenAI 的配对约束", why)


def test_normal_history_untouched() -> None:
    """阶段 3：正常的多轮 + 完整工具调用不能被误伤。"""
    print("\n阶段 3 · 正常历史不受影响")
    memory = Memory(system_prompt="你是小星", limit=20)
    memory.append(ChatMessage.user("你前面有什么"))
    memory.append(assistant_with_tool("c1"))
    memory.append(ChatMessage.tool("c1", '{"ok":true}'))
    memory.append(ChatMessage(role="assistant", content="有张桌子。"))
    memory.append(ChatMessage.user("往前走一点"))
    memory.append(assistant_with_tool("c2"))
    memory.append(ChatMessage.tool("c2", '{"ok":true}'))
    memory.append(ChatMessage(role="assistant", content="好，走了。"))

    sent = memory.messages()
    check(len(sent) == 9, "正常的 8 条历史 + system 全部保留", str(len(sent)))
    ok, why = payload_is_valid(sent)
    check(ok, "正常历史本身也满足配对约束", why)


def test_pair_survives_trimming() -> None:
    """阶段 4：裁剪时整组一起丢，不留下半组。"""
    print("\n阶段 4 · 裁剪保持成组")
    memory = Memory(system_prompt="你是小星", limit=3)
    for i in range(6):
        memory.append(ChatMessage.user(f"问题{i}"))
        memory.append(assistant_with_tool(f"call_{i}"))
        memory.append(ChatMessage.tool(f"call_{i}", '{"ok":true}'))
        memory.append(ChatMessage(role="assistant", content=f"回答{i}"))

    sent = memory.messages()
    ok, why = payload_is_valid(sent)
    check(ok, "反复裁剪后历史依然配对完整", why)
    check(len(sent) <= memory.limit + 1, "长度仍受上限约束", f"{len(sent)} 条")


def main() -> int:
    """跑完全部阶段。"""
    print("会话历史配对测试")
    print("=" * 64)
    test_orphan_tool_message()
    test_dangling_tool_calls()
    test_normal_history_untouched()
    test_pair_survives_trimming()
    print("=" * 64)
    total = len(_PASSED) + len(_FAILED)
    print(f"断言汇总: {len(_PASSED)}/{total} 通过")
    if _FAILED:
        print("失败项:")
        for item in _FAILED:
            print(f"  · {item}")
        return 1
    print("全部通过")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
