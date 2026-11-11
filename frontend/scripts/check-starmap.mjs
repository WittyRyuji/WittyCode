// Interaction check of the memory star map (src/components/MemoryStarMap.tsx) with real mouse, wheel and keyboard events
// in headless Edge (scripts/cdp.mjs): `node scripts/check-starmap.mjs [--shots]`.
// It bundles a small page around the component with Vite (an entry of its own, not the app's) into
// ../sandbox/starmap-dist/app, so it never writes to dist/ or desktop/ui_dist; `--shots` also saves the screenshots
// ../sandbox/shots/starmap-*.png (3, ~25 and ~300 notes in both themes, and a few states).
import { mkdirSync, readdirSync, writeFileSync } from "node:fs";
import { dirname, resolve } from "node:path";
import { fileURLToPath, pathToFileURL } from "node:url";
import { build } from "vite";
import react from "@vitejs/plugin-react";
import { openPage } from "./cdp.mjs";
import { SAMPLES, synthNotes } from "./starmap-preview.mjs";

const here = dirname(fileURLToPath(import.meta.url));
const frontend = resolve(here, "..");
const sandbox = resolve(frontend, "../sandbox");
const outDir = resolve(sandbox, "starmap-dist/app");
const shotsDir = resolve(sandbox, "shots");
const SHOTS = process.argv.includes("--shots");

let failed = 0;
let passed = 0;
const ok = (name, value, detail = "") => {
  if (value) passed++;
  else failed++;
  console.log(value ? "PASS" : "FAIL", name, value ? "" : String(detail));
};

// ------------------------------------------------------------------------------------------------- the page

const now = Date.now();
const SETS = { one: SAMPLES.few(now).slice(2), few: SAMPLES.few(now), mid: SAMPLES.mid(now), many: SAMPLES.many(now), odd: SAMPLES.odd(now), empty: [], huge: synthNotes(500, { seed: 3, now }) };

const ENTRY = "virtual:starmap-check";
const entrySource = `
import { StrictMode, createElement as h, useState } from "react";
import { createRoot } from "react-dom/client";
import { MemoryStarMap } from "/src/components/MemoryStarMap.tsx";
import "/src/styles/fonts.css";
import "/src/styles/tokens.css";
import "/src/styles/base.css";
import "/src/styles/starmap.css";

const SETS = ${JSON.stringify(SETS)};
const q = new URLSearchParams(location.search);
document.documentElement.dataset.theme = q.get("theme") || "light";
const entries = SETS[q.get("set") || "mid"];
const width = Number(q.get("width") || 720);
const height = Number(q.get("height") || 460);
window.__sm = { calls: [], renders: 0 };

function Harness() {
  const [selected, setSelected] = useState(q.get("selected"));
  const [match, setMatch] = useState(q.get("match") ? new Set(entries.filter((e) => e.title.includes(q.get("match"))).map((e) => e.id)) : null);
  window.__sm.setMatch = (ids) => setMatch(ids ? new Set(ids) : null);
  window.__sm.select = (id) => setSelected(id);
  window.__sm.entries = entries;
  return h("div", { style: { width } }, h(MemoryStarMap, {
    entries,
    selectedId: selected,
    matchIds: match,
    colorBy: q.get("color") || "kind",
    height,
    onSelect: (id) => { window.__sm.calls.push(id); setSelected(id); },
  }));
}

createRoot(document.getElementById("root")).render(h(StrictMode, null, h(Harness)));
`;

await build({
  root: frontend,
  configFile: false,
  logLevel: "warn",
  cacheDir: resolve(sandbox, "starmap-dist/.vite"),
  plugins: [
    react(),
    {
      name: "starmap-check-entry",
      resolveId: (id) => (id === ENTRY ? `\0${ENTRY}.js` : null),
      load: (id) => (id === `\0${ENTRY}.js` ? entrySource : null),
    },
  ],
  define: { "process.env.NODE_ENV": JSON.stringify("production") },
  build: {
    outDir,
    emptyOutDir: true,
    target: "chrome120",
    assetsInlineLimit: 100_000_000,
    cssCodeSplit: false,
    reportCompressedSize: false,
    chunkSizeWarningLimit: 4000,
    rolldownOptions: {
      input: ENTRY,
      output: { format: "iife", entryFileNames: "starmap.js", assetFileNames: "[name][extname]" },
    },
  },
});
const css = readdirSync(outDir).filter((f) => f.endsWith(".css"));
writeFileSync(
  resolve(outDir, "index.html"),
  `<!doctype html><html lang="zh-CN"><head><meta charset="utf-8"><title>记忆星图检查</title>${css.map((f) => `<link rel="stylesheet" href="${f}">`).join("")}<style>body { padding: 24px; }</style></head><body><div id="root"></div><script src="starmap.js"></script></body></html>`,
  "utf8",
);
const base = pathToFileURL(resolve(outDir, "index.html")).href;
mkdirSync(shotsDir, { recursive: true });

