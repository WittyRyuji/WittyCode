// Native HTML drag/drop and menus in Chromium (not synthetic DOM DragEvents).
import assert from "node:assert/strict";
import { resolve } from "node:path";
import { pathToFileURL } from "node:url";
import { openPage } from "./cdp.mjs";
import { fakeBackendScripts } from "./fake-backend.mjs";

const page = await openPage(`${pathToFileURL(resolve("dist/index.html")).href}?scene=done`, { width: 1440, height: 900 });
const rows = ".proj:first-child .srow";
const titles = () => page.eval(`[...document.querySelectorAll('${rows} .stitle')].map(r => r.textContent)`);
const waitTitles = (expected) => page.waitFor(`JSON.stringify([...document.querySelectorAll('${rows} .stitle')].map(r => r.textContent)) === ${JSON.stringify(JSON.stringify(expected))}`);
const menu = async (label) => {
  const at = await page.eval(`[...document.querySelectorAll('.menu-item')].findIndex(e => e.textContent.trim() === ${JSON.stringify(label)})`);
  assert.ok(at >= 0, `menu contains ${label}`);
  await page.click(".menu-item", at);
};
async function drag(sourceIndex, targetSelector, targetIndex, allowed) {
  const [source, target] = await Promise.all([page.rect(rows, sourceIndex), page.rect(targetSelector, targetIndex)]);
  const x = source.x + source.w / 2;
  const y = source.y + source.h / 2;
  const first = page.events.length;
  await page.mouse("mouseMoved", x, y, { button: "none", clickCount: 0 });
  await page.mouse("mousePressed", x, y, { buttons: 1 });
  await page.mouse("mouseMoved", x + 12, y - 10, { buttons: 1 });
  const intercepted = () => page.events.slice(first).find(e => e.method === "Input.dragIntercepted");
  for (let n = 0; n < 40 && !intercepted(); n++) await page.wait(50);
  const event = intercepted();
  assert.ok(event, "the mouse starts a native browser drag");
  const data = event.params.data;
  assert.ok(data.items.some(item => item.mimeType === "application/x-wittycode-session"));
  const at = { x: target.x + target.w / 2, y: target.y + 2, data };
  await page.send("Input.dispatchDragEvent", { type: "dragEnter", ...at });
  await page.send("Input.dispatchDragEvent", { type: "dragOver", ...at });
  if (allowed) await page.waitFor("!!document.querySelector('.srow.drop-before')");
  else assert.equal(await page.eval("!!document.querySelector('.srow.drop-before, .srow.drop-after')"), false);
  await page.send("Input.dispatchDragEvent", { type: "drop", ...at });
  await page.mouse("mouseReleased", at.x, at.y);
}

try {
  await page.waitFor(`document.querySelectorAll('${rows}').length >= 3`);
  await page.send("Input.setInterceptDrags", { enabled: true });
  const [first, second, third, ...rest] = await titles();
  await drag(1, rows, 0, true);
  await waitTitles([second, first, third, ...rest]);

  await page.click(rows, 2, { right: true });
  await menu("置顶");
  await waitTitles([third, second, first, ...rest]);
  await page.click(`${rows} .more`, 2);
  await menu("置顶");
  await waitTitles([first, third, second, ...rest]);
  assert.deepEqual(await page.eval(`[...document.querySelectorAll('${rows} .srow-pin')].map(e => e.getAttribute('aria-label'))`), ["已置顶", "已置顶"]);
  await drag(1, rows, 0, true);
  await waitTitles([third, first, second, ...rest]);

  await drag(2, rows, 0, false);
  const other = ".proj:nth-child(2) .srow";
  await page.waitFor(`!!document.querySelector('${other}')`);
  await drag(2, other, 0, false);
  assert.deepEqual(await titles(), [third, first, second, ...rest]);
  await page.click(`${rows} .more`, 1);
  await menu("取消置顶");
  await waitTitles([third, first, second, ...rest]);
  assert.equal(await page.eval(`document.querySelectorAll('${rows} .srow-pin').length`), 1);

  const newButton = '.proj:first-child [aria-label="新会话"]';
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
  assert.deepEqual(await titles(), [third, first, second, ...rest]);
  await page.click(".tab.current .tab-x");
  await page.waitFor(`document.querySelectorAll('.tab').length === ${tabCount}`);
  assert.deepEqual(await titles(), [third, first, second, ...rest]);
  await page.click(newButton);
  await page.click(".input");
  await page.type("new ordinary row");
  await page.key("Enter");
  await waitTitles([third, "new ordinary row", first, second, ...rest]);
  assert.deepEqual(page.errorTexts(), []);
  console.log("PASS mock: both pin menus, badges, native dragging, draft discard and first-send admission below the pins");
} finally {
  await page.close();
}

if (process.argv.includes("--live")) {
  const { scripts, client } = fakeBackendScripts();
  const live = await openPage(pathToFileURL(resolve("dist/index.html")).href, { init: scripts });
  try {
    const devin = '.srow[data-session-id="dv:th-9"]';
    await live.waitFor(`!!document.querySelector('${devin}')`);
    await live.click(devin, 0, { right: true });
    assert.deepEqual(await live.eval("[...document.querySelectorAll('.menu-label')].map(e => e.textContent)"), ["打开", "置顶", "重命名…", "复制会话 ID", "归档"]);
    await live.click(".menu-item", 1);
    await live.waitFor(`!!document.querySelector('${devin}:first-child .srow-pin')`);
    assert.deepEqual(await live.eval("__fake.callsOf('session.pin').at(-1)"), { projectId: "D:\\Work\\demo-project", id: "dv:th-9", pinned: true });
    await live.eval(`__fake.failNext('session.send', '第一条消息未发出')`);
    await live.click(".newbtn");
    await live.click(".input");
    await live.type("quick: draft");
    await live.key("Enter");
    await live.waitFor("!!document.querySelector('.tab.current') && document.querySelector('.notice-text')?.textContent.includes('第一条消息未发出')");
    assert.equal(await live.eval("document.querySelectorAll('.srow.open').length"), 0);
    assert.equal(await live.eval("document.querySelector('.input').value"), "quick: draft");
    await live.click(".input");
    await live.key("End");
    await live.type(" edited");
    assert.equal(await live.eval("document.querySelector('.input').value"), "quick: draft edited");
    await live.key("Enter");
    await live.waitFor("!!document.querySelector('.srow.open.current') && document.querySelector('.transcript').textContent.includes('quick: draft edited')");
    assert.equal(await live.eval("document.querySelector('.proj:first-child .srow').dataset.sessionId"), "dv:th-9");
    await live.click(`${devin} .more`);
    assert.equal(await live.eval("document.querySelectorAll('.menu-label')[1].textContent"), "取消置顶");
    await live.click(".menu-item", 1);
    await live.waitFor(`!document.querySelector('${devin} .srow-pin')`);
    assert.equal(await live.eval("__fake.callsOf('session.pin').at(-1).pinned"), false);
    assert.deepEqual(live.errorTexts(), []);
    console.log(`PASS live (${client}): pin, rejected draft, editable retry and unpin`);
  } finally {
    await live.close();
  }
}
