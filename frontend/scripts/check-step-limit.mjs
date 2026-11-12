import assert from "node:assert/strict";
import { mkdirSync } from "node:fs";
import { resolve } from "node:path";
import { pathToFileURL } from "node:url";
import { openPage } from "./cdp.mjs";

const index = pathToFileURL(resolve("dist/index.html")).href;
mkdirSync(resolve("shots"), { recursive: true });
for (const theme of ["light", "dark"]) {
  const page = await openPage(`${index}?scene=done&settings=sessions&theme=${theme}&review=0`, { width: 1100 });
  const value = () => page.eval("document.querySelector('.step-limit-setting input').value");
  const fill = (text) => page.eval(`(() => { const e = document.querySelector('.step-limit-setting input'); Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, 'value').set.call(e, ${JSON.stringify(text)}); e.dispatchEvent(new Event('input', { bubbles: true })); })()`);
  try {
    await page.waitFor("!!document.querySelector('.step-limit-setting')", 3000);
    assert.equal(await value(), "80");
    await fill("0");
    await page.waitFor("document.querySelector('.step-limit-setting input').getAttribute('aria-invalid') === 'true'", 1000);
    assert(await page.eval("document.querySelector('.step-limit-setting .btn').disabled"));
    await fill("240");
    await page.waitFor("!document.querySelector('.step-limit-setting .btn').disabled", 1000);
    await page.eval("document.querySelector('.step-limit-setting').scrollIntoView({ block: 'center' })");
    await page.click(".step-limit-setting .btn");
    await page.waitFor("document.querySelector('.step-limit-setting .btn').disabled", 1000);
    await page.click(".snav", 0);
    await page.click(".snav", 3);
    assert.equal(await value(), "240");
    await page.shot(resolve(`shots/step-limit-${theme}.png`));
    assert.equal(page.consoleErrors().length, 0);
    console.log(`PASS step limit settings: ${theme}, default 80, validation, explicit save, larger value retained`);
  } finally {
    await page.close();
  }
}
