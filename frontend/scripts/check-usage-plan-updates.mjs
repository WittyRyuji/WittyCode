// Live payloads must update an already open chart and plan; no model or real account is involved.
import assert from "node:assert/strict";
import { resolve } from "node:path";
import { pathToFileURL } from "node:url";
import { openPage } from "./cdp.mjs";
import { fakeBackendScripts } from "./fake-backend.mjs";

const index = pathToFileURL(resolve("dist/index.html")).href;
const ids = ["chatgpt:gpt-6-astra", "chatgpt:gpt-6.1-sol", "chatgpt:gpt-6.1-terra"];
for (const theme of ["light", "dark"]) {
  const page = await openPage(index, { width: 1500, height: 960, init: fakeBackendScripts().scripts });
  try {
    await page.waitFor("document.querySelectorAll('.srow').length >= 2 && !document.querySelector('.boot')");
    await page.click(".srow", 0);
    await page.waitFor("!!document.querySelector('.tab.current .dot-ready')");
    const id = await page.eval("__fake.events.findLast(([name, payload]) => name === 'tabs' && payload.select)[1].select");
    const emit = (name, payload) => page.eval(`__fake.emit(${JSON.stringify(name)}, ${JSON.stringify(payload)})`);
    await emit("settings", { settings: { maxOpen: 4, theme, fontScale: "md", sidebarWidth: 300 } });
    await emit("session", { id, patch: { usage: { input: 5800, output: 200, cacheWrite: null, cacheRead: 0 }, usageModels: Object.fromEntries(ids.map((name, i) => [name, [2100, 2000, 1900][i]])) } });
    const stale = { kind: "activity", id: "old-group", entries: [{ kind: "tool", id: "old-plan", name: "TodoWrite", category: "plan", status: "done", todos: [{ text: "不该继续显示的旧计划", state: "pending" }] }] };
    const turn = { id: "t-live", status: "running", prompts: [], blocks: [{ kind: "user", id: "u-live", text: "检查计划实时更新" }, stale], plan: { id: "same-id", steps: [] } };
    const update = async (steps) => { turn.plan.steps = steps; await emit("turns", { id, all: true, turns: [turn] }); };
    const steps = (completed) => ["检查事件", "修复同步", "运行回归"].map((text, i) => ({ text, state: i < completed ? "completed" : i === completed ? "in_progress" : "pending" }));
    await update(steps(0));
    await page.waitFor("document.querySelector('.plan-count')?.textContent === '0/3'");
    for (const done of [1, 2, 3]) {
      await update(steps(done));
      await page.waitFor(`document.querySelector('.plan-count')?.textContent === '${done}/3' && document.querySelectorAll('.plan-step.st-completed').length === ${done}`);
      assert(!(await page.eval("document.querySelector('.planpanel').textContent")).includes("旧计划"));
    }
    await page.click(".plan-head");
    await update([{ text: "新的第一步", state: "completed" }, { text: "更新后的进行中任务", state: "in_progress" }]);
    await page.waitFor("document.querySelector('.plan-count')?.textContent === '1/2'");
    assert(await page.eval("!document.querySelector('.plan-steps')"), "progress updates preserve the user's collapsed state");
    await page.click(".plan-head");
    await page.waitFor("document.querySelector('.plan-step.st-in_progress')?.textContent.includes('更新后的进行中任务')");
    await page.hover(".usage");
    await page.waitFor("document.querySelectorAll('.usage-swatch').length === 3");
    const colors = () => page.eval("Object.fromEntries([...document.querySelectorAll('.usage-legend li')].map(e => [e.dataset.model, getComputedStyle(e.querySelector('.usage-swatch')).backgroundColor]))");
    const before = await colors();
    assert.equal(new Set(Object.values(before)).size, 3, "the screenshot's three models have distinct actual RGB colors");
    assert((await page.eval("document.querySelector('.usage-pie').style.background")).includes("conic-gradient"));
    await page.shot(resolve(`shots/usage-plan-updates-${theme}.png`));
    await emit("session", { id, patch: { usageModels: { [ids[2]]: 2100, [ids[1]]: 2000, [ids[0]]: 1900 } } });
    await page.waitFor(`document.querySelector('.usage-legend li')?.dataset.model === '${ids[2]}'`);
    assert.deepEqual(await colors(), before, "live token ranking changes do not exchange model colors");
    const many = Object.fromEntries(Array.from({ length: 32 }, (_, i) => [`api:demo:model-${i}`, 10]));
    await emit("session", { id, patch: { usage: { input: 320, output: 0, cacheWrite: null, cacheRead: 0 }, usageModels: many } });
    await page.waitFor("document.querySelectorAll('.usage-swatch').length === 32");
    assert.equal(new Set(Object.values(await colors())).size, 32, "palette overflow still has no duplicate rendered RGB colors");
    await page.hover(".titlebar");
    await page.waitFor("!document.querySelector('.usage > .tip')");
    await page.click(".plan-close");
    await update([{ text: "关闭期间完成", state: "completed" }]);
    await page.waitFor("document.querySelector('.plan-toggle')?.textContent.includes('1/1')");
    assert(await page.eval("!document.querySelector('.planpanel')"));
    await page.click(".plan-toggle");
    await page.waitFor("document.querySelector('.plan-step')?.textContent.includes('关闭期间完成')");
    await update([]);
    await page.waitFor("!document.querySelector('.planpanel, .plan-toggle')");
    assert.equal(page.consoleErrors().length, 0, page.errorTexts().join("\n"));
    assert.equal(page.cspViolations().length, 0);
    console.log(`PASS usage and live plan: ${theme}, distinct muted colors, live rank stability, 32 models, same-id progress/text/clear updates, collapse and close preserved`);
  } finally {
    await page.close();
  }
}
