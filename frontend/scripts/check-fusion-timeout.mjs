// Run under the project's managed test runner. Build and browse only this run's isolated copy of the page.
import assert from "node:assert/strict";
import { resolve, join } from "node:path";
import { fileURLToPath, pathToFileURL } from "node:url";
import { build } from "vite";
import { openPage } from "./cdp.mjs";

const work = process.env.WITTYCODE_TEST_WORK, evidence = process.env.WITTYCODE_TEST_EVIDENCE;
const root = fileURLToPath(new URL("../", import.meta.url));
let dist = join(root, "dist"); // The regular page suite already built its own page.
if (resolve(process.argv[1]) === fileURLToPath(import.meta.url)) {
  assert(work && evidence, "run with desktop/core/test_runs.py");
  dist = resolve(work, "page");
  await build({ root, cacheDir: join(work, "cache"), envDir: work, build: { outDir: dist } });
}
const page = await openPage(`${pathToFileURL(join(dist, "index.html"))}?settings=fusion&theme=light&review=0`, { width: 1200, height: 920 });
const box = '.fusion-timeout input[type="checkbox"]', number = '.fusion-timeout input[type="number"]', save = ".fusion-timeout button";
const prop = (selector, key) => page.eval(`document.querySelector('${selector}').${key}`);
try {
  await page.waitFor("!!document.querySelector('.fusion-timeout')");
  assert(await prop(box, "checked"));
  assert(await prop(number, "disabled"));
  await page.click(box);
  assert(!await prop(number, "disabled"));
  await page.click(number);
  await page.key("a", { modifiers: 2 });
  await page.type("0");
  assert(await prop(save, "disabled"));
  assert(await page.eval("document.querySelector('.fusion-timeout [role=alert]').textContent.includes('86400')"));
  await page.key("a", { modifiers: 2 });
  await page.type("37");
  await page.click(save);
  assert(await prop(save, "disabled"));
  await page.click(box);
  await page.click(save);
  assert(await prop(number, "disabled"));
  assert.equal(await prop(number, "value"), "37");
  if (evidence) await page.shot(join(evidence, "fusion-timeout-adaptive.png"));
  await page.click(box);
  assert.equal(await prop(number, "value"), "37");
  assert.deepEqual(page.errorTexts(), []);
  console.log("PASS: Adaptive disables the remembered seconds, manual validates and saves, toggling retains 37.");
} finally {
  await page.close();
}