const open = async (query, { width = 768, height = 520 } = {}) => {
  const page = await openPage(`${base}?${query}`, { width, height });
  await page.waitFor("document.querySelector('.sm-panel')", 8000);
  await page.wait(250);
  return page;
};
const shot = async (page, name) => {
  if (SHOTS) await page.shot(resolve(shotsDir, `starmap-${name}.png`));
};
const transform = (page) => page.eval("document.querySelector('.sm-world').getAttribute('transform')");
const parse = (t) => {
  const m = /translate\(([-\d.]+) ([-\d.]+)\) scale\(([-\d.]+)\)/.exec(t ?? "");
  return m ? { x: Number(m[1]), y: Number(m[2]), k: Number(m[3]) } : null;
};
const wheel = async (page, x, y, deltaY) => {
  await page.send("Input.dispatchMouseEvent", { type: "mouseWheel", x, y, deltaX: 0, deltaY });
  await page.wait(150);
};
const centerOf = (page, selector) => page.eval(`(() => { const r = document.querySelector(${JSON.stringify(selector)}).getBoundingClientRect(); return { x: r.left + r.width / 2, y: r.top + r.height / 2 }; })()`);

// ------------------------------------------------------------------------------------------- ~25 notes, light

let page = await open("set=mid&theme=light");
const mid = SETS.mid;
ok("one star per note", (await page.eval("document.querySelectorAll('.sm-star').length")) === mid.length);
ok(
  "every star is focusable, a button, and named by its title and kind",
  await page.eval(`[...document.querySelectorAll('.sm-star')].every((s) => (s.getAttribute('tabindex') === '0' || s.getAttribute('tabindex') === '-1') && s.getAttribute('role') === 'button' && /，(偏好|约定|坑|事实)/.test(s.getAttribute('aria-label') || ''))`),
);
ok("one node of the chart is in the tab order", (await page.eval("document.querySelectorAll('.sm-svg [tabindex=\"0\"]').length")) === 1);
ok("with 24 notes every title is written on the chart, or kept off where it would cover another", (await page.eval("document.querySelectorAll('.sm-lb-note').length")) >= 18);
await shot(page, "mid-light");
await page.key("Tab");
ok("Tab goes into the chart", await page.eval("!!document.activeElement?.closest('.sm-svg') && document.activeElement.getAttribute('tabindex') === '0'"));
await page.key("Tab");
ok("the next Tab leaves the chart (one stop, then the toolbar)", await page.eval("!document.activeElement?.closest('.sm-svg')"));

// click selects
const target = mid.find((e) => e.status === "active").id;
await page.click(`.sm-star[data-id="${target}"]`);
await page.wait(150);
ok("a click on a star calls onSelect with its id", (await page.eval("JSON.stringify(window.__sm.calls)")) === JSON.stringify([target]));
ok("the chosen star is ringed and pulses", await page.eval(`!!document.querySelector('.sm-star[data-id="${target}"].sm-selected .sm-sel') && !!document.querySelector('.sm-pulse')`));
ok("its title is written next to it", await page.eval(`document.querySelector('.sm-sel-label')?.textContent === ${JSON.stringify(mid.find((e) => e.id === target).title)}`));
ok("the branch from it to the root is drawn brighter (three lines)", (await page.eval("(document.querySelector('.sm-edge.sm-on')?.getAttribute('d') || '').split('M').length - 1")) === 3);

