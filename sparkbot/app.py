"""Web 层：FastAPI 应用工厂、设备 WebSocket 端点、控制与监控 API。

路由一览
--------
* ``WS  /robot``            —— ESP32-S3 设备接入点（见 ``docs/protocol.md``）
* ``WS  /api/events``       —— 事件流，供控制台实时显示
* ``GET /api/status``       —— 运行状态
* ``GET /api/devices``      —— 在线设备与其遥测
* ``GET /api/tools``        —— 已注册工具清单（含参数字段）
* ``GET /api/events/recent``—— 最近事件
* ``POST /api/chat``        —— 文本（可带图片）走一轮对话
* ``POST /api/voice``       —— 上传 PCM 音频走一轮对话
* ``POST /api/stop``        —— 急停（绕过模型）
* ``POST /api/action``      —— 直接下发单个动作，用于调试
* ``POST /api/agent/reset`` —— 清空对话历史
* ``GET /``                 —— 简易控制台页面
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import hashlib
import logging
import time
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, AsyncIterator

from fastapi import Body, FastAPI, HTTPException, Query, Request, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, JSONResponse

from . import __version__
from .config import Settings, get_settings
from .discovery import DiscoveryResponder
from .core.errors import DeviceError, DeviceOfflineError, SparkBotError
from .core.events import Event
from .device.protocol import Emotion
from .runtime import SparkBotRuntime

logger = logging.getLogger(__name__)


# --------------------------------------------------------------------------- #
# 应用工厂
# --------------------------------------------------------------------------- #
def create_app(settings: Settings | None = None) -> FastAPI:
    """构造 FastAPI 应用。

    Args:
        settings: 显式配置；``None`` 时读取环境变量 / ``.env``。
    """
    resolved = settings or get_settings()
    _configure_logging(resolved)

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        """管理运行时的启停。"""
        runtime = SparkBotRuntime(resolved)
        app.state.runtime = runtime
        await runtime.start()

        # 自动发现：回应设备的 UDP 广播探测，让设备不必写死 PC 的 IP。
        # 失败只是警告 —— 设备会回退到固件里配置的地址，功能不受影响。
        discovery = DiscoveryResponder(port=resolved.server.port)
        app.state.discovery = discovery
        await discovery.start()

        logger.info(
            "SparkBot %s 已启动 http://%s:%d  (设备接入: ws://<本机 IP>:%d%s)",
            __version__,
            resolved.server.host,
            resolved.server.port,
            resolved.server.port,
            resolved.server.ws_path,
        )
        try:
            yield
        finally:
            # 关闭前必须释放串口：进程退出后 OS 一般会回收，但若是被 Ctrl+C
            # 中断或嵌入别的主机运行，不显式关闭会一直占着 COM 口，让命令行
            # 工具和其它程序都打不开。
            with contextlib.suppress(Exception):
                from .serial_log import stop_reader

                stop_reader()
            with contextlib.suppress(Exception):
                await discovery.stop()
            await runtime.stop()
            logger.info("SparkBot 已停止")

    app = FastAPI(
        title="SparkBot",
        version=__version__,
        description="面向 ESP32-S3 移动机器人的 PC 端 Agent 框架",
        lifespan=lifespan,
    )
    app.add_middleware(
        CORSMiddleware,
        allow_origins=resolved.server.cors_origins,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    _register_routes(app)
    return app


def _configure_logging(settings: Settings) -> None:
    """按配置初始化日志，重复调用安全。"""
    level = getattr(logging, settings.server.log_level.upper(), logging.INFO)
    root = logging.getLogger()
    if not root.handlers:
        logging.basicConfig(
            level=level,
            format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
            datefmt="%H:%M:%S",
        )
    root.setLevel(level)


def _runtime(request: Request) -> SparkBotRuntime:
    """从请求上下文取出运行时。"""
    runtime: SparkBotRuntime | None = getattr(request.app.state, "runtime", None)
    if runtime is None:  # pragma: no cover - lifespan 未执行时的防御
        raise HTTPException(status_code=503, detail="运行时尚未就绪")
    return runtime


def _serial_reader() -> Any:
    """取得进程级共享的串口读取器。

    必须是单例：串口是独占资源，若每个连接各建一个 reader，
    第二个必然打不开端口。
    """
    from .serial_log import get_reader

    return get_reader()


def _memory(settings: Settings) -> Any:
    """取得长期记忆库（未启用时返回 None）。

    直接走单例而不是从 runtime 取：这样设备离线、Agent 未创建时
    也能查看与管理记忆。
    """
    if not settings.memory.enabled:
        return None
    from .brain.agent import memory_path
    from .brain.long_term import get_memory

    try:
        return get_memory(
            memory_path(settings),
            capacity=settings.memory.capacity,
            enabled=True,
        )
    except Exception:  # noqa: BLE001 - 管理接口不该因为记忆坏了而 500
        logger.exception("取得长期记忆失败")
        return None


def _serial_offer(queue: asyncio.Queue, item: dict[str, Any]) -> None:
    """把一行日志放进队列；满了就丢最旧的。

    在事件循环线程里执行（由 ``call_soon_threadsafe`` 调度）。
    **绝不能阻塞**：队列反压会拖慢串口读取线程，进而丢真实日志，
    所以宁可丢掉页面上最旧的几行。
    """
    try:
        queue.put_nowait(item)
    except asyncio.QueueFull:
        with contextlib.suppress(asyncio.QueueEmpty):
            queue.get_nowait()
        with contextlib.suppress(asyncio.QueueFull):
            queue.put_nowait(item)

#: 各 provider 需要用户填写什么。控制台据此给出针对性的提示，
#: 而不是让用户对着一堆字段猜。
_PROVIDER_HINTS: dict[str, dict[str, str]] = {
    "mock": {
        "summary": "离线假模型，不需要 key 与网络",
        "required": [],
    },
    "openai": {
        "summary": "OpenAI 官方接口",
        "required": ["api_key"],
        "suggest_model": "gpt-4o-mini",
    },
    # 说明：语音项的 provider 值为 "openai" 时，实际走的是
    # **OpenAI 兼容协议**（/audio/transcriptions、/audio/speech）。
    # 因此填国内服务时，provider 选 "openai"，把 base_url 与 model
    # 改成对方的值即可 —— 控制台里那些字段现在都可编辑。
    "speech_asr": {
        "summary": "语音识别。provider 用 openai（即 OpenAI 兼容协议），"
                   "国内可用硅基流动：base_url=https://api.siliconflow.cn/v1，"
                   "model=FunAudioLLM/SenseVoiceSmall",
        "required": ["api_key"],
        "suggest_model": "FunAudioLLM/SenseVoiceSmall",
    },
    "speech_tts": {
        "summary": "语音合成。provider 用 openai（即 OpenAI 兼容协议），"
                   "国内可用硅基流动：base_url=https://api.siliconflow.cn/v1，"
                   "model=FunAudioLLM/CosyVoice2-0.5B，voice=alex",
        "required": ["api_key"],
        "suggest_model": "FunAudioLLM/CosyVoice2-0.5B",
    },
    "deepseek": {
        "summary": "DeepSeek 官方接口（注意：对话模型不支持图像输入）",
        "required": ["api_key"],
        "suggest_model": "deepseek-chat",
    },
    "openai_compat": {
        "summary": "任意 OpenAI 兼容网关，必须填 base_url（如 vLLM / Ollama / one-api）",
        "required": ["api_key", "base_url"],
        "suggest_model": "qwen2.5-vl-7b",
    },
}


def _env_path() -> Path:
    """``.env`` 的落盘位置：项目根目录（当前工作目录）。"""
    return Path(".env")


def _coerce_config_value(raw: Any, current: Any) -> Any:
    """把前端传来的 JSON 值转换成配置字段需要的类型。

    前端表单拿到的都是字符串或原生 JSON，而 pydantic 模型是强类型的
    （``temperature`` 是 float、``enabled`` 是 bool、``provider`` 是 Literal）。
    不转换就会在 ``setattr`` 时静默存进错误类型，直到构造 provider 才炸。

    ``current`` 用来推断目标类型；为 ``None`` 时按字符串处理。
    """
    if raw is None:
        return None

    # bool 必须在 int 之前判断，因为 Python 里 bool 是 int 的子类。
    if isinstance(current, bool):
        if isinstance(raw, bool):
            return raw
        return str(raw).strip().lower() in ("1", "true", "yes", "on", "是")

    if isinstance(current, int) and not isinstance(current, bool):
        return int(raw)

    if isinstance(current, float):
        return float(raw)

    if current is None and isinstance(raw, str):
        # 目标类型未知时，空白字符串统一当空值处理（provider 字段可留空）。
        return raw.strip()

    return raw



# --------------------------------------------------------------------------- #
# 路由
# --------------------------------------------------------------------------- #
def _register_routes(app: FastAPI) -> None:
    # ---------------------------------------------------------------- #
    # 设备接入
    # ---------------------------------------------------------------- #
    @app.websocket("/robot")
    async def robot_endpoint(websocket: WebSocket) -> None:
        """ESP32-S3 的设备接入点。

        握手与消息循环全部由 ``DeviceGateway`` 处理。

        注意：这里**不能**再挂一个并发的 ``websocket.receive()`` 监听器来探测断开。
        同一个 WebSocket 只允许一个消费者——两个协程同时 receive 会把消息
        随机分走，导致上行帧被静默丢弃（曾因此丢掉摄像头帧）。
        断开检测已内建在网关的接收循环里：收到 ``websocket.disconnect``
        或 receive 抛异常时即结束会话。
        """
        runtime: SparkBotRuntime = websocket.app.state.runtime
        await runtime.gateway.serve(websocket)

    # ---------------------------------------------------------------- #
    # 事件流
    # ---------------------------------------------------------------- #
    @app.websocket("/api/events")
    async def events_endpoint(websocket: WebSocket) -> None:
        """把事件总线推给控制台。"""
        runtime: SparkBotRuntime = websocket.app.state.runtime
        await websocket.accept()
        try:
            # 先补发最近的事件，避免刚打开页面时一片空白。
            for event in runtime.bus.recent(limit=30):
                await websocket.send_json(event.to_dict())
            async with runtime.bus.subscribe() as queue:
                while True:
                    event: Event = await queue.get()
                    await websocket.send_json(event.to_dict())
        except WebSocketDisconnect:
            return
        except Exception as exc:  # noqa: BLE001 - 控制台断开不该影响服务
            logger.debug("事件流结束: %s", exc)

    # ---------------------------------------------------------------- #
    # 串口日志
    # ---------------------------------------------------------------- #
    # 浏览器无法直接访问串口，所以由服务端独占打开 COM 口，再把数据
    # 通过 WebSocket 转推给页面。
    #
    # 串口是**独占资源**：本服务打开后，命令行工具 serial_log.py 与
    # esp32gw.exe 都会失败。因此这里提供显式的打开/关闭控制，
    # 而不是服务启动就自动占用。

    @app.get("/api/serial/ports")
    async def serial_ports() -> dict[str, Any]:
        """列出本机可用串口，供页面下拉选择。"""
        from .serial_log import list_ports

        ports = list_ports()
        return {
            "ok": True,
            "ports": [p.to_dict() for p in ports],
            "count": len(ports),
            "status": _serial_reader().status(),
        }

    @app.get("/api/serial/status")
    async def serial_status() -> dict[str, Any]:
        """串口日志当前状态。"""
        return {"ok": True, **_serial_reader().status()}

    @app.get("/api/serial/recent")
    async def serial_recent(limit: int = Query(500, ge=1, le=5000)) -> dict[str, Any]:
        """最近若干行串口日志（用于页面刷新后补历史）。"""
        reader = _serial_reader()
        return {
            "ok": True,
            "lines": [ln.to_dict() for ln in reader.recent(limit)],
            "status": reader.status(),
        }

    @app.post("/api/serial/open")
    async def serial_open(payload: dict[str, Any] = Body(...)) -> dict[str, Any]:
        """打开串口并开始把日志推给所有订阅的页面。

        请求体::

            {"port": "COM15", "baudrate": 115200}
        """
        reader = _serial_reader()
        port = str(payload.get("port") or "").strip()
        if not port:
            raise HTTPException(status_code=400, detail="缺少 port 参数")
        try:
            baudrate = int(payload.get("baudrate") or 115200)
        except (TypeError, ValueError):
            raise HTTPException(status_code=400, detail="baudrate 必须是整数") from None

        try:
            # open() 会阻塞几毫秒到几十毫秒（打开设备节点），
            # 放到线程池里，避免卡住事件循环导致其它 API 一起变慢。
            await asyncio.to_thread(reader.open, port, baudrate)
        except RuntimeError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return {"ok": True, **reader.status()}

    @app.post("/api/serial/close")
    async def serial_close() -> dict[str, Any]:
        """关闭串口，释放给其它程序使用。"""
        reader = _serial_reader()
        await asyncio.to_thread(reader.close)
        return {"ok": True, **reader.status()}

    @app.post("/api/serial/clear")
    async def serial_clear() -> dict[str, Any]:
        """清空日志缓冲（不改变串口开关状态）。"""
        reader = _serial_reader()
        reader.clear()
        return {"ok": True, **reader.status()}

    @app.websocket("/api/serial/stream")
    async def serial_stream(websocket: WebSocket) -> None:
        """把串口日志实时推给页面。

        为什么用**队列 + call_soon_threadsafe**：
        串口读取是独立线程，而 WebSocket 只能在事件循环里发。回调里直接
        ``await`` 是不行的（跨线程），所以回调只把行丢进 asyncio 队列，
        由本协程负责发送。队列设上限，页面卡住时丢最旧的，绝不反压串口
        读取线程 —— 否则会丢真实日志。
        """
        await websocket.accept()
        reader = _serial_reader()
        loop = asyncio.get_running_loop()
        queue: asyncio.Queue = asyncio.Queue(maxsize=2000)

        def on_line(line: Any) -> None:
            """串口线程回调：只做入队，绝不阻塞。"""
            try:
                loop.call_soon_threadsafe(_serial_offer, queue, line.to_dict())
            except RuntimeError:
                # 事件循环已关闭（页面正在断开），忽略即可
                pass

        reader.subscribe(on_line)
        try:
            # 先补发最近的历史，页面一打开就能看到上下文
            await websocket.send_json(
                {"type": "init", "status": reader.status(),
                 "lines": [ln.to_dict() for ln in reader.recent(300)]}
            )
            while True:
                item = await queue.get()
                await websocket.send_json(item)
        except WebSocketDisconnect:
            return
        except Exception as exc:  # noqa: BLE001 - 页面断开不影响服务
            logger.debug("串口日志流结束: %s", exc)
        finally:
            reader.unsubscribe(on_line)

    # ---------------------------------------------------------------- #
    # 长期记忆
    # ---------------------------------------------------------------- #
    # 长期记忆是跨会话保留的用户信息，用户应当能查看与清除 ——
    # 这是对自己数据的控制权，也是排查"它为什么记得这个"的必要手段。

    @app.get("/api/memory")
    async def memory_list(
        request: Request,
        limit: int = Query(200, ge=1, le=2000),
    ) -> dict[str, Any]:
        """列出长期记忆。"""
        runtime = _runtime(request)
        mem = _memory(runtime.settings)
        if mem is None or not mem.enabled:
            return {"ok": True, "enabled": False, "count": 0, "facts": []}
        facts = mem.all()[:limit]
        return {
            "ok": True,
            "enabled": True,
            "count": mem.count(),
            "capacity": mem.capacity,
            "path": str(mem.path),
            "write_errors": mem.write_errors,
            "facts": [f.to_dict() for f in facts],
        }

    @app.get("/api/memory/search")
    async def memory_search(
        request: Request,
        q: str = Query("", description="检索关键词；留空返回全部"),
        limit: int = Query(5, ge=1, le=50),
    ) -> dict[str, Any]:
        """按关键词检索长期记忆（与 Agent 用的是同一套检索）。"""
        runtime = _runtime(request)
        mem = _memory(runtime.settings)
        if mem is None or not mem.enabled:
            return {"ok": True, "enabled": False, "count": 0, "facts": []}
        facts = mem.search(q, limit=limit) if q.strip() else mem.all()[:limit]
        return {
            "ok": True,
            "enabled": True,
            "query": q,
            "count": len(facts),
            "facts": [f.to_dict() for f in facts],
        }

    @app.post("/api/memory")
    async def memory_add(
        request: Request,
        payload: dict[str, Any] = Body(...),
    ) -> dict[str, Any]:
        """手动添加一条长期记忆。

        请求体::

            {"content": "主人叫张伟", "importance": 5}
        """
        mem = _memory(_runtime(request).settings)
        if mem is None:
            raise HTTPException(status_code=400, detail="长期记忆未启用")
        content = str(payload.get("content") or "").strip()
        if not content:
            raise HTTPException(status_code=400, detail="content 不能为空")
        try:
            importance = int(payload.get("importance") or 3)
        except (TypeError, ValueError):
            raise HTTPException(status_code=400, detail="importance 必须是整数") from None
        fact = mem.remember(content, importance=importance, source="user")
        if fact is None:
            raise HTTPException(status_code=400, detail="写入失败")
        return {"ok": True, "fact": fact.to_dict()}

    @app.delete("/api/memory/{fact_id}")
    async def memory_delete(request: Request, fact_id: str) -> dict[str, Any]:
        """删除一条长期记忆。"""
        mem = _memory(_runtime(request).settings)
        if mem is None:
            raise HTTPException(status_code=400, detail="长期记忆未启用")
        if not mem.forget(fact_id):
            raise HTTPException(status_code=404, detail="没有这条记忆")
        return {"ok": True, "deleted": fact_id, "count": mem.count()}

    @app.post("/api/memory/clear")
    async def memory_clear(request: Request) -> dict[str, Any]:
        """清空全部长期记忆。"""
        mem = _memory(_runtime(request).settings)
        if mem is None:
            raise HTTPException(status_code=400, detail="长期记忆未启用")
        return {"ok": True, "cleared": mem.clear()}

    # ---------------------------------------------------------------- #
    # 只读 API
    # ---------------------------------------------------------------- #
    @app.get("/health")
    async def health() -> dict[str, Any]:
        """健康检查，供容器/监控探针使用。

        ``ui_version`` 与控制台页面注入的版本号一致 —— 排查"页面是不是
        缓存了旧版本"时，用它和浏览器控制台打印的值对照即可。
        """
        return {
            "ok": True,
            "version": __version__,
            "ui_version": _UI_VERSION,
            "ts": time.time(),
        }

    @app.get("/api/status")
    async def status(request: Request) -> dict[str, Any]:
        """返回运行状态全貌。"""
        runtime = _runtime(request)
        return {"ok": True, **runtime.status().to_dict()}

    @app.get("/api/devices")
    async def devices(request: Request) -> dict[str, Any]:
        """列出在线设备及其遥测。"""
        runtime = _runtime(request)
        return {"ok": True, "count": runtime.gateway.connected_count, "devices": runtime.gateway.describe()}

    @app.get("/api/tools")
    async def tools(request: Request) -> dict[str, Any]:
        """列出已注册工具（含参数 schema），可直接用于生成前端表单。"""
        runtime = _runtime(request)
        catalog = runtime.tools_catalog()
        return {"ok": True, "count": len(catalog), "tools": catalog}

    @app.get("/api/events/recent")
    async def events_recent(request: Request, limit: int = Query(50, ge=1, le=500)) -> dict[str, Any]:
        """返回最近的事件。"""
        runtime = _runtime(request)
        return {"ok": True, "events": [e.to_dict() for e in runtime.bus.recent(limit=limit)]}

    # ---------------------------------------------------------------- #
    # 对话
    # ---------------------------------------------------------------- #
    @app.post("/api/chat")
    async def chat(request: Request, payload: dict[str, Any] = Body(...)) -> dict[str, Any]:
        """走一轮对话。

        请求体::

            {"text": "你前面有什么", "device_id": "esp32s3-xx", "announce": false,
             "images": ["data:image/jpeg;base64,..."], "allow_tools": true}
        """
        runtime = _runtime(request)
        text = str(payload.get("text") or "").strip()
        images = payload.get("images") or []
        if not isinstance(images, list):
            raise HTTPException(status_code=422, detail="images 必须是数组")

        if not text and not images:
            raise HTTPException(status_code=422, detail="text 与 images 至少要有一个")

        try:
            turn = await runtime.chat(
                text,
                device_id=payload.get("device_id") or None,
                images=[str(i) for i in images],
                announce=bool(payload.get("announce", False)),
                allow_tools=bool(payload.get("allow_tools", True)),
            )
        except DeviceOfflineError as exc:
            # 没有设备时仍然允许纯文本对话，因此不当作错误。
            logger.info("无在线设备，按纯文本处理: %s", exc.message)
            raise HTTPException(status_code=409, detail=exc.message) from exc
        except SparkBotError as exc:
            raise HTTPException(status_code=500, detail=exc.message) from exc

        return {"ok": turn.ok, **turn.to_dict()}

    @app.post("/api/voice")
    async def voice(request: Request, payload: dict[str, Any] = Body(...)) -> dict[str, Any]:
        """上传一段 PCM 音频（base64）走完整语音链路。

        请求体::

            {"pcm_b64": "...", "sample_rate": 16000, "device_id": "esp32s3-xx",
             "respond": true}

        ``respond`` 为 ``False`` 时只做识别，不触发对话。
        """
        runtime = _runtime(request)
        raw = payload.get("pcm_b64")
        if not raw:
            raise HTTPException(status_code=422, detail="缺少 pcm_b64")

        try:
            pcm = base64.b64decode(str(raw), validate=True)
        except Exception as exc:  # noqa: BLE001 - 明确的 422 比 500 更有用
            raise HTTPException(status_code=422, detail=f"pcm_b64 不是合法 base64: {exc}") from exc

        if runtime.asr is None:
            raise HTTPException(status_code=503, detail="语音识别未启用")

        sample_rate = int(payload.get("sample_rate") or runtime.settings.speech.input_sample_rate)
        device_id = payload.get("device_id") or None

        if not bool(payload.get("respond", True)):
            transcript = await runtime.asr.transcribe(pcm, sample_rate=sample_rate)
            return {"ok": True, "text": transcript.text, "transcript": transcript.to_dict()}

        try:
            text, turn = await runtime.transcribe_and_chat(pcm, device_id=device_id, sample_rate=sample_rate)
        except SparkBotError as exc:
            raise HTTPException(status_code=500, detail=exc.message) from exc

        return {
            "ok": True,
            "text": text,
            "turn": turn.to_dict() if turn else None,
        }

    @app.post("/api/voice/trigger")
    async def voice_trigger(request: Request, payload: dict[str, Any] = Body(default={})) -> dict[str, Any]:
        """手动触发一次完整语音对话（等价于设备上报了唤醒词）。

        为什么需要它：语音闭环本来只由设备的 `wake_word` / `button` 事件
        触发，而很多板子没做本地唤醒词（WakeNet）。没有这个入口，
        「说一句 → 机器人在板子上回话」这条闭环就**完全没法手动测**。

        请求体::

            {"device_id": "...", "wait": true}

        ``wait=true``（默认）会等到整轮结束再返回，便于脚本判断结果；
        ``wait=false`` 立即返回，对话在后台跑。
        """
        runtime = _runtime(request)
        device_id = payload.get("device_id") or None

        robot = runtime.robots.try_get(device_id)
        if robot is None:
            raise HTTPException(status_code=409, detail="没有在线设备，无法触发语音对话")
        if not robot.has("microphone"):
            raise HTTPException(status_code=409, detail=f"设备 {robot.device_id} 没有麦克风")

        started = time.perf_counter()

        if not bool(payload.get("wait", True)):
            asyncio.create_task(runtime._handle_voice_turn(robot.device_id))  # noqa: SLF001
            return {"ok": True, "device_id": robot.device_id, "wait": False}

        try:
            result = await runtime.voice_turn(robot.device_id)
        except SparkBotError as exc:
            raise HTTPException(status_code=500, detail=exc.message) from exc

        return {
            "ok": True,
            "device_id": robot.device_id,
            "elapsed_ms": int((time.perf_counter() - started) * 1000),
            "heard": result.get("text", ""),
            "replied": result.get("reply", ""),
        }

    # ---------------------------------------------------------------- #
    # 控制
    # ---------------------------------------------------------------- #
    @app.post("/api/stop")
    async def stop(request: Request) -> dict[str, Any]:
        """急停：绕过模型直接刹停所有在线机器人。"""
        runtime = _runtime(request)
        stopped = await runtime.emergency_stop()
        return {"ok": True, "stopped": stopped}

    @app.post("/api/action")
    async def action(request: Request, payload: dict[str, Any] = Body(...)) -> dict[str, Any]:
        """直接下发单个动作，用于调试与测试。

        请求体::

            {"action": "drive", "params": {"linear": 0.2, "duration_ms": 500},
             "device_id": "esp32s3-xx"}
        """
        runtime = _runtime(request)
        name = str(payload.get("action") or "").strip()
        if not name:
            raise HTTPException(status_code=422, detail="缺少 action")

        params = payload.get("params") or {}
        if not isinstance(params, dict):
            raise HTTPException(status_code=422, detail="params 必须是对象")

        try:
            robot = runtime.robots.get(payload.get("device_id") or None)
            envelope = await robot.conn.command(name, params)
        except DeviceOfflineError as exc:
            raise HTTPException(status_code=409, detail=exc.message) from exc
        except SparkBotError as exc:
            raise HTTPException(status_code=500, detail=exc.message) from exc

        return {"ok": True, "action": name, "device_id": robot.device_id, "result": envelope.raw.get("data") or {}}

    @app.post("/api/face")
    async def face(request: Request, payload: dict[str, Any] = Body(...)) -> dict[str, Any]:
        """快捷设置表情，便于控制台按钮直接调用。"""
        runtime = _runtime(request)
        try:
            robot = runtime.robots.get(payload.get("device_id") or None)
            result = await robot.set_face(payload.get("emotion") or Emotion.NEUTRAL)
        except DeviceOfflineError as exc:
            raise HTTPException(status_code=409, detail=exc.message) from exc
        except SparkBotError as exc:
            raise HTTPException(status_code=500, detail=exc.message) from exc
        return {"ok": True, **result}

    @app.post("/api/agent/reset")
    async def reset_agent(request: Request, payload: dict[str, Any] = Body(default={})) -> dict[str, Any]:
        """清空对话历史（保留人格设定）。"""
        runtime = _runtime(request)
        count = runtime.agents.reset(payload.get("device_id") or None)
        return {"ok": True, "reset": count}

    # ---------------------------------------------------------------- #
    # 配置
    # ---------------------------------------------------------------- #
    @app.get("/api/config")
    async def get_config(request: Request) -> dict[str, Any]:
        """返回当前可编辑配置与元信息（密钥只回"是否已设置"）。

        响应结构::

            {"ok": true,
             "config":  {"llm.provider": "mock", "llm.api_key": "",
                         "llm.api_key_set": false, ...},
             "fields":  ["llm.provider", ...],
             "secrets": ["llm.api_key", ...],
             "choices": {"llm.provider": ["mock", "openai", ...]},
             "runtime": {...},           # 实际生效的 provider / model
             "provider_hints": {...}}    # 各 provider 需要填哪些字段
        """
        from . import config as cfg

        runtime = _runtime(request)
        return {
            "ok": True,
            "config": cfg.config_snapshot(runtime.settings),
            "fields": list(cfg.EDITABLE_FIELDS),
            "secrets": [f for f in cfg.EDITABLE_FIELDS if cfg.is_secret_field(f)],
            "choices": cfg.PROVIDER_CHOICES,
            "runtime": runtime.status().to_dict(),
            "provider_hints": _PROVIDER_HINTS,
        }

    @app.post("/api/config")
    async def update_config(request: Request, payload: dict[str, Any] = Body(...)) -> dict[str, Any]:
        """更新配置并热重建 provider。

        请求体::

            {"values": {"llm.provider": "deepseek",
                        "llm.model": "deepseek-chat",
                        "llm.api_key": "sk-xxx"},
             "persist": true,          # 是否写入 .env（默认 true）
             "reload_provider": true}  # 是否立刻重建 provider（默认 true）

        关于密钥：``llm.api_key`` 传空字符串表示**保持原值不变**——
        前端拿到的是打码后的空串，不能让它把已配置的 key 抹掉。
        要真正清空请传 ``null``。
        """
        from . import config as cfg

        runtime = _runtime(request)
        values = payload.get("values") or {}
        if not isinstance(values, dict):
            raise HTTPException(status_code=422, detail="values 必须是对象")

        unknown = [key for key in values if key not in cfg.EDITABLE_FIELDS]
        if unknown:
            raise HTTPException(
                status_code=422,
                detail=f"不可修改的配置项: {', '.join(unknown)}；"
                       f"可修改项见 GET /api/config 的 fields 字段",
            )

        settings = runtime.settings
        applied: dict[str, Any] = {}
        env_updates: dict[str, str] = {}

        for field, raw_value in values.items():
            secret = cfg.is_secret_field(field)
            if secret and raw_value == "":
                continue  # 留空即不变

            try:
                current = cfg.get_field(settings, field)
            except (AttributeError, KeyError):
                current = None

            try:
                coerced = _coerce_config_value(raw_value, current)
                cfg.set_field(settings, field, coerced)
            except (TypeError, ValueError) as exc:
                raise HTTPException(status_code=422,
                                    detail=f"{field} 取值非法: {exc}") from exc

            applied[field] = "***" if secret else coerced
            env_updates[cfg.EDITABLE_FIELDS[field]] = (
                cfg.secret_plain(settings, field) if secret else str(coerced)
            )

        env_path = _env_path()
        persisted = 0
        if bool(payload.get("persist", True)) and env_updates:
            try:
                persisted = cfg.persist_env(env_path, env_updates)
            except OSError as exc:
                # 写盘失败不回滚内存配置，但必须让用户知道重启会丢。
                logger.warning("写入 .env 失败: %s", exc)
                raise HTTPException(
                    status_code=500,
                    detail=f"配置已生效但写入 .env 失败（重启后会丢失）: {exc}",
                ) from exc

        status_payload: dict[str, Any] = {}
        if bool(payload.get("reload_provider", True)):
            try:
                status_payload = runtime.apply_config(settings)
            except SparkBotError as exc:
                raise HTTPException(status_code=400, detail=exc.message) from exc

        return {
            "ok": True,
            "applied": applied,
            "persisted": persisted,
            "env_file": str(env_path),
            "runtime": status_payload or runtime.status().to_dict(),
        }

    @app.post("/api/config/test")
    async def test_config(request: Request) -> dict[str, Any]:
        """用当前配置对 LLM 做一次最小连通性测试。

        只要求模型回一句「收到」，调用成本极低——用来确认
        key、base_url、模型名三者是否配对，而不是评测模型能力。
        """
        runtime = _runtime(request)
        return await runtime.test_llm()

    # ---------------------------------------------------------------- #
    # 控制台
    # ---------------------------------------------------------------- #
    @app.get("/", response_class=HTMLResponse)
    async def console() -> HTMLResponse:
        """极简控制台：对话、看状态、按表情、急停、看串口日志。

        响应显式禁用缓存。原因：这个页面是内联的单个 HTML（没有独立的
        .js/.css 资源带指纹），浏览器会把它连同内联脚本一起缓存。用户
        改了服务端代码后刷新页面仍拿到旧脚本，表现为"点按钮毫无反应"
        —— 排查时极易误判成后端坏了。加 no-store 后每次刷新都拿最新。

        另外注入一个 UI 版本号，浏览器控制台会打印它，用来快速确认
        "页面到底是不是最新的"。
        """
        html = _CONSOLE_HTML.replace("__UI_VERSION__", _UI_VERSION)
        return HTMLResponse(
            content=html,
            headers={
                "Cache-Control": "no-store, no-cache, must-revalidate, max-age=0",
                "Pragma": "no-cache",
                "Expires": "0",
                # 便于自动化与排查：这个头随每次改动变化
                "X-SparkBot-UI": _UI_VERSION,
            },
        )

    @app.exception_handler(DeviceError)
    async def device_error_handler(request: Request, exc: DeviceError) -> JSONResponse:
        """把设备层错误统一映射成 502，便于前端区分「设备问题」。"""
        return JSONResponse(status_code=502, content={"ok": False, "error": exc.message})


# --------------------------------------------------------------------------- #
# 内置控制台
# --------------------------------------------------------------------------- #
_CONSOLE_HTML = """<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>SparkBot 控制台</title>
<style>
  :root { color-scheme: dark; }
  * { box-sizing: border-box; }
  body { margin: 0; font: 14px/1.6 system-ui, "Segoe UI", "Microsoft YaHei", sans-serif;
         background: #0f1216; color: #e6edf3; }
  header { padding: 14px 20px; border-bottom: 1px solid #232a33; display: flex;
           align-items: center; gap: 16px; flex-wrap: wrap; }
  h1 { font-size: 16px; margin: 0; font-weight: 600; }
  .pill { padding: 2px 10px; border-radius: 999px; background: #1b2430; font-size: 12px;
          border: 1px solid #2b3644; }
  .ok { color: #3fb950; } .bad { color: #f85149; }
  main { display: grid; grid-template-columns: minmax(0, 2fr) minmax(280px, 1fr); gap: 16px;
         padding: 16px 20px; }
  @media (max-width: 860px) { main { grid-template-columns: 1fr; } }
  section { background: #131922; border: 1px solid #232a33; border-radius: 10px; padding: 14px; }
  h2 { font-size: 13px; margin: 0 0 10px; color: #8b98a5; font-weight: 600;
       text-transform: uppercase; letter-spacing: .04em; }
  #log { height: 46vh; overflow-y: auto; display: flex; flex-direction: column; gap: 8px; }
  .msg { padding: 8px 11px; border-radius: 8px; max-width: 88%; white-space: pre-wrap; word-break: break-word; }
  .user { background: #1f6feb22; border: 1px solid #1f6feb55; align-self: flex-end; }
  .bot { background: #21262d; border: 1px solid #30363d; }
  .tool { font-size: 12px; color: #8b98a5; font-family: ui-monospace, Consolas, monospace; }
  .err { color: #f85149; }
  form { display: flex; gap: 8px; margin-top: 12px; }
  input[type=text] { flex: 1; padding: 9px 11px; border-radius: 8px; border: 1px solid #30363d;
                     background: #0d1117; color: inherit; font: inherit; }
  button { padding: 9px 14px; border-radius: 8px; border: 1px solid #30363d; background: #21262d;
           color: inherit; font: inherit; cursor: pointer; }
  button:hover { background: #2d333b; }
  button.danger { background: #da3633; border-color: #f85149; }
  .faces { display: flex; flex-wrap: wrap; gap: 6px; }
  .faces button { padding: 5px 9px; font-size: 12px; }
  pre { margin: 0; font-size: 12px; white-space: pre-wrap; word-break: break-word;
        font-family: ui-monospace, Consolas, monospace; color: #8b98a5; max-height: 34vh; overflow-y: auto; }
  .events { max-height: 26vh; overflow-y: auto; font-size: 12px;
            font-family: ui-monospace, Consolas, monospace; color: #7d8590; }
  /* --- 标签页 --- */
  nav { display: flex; gap: 4px; padding: 0 20px; border-bottom: 1px solid #232a33; }
  nav button { border: none; background: none; border-bottom: 2px solid transparent;
               border-radius: 0; padding: 10px 14px; color: #8b98a5; }
  nav button.active { color: #e6edf3; border-bottom-color: #1f6feb; }
  .hidden { display: none !important; }
  /* --- 设置面板 --- */
  .grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(240px, 1fr)); gap: 12px; }
  .field label { display: block; font-size: 11px; color: #8b98a5; margin-bottom: 4px;
                 font-family: ui-monospace, Consolas, monospace; }
  .field input, .field select, .field textarea {
    width: 100%; padding: 8px 10px; border-radius: 6px; border: 1px solid #30363d;
    background: #0d1117; color: inherit; font: inherit; }
  .field textarea { min-height: 62px; resize: vertical; font-size: 13px; }
  .field .hint { font-size: 11px; color: #6e7681; margin-top: 3px; }
  .hint-ok { color: #3fb950; }
  .bar { display: flex; gap: 8px; align-items: center; flex-wrap: wrap; margin-top: 14px; }
  .bar .grow { flex: 1; }
  #cfg-msg { font-size: 12px; font-family: ui-monospace, Consolas, monospace; }
  fieldset { border: 1px solid #232a33; border-radius: 8px; padding: 12px 14px; margin: 0 0 14px; }
  legend { font-size: 11px; color: #8b98a5; text-transform: uppercase;
           letter-spacing: .04em; padding: 0 6px; }
</style>
</head>
<body>
<header>
  <h1>🤖 SparkBot 控制台</h1>
  <span class="pill" id="s-status">连接中…</span>
  <span class="pill" id="s-device">设备：—</span>
  <button class="danger" onclick="estop()">急停</button>
  <button onclick="reset()">清空对话</button>
</header>
<nav>
  <button id="tab-chat" class="active" onclick="switchTab('chat')">对话</button>
  <button id="tab-serial" onclick="switchTab('serial')">串口日志</button>
  <button id="tab-config" onclick="switchTab('config')">设置</button>
</nav>

<div id="pane-chat">
<main>
  <section>
    <h2>对话</h2>
    <div id="log"></div>
    <form onsubmit="send(event)">
      <input type="text" id="input" placeholder="说点什么，例如：你前面有什么东西？" autocomplete="off">
      <button type="submit">发送</button>
      <button type="button" onclick="voiceTurn()" title="等价于对着板子说唤醒词：板子采集 → 识别 → 回复 → 从板子喇叭说出来">🎤 语音对话</button>
    </form>
  </section>
  <aside style="display:flex;flex-direction:column;gap:16px">
    <section>
      <h2>表情</h2>
      <div class="faces" id="faces"></div>
    </section>
    <section>
      <h2>状态</h2>
      <pre id="status">—</pre>
    </section>
    <section>
      <h2>事件流</h2>
      <div class="events" id="events"></div>
    </section>
  </aside>
</main>
</div>

<div id="pane-serial" class="hidden">
<main style="grid-template-columns: minmax(0,1fr)">
  <section>
    <h2>串口日志</h2>
    <p class="field hint" style="margin-top:-4px">
      服务端独占打开串口，把固件输出通过网络推到本页。<b>串口同一时刻只能被一个程序打开</b>，
      用完请点「关闭串口」释放出去。
    </p>
    <div class="bar" style="flex-wrap:wrap;gap:8px">
      <label class="field" style="flex-direction:row;align-items:center;gap:6px">
        端口
        <select id="ser-port" style="min-width:140px"></select>
      </label>
      <label class="field" style="flex-direction:row;align-items:center;gap:6px">
        波特率
        <select id="ser-baud" style="min-width:110px">
          <option>115200</option><option>921600</option>
          <option>460800</option><option>230400</option>
          <option>74880</option><option>9600</option>
        </select>
      </label>
      <button type="button" id="ser-open" onclick="serialOpen()">打开串口</button>
      <button type="button" class="danger" id="ser-close" onclick="serialClose()">关闭串口</button>
      <button type="button" onclick="serialClear()">清空</button>
      <label class="field" style="flex-direction:row;align-items:center;gap:6px">
        <input type="checkbox" id="ser-follow" checked style="width:auto"> 自动滚动
      </label>
      <label class="field" style="flex-direction:row;align-items:center;gap:6px">
        <input type="checkbox" id="ser-wrap" style="width:auto"> 自动换行
      </label>
      <span id="ser-msg" class="hint"></span>
    </div>
    <div class="bar" style="gap:8px">
      <label class="field" style="flex-direction:row;align-items:center;gap:6px">
        过滤
        <input type="text" id="ser-filter" placeholder="只显示含此关键字的行，留空显示全部"
               oninput="serialRender()" style="min-width:280px">
      </label>
      <span class="grow"></span>
      <span class="pill" id="ser-status">未打开</span>
    </div>
    <pre id="ser-log" style="max-height:60vh;overflow:auto;white-space:pre;font-family:Consolas,monospace;font-size:12px;line-height:1.5"></pre>
  </section>
</main>
</div>

<div id="pane-config" class="hidden">
<main style="grid-template-columns: minmax(0,1fr)">
  <section>
    <h2>模型与语音配置</h2>
    <p class="field hint" style="margin-top:-4px">
      修改后立即生效并写入 <code>.env</code>。<b>API Key 留空表示保持不变</b>，
      保存后会一直显示为「已设置」而不再回显明文。
    </p>
    <form onsubmit="saveConfig(event)">
      <fieldset><legend>大模型（决策）</legend><div class="grid" id="grp-llm"></div></fieldset>
      <fieldset><legend>视觉理解（看图）</legend><div class="grid" id="grp-vision"></div></fieldset>
      <fieldset><legend>语音（说与听）</legend><div class="grid" id="grp-speech"></div></fieldset>
      <fieldset><legend>安全与行为</legend><div class="grid" id="grp-behavior"></div></fieldset>
      <fieldset><legend>人格</legend><div class="grid" id="grp-persona"></div></fieldset>
      <div class="bar">
        <button type="submit">保存并生效</button>
        <button type="button" onclick="testLlm()">测试连接</button>
        <button type="button" onclick="loadConfig()">重新载入</button>
        <span class="grow"></span>
        <span id="cfg-msg"></span>
      </div>
    </form>
  </section>
  <aside style="display:flex;flex-direction:column;gap:16px">
    <section>
      <h2>当前生效</h2>
      <pre id="cfg-runtime">—</pre>
    </section>
  </aside>
</main>
</div>
<script>
const EMOTIONS = ["neutral","happy","sad","angry","surprised","sleepy","confused",
                  "thinking","love","excited","scared","bored"];
const log = document.getElementById("log");
const events = document.getElementById("events");

function add(text, cls) {
  const div = document.createElement("div");
  div.className = "msg " + cls;
  div.textContent = text;
  log.appendChild(div);
  log.scrollTop = log.scrollHeight;
  return div;
}

async function send(e) {
  e.preventDefault();
  const input = document.getElementById("input");
  const text = input.value.trim();
  if (!text) return;
  input.value = "";
  add(text, "user");
  const pending = add("…思考中", "bot");
  try {
    const res = await fetch("api/chat", {
      method: "POST",
      headers: {"Content-Type": "application/json"},
      body: JSON.stringify({text})
    });
    const data = await res.json();
    if (!res.ok) { pending.className = "msg bot err"; pending.textContent = data.detail || "请求失败"; return; }
    pending.remove();
    (data.tools || []).forEach(t => add(
      `🔧 ${t.name}(${JSON.stringify(t.arguments)}) → ${t.ok ? "成功" : "失败"} ${t.duration_ms}ms`
        + (t.ok ? "" : " " + (t.error || "")), "tool"));
    add(data.reply || "(空回复)", "bot");
  } catch (err) {
    pending.className = "msg bot err";
    pending.textContent = "网络错误：" + err;
  }
}

async function estop() {
  await fetch("api/stop", {method: "POST"});
  add("⛔ 已触发急停", "tool");
}

/* 语音对话：等价于对着板子说唤醒词。
 * 板子会先"叮"一声表示可以说话了，然后采集 → 识别 → 回复 → 从板子喇叭说出。 */
async function voiceTurn() {
  add("🎤 语音对话开始 —— 注意听板子先「叮」一声，然后对着板子说话（约 8 秒）", "tool");
  const pending = add("…采集中（说话吧）", "bot");
  try {
    const res = await fetch("api/voice/trigger", {
      method: "POST",
      headers: {"Content-Type": "application/json"},
      body: JSON.stringify({wait: true})
    });
    const data = await res.json();
    pending.remove();
    if (!res.ok) { add("语音对话失败：" + (data.detail || "未知原因"), "tool err"); return; }
    if (data.heard) add("👂 听到：" + data.heard, "tool");
    else add("没有听清（可能没说话，或麦克风没拾到）", "tool");
    if (data.replied) add(data.replied, "bot");
    add(`⏱ 整轮耗时 ${data.elapsed_ms} ms`, "tool");
  } catch (err) {
    pending.remove();
    add("语音对话失败：" + err, "tool err");
  }
}

async function reset() {
  await fetch("api/agent/reset", {method: "POST",
    headers: {"Content-Type": "application/json"}, body: "{}"});
  log.innerHTML = "";
  add("对话已清空", "tool");
}

function buildFaces() {
  const box = document.getElementById("faces");
  EMOTIONS.forEach(name => {
    const b = document.createElement("button");
    b.textContent = name;
    b.onclick = async () => {
      const res = await fetch("api/face", {method: "POST",
        headers: {"Content-Type": "application/json"}, body: JSON.stringify({emotion: name})});
      add(res.ok ? `😀 表情 → ${name}` : `表情设置失败：${name}`, "tool");
    };
    box.appendChild(b);
  });
}

let statusTick = 0;
async function refreshStatus() {
  statusTick++;
  const pill = document.getElementById("s-status");
  const devPill = document.getElementById("s-device");
  try {
    const [s, d] = await Promise.all([
      fetch("api/status").then(r => {
        if (!r.ok) throw new Error("api/status 返回 " + r.status);
        return r.json();
      }),
      fetch("api/devices").then(r => {
        if (!r.ok) throw new Error("api/devices 返回 " + r.status);
        return r.json();
      })
    ]);
    pill.textContent =
      `LLM ${s.llm_provider}/${s.llm_model} · 工具 ${s.tools} · 语音 ${s.voice_loop ? "开" : "关"}`;
    // 成功时清掉失败留下的红色标记，否则一旦失败过一次就永远是红的。
    pill.className = "pill";
    // telemetry 可能是 null（设备刚连上还没上报），用可选链兜住，
    // 否则这里抛异常会让整个 refreshStatus 静默失败。
    const first = (d.devices || [])[0];
    const telem = first && first.telemetry;
    const batt = telem && telem.battery ? telem.battery.percent : null;
    devPill.innerHTML = first
      ? `<span class="ok">●</span> ${first.device_id} · 电量 ${batt ?? "?"}%`
      : `<span class="bad">●</span> 无设备`;
    const st = document.getElementById("status");
    if (st) st.textContent = JSON.stringify({runtime: s, device: first || null}, null, 2);
  } catch (err) {
    // 失败必须**看得见**。早先这里把异常整个吞掉，结果页面一直显示
    // 「连接中…」，完全不知道是接口错了、网络断了还是脚本没跑，
    // 排查时只能靠猜。现在把错误直接写到状态条上。
    pill.textContent = `状态获取失败（第 ${statusTick} 次）：${err.message || err}`;
    pill.className = "pill bad";
  }
}

function connectEvents() {
  const ws = new WebSocket((location.protocol === "https:" ? "wss://" : "ws://") + location.host + "/api/events");
  ws.onmessage = e => {
    const ev = JSON.parse(e.data);
    const line = document.createElement("div");
    line.textContent = `${new Date(ev.ts * 1000).toLocaleTimeString()} ${ev.topic} ${
      JSON.stringify(ev.payload).slice(0, 160)}`;
    events.appendChild(line);
    while (events.childElementCount > 200) events.removeChild(events.firstChild);
    events.scrollTop = events.scrollHeight;
    if (ev.topic.startsWith("device.") || ev.topic.startsWith("agent.")) refreshStatus();
  };
  ws.onclose = () => setTimeout(connectEvents, 2000);
}

buildFaces();
refreshStatus();
connectEvents();
setInterval(refreshStatus, 5000);

// 版本标记：__UI_VERSION__ 由服务端在返回页面时替换成当前版本号。
// 作用和用途：控制台是单个内联 HTML，浏览器会缓存它。排查"点了没反应"
// 时第一件事就是确认页面是不是最新的 —— 打开浏览器控制台就能看到这行，
// 与 http://127.0.0.1:8765/health 里的 ui_version 对照即可。
// 样式参数用 %c 加第二个实参，否则格式符会原样显示出来。
console.log("[SparkBot 控制台] UI 版本 __UI_VERSION__");

/* ------------------------------------------------------------------ *
 * 标签页
 * ------------------------------------------------------------------ */
function switchTab(name) {
  // 三个面板共用一套显隐逻辑：先全部隐藏，再只显示目标那个。
  const panes = {chat: "pane-chat", serial: "pane-serial", config: "pane-config"};
  for (const [key, id] of Object.entries(panes)) {
    document.getElementById(id).classList.toggle("hidden", key !== name);
    document.getElementById("tab-" + key).classList.toggle("active", key === name);
  }
  if (name === "config") loadConfig();
  if (name === "serial") serialInit();
}

/* ------------------------------------------------------------------ *
 * 串口日志
 *
 * 数据路径：固件 → COM15 → 服务端读取线程 → WebSocket(/api/serial/stream)
 *          → 本页。浏览器不能直接开串口，所以必须走服务端中转。
 * ------------------------------------------------------------------ */
let serSocket = null;
let serLines = [];        // 全部已收到的行
let serMaxLines = 3000;   // 前端也设上限，防止长时间挂在页面上吃满内存

function serSetStatus(text, cls) {
  const el = document.getElementById("ser-status");
  el.textContent = text;
  el.className = "pill" + (cls ? " " + cls : "");
}

function serMsg(text) {
  document.getElementById("ser-msg").textContent = text || "";
}

/** 首次进入标签页时加载端口列表并补历史。 */
async function serialInit() {
  if (serSocket === null) {
    await serialLoadPorts();
    await serialLoadRecent();
  }
  serialConnectStream();
}

async function serialLoadPorts() {
  try {
    const res = await fetch("api/serial/ports");
    const data = await res.json();
    const sel = document.getElementById("ser-port");
    const want = sel.value || "COM15";   // 常用端口优先
    sel.innerHTML = "";
    const ports = data.ports || [];
    if (!ports.length) {
      const opt = document.createElement("option");
      opt.textContent = "(未检测到串口)";
      opt.value = "";
      sel.appendChild(opt);
    }
    for (const p of ports) {
      const opt = document.createElement("option");
      opt.value = p.device;
      opt.textContent = p.device + (p.description ? " — " + p.description : "");
      sel.appendChild(opt);
    }
    // 尽量保持/恢复选择：没有 COM15 时退回第一项
    const has = ports.some(p => p.device === want);
    sel.value = has ? want : (ports[0] ? ports[0].device : "");
    serApplyStatus(data.status);
  } catch (e) {
    serMsg("加载串口列表失败：" + e);
  }
}

function serApplyStatus(st) {
  if (!st) return;
  if (st.pyserial === false) {
    serSetStatus("未安装 pyserial", "bad");
    serMsg("服务端缺少 pyserial，请执行：python -m pip install pyserial");
    return;
  }
  if (st.open) {
    serSetStatus("已打开 " + st.port, "ok");
    document.getElementById("ser-port").value = st.port;
  } else {
    serSetStatus("未打开");
  }
  if (st.error) serMsg(st.error);
}

async function serialLoadRecent() {
  try {
    const res = await fetch("api/serial/recent?limit=1000");
    const data = await res.json();
    serLines = (data.lines || []).map(l => l);
    serApplyStatus(data.status);
    serialRender();
  } catch (e) { /* 首次没有历史很正常 */ }
}

function serialConnectStream() {
  if (serSocket && (serSocket.readyState === WebSocket.OPEN ||
                    serSocket.readyState === WebSocket.CONNECTING)) return;
  const proto = location.protocol === "https:" ? "wss" : "ws";
  const url = proto + "://" + location.host + "/api/serial/stream";
  serMsg("正在连接日志流 " + url + " …");
  try {
    serSocket = new WebSocket(url);
  } catch (e) {
    serMsg("创建 WebSocket 失败：" + e);
    return;
  }
  serSocket.onopen = () => serMsg("日志流已连接");
  serSocket.onmessage = (ev) => {
    let msg;
    try { msg = JSON.parse(ev.data); } catch (e) {
      serMsg("日志流数据无法解析（忽略）");
      return;
    }
    if (msg.type === "init") {
      // 服务端补发的历史（连接建立时可能已经积累了一些）
      serLines = msg.lines || [];
      serApplyStatus(msg.status);
      serMsg("日志流已就绪，已载入 " + serLines.length + " 行历史");
    } else if (msg.text !== undefined) {
      serLines.push(msg);
    }
    if (serLines.length > serMaxLines) {
      serLines = serLines.slice(-serMaxLines);
    }
    serialRender();
  };
  serSocket.onerror = () => serMsg("日志流连接出错（服务端在跑吗？页面是不是缓存了旧版本？）");
  serSocket.onclose = (ev) => {
    serSetStatus("日志流已断开");
    serMsg("日志流已断开 (code=" + ev.code + ")。刷新页面可重连。");
    serSocket = null;
  };
}

function serialRender() {
  const box = document.getElementById("ser-log");
  const filter = (document.getElementById("ser-filter").value || "").trim();
  const follow = document.getElementById("ser-follow").checked;
  const wrap = document.getElementById("ser-wrap").checked;

  const rows = filter
    ? serLines.filter(l => (l.text || "").includes(filter))
    : serLines;

  box.style.whiteSpace = wrap ? "pre-wrap" : "pre";
  if (!rows.length) {
    box.textContent = filter ? "（没有匹配的行）" : "（暂无输出）";
    return;
  }
  // 每行带时间戳，与串口工具的格式一致。
  //
  // 分隔符必须用两个反斜杠加 n 的形式（Python 源码里写四个反斜杠），
  // 这样浏览器收到的才是 JS 的换行转义：反斜杠 + n。
  //
  // 如果 Python 源码里只写一个反斜杠，Python 会在生成 HTML 时就把它
  // 换成真正的换行符，于是 JS 里的字符串字面量跨行，浏览器报
  // SyntaxError: Invalid or unexpected token，整段脚本解析失败 ——
  // 所有函数都不存在，页面表现为状态条永远停在「连接中…」、
  // 点任何标签都没反应。服务端看不出异常（HTML 返回 200、长度正常），
  // 必须用真实浏览器加载才能发现。
  box.textContent = rows.map(l => {
    const t = new Date(l.ts * 1000).toLocaleTimeString("zh-CN", {hour12: false});
    return "[" + t + "] " + (l.text || "");
  }).join("\\n");

  if (follow) box.scrollTop = box.scrollHeight;
}

async function serialOpen() {
  const port = document.getElementById("ser-port").value;
  const baudrate = parseInt(document.getElementById("ser-baud").value, 10) || 115200;
  if (!port) { serMsg("请先选择一个串口"); return; }
  serMsg("正在打开 " + port + " …");
  try {
    const res = await fetch("api/serial/open", {
      method: "POST",
      headers: {"Content-Type": "application/json"},
      body: JSON.stringify({port, baudrate})
    });
    const data = await res.json();
    if (!res.ok) {
      serMsg("打开失败：" + (data.detail || res.status));
      serApplyStatus(await (await fetch("api/serial/status")).json());
      return;
    }
    serMsg("已打开 " + port + " @" + baudrate);
    serApplyStatus(data);
    serialConnectStream();
  } catch (e) {
    serMsg("打开失败：" + e);
  }
}

async function serialClose() {
  try {
    const res = await fetch("api/serial/close", {method: "POST"});
    const data = await res.json();
    serApplyStatus(data);
    serMsg("已关闭串口（现在别的程序可以使用它了）");
  } catch (e) {
    serMsg("关闭失败：" + e);
  }
}

async function serialClear() {
  try {
    await fetch("api/serial/clear", {method: "POST"});
    serLines = [];
    serialRender();
    serMsg("已清空");
  } catch (e) {
    serMsg("清空失败：" + e);
  }
}

/* ------------------------------------------------------------------ *
 * 设置面板
 * ------------------------------------------------------------------ */
// 分组前缀 → 容器 id。字段按 "分组.字段名" 自动落进对应分组。
const GROUPS = {
  llm:      "grp-llm",
  vision:   "grp-vision",
  speech:   "grp-speech",
  behavior: "grp-behavior",
  persona:  "grp-persona",
};
// 字段的中文标签与提示（未列出的字段回退为原始字段名）
const LABELS = {
  "llm.provider":        ["供应商", "mock = 离线假模型，无需 key"],
  "llm.model":           ["模型名", "如 deepseek-chat / gpt-4o-mini"],
  "llm.base_url":        ["接口地址", "仅 openai_compat 必填，如 http://ip:8000/v1"],
  "llm.api_key":         ["API Key", "留空 = 保持原值不变"],
  "llm.temperature":     ["温度", "0~2，越大越随机"],
  "llm.max_tokens":      ["最大输出", "单次回复的 token 上限"],
  "llm.max_tool_rounds": ["工具轮次上限", "防止模型反复调工具停不下来"],
  "vision.enabled":      ["启用视觉", "关掉后模型看不到图像"],
  "vision.provider":     ["供应商", "留空 = 复用大模型的 key 与地址"],
  "vision.model":        ["模型名", "必须是多模态模型，如 gpt-4o-mini"],
  "vision.base_url":     ["接口地址", "留空 = 复用大模型设置"],
  "vision.api_key":      ["API Key", "留空 = 复用大模型设置"],
  "speech.asr_provider": ["识别 (ASR)", "mock = 离线占位"],
  "speech.asr_api_key":  ["识别 Key", "留空 = 保持原值"],
  "speech.tts_provider": ["合成 (TTS)", "mock = 离线提示音"],
  "speech.tts_api_key":  ["合成 Key", "留空 = 保持原值"],
  "speech.tts_voice":    ["发音人", "如 alloy / nova"],
  "behavior.max_linear_mps":     ["限速 线速度", "米/秒，会与设备上限取较小值"],
  "behavior.max_angular_rps":    ["限速 角速度", "弧度/秒"],
  "behavior.max_duration_ms":    ["单次最长运动", "毫秒，防止一直往前冲"],
  "behavior.talking_animation":  ["说话时换表情", "让机器人显得有互动感"],
  "behavior.history_limit":      ["历史条数", "对话记忆保留多少条"],
  "persona":                     ["人格设定", "直接写进 system prompt，改这里就能换性格"],
};

let cfgMeta = null;

async function loadConfig() {
  const msg = document.getElementById("cfg-msg");
  msg.textContent = "";
  try {
    const res = await fetch("api/config");
    const data = await res.json();
    if (!res.ok) { msg.textContent = data.detail || "读取失败"; return; }
    cfgMeta = data;
    Object.values(GROUPS).forEach(id => { document.getElementById(id).innerHTML = ""; });
    data.fields.forEach(field => renderField(field, data));
    renderRuntime(data.runtime);
  } catch (err) {
    msg.textContent = "读取失败：" + err;
  }
}

function renderField(field, data) {
  const group = field.includes(".") ? field.split(".")[0] : "persona";
  const box = document.getElementById(GROUPS[group] || GROUPS.persona);
  if (!box) return;

  const isSecret = (data.secrets || []).includes(field);
  const value = data.config[field];
  const [label, hint] = LABELS[field] || [field, ""];

  const wrap = document.createElement("div");
  wrap.className = "field";
  const lab = document.createElement("label");
  lab.textContent = label;
  lab.title = field;
  wrap.appendChild(lab);

  const choices = (data.choices || {})[field];
  let input;
  if (choices) {
    input = document.createElement("select");
    choices.forEach(c => {
      const opt = document.createElement("option");
      opt.value = c;
      opt.textContent = c === "" ? "(继承大模型设置)" : c;
      input.appendChild(opt);
    });
    input.value = value ?? "";
  } else if (field === "persona") {
    input = document.createElement("textarea");
    input.value = value ?? "";
  } else if (typeof value === "boolean") {
    input = document.createElement("select");
    [["true", "是"], ["false", "否"]].forEach(([v, t]) => {
      const opt = document.createElement("option");
      opt.value = v; opt.textContent = t;
      input.appendChild(opt);
    });
    input.value = value ? "true" : "false";
  } else {
    input = document.createElement("input");
    input.type = (typeof value === "number" && field !== "llm.api_key") ? "number" : "text";
    if (input.type === "number") input.step = "any";
    input.value = isSecret ? "" : (value ?? "");
    if (isSecret) {
      input.type = "password";
      input.placeholder = data.config[field + "_set"]
        ? "已设置（留空表示不变）" : "尚未设置";
    }
  }
  input.id = "cfg-" + field;
  input.dataset.field = field;
  wrap.appendChild(input);

  if (hint || isSecret) {
    const h = document.createElement("div");
    h.className = "hint" + (isSecret && data.config[field + "_set"] ? " hint-ok" : "");
    h.textContent = isSecret && data.config[field + "_set"] ? "已设置 ✓" : hint;
    wrap.appendChild(h);
  }
  box.appendChild(wrap);
}

function renderRuntime(rt) {
  if (!rt) return;
  document.getElementById("cfg-runtime").textContent = JSON.stringify({
    实际使用的模型: `${rt.llm_provider} / ${rt.llm_model}`,
    视觉: rt.vision_provider,
    识别: rt.asr_provider,
    合成: rt.tts_provider,
    工具数: rt.tools,
    在线设备: rt.devices_online,
    语音闭环: rt.voice_loop,
    运行时长秒: rt.uptime_s,
  }, null, 2);
}

function collectConfig() {
  const values = {};
  document.querySelectorAll("[data-field]").forEach(el => {
    const field = el.dataset.field;
    let v = el.value;
    const isSecret = (cfgMeta?.secrets || []).includes(field);
    if (isSecret && v === "") return;           // 留空 = 不变
    if (el.tagName === "SELECT" && v === "true") v = true;
    else if (el.tagName === "SELECT" && v === "false") v = false;
    values[field] = v;
  });
  return values;
}

async function saveConfig(e) {
  e.preventDefault();
  const msg = document.getElementById("cfg-msg");
  msg.textContent = "保存中…";
  msg.className = "";
  try {
    const res = await fetch("api/config", {
      method: "POST",
      headers: {"Content-Type": "application/json"},
      body: JSON.stringify({values: collectConfig(), persist: true})
    });
    const data = await res.json();
    if (!res.ok) { msg.textContent = "✗ " + (data.detail || "保存失败"); msg.className = "err"; return; }
    msg.textContent = `✓ 已生效（写入 .env ${data.persisted} 项）`;
    msg.className = "hint-ok";
    renderRuntime(data.runtime);
    refreshStatus();
  } catch (err) {
    msg.textContent = "✗ 网络错误：" + err;
    msg.className = "err";
  }
}

async function testLlm() {
  const msg = document.getElementById("cfg-msg");
  msg.textContent = "测试中…";
  msg.className = "";
  try {
    const res = await fetch("api/config/test", {method: "POST"});
    const data = await res.json();
    if (data.ok) {
      msg.textContent = `✓ ${data.provider}/${data.model} 连通，${data.latency_ms}ms，回复「${data.reply}」`;
      msg.className = "hint-ok";
    } else {
      msg.textContent = `✗ ${data.provider || ""} 失败：${data.error}`;
      msg.className = "err";
    }
  } catch (err) {
    msg.textContent = "✗ 网络错误：" + err;
    msg.className = "err";
  }
}
</script>
</body>
</html>
"""

#: 控制台页面的版本号。
#:
#: 存在的意义是"确认浏览器拿到的页面是不是最新的"。控制台是**单个内联
#: HTML**，浏览器会连内联脚本一起缓存；改完前端若拿到旧页面，现象是
#: "点按钮毫无反应"，而服务端日志一切正常，极易误判成后端坏了。
#:
#: 用法：浏览器控制台会打印 `[SparkBot 控制台] UI 版本 xxxxxxxx`，
#: 与 `GET /health` 的 `ui_version` 字段对照即可判断是否最新。
#:
#: 用**内容哈希**而不是时间戳：时间戳每次重启都变，无法区分"代码改了"
#: 和"只是重启了"；哈希只在控制台内容真正变化时才变。
_UI_VERSION = hashlib.sha256(_CONSOLE_HTML.encode("utf-8")).hexdigest()[:8]
