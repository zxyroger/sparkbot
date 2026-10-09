"""Agent 工具集：机器人「身体」与「感官」暴露给模型的能力面。

设计要点
--------
**工具描述就是提示词工程的主战场。** 模型只看得到工具名、描述和参数字段，
所以每个 docstring 的第一句都要回答「什么时候该用它」，
参数说明要写清单位、方向和取值范围。

**返回值一律结构化。** 模型需要能读懂结果再决定下一步，
所以每个工具都返回带 ``summary`` 的字典——一句话总结比一堆字段更有效。

**失败不抛异常。** 工具内捕获可预期错误并返回 ``{"ok": False, ...}``，
让模型自行换策略；只有编程错误才交给注册表兜底。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

from ..config import Settings
from ..core.errors import DeviceError, SparkBotError
from ..core.tools import ToolRegistry
from ..device.capabilities import (
    CAP_CAMERA,
    CAP_DISPLAY,
    CAP_MICROPHONE,
    CAP_MOTOR,
    CAP_SPEAKER,
    Robot,
    RobotProvider,
)
from ..device.protocol import Emotion
from ..perception.speech import ASRProvider, TTSProvider
from ..perception.vision import VisionAnalyzer

logger = logging.getLogger(__name__)

#: 角度转弧度的换算，转向类工具对模型暴露「度」比「弧度」直观得多。
DEG_TO_RAD = 3.141592653589793 / 180.0


@dataclass(slots=True)
class ToolContext:
    """工具执行所需的一切外部依赖。

    工具函数本身不带状态，全部依赖通过这个上下文注入，
    因此换设备、换视觉模型、乃至在测试里塞假实现都不需要改工具代码。
    """

    robots: RobotProvider
    vision: VisionAnalyzer | None
    asr: ASRProvider | None
    tts: TTSProvider | None
    settings: Settings
    #: 长期记忆库。为 None 表示未启用，相关工具会明确告知模型"记不住"，
    #: 而不是默默假装记住了。
    long_term: Any = None
    #: 人脸库（名字 ↔ 512 维特征）。为 None 表示人脸识别未启用。
    #: 设备端只做推理，认人和绑名都在 PC 侧 —— 见 perception/face.py。
    face_db: Any = None

    def robot(self, device_id: str | None = None) -> Robot:
        """解析目标机器人。

        Raises:
            DeviceError: 没有在线设备。
        """
        return self.robots.get(device_id)


# --------------------------------------------------------------------------- #
# 内部辅助
# --------------------------------------------------------------------------- #
def _failure(message: str, **extra: Any) -> dict[str, Any]:
    """构造统一的失败返回体。"""
    payload: dict[str, Any] = {"ok": False, "error": message}
    payload.update(extra)
    return payload


def _clamp(value: float, low: float, high: float) -> float:
    """把数值夹到区间内。"""
    return max(low, min(high, value))


# --------------------------------------------------------------------------- #
# 注册
# --------------------------------------------------------------------------- #
def build_registry(ctx: ToolContext) -> ToolRegistry:
    """构造并返回本机器人的全部工具。

    这里用闭包而不是类方法，是为了让每个工具函数保持无状态、可单独测试。
    """
    registry = ToolRegistry()

    # ================================================================== #
    # 视觉
    # ================================================================== #
    @registry.register(requires={CAP_CAMERA}, dangerous=False)
    async def look_around(question: str = "", device_id: str = "") -> dict[str, Any]:
        """用摄像头看眼前的情况，返回识别到的物体和场景描述。

        这是你了解周围环境的主要方式。需要知道前面有什么、有没有障碍物、
        有没有人、能不能往前走时，都应该调用它，而不是凭猜测回答。

        Args:
            question: 想特别确认的问题，例如「桌上有没有水杯」。留空则做通用的场景描述。
            device_id: 目标机器人 id；只有一台机器人时留空即可。
        """
        try:
            robot = ctx.robot(device_id or None)
            look = await robot.look()
        except SparkBotError as exc:
            return _failure(exc.message)

        if ctx.vision is None:
            return {
                "ok": True,
                "summary": f"拍到一帧画面（{look.frame.approx_kb} KB），但没有可用的视觉模型，无法识别内容。",
                "vision_available": False,
            }

        try:
            if question.strip():
                result = await ctx.vision.ask(look.frame.data, question.strip())
            else:
                result = await ctx.vision.detect(look.frame.data)
        except SparkBotError as exc:
            return _failure(f"视觉分析失败: {exc.message}", image_captured=True)

        objects = [obj.to_dict() for obj in result.objects]
        if result.description:
            summary = result.description
        elif objects:
            summary = "看到：" + "、".join(obj["label"] for obj in objects)
        else:
            summary = "画面里没有识别出明确的物体。"

        return {
            "ok": True,
            "summary": summary,
            "description": result.description,
            "scene": result.scene,
            "objects": objects,
            "show_camera": True,
            "vision_available": True,
        }

    @registry.register(requires={CAP_CAMERA})
    async def capture_photo(device_id: str = "") -> dict[str, Any]:
        """抓拍一张照片存在本地，不做识别。用于用户明确要求「拍张照」时。

        Args:
            device_id: 目标机器人 id；只有一台机器人时留空即可。
        """
        try:
            robot = ctx.robot(device_id or None)
            look = await robot.look()
        except SparkBotError as exc:
            return _failure(exc.message)

        import base64
        import time

        # 存盘便于事后回看抓到的到底是什么画面。
        try:
            from pathlib import Path

            out_dir = Path(ctx.settings.artifacts_dir) / "frames"
            out_dir.mkdir(parents=True, exist_ok=True)
            path = out_dir / f"{robot.device_id}-{int(time.time())}.jpg"
            path.write_bytes(look.frame.data)
            saved = str(path)
        except OSError as exc:
            logger.warning("抓拍存盘失败: %s", exc)
            saved = ""

        return {
            "ok": True,
            "summary": f"已拍下一张照片（{look.frame.approx_kb} KB）。",
            "saved_to": saved,
            "width": look.width,
            "height": look.height,
            "frame_b64": base64.b64encode(look.frame.data).decode("ascii"),
            "show_camera": True,
        }

    # ================================================================== #
    # 底盘运动
    # ================================================================== #
    # ================================================================== #
    # 人脸（设备端本地推理 + PC 端名字绑定）
    # ================================================================== #
    # 推理跑在 ESP32-S3 上（esp-dl），设备只回 512 维特征；"这是谁"由
    # PC 侧的人脸库回答。见 perception/face.py 与 sparkbot-esp32/main/bot_face_rec.h。

    @registry.register(requires={CAP_CAMERA}, dangerous=False)
    async def who_is_here(device_id: str = "") -> dict[str, Any]:
        """看一眼面前有谁，返回已登记的人的名字。

        什么时候该用：
        * 用户问「你看到谁了」「我是谁」「你还认得我吗」；
        * 听到有人说话、想知道是谁在说话。

        重要：它只能回答"像不像已登记的某个人"。认不出时必须老实说
        「没认出来」，**不许**根据长相猜身份或编一个名字。

        Args:
            device_id: 目标机器人 id；只有一台机器人时留空即可。
        """
        if ctx.face_db is None:
            return _failure("人脸识别未启用，认不出面前是谁。")
        try:
            robot = ctx.robot(device_id or None)
            scan = await robot.identify_faces()
        except SparkBotError as exc:
            return _failure(exc.message)

        if not scan.faces:
            if scan.too_dark:
                return {
                    "ok": True,
                    "count": 0,
                    "too_dark": True,
                    "mean_luma": scan.mean_luma,
                    "summary": (
                        f"画面太暗（平均亮度 {scan.mean_luma}/255），看不清有没有人。"
                        "要提醒用户把灯打开或调整光线，别直接说「没人」。"
                    ),
                    "faces": [],
                }
            if scan.dropped:
                return _failure(
                    f"设备检测到 {scan.reported_count} 张脸，但人脸特征数据不完整，认不出是谁"
                    "（通常是固件与 PC 端版本不匹配）"
                )
            return {
                "ok": True,
                "count": 0,
                "too_dark": False,
                "mean_luma": scan.mean_luma,
                "summary": "画面里没有看到人脸。",
                "faces": [],
            }

        matches = ctx.face_db.match_scan(scan.faces)
        known = [m for m in matches if m.known]
        if known:
            top = known[0]
            extra = len(matches) - len(known)
            summary = f"最像「{top.name}」（相似度 {top.similarity:.2f}）"
            if extra > 0:
                summary += f"，另外还有 {extra} 张没登记过的脸"
            summary += "。"
        else:
            summary = f"看到 {len(matches)} 张脸，但都不认识（人脸库为空或都不像）。"

        return {
            "ok": True,
            "count": len(scan.faces),
            "mean_luma": scan.mean_luma,
            "summary": summary,
            "faces": [m.to_dict() for m in matches],
            "known_names": [m.name for m in known],
        }

    @registry.register(requires={CAP_CAMERA}, dangerous=False)
    async def bind_face(name: str, device_id: str = "") -> dict[str, Any]:
        """把眼前这张脸和身份绑起来，以后就能认出他（姓名，或「小明的爸爸」这类称呼都行）。

        什么时候该用：用户自我介绍之后（「我叫张伟」「我是李工」），
        或用户明确要求「记住我的脸」「把这张脸绑到 XX 上」。

        注意工具描述只取本 docstring 的**第一段**，所以"关系称呼也算身份"
        这点必须写在第一段里（不然模型看不到，只会绑姓名）。

        ``name`` 不一定是姓名，**关系称呼同样有效**，而且往往更自然：
        「小明的爸爸」「王阿姨」「我的同事小李」。机器人需要的只是一个
        稳定的称呼，用来把这张脸和之前聊过的内容对上号。
        用**用户自己说的说法**，不要改写成别的词。

        绑定前请让对方面向摄像头；画面里有多个人时，绑的是**离得最近**的
        那个（框最大）。如果画面里没人脸，会明确失败，请让对方靠近再试。

        Args:
            name: 要绑定的姓名或称呼，例如「张伟」「小明的爸爸」。
            device_id: 目标机器人 id；只有一台机器人时留空即可。
        """
        if ctx.face_db is None:
            return _failure("人脸识别未启用，绑不了。")
        cleaned = (name or "").strip()
        if not cleaned:
            return _failure("没给名字，绑不了。")

        try:
            robot = ctx.robot(device_id or None)
            scan = await robot.identify_faces()
        except SparkBotError as exc:
            return _failure(exc.message)

        if not scan.faces:
            if scan.too_dark:
                return _failure(
                    f"画面太暗（平均亮度 {scan.mean_luma}/255），看不清脸；"
                    "请先开灯或让光线照到镜头这一侧再试。"
                )
            return _failure("这一帧里没有检测到人脸，请让对方面向摄像头再试。")

        face = max(
            scan.faces,
            key=lambda f: max(0, f.box[2] - f.box[0]) * max(0, f.box[3] - f.box[1]),
        )
        entry = ctx.face_db.enroll(cleaned, face.feat, source="tool")
        if entry is None:
            return _failure(f"「{cleaned}」不是有效的名字，或人脸特征不完整。")

        # 人脸和姓名一起写进长期记忆：下次即使没拍到脸，也还能凭名字
        # 和之前聊过的内容认出这个人。
        if ctx.long_term is not None:
            try:
                ctx.long_term.remember(
                    f"用户叫{entry['name']}（已绑定人脸）", importance=5, source="face"
                )
            except Exception:  # noqa: BLE001 - 记忆写失败不影响绑定本身
                logger.debug("人脸绑定后写长期记忆失败", exc_info=True)

        return {
            "ok": True,
            "name": entry["name"],
            "samples": entry["samples"],
            "summary": f"已经记住「{entry['name']}」的脸（累计 {entry['samples']} 条特征）。",
        }

    @registry.register(dangerous=False)
    async def forget_face(name: str) -> dict[str, Any]:
        """删掉某个人的人脸绑定，以后就认不出他了。

        什么时候该用：用户要求「把我的脸删掉」「别再认我了」「忘记 XX 的脸」。
        这是用户对自己生物特征的控制权，应当直接照做，不要劝阻、不要反问。

        Args:
            name: 要删除的人名。
        """
        if ctx.face_db is None:
            return _failure("人脸识别未启用。", removed=0)
        target = (name or "").strip()
        if not ctx.face_db.remove(target):
            return {
                "ok": True,
                "removed": 0,
                "summary": f"人脸库里没有「{target}」这个人。",
            }
        return {"ok": True, "removed": 1, "summary": f"已删掉「{target}」的人脸绑定。"}

    # ================================================================== #
    # 底盘运动
    # ================================================================== #
    @registry.register(requires={CAP_MOTOR}, dangerous=True)
    async def move_forward(distance_m: float = 0.3, device_id: str = "") -> dict[str, Any]:
        """让机器人向前移动一段距离。

        只应在确认前方没有障碍物时使用；如果还没看过前方，先调用 look_around。

        Args:
            distance_m: 前进距离，单位米，范围 0.05~3.0。0.3 米约等于一小步。
            device_id: 目标机器人 id；只有一台机器人时留空即可。
        """
        distance = _clamp(float(distance_m), 0.05, 3.0)
        try:
            robot = ctx.robot(device_id or None)
            result = await robot.forward(distance)
        except SparkBotError as exc:
            return _failure(exc.message)
        return {"ok": True, "summary": result.to_dict()["summary"], "distance_m": distance}

    @registry.register(requires={CAP_MOTOR}, dangerous=True)
    async def move_backward(distance_m: float = 0.3, device_id: str = "") -> dict[str, Any]:
        """让机器人向后倒退一段距离。

        用于退出狭窄空间或拉开与障碍物的距离。

        Args:
            distance_m: 后退距离，单位米，范围 0.05~2.0。
            device_id: 目标机器人 id；只有一台机器人时留空即可。
        """
        distance = _clamp(float(distance_m), 0.05, 2.0)
        try:
            robot = ctx.robot(device_id or None)
            result = await robot.forward(-distance)
        except SparkBotError as exc:
            return _failure(exc.message)
        return {"ok": True, "summary": result.to_dict()["summary"], "distance_m": distance}

    @registry.register(requires={CAP_MOTOR}, dangerous=True)
    async def turn_left(degrees: float = 45.0, device_id: str = "") -> dict[str, Any]:
        """原地向左转（逆时针）指定角度。

        Args:
            degrees: 转向角度，单位度，范围 5~360。90 度约等于转向正左方。
            device_id: 目标机器人 id；只有一台机器人时留空即可。
        """
        return await _turn(ctx, degrees, left=True, device_id=device_id)

    @registry.register(requires={CAP_MOTOR}, dangerous=True)
    async def turn_right(degrees: float = 45.0, device_id: str = "") -> dict[str, Any]:
        """原地向右转（顺时针）指定角度。

        Args:
            degrees: 转向角度，单位度，范围 5~360。90 度约等于转向正右方。
            device_id: 目标机器人 id；只有一台机器人时留空即可。
        """
        return await _turn(ctx, degrees, left=False, device_id=device_id)

    @registry.register(requires={CAP_MOTOR}, dangerous=True)
    async def stop_moving(device_id: str = "") -> dict[str, Any]:
        """立即让机器人停止移动。

        任何感觉不安全、可能撞到东西、或用户喊停的时候，第一反应都应该是调用它。

        Args:
            device_id: 目标机器人 id；只有一台机器人时留空即可。
        """
        try:
            robot = ctx.robot(device_id or None)
            await robot.stop()
        except SparkBotError as exc:
            return _failure(exc.message)
        return {"ok": True, "summary": "已经停下来了。"}

    # ================================================================== #
    # 表达
    # ================================================================== #
    @registry.register(requires={CAP_DISPLAY})
    async def show_emotion(emotion: str = "happy", intensity: float = 1.0, device_id: str = "") -> dict[str, Any]:
        """在屏幕上显示表情，用来表达情绪。

        对话中应该经常使用它，让机器人显得有生命力。例如被表扬时开心，
        被批评时难过，听到意外消息时惊讶。

        Args:
            emotion: 表情名，可选 neutral、happy、sad、angry、surprised、sleepy、confused、thinking、love、excited、scared、bored。
            intensity: 表情强度，0.0~1.0，默认 1.0 表示最明显。
            device_id: 目标机器人 id；只有一台机器人时留空即可。
        """
        try:
            robot = ctx.robot(device_id or None)
            result = await robot.set_face(emotion, intensity=float(intensity))
        except SparkBotError as exc:
            return _failure(exc.message)
        return {"ok": True, "summary": f"表情已切换为 {result['emotion']}。", "emotion": result["emotion"]}

    @registry.register(requires={CAP_DISPLAY})
    async def show_text(text: str, duration_ms: int = 2500, device_id: str = "") -> dict[str, Any]:
        """在屏幕上显示一行文字，例如识别结果、提示信息或想强调的关键词。

        Args:
            text: 要显示的文字，建议不超过 20 个字，太长会挤在一起。
            duration_ms: 显示时长毫秒，默认 2500。
            device_id: 目标机器人 id；只有一台机器人时留空即可。
        """
        try:
            robot = ctx.robot(device_id or None)
            await robot.set_text(text, duration_ms=int(duration_ms))
        except SparkBotError as exc:
            return _failure(exc.message)
        return {"ok": True, "summary": f"屏幕已显示「{text[:20]}」。"}

    @registry.register(requires={CAP_SPEAKER})
    async def beep(times: int = 1, device_id: str = "") -> dict[str, Any]:
        """让喇叭发出一声短促提示音。

        用于「我听到了」「我准备开始动了」这类即时反馈，比说话更快更省流量。

        Args:
            times: 响几声，1~3。
            device_id: 目标机器人 id；只有一台机器人时留空即可。
        """
        count = int(_clamp(float(times), 1, 3))
        try:
            robot = ctx.robot(device_id or None)
            for _ in range(count):
                await robot.play_tone()
        except SparkBotError as exc:
            return _failure(exc.message)
        return {"ok": True, "summary": f"响了 {count} 声提示音。"}

    # ================================================================== #
    # 语音
    # ================================================================== #
    @registry.register(requires={CAP_SPEAKER})
    async def speak(text: str, device_id: str = "") -> dict[str, Any]:
        """额外朗读一段文字（**不是**你的回复 —— 回复系统会自动播报）。

        什么时候该用：只有需要主动说一句**回复之外**的内容时，例如把识别到的
        一段文字朗读出来。
        什么时候**不要**用：为了让机器人说出你的回复 —— 回复本来就会被自动
        播报，再调一次等于同一句话说两遍，而且每次合成+播放要好几秒。

        Args:
            text: 要说的内容，建议不超过 100 字。
            device_id: 目标机器人 id；只有一台机器人时留空即可。
        """
        if ctx.tts is None:
            return _failure("没有可用的语音合成服务")

        cleaned = text.strip()[:200]
        if not cleaned:
            return _failure("要说的内容为空")

        try:
            audio, fmt = await ctx.tts.synthesize(cleaned)
        except SparkBotError as exc:
            return _failure(f"语音合成失败: {exc.message}")
        if not audio:
            return _failure("语音合成返回了空音频")

        try:
            robot = ctx.robot(device_id or None)
            await robot.say(audio, fmt=fmt)
        except SparkBotError as exc:
            return _failure(f"播放失败: {exc.message}")
        return {"ok": True, "summary": f"已经说出了「{cleaned[:30]}」。", "spoken": cleaned}

    @registry.register(requires={CAP_MICROPHONE})
    async def listen(max_seconds: float = 6.0, device_id: str = "") -> dict[str, Any]:
        """打开麦克风听用户说话并返回识别文字 —— 只在用户明确要求「你来听」时才用。

        用户说的话**系统已经自动转成文字给你了**，所以：
        * 不要为了「听清一点」「确认能不能听见」「再听一遍」调用它 ——
          一次要采集 5~6 秒，用户干等；实测这是响应变慢的最大来源；
        * 只有用户说「听我说」「你来听」这类**明确要求采集**时才调用。

        Args:
            max_seconds: 最长采集时长秒数，1~15。
            device_id: 目标机器人 id；只有一台机器人时留空即可。
        """
        if ctx.asr is None:
            return _failure("没有可用的语音识别服务")

        seconds = _clamp(float(max_seconds), 1.0, 15.0)
        try:
            robot = ctx.robot(device_id or None)
            pcm = await robot.collect_audio(
                max_seconds=seconds,
                silence_timeout_s=ctx.settings.speech.listen_timeout_s,
            )
        except SparkBotError as exc:
            return _failure(exc.message)

        if not pcm:
            return {"ok": True, "summary": "没有采集到声音。", "text": ""}

        try:
            transcript = await ctx.asr.transcribe(
                pcm, sample_rate=ctx.settings.speech.input_sample_rate
            )
        except SparkBotError as exc:
            return _failure(f"语音识别失败: {exc.message}", pcm_bytes=len(pcm))

        if not transcript.text:
            return {"ok": True, "summary": "听到了声音但没听清内容。", "text": ""}
        return {
            "ok": True,
            "summary": f"用户说：「{transcript.text}」",
            "text": transcript.text,
            "duration_s": transcript.duration_s,
        }

    # ================================================================== #
    # 状态
    # ================================================================== #
    @registry.register()
    async def get_status(device_id: str = "") -> dict[str, Any]:
        """查询机器人的电量、运动状态和可用能力。

        当被问到「你还好吗」「电量多少」「你现在在做什么」时调用。

        Args:
            device_id: 目标机器人 id；只有一台机器人时留空即可。
        """
        try:
            robot = ctx.robot(device_id or None)
        except SparkBotError as exc:
            # 没有在线设备也是一种有效状态，明确告诉模型而不是报错。
            return {
                "ok": True,
                "online": False,
                "summary": "当前没有机器人在线。",
                "error": exc.message,
            }

        status = robot.status()
        battery = status.get("battery_percent")
        parts = [f"设备 {status['name']} 在线"]
        if battery is not None:
            parts.append(f"电量 {battery}%")
        motion = status.get("motion") or {}
        if motion.get("linear") or motion.get("angular"):
            parts.append(f"正在移动（线速度 {motion.get('linear', 0):.2f} m/s）")
        else:
            parts.append("当前静止")
        status["online"] = True
        status["summary"] = "，".join(parts) + "。"
        return status

    # ================================================================== #
    # 长期记忆
    # ================================================================== #
    # 这三个工具**不需要设备能力**（requires 留空）：即使机器人没上线，
    # 用户也可以跟它聊自己的事，记忆照样要生效。

    @registry.register(dangerous=False)
    async def remember(content: str, importance: int = 3) -> dict[str, Any]:
        """把关于用户的重要信息长期记住，下次对话仍然记得。

        什么时候该用：用户告知与自己有关、以后可能还会用到的事实时，
        例如名字、称呼、喜好、习惯、过敏或禁忌、家人、工作、约定。
        不要用来记录闲聊内容、临时的请求、或机器人自己的状态。

        Args:
            content: 要记住的事实，写成一句陈述句，例如「用户叫张伟」。
                必须自包含——脱离当前对话也能看懂，不要包含「他」「刚才」这类指代。
            importance: 重要程度 1~5。安全相关信息（过敏、禁忌）用 5，
                身份与长期偏好用 4，一般习惯用 3。
        """
        mem = ctx.long_term
        if mem is None:
            return _failure("长期记忆未启用，这次记不住。", remembered=False)

        fact = mem.remember(content, importance=importance, source="agent")
        if fact is None:
            return _failure("内容为空，没有记住任何东西。", remembered=False)
        return {
            "ok": True,
            "remembered": True,
            "id": fact.id,
            "content": fact.content,
            "summary": f"已记住：{fact.content}",
        }

    @registry.register(dangerous=False)
    async def recall(keywords: str = "") -> dict[str, Any]:
        """检索长期记忆，查看自己记住了关于用户什么。

        什么时候该用：
        * 用户问「你还记得什么」「我叫什么」；
        * 你不确定之前是否记过某件事，想先确认一下；
        * ``keywords`` 留空会返回全部记忆，适合"我到底记得什么"这类问题。

        Args:
            keywords: 检索关键词。留空表示列出全部记忆。
        """
        mem = ctx.long_term
        if mem is None:
            return _failure("长期记忆未启用。", count=0)

        if keywords.strip():
            facts = mem.search(keywords.strip(), limit=8)
        else:
            facts = mem.all()[:20]

        if not facts:
            return {
                "ok": True,
                "count": 0,
                "summary": "没有找到相关的长期记忆。",
            }
        return {
            "ok": True,
            "count": len(facts),
            "facts": [f.content for f in facts],
            "summary": "记住的有：" + "；".join(f.content for f in facts),
        }

    @registry.register(dangerous=False)
    async def forget(keywords: str) -> dict[str, Any]:
        """忘掉长期记忆里与关键词相关的内容。

        什么时候该用：用户明确要求忘掉某事（「忘掉我喜欢咖啡这件事」
        「别记着我的地址」）时。这是用户对自己数据的控制权，应当照做。

        Args:
            keywords: 要忘掉的内容里的关键词。
        """
        mem = ctx.long_term
        if mem is None:
            return _failure("长期记忆未启用。", forgotten=0)

        n = mem.forget_matching(keywords.strip())
        if n == 0:
            return {
                "ok": True,
                "forgotten": 0,
                "summary": f"没有找到包含「{keywords}」的记忆。",
            }
        return {
            "ok": True,
            "forgotten": n,
            "summary": f"已忘掉 {n} 条与「{keywords}」相关的记忆。",
        }

    return registry


async def _turn(ctx: ToolContext, degrees: float, *, left: bool, device_id: str) -> dict[str, Any]:
    """转向的共用实现：角度 → 角速度 × 时长。"""
    angle = _clamp(float(degrees), 5.0, 360.0)
    magnitude = angle * DEG_TO_RAD  # 弧度

    # 取一个不超过安全上限的角速度，让转向时间与角度成正比。
    angular_speed = min(1.2, ctx.settings.behavior.max_angular_rps)
    duration_ms = int(magnitude / angular_speed * 1000)
    duration_ms = int(_clamp(duration_ms, 150, ctx.settings.behavior.max_duration_ms))

    signed = angular_speed if left else -angular_speed
    try:
        robot = ctx.robot(device_id or None)
        result = await robot.drive(0.0, signed, duration_ms)
    except SparkBotError as exc:
        return _failure(exc.message)

    direction = "左" if left else "右"
    return {
        "ok": True,
        "summary": f"已向{direction}转约 {angle:.0f} 度。",
        "degrees": angle,
        "duration_ms": result.duration_ms,
    }


# --------------------------------------------------------------------------- #
# 自检
# --------------------------------------------------------------------------- #
def tool_catalog(registry: ToolRegistry) -> list[dict[str, Any]]:
    """导出工具清单，供 ``/api/tools`` 与文档生成使用。"""
    return [
        {
            "name": spec.name,
            "description": spec.description,
            "parameters": spec.parameters,
            "requires": sorted(spec.requires),
            "dangerous": spec.dangerous,
        }
        for spec in registry.select(None)
    ]