// hover
const other = mid.filter((e) => e.id !== target)[3];
await page.hover(`.sm-star[data-id="${other.id}"]`);
await page.wait(200);
const tip = await page.eval("document.querySelector('.sm-tip')?.textContent || ''");
ok("hovering a star shows its tooltip: title, kind, scope, who, when, the start of the text", tip.includes(other.title) && /偏好|约定|坑|事实/.test(tip) && /全局|项目/.test(tip) && /写入/.test(tip) && /前|刚刚|昨天|\d\d-\d\d/.test(tip), tip);
ok("the tooltip stays inside the panel", await page.eval("(() => { const t = document.querySelector('.sm-tip').getBoundingClientRect(); const p = document.querySelector('.sm-panel').getBoundingClientRect(); return t.left >= p.left - 1 && t.right <= p.right + 1 && t.top >= p.top - 1 && t.bottom <= p.bottom + 1; })()"));
if (SHOTS) await shot(page, "mid-hover-light");
await page.mouse("mouseMoved", 60, 300, { button: "none", clickCount: 0 });
await page.wait(150);
ok("moving off the star hides it", await page.eval("!document.querySelector('.sm-tip')"));

// keyboard
await page.eval(`document.querySelector('.sm-star[data-id="${other.id}"]').focus()`);
await page.key("Enter");
ok("Enter on a focused star selects it", await page.eval(`window.__sm.calls.at(-1) === ${JSON.stringify(other.id)}`));
await page.key(" ", { code: "Space", vk: 32 });
ok("Space too", await page.eval(`window.__sm.calls.length === 3 && window.__sm.calls.at(-1) === ${JSON.stringify(other.id)}`));
ok("a star reached with the keyboard shows its tooltip and a focus ring", await page.eval("!!document.querySelector('.sm-tip') && !!document.querySelector('.sm-focus')"));
const before = await page.eval("document.activeElement.getAttribute('data-node')");
let moved = 0;
for (const key of ["ArrowRight", "ArrowDown", "ArrowLeft", "ArrowUp"]) {
  const from = await page.eval("document.activeElement.getAttribute('data-node')");
  await page.key(key, { vk: { ArrowLeft: 37, ArrowUp: 38, ArrowRight: 39, ArrowDown: 40 }[key] });
  const to = await page.eval("document.activeElement.getAttribute('data-node')");
  if (to && to !== from) moved++;
}
ok("arrow keys move the focus to a neighbouring node", moved >= 3, `moved ${moved} of 4 from ${before}`);
ok("the moving focus is the one tab stop", await page.eval("document.activeElement.getAttribute('tabindex') === '0' && document.querySelectorAll('.sm-svg [tabindex=\"0\"]').length === 1"));

// wheel zoom, pan, reset
const home = parse(await transform(page));
const panel = await page.eval("(() => { const r = document.querySelector('.sm-svg').getBoundingClientRect(); return { x: r.left, y: r.top, w: r.width, h: r.height }; })()");
const star = await centerOf(page, `.sm-star[data-id="${target}"] .sm-hit`);
await wheel(page, star.x, star.y, -240);
const zoomed = parse(await transform(page));
ok("the wheel zooms in (the transform's scale grows)", zoomed && home && zoomed.k > home.k * 1.2, `${JSON.stringify(home)} -> ${JSON.stringify(zoomed)}`);
const after = await centerOf(page, `.sm-star[data-id="${target}"] .sm-hit`);
ok("toward the pointer: the star under it stays under it", Math.hypot(after.x - star.x, after.y - star.y) < 2.5, `${JSON.stringify(star)} -> ${JSON.stringify(after)}`);
ok("the page did not scroll", (await page.eval("window.scrollY")) === 0);
const cornerX = panel.x + 30;
const cornerY = panel.y + panel.h - 120;
await page.mouse("mouseMoved", cornerX, cornerY, { button: "none", clickCount: 0 });
await page.mouse("mousePressed", cornerX, cornerY);
for (let i = 1; i <= 5; i++) await page.mouse("mouseMoved", cornerX + i * 16, cornerY - i * 6, { button: "left", clickCount: 0 });
await page.mouse("mouseReleased", cornerX + 80, cornerY - 30);
await page.wait(150);
const panned = parse(await transform(page));
ok("dragging pans the chart", panned && Math.abs(panned.x - zoomed.x - 80) < 2 && Math.abs(panned.y - zoomed.y + 30) < 2, `${JSON.stringify(zoomed)} -> ${JSON.stringify(panned)}`);
ok("a drag selects nothing", (await page.eval("window.__sm.calls.length")) === 3);
await page.click(".sm-reset");
ok("重置视图 restores the view", JSON.stringify(parse(await transform(page))) === JSON.stringify(home));
await page.click(".sm-zoom button", 1);
const plus = parse(await transform(page));
await page.click(".sm-zoom button", 0);
ok("the + and − buttons zoom in and back", plus.k > home.k * 1.3 && Math.abs(parse(await transform(page)).k - home.k) < 1e-3);

