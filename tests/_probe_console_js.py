"""在 node 里加载控制台页面，检查前端是否真的能工作。

为什么需要它：``node --check`` 只验证语法，发现不了"脚本运行时抛异常"
或"引用了不存在的 DOM 元素"这类问题 —— 而后者恰恰是"点按钮没反应"
最常见的原因（脚本一开头就抛错，后面所有 onclick 都不生效）。

做法：把页面 HTML 抽出来，在一个极简的 DOM 桩里跑一遍脚本，然后调用
``switchTab('serial')`` 看它是否会抛异常。
"""

from __future__ import annotations

import io
import json
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
APP = ROOT / "sparkbot" / "app.py"

#: 极简 DOM 桩：足够让页面脚本跑起来，不需要真的浏览器。
#: 每个 getElementById 都返回一个"万能元素"，并记录访问过的 id，
#: 这样就能列出脚本引用了哪些 id。
STUB = r"""
const ACCESSED = new Set();
function makeEl(id) {
  const el = {
    id: id,
    _cls: new Set(),
    style: {},
    value: "",
    textContent: "",
    innerHTML: "",
    scrollTop: 0, scrollHeight: 0,
    checked: false,
    appendChild(c) { return c; },
    classList: {
      add(c) { el._cls.add(c); },
      remove(c) { el._cls.delete(c); },
      toggle(c, on) { if (on === undefined) { el._cls.has(c) ? el._cls.delete(c) : el._cls.add(c); } 
                      else { on ? el._cls.add(c) : el._cls.delete(c); } },
      contains(c) { return el._cls.has(c); },
    },
  };
  return el;
}
const _cache = {};
global.document = {
  getElementById(id) { ACCESSED.add(id); if (!_cache[id]) _cache[id] = makeEl(id); return _cache[id]; },
  createElement(tag) { return makeEl("new-" + tag); },
};
global.location = { protocol: "http:", host: "127.0.0.1:8765", href: "http://127.0.0.1:8765/" };
global.WebSocket = class { constructor(u) { this.url = u; this.readyState = 0; } send() {} close() {} };
global.fetch = async () => ({ ok: true, status: 200,
  json: async () => ({ok:true, ports:[{device:"COM15",description:"USB"}], lines:[], status:{}}),
  text: async () => "" });
global.setInterval = () => 0;
global.setTimeout = (f) => 0;
global.clearInterval = () => {};
global.alert = () => {};
"""

PROBE = r"""
// 脚本加载后，逐个调用标签切换，看是否抛异常。
const results = {};
for (const name of ["chat", "serial", "config", "chat"]) {
  try {
    switchTab(name);
    results[name] = "ok";
  } catch (e) {
    results[name] = "THREW: " + e.message;
  }
}
// 再单独探一下串口相关的函数。
// 注意：不能写 global[fn] —— 页面脚本里的 `function foo()` 在 node 的模块
// 作用域里，不会挂到 global 上，那样查会误报 NOT DEFINED。
// 直接写函数名，未定义时 ReferenceError 会被 catch 捕获。
for (const fn of ["serialLoadPorts", "serialRender", "serialConnectStream"]) {
  try {
    const f = eval(fn);
    if (typeof f === "function") { results[fn] = "ok"; }
    else { results[fn] = "NOT A FUNCTION"; }
  } catch (e) {
    results[fn] = "NOT DEFINED";
  }
}
console.log("__RESULT__" + JSON.stringify({results, accessed: [...ACCESSED].sort()}));
"""


def main() -> int:
    """抽取脚本、跑桩、报告结果。"""
    src = io.open(APP, encoding="utf-8").read()
    m = re.search(r'_CONSOLE_HTML = """(.*?)"""', src, re.S)
    if not m:
        print("没找到 _CONSOLE_HTML")
        return 1
    html = m.group(1)

    scripts = re.findall(r"<script>(.*?)</script>", html, re.S)
    js = "\n".join(scripts)

    # 页面里真正存在的 id
    declared_ids = set(re.findall(r'id="([^"]+)"', html))

    combined = STUB + "\n" + js + "\n" + PROBE
    out_file = ROOT.parent / ".tmp" / "console-probe.js"
    out_file.parent.mkdir(parents=True, exist_ok=True)
    io.open(out_file, "w", encoding="utf-8").write(combined)

    proc = subprocess.run(
        ["node", str(out_file)], capture_output=True, text=True, timeout=60
    )
    if proc.returncode != 0:
        print("node 执行失败：")
        print(proc.stderr[:2000])
        return 1

    payload = None
    for line in proc.stdout.splitlines():
        if line.startswith("__RESULT__"):
            payload = json.loads(line[len("__RESULT__"):])
    if payload is None:
        print("没拿到结果，stdout:")
        print(proc.stdout[:2000])
        return 1

    print("=" * 60)
    print("控制台前端探针")
    print("=" * 60)
    print()
    print("标签切换与串口函数调用:")
    bad = 0
    for name, res in payload["results"].items():
        flag = "OK" if res == "ok" else "❌"
        if res != "ok":
            bad += 1
        print(f"  {flag}  {name:24} {res}")

    print()
    accessed = set(payload["accessed"])
    missing = sorted(accessed - declared_ids)
    print(f"脚本引用了 {len(accessed)} 个元素 id；页面声明了 {len(declared_ids)} 个")
    if missing:
        print()
        print("⚠️  引用了但页面里不存在的 id（会导致运行时报错）:")
        for mid in missing:
            print(f"    {mid}")
        bad += len(missing)
    else:
        print("  所有引用的 id 都存在 ✅")

    print()
    if bad:
        print(f"发现 {bad} 个问题")
        return 1
    print("未发现问题")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
