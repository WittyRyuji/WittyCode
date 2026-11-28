// Native HTML drag/drop and menus in Chromium (not synthetic DOM DragEvents), over the sidebar's new
// layout: one global pinned strip between 新会话 and the projects, draggable project titles, ordinary
// rows reordering inside their own project only. The pure ordering helpers get plain-Node checks first.
import assert from "node:assert/strict";
import { resolve } from "node:path";
import { pathToFileURL } from "node:url";
import { openPage } from "./cdp.mjs";
import { fakeBackendScripts } from "./fake-backend.mjs";
import { moveBefore, orderProjects } from "../src/lib/projectOrder.ts";
import { pinnedOrder, sameSessionPartition } from "../src/lib/sessionOrder.ts";

// ---------------------------------------------------------------- pure ordering helpers

assert.deepEqual(moveBefore(["a", "b", "c"], "c", "a", false), ["c", "a", "b"], "moveBefore: before");
assert.deepEqual(moveBefore(["a", "b", "c"], "a", "c", true), ["b", "c", "a"], "moveBefore: after");
assert.deepEqual(moveBefore(["a", "b"], "a", "a", true), ["a", "b"], "moveBefore: same id is a no-op");
assert.deepEqual(moveBefore(["a", "b"], "x", "a", false), ["a", "b"], "moveBefore: unknown source is a no-op");
assert.deepEqual(orderProjects([{ id: "a" }, { id: "b" }, { id: "c" }], ["c", "a"]).map((p) => p.id), ["c", "a", "b"], "orderProjects: named first, the forgotten keep their place");

const row = (id, projectId, pinned = false) => ({ id, projectId, pinned });
assert.equal(sameSessionPartition(row("a", "p1"), row("b", "p1")), true, "ordinary: same project");
assert.equal(sameSessionPartition(row("a", "p1"), row("b", "p2")), false, "ordinary: cross project refused");
assert.equal(sameSessionPartition(row("a", "p1", true), row("b", "p2", true)), true, "pinned: one partition across projects");
assert.equal(sameSessionPartition(row("a", "p1", true), row("b", "p1")), false, "pinned onto ordinary refused");
assert.equal(sameSessionPartition(row("a", "p1"), row("b", "p1", true)), false, "ordinary onto pinned refused");
assert.equal(sameSessionPartition(undefined, row("b", "p1")), false, "no source");

const projects = [{ sessionIds: ["p1a", "p1b", "p1c"] }, { sessionIds: ["p2a", "p2b"] }];
const sessions = {
  p1a: row("p1a", "p1"),
  p1b: row("p1b", "p1", true),
  p1c: row("p1c", "p1"),
  p2a: row("p2a", "p2", true),
  p2b: row("p2b", "p2"),
};
assert.deepEqual(pinnedOrder(projects, sessions, ["p2a", "p1b"]), ["p2a", "p1b"], "declared order wins");
assert.deepEqual(pinnedOrder(projects, sessions, undefined), ["p1b", "p2a"], "no field: the flags stand in, in project order");
assert.deepEqual(pinnedOrder(projects, sessions, ["p2a", "p2a", "gone", "p1b"]), ["p2a", "p1b"], "deduped and kept to visible rows");
assert.deepEqual(pinnedOrder(projects, { p1b: sessions.p1b }, ["p2a", "p1b"]), ["p2a", "p1b"], "declared ids keep members whose details are missing");
assert.deepEqual(pinnedOrder(projects, sessions, []), [], "an explicitly empty list stays empty");

// ---------------------------------------------------------------- the page

