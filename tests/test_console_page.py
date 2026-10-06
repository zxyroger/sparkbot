"""控制台页面的 JS 语法测试 —— 防止"整个脚本解析失败"这类 bug 重现。

为什么必须有这个测试：

    ``_CONSOLE_HTML`` 是**普通 Python 三引号字符串**，里面的 ``\\n`` /
    ``\\t`` 会被 Python 在生成 HTML 时就替换掉。所以 JS 里想表达
    "换行转义"必须写成两个反斜杠，写成一个是致命的：

        }).join("\\n");     # 正确：JS 收到 反斜杠 + n
        }).join("\n");      # 错误：JS 收到真实换行 → 字符串跨行

    后者会让**整段脚本**解析失败（SyntaxError），后果不是"某个功能坏
    了"，而是所有函数都不存在：状态条永远停在「连接中…」、点任何标签
    都没反应。而服务端完全看不出异常（HTTP 200、长度正常），
    极难定位。这个 bug 真实发生过一次。

做法：把 HTML 里的内联脚本抽出来，交给 ``node --check`` 做**权威**语法
检查。node 不可用时跳过（不算失败），但会明确提示。
"""

from __future__ import annotations

import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / ".vendor"))

from sparkbot.app import _CONSOLE_HTML  # noqa: E402

_passed = 0
_failed: list[str] = []


def check(cond: bool, label: str) -> None:
    """记录断言。"""
    global _passed
    if cond:
        _passed += 1
    else:
        _failed.append(label)
        print(f"  [FAIL] {label}")


def extract_js(html: str) -> str:
    """抽出全部内联脚本并拼在一起。"""
    blocks = re.findall(r"<script[^>]*>(.*?)</script>", html, re.S)
    return "\n".join(blocks)


def test_js_parses() -> None:
    """内联脚本必须能通过 node 的语法检查。"""
    print("JS 语法（node --check）")
    js = extract_js(_CONSOLE_HTML)
    check(len(js) > 5000, f"抽出的脚本应该不小（得到 {len(js)} 字符）")

    node = shutil.which("node")
    if node is None:
        print("  [SKIP] 没找到 node，跳过语法检查")
        return

    with tempfile.NamedTemporaryFile("w", suffix=".js", delete=False, encoding="utf-8") as f:
        f.write(js)
        path = f.name

    try:
        proc = subprocess.run(
            [node, "--check", path], capture_output=True, text=True, timeout=60
        )
        ok = proc.returncode == 0
        detail = (proc.stderr or "").strip().splitlines()
        check(
            ok,
            "内联脚本语法必须正确"
            + ("" if ok else f" -> {detail[:4] if detail else '未知错误'}"),
        )
        if not ok:
            # 把出错行附近打出来，方便直接定位
            m = re.search(r":(\d+)\s*$", detail[0] if detail else "")
            if m:
                lineno = int(m.group(1))
                js_lines = js.split("\n")
                print(f"  出错在第 {lineno} 行附近:")
                for i in range(max(0, lineno - 3), min(len(js_lines), lineno + 2)):
                    mark = ">>" if i + 1 == lineno else "  "
                    print(f"    {mark} {i + 1}: {js_lines[i][:120]}")
    finally:
        Path(path).unlink(missing_ok=True)


def test_no_raw_newline_in_js_strings() -> None:
    """JS 里不应出现"双引号字符串跨行"的情况。

    这是上面那个 bug 的**直接特征**，即使 node 不可用也能查。
    用状态机扫一遍，遇到字符串里出现换行就报错。
    """
    print("字符串跨行扫描")
    js = extract_js(_CONSOLE_HTML)

    in_str: str | None = None
    line = 1
    problems: list[tuple[int, str]] = []
    i = 0
    n = len(js)

    while i < n:
        ch = js[i]
        if ch == "\n":
            if in_str in ('"', "'"):
                problems.append((line, in_str))
                in_str = None  # 复原，继续扫后面的
            line += 1
            i += 1
            continue
        if in_str:
            if ch == "\\":
                i += 2  # 跳过转义
                continue
            if ch == in_str:
                in_str = None
            i += 1
            continue
        # 不在字符串里
        if ch == "/" and i + 1 < n and js[i + 1] == "/":
            while i < n and js[i] != "\n":
                i += 1
            continue
        if ch in ('"', "'"):
            in_str = ch
            i += 1
            continue
        if ch == "`":
            # 模板串允许跨行，直接跳到结束
            i += 1
            while i < n and js[i] != "`":
                if js[i] == "\\":
                    i += 1
                i += 1
            i += 1
            continue
        i += 1

    check(
        not problems,
        f"不应有跨行的字符串字面量（发现 {len(problems)} 处: {problems[:4]}）",
    )


def test_ui_version_placeholder() -> None:
    """版本占位符必须存在，否则服务端注入会静默失效。"""
    print("UI 版本占位符")
    check("__UI_VERSION__" in _CONSOLE_HTML, "HTML 里应有 __UI_VERSION__ 占位符")


def test_required_elements() -> None:
    """关键元素必须存在（前端靠 id 取它们）。"""
    print("关键元素")
    for eid in ("s-status", "s-device", "tab-chat", "tab-serial", "tab-config",
                "pane-chat", "pane-serial", "pane-config", "ser-log", "ser-port"):
        check(f'id="{eid}"' in _CONSOLE_HTML, f"应有 id={eid}")


def test_switch_tab_covers_all_panes() -> None:
    """switchTab 必须覆盖全部三个面板，否则某个标签点了没反应。"""
    print("switchTab 覆盖")
    for name in ("pane-chat", "pane-serial", "pane-config"):
        check(name in _CONSOLE_HTML, f"switchTab 逻辑里应出现 {name}")


def main() -> int:
    """跑全部用例。"""
    print("=" * 62)
    print("控制台页面测试")
    print("=" * 62)
    print()

    test_js_parses()
    test_no_raw_newline_in_js_strings()
    test_ui_version_placeholder()
    test_required_elements()
    test_switch_tab_covers_all_panes()

    print()
    print("=" * 62)
    total = _passed + len(_failed)
    print(f"断言汇总: {_passed}/{total} 通过")
    if _failed:
        print()
        print("失败项:")
        for f in _failed:
            print(f"  - {f}")
        return 1
    print("全部通过")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
