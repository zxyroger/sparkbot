"""检查服务端返回的控制台页面里，JS 字符串字面量是否被某个字符截断。

背景：浏览器报 ``SyntaxError: Invalid or unexpected token``，而
``node --check`` 也指向 ``}).join("`` 处的字符串跨行。说明某个中文
字符串里混进了一个裸双引号。

这个脚本不看控制台输出的乱码，而是**直接检查字节**，把出问题的
字符串连同行号一起打出来。
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / ".vendor"))

import httpx  # noqa: E402


def main() -> int:
    """拉取页面，扫描可能截断字符串的位置。"""
    r = httpx.get("http://127.0.0.1:8765/", timeout=20)
    html = r.text
    print(f"页面长度: {len(html)} 字符")
    print()

    # 取出内联脚本
    m = re.search(r"<script>(.*?)</script>", html, re.S)
    js = m.group(1)
    lines = js.split("\n")
    print(f"脚本 {len(lines)} 行")
    print()

    # 逐行统计双引号数量：正常一行里字符串外的引号应当是偶数个，
    # 奇数个往往意味着字符串被截断。
    print("=== 双引号数为奇数的行（可疑）===")
    found = 0
    for i, line in enumerate(lines, 1):
        # 去掉转义引号后统计
        cleaned = line.replace('\\"', "")
        n = cleaned.count('"')
        if n % 2 == 1:
            found += 1
            print(f"  行 {i}: 引号 {n} 个")
            print(f"    {line[:170]}")
            if found >= 12:
                break
    if not found:
        print("  没有奇数引号的行")

    print()
    print("=== 含裸中文双引号/全角引号的行 ===")
    for i, line in enumerate(lines, 1):
        if "“" in line or "”" in line or "＂" in line:
            print(f"  行 {i}: {line[:170]}")

    print()
    print("=== 直接找 .join( 附近的字符串 ===")
    for i, line in enumerate(lines, 1):
        if ".join(" in line or "join(" in line:
            print(f"  行 {i}: {line[:170]}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
