"""离线假模型：不联网也能驱动完整的工具调用循环。

存在的意义不是「假装有智能」，而是让框架的**每个环节都可验证**：

* 协议往返、工具注册表、安全钳制、多轮工具循环、语音链路——
  这些都与模型质量无关，用假模型才能在 CI 里稳定断言；
* 没有 API key 的开发者拉下代码就能 ``python -m sparkbot`` 跑通全链路；
* 真模型出故障时可以一键切回，机器人至少还能响应基本口令。

它用关键词规则挑工具，再按工具返回结果拼一句中文回复。
"""

from __future__ import annotations

import json
import logging
import re
from typing import Any

from .base import ChatMessage, LLMProvider, LLMResponse, ToolCallRequest, Usage

logger = logging.getLogger(__name__)

#: 规则表：``(触发词, 工具名, 参数构造)``。顺序即优先级。
#:
#: 这里的 **工具名必须与 ``brain/tools.py`` 注册的名字完全一致**
#: （``show_emotion``，而不是协议层的 action ``set_face``）。
#: 写错名字不会报错——规则只会被"工具不可用"静默跳过，
#: 表现为某类输入永远不触发任何动作。``tests/test_mock_provider.py``
#: 里有一条断言专门守着这个一致性。
_RULES: list[tuple[tuple[str, ...], str, dict[str, Any]]] = [
    (("你好", "您好", "嗨", "在吗", "hello", "hi"), "show_emotion", {"emotion": "happy"}),
    (("看", "看见", "前面", "什么", "识别", "这是", "那是什么"), "look_around", {}),
    (("前进", "往前", "向前", "过来", "走"), "move_forward", {"distance_m": 0.3}),
    (("后退", "退后", "往后"), "move_backward", {"distance_m": 0.3}),
    (("左转", "向左"), "turn_left", {"degrees": 45}),
    (("右转", "向右"), "turn_right", {"degrees": 45}),
    (("停", "别动", "停下", "刹车"), "stop_moving", {}),
    (("开心", "高兴", "笑"), "show_emotion", {"emotion": "happy"}),
    (("难过", "伤心", "悲伤"), "show_emotion", {"emotion": "sad"}),
    (("生气", "愤怒"), "show_emotion", {"emotion": "angry"}),
    (("惊讶", "吃惊"), "show_emotion", {"emotion": "surprised"}),
    (("困", "睡觉", "累"), "show_emotion", {"emotion": "sleepy"}),
    (("状态", "电量", "怎么样", "报告"), "get_status", {}),
    (("叫一声", "响一下", "提示音"), "beep", {}),
]

#: 协议层 action 名 → 工具层工具名。两者刻意不同名
#: （``set_face`` 是设备指令，``show_emotion`` 是给模型用的工具），
#: 这里显式记录映射，避免再写错。
_ACTION_TO_TOOL: dict[str, str] = {
    "set_face": "show_emotion",
    "drive": "move_forward",
    "stop": "stop_moving",
    "snapshot": "look_around",
    "play_tone": "beep",
}

#: 给每个工具准备一句自然的收尾话，让假模型听起来像在对话。
_FOLLOWUP: dict[str, str] = {
    "show_emotion": "我换了个表情，你看到了吗？",
    "look_around": "我看了一下周围。",
    "move_forward": "我往前挪了一点。",
    "move_backward": "我往后退了一点。",
    "turn_left": "我向左转了一下。",
    "turn_right": "我向右转了一下。",
    "stop_moving": "好，我停下了。",
    "get_status": "这是我现在的情况。",
    "beep": "叮！",
}

#: 多工具链收尾时用的步骤名。
_STEP_LABEL: dict[str, str] = {
    "show_emotion": "换表情",
    "look_around": "看一眼周围",
    "move_forward": "往前走",
    "move_backward": "往后退",
    "turn_left": "向左转",
    "turn_right": "向右转",
    "stop_moving": "停下来",
    "get_status": "查状态",
    "beep": "响一声",
}

