// The memory star map (src/components/MemoryStarMap.tsx) rendered to one static HTML page with sample notes, light and
// dark side by side, to look at without the app: `node scripts/starmap-preview.mjs [output.html]`
// (default ../sandbox/starmap-dist/preview.html; `?only=many-dark` on the page shows one card). The component is loaded
// through Vite's SSR loader (no build output) and rendered with react-dom/server: no React runs in the page itself, so
// it shows the first frame only (no hover, no zoom); scripts/check-starmap.mjs drives a real bundle.
//
// It also exports the sample notes the unit test and the interaction check use: `synthNotes(n, options)` (the same
// notes for the same arguments) and `SAMPLES` (3 notes, ~25, ~300 and the odd cases).
import { mkdirSync, readFileSync, writeFileSync } from "node:fs";
import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";

const here = dirname(fileURLToPath(import.meta.url));
const frontend = resolve(here, "..");

/** A fixed "now" so that the same call always gives the same notes; pass `now: Date.now()` for relative times that read
 *  naturally on a page. */
export const NOW = Date.UTC(2026, 10, 7, 9, 0, 0);
const MIN = 60_000;
const HOUR = 60 * MIN;
const DAY = 24 * HOUR;

export const PROJECTS = [
  { path: "C:\\Users\\me\\code\\wittycode", name: "wittycode", weight: 6 },
  { path: "C:\\Users\\me\\code\\shop-web", name: "shop-web", weight: 3 },
  { path: "C:\\Users\\me\\code\\inventory-api", name: "inventory-api", weight: 2 },
  { path: "D:\\Work\\app", name: "app", weight: 1.4 },
  { path: "D:\\Old\\app", name: "app", weight: 0.8 },
  { path: "C:\\Users\\me\\src\\a-project-with-a-rather-long-folder-name", name: "a-project-with-a-rather-long-folder-name", weight: 0.7 },
];

const TITLES = {
  preference: ["回答用中文，代码标识符保持英文", "先给结论，再给理由", "提交信息用英文祈使句", "不要在回复里用表情符号", "解释改动时带上文件路径和行号", "命名用完整单词，不用缩写", "改代码前先读 AGENTS.md", "测试失败时贴出完整输出", "界面文字一律中文", "长命令拆成几步说明", "能用标准库就不引入新依赖", "图表优先用深色背景"],
  convention: ["改了 frontend 要先 npm run build:app", "单元测试：npm run unit", "样式类名加组件前缀", "新文件用 LF 换行", "公开仓库里不写真实路径和邮箱", "Python 只用 3.11 的 .venv", "提交前看一遍 git status", "截图放在 sandbox/shots", "设置在 %APPDATA%\\WittyCode", "每个任务结束更新 docs/handoff.md", "e2e 用 Rig 驱动真实窗口", "组件只用 tokens.css 的变量"],
  pitfall: ["PowerShell 5.1 不支持 && 和 ||", "Edit 工具改文件前要重新读一遍", "qasync 的定时器在 loop 关闭后不会被杀", "here-string 里的中文会变成问号", "offscreen 平台下窗口不激活", "runJavaScript 只带得回标量", "类名撞了会把别的组件弄乱", "QDragEnterEvent 用 QPointF 会崩溃", "file:// 下 ES module 脚本加载不了", "Droid 对未知模型 id 会静默替换"],
  fact: ["Codex 数据目录是 E:\\tools\\codex", "每个 droid.exe 约 300 MB", "Node 24 能直接运行 .ts", "Windows 11 窗口圆角半径 8px", "Haiku 5.5 的窗口是 872000", "提示缓存空闲约 5 分钟失效", "Codex 的 rollout 时间戳是 UTC", "ChatGPT Plan 的访问令牌 1 小时过期", "Devin 走 devin acp", "页面构建成单文件 index.html"],
};
const SENTENCES = [
  "这条是在一次真实的会话里记下的。",
  "下次遇到同样的情况直接照做，不要再查一遍。",
  "原因在 docs/handoff.md 的批次记录里写过。",
  "Windows 上的行为和 Linux 不一样，要单独测。",
  "如果不确定，先在 sandbox 里试一下。",
  "改动以后跑一遍单元测试和界面检查。",
  "用户明确要求过，不要自作主张地改掉。",
  "这个问题第一次出现是在接入 Codex 的时候。",
  "相关代码在 core/memory/store.py。",
  "旧的做法已经不再适用，别照着旧文档做。",
];
const CONVERSATIONS = ["修复缓存", "整理测试", "接入 Devin", "共享记忆", "文件链接", "换引擎交接"];

