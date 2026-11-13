import assert from "node:assert/strict";
import { resolve } from "node:path";
import { pathToFileURL } from "node:url";
import { openPage } from "./cdp.mjs";
import { fakeBackendScripts } from "./fake-backend.mjs";

const index = pathToFileURL(resolve("dist/index.html")).href;
for (const live of [false, true]) for (const theme of ["light", "dark"]) {
  const connectionErrors = [
    { source: "C:/Users/me/.codex/config.toml", text: "这个远程 MCP 尚不支持，未启用。" },
    { source: "C:/Users/me/.codex/config.toml", text: "这个 Hook 类型尚不支持，未启用。" },
  ];
  const init = live ? fakeBackendScripts({ initDelay: 0, accountDelay: 0, connectionErrors }).scripts : [];
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
    if (live) {
      assert(await page.eval("document.querySelector('.connection-issues summary').textContent.includes('2') && !document.querySelector('.connection-issues').open && !document.querySelector('.connection-error')"), "compatibility notes are visible as a collapsed summary, not a wall of operation errors");
      await page.click('.connection-issues summary');
      assert.equal(await page.eval("document.querySelectorAll('.connection-issue').length"), 2, "distinct notes from one source have distinct React keys");
      await page.click('.connection-issues summary');
    }
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
    if (live) {
      await page.eval("__fake.failNext('connections.test', '找不到本地启动程序，请检查依赖。')");
      await clickText('.connection-row .btn', '测试连接');
      await page.waitFor("document.querySelector('.connection-error[role=alert]')?.textContent.includes('找不到')", 3000);
      assert(!(await page.eval("!!document.querySelector('.connection-result')")), "a failed connection test clears the old success");
      await clickText('.connection-row .btn', '测试连接');
      await page.waitFor("!!document.querySelector('.connection-result') && !document.querySelector('.connection-error[role=alert]')", 3000);
    }
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

for (const theme of ["light", "dark"]) {
  const connectionItems = ["Zeta", "alpha", "Beta"].map((name) => ({ id: `order-${name}`, kind: "skill", name, source: `C:/Users/me/.agents/skills/${name}/SKILL.md`, project: "", config: { path: `C:/Users/me/.agents/skills/${name}/SKILL.md` }, enabled: true, imported: false }));
  const { scripts } = fakeBackendScripts({ initDelay: 0, accountDelay: 0, connectionItems });
  const page = await openPage(`${index}?live=1&theme=${theme}`, { init: scripts, width: theme === "light" ? 1500 : 900, height: 950 });
  const clickText = async (selector, text) => page.click(selector, await page.eval(`[...document.querySelectorAll(${JSON.stringify(selector)})].findIndex(e => e.textContent.trim() === ${JSON.stringify(text)})`));
  try {
    await page.waitFor("!!document.querySelector('.sidebar')", 6000);
    await page.key(",", { code: "Comma", vk: 188, modifiers: 2 });
    await clickText(".snav", "外观");
    await clickText(".segc button", theme === "light" ? "浅色" : "深色");
    await page.waitFor(`document.documentElement.dataset.theme === '${theme}'`, 3000);
    await clickText(".snav", "Connections");
    await page.waitFor("document.querySelectorAll('.connection-row').length === 3", 6000);
    assert.deepEqual(await page.eval("[...document.querySelectorAll('.connection-info b')].map(e => e.textContent)"), ["alpha", "Beta", "Zeta"], "sorting must not depend on discovery order or case");
    await page.eval("window.__orderRows = [...document.querySelectorAll('.connection-row')]; true");
    for (const enabled of [false, true, false, true]) {
      await page.click('.connection-row .switch', 1);
      await page.waitFor(`!document.querySelector('.connection-row .switch').disabled && document.querySelectorAll('.connection-row .switch')[1].getAttribute('aria-checked') === '${enabled}'`, 3000);
      assert(await page.eval("document.querySelectorAll('.connection-row').length === 3 && __orderRows.every((e, i) => e === document.querySelectorAll('.connection-row')[i])"), "switches keep the same DOM rows in the same order");
      assert.equal(await page.eval("document.querySelector('.connection-tabs .num').textContent"), "3");
    }
    await clickText(".snav", "外观");
    assert.deepEqual(await page.eval("[...document.querySelectorAll('.settings-scroll .field-label')].map(e => e.textContent)"), ["主题", "文字大小"]);
    assert(await page.eval("!document.querySelector('.preview, .preview-k') && !document.querySelector('.settings-scroll').textContent.includes('session_manager.py')"));
    await clickText('.settings-scroll .segc button', '大');
    await page.waitFor("[...document.querySelectorAll('.segc button[aria-pressed=true]')].some(e => e.textContent === '大')", 3000);
    await page.shot(resolve(`shots/appearance-clean-${theme}.png`));
    await clickText(".snav", "关于");
    assert.equal(await page.eval("document.querySelector('.about .kvs b').textContent"), "0.2.1");
    assert.equal(page.consoleErrors().length, 0);
    assert.equal(page.cspViolations().length, 0);
    console.log(`PASS stable alphabetical connection rows, repeated switches, clean appearance and version: ${theme}`);
  } finally { await page.close(); }
}
