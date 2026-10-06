"""启动前自检：一条命令定位「为什么起不来 / 为什么页面打不开」。

用法::

    python check.py
    python check.py --port 8765

不需要任何第三方库就能跑（依赖检查本身会报告缺了什么），
所以哪怕 ``.vendor`` 是空的它也能给出有用的结论。
"""

from __future__ import annotations

import argparse
import platform
import socket
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
VENDOR = ROOT / ".vendor"

OK = "  [OK]  "
WARN = "  [警告]"
FAIL = "  [失败]"


def main() -> int:
    """跑完全部检查，返回退出码（0 正常 / 1 有致命问题）。"""
    parser = argparse.ArgumentParser(description="SparkBot 启动前自检")
    parser.add_argument("--port", type=int, default=8765, help="要检查的端口")
    parser.add_argument("--host", default="0.0.0.0", help="要检查的绑定地址（默认与 run.py 一致）")
    args = parser.parse_args()

    problems: list[str] = []
    warnings: list[str] = []

    print()
    print("=" * 64)
    print("  SparkBot 启动前自检")
    print("=" * 64)

    # ---- 1. 运行环境 ---------------------------------------------------- #
    print("\n[1/6] 运行环境")
    print(f"{OK}Python {platform.python_version()}  ({sys.executable})")
    if sys.version_info < (3, 11):
        print(f"{FAIL}需要 Python 3.11 或更高版本")
        problems.append("Python 版本过低")

    # ---- 2. 项目文件 ---------------------------------------------------- #
    print("\n[2/6] 项目文件")
    required = ["run.py", "requirements.txt", "sparkbot", "sparkbot/app.py"]
    for item in required:
        target = ROOT / item
        if target.exists():
            print(f"{OK}{item}")
        else:
            print(f"{FAIL}{item} 不存在")
            problems.append(f"缺少 {item}")

    # ---- 3. 依赖 -------------------------------------------------------- #
    print("\n[3/6] 第三方依赖")
    if not VENDOR.is_dir():
        print(f"{WARN}.vendor 目录不存在，依赖尚未安装")
        warnings.append(".vendor 缺失")

    if str(VENDOR) not in sys.path:
        sys.path.insert(0, str(VENDOR))

    needed = {
        "fastapi": "fastapi",
        "uvicorn": "uvicorn",
        "websockets": "websockets",
        "httpx": "httpx",
        "pydantic": "pydantic",
        "pydantic_settings": "pydantic-settings",
    }
    missing = []
    for module, package in needed.items():
        try:
            __import__(module)
        except ImportError:
            missing.append(package)
            print(f"{FAIL}{package} 未安装")
        else:
            print(f"{OK}{package}")

    if missing:
        problems.append(f"缺少依赖: {', '.join(missing)}")
        print()
        print("       安装命令（在项目根目录执行）：")
        print(f"         {Path(sys.executable).name} -m pip install --target .vendor -r requirements.txt")

    # 可选依赖只提示
    try:
        import PIL  # noqa: F401

        print(f"{OK}Pillow（可选：模拟摄像头出图更真实）")
    except ImportError:
        print(f"{WARN}Pillow 未安装（可选，不影响启动）")

    # ---- 4. 端口 -------------------------------------------------------- #
    print(f"\n[4/6] 端口 {args.host}:{args.port}")
    if _port_in_use(args.host, args.port):
        print(f"{WARN}端口已被占用 —— 这通常说明**服务其实已经启动了**")
        print(f"       先直接试： http://127.0.0.1:{args.port}/")
        print(f"       或查占用进程： netstat -ano | findstr :{args.port}")
        print(f"       或换端口启动： python run.py --port {args.port + 1}")
        warnings.append(f"端口 {args.port} 被占用（可能已有实例在跑）")
    else:
        print(f"{OK}端口空闲，可以启动")

    if missing:
        # 依赖都没装齐，后面的检查没有意义。
        _summarize(problems, warnings)
        return 1

    # ---- 5. 应用能否装配 ------------------------------------------------ #
    print("\n[5/6] 应用装配")
    try:
        from sparkbot.config import Settings
        from sparkbot.paths import ensure_path

        ensure_path()
        from sparkbot.app import create_app

        settings = Settings()
        settings.server.log_level = "warning"
        app = create_app(settings)
        routes = {getattr(r, "path", "") for r in app.routes}
        print(f"{OK}FastAPI 应用创建成功，注册路由 {len(routes)} 个")
        for path in ("/", "/api/status", "/api/config", "/api/chat", "/robot"):
            mark = OK if path in routes else FAIL
            print(f"{mark}路由 {path}")
            if path not in routes:
                problems.append(f"路由 {path} 缺失")
    except Exception as exc:  # noqa: BLE001 - 自检必须把异常变成可读结论
        print(f"{FAIL}应用创建失败: {type(exc).__name__}: {exc}")
        problems.append("应用无法装配")
        import traceback

        traceback.print_exc()
        _summarize(problems, warnings)
        return 1

    # ---- 6. 配置 -------------------------------------------------------- #
    print("\n[6/6] 配置")
    env_file = ROOT / ".env"
    if env_file.exists():
        print(f"{OK}.env 存在（{env_file}）")
        try:
            from sparkbot.config import secret_plain

            if secret_plain(settings, "llm.api_key"):
                print(f"{OK}已配置 LLM API Key（provider={settings.llm.provider}）")
            else:
                print(f"{OK}未配置 LLM Key，将使用离线假模型（provider={settings.llm.provider}）")
        except Exception:  # noqa: BLE001
            pass
    else:
        print(f"{OK}无 .env，使用内置默认值（离线假模型，不需要 Key）")

    print(f"{OK}LLM      {settings.llm.provider} / {settings.llm.model}")
    print(f"{OK}视觉     {'开' if settings.vision.enabled else '关'}")
    print(f"{OK}语音     ASR={settings.speech.asr_provider} TTS={settings.speech.tts_provider}")

    _summarize(problems, warnings)
    return 1 if problems else 0