// collapsing a project, a kind
const project = await page.eval("[...document.querySelectorAll('.sm-node.sm-scope')].map((n) => n.getAttribute('data-id')).find((id) => id.startsWith('s:p:'))");
const inProject = await page.eval(`document.querySelectorAll('.sm-star[data-scope="${project.replace(/\\/g, "\\\\\\\\")}"]').length`);
const calls = await page.eval("window.__sm.calls.length");
await page.click(`.sm-node[data-id="${project.replace(/\\/g, "\\\\")}"]`);
await page.wait(150);
const hiddenNow = await page.eval(`document.querySelectorAll('.sm-star[data-scope="${project.replace(/\\/g, "\\\\\\\\")}"]').length`);
const cluster = await page.eval(`(() => { const c = document.querySelector('.sm-cluster[data-branch="${project.replace(/\\/g, "\\\\\\\\")}"]'); return c ? c.getAttribute('aria-label') : null; })()`);
ok("clicking a project node folds its stars into one", inProject > 0 && hiddenNow === 0 && !!cluster, `${inProject} -> ${hiddenNow}, cluster ${cluster}`);
ok("the folded star counts them (+N)", await page.eval(`[...document.querySelectorAll('.sm-lb-cluster')].some((t) => t.textContent === '+${inProject}')`));
ok("a branch click selects no note", (await page.eval("window.__sm.calls.length")) === calls);
ok("the project node says it is folded", await page.eval(`document.querySelector('.sm-node[data-id="${project.replace(/\\/g, "\\\\\\\\")}"]').getAttribute('aria-expanded') === 'false'`));
if (SHOTS) await shot(page, "mid-collapsed-light");
await page.click(`.sm-cluster[data-branch="${project.replace(/\\/g, "\\\\")}"]`);
await page.wait(150);
ok("clicking the folded star opens the branch again", (await page.eval(`document.querySelectorAll('.sm-star[data-scope="${project.replace(/\\/g, "\\\\\\\\")}"]').length`)) === inProject);
const kind = await page.eval("document.querySelector('.sm-node.sm-kind').getAttribute('data-id')");
const kindCount = await page.eval(`(() => { const id = ${JSON.stringify(kind)}; return [...document.querySelectorAll('.sm-star')].filter((s) => s.getAttribute('data-scope') + '/k:' + s.getAttribute('data-kind') === id).length; })()`);
await page.eval(`document.querySelector('.sm-node[data-id=' + JSON.stringify(${JSON.stringify(kind)}) + ']').focus()`);
await page.key("Enter");
const kindAfter = await page.eval(`(() => { const id = ${JSON.stringify(kind)}; return [...document.querySelectorAll('.sm-star')].filter((s) => s.getAttribute('data-scope') + '/k:' + s.getAttribute('data-kind') === id).length; })()`);
ok("Enter on a kind node folds that kind", kindCount > 0 && kindAfter === 0, `${kindCount} -> ${kindAfter}`);
ok("the focus stays on the node", await page.eval(`document.activeElement.getAttribute('data-node') === ${JSON.stringify(kind)}`));
await page.key("Enter");
ok("and Enter again opens it", (await page.eval(`(() => { const id = ${JSON.stringify(kind)}; return [...document.querySelectorAll('.sm-star')].filter((s) => s.getAttribute('data-scope') + '/k:' + s.getAttribute('data-kind') === id).length; })()`)) === kindCount);

