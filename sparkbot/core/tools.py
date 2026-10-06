"""工具注册表：把普通 Python 函数变成 LLM 可调用的工具。

设计目标
--------
1. **零样板**：只写类型注解 + docstring，JSON Schema 自动生成，不引入额外依赖。
2. **同步异步通吃**：``def`` 和 ``async def`` 都能注册，调用方统一 ``await``。
3. **错误不外泄**：工具抛异常时打包成 ``ToolResult`` 回喂给模型，让模型自己纠正，
   而不是让整个对话轮次崩掉。
4. **按设备裁剪**：工具可以声明 ``requires``（例如 ``{"camera"}``），
   注册表在给模型列工具时会按当前设备能力过滤。

docstring 约定::

    async def drive(linear: float, angular: float = 0.0, duration_ms: int = 800) -> dict:
        \"\"\"让机器人按给定速度移动一小段时间。

        Args:
            linear: 前进速度，米/秒，负数后退。
            angular: 转向角速度，弧度/秒，正数左转。
            duration_ms: 持续时间毫秒，到点自动停下。
        \"\"\"
"""

from __future__ import annotations

import asyncio
import enum
import inspect
import logging
import re
import types
from dataclasses import dataclass, field
from typing import (
    Any,
    Callable,
    Iterable,
    Mapping,
    Sequence,
    Union,
    get_args,
    get_origin,
    get_type_hints,
)

from .errors import SparkBotError, ToolError

logger = logging.getLogger(__name__)

# --------------------------------------------------------------------------- #
# JSON Schema 生成
# --------------------------------------------------------------------------- #
_PRIMITIVES: dict[Any, str] = {
    str: "string",
    int: "integer",
    float: "number",
    bool: "boolean",
    list: "array",
    dict: "object",
}


def _json_type(annotation: Any) -> tuple[str, dict[str, Any]]:
    """把 Python 注解映射成 ``(json_type, 附加约束)``。

    只覆盖 JSON Schema 能表达的公共子集；无法识别的注解退化成 ``string``，
    并依赖模型按描述自行给出合理值。
    """
    if annotation is inspect.Parameter.empty or annotation is Any:
        return "string", {}

    # Optional[X] / X | None  -> 取非 None 分支，字段本身不进 required
    origin = get_origin(annotation)
    if origin is Union or origin is types.UnionType:
        args = [a for a in get_args(annotation) if a is not type(None)]
        if len(args) == 1:
            return _json_type(args[0])
        return "string", {}

    if origin is list or origin is Sequence:
        args = get_args(annotation)
        item_type, item_extra = _json_type(args[0]) if args else ("string", {})
        items: dict[str, Any] = {"type": item_type}
        if item_extra:
            items.update(item_extra)
        return "array", {"items": items}

    if origin is dict or origin is Mapping:
        return "object", {}

    if isinstance(annotation, type) and issubclass(annotation, enum.Enum):
        return "string", {"enum": [str(m.value) for m in annotation]}

    if annotation in _PRIMITIVES:
        return _PRIMITIVES[annotation], {}

    return "string", {}


_ARGS_SECTION = re.compile(r"^\s*(Args|Arguments|参数)\s*:\s*$", re.IGNORECASE)
_SECTION_END = re.compile(
    r"^\s*(Returns|Raises|Yields|Examples?|Note|Notes|返回|抛出|示例)\s*:\s*$", re.IGNORECASE
)
_ARG_LINE = re.compile(r"^\s{2,}(\*{0,2}\w+)\s*(?:\([^)]*\))?\s*:\s*(.*)$")


def parse_docstring(doc: str | None) -> tuple[str, dict[str, str]]:
    """拆出「摘要」与「参数说明」。

    摘要取 docstring 第一个非空段落并压成单行——它是模型判断何时调用该工具
    的唯一依据，必须精炼。
    """
    if not doc:
        return "", {}

    lines = inspect.cleandoc(doc).splitlines()

    summary_lines: list[str] = []
    for line in lines:
        if _ARGS_SECTION.match(line) or _SECTION_END.match(line):
            break
        if not line.strip():
            if summary_lines:
                break
            continue
        summary_lines.append(line.strip())
    summary = " ".join(summary_lines).strip()

    param_docs: dict[str, str] = {}
    in_args = False
    current_key: str | None = None
    for line in lines:
        if _ARGS_SECTION.match(line):
            in_args = True
            continue
        if not in_args:
            continue
        if _SECTION_END.match(line):
            break
        if not line.strip():
            continue
        match = _ARG_LINE.match(line)
        if match:
            current_key = match.group(1).lstrip("*")
            param_docs[current_key] = match.group(2).strip()
        elif current_key:
            param_docs[current_key] = f"{param_docs[current_key]} {line.strip()}".strip()

    return summary, param_docs


