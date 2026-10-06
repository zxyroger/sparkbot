/**
 * 从真实浏览器里取出页面脚本，逐段做语法检查，定位出错位置。
 *
 * 为什么需要：`_CONSOLE_HTML` 里可能含嵌入内容（例如我自己写的
 * 诊断代码里的模板串），从 Python 源文件里用正则抽 script 会漏掉
 * 或截断，导致语法检查"通过"但浏览器实际报 SyntaxError。
 * 直接从运行中的页面取脚本才是权威的。
 *
 * 用法: node find-js-syntax-error.mjs
 */

const res = await fetch("http://127.0.0.1:9222/json/list");
const targets = await res.json();
const page = targets.find((t) => t.type === "page" && t.url.includes("8765"));
if (!page) {
  console.error("没找到 8765 的页面");
  process.exit(1);
}

const ws = new WebSocket(page.webSocketDebuggerUrl);
let nextId = 1;
const pending = new Map();

function send(method, params = {}) {
  const id = nextId++;
  ws.send(JSON.stringify({ id, method, params }));
  return new Promise((r) => pending.set(id, r));
}

ws.addEventListener("message", (ev) => {
  const m = JSON.parse(ev.data);
  if (m.id && pending.has(m.id)) {
    pending.get(m.id)(m);
    pending.delete(m.id);
  }
});

ws.addEventListener("open", async () => {
  await send("Runtime.enable");

  // 拿到页面里全部 script 的源码
  const r = await send("Runtime.evaluate", {
    expression: `JSON.stringify([...document.scripts].map(s => s.textContent))`,
    returnByValue: true,
  });
  const scripts = JSON.parse(r.result.result.value);
  console.log(`页面里有 ${scripts.length} 个 script 块`);

  let bad = 0;
  scripts.forEach((src, i) => {
    console.log(`\n--- script[${i}] 长度 ${src.length} ---`);
    // 逐段做语法检查：从可疑位置切出小片段，找出第一个出错的地方
    try {
      new Function(src);
      console.log("  语法 OK");
    } catch (e) {
      bad++;
      console.log(`  ❌ 语法错误: ${e.message}`);

      // 找出行号：逐行累积，二分/线性找出让 new Function 失败的最短前缀
      const lines = src.split("\n");
      for (let n = 1; n <= lines.length; n++) {
        const chunk = lines.slice(0, n).join("\n");
        try {
          new Function(chunk);
        } catch (err) {
          // 前缀本身不完整也会报错，所以看错误信息是否"变化"
          if (n > 1) {
            const prev = lines.slice(0, n - 1).join("\n");
            let prevOk = true;
            try { new Function(prev); } catch { prevOk = false; }
            if (prevOk) {
              console.log(`  首个无法解析的行: ${n}`);
              console.log(`    上一行(${n - 1}): ${JSON.stringify(lines[n - 2]).slice(0, 160)}`);
              console.log(`    本行  (${n}): ${JSON.stringify(lines[n - 1]).slice(0, 160)}`);
              break;
            }
          }
        }
      }
    }
  });

  ws.close();
  process.exit(bad ? 1 : 0);
});

ws.addEventListener("error", (e) => {
  console.error("WS 错误", e.message || e);
  process.exit(1);
});