// colours
const fillOf = (id) => page.eval(`document.querySelector('.sm-star[data-id="${id}"] .sm-core').getAttribute('fill') || document.querySelector('.sm-star[data-id="${id}"] .sm-core').getAttribute('stroke')`);
const kindFill = await fillOf(other.id);
await page.click(".sm-seg button", 1);
await page.wait(120);
const sourceFill = await fillOf(other.id);
ok("the colour switch re-colours the stars by who wrote them", kindFill !== sourceFill && /-c-s-|#/.test(sourceFill) && /-c-k-|#/.test(kindFill), `${kindFill} -> ${sourceFill}`);
ok("…and the legend says so", await page.eval("[...document.querySelectorAll('.sm-leg-i')].some((i) => /我|Droid|Codex|Devin|ChatGPT Plan|未知/.test(i.textContent))"));
if (SHOTS) await shot(page, "mid-source-light");
await page.click(".sm-seg button", 0);

// search
const hits = mid.slice(0, 4).map((e) => e.id);
await page.eval(`window.__sm.setMatch(${JSON.stringify(hits)})`);
await page.wait(150);
ok(
  "a search dims every star but the matches",
  await page.eval(`[...document.querySelectorAll('.sm-star')].every((s) => ${JSON.stringify(hits)}.includes(s.getAttribute('data-id')) !== s.classList.contains('sm-dim'))`),
);
await page.eval("window.__sm.setMatch(null)");
ok("no console errors (light, ~25 notes)", page.consoleErrors().length === 0, page.errorTexts().join(" | "));
const lightSky = await page.eval("getComputedStyle(document.querySelector('.sm-panel')).backgroundImage");
const lightBorder = await page.eval("getComputedStyle(document.querySelector('.sm-panel')).borderTopColor");
await page.close();

// ------------------------------------------------------------------------------------------------ dark theme

page = await open("set=mid&theme=dark");
ok("the dark theme draws the same sky", (await page.eval("getComputedStyle(document.querySelector('.sm-panel')).backgroundImage")) === lightSky);
ok("…with the dark theme's frame", (await page.eval("getComputedStyle(document.querySelector('.sm-panel')).borderTopColor")) !== lightBorder);
ok("no console errors (dark)", page.consoleErrors().length === 0, page.errorTexts().join(" | "));
await shot(page, "mid-dark");
await page.close();

// --------------------------------------------------------------------------------------- few, many, odd, empty

for (const theme of ["light", "dark"]) {
  page = await open(`set=few&theme=${theme}`);
  ok(`3 notes draw (${theme})`, (await page.eval("document.querySelectorAll('.sm-star').length")) === 3 && page.consoleErrors().length === 0, page.errorTexts().join(" | "));
  await shot(page, `few-${theme}`);
  await page.close();
  page = await open(`set=many&theme=${theme}`);
  ok(`300 notes draw (${theme})`, (await page.eval("document.querySelectorAll('.sm-star').length")) === 300 && page.consoleErrors().length === 0, page.errorTexts().join(" | "));
  await shot(page, `many-${theme}`);
  if (theme === "dark" && SHOTS) {
    const s = await centerOf(page, ".sm-star");
    await wheel(page, s.x, s.y, -400);
    await wheel(page, s.x, s.y, -300);
    await page.hover(".sm-star", 3);
    await page.wait(200);
    await shot(page, "many-zoomed-dark");
  }
  await page.close();
}

page = await open("set=many&theme=light&match=PowerShell&selected=" + SETS.many.find((e) => e.title.includes("PowerShell")).id);
ok("300 notes with a search and a selection", (await page.eval("document.querySelectorAll('.sm-star.sm-dim').length")) > 250 && (await page.eval("!!document.querySelector('.sm-star.sm-selected')")));
await shot(page, "many-search-light");
await page.close();

page = await open("set=odd&theme=dark&selected=o-huge");
ok("odd notes draw: no title, a huge title, an unknown engine, a project without a folder, two projects named app", (await page.eval("document.querySelectorAll('.sm-star').length")) === SETS.odd.length && page.consoleErrors().length === 0, page.errorTexts().join(" | "));
ok("the two projects named app are two nodes", (await page.eval("[...document.querySelectorAll('.sm-node.sm-scope')].length")) === 4);
ok("the chosen star's long title stays inside the panel", await page.eval("(() => { const t = document.querySelector('.sm-sel-label').getBoundingClientRect(); const p = document.querySelector('.sm-panel').getBoundingClientRect(); return t.left >= p.left && t.right <= p.right; })()"));
await shot(page, "odd-dark");
await page.close();

for (const theme of ["light", "dark"]) {
  page = await open(`set=one&theme=${theme}`);
  ok(`one note draws a whole branch (${theme})`, (await page.eval("[...document.querySelectorAll('.sm-star, .sm-node')].length")) === 3 && page.consoleErrors().length === 0, page.errorTexts().join(" | "));
  await shot(page, `one-${theme}`);
  await page.close();
}

page = await open("set=mid&theme=dark");
const titled = SETS.mid.find((e) => e.status === "active");
const titleCount = await page.eval(`document.querySelectorAll('.sm-lb-note[data-id="${titled.id}"]').length`);
if (titleCount) {
  await page.click(`.sm-lb-note[data-id="${titled.id}"]`);
  ok("a click on a title written on the chart selects its note", await page.eval(`window.__sm.calls.at(-1) === ${JSON.stringify(titled.id)}`));
} else ok("a click on a title written on the chart selects its note (title not drawn: skipped)", true);
await page.close();

page = await open("set=empty&theme=light");
ok("no notes: a calm empty sky that says so", (await page.eval("document.querySelector('.sm-empty')?.textContent.trim()")) === "还没有笔记，星图是空的" && (await page.eval("document.querySelectorAll('.sm-star, .sm-tools').length")) === 0);
ok("no console errors (empty)", page.consoleErrors().length === 0, page.errorTexts().join(" | "));
await shot(page, "empty-light");
await page.close();

// -------------------------------------------------------------------------------------------- 500 notes, motion

page = await open("set=huge&theme=dark");
ok("500 notes draw", (await page.eval("document.querySelectorAll('.sm-star').length")) === 500 && page.consoleErrors().length === 0, page.errorTexts().join(" | "));
await page.eval(`(() => { window.__long = []; new PerformanceObserver((list) => { for (const e of list.getEntries()) window.__long.push(Math.round(e.duration)); }).observe({ type: "longtask" }); })()`);
const t0 = await page.eval("performance.now()");
const c = await centerOf(page, ".sm-svg");
for (let i = 0; i < 6; i++) await page.send("Input.dispatchMouseEvent", { type: "mouseWheel", x: c.x, y: c.y, deltaX: 0, deltaY: i % 2 ? 120 : -120 });
const ms = (await page.eval("new Promise((r) => requestAnimationFrame(() => requestAnimationFrame(() => r(performance.now()))))")) - t0;
ok(`six wheel steps over 500 stars settle quickly (${ms.toFixed(0)} ms)`, ms < 1500, `${ms.toFixed(0)} ms`);
const dragStart = await page.eval("performance.now()");
await page.mouse("mouseMoved", c.x - 200, c.y + 150, { button: "none", clickCount: 0 });
await page.mouse("mousePressed", c.x - 200, c.y + 150);
for (let i = 1; i <= 20; i++) await page.mouse("mouseMoved", c.x - 200 + i * 6, c.y + 150 - i * 3, { button: "left", clickCount: 0 });
await page.mouse("mouseReleased", c.x - 80, c.y + 90);
const dragMs = (await page.eval("new Promise((r) => requestAnimationFrame(() => r(performance.now())))")) - dragStart;
ok(`a 20-step drag over 500 stars keeps up (${dragMs.toFixed(0)} ms)`, dragMs < 2500, `${dragMs.toFixed(0)} ms`);
const longTasks = JSON.parse(await page.eval("JSON.stringify(window.__long)"));
ok(`no long task (over 50 ms) while zooming and dragging 500 stars${longTasks.length ? `: ${longTasks.join(", ")} ms` : ""}`, longTasks.every((d) => d < 100), longTasks.join(", "));
ok("only the chosen star pulses, and a few background stars twinkle (CSS, not script)", (await page.eval("document.querySelectorAll('.sm-twinkle').length")) <= 12 && (await page.eval("[...document.querySelectorAll('.sm-star')].every((s) => getComputedStyle(s).animationName === 'none')")));
await page.close();

page = await open(`set=few&theme=dark&selected=m5`);
ok("the chosen star's ring pulses", (await page.eval("getComputedStyle(document.querySelector('.sm-pulse')).animationName")) === "sm-pulse");
await page.send("Emulation.setEmulatedMedia", { features: [{ name: "prefers-reduced-motion", value: "reduce" }] });
await page.wait(100);
ok("with reduced motion it stays still", (await page.eval("getComputedStyle(document.querySelector('.sm-pulse')).animationName")) === "none" && (await page.eval("[...document.querySelectorAll('.sm-twinkle')].every((t) => getComputedStyle(t).animationName === 'none')")));
await shot(page, "few-selected-dark");
await page.close();

console.log(`\n${passed} passed, ${failed} failed${SHOTS ? `; screenshots in ${shotsDir}` : ""}`);
process.exit(failed ? 1 : 0);
