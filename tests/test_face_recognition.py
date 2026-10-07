"""人脸识别端到端测试：**不需要硬件，也不需要真实人脸照片**。

覆盖的目标链路（用户诉求：设备端本地推理 → 上传识别信息 → Agent 端绑名字）：

    设备（模拟）本地推理 → 只回 512 维特征 → PC 建人脸库 → 认人 → 绑名字

为什么用模拟设备而不是真机：真机测不了"换个陌生人会不会认错"这种分支，
而且每次都要有人站在摄像头前。模拟设备的特征由名字哈希生成、**带噪声**，
所以"同一个人相似度高、不同人相似度低"这两个关键性质都能被稳定复现
（见 ``mock_device.device.mock_face_feature``）。

运行::

    python tests/test_face_recognition.py
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from sparkbot.paths import ensure_path  # noqa: E402

ensure_path()

from sparkbot.app import create_app  # noqa: E402
from sparkbot.config import Settings  # noqa: E402
from sparkbot.mock_device import MockDevice, MockDeviceConfig  # noqa: E402
from sparkbot.perception.face import FEAT_LEN, FaceDB, cosine  # noqa: E402
from sparkbot.runtime import SparkBotRuntime  # noqa: E402
from sparkbot.testing import ServedApp, serve_app  # noqa: E402

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-6s %(name)s: %(message)s",
    datefmt="%H:%M:%S",
)
logging.getLogger("httpx").setLevel(logging.WARNING)
logger = logging.getLogger("face-e2e")

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


async def wait_until(predicate, *, timeout: float = 20.0, interval: float = 0.05) -> bool:
    """轮询等待条件成立。"""
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while loop.time() < deadline:
        if predicate():
            return True
        await asyncio.sleep(interval)
    return False


def build_settings(face_path: Path) -> Settings:
    """完全离线的测试配置：假模型 + 假语音 + 临时人脸库。"""
    settings = Settings()
    settings.server.host = "127.0.0.1"
    settings.server.log_level = "info"
    settings.llm.provider = "mock"
    settings.llm.model = "mock-model"
    settings.vision.provider = "mock"
    settings.speech.asr_provider = "mock"
    settings.speech.tts_provider = "mock"
    settings.device.command_timeout_ms = 5_000
    settings.device.heartbeat_ms = 2_000
    settings.behavior.talking_animation = False
    settings.behavior.min_command_interval_ms = 0
    # 隔离：绝不能让测试写到真实的人脸库。
    settings.face.path = str(face_path)
    # 长期记忆同样要隔离 —— 否则测试里的"王五"会写进用户真实的 facts.jsonl。
    settings.memory.path = str(face_path.parent / "facts.jsonl")
    return settings


# --------------------------------------------------------------------------- #
# 阶段
# --------------------------------------------------------------------------- #
async def test_feature_similarity() -> None:
    """阶段 0：人脸库本身的相似度判据（纯离线）。"""
    logger.info("阶段 0 · 人脸库相似度")
    from sparkbot.mock_device.device import mock_face_feature

    a1 = mock_face_feature("张三", noise=0.03)
    a2 = mock_face_feature("张三", noise=0.03)
    b = mock_face_feature("李四", noise=0.03)

    check(len(a1) == FEAT_LEN, "特征维度与固件一致", f"{len(a1)} 维")
    same = cosine(a1, a2)
    diff = cosine(a1, b)
    check(same > 0.9, "同一个人（含噪声）相似度很高", f"{same:.3f}")
    check(diff < 0.5, "不同的人相似度低于阈值", f"{diff:.3f}")

    with tempfile.TemporaryDirectory() as tmp:
        db = FaceDB(Path(tmp) / "f.json", threshold=0.5)
        db.enroll("张三", a1)
        check(db.match(a1).name == "张三", "登记后能认出来")
        check(db.match(b).name is None, "陌生人不会被认成张三",
              f"similarity={db.match(b).similarity:.3f}")
        # 落盘 → 重新加载，名字不能丢（这是"长期绑定"的底线）
        again = FaceDB(Path(tmp) / "f.json", threshold=0.5)
        check(again.names() == ["张三"], "人脸库能持久化并重载", str(again.names()))


async def test_device_inference(served: ServedApp, rt: SparkBotRuntime) -> None:
    """阶段 1：设备端本地推理只回特征，不回图。"""
    logger.info("阶段 1 · 设备端人脸推理")
    import httpx

    robot = rt.robots.get()
    scan = await robot.identify_faces()
    check(len(scan) == 1, "设备回传了 1 张脸", f"count={len(scan)}")
    check(scan.width == 640 and scan.height == 480,
          "带回原图尺寸（用于把框映射回全图）", f"{scan.width}x{scan.height}")
    if scan.faces:
        face = scan.faces[0]
        check(len(face.feat) == FEAT_LEN, "特征已按 float32 解出", f"{len(face.feat)} 维")
        check(face.box[2] > face.box[0] and face.box[3] > face.box[1], "人脸框有效", str(face.box))
        check(0.9 < face.score <= 1.0, "检测分数合理", f"{face.score}")

    async with httpx.AsyncClient(base_url=served.base_url, timeout=15.0) as client:
        listed = (await client.get("/api/faces")).json()
        check(listed.get("count") == 0, "初始人脸库为空")
        check(listed.get("threshold") == 0.5, "阈值来自配置", str(listed.get("threshold")))


async def test_enroll_and_recognize(served: ServedApp, rt: SparkBotRuntime) -> None:
    """阶段 2：绑名字 → 再扫就认得出。"""
    logger.info("阶段 2 · 绑定与识别")
    import httpx

    async with httpx.AsyncClient(base_url=served.base_url, timeout=20.0) as client:
        scan = (await client.post("/api/faces/scan", json={})).json()
        check(scan.get("count") == 1, "扫到一张脸")
        check(scan["faces"][0].get("known") is False,
              "绑定前不认识（不会瞎猜名字）", str(scan["faces"][0].get("name")))

        enrolled = (await client.post("/api/faces/enroll", json={"name": "张三"})).json()
        check(enrolled.get("ok") is True and enrolled.get("samples") == 1,
              "绑定成功", str(enrolled))

        scan2 = (await client.post("/api/faces/scan", json={})).json()
        top = scan2["faces"][0]
        check(top.get("name") == "张三", "再次扫描认出了张三", str(top))
        check(top.get("similarity", 0) > 0.9, "相似度高于阈值", f"{top.get('similarity')}")

        listed = (await client.get("/api/faces")).json()
        check(listed.get("count") == 1, "人脸库里有 1 个人")
        check(listed["people"][0]["name"] == "张三", "库里存的是张三")

    # 换个人站到摄像头前 → 必须认不出来（不能把谁都当成张三）
    await rt.robots.get().conn.command("config", {"face_people": ["李四"]})
    async with httpx.AsyncClient(base_url=served.base_url, timeout=20.0) as client:
        scan3 = (await client.post("/api/faces/scan", json={})).json()
        top3 = scan3["faces"][0]
        check(top3.get("known") is False, "换了个人就认不出来（没有乱认）",
              f"name={top3.get('name')} sim={top3.get('similarity')}")

    await rt.robots.get().conn.command("config", {"face_people": ["张三"]})


async def test_agent_uses_names(served: ServedApp, rt: SparkBotRuntime) -> None:
    """阶段 3：认出的名字进入对话上下文 + 谁在面前的工具。"""
    logger.info("阶段 3 · 名字进入对话上下文")
    import httpx

    async with httpx.AsyncClient(base_url=served.base_url, timeout=30.0) as client:
        chat = (await client.post("/api/chat", json={"text": "你好呀"})).json()
        faces = chat.get("faces") or []
        check(any(f.get("name") == "张三" for f in faces),
              "每轮对话前自动扫脸并把名字带进本轮", str(faces))

        # 假模型命中"认识我"→ who_is_here，走的是真实工具执行路径
        ask = (await client.post("/api/chat", json={"text": "你认得我吗"})).json()
        names = [t["name"] for t in ask.get("tools", [])]
        check("who_is_here" in names, "模型选择了 who_is_here", ", ".join(names))
        tool = next((t for t in ask.get("tools", []) if t["name"] == "who_is_here"), None)
        check(bool(tool and tool.get("ok")), "who_is_here 执行成功",
              str((tool or {}).get("result", {}).get("summary", ""))[:60])

    # 直接看注入的提示词，确认名字真的进了 system prompt
    agent = rt.agents.get()
    _, hint = await agent._look_at_faces()  # noqa: SLF001 - 白盒验证提示词
    check("张三" in hint, "提示词里给出了名字", hint[:70])


async def test_auto_bind_from_introduction(served: ServedApp, rt: SparkBotRuntime) -> None:
    """阶段 4：用户自报姓名时自动绑脸（本需求的核心）。"""
    logger.info("阶段 4 · 自我介绍自动绑脸")
    import httpx

    await rt.robots.get().conn.command("config", {"face_people": ["王五"]})
    async with httpx.AsyncClient(base_url=served.base_url, timeout=30.0) as client:
        await client.post("/api/chat", json={"text": "你好，我叫王五"})
        listed = (await client.get("/api/faces")).json()
        names = [p["name"] for p in listed["people"]]
        check("王五" in names, "自我介绍后自动把人脸绑到这个名字", str(names))

        # 名字也要进长期记忆 —— 没拍到脸时还能靠名字认出这个人
        mem = (await client.get("/api/memory")).json()
        facts = [f["content"] for f in mem.get("facts", [])]
        check(any("王五" in c for c in facts), "姓名写进了长期记忆", str(facts)[:90])

        # 认出来之后，下一轮上下文里应当带上王五
        chat = (await client.post("/api/chat", json={"text": "你还记得我吗"})).json()
        check(any(f.get("name") == "王五" for f in (chat.get("faces") or [])),
              "新一轮对话里认出了王五", str(chat.get("faces")))


async def test_delete_and_privacy(served: ServedApp, rt: SparkBotRuntime) -> None:
    """阶段 5：删除（用户的生物特征控制权）+ 关掉开关时的降级。"""
    logger.info("阶段 5 · 删除与降级")
    import httpx

    async with httpx.AsyncClient(base_url=served.base_url, timeout=20.0) as client:
        deleted = (await client.delete("/api/faces/王五")).json()
        check(deleted.get("removed") is True, "可以删掉某个人的人脸")
        listed = (await client.get("/api/faces")).json()
        check(all(p["name"] != "王五" for p in listed["people"]), "删除后库里不再有王五")

        cleared = (await client.post("/api/faces/clear", json={})).json()
        check(cleared.get("ok") is True, "可以一键清空人脸库", str(cleared))
        listed2 = (await client.get("/api/faces")).json()
        check(listed2.get("count") == 0, "清空后为空")

    # 工具在"人脸库未启用"时必须明确说做不到，而不是假装成功
    agent = rt.agents.get()
    saved = agent.face_db
    agent.face_db = None
    try:
        result = await agent.scan_faces()
        check(False, "未启用时 scan_faces 应当报错", str(result))
    except Exception as exc:  # noqa: BLE001 - 预期就是抛错
        check("未启用" in str(exc), "未启用时明确报「人脸识别未启用」", str(exc)[:50])
    finally:
        agent.face_db = saved


# --------------------------------------------------------------------------- #
# 主流程
# --------------------------------------------------------------------------- #
async def main() -> int:
    """启动真实服务 + 模拟设备，跑完全部阶段。"""
    with tempfile.TemporaryDirectory() as tmp:
        settings = build_settings(Path(tmp) / "face_db.json")
        app = create_app(settings)
        served = await serve_app(app)
        rt: SparkBotRuntime = app.state.runtime

        device = MockDevice(
            MockDeviceConfig(
                url=served.ws_url(),
                device_id="esp32s3-face",
                name="小星（人脸测试）",
                telemetry_ms=1_000,
                face_people=["张三"],
            )
        )
        device_task = asyncio.create_task(device.run(), name="mock-device")

        try:
            await test_feature_similarity()
            connected = await wait_until(lambda: rt.gateway.connected_count == 1, timeout=20.0)
            check(connected, "模拟设备已接入")
            if not connected:
                return 1
            await test_device_inference(served, rt)
            await test_enroll_and_recognize(served, rt)
            await test_agent_uses_names(served, rt)
            await test_auto_bind_from_introduction(served, rt)
            await test_delete_and_privacy(served, rt)
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