#: 需要视觉能力、但在离线假模型下拿不到真实结论的工具。
_VISION_TOOLS = frozenset({"look_around"})

_RESULT_NOISE = re.compile(r"\s+")


class MockProvider(LLMProvider):
    """基于关键词规则的假模型。"""

    name = "mock"
    supports_vision = True

    def __init__(self, *, model: str = "mock-model") -> None:
        self.model = model
        #: 记录调用次数，便于测试断言「确实走了模型」。
        self.call_count = 0

    async def chat(
        self,
        messages: list[ChatMessage],
        *,
        tools: list[dict[str, Any]] | None = None,
        temperature: float | None = None,
        max_tokens: int | None = None,
    ) -> LLMResponse:
        """按规则决定「调工具」还是「直接回答」。"""
        self.call_count += 1
        available = self._available_tool_names(tools or [])

        # 所有判断都只针对「本轮」——最后一条 user 消息之后的部分。
        # 这一点非常关键：如果拿整段历史来判断「有没有工具结果」，
        # 那么在第二轮对话时会立刻命中历史里上一轮的 tool 消息，
        # 导致模型永远只重复上一轮的回答，再也不调用任何新工具。
        turn_start = self._turn_start(messages)
        current_turn = messages[turn_start:]

        last_user = self._last_user_message(messages)
        if last_user is None:
            return LLMResponse(content="我在。", model=self.model, finish_reason="stop")

        text = last_user.content or ""

        # 1) 本轮已经有工具结果 -> 给最终回复，避免无限循环。
        current_tools = [m for m in current_turn if m.role_value == "tool"]
        if current_tools:
            return self._finalize(current_tools[-1], messages)

        # 2) 用户带了图片 -> 假装看图，直接给结论。
        if last_user.images:
            return LLMResponse(
                content="我看到画面了：镜头前有东西，不过我这里是离线假模型，接上云端视觉模型就能说出具体是什么。",
                model=self.model,
                finish_reason="stop",
                usage=Usage(prompt_tokens=0, completion_tokens=0, total_tokens=0),
            )

        # 3) 本轮已经调用过哪些工具——用 assistant 消息里的 tool_calls 反推。
        already_called = self._called_tools(messages)
        logger.debug(
            "mock.chat 输入 %d 条消息（本轮 %d 条），最后一条 user=%r，本轮已调用=%s，可用工具=%d",
            len(messages),
            len(current_turn),
            text[:20],
            already_called,
            len(available),
        )

        # 4) 命中规则且本轮尚未调用过 -> 发起工具调用。
        for keywords, tool_name, arguments in _RULES:
            if tool_name not in available or tool_name in already_called:
                continue
            if any(keyword in text for keyword in keywords):
                call = ToolCallRequest(
                    id=f"call_{self.call_count}_{tool_name}",
                    name=tool_name,
                    arguments=dict(arguments),
                    raw_arguments=json.dumps(arguments, ensure_ascii=False),
                )
                return LLMResponse(
                    content="",
                    tool_calls=[call],
                    model=self.model,
                    finish_reason="tool_calls",
                )

        # 5) 本轮的规则都已执行完 -> 给最终回复，说明做了哪些动作。
        if already_called:
            return self._summarize(already_called)

        # 6) 没命中规则 -> 普通闲聊回复。
        if text.strip():
            reply = f"我听到你说「{text.strip()[:40]}」。我是一台会看、会走、会做表情的小机器人。"
        else:
            reply = "我在听呢。"
        return LLMResponse(content=reply, model=self.model, finish_reason="stop")

    # ------------------------------------------------------------------ #
    # 内部
    # ------------------------------------------------------------------ #
    @staticmethod
    def _available_tool_names(tools: list[dict[str, Any]]) -> set[str]:
        """从 OpenAI 形状的 tools 数组里取出可用工具名。"""
        names: set[str] = set()
        for item in tools:
            function = item.get("function") if isinstance(item, dict) else None
            if isinstance(function, dict) and function.get("name"):
                names.add(str(function["name"]))
        return names

    @staticmethod
    def _turn_start(messages: list[ChatMessage]) -> int:
        """返回最后一条 user 消息的索引，即本轮对话的起点。"""
        start = 0
        for index, message in enumerate(messages):
            if message.role_value == "user":
                start = index
        return start

    @staticmethod
    def _last_user_message(messages: list[ChatMessage]) -> ChatMessage | None:
        """取最后一条 user 消息。"""
        for message in reversed(messages):
            if message.role_value == "user":
                return message
        return None

    @staticmethod
    def _called_tools(messages: list[ChatMessage]) -> list[str]:
        """取出**本轮**已经调用过的工具名。

        必须先定位最后一条 user 消息，再只看它之后的消息。
        这一点很容易写错：如果从末尾往前扫、遇到 user 就 break，
        那么在「本轮还没有任何工具调用」时会继续往前扫到**上一轮**的
        assistant 消息，把上一轮的工具误算进本轮，导致规则被整体跳过。
        """
        start = MockProvider._turn_start(messages) + 1
        names: list[str] = []
        for message in messages[start:]:
            if message.role_value == "assistant":
                names.extend(call.name for call in message.tool_calls)
        return names

    def _summarize(self, called: list[str]) -> LLMResponse:
        """所有规则都执行完后，拼一句说明做了什么的收尾话。

        不要在这里再拼接 ``_FOLLOWUP`` 里的原句——那些句子在每次工具成功后
        已经各自说过一遍了，重复拼会让回复读起来像复读机。
        """
        labels = "、".join(_STEP_LABEL.get(name, name) for name in called)
        text = f"这一轮我依次做了：{labels}。"
        if any(name in _VISION_TOOLS for name in called):
            text += "顺便说一句，我这里跑的是离线假模型，接上云端视觉模型才能说清看到的具体是什么。"
        return LLMResponse(content=text, model=self.model, finish_reason="stop")

    @staticmethod
    def _tool_name_of(messages: list[ChatMessage], tool_message: ChatMessage) -> str:
        """由 tool_call_id 反查工具名。

        优先在 assistant 消息的 tool_calls 里按 id 查——这是协议保证可靠的路径，
        因为 id 由模型生成，格式不受我们控制。只有在查不到时，
        才退回解析假模型自己那种 ``call_<n>_<tool_name>`` 形式的 id。
        """
        call_id = tool_message.tool_call_id or ""
        for message in messages:
            for call in message.tool_calls:
                if call.id == call_id:
                    return call.name

        parts = call_id.split("_", 2)
        return parts[2] if len(parts) == 3 else ""

    def _finalize(self, tool_message: ChatMessage, messages: list[ChatMessage]) -> LLMResponse:
        """把工具结果转成一句自然语言收尾。"""
        try:
            payload = json.loads(tool_message.content or "{}")
        except json.JSONDecodeError:
            payload = {}

        tool_name = self._tool_name_of(messages, tool_message)
        followup = _FOLLOWUP.get(tool_name, "好了。")

        if isinstance(payload, dict) and payload.get("ok") is False:
            error = _RESULT_NOISE.sub(" ", str(payload.get("error", "未知问题")))
            return LLMResponse(
                content=f"这个我没做成：{error}",
                model=self.model,
                finish_reason="stop",
            )

        result = payload.get("result") if isinstance(payload, dict) else payload
        if isinstance(result, dict):
            summary = result.get("summary") or result.get("note")
            if summary:
                return LLMResponse(
                    content=f"{followup}{summary}", model=self.model, finish_reason="stop"
                )
        return LLMResponse(content=followup, model=self.model, finish_reason="stop")
