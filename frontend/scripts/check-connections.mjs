import assert from "node:assert/strict";
import { resolve } from "node:path";
import { pathToFileURL } from "node:url";
import { openPage } from "./cdp.mjs";
import { fakeBackendScripts } from "./fake-backend.mjs";

const index = pathToFileURL(resolve("dist/index.html")).href;
for (const live of [false, true]) for (const theme of ["light", "dark"]) {
  const init = live ? fakeBackendScripts({ initDelay: 0, accountDelay: 0 }).scripts : [];
  const page = await openPage(`${index}?scene=done&settings=connections&theme=${theme}&review=0`, { init, width: theme === "light" ? 1500 : 900, height: 950 });
  const clickText = async (selector, text) => {
    const at = await page.eval(`[...document.querySelectorAll(${JSON.stringify(selector)})].findIndex(e => e.textContent.includes(${JSON.stringify(text)}))`);
    assert(at >= 0, `missing ${selector}: ${text}`);
    await page.click(selector, at);
  };
  const fill = (selector, value) => page.eval(`(() => { const e = document.querySelector(${JSON.stringify(selector)}); const p = e instanceof HTMLTextAreaElement ? HTMLTextAreaElement.prototype : HTMLInputElement.prototype; Object.getOwnPropertyDescriptor(p, 'value').set.call(e, ${JSON.stringify(value)}); e.dispatchEvent(new Event('input', { bubbles: true })); })()`);
  try {
    if (live) {
      await page.waitFor("!!document.querySelector('.sidebar')", 6000);
      await page.key(",", { code: "Comma", vk: 188, modifiers: 2 });
      await clickText(".snav", "Connections");
    }
    await page.waitFor("document.querySelectorAll('.connection-tabs [role=tab]').length === 4 && !!document.querySelector('.connection-row')", 6000);
    assert.deepEqual(await page.eval("[...document.querySelectorAll('.connection-tabs button')].map(e => e.textContent.replace(/[0-9]/g, '').trim())"), ["Skills", "Plugins", "Hooks", "MCP"]);
    assert.equal(await page.eval("[...document.querySelectorAll('.snav')].at(-1).textContent.trim()"), "关于", "About must remain the final settings page");
    await page.click(".connection-row .switch");
    await page.waitFor("document.querySelector('.connection-row .switch').getAttribute('aria-checked') === 'false'", 3000);
    await page.click(".connection-row .switch");
    await page.waitFor("document.querySelector('.connection-row .switch').getAttribute('aria-checked') === 'true'", 3000);
    await clickText(".connection-tabs button", "MCP");
    await clickText(".connection-row .btn", "导入");
    await page.waitFor("!!document.querySelector('.connection-preview')", 3000);
    assert(await page.eval("document.querySelector('.connection-preview .primary').disabled"));
    assert(await page.eval("document.querySelector('.connection-preview pre').textContent.includes('python') && document.querySelector('.connection-preview pre').textContent.includes('demo_mcp')"));
    if (live) assert.equal(await page.eval("__fake.callsOf('connections.import').length"), 0);
    await page.shot(resolve(`shots/connections-${live ? "live" : "design"}-${theme}-consent.png`));
    await page.click(".connection-preview input[type=checkbox]");
    await page.click(".connection-preview .primary");
    await page.waitFor("!document.querySelector('.connection-preview') && document.querySelector('.connection-row .switch').getAttribute('aria-checked') === 'true'", 3000);
    if (live) assert.deepEqual(await page.eval("__fake.callsOf('connections.import').at(-1)"), { preview: "local-preview", confirmed: true });
    await clickText(".connection-row .btn", "测试连接");
    await page.waitFor("document.querySelector('.connection-result')?.textContent.includes('连接成功')", 3000);
    await clickText(".connection-row .btn", "编辑");
    await page.waitFor("!!document.querySelector('.connection-editor textarea')", 3000);
    assert(await page.eval("document.querySelector('.connection-editor .primary').disabled"));
    await fill(".connection-editor textarea", "not json");
    await page.click(".connection-editor input[type=checkbox]");
    await page.click(".connection-editor .primary");
    await page.waitFor("!!document.querySelector('[role=alert]')", 2000);
    await fill(".connection-editor textarea", JSON.stringify({ command: "python", args: ["-m", "demo_mcp", "--compact"] }, null, 2));
    await page.click(".connection-editor .primary");
    await page.waitFor("!document.querySelector('.connection-editor')", 3000);
    await page.click(".connection-row .switch");
    await page.waitFor("document.querySelector('.connection-row .switch').getAttribute('aria-checked') === 'false'", 3000);
    await page.click(".connection-row .switch");
    await page.waitFor("document.querySelector('.connection-row .switch').getAttribute('aria-checked') === 'true'", 3000);
    assert(!(await page.eval("!!document.querySelector('.connection-preview, .connection-editor')")), "unchanged imports retain their trust across the switch");
    assert(await page.eval("[...document.querySelectorAll('.connection-toolbar, .connection-import, .connection-row')].every(e => e.scrollWidth <= e.clientWidth + 1)"));
    await page.shot(resolve(`shots/connections-${live ? "live" : "design"}-${theme}.png`));
    await clickText(".snav", "账号与模型");
    await page.waitFor("!!document.querySelector('.factory-card') && !document.querySelector('#factory-path').disabled", 3000);
    assert(await page.eval("document.querySelector('.factory-card').textContent.includes('Factory CLI（可选）') && document.querySelector('.factory-card').textContent.includes('不依赖 Factory SDK')"));
    await fill("#factory-path", "droid.exe");
    await clickText(".factory-card .btn", "保存路径");
    await page.waitFor("!document.querySelector('.factory-card .btn').disabled", 3000);
    await page.click('.factory-card .switch[aria-label="启用 Factory CLI"]');
    await page.waitFor("document.querySelector('.factory-card .switch').getAttribute('aria-checked') === 'true'", 3000);
    if (live) assert.deepEqual(await page.eval("__fake.callsOf('factory.configure').at(-1)"), { path: "droid.exe", enabled: true });
    assert.equal(page.consoleErrors().length, 0);
    assert.equal(page.cspViolations().length, 0);
    console.log(`PASS Connections and optional Factory UI: ${live ? "real QWebChannel" : "design"}, ${theme}, consent, edit, MCP test and switches`);
  } finally { await page.close(); }
}