function mulberry(seed) {
  let a = seed >>> 0;
  return () => {
    a = (a + 0x6d2b79f5) >>> 0;
    let t = a;
    t = Math.imul(t ^ (t >>> 15), t | 1);
    t ^= t + Math.imul(t ^ (t >>> 7), t | 61);
    return ((t ^ (t >>> 14)) >>> 0) / 4294967296;
  };
}

function pick(rnd, items, weights) {
  const total = weights.reduce((a, b) => a + b, 0);
  let x = rnd() * total;
  for (let i = 0; i < items.length; i++) {
    x -= weights[i];
    if (x < 0) return items[i];
  }
  return items[items.length - 1];
}

/** `n` notes in the shape Python sends (MemoryEntry), spread over global and the projects, the four kinds, every source
 *  (an engine WittyCode does not know among them), lengths from a line to a page, ages from minutes to three months, a
 *  few forgotten. The same arguments always give the same notes. */
export function synthNotes(n, { seed = 11, now = NOW, projects = PROJECTS, global = 0.3, forgotten = 0.06 } = {}) {
  const rnd = mulberry(seed);
  const kinds = ["preference", "convention", "pitfall", "fact"];
  const used = { preference: 0, convention: 0, pitfall: 0, fact: 0 };
  const out = [];
  for (let i = 0; i < n; i++) {
    const kind = pick(rnd, kinds, [1, 1.3, 1, 1.2]);
    const isGlobal = rnd() < global;
    const project = isGlobal ? null : pick(rnd, projects, projects.map((p) => p.weight));
    const pool = TITLES[kind];
    const turn = used[kind]++;
    const title = pool[turn % pool.length] + (turn >= pool.length ? ` · ${Math.floor(turn / pool.length) + 1}` : "");
    const length = Math.round(Math.exp(Math.log(12) + rnd() * (Math.log(1600) - Math.log(12))));
    let text = "";
    while (text.length < length) text += SENTENCES[Math.floor(rnd() * SENTENCES.length)];
    text = text.slice(0, Math.max(12, length));
    const who = pick(rnd, ["user", "droid", "codex", "devin", "chatgpt", "cursor", "unknown"], [3, 3, 2.2, 1, 1.4, 0.35, 0.3]);
    const conversation = rnd() < 0.6 ? { title: CONVERSATIONS[Math.floor(rnd() * CONVERSATIONS.length)], rowId: `s-${seed}-${i}`, projectId: project?.path ?? "" } : undefined;
    const source = who === "user" ? { kind: "user" } : who === "unknown" ? { kind: "unknown" } : { kind: "agent", engine: who, ...(conversation ? { conversation } : {}) };
    const age = Math.exp(Math.log(2 * MIN) + rnd() * (Math.log(90 * DAY) - Math.log(2 * MIN)));
    const updated = Math.round(now - age);
    out.push({
      id: `n${seed}-${i}`,
      title,
      text,
      scope: isGlobal ? "global" : "project",
      project: project?.path ?? "",
      projectName: project?.name ?? "",
      kind,
      tags: [],
      created: Math.round(updated - rnd() * 10 * DAY),
      updated,
      status: rnd() < forgotten ? "forgotten" : "active",
      rev: 1,
      source,
      editor: source,
    });
  }
  return out;
}

