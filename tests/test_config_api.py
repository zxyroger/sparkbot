"""配置 API 的端到端测试：真实 HTTP 服务 + 真实 .env 落盘。

覆盖：
1. GET /api/config 的结构，以及**密钥绝不明文回传**；
2. POST 修改后立即生效（provider 被真正重建）；
3. 密钥「留空即不变」的约定；
4. 写入 .env 且**保留原有注释与未改动项**；
5. 非法字段名与非法取值被拒绝；
6. POST /api/config/test 的连通性探测。

不联网：全部用 mock provider，测试结束会清理临时 .env。

运行::

    python tests/test_config_api.py
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

from sparkbot.app import create_app  # noqa: E402
from sparkbot.config import Settings  # noqa: E402
from sparkbot.testing import serve_app  # noqa: E402

logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(message)s")

_PASSED = 0
_FAILED: list[str] = []

#: 测试用的临时 .env —— 放在项目根，与 ``_env_path()`` 的解析一致。
ENV_PATH = Path(".env")


def check(condition: bool, label: str, detail: str = "") -> None:
    """记录断言结果。"""
    global _PASSED
    if condition:
        _PASSED += 1
        print(f"  PASS  {label}" + (f" — {detail}" if detail else ""))
    else:
        _FAILED.append(label)
        print(f"  FAIL  {label}" + (f" — {detail}" if detail else ""))


def build_settings() -> Settings:
    """离线测试配置。"""
    settings = Settings()
    settings.server.log_level = "warning"
    settings.llm.provider = "mock"
    settings.vision.provider = "mock"
    settings.speech.asr_provider = "mock"
    settings.speech.tts_provider = "mock"
    return settings


def rt_provider(app: object) -> str:
    """取出运行时当前**实际使用**的 provider 名称。"""
    return app.state.runtime.provider.name  # type: ignore[attr-defined]


def check_port_conflict_checks() -> None:
    """验证端口占用检测用的是与 uvicorn 相同的绑定地址。

    这是一条回归测试。最初的实现拿 ``127.0.0.1`` 去探测，而 uvicorn 绑的是
    ``0.0.0.0``——Windows 下这两者不算冲突，于是检查恒为"端口空闲"，
    冲突仍要等到 uvicorn 报 ``[Errno 10048]`` 才暴露。
    """
    import socket

    from sparkbot.__main__ import check_port_available

    # 占住一个端口：模拟"已经有一个实例在跑"
    holder = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    holder.bind(("0.0.0.0", 0))
    holder.listen(1)
    busy_port = holder.getsockname()[1]

    try:
        # 用 0.0.0.0 探测必须判定为占用
        raised = False
        try:
            check_port_available("0.0.0.0", busy_port)
        except SystemExit as exc:
            raised = True
            check(exc.code == 3, "端口被占用时以退出码 3 结束检查", str(exc.code))
        check(raised, "复用 uvicorn 的绑定地址能检出占用", f"port={busy_port}")

        # 空闲端口不应误报
        free_probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        free_probe.bind(("0.0.0.0", 0))
        free_port = free_probe.getsockname()[1]
        free_probe.close()

        ok = True
        try:
            check_port_available("0.0.0.0", free_port)
        except SystemExit:
            ok = False
        check(ok, "空闲端口不会被误判为占用", f"port={free_port}")
    finally:
        holder.close()


async def main() -> int:
    """跑完全部检查。"""
    import httpx

    # 备份可能存在的真实 .env，测试后恢复，绝不动用户的配置。
    backup = ENV_PATH.read_text(encoding="utf-8") if ENV_PATH.exists() else None
    if ENV_PATH.exists():
        ENV_PATH.unlink()

    try:
        settings = build_settings()
        app = create_app(settings)
        served = await serve_app(app)

        async with httpx.AsyncClient(base_url=served.base_url, timeout=60.0) as client:
            # ---------------------------------------------------------- #
            print("\n阶段 1 · 读取配置")
            res = await client.get("/api/config")
            check(res.status_code == 200, "GET /api/config 返回 200", str(res.status_code))
            data = res.json()

            check(data.get("ok") is True, "响应 ok=true")
            check(bool(data.get("fields")), "返回可编辑字段列表", f"{len(data.get('fields', []))} 项")
            check(bool(data.get("choices", {}).get("llm.provider")), "返回 provider 候选项",
                  str(data["choices"]["llm.provider"]))

            cfg = data["config"]
            check(cfg.get("llm.provider") == "mock", "读到当前 provider", str(cfg.get("llm.provider")))
            check(cfg.get("llm.api_key") == "", "密钥字段不明文回传", repr(cfg.get("llm.api_key")))
            check(cfg.get("llm.api_key_set") is False, "未配置时密钥状态为 false")
            check("llm.api_key" in data.get("secrets", []), "密钥字段被标记为 secret")

            # ---------------------------------------------------------- #
            print("\n阶段 2 · 修改配置并立即生效")
            res = await client.post("/api/config", json={
                "values": {
                    "llm.provider": "deepseek",
                    "llm.model": "deepseek-chat",
                    "llm.api_key": "sk-test-12345",
                    "llm.temperature": "0.9",
                    "behavior.max_duration_ms": "2500",
                    "behavior.talking_animation": False,
                },
            })
            check(res.status_code == 200, "POST /api/config 返回 200", str(res.status_code))
            body = res.json()
            check(body.get("ok") is True, "保存成功")
            check(body.get("applied", {}).get("llm.api_key") == "***", "响应里密钥被打码",
                  str(body.get("applied", {}).get("llm.api_key")))
            check(body.get("persisted", 0) >= 6, "写入 .env 的条目数", str(body.get("persisted")))

            rt = body.get("runtime", {})
            check(rt.get("llm_provider") == "deepseek", "provider 已热切换", str(rt.get("llm_provider")))
            check(rt.get("llm_model") == "deepseek-chat", "model 已生效", str(rt.get("llm_model")))

            # 类型转换是否正确落到 settings 上
            check(settings.llm.temperature == 0.9, "字符串温度被转成 float",
                  f"{settings.llm.temperature!r}")
            check(settings.behavior.max_duration_ms == 2500, "字符串时长被转成 int",
                  f"{settings.behavior.max_duration_ms!r}")
            check(settings.behavior.talking_animation is False, "字符串布尔被转成 bool",
                  f"{settings.behavior.talking_animation!r}")
            check(settings.llm.api_key is not None
                  and settings.llm.api_key.get_secret_value() == "sk-test-12345",
                  "密钥已写入且仍是 SecretStr")

            # ---------------------------------------------------------- #
            print("\n阶段 3 · 密钥「留空即不变」")
            res = await client.post("/api/config", json={
                "values": {"llm.provider": "openai", "llm.api_key": ""},
            })
            check(res.status_code == 200, "只改 provider 成功")
            check(settings.llm.api_key is not None
                  and settings.llm.api_key.get_secret_value() == "sk-test-12345",
                  "留空时原密钥保持不变",
                  settings.llm.api_key.get_secret_value() if settings.llm.api_key else "None")

            res = await client.get("/api/config")
            check(res.json()["config"]["llm.api_key_set"] is True, "已配置后状态为 true")

            # 传 null 才是真正清空
            res = await client.post("/api/config", json={"values": {"llm.api_key": None}})
            check(res.status_code == 200, "传 null 可以清空密钥")
            check(settings.llm.api_key is None, "密钥确实被清空",
                  repr(settings.llm.api_key))
            # 恢复，供后面步骤使用
            await client.post("/api/config", json={"values": {"llm.api_key": "sk-test-12345"}})

            # ---------------------------------------------------------- #
            print("\n阶段 4 · .env 落盘")
            check(ENV_PATH.exists(), ".env 已创建", str(ENV_PATH))
            text = ENV_PATH.read_text(encoding="utf-8")
            check("SPARKBOT_LLM_PROVIDER=openai" in text, ".env 含新 provider")
            check("SPARKBOT_LLM_MODEL=deepseek-chat" in text, ".env 含 model")
            check("SPARKBOT_LLM_API_KEY=sk-test-12345" in text, ".env 含明文 key（落盘必须明文）")
            check("SPARKBOT_BEHAVIOR_MAX_DURATION_MS=2500" in text, ".env 含安全参数")

            # 手工加一行注释和一个自定义项，再改配置，验证不会被覆写掉
            ENV_PATH.write_text(
                "# 我的手工注释\nSPARKBOT_CUSTOM_THING=keep-me\n" + text,
                encoding="utf-8",
            )
            await client.post("/api/config", json={"values": {"llm.temperature": "0.3"}})
            text2 = ENV_PATH.read_text(encoding="utf-8")
            check("# 我的手工注释" in text2, ".env 原有注释被保留")
            check("SPARKBOT_CUSTOM_THING=keep-me" in text2, ".env 未管理的项被保留")
            check("SPARKBOT_LLM_TEMPERATURE=0.3" in text2, "改动项被就地更新")
            # 同一个 key 不应出现两次
            check(text2.count("SPARKBOT_LLM_PROVIDER=") == 1, "provider 条目没有重复")

            # ---------------------------------------------------------- #
            print("\n阶段 5 · 错误处理")
            res = await client.post("/api/config",
                                    json={"values": {"server.port": 9999}})
            check(res.status_code == 422, "启动期字段被拒绝为 422", str(res.status_code))
            check("server.port" in res.json().get("detail", ""), "错误信息点名了该字段",
                  res.json().get("detail", "")[:60])

            res = await client.post("/api/config",
                                    json={"values": {"behavior.max_duration_ms": "abc"}})
            check(res.status_code == 422, "非法取值被拒绝为 422", str(res.status_code))

            res = await client.post("/api/config", json={"values": "not-an-object"})
            check(res.status_code == 422, "values 非对象被拒绝", str(res.status_code))

            # ---------------------------------------------------------- #
            print("\n阶段 6 · 连通性测试接口")
            await client.post("/api/config", json={"values": {"llm.provider": "mock"}})
            res = await client.post("/api/config/test")
            check(res.status_code == 200, "POST /api/config/test 返回 200")
            probe = res.json()
            check(probe.get("ok") is True, "mock provider 连通", str(probe.get("error") or ""))
            check(probe.get("provider") == "mock", "报告了 provider", str(probe.get("provider")))
            check(isinstance(probe.get("latency_ms"), int), "报告了延迟",
                  f"{probe.get('latency_ms')}ms")

            # ---------------------------------------------------------- #
            print("\n阶段 7 · 控制台页面含设置面板")
            html = (await client.get("/")).text
            check("id=\"pane-config\"" in html, "控制台含设置面板容器")
            check("saveConfig" in html and "testLlm" in html, "控制台含保存与测试逻辑")
            check("api/config" in html, "控制台会调用配置接口")

            # ---------------------------------------------------------- #
            print("\n阶段 8 · persist / reload_provider 开关")
            before = ENV_PATH.read_text(encoding="utf-8")
            res = await client.post("/api/config", json={
                "values": {"llm.temperature": "0.55"},
                "persist": False,
            })
            check(res.status_code == 200, "persist=false 仍然成功")
            check(res.json().get("persisted") == 0, "persist=false 时不写盘",
                  str(res.json().get("persisted")))
            check(ENV_PATH.read_text(encoding="utf-8") == before,
                  "persist=false 时 .env 内容未变")

            old_provider = rt_provider(app)
            res = await client.post("/api/config", json={
                "values": {"llm.model": "some-other-model"},
                "reload_provider": False,
            })
            check(res.status_code == 200, "reload_provider=false 仍然成功")
            check(rt_provider(app) == old_provider,
                  "reload_provider=false 时不重建 provider（仍用旧实例）",
                  f"{old_provider} -> {rt_provider(app)}")
            check(settings.llm.model == "some-other-model",
                  "但配置值本身已写入 settings", settings.llm.model)

        await served.close()

        # -------------------------------------------------------------- #
        print("\n阶段 9 · 端口占用检测")
        check_port_conflict_checks()

    finally:
        # 还原用户的 .env，绝不留测试残留
        if backup is not None:
            ENV_PATH.write_text(backup, encoding="utf-8")
        elif ENV_PATH.exists():
            ENV_PATH.unlink()

    total = _PASSED + len(_FAILED)
    print()
    print("=" * 62)
    print(f"断言汇总: {_PASSED}/{total} 通过")
    if _FAILED:
        print("失败项:")
        for item in _FAILED:
            print(f"  · {item}")
        print("=" * 62)
        return 1
    print("全部通过")
    print("=" * 62)
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
