/**
 * 逐行定位 JS 里的语法错误，报告**真实字节**。
 *
 * 用法: node find-bad-line.mjs <文件>
 *
 * 做法：对文件的前 N 行做语法检查，找到第一个无法解析的前缀，
 * 打印那一行及其前后行的原始码点，这样能看出是哪个字符坏了
 * （而不是靠终端显示的乱码猜）。
 */

import { readFileSync } from "node:fs";

const file = process.argv[2];
if (!file) {
  console.error("用法: node find-bad-line.mjs <文件>");
  process.exit(2);
}

const src = readFileSync(file, "utf8");
const lines = src.split("\n");
console.log(`文件 ${lines.length} 行`);

// 注意：不能简单地"前缀能否解析"——JS 里不完整的块本来就不能解析。
// 改用括号/字符串状态机扫一遍，直接找出"字符串未闭合就换行"的位置，
// 这正是本次 bug 的形态。
let inStr = null; // null | '"' | "'" | '`'
let line = 1;
let col = 0;
const problems = [];

for (let i = 0; i < src.length; i++) {
  const ch = src[i];
  col++;
  if (ch === "\n") {
    if (inStr === '"' || inStr === "'") {
      problems.push({
        line,
        col,
        msg: `${inStr} 字符串跨行未闭合`,
        text: lines[line - 1],
      });
    }
    line++;
    col = 0;
    continue;
  }
  if (inStr) {
    if (ch === "\\") {
      i++; // 跳过转义的下一个字符
      col++;
      continue;
    }
    if (ch === inStr) inStr = null;
    continue;
  }
  // 不在字符串里：跳过行注释
  if (ch === "/" && src[i + 1] === "/") {
    while (i < src.length && src[i] !== "\n") i++;
    i--;
    continue;
  }
  if (ch === '"' || ch === "'" || ch === "`") {
    inStr = ch;
    continue;
  }
}

console.log(`发现 ${problems.length} 处字符串跨行问题`);
for (const p of problems.slice(0, 10)) {
  console.log();
  console.log(`  行 ${p.line} 列 ${p.col}: ${p.msg}`);
  // 打印该行的码点，暴露隐藏字符
  const t = p.text || "";
  console.log(`    文本: ${JSON.stringify(t.slice(0, 120))}`);
  console.log(
    `    码点: ${[...t.slice(0, 60)]
      .map((c) => "U+" + c.codePointAt(0).toString(16).toUpperCase().padStart(4, "0"))
      .join(" ")}`
  );
}
