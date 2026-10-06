"""视觉理解：把一帧图像交给多模态模型，取回结构化描述。

为什么复用 :class:`LLMProvider` 而不是另写一套视觉 SDK：
看图本质上就是「一条带图片的对话」，复用同一接口意味着
切 provider、加重试、统计用量这些逻辑只需要维护一份。
"""

from __future__ import annotations

import base64
import binascii
import io
import logging
import re
from dataclasses import dataclass, field
from typing import Any

from ..config import LLMSettings, VisionSettings
from ..core.errors import ProviderError
from ..llm.base import ChatMessage, LLMProvider, create_provider, resolve_vision_config

logger = logging.getLogger(__name__)


# --------------------------------------------------------------------------- #
# 结构化结果
# --------------------------------------------------------------------------- #
@dataclass(slots=True)
class Detection:
    """一个被识别到的物体。"""

    label: str
    confidence: float | None = None
    position: str | None = None
    """画面中的方位描述，例如「左侧」「正前方」；由模型用自然语言给出。"""

    def to_dict(self) -> dict[str, Any]:
        """转成给模型/前端看的字典。"""
        payload: dict[str, Any] = {"label": self.label}
        if self.confidence is not None:
            payload["confidence"] = self.confidence
        if self.position:
            payload["position"] = self.position
        return payload


@dataclass(slots=True)
class VisionResult:
    """一次视觉分析的结果。"""

    description: str
    objects: list[Detection] = field(default_factory=list)
    scene: str = ""
    model: str = ""
    mocked: bool = False

    def to_dict(self) -> dict[str, Any]:
        """转成给模型看的字典。"""
        return {
            "description": self.description,
            "objects": [o.to_dict() for o in self.objects],
            "scene": self.scene,
            "model": self.model,
            "mocked": self.mocked,
        }


# --------------------------------------------------------------------------- #
# 图像预处理
# --------------------------------------------------------------------------- #
def _downscale(data: bytes, max_edge: int) -> tuple[bytes, str]:
    """把图像缩到最长边不超过 ``max_edge``，返回 ``(字节, mime)``。

    缩图是**省钱的关键**：VLM 的视觉 token 与像素数正相关，
    640x480 的原图缩到 768 以内通常能省一半以上成本，且不损失判别力。
    Pillow 不可用时原样返回，功能降级但不断链。
    """
    try:
        from PIL import Image
    except ImportError:
        logger.debug("未安装 Pillow，跳过图像缩放")
        return data, "image/jpeg"

    try:
        image = Image.open(io.BytesIO(data))
    except Exception as exc:  # noqa: BLE001 - 损坏图片不应崩掉对话
        logger.warning("无法解析图像，按原样发送: %s", exc)
        return data, "image/jpeg"

    width, height = image.size
    longest = max(width, height)
    if longest > max_edge:
        scale = max_edge / float(longest)
        image = image.resize((max(1, int(width * scale)), max(1, int(height * scale))))

    if image.mode not in ("RGB", "L"):
        image = image.convert("RGB")

    buffer = io.BytesIO()
    image.save(buffer, format="JPEG", quality=85, optimize=True)
    return buffer.getvalue(), "image/jpeg"


def to_data_uri(data: bytes, *, mime: str = "image/jpeg") -> str:
    """把图像字节编码成 data URI。"""
    return f"data:{mime};base64,{base64.b64encode(data).decode('ascii')}"


def strip_data_uri(uri: str) -> tuple[bytes, str]:
    """把 data URI 还原成 ``(字节, mime)``；非 data URI 原样抛错。"""
    match = re.match(r"^data:(?P<mime>[^;]+);base64,(?P<payload>.*)$", uri, re.DOTALL)
    if not match:
        raise ValueError("不是合法的 data URI")
    try:
        return base64.b64decode(match.group("payload"), validate=True), match.group("mime")
    except (binascii.Error, ValueError) as exc:
        raise ValueError(f"data URI 内容不是合法 base64: {exc}") from exc


# --------------------------------------------------------------------------- #
# 提示词
# --------------------------------------------------------------------------- #
_COMMON_RULES = (
    "你是一台移动机器人的视觉系统。回答必须基于画面中真实存在的内容，"
    "看不清就说看不清，绝不许猜测或编造。"
    "回答用中文，简洁直接，不要寒暄。"
)

_DETECT_PROMPT = (
    f"{_COMMON_RULES}\n\n"
    "请识别画面中的主要物体，并按以下格式输出：\n"
    "场景: <一句话描述整体环境>\n"
    "物体: <名称1>|<方位>|<置信度0-1>; <名称2>|<方位>|<置信度0-1>\n"
    "描述: <两三句话说明你看到了什么，重点说明与机器人导航相关的信息，例如障碍物、人、可通行方向>"
)

_DESCRIBE_PROMPT = (
    f"{_COMMON_RULES}\n\n"
    "用两三句话描述你看到的画面，并指出对一台正在移动的机器人最重要的信息"
    "（障碍物、人、地面状况、可前进方向）。"
)

_ASK_TEMPLATE = (
    f"{_COMMON_RULES}\n\n"
    "请看着画面回答这个问题：{question}\n"
    "直接给答案，不要重复问题。"
)

_OBJECT_LINE = re.compile(r"物体\s*:\s*(.+)")
_SCENE_LINE = re.compile(r"场景\s*:\s*(.+)")
_DESC_LINE = re.compile(r"描述\s*:\s*(.+)")


