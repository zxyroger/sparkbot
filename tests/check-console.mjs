/**
 * 用 Chrome DevTools Protocol 检查控制台页面在**真实浏览器**里的状态。
 *
 * 为什么需要它：服务端返回的 HTML 看着没问题、node 语法检查也过，
 * 但页面在浏览器里到底跑成什么样，只有真浏览器才知道。这个脚本会：
 *   1. 订阅 Runtime.consoleAPICalled 与 Runtime.exceptionThrown
 *   2. 读取状态条 (#s-status) 的真实文字
 *   3. 主动调用 refreshStatus() 并汇报结果
 *
 * 用法:
 *   node check-console.mjs                       // 自动找 9222 上的页面
 *   node check-console.mjs <wsDebuggerUrl>       // 指定调试 URL
 *
 * 不通过 PowerShell 传 URL 的原因：PowerShell 会把 /json/list 的对象数组
 * 拆解、拼接，导致传进来的"URL"其实是多个 URL 粘在一起（实测踩过）。
 * 这里自己用 fetch 拿目标列表，稳妥。
 */

async function resolveWsUrl() {
  const arg = process.argv[2];
  // 只接受单个合法 URL：必须是一个 ws:// 且不含空格
  if (arg && arg.startsWith("ws://") && !arg.includes(" ")) {
    return arg;
  }
  const res = await fetch("http://127.0.0.1:9222/json/list");
  const targets = await res.json();
  const pages = targets.filter((t) => t.type === "page" && t.url.includes("8765"));
  const page = pages[0] || targets.find((t) => t.type === "page");
  if (!page) throw new Error("调试端口上没有找到页面目标");
  console.log(`目标页面: ${page.url}`);
  return page.webSocketDebuggerUrl;
}

const wsUrl = await resolveWsUrl();

const ws = new WebSocket(wsUrl);
let nextId = 1;
const pending = new Map();
const logs = [];
const errors = [];

function send(method, params = {}) {
  const id = nextId++;
  ws.send(JSON.stringify({ id, method, params }));
  return new Promise((resolve) => pending.set(id, resolve));
}

ws.addEventListener("message", (ev) => {
  const msg = JSON.parse(ev.data);

  if (msg.id && pending.has(msg.id)) {
    pending.get(msg.id)(msg);
    pending.delete(msg.id);
    return;
  }

  if (msg.method === "Runtime.consoleAPICalled") {
    const text = (msg.params.args || [])
      .map((a) => (a.value !== undefined ? a.value : a.description || a.type))
      .join(" ");
    logs.push(`[${msg.params.type}] ${text}`);
  }

  if (msg.method === "Runtime.exceptionThrown") {
    const d = msg.params.exceptionDetails || {};
    errors.push(
      d.exception?.description || d.text || JSON.stringify(d).slice(0, 300)
    );
  }
});

ws.addEventListener("open", async () => {
  await send("Runtime.enable");
  await send("Page.enable");

  // 先强制重载：页面可能还挂着改动之前的旧脚本，那样检查的是旧内容，
  // 会出现"改了没效果"或"修好了还报错"的假象（两边都踩过）。
  const target = process.argv[2] || "http://127.0.0.1:8765/";
  await send("Page.navigate", { url: target });
  console.log(`已重新加载 ${target}`);

  // 给页面一点时间把脚本和第一次 setInterval 刷新都跑完
  await new Promise((r) => setTimeout(r, 4000));

  const probe = await send("Runtime.evaluate", {
    expression: `(() => {
      const pill = document.getElementById("s-status");
      const dev = document.getElementById("s-device");
      return JSON.stringify({
        uiVersionPresent: typeof statusTick !== "undefined",
        statusText: pill ? pill.textContent : "(元素不存在)",
        statusClass: pill ? pill.className : "",
        deviceText: dev ? dev.textContent : "(元素不存在)",
        hasTabSerial: !!document.getElementById("tab-serial"),
        hasPaneSerial: !!document.getElementById("pane-serial"),
        spantabs: [...document.querySelectorAll("nav button")].map(b => b.textContent),
      });
    })()`,
    returnByValue: true,
  });

  // 再主动调一次，看它是否抛错
  const manual = await send("Runtime.evaluate", {
    expression: `(async () => {
      try { await refreshStatus(); return "ok:" + document.getElementById("s-status").textContent; }
      catch (e) { return "THREW: " + (e && e.message ? e.message : String(e)); }
    })()`,
    awaitPromise: true,
    returnByValue: true,
  });

  console.log("=== 页面状态 ===");
  const val = probe.result?.result?.value;
  if (val) {
    const o = JSON.parse(val);
    for (const [k, v] of Object.entries(o)) {
      console.log(`  ${k}: ${JSON.stringify(v)}`);
    }
  } else {
    console.log("  取值失败:", JSON.stringify(probe).slice(0, 400));
  }

  console.log();
  console.log("=== 手动调用 refreshStatus() ===");
  // 注意取值层级：Runtime.evaluate 的返回是
  //   { id, result: { result: { type, value } } }
  // 写成 manual.result.value 会拿到 undefined。
  console.log("  " + (manual.result?.result?.value ?? JSON.stringify(manual).slice(0, 300)));

  console.log();
  console.log(`=== 控制台输出（${logs.length} 条）===`);
  for (const l of logs.slice(-15)) console.log("  " + l);

  console.log();
  console.log(`=== 未捕获异常（${errors.length} 条）===`);
  for (const e of errors.slice(-8)) console.log("  " + e.split("\n")[0]);

  ws.close();
  process.exit(0);
});

ws.addEventListener("error", (e) => {
  console.error("WebSocket 错误:", e.message || e);
  process.exit(1);
});