const SESSION_MIME = "application/x-wittycode-session";
const PROJECT_MIME = "application/x-wittycode-project";
const page = await openPage(`${pathToFileURL(resolve("dist/index.html")).href}?scene=done`, { width: 1440, height: 900 });
const WIT = '.proj[data-project-id="p-wittycode"]';
const INV = '.proj[data-project-id="p-inventory"]';
const PIN = ".side-pinned";
const witRows = `${WIT} .srow`;
const invRows = `${INV} .srow`;
const pinRows = `${PIN} .srow`;
const titles = (sel) => page.eval(`[...document.querySelectorAll('${sel} .stitle')].map(r => r.textContent)`);
const pnames = () => page.eval(`[...document.querySelectorAll('.proj .pname')].map(r => r.textContent)`);
const waitTitles = (sel, expected) => page.waitFor(`JSON.stringify([...document.querySelectorAll('${sel} .stitle')].map(r => r.textContent)) === ${JSON.stringify(JSON.stringify(expected))}`);
const count = (sel) => page.eval(`document.querySelectorAll('${sel}').length`);
const menu = async (label, pg = page) => {
  const at = await pg.eval(`[...document.querySelectorAll('.menu-item')].findIndex(e => e.textContent.trim() === ${JSON.stringify(label)})`);
  assert.ok(at >= 0, `menu contains ${label}`);
  await pg.click(".menu-item", at);
};
/** Drop `source` at the top edge of `target` (`after`: the bottom edge). `allowed` expects the indicator. */
async function drag(pg, sourceSel, sourceIndex, targetSel, targetIndex, allowed, { mime = SESSION_MIME, after = false } = {}) {
  const source = await pg.rect(sourceSel, sourceIndex); // measure first: the scroll lands the press on the row
  const x = source.x + source.w / 2;
  const y = source.y + source.h / 2;
  const first = pg.events.length;
  await pg.mouse("mouseMoved", x, y, { button: "none", clickCount: 0 });
  await pg.mouse("mousePressed", x, y, { buttons: 1 });
  await pg.mouse("mouseMoved", x + 12, y - 10, { buttons: 1 });
  await pg.mouse("mouseMoved", x + 20, y - 16, { buttons: 1 });
  const intercepted = () => pg.events.slice(first).find((e) => e.method === "Input.dragIntercepted");
  for (let n = 0; n < 60 && !intercepted(); n++) {
    await pg.wait(50);
    if (n === 12) await pg.mouse("mouseMoved", x + 26, y - 20, { buttons: 1 }); // some sources need another nudge
  }
  const event = intercepted();
  assert.ok(event, "the mouse starts a native browser drag");
  const data = event.params.data;
  assert.ok(data.items.some((item) => item.mimeType === mime), `drag carries ${mime}`);
  const target = await pg.rect(targetSel, targetIndex); // fresh after the press: concurrent scrollIntoViews would stale the source
  const at = { x: target.x + target.w / 2, y: after ? target.y + target.h - 2 : target.y + 2, data };
  await pg.send("Input.dispatchDragEvent", { type: "dragEnter", ...at });
  await pg.send("Input.dispatchDragEvent", { type: "dragOver", ...at });
  const indicator = `.${mime === PROJECT_MIME ? "proj" : "srow"}.drop-${after ? "after" : "before"}`;
  if (allowed) await pg.waitFor(`!!document.querySelector('${indicator}')`);
  else assert.equal(await pg.eval("!!document.querySelector('.srow.drop-before, .srow.drop-after, .proj.drop-before, .proj.drop-after')"), false);
  await pg.send("Input.dispatchDragEvent", { type: "drop", ...at });
  await pg.mouse("mouseReleased", at.x, at.y);
  await pg.wait(120);
}

