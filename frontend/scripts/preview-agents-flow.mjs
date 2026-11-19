// From frontend/: npm run build:app; node scripts/preview-agents-flow.mjs
import assert from "node:assert/strict";
import { mkdirSync, writeFileSync } from "node:fs";
import { resolve } from "node:path";
import { pathToFileURL } from "node:url";
import { openPage } from "./cdp.mjs";
import { fakeBackendScripts } from "./fake-backend.mjs";
import { agentFlowColours } from "./check-agents-team.mjs";

const out = resolve(process.env.WITTYCODE_TEST_WORK ?? "../outputs", "agents-flow-preview");
mkdirSync(out, { recursive: true });
const url = pathToFileURL(resolve("dist/index.html")).href;
const { scripts, client } = fakeBackendScripts();
const report = { client, window: { width: 1440, height: 900 }, shots: {}, colours: {}, interaction: {}, consoleErrors: [], cspViolations: [] };
const time = Date.now();
const expert = (id, name, status, order, fusionRole = "worker") => ({
  id, name, status, fusionRole, role: fusionRole === "light" ? "轻量专家" : "工作专家",
  model: fusionRole === "light" ? "GPT-6-Luna" : "GPT-6.1-Sol", effort: "high",
  task: `复核${name}的示例。\n\n记录预期和实际结果。`, startedAt: time - order * 1000,
  finishedAt: ["done", "failed", "stopped"].includes(status) ? time - order * 1000 + 500 : null,
  tokens: status === "stopped" ? null : { input: 8500, output: 1500, cacheWrite: null, cacheRead: 12000 },
});
const historical = [expert("login", "登录流程", "done", 0), expert("tests", "回归测试", "stopped", 1), expert("docs", "文档校对", "done", 2, "light")];
const active = [expert("login", "登录流程", "working", 0), expert("docs", "文档校对", "working", 1, "light"),
  expert("tests", "回归测试", "waiting", 2), expert("sample", "示例目录", "done", 3),
  expert("preview", "界面预览", "failed", 4), expert("guide", "使用说明", "stopped", 5)];
const helpText = "当前在核对示例，专家的进展可在右侧查看。";
const block = (i, kind, text) => ({ id: `block-${i}`, kind, [kind === "text" ? "markdown" : "text"]: text, streaming: false });
const steps = ["确认检查范围", "准备示例数据", "分配登录流程", "分配文档校对", "记录复核结果"];

const measures = `(() => {
  const $ = (s) => document.querySelector(s);
  const r = (el) => { const {x, y, width, height} = el.getBoundingClientRect(); return { x, y, width, height }; };
  const chart = $('.agent-flow'), rail = $('.conversation-rail'), scroll = $('.conversation-rail-content');
  const sprite = (el) => el ? { onScreen: r(el), css: { width: el.offsetWidth, height: el.offsetHeight } } : null;
  const view = chart && new DOMMatrixReadOnly(getComputedStyle(chart.querySelector('.react-flow__viewport')).transform);
  const box = chart?.getBoundingClientRect();
  const allNodesFit = chart ? [...chart.querySelectorAll('.react-flow__node')].every((node) => {
    const b = node.getBoundingClientRect();
    return b.left >= box.left && b.right <= box.right && b.top >= box.top && b.bottom <= box.bottom;
  }) : null;
  return { chart: chart ? r(chart) : null, heightShare: chart ? r(chart).height / innerHeight : null, allNodesFit,
    railWidth: rail ? r(rail).width : null, inspectorWidth: $('.agentpanel') ? r($('.agentpanel')).width : null,
    railScrollTop: scroll?.scrollTop ?? null, railMaxScroll: scroll ? scroll.scrollHeight - scroll.clientHeight : null,
    viewport: view ? { x: view.e, y: view.f, zoom: view.a } : null,
    nodes: chart?.querySelectorAll('.react-flow__node').length ?? 0, edges: chart?.querySelectorAll('.agent-flow-edge').length ?? 0,
    chipSprite: sprite($('.agent-chip .dragon-sprite')), chartSprite: sprite($('.agent-flow-node .dragon-sprite')),
    attribution: !!chart?.querySelector('.react-flow__attribution') || !!chart?.textContent.includes('React Flow') };
})()`;