/** Three notes like the design page's (src/mock/memory.ts). */
export function fewNotes(now = NOW) {
  const base = { tags: [], status: "active", rev: 1 };
  const user = { kind: "user" };
  return [
    { ...base, id: "m1", title: "回答用中文，代码标识符保持英文", text: "和我说话用中文；变量名、函数名、命令、报错原文保持英文，不要翻译。", scope: "global", project: "", projectName: "", kind: "preference", created: now - 72 * HOUR, updated: now - 72 * HOUR, source: user, editor: user },
    { ...base, id: "m3", title: "PowerShell 5.1 不支持 && 和 ||", text: "这台机器的 shell 是 Windows PowerShell 5.1：用分号串联命令，用 $LASTEXITCODE 判断结果。", scope: "global", project: "", projectName: "", kind: "pitfall", created: now - 26 * HOUR, updated: now - 26 * HOUR, source: { kind: "agent", engine: "droid", conversation: { title: "整理测试", rowId: "s-tests", projectId: PROJECTS[0].path } }, editor: { kind: "agent", engine: "droid" } },
    { ...base, id: "m5", title: "改了 frontend 要先 npm run build:app", text: "界面是单文件页面，不构建的话应用和 e2e 跑的都是旧页面。构建后再跑 node scripts/check.mjs。", scope: "project", project: PROJECTS[0].path, projectName: "wittycode", kind: "convention", created: now - 40 * MIN, updated: now - 40 * MIN, source: { kind: "agent", engine: "codex", conversation: { title: "修复缓存", rowId: "s-file-ops", projectId: PROJECTS[0].path } }, editor: { kind: "agent", engine: "codex" } },
  ];
}

/** Notes the layout must not trip over: no title, a huge one, an engine nobody knows, a project note without a folder,
 *  two projects of the same name, a forgotten note, emoji and other scripts. */
export function oddNotes(now = NOW) {
  const note = (id, extra) => ({ id, title: id, text: "内容", scope: "global", project: "", projectName: "", kind: "fact", tags: [], created: now - DAY, updated: now - DAY, status: "active", rev: 1, source: { kind: "user" }, editor: { kind: "user" }, ...extra });
  return [
    note("o-empty", { title: "" }),
    note("o-huge", { title: "很长的标题".repeat(400), text: "长".repeat(5000), kind: "pitfall" }),
    note("o-engine", { source: { kind: "agent", engine: "cursor" }, kind: "convention" }),
    note("o-nofolder", { scope: "project", project: "", projectName: "", kind: "preference" }),
    note("o-app1", { scope: "project", project: "D:\\Work\\app", projectName: "app", updated: now - 5 * MIN }),
    note("o-app2", { scope: "project", project: "D:\\Old\\app", projectName: "app", updated: now - 50 * DAY }),
    note("o-forgot", { status: "forgotten", kind: "pitfall", scope: "project", project: "D:\\Work\\app", projectName: "app" }),
    note("o-emoji", { title: "🚀 部署前先跑 🧪 测试 👩‍💻", kind: "convention" }),
    note("o-rtl", { title: "مرحبا بالعالم — Ελληνικά — Ünïcödé", kind: "preference" }),
  ];
}

export const SAMPLES = {
  few: (now = NOW) => fewNotes(now),
  mid: (now = NOW) => synthNotes(24, { seed: 5, now, projects: PROJECTS.slice(0, 3) }),
  many: (now = NOW) => synthNotes(300, { seed: 9, now }),
  odd: (now = NOW) => oddNotes(now),
  empty: () => [],
};

// ------------------------------------------------------------------------------------------------- the page

const fontData = (file) => `data:font/woff2;base64,${readFileSync(resolve(frontend, "node_modules", file)).toString("base64")}`;