class VisionAnalyzer:
    """基于多模态模型的视觉分析器。"""

    def __init__(
        self,
        *,
        provider: LLMProvider,
        settings: VisionSettings,
        mocked: bool = False,
    ) -> None:
        self.provider = provider
        self.settings = settings
        self.mocked = mocked or provider.name == "mock"

    # ------------------------------------------------------------------ #
    # 工厂
    # ------------------------------------------------------------------ #
    @classmethod
    def from_settings(cls, llm: LLMSettings, vision: VisionSettings) -> VisionAnalyzer:
        """按配置构造；视觉 provider 未单独配置时复用主 LLM 的设置。"""
        resolved = resolve_vision_config(llm, vision)
        provider = create_provider(resolved, purpose="vision")

        if not provider.supports_vision:
            logger.warning(
                "provider %s 不支持图像输入，视觉能力将退化为占位描述", provider.name
            )
        return cls(provider=provider, settings=resolved, mocked=provider.name == "mock")

    # ------------------------------------------------------------------ #
    # 主入口
    # ------------------------------------------------------------------ #
    async def _ask_with_image(self, image: str, prompt: str) -> str:
        """带图问一句话，返回模型文本。

        Args:
            image: 已经处理好的 data URI。
            prompt: 指令文本。
        """
        message = ChatMessage.user(prompt, images=[image])
        response = await self.provider.chat(
            [message],
            tools=None,  # 视觉阶段不需要工具，省 token 也更稳定
            temperature=0.2,  # 描述类任务要稳定，压低随机性
            max_tokens=512,
        )
        return (response.content or "").strip()

    async def _prepare(self, image: bytes | str) -> str:
        """把输入统一成缩放后的 data URI。"""
        if isinstance(image, str):
            if image.startswith("data:"):
                raw, _ = strip_data_uri(image)
            else:
                # 公网 URL 直接交给模型，避免本机下载。
                return image
        else:
            raw = image

        scaled, mime = _downscale(raw, self.settings.downscale_max_edge)
        return to_data_uri(scaled, mime=mime)

    async def detect(
        self, image: bytes | str, *, prompt: str | None = None
    ) -> VisionResult:
        """识别画面中的物体，返回结构化结果。

        Args:
            image: JPEG/PNG 字节，或 data URI，或可公网访问的图片 URL。
            prompt: 覆盖默认的识别指令（用于特殊场景）。
        """
        data_uri = await self._prepare(image)
        text = await self._ask_with_image(data_uri, prompt or _DETECT_PROMPT)
        return self._parse(text)

    async def describe(self, image: bytes | str) -> VisionResult:
        """用一小段话描述画面，不要求结构化输出。"""
        data_uri = await self._prepare(image)
        text = await self._ask_with_image(data_uri, _DESCRIBE_PROMPT)
        return VisionResult(description=text, model=getattr(self.provider, "model", ""), mocked=self.mocked)

    async def ask(self, image: bytes | str, question: str) -> VisionResult:
        """就画面回答一个具体问题，例如「桌上有什么颜色的杯子」。"""
        data_uri = await self._prepare(image)
        text = await self._ask_with_image(data_uri, _ASK_TEMPLATE.format(question=question))
        return VisionResult(description=text, model=getattr(self.provider, "model", ""), mocked=self.mocked)

    # ------------------------------------------------------------------ #
    # 解析
    # ------------------------------------------------------------------ #
    @staticmethod
    def _parse(text: str) -> VisionResult:
        """解析「场景/物体/描述」三行式输出。

        模型偶尔不守格式，所以每一行都是**可选**的：
        全都解析不出来时，整段文本直接当描述用，绝不因为格式而丢结果。
        """
        scene = ""
        description = text
        objects: list[Detection] = []

        for line in text.splitlines():
            stripped = line.strip()
            if not stripped:
                continue
            if match := _SCENE_LINE.match(stripped):
                scene = match.group(1).strip()
                continue
            if match := _OBJECT_LINE.match(stripped):
                objects.extend(VisionAnalyzer._parse_objects(match.group(1)))
                continue
            if match := _DESC_LINE.match(stripped):
                description = match.group(1).strip()
                continue

        # 没有「描述:」行时，把场景之外的原文当描述。
        if description == text and scene:
            leftover = [
                ln.strip()
                for ln in text.splitlines()
                if ln.strip() and not _SCENE_LINE.match(ln.strip()) and not _OBJECT_LINE.match(ln.strip())
            ]
            if leftover:
                description = " ".join(leftover)

        return VisionResult(description=description.strip(), objects=objects, scene=scene)

    @staticmethod
    def _parse_objects(segment: str) -> list[Detection]:
        """解析 ``名称|方位|置信度`` 形式的物体列表，容忍缺字段与杂质。"""
        detections: list[Detection] = []
        for item in segment.split(";"):
            item = item.strip()
            if not item:
                continue
            parts = [p.strip() for p in item.split("|")]
            label = parts[0].lstrip("-•* ").strip()
            if not label:
                continue

            confidence: float | None = None
            position: str | None = None
            for extra in parts[1:]:
                if not extra:
                    continue
                try:
                    value = float(extra)
                except ValueError:
                    position = position or extra
                else:
                    # 模型偶尔给百分数，例如 85 而非 0.85。
                    confidence = value / 100.0 if value > 1.0 else value
            detections.append(
                Detection(
                    label=label,
                    confidence=None if confidence is None else max(0.0, min(1.0, confidence)),
                    position=position,
                )
            )
        return detections

    async def aclose(self) -> None:
        """释放 provider 的网络资源。"""
        await self.provider.aclose()