def build_parameters_schema(func: Callable[..., Any]) -> tuple[dict[str, Any], list[str]]:
    """由函数签名生成 ``parameters`` 对象，并返回参数名列表。

    注意用 ``typing.get_type_hints`` 而不是 ``inspect`` 上的同名函数——
    后者并不存在。这个笔误曾经被宽泛的 ``except`` 吞掉，
    导致所有参数的类型都退化成 ``string``，表现为模型把
    ``intensity: float`` 传成 ``"1.0"`` 字符串、``distance_m`` 传成文本。
    """
    try:
        hints = get_type_hints(func)
    except Exception as exc:  # noqa: BLE001 - 前向引用等异常情况
        logger.warning("解析 %s 的类型注解失败，将退化用签名注解: %s", func.__name__, exc)
        hints = {}

    signature = inspect.signature(func)
    _, param_docs = parse_docstring(func.__doc__)

    properties: dict[str, Any] = {}
    required: list[str] = []

    for name, param in signature.parameters.items():
        if name in ("self", "cls") or param.kind in (
            inspect.Parameter.VAR_POSITIONAL,
            inspect.Parameter.VAR_KEYWORD,
        ):
            continue

        annotation = hints.get(name, param.annotation)
        json_type, extra = _json_type(annotation)
        schema: dict[str, Any] = {"type": json_type}
        schema.update(extra)
        if name in param_docs:
            schema["description"] = param_docs[name]
        if param.default is not inspect.Parameter.empty:
            # 有默认值的参数一律不进 required，JSON Schema 里显式给出默认值，
            # 这样模型即使省略该参数，工具函数也能拿到文档里那个默认值。
            if param.default is not None:
                schema["default"] = param.default
        else:
            required.append(name)

        properties[name] = schema

    schema: dict[str, Any] = {"type": "object", "properties": properties, "additionalProperties": False}
    if required:
        schema["required"] = required
    return schema, list(properties)


# --------------------------------------------------------------------------- #
# 工具描述与结果
# --------------------------------------------------------------------------- #
@dataclass(slots=True)
class ToolSpec:
    """一个可被模型调用的工具。"""

    name: str
    description: str
    parameters: dict[str, Any]
    func: Callable[..., Any]
    requires: frozenset[str] = frozenset()
    """需要设备具备的能力，例如 ``{"camera"}``；空集表示不依赖设备。"""

    dangerous: bool = False
    """是否会移动或产生物理后果，供安全层与人工确认使用。"""

    @property
    def is_async(self) -> bool:
        """是否是协程函数。"""
        return inspect.iscoroutinefunction(self.func)

    def to_openai_schema(self) -> dict[str, Any]:
        """转成 OpenAI / DeepSeek ``tools`` 数组里的一项。"""
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": self.parameters,
            },
        }


@dataclass(slots=True)
class ToolResult:
    """工具执行结果。失败也是一种「正常」结果，会被回喂给模型。"""

    name: str
    ok: bool
    data: Any = None
    error: str | None = None
    error_code: str | None = None
    duration_ms: int = 0

    def to_payload(self) -> dict[str, Any]:
        """转成回喂给模型的 content 负载。"""
        if self.ok:
            return {"ok": True, "result": self.data}
        return {"ok": False, "error": self.error, "code": self.error_code}

    def to_json(self) -> str:
        """序列化成 JSON 字符串，作为 ``role=tool`` 消息正文。"""
        import json

        return json.dumps(self.to_payload(), ensure_ascii=False, default=str)