try {
  await page.waitFor(`document.querySelectorAll('${witRows}').length >= 3`);
  await page.send("Input.setInterceptDrags", { enabled: true });
  assert.equal(await count(PIN), 0, "no pinned strip without pinned rows");

  const [first, second, third, ...rest] = await titles(witRows);
  await drag(page, witRows, 1, witRows, 0, true);
  await waitTitles(witRows, [second, first, third, ...rest]);

  // pin by the context menu: the row leaves its project for the global strip, naming its project
  await page.click(witRows, 2, { right: true });
  await menu("置顶");
  await page.waitFor(`document.querySelectorAll('${pinRows}').length === 1`);
  await waitTitles(witRows, [second, first, ...rest]);
  assert.deepEqual(await titles(pinRows), [third]);
  assert.ok(await page.eval(`document.querySelector('${pinRows} .ssub').textContent.includes('wittycode')`), "the strip row names its project");
  assert.ok(
    await page.eval("document.querySelector('.side-top').nextElementSibling.classList.contains('side-pinned') && document.querySelector('.side-pinned').nextElementSibling.classList.contains('side-head')"),
    "the strip sits between 新会话 and 项目",
  );

  // pin by the more button: the new pin goes to the top of the strip, each row once
  await page.click(`${witRows} .more`, 1);
  await menu("置顶");
  await page.waitFor(`document.querySelectorAll('${pinRows}').length === 2`);
  assert.deepEqual(await titles(pinRows), [first, third]);
  assert.deepEqual(await page.eval(`[...document.querySelectorAll('${pinRows} .srow-pin')].map(e => e.getAttribute('aria-label'))`), ["已置顶", "已置顶"]);
  assert.equal(
    await page.eval("new Set([...document.querySelectorAll('.sidebar [data-session-id]')].map(e => e.dataset.sessionId)).size"),
    await count(".sidebar [data-session-id]"),
    "each row id appears once in the sidebar",
  );

  // a row of another project joins the same strip and still names its own project
  const invTitle = (await titles(invRows))[0];
  await page.click(`${invRows} .more`, 0);
  await menu("置顶");
  await page.waitFor(`document.querySelectorAll('${pinRows}').length === 3`);
  assert.deepEqual(await titles(pinRows), [invTitle, first, third]);
  assert.ok(await page.eval(`document.querySelectorAll('${pinRows} .ssub')[0].textContent.includes('inventory-api')`), "the other project's row names its project");

  // pinned rows reorder across projects inside the strip — and only there
  await drag(page, pinRows, 2, pinRows, 0, true);
  await waitTitles(pinRows, [third, invTitle, first]);
  await drag(page, witRows, 0, pinRows, 0, false); // ordinary onto the strip
  await drag(page, pinRows, 0, witRows, 0, false); // the strip onto ordinary
  await drag(page, invRows, 0, witRows, 0, false); // ordinary across projects

  // unpin from the strip: the row goes back to the head of its own project's ordinary rows
  await page.click(`${pinRows} .more`, 0);
  await menu("取消置顶");
  await waitTitles(pinRows, [invTitle, first]);
  assert.deepEqual((await titles(witRows)).slice(0, 2), [third, second]);
  assert.equal(await page.eval(`document.querySelectorAll('${witRows} .srow-pin').length`), 0);

  // project titles drag up and down, before and after; the strip does not follow the projects
  assert.deepEqual(await pnames(), ["wittycode", "inventory-api", "demo"]);
  await drag(page, `${INV} .phead-row`, 0, WIT, 0, true, { mime: PROJECT_MIME });
  assert.deepEqual(await pnames(), ["inventory-api", "wittycode", "demo"], "a project drops before another");
  await drag(page, `${INV} .phead-row`, 0, WIT, 0, true, { mime: PROJECT_MIME, after: true });
  assert.deepEqual(await pnames(), ["wittycode", "inventory-api", "demo"], "a project drops after another");
  assert.equal(await count(`${PIN} .srow`), 2, "the strip is untouched by project reordering");
  assert.ok(await page.eval("!document.querySelector('.side-pinned').className.includes('side-scroll') && document.querySelector('.side-pinned').parentElement === document.querySelector('.sidebar')"), "the strip does not scroll with the projects");

  // a new project lands at the top
  await page.click('[aria-label="添加项目文件夹"]');
  await page.waitFor(`document.querySelector('.proj .pname')?.textContent === 'my-project-1'`);
  assert.deepEqual(await pnames(), ["my-project-1", "wittycode", "inventory-api", "demo"], "a new project lands at the top");

  // a discarded draft admits no row; the first send lands at the head of the ordinary rows
  const newButton = `${WIT} [aria-label="新会话"]`;
  const tabCount = await page.eval("document.querySelectorAll('.tab').length");
  await page.click(newButton);
  await page.eval(`(() => {
    const data = new DataTransfer();
    data.items.add(new File(['fixture'], 'draft.txt', { type: 'text/plain' }));
    document.querySelector('.input').dispatchEvent(new ClipboardEvent('paste', { clipboardData: data, bubbles: true, cancelable: true }));
  })()`);
  await page.waitFor("!!document.querySelector('.tab.current .tab-x') && !!document.querySelector('.att-row .att')");
  await page.click(".input");
  await page.type("editable draft");
  assert.equal(await page.eval("document.querySelector('.input').value"), "editable draft");
  const witBefore = await titles(witRows);
  await page.click(".tab.current .tab-x");
  await page.waitFor(`document.querySelectorAll('.tab').length === ${tabCount}`);
  assert.deepEqual(await titles(witRows), witBefore);
  await page.click(newButton);
  await page.click(".input");
  await page.type("new ordinary row");
  await page.key("Enter");
  await waitTitles(witRows, ["new ordinary row", ...witBefore]);
  assert.deepEqual(page.errorTexts(), []);
  console.log("PASS mock: global pinned strip (once, named projects, cross-project order), partition rules, project drag before/after, new project first, draft discard and first-send admission");
} finally {
  await page.close();
}