async function capture(theme, history, fn) {
  const page = await openPage(url, { ...report.window, init: scripts, hideScrollbars: false });
  const js = page.eval;
  const shot = async (filename) => {
    const layout = await js(measures);
    if (layout.chart && !filename.includes("zoomed")) assert.ok(layout.allNodesFit, `${filename}: the fitted canvas shows all nodes`);
    assert.equal(layout.attribution, false, `${filename}: no visible React Flow attribution`);
    report.shots[filename] = layout;
    await page.shot(resolve(out, filename));
    console.log(filename, JSON.stringify(layout));
  };
  try {
    await page.send("Emulation.setEmulatedMedia", { features: [{ name: "prefers-reduced-motion", value: "no-preference" }] });
    await page.waitFor("!!document.querySelector('.srow')");
    await page.click(".srow");
    await page.waitFor("!!document.querySelector('.tab.current .dot-ready')");
    const id = await js("__fake.events.findLast(([name, payload]) => name === 'tabs' && payload.select)[1].select");
    const emit = (name, payload) => js(`__fake.emit(${JSON.stringify(name)}, ${JSON.stringify(payload)})`);
    const team = history ? historical : active;
    const runs = Object.fromEntries(team.map((agent) => [agent.id, [{ id: "expert-run", status: "done", prompts: [], blocks: [
      block(1, "user", agent.task), block(2, "text", "已核对示例数据，结果已记录。") ] }]]));
    await emit("settings", { settings: { maxOpen: 4, theme, fontScale: "md", sidebarWidth: 260, conversationWidth: 280 } });
    await js(`__fake.patch(${JSON.stringify(id)}, ${JSON.stringify({ title: "示例项目 · 专家协作", mark: history ? "ready" : "running",
      workingLabel: history ? "就绪" : "正在检查样例…", agents: team, agentRuns: runs })})`);
    await emit("turns", { id, all: true, turns: [{ id: "demo-plan", status: history ? "done" : "running", prompts: [],
      blocks: [block(3, "user", "请按计划检查示例项目。"), block(4, "text", history ? "检查完成，可以回看各位专家的工作记录。" : helpText)],
      plan: { id: "demo-plan", steps: steps.slice(0, history ? 4 : steps.length).map((text, i) => ({ text,
        state: history || i < 2 ? "completed" : i === 2 ? "in_progress" : "pending" })) } }] });
    await page.waitFor(`document.documentElement.dataset.theme === '${theme}' && document.querySelectorAll('.agent-flow-edge').length === ${team.length}`);
    await page.click(".conversation-rail .agent-history-toggle");
    await js("document.querySelector('.conversation-rail-content').scrollTop = 0");
    await page.wait(200);
    if (!history) report.colours[theme] = await js(`(${agentFlowColours.toString()})()`);
    await fn({ page, js, shot });
  } finally {
    report.consoleErrors.push(...page.errorTexts().filter((text) => !text.startsWith("CSP violation")));
    report.cspViolations.push(...page.cspViolations());
    await page.close();
  }
}

await capture("light", true, async ({ shot }) => shot("rail-light-history.png"));
await capture("dark", false, async ({ shot }) => shot("rail-dark-active.png"));
await capture("light", false, async ({ page, js, shot }) => {
  await shot("rail-light-active.png");
  const separator = await page.rect(".conversation-splitter");
  const handleX = separator.x + separator.w / 2, handleY = separator.y + 20;
  await page.mouse("mousePressed", handleX, handleY, { buttons: 1 });
  await page.mouse("mouseMoved", handleX - 140, handleY, { buttons: 1 });
  await page.mouse("mouseReleased", handleX - 140, handleY, { buttons: 0 });
  await page.waitFor("document.querySelector('.conversation-rail').clientWidth === 420");
  await page.wait(200);
  await shot("rail-wide-light.png");

  await js("document.querySelector('.conversation-rail-content').scrollTop = 16");
  const before = await js(measures);
  assert.ok(before.railScrollTop > 0 && before.railScrollTop < before.railMaxScroll, "the rail can scroll in either direction");
  const { chart } = before;
  const x = chart.x + chart.width * 0.48, y = chart.y + chart.height * 0.48;
  await page.send("Input.dispatchMouseEvent", { type: "mouseWheel", x, y, deltaX: 0, deltaY: -150 });
  await page.wait(300);
  const wheel = await js(measures);
  await page.mouse("mousePressed", x, y, { button: "middle", buttons: 4 });
  await page.mouse("mouseMoved", x + 24, y + 13, { button: "middle", buttons: 4 });
  await page.mouse("mouseReleased", x + 24, y + 13, { button: "middle", buttons: 0 });
  await page.wait(200);
  const drag = await js(measures);
  report.interaction = {
    beforeWheel: before.viewport, afterWheel: wheel.viewport, afterMiddleDrag: drag.viewport,
    railScrollTopBeforeWheel: before.railScrollTop, railScrollTopAfterWheel: wheel.railScrollTop,
    railScrollTopChanged: before.railScrollTop !== wheel.railScrollTop,
  };
  await shot("rail-zoomed-light.png");
  assert.ok(wheel.viewport.zoom > before.viewport.zoom, "the real wheel zooms the chart in");
  assert.equal(report.interaction.railScrollTopChanged, false, "the wheel does not scroll the rail over the chart");
  assert.ok(Math.abs(drag.viewport.x - wheel.viewport.x - 24) < 1 && Math.abs(drag.viewport.y - wheel.viewport.y - 13) < 1,
    "the real middle drag pans both axes");
  await page.click('.agent-flow-node.working button.agent-flow-character');
  await page.waitFor("!!document.querySelector('.agentpanel .agent-chip.on .dragon-sprite') && document.querySelector('.agentpanel-run').textContent.includes('结果已记录')");
  await shot("inspector-light.png");
});
writeFileSync(resolve(out, "report.json"), `${JSON.stringify(report, null, 2)}\n`);
assert.deepEqual(report.consoleErrors, []);
assert.deepEqual(report.cspViolations, []);
console.log("Wrote 6 screenshots and report.json");