/** tokens.css with each theme on a class, so one page can show both */
function scopedTokens() {
  return readFileSync(resolve(frontend, "src/styles/tokens.css"), "utf8")
    .replace(/:root\s*\{/, ":root, .theme-light {")
    .replace(/html\[data-theme="dark"\]\s*\{/, ".theme-dark {");
}

function fontsCss() {
  return readFileSync(resolve(frontend, "src/styles/fonts.css"), "utf8").replace(/url\("\.\.\/\.\.\/node_modules\/([^"]+)"\)/g, (_, file) => `url("${fontData(file)}")`);
}

const PAGE_CSS = `
*, *::before, *::after { box-sizing: border-box; }
body { margin: 0; background: #8f8f8f; font: 13.5px/1.5 var(--font-sans); -webkit-font-smoothing: antialiased; }
button { background: none; border: 0; padding: 0; margin: 0; font: inherit; color: inherit; cursor: pointer; }
.num { font-variant-numeric: tabular-nums; }
@keyframes fade { from { opacity: 0; } to { opacity: 1; } }
h1 { margin: 0; padding: 18px 24px 6px; font-size: 16px; color: #111; }
.row { display: flex; flex-wrap: wrap; gap: 0; }
.card { width: 768px; padding: 18px 24px 26px; background: var(--bg); color: var(--fg); }
.card h2 { margin: 0 0 10px; font-size: 12px; font-weight: 800; letter-spacing: 0.08em; color: var(--muted); }
.frame { width: 720px; }
`;

async function main() {
  const output = resolve(process.argv[2] ?? resolve(frontend, "../sandbox/starmap-dist/preview.html"));
  const { createServer } = await import("vite");
  const { createElement: h } = await import("react");
  const { renderToStaticMarkup } = await import("react-dom/server");
  const server = await createServer({
    root: frontend,
    configFile: false,
    logLevel: "error",
    appType: "custom",
    cacheDir: resolve(frontend, "../sandbox/starmap-dist/.vite"),
    optimizeDeps: { noDiscovery: true, include: [] },
    server: { middlewareMode: true, hmr: false, ws: false, watch: null },
  });
  try {
    const { MemoryStarMap } = await server.ssrLoadModule("/src/components/MemoryStarMap.tsx");
    const now = Date.now();
    const cases = [
      { id: "few", title: "3 条笔记", entries: SAMPLES.few(now) },
      { id: "few-selected", title: "3 条，选中一条", entries: SAMPLES.few(now), selectedId: "m5" },
      { id: "mid", title: "约 25 条", entries: SAMPLES.mid(now) },
      { id: "mid-source", title: "约 25 条 · 按来源着色 · 选中", entries: SAMPLES.mid(now), colorBy: "source", selectedId: "n5-3" },
      { id: "many", title: "300 条", entries: SAMPLES.many(now) },
      { id: "many-search", title: "300 条 · 搜索「PowerShell」", entries: SAMPLES.many(now), match: (e) => e.title.includes("PowerShell") },
      { id: "odd", title: "奇怪的数据", entries: SAMPLES.odd(now), selectedId: "o-huge" },
      { id: "empty", title: "没有笔记", entries: [] },
    ];
    const render = (c) =>
      renderToStaticMarkup(
        h(MemoryStarMap, {
          entries: c.entries,
          selectedId: c.selectedId ?? null,
          matchIds: c.match ? new Set(c.entries.filter(c.match).map((e) => e.id)) : null,
          colorBy: c.colorBy ?? "kind",
          onSelect() {},
        }),
      );
    const sections = cases
      .map((c) => {
        const markup = render(c);
        const card = (theme, label) => `<div class="card theme-${theme}" data-case="${c.id}-${theme}"><h2>${label} · ${c.title}</h2><div class="frame">${markup}</div></div>`;
        return `<section class="row">${card("light", "浅色")}${card("dark", "深色")}</section>`;
      })
      .join("\n");
    const css = [fontsCss(), scopedTokens(), PAGE_CSS, readFileSync(resolve(frontend, "src/styles/starmap.css"), "utf8")].join("\n");
    const only = `const only = new URLSearchParams(location.search).get("only"); if (only) { document.querySelectorAll("[data-case]").forEach((c) => { if (c.dataset.case !== only) c.remove(); }); document.querySelectorAll("section").forEach((s) => { if (!s.children.length) s.remove(); }); document.querySelector("h1")?.remove(); document.body.style.background = "transparent"; }`;
    const page = `<!doctype html><html lang="zh-CN"><head><meta charset="utf-8"><title>记忆星图 · 预览</title><style>${css}</style></head><body><h1>记忆星图 · 预览（静态，第一帧）</h1>\n${sections}\n<script>${only}</script></body></html>`;
    mkdirSync(dirname(output), { recursive: true });
    writeFileSync(output, page, "utf8");
    console.log(`wrote ${output} (${(page.length / 1024).toFixed(0)} KB, ${cases.length} cases x 2 themes)`);
  } finally {
    await server.close();
  }
}

if (import.meta.main) await main();