if (process.argv.includes("--live")) {
  const { scripts, client } = fakeBackendScripts();
  const live = await openPage(pathToFileURL(resolve("dist/index.html")).href, { init: scripts });
  const liveTitles = (sel) => live.eval(`[...document.querySelectorAll('${sel} .stitle')].map(r => r.textContent)`);
  const savedRow = (id, projectId, title, pinned = false) => ({ id, projectId, title, modified: "刚刚", modifiedAt: Date.now(), engine: "chatgpt", pinned, settings: { model: "", reasoning: "", autonomy: "", mode: "" }, usage: null, context: { used: null, window: 0, model: "" }, workingLabel: "" });
  try {
    const devin = '.srow[data-session-id="dv:th-9"]';
    await live.waitFor(`!!document.querySelector('${devin}')`);
    await live.click(devin, 0, { right: true });
    assert.deepEqual(await live.eval("[...document.querySelectorAll('.menu-label')].map(e => e.textContent)"), ["打开", "置顶", "重命名…", "复制会话 ID", "归档"]);
    await live.click(".menu-item", 1);
    // (this fake backend is an older sender: no pinnedSessionIds — the page derives the strip from the flags)
    await live.waitFor(`!!document.querySelector('.side-pinned ${devin} .srow-pin')`);
    assert.equal(await live.eval(`document.querySelectorAll('.proj ${devin}').length`), 0, "the pinned row left its project");
    assert.equal(await live.eval(`document.querySelectorAll('${devin}').length`), 1, "the row appears once");
    assert.deepEqual(await live.eval("__fake.callsOf('session.pin').at(-1)"), { projectId: "D:\\Work\\demo-project", id: "dv:th-9", pinned: true });

    // a declared pinnedSessionIds drives the strip's order, deduped, across projects
    await live.eval(`__fake.emit("sidebar", ${JSON.stringify({
      projects: [
        { id: "D:\\Work\\demo-project", name: "demo-project", path: "D:\\Work\\demo-project", collapsed: false, sessionIds: ["s-saved-1", "dv:th-9"] },
        { id: "D:\\Work\\second-project", name: "second-project", path: "D:\\Work\\second-project", collapsed: false, sessionIds: ["s-saved-2"] },
      ],
      sessions: {
        "s-saved-1": savedRow("s-saved-1", "D:\\Work\\demo-project", "整理发布说明"),
        "dv:th-9": savedRow("dv:th-9", "D:\\Work\\demo-project", "Devin 会话：迁移脚本", true),
        "s-saved-2": savedRow("s-saved-2", "D:\\Work\\second-project", "旧的会话", true),
      },
      pinnedSessionIds: ["s-saved-2", "dv:th-9"],
    })})`);
    await live.waitFor(`document.querySelectorAll('.side-pinned .srow').length === 2`);
    assert.deepEqual(await liveTitles(".side-pinned .srow"), ["旧的会话", "Devin 会话：迁移脚本"], "the declared order wins");
    assert.equal(await live.eval(`document.querySelectorAll('[data-session-id="s-saved-2"]').length`), 1, "deduped across projects");
    assert.ok(await live.eval(`[...document.querySelectorAll('.side-pinned .ssub')].map(e => e.textContent).join('|').includes('second-project') && [...document.querySelectorAll('.side-pinned .ssub')].map(e => e.textContent).join('|').includes('demo-project')`), "both source projects are named");

    // a pinned row dragged across projects calls session.reorder with the SOURCE project
    await live.send("Input.setInterceptDrags", { enabled: true });
    await drag(live, ".side-pinned .srow", 1, ".side-pinned .srow", 0, true);
    const reorders = await live.eval("__fake.callsOf('session.reorder')");
    assert.deepEqual(reorders.at(-1), { projectId: "D:\\Work\\demo-project", id: "dv:th-9", targetId: "s-saved-2", after: false }, "reorder sends the dragged row's own project");

    // an ordinary row dragged onto the strip is refused: no further call
    await drag(live, ".side-scroll .proj .srow", 0, ".side-pinned .srow", 0, false);
    assert.equal((await live.eval("__fake.callsOf('session.reorder')")).length, reorders.length, "ordinary onto the strip is refused");

    // a project title dragged onto another project calls project.reorder
    await drag(live, ".side-scroll .proj .phead-row", 1, ".side-scroll .proj", 0, true, { mime: PROJECT_MIME });
    assert.deepEqual(await live.eval("__fake.callsOf('project.reorder').at(-1)"), { id: "D:\\Work\\second-project", targetId: "D:\\Work\\demo-project", after: false });

    // unpin from the strip puts the row back into its project
    await live.click(`${devin} .more`);
    assert.equal(await live.eval("document.querySelectorAll('.menu-label')[1].textContent"), "取消置顶");
    await live.click(".menu-item", 1);
    await live.waitFor(`!document.querySelector('.side-pinned ${devin}')`);
    assert.equal(await live.eval("__fake.callsOf('session.pin').at(-1).pinned"), false);
    assert.equal(await live.eval(`document.querySelectorAll('.proj ${devin}').length`), 1, "back into its project, once");

    // a pin declared before its session event: the strip reserves the id and the late row lands in it, not in its project
    await live.eval(`__fake.emit("sidebar", ${JSON.stringify({
      projects: [
        { id: "D:\\Work\\demo-project", name: "demo-project", path: "D:\\Work\\demo-project", collapsed: false, sessionIds: ["c-late", "s-saved-1", "dv:th-9"] },
        { id: "D:\\Work\\second-project", name: "second-project", path: "D:\\Work\\second-project", collapsed: false, sessionIds: ["s-saved-2"] },
      ],
      sessions: {
        "s-saved-1": savedRow("s-saved-1", "D:\\Work\\demo-project", "整理发布说明"),
        "dv:th-9": savedRow("dv:th-9", "D:\\Work\\demo-project", "Devin 会话：迁移脚本"),
        "s-saved-2": savedRow("s-saved-2", "D:\\Work\\second-project", "旧的会话", true),
      },
      pinnedSessionIds: ["c-late", "s-saved-2"],
    })})`);
    await live.wait(200);
    assert.equal(await live.eval(`document.querySelectorAll('[data-session-id="c-late"]').length`), 0, "the reserved id shows nowhere without details");
    assert.equal(await live.eval(`document.querySelectorAll('.side-pinned .srow').length`), 1, "only rows with details render");
    await live.eval(`__fake.emit("tabs", ${JSON.stringify({ tabs: ["c-late"], select: null })})`);
    await live.eval(`__fake.emit("session", ${JSON.stringify({
      id: "c-late",
      patch: { id: "c-late", projectId: "D:\\Work\\demo-project", title: "晚到的会话", mark: "opening", workingLabel: "正在连接…", modified: "刚刚", modifiedAt: Date.now(), engine: "chatgpt", nativeId: "", settings: { model: "", reasoning: "", autonomy: "", mode: "auto" }, usage: null, context: { used: null, window: 0, model: "", compacted: false }, handoff: null, agents: [] },
    })})`);
    await live.waitFor(`!!document.querySelector('.side-pinned [data-session-id="c-late"]')`);
    assert.equal(await live.eval(`document.querySelectorAll('.proj [data-session-id="c-late"]').length`), 0, "lands in the strip, not its project");
    assert.equal(await live.eval(`document.querySelectorAll('[data-session-id="c-late"]').length`), 1, "once");
    assert.equal(await live.eval(`document.querySelector('.side-pinned .srow').dataset.sessionId`), "c-late", "declared order keeps it first");
    assert.deepEqual(live.errorTexts(), []);
    console.log(`PASS live (${client}): pin strip, declared order/dedup, source-project reorder, project.reorder, unpin`);
  } finally {
    await live.close();
  }
}