# --------------------------------------------------------------------------- #
# 注册表
# --------------------------------------------------------------------------- #
class ToolRegistry:
    """工具集合，支持装饰器注册与按能力过滤。"""

    def __init__(self) -> None:
        self._tools: dict[str, ToolSpec] = {}

    # ------------------------------------------------------------------ #
    # 注册
    # ------------------------------------------------------------------ #
    def register(
        self,
        func: Callable[..., Any] | None = None,
        *,
        name: str | None = None,
        description: str | None = None,
        requires: Iterable[str] = (),
        dangerous: bool = False,
    ) -> Any:
        """注册工具。既可用作装饰器，也可直接调用。

        Args:
            func: 被注册的函数；作为装饰器使用时由 Python 传入。
            name: 覆盖工具名，默认用函数名。
            description: 覆盖描述，默认取 docstring 摘要。
            requires: 需要的设备能力列表。
            dangerous: 是否标记为有物理后果。
        """

        def decorator(target: Callable[..., Any]) -> Callable[..., Any]:
            summary, _ = parse_docstring(target.__doc__)
            parameters, _ = build_parameters_schema(target)
            spec = ToolSpec(
                name=name or target.__name__,
                description=description or summary or target.__name__,
                parameters=parameters,
                func=target,
                requires=frozenset(requires),
                dangerous=dangerous,
            )
            if spec.name in self._tools:
                raise ToolError(f"工具名重复注册: {spec.name}")
            self._tools[spec.name] = spec
            return target

        return decorator(func) if func is not None else decorator

    # ------------------------------------------------------------------ #
    # 查询
    # ------------------------------------------------------------------ #
    def get(self, name: str) -> ToolSpec:
        """按名字取工具，不存在则抛 :class:`ToolError`。"""
        try:
            return self._tools[name]
        except KeyError as exc:
            raise ToolError(f"未知工具: {name}") from exc

    def __contains__(self, name: object) -> bool:
        return name in self._tools

    def __len__(self) -> int:
        return len(self._tools)

    @property
    def names(self) -> list[str]:
        """已注册的全部工具名。"""
        return sorted(self._tools)

    def select(self, capabilities: Iterable[str] | None = None) -> list[ToolSpec]:
        """按当前设备能力筛出可用工具。

        Args:
            capabilities: 设备上报的能力集合；``None`` 表示不过滤（用于自检）。
        """
        if capabilities is None:
            return list(self._tools.values())
        available = set(capabilities)
        return [spec for spec in self._tools.values() if spec.requires <= available]

    def openai_schemas(self, capabilities: Iterable[str] | None = None) -> list[dict[str, Any]]:
        """生成可直接放进请求体的 ``tools`` 数组。"""
        return [spec.to_openai_schema() for spec in self.select(capabilities)]

    # ------------------------------------------------------------------ #
    # 执行
    # ------------------------------------------------------------------ #
    async def execute(self, name: str, arguments: Mapping[str, Any] | None = None) -> ToolResult:
        """执行工具，永不抛异常——任何失败都转成 ``ToolResult(ok=False)``。

        这样模型能看到错误原因并换个做法重试，而不是让整轮对话失败。
        """
        import time

        args = dict(arguments or {})
        started = time.perf_counter()
        try:
            spec = self.get(name)
        except ToolError as exc:
            return ToolResult(
                name=name,
                ok=False,
                error=str(exc),
                error_code="unknown_tool",
                duration_ms=int((time.perf_counter() - started) * 1000),
            )

        try:
            if spec.is_async:
                data = await spec.func(**args)
            else:
                # 同步工具丢到线程池，避免阻塞事件循环（推流/串口等会阻塞）。
                data = await asyncio.to_thread(spec.func, **args)
            return ToolResult(
                name=name,
                ok=True,
                data=data,
                duration_ms=int((time.perf_counter() - started) * 1000),
            )
        except TypeError as exc:
            # 参数名/数量不对是最常见的模型犯错方式，给出明确提示。
            return ToolResult(
                name=name,
                ok=False,
                error=f"参数不匹配: {exc}。期望参数: {sorted(spec.parameters.get('properties', {}))}",
                error_code="bad_arguments",
                duration_ms=int((time.perf_counter() - started) * 1000),
            )
        except SparkBotError as exc:
            return ToolResult(
                name=name,
                ok=False,
                error=exc.message,
                error_code=type(exc).__name__,
                duration_ms=int((time.perf_counter() - started) * 1000),
            )
        except Exception as exc:  # noqa: BLE001 - 工具边界必须吞掉一切
            return ToolResult(
                name=name,
                ok=False,
                error=f"{type(exc).__name__}: {exc}",
                error_code="internal",
                duration_ms=int((time.perf_counter() - started) * 1000),
            )
