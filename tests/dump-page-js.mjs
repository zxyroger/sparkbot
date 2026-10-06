/**
 * 从真实页面取脚本，保存到文件，并用浏览器本身报告准确错误位置。
 *
 * 用法: node dump-page-js.mjs [输出路径]
 */

import { writeFileSync } from "node:fs";

const out = process.argv[2] || "D:/dsh/.tmp/page.js";

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
  await send("Page.enable");

  // 必须显式重新导航：浏览器里可能还挂着改动之前的旧页面，
  // 直接读 document.scripts 会拿到旧脚本，导致"修了还是报错"的假象。
  const url = process.argv[3] || "http://127.0.0.1:8765/";
  console.log(`重新加载 ${url} …`);
  await send("Page.navigate", { url });
  await new Promise((r) => setTimeout(r, 4000)); // 等脚本执行完

  const r = await send("Runtime.evaluate", {
    expression: `[...document.scripts].map(s => s.textContent).join("\\n")`,
    returnByValue: true,
  });
  const src = r.result.result.value;
  writeFileSync(out, src, "utf8");
  console.log(`已保存 ${src.length} 字符到 ${out}`);

  // 让**浏览器自己**报告语法错误位置：用 Runtime.compileScript 编译这段源码
  const c = await send("Runtime.compileScript", {
    expression: src,
    sourceURL: "page-inline.js",
    persistScript: false,
  });
  console.log();
  if (c.error) {
    const d = c.error;
    console.log("浏览器编译结果: 失败");
    console.log("  message:", d.message);
    console.log("  line   :", d.lineNumber, "(0-based)");
    console.log("  column :", d.columnNumber);
  } else {
    console.log("浏览器编译结果: 成功");
  }

  // 找可疑字符（非 ASCII 控制字符等）
  console.log();
  console.log("可疑字符扫描（前 10 处）:");
  let found = 0;
  const lines = src.split("\n");
  for (let i = 0; i < lines.length && found < 10; i++) {
    for (let j = 0; j < lines[i].length; j++) {
      const code = lines[i].charCodeAt(j);
      // 允许 tab(9)，排除正常可打印与常见全角/中文
      if (code < 32 && code !== 9) {
        console.log(
          `  行 ${i + 1} 列 ${j + 1}: 控制字符 U+${code.toString(16).padStart(4, "0")}`
        );
        found++;
        break;
      }
    }
  }
  if (!found) console.log("  未发现控制字符");

  ws.close();
  process.exit(0);
});

ws.addEventListener("error", (e) => {
  console.error("WS 错误", e.message || e);
  process.exit(1);
});
