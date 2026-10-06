"""``python -m sparkbot`` 入口：启动 PC 端服务。

用法::

    python -m sparkbot                          # 读取 .env / 环境变量
    python -m sparkbot --host 0.0.0.0 --port 8765
    python -m sparkbot --provider mock          # 离线跑通全链路

环境变量前缀统一为 ``SPARKBOT_``，例如::

    SPARKBOT_LLM_PROVIDER=deepseek
    SPARKBOT_LLM_MODEL=deepseek-chat
    SPARKBOT_LLM_API_KEY=sk-xxxx
"""

from __future__ import annotations

import argparse
import sys

# 必须在导入任何第三方库之前完成依赖路径引导。
from .paths import ensure_path, install_hint, missing_dependencies

ensure_path()


def build_arg_parser() -> argparse.ArgumentParser:
    """构造命令行参数解析器。"""
    parser = argparse.ArgumentParser(
        prog="python -m sparkbot",
        description="SparkBot —— 面向 ESP32-S3 移动机器人的 PC 端 Agent 服务",
    )
    parser.add_argument("--host", default=None, help="监听地址，默认 0.0.0.0")
    parser.add_argument("--port", type=int, default=None, help="监听端口，默认 8765")
    parser.add_argument("--path", default=None, help="设备接入的 WebSocket 路径，默认 /robot")
    parser.add_argument(
        "--provider",
        default=None,
        choices=["mock", "openai", "deepseek", "openai_compat"],
        help="LLM provider；默认 mock（离线可跑）",
    )
    parser.add_argument("--model", default=None, help="模型名")
    parser.add_argument("--api-key", default=None, help="API key（也可用环境变量传入）")
    parser.add_argument("--base-url", default=None, help="OpenAI 兼容网关的 base_url")
    parser.add_argument("--log-level", default=None,
                        choices=["debug", "info", "warning", "error"], help="日志级别")
    parser.add_argument("--reload", action="store_true", help="代码变更自动重启（开发用）")
    return parser


def check_port_available(host: str, port: int) -> None:
    """启动前检查端口是否被占用，占用则给出可操作的提示。

    没有这道检查时，端口冲突的表现是 uvicorn 打出一串
    ``[Errno 10048] error while attempting to bind on address``，
    新手很难看出是自己已经开了一个实例。

    **必须用与 uvicorn 完全相同的地址去探测。** Windows 下
    ``0.0.0.0:8765`` 与 ``127.0.0.1:8765`` 不算冲突：如果拿
    ``127.0.0.1`` 去试绑定，即使 ``0.0.0.0:8765`` 已被占用也会"成功"，
    于是检查形同虚设，最终还是要在 uvicorn 那里炸一次。
    """
    import socket

    family = socket.AF_INET6 if ":" in host else socket.AF_INET
    with socket.socket(family, socket.SOCK_STREAM) as sock:
        # 刻意不设 SO_REUSEADDR：设了就会把"已被占用"误判为可用。
        try:
            sock.bind((host, port))
        except OSError:
            hint = {
                8765: "（这是 SparkBot 的默认端口，通常说明已经有一个实例在跑了）",
            }.get(port, "")
            print(
                f"\n端口 {host}:{port} 已被占用{hint}\n"
                f"  先直接试： http://127.0.0.1:{port}/\n"
                f"  查占用进程：netstat -ano | findstr :{port}\n"
                f"  换一个端口：python run.py --port {port + 1}\n",
                file=sys.stderr,
            )
            raise SystemExit(3) from None


def main(argv: list[str] | None = None) -> int:
    """命令行入口，返回进程退出码。"""
    missing = missing_dependencies()
    if missing:
        print(f"缺少依赖: {', '.join(missing)}\n\n{install_hint()}", file=sys.stderr)
        return 2

    args = build_arg_parser().parse_args(argv)

    # 命令行参数优先，其次环境变量，最后内置默认值。
    from .config import get_settings

    settings = get_settings()
    if args.host:
        settings.server.host = args.host
    if args.port:
        settings.server.port = args.port
    if args.path:
        settings.server.ws_path = args.path
    if args.log_level:
        settings.server.log_level = args.log_level
    if args.provider:
        settings.llm.provider = args.provider
    if args.model:
        settings.llm.model = args.model
    if args.base_url:
        settings.llm.base_url = args.base_url
    if args.api_key:
        from pydantic import SecretStr

        settings.llm.api_key = SecretStr(args.api_key)

    check_port_available(settings.server.host, settings.server.port)
    _print_banner(settings)

    import uvicorn  # noqa: PLC0415 - 放到依赖检查之后

    from .app import create_app  # noqa: PLC0415

    uvicorn.run(
        create_app(settings),
        host=settings.server.host,
        port=settings.server.port,
        log_level=settings.server.log_level,
        reload=args.reload,
    )
    return 0


def _print_banner(settings: object) -> None:
    """打印启动信息与关键提示。"""
    llm = settings.llm  # type: ignore[attr-defined]
    server = settings.server  # type: ignore[attr-defined]
    speech = settings.speech  # type: ignore[attr-defined]

    lines = [
        "",
        "  🤖 SparkBot —— ESP32-S3 机器人 PC 端 Agent",
        "  " + "─" * 52,
        f"  监听      http://{server.host}:{server.port}",
        f"  设备接入  ws://<本机IP>:{server.port}{server.ws_path}",
        f"  控制台    http://127.0.0.1:{server.port}/",
        f"  LLM       {llm.provider} / {llm.model}",
        f"  语音      ASR={speech.asr_provider}  TTS={speech.tts_provider}",
    ]
    if llm.provider == "mock":
        lines.append("")
        lines.append("  提示：当前使用离线假模型，不需要 API key，可完整跑通工具调用链路。")
        lines.append("        接真实模型：设置 SPARKBOT_LLM_PROVIDER=deepseek")
        lines.append("                    SPARKBOT_LLM_API_KEY=sk-xxx")
    lines.append("")
    lines.append("  启动模拟设备（另一个终端）：")
    lines.append(f"      python -m sparkbot.mock_device --url ws://127.0.0.1:{server.port}{server.ws_path}")
    lines.append("")
    print("\n".join(lines))


if __name__ == "__main__":
    raise SystemExit(main())