def _port_in_use(host: str, port: int) -> bool:
    """检测端口是否已被占用。

    **必须用与 uvicorn 相同的地址去试绑定。** Windows 下
    ``0.0.0.0:8765`` 与 ``127.0.0.1:8765`` 不算冲突：拿 ``127.0.0.1``
    去探测，即使服务已经占用 ``0.0.0.0:8765`` 也会"成功"，
    于是自检给出"端口空闲"的错误结论，最后还是要 uvicorn 炸一次。
    """
    family = socket.AF_INET6 if ":" in host else socket.AF_INET
    with socket.socket(family, socket.SOCK_STREAM) as probe:
        try:
            # 不设 SO_REUSEADDR：设了会把"已占用"误判为可用。
            probe.bind((host, port))
        except OSError:
            return True
    return False


def _summarize(problems: list[str], warnings: list[str]) -> None:
    """打印结论与下一步操作。"""
    print()
    print("=" * 64)
    if problems:
        print("  结论：还不能启动，请先解决：")
        for item in problems:
            print(f"    · {item}")
        print()
        print("  装依赖：python -m pip install --target .vendor -r requirements.txt")
        print("=" * 64)
        print()
        return

    if warnings:
        print("  结论：可以启动，但请注意：")
        for item in warnings:
            print(f"    · {item}")
        print()
        print("  如果上面说端口被占用，说明服务**很可能已经在跑了**，")
        print(f"  直接打开 http://127.0.0.1:{args.port}/ 看看。")
        print("=" * 64)
        print()
        return

    print("  结论：一切正常。启动命令：")
    print()
    print("      python run.py")
    print()
    print("  然后用浏览器打开：")
    print("      http://127.0.0.1:8765/")
    print()
    print("  提示：服务是**前台运行**的，那个终端窗口必须一直开着。")
    print("        关掉窗口 = 服务停止 = 页面打不开。")
    print("=" * 64)
    print()


if __name__ == "__main__":
    raise SystemExit(main())
