// The structured plan sits beside the conversation, not inside the thinking log. No model or user settings are involved.
import assert from "node:assert/strict";
import { mkdirSync } from "node:fs";
import { resolve } from "node:path";
import { pathToFileURL } from "node:url";
import { openPage } from "./cdp.mjs";

const index = pathToFileURL(resolve("dist/index.html")).href;
mkdirSync(resolve("shots"), { recursive: true });
for (const [theme, width] of [["light", 1500], ["dark", 1500], ["light", 1100], ["light", 900], ["dark", 900]]) {
  const page = await openPage(`${index}?scene=live&theme=${theme}&review=0&expand=all`, { width, hideScrollbars: false });
  try {
    await page.waitFor("document.querySelectorAll('.plan-step').length === 3", 3000);
    assert.equal(await page.eval("document.querySelector('.plan-count').textContent"), "1/3");
    assert(await page.eval("['.planpanel', '.plan-toggle', '.plan-head', '.plan-meter'].every(s => parseFloat(getComputedStyle(document.querySelector(s)).borderRadius) === 0)"), "the plan retains square controls and a square drawer");
    assert(await page.eval("(() => { const s = getComputedStyle(document.querySelector('.planpanel')); return s.position !== 'absolute' || (s.borderTopWidth === '2px' && s.boxShadow.includes('4px 4px 0px')); })()"), "the narrow plan drawer uses the original outline and offset shadow");
    const aligned = "(() => { const t = document.querySelector('.transcript').getBoundingClientRect(); const c = document.querySelector('.composer').getBoundingClientRect(); return Math.abs(t.left - c.left) < 1 && Math.abs(t.right - c.right) < 1 && document.documentElement.scrollWidth <= innerWidth; })()";
    assert(await page.eval(aligned), "messages and composer share both edges, including the real scrollbar gutter");
    assert(await page.eval("(() => { const p = document.querySelector('.planpanel').getBoundingClientRect(); const t = document.querySelector('.stage').getBoundingClientRect(); const c = document.querySelector('.composer').getBoundingClientRect(); const narrow = getComputedStyle(document.querySelector('.planpanel')).position === 'absolute'; return narrow ? p.top >= t.top && p.bottom < c.top && p.right <= t.right : p.left >= t.right - 1; })()"), "the plan is a separate column on wide windows and a bounded drawer on narrow ones");
    assert(await page.eval("document.querySelectorAll('.plan-step.st-completed').length === 1 && document.querySelectorAll('.plan-step.st-in_progress').length === 1 && document.querySelectorAll('.plan-step.st-pending').length === 1"));
    assert(await page.eval("!!document.querySelector('.plan-record') && !document.querySelector('.plan-record').open && document.querySelector('.plan-record .todos').closest('details') === document.querySelector('.plan-record')"), "historical plan data is available but not laid out inside thinking by default");
    await page.shot(resolve(`shots/plan-panel-${theme}-${width}.png`));
    await page.click(".plan-head");
    assert(await page.eval("document.querySelector('.plan-head').getAttribute('aria-expanded') === 'false' && !document.querySelector('.plan-steps')"));
    await page.click(".plan-head");
    await page.click(".plan-close");
    assert(await page.eval("!document.querySelector('.planpanel') && document.querySelector('.plan-toggle').getAttribute('aria-expanded') === 'false'"), "close removes the entire rail, not just its steps");
    assert(await page.eval(aligned), "closing the plan keeps both reading edges aligned");
    await page.click(".tab", 0);
    assert(await page.eval("!document.querySelector('.planpanel, .plan-toggle') && document.querySelectorAll('.main .transcript').length === 1"), "a conversation with no steps reserves no plan column and does not retain the previous transcript");
    await page.click(".tab", 1);
    assert(await page.eval("!document.querySelector('.planpanel') && !!document.querySelector('.plan-toggle')"), "a dismissed plan stays dismissed after switching tabs");
    await page.click(".plan-toggle");
    await page.waitFor("document.querySelectorAll('.plan-step').length === 3", 1000);
    assert(await page.eval(aligned));
    assert(await page.eval("document.querySelectorAll('.main .transcript').length === 1"), "returning to the plan conversation never duplicates its transcript");
    assert.equal(page.consoleErrors().length, 0);
    assert.equal(page.cspViolations().length, 0);
    console.log(`PASS plan panel: ${theme}, ${width}px, conditional layout, progress, collapse, tab isolation`);
  } finally {
    await page.close();
  }
}
const page = await openPage(`${index}?scene=live&theme=light&review=0&file=hello.txt`);
try {
  await page.waitFor("!!document.querySelector('.filepanel')", 3000);
  assert(await page.eval("!document.querySelector('.planpanel')"), "the existing file inspector has priority over the plan rail");
  await page.click("[aria-label='关闭文件面板']");
  await page.waitFor("document.querySelectorAll('.plan-step').length === 3", 1000);
  assert.equal(page.consoleErrors().length, 0);
  console.log("PASS closing the file inspector restores the same conversation's plan");
} finally {
  await page.close();
}
