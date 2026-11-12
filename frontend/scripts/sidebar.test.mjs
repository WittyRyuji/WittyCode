// Native HTML drag/drop in Chromium (not synthetic DOM DragEvents). The real-backend path is in test_app_sidebar_e2e.py.
import assert from "node:assert/strict";
import { mkdirSync } from "node:fs";
import { resolve } from "node:path";
import { pathToFileURL } from "node:url";
import { openPage } from "./cdp.mjs";

const page = await openPage(`${pathToFileURL(resolve("dist/index.html")).href}?scene=done`, { width: 1440, height: 900 });
const titles = () => page.eval("[...document.querySelectorAll('.proj:first-child .srow .stitle')].map(r => r.textContent)");
try {
  await page.waitFor("document.querySelectorAll('.proj:first-child .srow').length >= 2");
  await page.send("Input.setInterceptDrags", { enabled: true });
  const before = await titles();
  const [target, source] = await Promise.all([page.rect(".proj:first-child .srow", 0), page.rect(".proj:first-child .srow", 1)]);
  const x = source.x + source.w / 2;
  const y = source.y + source.h / 2;
  await page.mouse("mouseMoved", x, y, { button: "none", clickCount: 0 });
  await page.mouse("mousePressed", x, y, { buttons: 1 });
  await page.mouse("mouseMoved", x + 12, y - 10, { buttons: 1 });
  for (let n = 0; n < 40 && !page.events.some(e => e.method === "Input.dragIntercepted"); n++) await page.wait(50);
  const event = page.events.find(e => e.method === "Input.dragIntercepted");
  assert.ok(event, "the mouse starts a native browser drag");
  const data = event.params.data;
  assert.ok(data.items.some(item => item.mimeType === "application/x-wittycode-session"));
  const at = { x: target.x + target.w / 2, y: target.y + 2, data };
  await page.send("Input.dispatchDragEvent", { type: "dragEnter", ...at });
  await page.send("Input.dispatchDragEvent", { type: "dragOver", ...at });
  await page.waitFor("!!document.querySelector('.srow.drop-before')");
  mkdirSync(resolve("shots"), { recursive: true });
  await page.shot(resolve("shots/sidebar-drag-before.png"));
  await page.send("Input.dispatchDragEvent", { type: "drop", ...at });
  await page.mouse("mouseReleased", at.x, at.y);
  await page.waitFor(`document.querySelector('.proj:first-child .srow .stitle').textContent === ${JSON.stringify(before[1])}`);
  const after = await titles();
  assert.deepEqual(after, [before[1], before[0], ...before.slice(2)]);
  await page.shot(resolve("shots/sidebar-drag-sorted.png"));
  assert.deepEqual(page.errorTexts(), []);
  console.log("PASS native sidebar drag, insertion indicator, stable row movement, and no browser errors");
} finally {
  await page.close();
}
