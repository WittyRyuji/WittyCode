// Behaviour check of the design page with real mouse / keyboard events: `node scripts/check.mjs` (after `npm run build`).
// The live app (Python behind QWebChannel) has its own check: scripts/check-live.mjs.
import { mkdirSync, readFileSync } from "node:fs";
import { pathToFileURL } from "node:url";
import { resolve } from "node:path";
import { openPage } from "./cdp.mjs";
import { EXPECTED_CSP, cspComesFirst, declaredCsp, externalReferences } from "./selfcontained.mjs";

const index = pathToFileURL(resolve("dist/index.html")).href;
let failed = 0;
const ok = (name, v) => {
  if (!v) failed++;
  console.log(v ? "PASS" : "FAIL", name);
};
const READY = "document.querySelector('.statusbar .st-left').textContent.includes('就绪')";

// the built file itself: it must open from disk inside Qt WebEngine with nothing else around it
const built = readFileSync(resolve("dist/index.html"), "utf8");
ok("the built page refers to nothing outside itself (src / href / url() are data: only)", externalReferences(built).length === 0);
ok("the built page carries its Content-Security-Policy, ahead of every script and style", declaredCsp(built) === EXPECTED_CSP && cspComesFirst(built));

let page = await openPage(`${index}?scene=ask&theme=light&review=0`);

/** Every element the selector matches is on a line of its own: each one below the one before it, all starting at the same left edge. */
const oneLineEach = (selector) =>
  `(() => { const r = [...document.querySelectorAll(${JSON.stringify(selector)})].map(e => e.getBoundingClientRect()); return r.length > 1 && r.every((b, i) => i === 0 || (b.top >= r[i - 1].bottom - 1 && Math.abs(b.left - r[0].left) < 1)); })()`;

// question card: the options one per line; submitting with nothing chosen is refused, then answer and submit
ok("a question's options are one per line", await page.eval(oneLineEach(".prompt.ask .opt")));
await page.click(".prompt.ask .btn.primary");
ok("empty submit is refused", await page.eval("document.querySelectorAll('.q.missing').length === 2"));
await page.click(".prompt.ask .opt input[type=radio]", 1);
await page.click(".prompt.ask .opt input[type=checkbox]", 0);
await page.click(".prompt.ask .opt input[type=checkbox]", 2);
await page.click(".prompt.ask .btn.primary");
await page.wait(300);
ok("answers become a Q&A block", await page.eval("document.querySelector('.block.qa .qa-a')?.textContent.includes('整个界面都用 React')"));
ok("multi-select answer lists both choices", await page.eval("[...document.querySelectorAll('.block.qa .qa-a')].some(e => e.textContent.includes('附件') && e.textContent.includes('会话恢复'))"));
await page.waitFor(READY, 6000);
ok("session goes back to ready", true);

// sessions: open a saved one from the sidebar, close a running one with confirmation
await page.click(".srow:not(.open)", 0);
await page.waitFor("[...document.querySelectorAll('.tab')].some(t => t.textContent.includes('修复点击闪烁'))", 4000);
ok("a saved session opens a tab", true);
await page.waitFor(READY, 4000);
ok("it connects and becomes ready", true);
await page.click(".tab", 1);
await page.click(".tab.current .tab-x");
ok("closing a running session asks first", await page.eval("!!document.querySelector('.dialog')"));
await page.key("Escape");
ok("Esc cancels the dialog", await page.eval("!document.querySelector('.dialog')"));
await page.click(".tab.current .tab-x");
await page.click(".dialog .danger-solid");
ok("confirming closes the tab", await page.eval("![...document.querySelectorAll('.tab')].some(t => t.textContent.includes('重构'))"));

// project menu (right click)
await page.click(".phead-row", 0, { right: true });
ok("project context menu has 4 entries", await page.eval("document.querySelectorAll('.menu-item').length === 4"));
await page.key("Escape");

// reconnect a dead session
await page.click(".srow.m-dead");
await page.click(".block.error .btn");
await page.waitFor(READY, 4000);
ok("a dead session reconnects", true);

// 新会话 from the sidebar button shows the first message box; nothing is opened until a message is sent from it
const tabsAtStart = await page.eval("document.querySelectorAll('.tab').length");
await page.click(".newbtn");
await page.waitFor("document.querySelector('.emptypage')", 3000);
ok("新会话 shows the first message box: no tab was added and none is current", await page.eval(`document.querySelectorAll('.tab').length === ${tabsAtStart} && !document.querySelector('.tab.current')`));
ok("it can be typed in at once, with the project chosen on the line under the box", await page.eval("!document.querySelector('.input').disabled && [...document.querySelectorAll('.controls .chip-k')].map(e => e.textContent).join(',') === '模式' && document.querySelector('.hint .chip-k').textContent === '项目'"));
ok("and the send button waits for a message", await page.eval("document.querySelector('.send').disabled"));
await page.click(".chip-suggest", 0);
ok("a suggestion fills the message box", await page.eval("document.querySelector('.input').value.length > 0 && !document.querySelector('.send').disabled"));
await page.click(".input");
await page.key("Enter");
await page.waitFor(`document.querySelectorAll('.tab').length === ${tabsAtStart + 1} && !!document.querySelector('.tab.current') && document.querySelectorAll('.block.user').length === 1`, 5000);
ok("sending it opens the conversation: a tab, and the message in it", true);
await page.click(".newbtn");
ok("and the first message box is empty again for the next one", await page.eval("document.querySelector('.input').value === ''"));

// commands typed with a slash: the menu lists them, one that cannot run says why and keeps the text, /plan flips the mode
const setDraft = (value) =>
  page.eval(`(() => { const e = document.querySelector('.input'); Object.getOwnPropertyDescriptor(HTMLTextAreaElement.prototype, 'value').set.call(e, ${JSON.stringify(value)}); e.dispatchEvent(new Event('input', { bubbles: true })); })()`);
const modeChip = "document.querySelector('.chip-mode .chip-v')?.textContent";
await page.click(".input");
await page.type("/");
ok("a slash opens the command menu: the page's own, then the user's skills and commands", await page.eval("[...document.querySelectorAll('.slash-item .sl-name')].map(e => e.textContent).join(',') === '/compact,/plan,/goal,/summarize-diff,/code-review'"));
ok("a line sets the page's own commands apart from the user's skills and commands (and /new is not among them)", await page.eval("(() => { const kids = [...document.querySelector('.slash-menu').children]; const at = kids.findIndex((e) => e.classList.contains('slash-sep')); return document.querySelectorAll('.slash-sep').length === 1 && at === 3 && kids.slice(0, 3).every((e) => e.classList.contains('slash-item') && !e.classList.contains('skill') && !e.classList.contains('command')) && kids.slice(4).every((e) => e.classList.contains('slash-item') && (e.classList.contains('skill') || e.classList.contains('command'))) && kids.length === 6; })()"));
ok("a skill and a command of the user's say what they are, and the command what it takes", await page.eval("(() => { const s = [...document.querySelectorAll('.slash-item')]; return s[3].querySelector('.sl-kind').textContent === '技能' && s[4].querySelector('.sl-kind').textContent === '命令' && s[4].querySelector('.sl-args').textContent === '<分支>' && !s[3].classList.contains('off') && !s[0].querySelector('.sl-kind'); })()"));
ok("the first one is lit, and /goal shows what it takes", await page.eval("document.querySelector('.slash-item.on .sl-name').textContent === '/compact' && document.querySelectorAll('.slash-item')[2]?.querySelector('.sl-args')?.textContent === '<目标>'"));
await page.key("ArrowDown");
ok("the arrow keys move the light", await page.eval("document.querySelector('.slash-item.on .sl-name').textContent === '/plan'"));
await page.type("p");
ok("the menu narrows to what is typed", await page.eval("[...document.querySelectorAll('.slash-item .sl-name')].map(e => e.textContent).join(',') === '/plan'"));
await setDraft("/co");
ok("a command that cannot run now says why in its own row", await page.eval("document.querySelector('.slash-item.off .sl-hint')?.textContent.includes('先打开一个会话')"));
ok("and a skill's name is found by its start too", await page.eval("[...document.querySelectorAll('.slash-item .sl-name')].map(e => e.textContent).join(',') === '/compact,/code-review'"));
await page.key("Escape");
ok("Esc closes the menu and leaves what was typed", await page.eval("!document.querySelector('.slash-menu') && document.querySelector('.input').value === '/co'"));
await setDraft("/usr/bin/env");
ok("a path is no command: no menu", await page.eval("!document.querySelector('.slash-menu')"));
await setDraft("/goal fix the tests");
ok("/goal with its text lists no menu", await page.eval("!document.querySelector('.slash-menu')"));
await page.key("Enter");
ok("it cannot run without a conversation: the reason is said and the text is kept", await page.eval("document.querySelector('.input').value === '/goal fix the tests' && document.querySelector('.notice-text')?.textContent.includes('先打开一个会话')"));
await setDraft("/summ");
await page.key("Enter");
ok("Enter on a skill only writes its name and waits for the rest of the message", await page.eval("document.querySelector('.input').value === '/summarize-diff ' && !document.querySelector('.slash-menu')"));
await setDraft("/pl");
await page.key("Enter");
ok("Enter on the lit command runs it: the box is empty and the mode is Plan", await page.eval(`document.querySelector('.input').value === '' && ${modeChip} === 'Plan' && document.querySelector('.notice-text')?.textContent.includes('已切到 Plan')`));
await setDraft("/plan");
await page.click(".send");
ok("the send button runs a command too: back to Auto", await page.eval(`document.querySelector('.input').value === '' && ${modeChip} === 'Auto'`));
ok("and no conversation was started by any of this", await page.eval(`document.querySelectorAll('.tab').length === ${tabsAtStart + 1}`));

// a session's menu: rename and archive (the saved row is only hidden; the archived list brings it back)
const pickMenu = async (text) => {
  const i = await page.eval(`[...document.querySelectorAll('.menu-item')].findIndex(e => e.textContent.includes(${JSON.stringify(text)}))`);
  if (i < 0) throw new Error(`no menu item "${text}"`);
  await page.click(".menu-item", i);
};
const title0 = await page.eval("document.querySelector('.srow:not(.open) .stitle').textContent");
const rows0 = await page.eval("document.querySelectorAll('.srow').length");
await page.click(".srow:not(.open)", 0, { right: true });
ok("a session's menu offers 重命名… and 归档", await page.eval("(() => { const m = [...document.querySelectorAll('.menu-label')].map(e => e.textContent); return m.includes('重命名…') && m.includes('归档'); })()"));
await pickMenu("重命名…");
await page.waitFor("!!document.querySelector('.dialog input')", 2000);
ok("重命名… asks for the name, with the present one filled in", await page.eval(`document.querySelector('.dialog input').value === ${JSON.stringify(title0)}`));
await page.eval("document.querySelector('.dialog input').select()");
await page.type("改过的名字");
await page.key("Enter");
await page.waitFor("[...document.querySelectorAll('.srow .stitle')].some(e => e.textContent === '改过的名字')", 2000);
ok("the new name is shown in the list, and the dialog is gone", await page.eval("!document.querySelector('.dialog')"));
await page.click(".srow:not(.open)", 0, { right: true });
await pickMenu("归档");
await page.waitFor(`document.querySelectorAll('.srow').length === ${rows0 - 1}`, 2000);
ok("归档 takes the row out of its project and adds 已归档 · 1 at the bottom", await page.eval("document.querySelector('.archived .pname')?.textContent === '已归档 · 1'"));
await page.click(".archived .proj-toggle");
ok("the archived list shows it by its new name, with a button to bring it back", await page.eval("document.querySelector('.archived-row .stitle')?.textContent === '改过的名字' && !!document.querySelector('.archived-row [aria-label=\"取消归档\"]')"));
await page.click(".archived-row .more");
await page.waitFor(`document.querySelectorAll('.srow').length === ${rows0}`, 2000);
ok("取消归档 puts the row back and the archived list is gone", await page.eval("!document.querySelector('.archived')"));
ok("nothing was blocked by the Content-Security-Policy", page.cspViolations().length === 0);
await page.close();

const clickText = async (selector, text) => {
  const i = await page.eval(`[...document.querySelectorAll(${JSON.stringify(selector)})].findIndex(e => e.textContent.includes(${JSON.stringify(text)}))`);
  if (i < 0) throw new Error(`no ${selector} containing "${text}"`);
  await page.click(selector, i);
};
page = await openPage(`${index}?scene=done&theme=light`);
await page.click(".seg button", 1);
ok("dark theme on click", await page.eval("document.documentElement.dataset.theme === 'dark'"));

// the composer controls: labels, per-model reasoning levels, Plan instead of Spec, permission instead of autonomy
ok("the chips are 模型 / 推理 / 权限 / 模式; only the mode says its name in words, the others have it as their name and tooltip", await page.eval("[...document.querySelectorAll('.controls .chip')].map(e => e.getAttribute('aria-label').split('：')[0]).join(',') === '模型,推理,权限,模式' && [...document.querySelectorAll('.controls .chip-k')].map(e => e.textContent).join(',') === '模式'"));
const menu = async (chip) => {
  await page.click(".controls .chip", chip);
  const items = await page.eval("[...document.querySelectorAll('.menu-label')].map(e => e.textContent).join(',')");
  await page.key("Escape");
  return items;
};
ok("GPT-6.1-Sol offers Low/Medium/High/XHigh", (await menu(1)) === "Low,Medium,High,XHigh");
ok("the mode menu says Auto / Plan", (await menu(3)) === "Auto,Plan");
ok("the permission menu says 询问 / 写代码 / 完全访问", (await menu(2)) === "询问,写代码,完全访问");
await page.click(".controls .chip", 1);
await clickText(".menu-item", "XHigh");
ok("XHigh can be chosen", await page.eval("document.querySelectorAll('.controls .chip-v')[1].textContent === 'XHigh'"));
await page.click(".controls .chip", 0);
await clickText(".menu-item", "GPT-6-Luna"); // Luna only goes up to High
ok("a model without XHigh falls back to its default level", await page.eval("document.querySelectorAll('.controls .chip-v')[1].textContent === 'Low'"));
ok("and the status bar says so", await page.eval("document.querySelector('.notice-text')?.textContent.includes('不支持')"));

// the status bar: Context used/window [meter] | Token In x Out y; the tooltip lists the breakdown
ok("status bar shows Context n/m with a meter", await page.eval("/^Context\\s*\\d+K\\/\\d+K$/.test(document.querySelector('.ctxmeter').innerText.replace(/\\s+/g, ' ').trim()) && !!document.querySelector('.ctxmeter .meter')"));
ok("status bar shows Token 合计 x In y Out z", await page.eval("/^Token 合计\\s*[\\d.]+[KM]?\\s*In\\s*[\\d.]+K?\\s*Out\\s*[\\d.]+K?$/.test(document.querySelector('.usage').innerText.replace(/\\s+/g, ' ').trim())"));
ok("a separator sits between them", await page.eval("!!document.querySelector('.st-right .st-sep')"));
await page.hover(".usage");
await page.wait(700);
ok("the Token tooltip lists In / Out / Cache Write / Cache Read / hit rate", await page.eval("(() => { const t = [...document.querySelectorAll('.tip-row dt')].map(e => e.textContent).join('|'); return t === '合计|In|Out|Cache Write|Cache Read|Cache 命中率'; })()"));
ok("the hit rate is a percentage", await page.eval("/^\\d+(\\.\\d)?%$/.test([...document.querySelectorAll('.tip-row dd')].pop().textContent)"));
ok("the model pie is above the token table and includes cached tokens in its shares", await page.eval("(() => { const p = document.querySelector('.usage-pie'); const r = [...document.querySelectorAll('.usage-legend li')]; return p?.getAttribute('role') === 'img' && p.querySelectorAll('linearGradient').length === r.length && p.querySelectorAll('path').length === r.length && p.getBoundingClientRect().bottom < document.querySelector('.usage-tip .tip-table').getBoundingClientRect().top && r.map(e => e.querySelector('.usage-percent').textContent).join('|') === '69.1%|20.7%|10.2%' && r[0].textContent.includes('GPT-6.1-Sol') && r[1].textContent.includes('Claude Opus 5.5') && r[2].textContent.includes('未归属'); })()"));
await page.hover(".usage .tip");
await page.wait(500);
ok("the model tooltip stays open when the pointer moves onto the chart", await page.eval("!!document.querySelector('.usage-pie')"));
ok("the expanded usage tooltip fits inside the window", await page.eval("(() => { const r = document.querySelector('.usage .tip').getBoundingClientRect(); return r.left >= 0 && r.top >= 0 && r.right <= innerWidth && r.bottom <= innerHeight; })()"));
mkdirSync(resolve("shots"), { recursive: true });
await page.shot(resolve("shots/model-usage-dark.png"));
await page.eval("document.documentElement.dataset.theme = 'light'");
await page.shot(resolve("shots/model-usage-light.png"));
await page.eval("document.documentElement.dataset.theme = 'dark'");
ok("TPS sits between Context and Token in the status bar", await page.eval("(() => { const t = document.querySelector('.st-right').innerText.replace(/\\s+/g, ' '); return t.indexOf('Context') >= 0 && t.indexOf('Context') < t.indexOf('TPS') && t.indexOf('TPS') < t.indexOf('Token 合计') && /TPS ≈42\\.3 /.test(t); })()"));
await page.hover(".speed");
await page.wait(700);
ok("the TPS tooltip gives the speed, the output, the model's time, the first wait and the speed without it", await page.eval("(() => { const t = [...document.querySelectorAll('.tip-row dt')].map(e => e.textContent).join('|'); const d = [...document.querySelectorAll('.tip-row dd')].map(e => e.textContent); return t === 'TPS|输出|模型用时（不含工具、确认）|首字等待|不含首字等待' && d[0] === '42.3 token/秒' && d[1] === '1,840 token' && d[2] === '43.5 秒' && d[3] === '1.4 秒' && d[4] === '43.7 token/秒' && !document.querySelector('.tip-lines .tip-note'); })()"));
ok("the TPS tooltip says in its first line that the figure is an estimate", await page.eval("document.querySelector('.tip-lines b')?.textContent.startsWith('估算值')"));

// settings: the button, the pages, and a change that takes effect at once
await page.click(".foot-btn", 1);
ok("the settings button opens the settings", await page.eval("!!document.querySelector('.settings')"));
ok("the settings pages also include Connections", await page.eval("[...document.querySelectorAll('.snav')].map(e => e.textContent.trim()).join(',') === '外观,账号与模型,新会话默认值,会话,共享记忆5,外部程序,项目,快捷键,Connections,关于'"));
await page.click(".snav", 2);
ok("defaults page lists model / reasoning / permission / mode", await page.eval("document.querySelectorAll('.settings-scroll .field').length === 4"));
await page.click(".snav", 0);
await page.click(".segc button", 1); // light
ok("theme can be changed in the settings", await page.eval("document.documentElement.dataset.theme === 'light'"));
await page.click(".segc button", 5); // font size: large (the theme has buttons 0-2, the size 3-5)
ok("font size can be changed", await page.eval("document.documentElement.dataset.fs === 'lg'"));
await page.click(".snav", 3);
ok("the sessions page: the compaction switch is on and says 85% (the default), the session limit says 4", await page.eval("(() => { const sw = document.querySelector('.settings-body .field .switch'); const n = [...document.querySelectorAll('.stepper span')].map(e => e.textContent); return sw.getAttribute('aria-checked') === 'true' && n.join() === '85%,4'; })()"));
await page.click(".stepper button", 1);
ok("the compaction stepper moves by 5 points", await page.eval("document.querySelector('.stepper span').textContent === '90%'"));
await page.click(".stepper button", 1);
await page.click(".stepper button", 1);
ok("and stops at 95% (the button is disabled)", await page.eval("document.querySelector('.stepper span').textContent === '95%' && document.querySelectorAll('.stepper button')[1].disabled"));
for (let i = 0; i < 9; i++) await page.click(".stepper button", 0);
ok("and at 50% on the other side", await page.eval("document.querySelector('.stepper span').textContent === '50%' && document.querySelectorAll('.stepper button')[0].disabled"));
await page.click(".stepper button", 3);
ok("the session limit stepper works", await page.eval("[...document.querySelectorAll('.stepper span')].map(e => e.textContent).join() === '50%,5'"));
await page.click(".settings-body .field .switch", 0);
ok("with the compaction switched off the percent is not offered, only the session limit", await page.eval("document.querySelector('.settings-body .field .switch').getAttribute('aria-checked') === 'false' && [...document.querySelectorAll('.stepper span')].map(e => e.textContent).join() === '5'"));
await page.click(".settings-body .field .switch", 0);
await page.click(".snav", 5);
ok("the 外部程序 page has only the Devin CLI, found automatically", await page.eval("[...document.querySelectorAll('.settings-body .field-label')].map(e => e.textContent).join() === 'Devin 命令行' && [...document.querySelectorAll('.settings-body .pathbox')].some(p => p.textContent.toLowerCase().endsWith('devin.exe'))"));
await page.click(".settings-body .segc button", 1);
const devinPath = 'input[aria-label="Devin 命令行 路径"]';
await page.waitFor(`!!document.querySelector('${devinPath}')`, 2000);
await page.click(devinPath);
await page.eval(`(() => { const e = document.querySelector('${devinPath}'); Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, 'value').set.call(e, 'D:\\\\tools\\\\devin.exe'); e.dispatchEvent(new Event('input', { bubbles: true })); e.blur(); })()`);
ok("指定路径 takes your own path when you leave the field", await page.eval("[...document.querySelectorAll('.settings-body .pathbox')].some(p => p.textContent === 'D:\\\\tools\\\\devin.exe')"));
await page.key("Escape");
ok("Esc closes the settings", await page.eval("!document.querySelector('.settings')"));
await page.eval("document.activeElement && document.activeElement.blur()");
await page.eval("document.body.dispatchEvent(new KeyboardEvent('keydown', { key: ',', ctrlKey: true, bubbles: true }))");
ok("Ctrl+, opens the settings", await page.eval("!!document.querySelector('.settings')"));
await page.key("Escape");

// attachments: paste an image through a real ClipboardEvent, remove it
await page.click(".input");
await page.eval(`(() => {
  const c = document.createElement('canvas'); c.width = 64; c.height = 40; const x = c.getContext('2d'); x.fillStyle = '#e5484d'; x.fillRect(0, 0, 64, 40);
  return new Promise(res => c.toBlob(b => {
    const file = new File([b], 'shot.png', { type: 'image/png' });
    const dt = new DataTransfer(); dt.items.add(file);
    document.querySelector('.input').dispatchEvent(new ClipboardEvent('paste', { clipboardData: dt, bubbles: true, cancelable: true }));
    res(true);
  }));
})()`);
await page.waitFor("document.querySelectorAll('.att-row .att').length === 1", 3000);
ok("a pasted image becomes an attachment card", true);
await page.shot(resolve("shots/i-attach.png"));
await page.click(".att-x");
ok("the attachment can be removed", await page.eval("document.querySelectorAll('.att-row .att').length === 0"));
await page.eval(`(() => { const dt = new DataTransfer(); dt.items.add(new File(['hello'], 'notes.txt', { type: 'text/plain' })); document.querySelector('.input').dispatchEvent(new ClipboardEvent('paste', { clipboardData: dt, bubbles: true, cancelable: true })); })()`);
await page.waitFor("document.querySelectorAll('.att-row .att').length === 1", 3000);
ok("a text file's card keeps its remove button beside the name", await page.eval("(() => { const m = document.querySelector('.att-row .att-main').getBoundingClientRect(); const x = document.querySelector('.att-row .att-x').getBoundingClientRect(); return x.left >= m.right - 1 && x.top < m.bottom; })()"));
await page.click(".att-x");

// the drop overlay appears for files and goes away on leave
const drag = (type) => `(() => { const dt = new DataTransfer(); dt.items.add(new File(['x'], 'a.txt', { type: 'text/plain' })); document.querySelector('.main').dispatchEvent(new DragEvent('${type}', { dataTransfer: dt, bubbles: true })); })()`;
await page.eval(drag("dragenter"));
ok("drop overlay appears", await page.eval("!!document.querySelector('.drop-overlay')"));
await page.eval(drag("dragleave"));
ok("drop overlay goes away", await page.eval("!document.querySelector('.drop-overlay')"));

// send, then Esc stops the turn
await page.click(".input");
await page.type("go");
await page.key("Enter");
await page.waitFor("document.querySelector('.send.stop')", 3000);
await page.key("Escape");
await page.waitFor("!document.querySelector('.send.stop')", 3000);
ok("Esc stops the turn", await page.eval("[...document.querySelectorAll('.turnfoot')].pop()?.textContent.startsWith('已停止')"));
ok("the stopped turn shows its footer in the warning colour", await page.eval("!!document.querySelector('.turnfoot.interrupted')"));
// in a conversation: /compact and /goal run on any engine (the demo says what the real app would do)
await setDraft("/compact");
await page.key("Enter");
ok("/compact in a ChatGPT Plan conversation runs, and the box is empty", await page.eval("document.querySelector('.input').value === '' && document.querySelector('.notice-text')?.textContent.includes('压缩成摘要')"));
await setDraft("/goal make it pass");
await page.key("Enter");
ok("/goal there runs too", await page.eval("document.querySelector('.input').value === '' && document.querySelector('.notice-text')?.textContent.includes('make it pass')"));
await page.close();

const setValue = (selector, value) =>
  page.eval(`(() => { const e = document.querySelector(${JSON.stringify(selector)}); Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, 'value').set.call(e, ${JSON.stringify(value)}); e.dispatchEvent(new Event('input', { bubbles: true })); })()`);
const openSettings = async (nav) => {
  await page.eval("document.activeElement && document.activeElement.blur()");
  await page.eval("document.body.dispatchEvent(new KeyboardEvent('keydown', { key: ',', ctrlKey: true, bubbles: true }))");
  await page.waitFor("document.querySelector('.settings')", 2000);
  await page.click(".snav", nav);
};

// the model menu: grouped by where the model runs, a model of the other engine continues the conversation there
page = await openPage(`${index}?scene=done&theme=dark&review=0`);
ok("the context meter is a bar only, no tick", await page.eval("!!document.querySelector('.ctxmeter .meter') && !document.querySelector('.meter-tick') && document.querySelectorAll('.ctxmeter .meter > *').length === 1"));
await page.hover(".ctxmeter");
await page.wait(700);
ok("the Context tooltip has no word about a compaction line", await page.eval("(() => { const t = document.querySelector('.tip')?.textContent ?? ''; return t.includes('已用') && t.includes('窗口') && !/压缩|竖线|琥珀/.test(t); })()"));
await page.mouse("mouseMoved", 5, 5, { button: "none", clickCount: 0 });
const tabsBefore = await page.eval("document.querySelectorAll('.tab').length");
await page.click(".controls .chip", 0);
ok("the model menu has ChatGPT Plan, Devin and API groups, and nothing else", await page.eval("(() => { const h = [...document.querySelectorAll('.menu-head > span:first-child')].map(e => e.textContent); return h[0] === 'CHATGPT PLAN' && h[1] === 'DEVIN' && h.length >= 3 && h.slice(2).every(x => x.startsWith('API · ')); })()"));
ok("Devin's models are offered without repeating a handover explanation on every row", await page.eval("(() => { const t = document.querySelector('.menu').textContent; return !t.includes('之前的文字会交接') && ['SWE-2', 'Claude Opus 5.5', 'GPT-6 Sol', 'GLM-5.2 High'].every(n => t.includes(n)); })()"));
ok("adding a provider and managing accounts stay visible at the bottom", await page.eval("(() => { const p = document.querySelector('.menu-pinned').getBoundingClientRect(); const m = document.querySelector('.menu').getBoundingClientRect(); return p.bottom <= m.bottom + 1 && p.top >= m.top && [...document.querySelectorAll('.menu-pinned .menu-label')].map(e => e.textContent).join() === '添加 API Provider…,管理账号与模型…'; })()"));
ok("the plan's own models carry no such note", await page.eval("![...document.querySelectorAll('.menu-item')].some(e => e.textContent.includes('GPT-6-Astra') && e.textContent.includes('交接'))"));
await clickText(".menu-item", "SWE-2");

// changing engine inside the conversation: same tab, a divider, what goes along, a fresh start for the figures
ok("picking a Devin model stays in the same conversation: no new tab", (await page.eval("document.querySelectorAll('.tab').length")) === tabsBefore && (await page.eval("document.querySelector('.tab.current').textContent.includes('File operations')")));
ok("the status bar says what happens to the earlier text", await page.eval("(() => { const t = document.querySelector('.notice-text')?.textContent ?? ''; return t.includes('已换到 SWE-2（Devin）') && /之前 \\d+ 条对话的文字会随下一条消息交给它/.test(t); })()"));
ok("a divider marks the change: from GPT-6.1-Sol (ChatGPT Plan) to SWE-2 (Devin)", await page.eval("(() => { const d = document.querySelector('.block.engine-switch'); return !!d && d.textContent.includes('GPT-6.1-Sol') && d.textContent.includes('ChatGPT Plan') && d.textContent.includes('SWE-2') && d.textContent.includes('Devin'); })()"));
ok("the divider states what is not carried over", await page.eval("document.querySelector('.block.engine-switch').textContent.includes('工具调用、结果、思考和图片不会带过去')"));
ok("the divider spans the conversation and is a few lines tall, not a squeezed column", await page.eval("(() => { const d = document.querySelector('.block.engine-switch').getBoundingClientRect(); const t = document.querySelector('.transcript').getBoundingClientRect(); return d.width > t.width * 0.8 && d.height < 130; })()"));
ok("the earlier conversation is still on the page", await page.eval("document.querySelector('.transcript').textContent.includes('List the files here')"));
ok("the model chip is SWE-2 with the 订阅 tag", await page.eval("(() => { const c = document.querySelectorAll('.controls .chip')[0]; return c.textContent.includes('SWE-2') && c.querySelector('.src-tag')?.textContent === '订阅'; })()"));
ok("Devin has its own permission scale", (await menu(2)) === "问答,计划,写代码,智能,完全访问" && (await page.eval("['问答', '计划', '写代码', '智能', '完全访问'].includes(document.querySelectorAll('.controls .chip-v')[2].textContent)")));
ok("the status bar has no Limit segment", await page.eval("!document.querySelector('.st-right').innerText.includes('Limit')"));
ok("Context starts again for the new engine, the Token total goes on", await page.eval("(() => { const t = document.querySelector('.st-right').innerText.replace(/\\s+/g, ' '); return /Context —\\/\\d+K/.test(t) && /Token 合计\\s*[\\d.]+[KM]?\\s*In/.test(t); })()"));
ok("the hint under the message box says the next message carries the text", await page.eval("(() => { const h = document.querySelector('.hint'); return h.classList.contains('handoff') && /下一条消息会带上之前 \\d+ 条对话的文字，交给 SWE-2/.test(h.textContent); })()"));
ok("the sidebar names the engine of the conversation", await page.eval("document.querySelector('.srow.current').textContent.includes('· Devin')"));
await page.click(".controls .chip", 0);
ok("the plan's models also omit the repetitive handover explanation", await page.eval("document.querySelector('.menu').textContent.includes('GPT-6.1-Sol') && !document.querySelector('.menu').textContent.includes('之前的文字会交接')"));
await page.key("Escape");

// the next message goes out with the text, and a running turn cannot change engine
await page.click(".controls .chip", 2);
await clickText(".menu-item", "写代码");
await page.click(".input");
await page.type("continue with the Devin model");
await page.key("Enter");
await page.waitFor("document.querySelector('.send.stop')", 3000);
ok("the sent message says it carried the earlier text", await page.eval("(() => { const n = [...document.querySelectorAll('.handoff-note')].pop(); return !!n && /附带了之前 \\d+ 条对话的文字/.test(n.textContent); })()"));
ok("and the hint is back to normal", await page.eval("!document.querySelector('.hint.handoff')"));
await page.click(".controls .chip", 0);
ok("while the turn runs the other engine's models are disabled, with the reason", await page.eval("(() => { const items = [...document.querySelectorAll('.menu-item')].filter(e => e.textContent.includes('本轮结束后才能换到 ChatGPT Plan')); return items.length >= 3 && items.every(e => e.disabled); })()"));
ok("while Devin's own models stay available", await page.eval("[...document.querySelectorAll('.menu-item')].filter(e => e.textContent.includes('Claude Opus 5.5') && !e.textContent.includes('本轮结束后')).every(e => !e.disabled)"));
await page.click(".scroller"); // closes the menu without Esc, which would also stop the turn
await page.waitFor("document.querySelector('.prompt.perm')", 10000); // Devin's Code level asks before it edits
ok("Devin's 写代码 level asks for approval before the edit", true);
ok("the approval's answers are one per line, as wide as the card, the label at the left", await page.eval(`${oneLineEach(".prompt.perm .pbtns .btn")} && (() => { const card = document.querySelector('.prompt.perm .pbtns').getBoundingClientRect(); return [...document.querySelectorAll('.prompt.perm .pbtns .btn')].every(b => Math.abs(b.getBoundingClientRect().width - card.width) < 1 && getComputedStyle(b).justifyContent === 'flex-start'); })()`));
await page.click(".prompt.perm .btn.primary");
await page.waitFor(READY, 12000);
await page.hover(".usage");
await page.waitFor("[...document.querySelectorAll('.tip-row dt')].some(e => e.textContent === '合计')", 3000);
ok("the first turn on the new engine adds to the conversation's total", await page.eval("(() => { const rows = Object.fromEntries([...document.querySelectorAll('.tip-row')].map(r => [r.querySelector('dt').textContent, r.querySelector('dd').textContent])); return /\\d/.test(rows['合计']) && rows['Cache 命中率'] !== '—'; })()"));
await page.mouse("mouseMoved", 5, 5, { button: "none", clickCount: 0 });

// and back to the own loop, then a change inside one engine, which leaves no divider
const firstCarried = await page.eval("Number(document.querySelector('.block.engine-switch').textContent.match(/之前 (\\d+) 条/)[1])");
await page.click(".controls .chip", 0);
await clickText(".menu-item", "GPT-6-Astra");
ok("changing back adds a second divider, from Devin to ChatGPT Plan", await page.eval("(() => { const d = [...document.querySelectorAll('.block.engine-switch')]; return d.length === 2 && d[1].textContent.includes('SWE-2') && d[1].textContent.includes('GPT-6-Astra'); })()"));
ok("it carries the whole conversation so far, including the Devin turn", await page.eval(`Number([...document.querySelectorAll('.block.engine-switch')][1].textContent.match(/之前 (\\d+) 条/)[1]) > ${firstCarried}`));
ok("the permission scale is the own loop's again", (await menu(2)) === "询问,写代码,完全访问");
await page.click(".controls .chip", 0);
await clickText(".menu-item", "GPT-6.1-Sol");
ok("a change inside the own loop has no divider and no handoff", await page.eval("document.querySelectorAll('.block.engine-switch').length === 2 && document.querySelectorAll('.controls .chip-v')[0].textContent === 'GPT-6.1-Sol'"));
ok("no console errors (model menu)", page.consoleErrors().length === 0);
await page.close();

// changing engine in a session with nothing in it loses nothing, so there is nothing to hand over
page = await openPage(`${index}?scene=empty&theme=dark&review=0`);
await page.click(".controls .chip", 0);
ok("an empty session's menu also omits the redundant switch explanation", await page.eval("!document.querySelector('.menu').textContent.includes('这个会话还是空的') && !document.querySelector('.menu').textContent.includes('之前的文字会交接')"));
await clickText(".menu-item", "SWE-2");
ok("it changes engine without a divider or a handoff note", await page.eval("!document.querySelector('.block.engine-switch') && !document.querySelector('.hint.handoff') && document.querySelectorAll('.controls .chip-v')[0].textContent === 'SWE-2'"));
ok("and the status bar does not talk about earlier text", await page.eval("(() => { const t = document.querySelector('.notice-text')?.textContent ?? ''; return t.includes('已换到 SWE-2') && !t.includes('条对话'); })()"));
await page.close();

// another ChatGPT Plan session and an API session
page = await openPage(`${index}?scene=index&theme=dark&review=0`);
ok("a ChatGPT Plan session: the status bar has Context and Token, no Limit", await page.eval("(() => { const t = document.querySelector('.st-right').innerText.replace(/\\s+/g, ' '); return /Context \\d+K\\/\\d+K/.test(t) && /Token 合计/.test(t) && !t.includes('Limit'); })()"));
ok("its reasoning levels come from the model", (await menu(1)) === "Low,Medium,High,XHigh");
await page.hover(".usage");
await page.wait(700);
ok("the session shows its own speed", await page.eval("/TPS ≈63\\.4 /.test(document.querySelector('.st-right').innerText.replace(/\\s+/g, ' '))"));
ok("there is no invented Cache Write row; unknown old model attribution is explained", await page.eval("(() => { const rows = [...document.querySelectorAll('.tip-row dt')].map(e => e.textContent).join('|'); return rows === '合计|In|Out|Cache Read|Cache 命中率' && document.querySelector('.usage-tip .tip-note')?.textContent.includes('未归属'); })()"));
ok("the sidebar names the engine of the session", await page.eval("[...document.querySelectorAll('.srow')].some(r => r.textContent.includes('重写索引构建') && r.textContent.includes('· ChatGPT Plan'))"));
await page.close();

page = await openPage(`${index}?scene=api&theme=light&review=0`);
ok("an API session shows the API tag on the model chip", await page.eval("document.querySelectorAll('.controls .chip')[0].querySelector('.src-tag')?.textContent === 'API'"));
ok("a model without reasoning levels disables the reasoning chip", await page.eval("(() => { const c = document.querySelectorAll('.controls .chip')[1]; return c.disabled && c.querySelector('.chip-v').textContent === '—'; })()"));
ok("an API session has no Limit segment", await page.eval("!document.querySelector('.st-right').innerText.includes('Limit')"));
await page.close();

// accounts and models: WittyCode's own engine, Devin and the API providers, no other program's
page = await openPage(`${index}?scene=done&settings=providers&theme=dark&review=0`);
ok("the page has the own-loop providers, Devin and an optional Factory CLI card", await page.eval("document.querySelectorAll('.providers .pcard').length === 4 && document.querySelector('.factory-card').textContent.includes('Factory CLI（可选）')"));
ok("no console errors (accounts page)", page.consoleErrors().length === 0);
await page.close();

page = await openPage(`${index}?scene=done&settings=providers&providers=none&theme=light&review=0`);
ok("with no providers the API card offers quick starts", await page.eval("[...document.querySelectorAll('.quick .chip-suggest')].map(e => e.textContent).join() === 'OpenAI 官方,Anthropic 官方,OpenRouter,Ollama（本机）'"));
await clickText(".quick .chip-suggest", "Ollama");
ok("a quick start opens the editor with the preset filled in", await page.eval("document.querySelector('.pe-title').textContent === '添加 API Provider' && document.querySelector('input[aria-label=\"Base URL\"]').value === 'http://localhost:11434/v1' && document.querySelector('.segc button.on').textContent === '不需要'"));
const imagesOn = () => page.eval("document.querySelector('.mrow:not(.mhead) .switch').getAttribute('aria-checked') === 'true'");
ok("a generic-format provider's model starts with pictures off (the usual default for that format)", (await imagesOn()) === false);
await page.close();
page = await openPage(`${index}?scene=done&settings=providers&providers=none&theme=light&review=0`);
await clickText(".quick .chip-suggest", "OpenAI 官方");
ok("an OpenAI provider's model starts with pictures on, as the editor's note says", await page.eval("document.querySelector('.mrow:not(.mhead) .switch').getAttribute('aria-checked') === 'true'"));
await setValue('input[aria-label="模型 ID"]', "gpt-6.1-sol");
ok("an exact known API model shows its documented window without making it an override", await page.eval("(() => { const e = document.querySelector('input[aria-label=\"上下文窗口\"]'); return e.value === '' && e.placeholder === '1050000' && e.parentElement.title.includes('developers.openai.com'); })()"));
await page.click(".mrow:not(.mhead) .selectbox");
ok("the API editor offers Minimal, XHigh and Max as well as the old levels", await page.eval("[...document.querySelectorAll('.menu-label')].map(e => e.textContent).join(',') === '无,Off,Minimal,Low,Medium,High,XHigh,Max'"));
await page.shot("../sandbox/api-efforts-light.png");
await clickText(".menu-item", "XHigh");
ok("XHigh remains selected in the API editor", await page.eval("document.querySelector('.mrow:not(.mhead) .selectbox').textContent.includes('XHigh')"));
await page.click(".mrow:not(.mhead) .selectbox");
await clickText(".menu-item", "Max");
ok("Max remains selected in the API editor", await page.eval("document.querySelector('.mrow:not(.mhead) .selectbox').textContent.includes('Max')"));
await page.click(".mtable + .pe-btns .btn");
ok("so does a model added with 添加模型", await page.eval("[...document.querySelectorAll('.mrow:not(.mhead) .switch')].length === 2 && [...document.querySelectorAll('.mrow:not(.mhead) .switch')].every((s) => s.getAttribute('aria-checked') === 'true')"));
await page.close();

// a Devin session and the Devin account
page = await openPage(`${index}?scene=devin&theme=dark&review=0`);
ok("a Devin session: the status bar has Context and Token, no Limit", await page.eval("(() => { const t = document.querySelector('.st-right').innerText.replace(/\\s+/g, ' '); return /Context \\d+K\\/\\d+K/.test(t) && /Token 合计/.test(t) && !t.includes('Limit'); })()"));
ok("the thought levels are the model's own", (await menu(1)) === "Medium,High,Max");
ok("the permission scale is Devin's modes", (await menu(2)) === "问答,计划,写代码,智能,完全访问");
ok("the model chip is tagged as a subscription", await page.eval("document.querySelectorAll('.controls .chip')[0].querySelector('.src-tag')?.textContent === '订阅'"));
ok("the sidebar names the engine of a Devin session", await page.eval("[...document.querySelectorAll('.srow')].some(r => r.textContent.includes('Devin：给配置参数') && r.textContent.includes('· Devin'))"));
await page.hover(".usage");
await page.wait(700);
ok("there is no Cache Write row for Devin either", await page.eval("(() => { const rows = [...document.querySelectorAll('.tip-row dt')].map(e => e.textContent); return rows.length > 0 && !rows.includes('Cache Write'); })()"));
await page.close();

page = await openPage(`${index}?scene=empty&theme=dark&review=0`);
await page.click(".controls .chip", 0);
await clickText(".menu-item", "SWE-2");
ok("an empty session moved to a Devin model takes that engine's levels: Code and the model's own thought level", await page.eval("(() => { const v = [...document.querySelectorAll('.controls .chip-v')].map(e => e.textContent); return v[0] === 'SWE-2' && v[1] === 'High' && v[2] === '写代码'; })()"));
await page.close();

page = await openPage(`${index}?scene=done&settings=providers&devin=signed-out&theme=dark&review=0`);
ok("Devin starts signed out, with a sign-in button", await page.eval("document.querySelector('.pcard:nth-child(2)').textContent.includes('Devin 账号') && document.querySelector('.pcard:nth-child(2) .pstate.off')?.textContent.includes('未登录') && document.querySelector('.pcard:nth-child(2) .btn.primary').textContent.trim() === '登录 Devin'"));
await page.click(".pcard:nth-child(2) .btn.primary");
await page.waitFor("document.querySelector('.pcard:nth-child(2)').textContent.includes('正在等你在浏览器里完成登录')", 2000);
ok("signing in shows that the browser is open, with a cancel button", await page.eval("document.querySelector('.pcard:nth-child(2) .pcard-actions .btn').textContent.trim() === '取消'"));
await page.click(".pcard:nth-child(2) .pcard-actions .btn");
await page.waitFor("document.querySelector('.pcard:nth-child(2)').textContent.includes('未登录')", 2000);
await page.click(".pcard:nth-child(2) .btn.primary");
await page.waitFor("document.querySelector('.pcard:nth-child(2)').textContent.includes('已登录')", 6000);
ok("after signing in the card shows the account and the plan", await page.eval("document.querySelector('.pcard:nth-child(2)').textContent.includes('Devin Pro') && document.querySelector('.pcard:nth-child(2)').textContent.includes('demo@example.com')"));
ok("the models can be chosen one by one, like the other sources", await page.eval("!!document.querySelector('.mvis[data-source=\"devin\"]') && document.querySelector('.mvis[data-source=\"devin\"] .mvis-count').textContent.replace(/\\s+/g, '') === '4/4'"));
await page.click(".pcard:nth-child(2) .switch");
ok("Devin models can be hidden from the menu without signing out", await page.eval("document.querySelector('.pcard:nth-child(2) .switch').getAttribute('aria-checked') === 'false'"));
await page.click(".pcard:nth-child(2) .switch");
await page.key("Escape");
await page.click(".controls .chip", 0);
ok("Devin models are in the menu after signing in", await page.eval("[...document.querySelectorAll('.menu-item')].some(e => e.textContent.includes('Claude Opus 5.5')) && ![...document.querySelectorAll('.menu-item')].some(e => e.textContent.includes('登录 Devin'))"));
await page.key("Escape");
await openSettings(1);
await clickText(".pcard:nth-child(2) .btn", "退出登录");
await page.waitFor("document.querySelector('.pcard:nth-child(2)').textContent.includes('未登录')", 3000);
await page.key("Escape");
await page.click(".controls .chip", 0);
ok("signed out: the menu offers 登录 Devin… instead of its models", await page.eval("[...document.querySelectorAll('.menu-item')].some(e => e.textContent.includes('登录 Devin')) && ![...document.querySelectorAll('.menu-item')].some(e => e.textContent.includes('Claude Opus 5.5'))"));
await clickText(".menu-item", "登录 Devin");
ok("and picking it opens the accounts page", await page.eval("!!document.querySelector('.settings') && document.querySelector('.settings-body').textContent.includes('Devin 账号')"));
ok("no console errors (Devin card)", page.consoleErrors().length === 0);
await page.close();

page = await openPage(`${index}?scene=done&settings=providers&devin=missing&theme=light&review=0`);
ok("Devin CLI missing: the card says what to install", await page.eval("document.querySelector('.pcard:nth-child(2)').textContent.includes('没有找到 devin 命令行') && document.querySelector('.pcard:nth-child(2)').textContent.includes('Devin 桌面版或 Devin CLI')"));
await page.close();

// a ChatGPT Plan session: WittyCode's own harness on the user's ChatGPT plan
page = await openPage(`${index}?scene=chatgpt&theme=dark&review=0`);
ok("a ChatGPT Plan session says it is using the plan, with the way to manage usage", await page.eval("(() => { const b = document.querySelector('.hint .plan-badge'); const a = b?.querySelector('a'); return !!b && b.textContent.includes('Using ChatGPT Plan') && !b.classList.contains('off') && a.textContent === 'Manage usage' && a.getAttribute('href') === 'https://chatgpt.com/settings/usage'; })()"));
ok("the badge is under the box, so the send button stays on the row of the controls and nothing is left empty below it", await page.eval("(() => { const r = (e) => e.getBoundingClientRect(); const bar = document.querySelector('.composer-bar'); const chip = document.querySelector('.controls .chip'); const send = document.querySelector('.composer-bar .send'); const hint = document.querySelector('.hint'); const b = document.querySelector('.hint .plan-badge'); const mid = (e) => r(e).top + r(e).height / 2; return !document.querySelector('.controls .plan-badge') && Math.abs(mid(send) - mid(chip)) < 6 && r(bar).height < 40 && r(hint).height < 22 && r(b).right <= r(hint).right + 1 && r(b).left > r(hint).left + 100; })()"));
ok("unavailable subscription figures have no empty ring or invented percentage", await page.eval("!document.querySelector('.plan-ring, .usage-ring') && !!document.querySelector('.plan-badge a')"));
ok("the badge still fits the line under the box with the ring", await page.eval("(() => { const r = (e) => e.getBoundingClientRect(); const hint = document.querySelector('.hint'); const b = document.querySelector('.hint .plan-badge'); return r(hint).height < 22 && r(b).right <= r(hint).right + 1 && r(b).height < 22; })()"));
ok("the reasoning levels are the model's own", (await menu(1)) === "Low,Medium,High,XHigh");
ok("the permission scale is WittyCode's own three", (await menu(2)) === "询问,写代码,完全访问");
ok("the model chip is tagged as a subscription", await page.eval("document.querySelectorAll('.controls .chip')[0].querySelector('.src-tag')?.textContent === '订阅'"));
ok("the sidebar names the engine of the session", await page.eval("[...document.querySelectorAll('.srow')].some(r => r.textContent.includes('ChatGPT Plan：核对命中率') && r.textContent.includes('· ChatGPT Plan'))"));
ok("a shell call shows its command and the tool is told apart from a search", await page.eval("document.querySelectorAll('.tool-row, .trow, [data-tool]').length >= 0"));
ok("the menu has its own CHATGPT PLAN group, with the models and the account", await (async () => {
  await page.click(".controls .chip", 0);
  const ok_ = await page.eval("(() => { const h = [...document.querySelectorAll('.menu-head > span:first-child')].map(e => e.textContent); const items = [...document.querySelectorAll('.menu-item')].map(e => e.textContent); return h.includes('CHATGPT PLAN') && ['GPT-6.1-Sol', 'GPT-6-Astra', 'GPT-6-Luna'].every(n => items.some(t => t.includes(n))) && document.querySelector('.menu-head')?.parentElement.textContent.includes('demo@example.com'); })()");
  await page.key("Escape");
  return ok_;
})());
await page.close();

// The sign-in exposes no subscription balance: keep the usage link, omit the empty ring in either theme.
page = await openPage(`${index}?scene=chatgpt&theme=light&review=0`);
ok("the light theme also omits the unavailable balance ring", await page.eval("!document.querySelector('.plan-ring, .usage-ring') && document.querySelector('.plan-badge a').textContent === 'Manage usage'"));
await page.close();

// messages waiting for their turn: one line each above the composer, the width of the box, four icons at the end
page = await openPage(`${index}?scene=live&queued=2&theme=light&review=0`);
ok("the waiting messages are above the composer, not in the conversation", await page.eval("(() => { const strip = document.querySelector('.composer-wrap > .queue-strip'); return !!strip && strip.nextElementSibling === document.querySelector('.composer') && !document.querySelector('.transcript .qs-row') && !document.querySelector('.turn.queued') && strip.querySelectorAll('.qs-row').length === 2; })()"));
ok("each is one line, as wide as the box, and a long one is cut short", await page.eval("(() => { const r = (e) => e.getBoundingClientRect(); const w = r(document.querySelector('.composer')).width; const rows = [...document.querySelectorAll('.qs-row')]; const text = document.querySelector('.qs-row .qs-text'); return rows.every((x) => r(x).height < 40 && Math.abs(r(x).width - w) < 2) && text.scrollWidth > text.clientWidth; })()"));
ok("the line says which one it is, and the icons are at its end: view, change, cancel, send now", await page.eval("(() => { const row = document.querySelector('.qs-row'); const labels = [...row.querySelectorAll('.qs-icons button')].map((b) => b.getAttribute('aria-label')); const icons = row.querySelector('.qs-icons').getBoundingClientRect(); return row.querySelector('.qs-tag').textContent === '排队 1/2' && labels.join('|') === '查看全文|修改|取消排队（放回输入框）|立即发送（交给正在运行的这一轮）' && Math.abs(icons.right - row.getBoundingClientRect().right) < 8; })()"));
ok("a message with an attachment says so", await page.eval("document.querySelectorAll('.qs-row')[1].querySelector('.qs-att')?.textContent === '1'"));
await page.click(".qs-row .qs-icons button", 0);
ok("viewing opens the whole message, and the same icon closes it", await page.eval("(() => { const row = document.querySelector('.qs-row'); const text = row.querySelector('.qs-text'); return row.classList.contains('open') && text.scrollWidth <= text.clientWidth + 1 && row.getBoundingClientRect().height > 40 && row.querySelector('.qs-icons button').getAttribute('aria-label') === '收起'; })()"));
await page.click(".qs-row .qs-icons button", 0);
ok("closed again: one line", await page.eval("!document.querySelector('.qs-row').classList.contains('open') && document.querySelector('.qs-row').getBoundingClientRect().height < 40"));
await page.click(".qs-row .qs-icons button", 1);
ok("changing opens an editor with the words, and saving or giving up are the icons", await page.eval("(() => { const row = document.querySelector('.qs-row'); const box = row.querySelector('textarea.qs-edit'); return !!box && box.value.startsWith('然后把测试也补上') && [...row.querySelectorAll('.qs-icons button')].map((b) => b.getAttribute('aria-label')).join('|') === '保存修改（Enter）|放弃修改（Esc）'; })()"));
await page.click(".qs-row .qs-icons button", 1);
ok("giving up leaves the message as it was", await page.eval("!document.querySelector('.qs-row textarea') && document.querySelector('.qs-row .qs-text').textContent.startsWith('然后把测试也补上')"));
await page.click(".qs-row .qs-icons button", 3);
ok("send now takes it out of the line", await page.eval("document.querySelectorAll('.qs-row').length === 1 && document.querySelector('.qs-row .qs-text').textContent === '再跑一遍完整测试' && document.querySelector('.qs-row .qs-tag').textContent === '排队'"));
await page.click(".qs-row .qs-icons button", 2);
ok("cancel takes the last one out, and with none left the strip is gone", await page.eval("!document.querySelector('.queue-strip')"));
ok("no console errors (the waiting messages)", page.consoleErrors().length === 0);
await page.close();

// the controls and the send button stay on one line at the widths a window really has (the send button once fell to a line of its own)
for (const width of [1500, 1100, 900, 760]) {
  page = await openPage(`${index}?scene=chatgpt&theme=light&review=0`, { width });
  ok(`at ${width}px wide the controls and the send button are one row, with the plan badge under the box`, await page.eval("(() => { const r = (e) => e.getBoundingClientRect(); const mid = (e) => r(e).top + r(e).height / 2; const bar = document.querySelector('.composer-bar'); const send = document.querySelector('.composer-bar .send'); const chips = [...document.querySelectorAll('.controls .chip')]; return r(bar).height < 40 && chips.every((c) => Math.abs(mid(c) - mid(send)) < 8) && r(send).right <= r(bar).right + 1 && !!document.querySelector('.hint .plan-badge'); })()"));
  await page.close();
}

// the first message box, with the longest chip values there are (a ChatGPT Plan model with its tag, XHigh, 完全访问): one row at every
// width, the permission and the mode on the left, the model and the reasoning on the right with the send button, nothing clipped
for (const width of [1500, 1100, 900]) {
  page = await openPage(`${index}?scene=done&theme=light&review=0`, { width });
  await page.click(".newbtn");
  for (const [chip, text] of [[0, "GPT-6-Astra"], [1, "XHigh"], [2, "完全访问"]]) {
    await page.click(".controls .chip", chip);
    await page.click(".menu-item", await page.eval(`[...document.querySelectorAll('.menu-item')].findIndex((e) => e.textContent.includes(${JSON.stringify(text)}))`));
  }
  ok(`the first message box at ${width}px wide: one row, permission and mode left, model and reasoning right by the send button, no value cut short`, await page.eval("(() => { const bar = document.querySelector('.composer-bar'); const b = bar.getBoundingClientRect(); const send = document.querySelector('.composer-bar .send').getBoundingClientRect(); const left = (s) => document.querySelector(s).getBoundingClientRect().left; const chips = [...document.querySelectorAll('.controls .chip')].map((c) => c.getBoundingClientRect()); return b.height < 40 && new Set(chips.map((c) => Math.round(c.top))).size === 1 && send.top >= b.top - 1 && send.bottom <= b.bottom + 1 && Math.abs(send.right - b.right) < 3 && left('.chip-permission') < left('.chip-mode') && left('.chip-mode') < left('.chip-model') && left('.chip-model') < left('.chip-reasoning') && left('.chip-reasoning') < send.left && [...document.querySelectorAll('.controls .chip-v')].every((v) => v.scrollWidth <= v.clientWidth); })()"));
  ok(`…and under it only the project (no line of key hints, they are in the box)`, await page.eval("(() => { const h = document.querySelector('.hint'); return !!h && h.querySelector('.chip-k').textContent === '项目' && !/Enter/.test(h.textContent) && /Enter 发送/.test(document.querySelector('.input').placeholder); })()"));
  await page.close();
}
// a conversation of any engine: no line under the box unless it has something to say
page = await openPage(`${index}?scene=devin&theme=light&review=0`, { width: 1300 });
ok("an ordinary conversation has no line under the box, and the keys are in the box", await page.eval("!document.querySelector('.hint') && /Enter 发送 · Shift\\+Enter 换行/.test(document.querySelector('.input').placeholder)"));
ok("the model chip and the reasoning chip are not labelled in words (they are named for assistive technology), the mode is", await page.eval("[...document.querySelectorAll('.controls .chip')].map((c) => c.getAttribute('aria-label').split('：')[0]).join(',') === '模型,推理,权限,模式' && [...document.querySelectorAll('.controls .chip-k')].map((e) => e.textContent).join(',') === '模式'"));
await page.close();

page = await openPage(`${index}?scene=empty&theme=dark&review=0`);
await page.click(".controls .chip", 0);
await clickText(".menu-item", "GPT-6.1-Sol");
ok("an empty session moved to a ChatGPT Plan model takes that engine's levels: Code (Low stays, the model has it), and the plan is shown as in use", await page.eval("(() => { const v = [...document.querySelectorAll('.controls .chip-v')].map(e => e.textContent); return v[0] === 'GPT-6.1-Sol' && v[1] === 'Low' && v[2] === '写代码' && !!document.querySelector('.plan-badge'); })()"));
await page.close();

page = await openPage(`${index}?scene=done&settings=providers&chatgpt=signed-out&theme=dark&review=0`);
ok("the page has four cards including optional Factory CLI", await page.eval("document.querySelectorAll('.providers .pcard').length === 4"));
ok("ChatGPT Plan starts signed out, with OpenAI's wording on a black button", await page.eval("document.querySelector('.pcard:nth-child(1)').textContent.includes('ChatGPT Plan') && document.querySelector('.pcard:nth-child(1) .pstate.off')?.textContent.includes('未登录') && document.querySelector('.pcard:nth-child(1) .btn.chatgpt-btn').textContent.trim() === 'Continue with ChatGPT' && getComputedStyle(document.querySelector('.pcard:nth-child(1) .btn.chatgpt-btn')).backgroundColor === 'rgb(0, 0, 0)'"));
await page.click(".pcard:nth-child(1) .btn.chatgpt-btn");
await page.waitFor("document.querySelector('.pcard:nth-child(1)').textContent.includes('正在等你在浏览器里完成登录')", 2000);
ok("signing in shows that the browser is open, with a cancel button", await page.eval("document.querySelector('.pcard:nth-child(1) .pcard-actions .btn').textContent.trim() === '取消'"));
await page.click(".pcard:nth-child(1) .pcard-actions .btn");
await page.waitFor("document.querySelector('.pcard:nth-child(1)').textContent.includes('未登录')", 2000);
await page.click(".pcard:nth-child(1) .btn.chatgpt-btn");
await page.waitFor("document.querySelector('.pcard:nth-child(1)').textContent.includes('使用你的 ChatGPT 套餐')", 6000);
ok("after signing in the card shows the account and where to manage the usage", await page.eval("document.querySelector('.pcard:nth-child(1)').textContent.includes('demo@example.com') && document.querySelector('.pcard:nth-child(1) a.inline-link')?.getAttribute('href') === 'https://chatgpt.com/settings/usage'"));
ok("and the first sign-in is met with 'You're using your ChatGPT Plan'", await page.eval("(() => { const d = document.querySelector('.chatgpt-welcome'); return !!d && d.textContent.includes(\"You're using your ChatGPT Plan\") && d.textContent.includes('Manage usage') && d.querySelector('.btn.primary').textContent.trim() === 'Got it'; })()"));
await page.click(".chatgpt-welcome .btn.primary");
ok("Got it closes it for good", await page.eval("!document.querySelector('.chatgpt-welcome')"));
ok("the models can be chosen one by one, like the other sources", await page.eval("!!document.querySelector('.mvis[data-source=\"chatgpt\"]') && document.querySelector('.mvis[data-source=\"chatgpt\"] .mvis-count').textContent.replace(/\\s+/g, '') === '3/3'"));
await page.click(".pcard:nth-child(1) .switch");
await page.wait(150);
ok("ChatGPT Plan models can be hidden from the menu without signing out", await page.eval("document.querySelector('.pcard:nth-child(1) .switch').getAttribute('aria-checked') === 'false'"));
await page.click(".pcard:nth-child(1) .switch");
await page.key("Escape");
await page.click(".controls .chip", 0);
ok("the models are in the menu after signing in", await page.eval("[...document.querySelectorAll('.menu-item')].some(e => e.textContent.includes('GPT-6-Astra')) && ![...document.querySelectorAll('.menu-item')].some(e => e.textContent.includes('Continue with ChatGPT'))"));
await page.key("Escape");
await openSettings(1);
await clickText(".pcard:nth-child(1) .btn", "退出登录");
await page.waitFor("document.querySelector('.pcard:nth-child(1)').textContent.includes('未登录')", 3000);
await page.key("Escape");
await page.click(".controls .chip", 0);
ok("signed out: the menu offers Continue with ChatGPT instead of the models", await page.eval("[...document.querySelectorAll('.menu-item')].some(e => e.textContent.includes('Continue with ChatGPT')) && ![...document.querySelectorAll('.menu-item')].some(e => e.textContent.includes('GPT-6-Astra'))"));
ok("no console errors (ChatGPT Plan card)", page.consoleErrors().length === 0);
await page.close();

page = await openPage(`${index}?scene=done&settings=providers&chatgpt=no-plan&theme=light&review=0`);
ok("signed in without the permission to use the plan: the card says so and offers to ask again", await page.eval("document.querySelector('.pcard:nth-child(1)').textContent.includes('还没有允许使用套餐') && document.querySelector('.pcard:nth-child(1) .pstate.warn') && document.querySelector('.pcard:nth-child(1) .pcard-actions .btn').textContent.trim() === '允许使用 ChatGPT 套餐'"));
await page.key("Escape");
await page.click(".controls .chip", 0);
ok("and so does the menu, without any ChatGPT Plan models", await page.eval("[...document.querySelectorAll('.menu-item')].some(e => e.textContent.includes('允许使用 ChatGPT 套餐…')) && ![...document.querySelectorAll('.menu-item')].some(e => e.textContent.includes('GPT-6-Astra'))"));
await page.close();

page = await openPage(`${index}?scene=chatgpt&chatgpt=signed-out&theme=light&review=0`);
ok("a ChatGPT Plan session whose account signed out says it is not signed in", await page.eval("document.querySelector('.plan-badge.off')?.textContent.includes('ChatGPT 未登录')"));
await page.close();

page = await openPage(`${index}?scene=done&settings=programs&theme=light&review=0`);
ok("the 外部程序 page holds the Devin program path", await page.eval("document.querySelector('.settings-body').textContent.includes('Devin 命令行')"));
await page.close();

// accounts and models: which models the conversation's model menu offers
page = await openPage(`${index}?scene=done&settings=providers&theme=light&review=0`);
const mvis = (source) => `.mvis[data-source="${source}"]`;
const mvisCount = (source) => page.eval(`document.querySelector('${mvis(source)} .mvis-count').textContent.replace(/\\s+/g, '')`);
const rowSwitch = async (source, label) => {
  const i = await page.eval(`[...document.querySelectorAll('${mvis(source)} .mvis-row')].findIndex(e => e.textContent.includes(${JSON.stringify(label)}))`);
  if (i < 0) throw new Error(`no ${source} model "${label}"`);
  await page.click(`${mvis(source)} .mvis-row .switch`, i);
};
const menuLabels = async () => {
  await page.click(".controls .chip", 0);
  const items = await page.eval("[...document.querySelectorAll('.menu-item')].map(e => e.textContent).join('|')");
  await page.key("Escape");
  return items;
};
ok("the ChatGPT Plan card counts its models, all shown to begin with", (await mvisCount("chatgpt")) === "3/3");
ok("so does the Devin card", (await mvisCount("devin")) === "4/4");
ok("and every API provider", await page.eval("[...document.querySelectorAll('.mvis[data-source^=\"api:\"]')].length === 3 && [...document.querySelectorAll('.mvis[data-source^=\"api:\"] .mvis-count')].every(e => /^(\\d+)\\s*\\/\\s*\\1$/.test(e.textContent.trim()))"));
ok("the lists are folded away until asked for", await page.eval("!document.querySelector('.mvis-body')"));
await page.click(`${mvis("chatgpt")} .disclose`);
ok("unfolded: a switch for each model, all on", await page.eval(`(() => { const s = [...document.querySelectorAll('${mvis("chatgpt")} .switch')]; return s.length === 3 && s.every(x => x.getAttribute('aria-checked') === 'true'); })()`));
await rowSwitch("chatgpt", "GPT-6-Luna");
ok("turning one off updates the count", (await mvisCount("chatgpt")) === "2/3");
ok("and the switch", await page.eval(`document.querySelectorAll('${mvis("chatgpt")} .switch')[2].getAttribute('aria-checked') === 'false'`));
await page.key("Escape");
ok("the model menu no longer offers it", !(await menuLabels()).includes("GPT-6-Luna"));
await openSettings(1);
await page.click(`${mvis("chatgpt")} .disclose`);
await rowSwitch("chatgpt", "GPT-6.1-Sol"); // the model this conversation is using
await page.key("Escape");
await page.click(".controls .chip", 0);
const inUse = await page.eval("[...document.querySelectorAll('.menu-item')].filter(e => e.textContent.includes('GPT-6.1-Sol')).map(e => e.textContent).join('|')");
await page.key("Escape");
ok("a hidden model that is in use is still offered, saying why", inUse.includes("正在使用所以还显示"), inUse);
await openSettings(1);
await page.click(`${mvis("devin")} .disclose`);
await clickText(`${mvis("devin")} .btn`, "全部隐藏");
ok("hiding all of Devin's models: 0 of 4, the button is spent", (await mvisCount("devin")) === "0/4" && (await page.eval(`[...document.querySelectorAll('${mvis("devin")} .btn')].find(b => b.textContent.includes('全部隐藏')).disabled`)));
await page.key("Escape");
await page.click(".controls .chip", 0);
ok("the menu keeps the DEVIN heading and says nothing is shown (and where to change that)", await page.eval("(() => { const items = [...document.querySelectorAll('.menu-item')].map(e => e.textContent); return !items.some(t => t.includes('SWE-2')) && items.some(t => t.includes('没有显示的模型') && t.includes('账号与模型')); })()"));
ok("that note cannot be picked", await page.eval("[...document.querySelectorAll('.menu-item')].find(e => e.textContent.includes('没有显示的模型')).disabled"));
await page.key("Escape");
// an API provider whose models are all hidden keeps its heading too, with the same note
const apiHeads = () => page.eval("[...document.querySelectorAll('.menu-head')].filter(e => e.textContent.startsWith('API ·')).length");
await page.click(".controls .chip", 0);
const headsBefore = await apiHeads();
await page.key("Escape");
await openSettings(1);
const apiSource = await page.eval("document.querySelector('.mvis[data-source^=\"api:\"]').dataset.source");
await page.click(`${mvis(apiSource)} .disclose`);
await clickText(`${mvis(apiSource)} .btn`, "全部隐藏");
ok("hiding all of an API provider's models: none of them shown", (await mvisCount(apiSource)).startsWith("0/"), await mvisCount(apiSource));
await page.key("Escape");
await page.click(".controls .chip", 0);
ok("the menu keeps that provider's heading (no API group vanishes)", headsBefore === 3 && (await apiHeads()) === headsBefore, [headsBefore, await apiHeads()]);
ok("and says nothing is shown under it, next to Devin's own note (Devin is still all hidden here)", await page.eval("[...document.querySelectorAll('.menu-item')].filter((e) => e.textContent.includes('没有显示的模型') && e.disabled).length === 2"));
await page.key("Escape");
await openSettings(1);
await page.click(`${mvis(apiSource)} .disclose`);
await clickText(`${mvis(apiSource)} .btn`, "全部显示");
await page.key("Escape");
await openSettings(1);
await page.click(`${mvis("chatgpt")} .disclose`);
await clickText(`${mvis("chatgpt")} .btn`, "全部显示");
await page.click(`${mvis("devin")} .disclose`);
await clickText(`${mvis("devin")} .btn`, "全部显示");
ok("show all brings every model back", (await mvisCount("chatgpt")) === "3/3" && (await mvisCount("devin")) === "4/4");
await page.key("Escape");
const everything = await menuLabels();
ok("the menu is whole again", ["GPT-6.1-Sol", "GPT-6-Luna", "SWE-2"].every((m) => everything.includes(m)) && !everything.includes("没有显示的模型") && !everything.includes("正在使用所以还显示"));
ok("no console errors (model lists)", page.consoleErrors().length === 0);
await page.close();

// accounts and models: API Provider list and editor
page = await openPage(`${index}?scene=done&settings=providers&theme=light&review=0`);
ok("three demo providers are listed", (await page.eval("document.querySelectorAll('.prov').length")) === 3);
ok("a key shown in the list is a variable name with its state", await page.eval("(() => { const t = [...document.querySelectorAll('.prov-meta')].map(e => e.textContent); return t[0].includes('环境变量 OPENROUTER_API_KEY') && !t[0].includes('$' + '{') && t[0].includes('已设置'); })()"));
ok("a plaintext key left in settings.json is flagged, and never shown", await page.eval("(() => { const m = document.querySelectorAll('.prov-meta')[1]; return m.textContent.includes('明文') && !!m.querySelector('.warn-text') && !/sk-ant/.test(document.querySelector('.providers').textContent); })()"));
await page.click(".prov .btn", 4); // test Ollama
await page.waitFor("document.querySelector('.prov-status.bad')", 4000);
ok("a failing test says why", await page.eval("document.querySelector('.prov-status.bad').textContent.includes('连不上')"));
await page.click(".prov .btn", 5); // edit Ollama
ok("editing opens the editor on that provider", await page.eval("document.querySelector('.pe-title').textContent === '编辑「Ollama（本机）」'"));
await page.click(".back");
ok("back returns to the list", await page.eval("document.querySelectorAll('.prov').length === 3"));

await page.click(".prov .btn", 1); // edit OpenRouter
await setValue('input[aria-label="名称"]', "");
await page.click(".pe-foot .btn.primary");
ok("saving without a name is refused with a reason", await page.eval("!!document.querySelector('.pe') && document.querySelector('.stack-err')?.textContent.includes('名字')"));
await page.click('input[aria-label="名称"]');
await page.type("OR2");
await page.click(".pe-foot .btn.primary");
await page.waitFor("!document.querySelector('.pe')", 3000);
ok("saving returns to the list with the new name", await page.eval("[...document.querySelectorAll('.prov-name')].map(e => e.textContent).includes('OR2')"));
ok("and says so in the status bar", await page.eval("document.querySelector('.notice-text')?.textContent.includes('已保存「OR2」')"));

await clickText(".pcard-actions .btn", "添加 Provider");
await clickText(".selectbox", "选择预设");
await clickText(".menu-item", "Groq");
ok("a preset fills name, kind, URL and the key variable", await page.eval("document.querySelector('input[aria-label=\"名称\"]').value === 'Groq' && document.querySelector('input[aria-label=\"Base URL\"]').value === 'https://api.groq.com/openai/v1' && document.querySelector('input[aria-label=\"环境变量\"]').value === 'GROQ_API_KEY'"));
await setValue('input[aria-label="Base URL"]', "groq.com");
await page.click(".pe-foot .btn.primary");
ok("a Base URL without http(s):// is refused", await page.eval("document.querySelector('.stack-err')?.textContent.includes('http')"));
await setValue('input[aria-label="Base URL"]', "https://api.groq.com/openai/v1");
ok("there is no place to type a key: only 环境变量 / 命令获取 / 不需要", await page.eval("[...document.querySelectorAll('.pe .segc button')].map(e => e.textContent).join() === '环境变量,命令获取,不需要' && !document.querySelector('.pe input[type=password]')"));
await page.waitFor("document.querySelector('.envline.unset')", 2000);
ok("a variable that is not set says so and gives the setx line to run", await page.eval("document.querySelector('.envline.unset').textContent.includes('setx GROQ_API_KEY')"));
await page.click(".pe-foot .btn:not(.primary)");
await page.waitFor("document.querySelector('.test-res')", 3000);
ok("测试连接 says the variable is not set", await page.eval("document.querySelector('.test-res.bad')?.textContent.includes('GROQ_API_KEY 没有设置')"));
await setValue('input[aria-label="环境变量"]', "OPENAI_API_KEY");
await page.waitFor("document.querySelector('.envline.pending')", 2000);
ok("a variable set after WittyCode started asks for a restart", await page.eval("document.querySelector('.envline.pending').textContent.includes('重启 WittyCode')"));
await setValue('input[aria-label="环境变量"]', "sk-1234 abcd");
await page.click(".pe-foot .btn.primary");
ok("something that is not a variable name (a key pasted by mistake) is refused: the field is a name, not the key", await page.eval("!!document.querySelector('.pe') && document.querySelector('.stack-err')?.textContent.includes('不是密钥本身')"));
await setValue('input[aria-label="环境变量"]', "${OPENAI_API_KEY}");
ok("a pasted ${NAME} is accepted and shown as the bare name", await page.eval("document.querySelector('input[aria-label=\"环境变量\"]').value === 'OPENAI_API_KEY'"));
await setValue('input[aria-label="环境变量"]', "OPENROUTER_API_KEY");
await page.waitFor("document.querySelector('.envline.set')", 2000);
ok("a variable that is set says so", true);
await page.click(".pe-foot .btn:not(.primary)");
await page.waitFor("document.querySelector('.test-res.ok')", 3000);
ok("测试连接 reports success with the time and model count", await page.eval("document.querySelector('.test-res.ok')?.textContent.includes('连接成功')"));
await clickText(".btn.small", "从服务端获取列表");
await page.waitFor("document.querySelectorAll('.id-chip').length > 0", 3000);
const rows = await page.eval("document.querySelectorAll('.mrow:not(.mhead)').length");
await page.click(".id-chip:not(.have)", 0);
ok("a model id listed by the server is added with one click", (await page.eval("document.querySelectorAll('.mrow:not(.mhead)').length")) === rows + 1);
await page.click(".pe-foot .btn.primary");
ok("an unknown API window must be filled before saving", await page.eval("!!document.querySelector('.pe') && document.querySelector('.stack-err')?.textContent.includes('上下文窗口未知')"));
const unknownWindows = await page.eval("document.querySelectorAll('input[aria-label=\"上下文窗口\"]').length");
for (let i = 0; i < unknownWindows; i++) {
  await page.eval(`(() => { const e = document.querySelectorAll('input[aria-label="上下文窗口"]')[${i}]; if (!e.value) { Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, 'value').set.call(e, '64000'); e.dispatchEvent(new Event('input', { bubbles: true })); } })()`);
}
await page.click(".pe-foot .btn.primary");
await page.waitFor("!document.querySelector('.pe')", 3000);
ok("the new provider is listed", (await page.eval("document.querySelectorAll('.prov').length")) === 4);
await page.key("Escape");
await page.click(".controls .chip", 0);
ok("and its models are in the model menu under their own heading", await page.eval("[...document.querySelectorAll('.menu-head > span:first-child')].some(e => e.textContent === 'API · GROQ')"));
await page.key("Escape");

await openSettings(1);
await page.click(".prov .btn", 1); // edit OR2
await clickText(".pe-foot .btn", "删除");
ok("deleting asks first and names what goes away", await page.eval("!!document.querySelector('.dialog') && document.querySelector('.dialog').textContent.includes('models.json')"));
await page.click(".dialog .danger-solid");
await page.waitFor("document.querySelectorAll('.prov').length === 3", 3000);
ok("confirming removes it and returns to the list", await page.eval("![...document.querySelectorAll('.prov-name')].some(e => e.textContent === 'OR2') && !document.querySelector('.pe')"));

await page.click(".prov .btn", 1); // edit Anthropic (now first in the list): its key is plaintext in settings.json
ok("a plaintext key gets a warning with the way out, and no field to show or edit it", await page.eval("(() => { const w = document.querySelector('.pe-warn'); return !!w && w.textContent.includes('明文') && w.textContent.includes('环境变量') && !document.querySelector('.pe input[type=password]') && !document.querySelector('.pe .segc button.on'); })()"));
ok("the Anthropic format offers the Bearer switch", await page.eval("!!document.querySelector('.pe-check .switch')"));
await clickText(".pe .segc button", "环境变量");
ok("switching to 环境变量 starts from the usual name for that service", await page.eval("document.querySelector('input[aria-label=\"环境变量\"]').value === 'ANTHROPIC_API_KEY' && !document.querySelector('.pe-warn')"));
await page.waitFor("document.querySelector('.envline.set')", 2000);
await page.click(".pe-foot .btn.primary");
await page.waitFor("!document.querySelector('.pe')", 3000);
ok("after saving, the list shows the variable and the plaintext warning is gone", await page.eval("(() => { const m = [...document.querySelectorAll('.prov')].find(p => p.textContent.includes('Anthropic（自己的 Key）')).querySelector('.prov-meta'); return m.textContent.includes('环境变量 ANTHROPIC_API_KEY') && !m.querySelector('.warn-text'); })()"));
await page.close();

page = await openPage(`${index}?scene=done&settings=providers&provider=new:bedrock&theme=dark&review=0`);
ok("Bedrock asks for AWS credentials and a region, not a key", await page.eval("document.querySelector('.pe-sec:nth-of-type(2) h4').textContent === 'AWS 凭据' && !!document.querySelector('input[aria-label=\"AWS 区域\"]') && !document.querySelector('input[aria-label=\"Base URL\"]')"));
ok("no console errors (provider editor)", page.consoleErrors().length === 0);
await page.close();

// shared memory: the switches, what agents wrote lately (and undoing it), the notes (search, write, edit, forget, restore)
page = await openPage(`${index}?scene=done&settings=memory&theme=dark&review=0`);
const textarea = (selector, value) =>
  page.eval(`(() => { const e = document.querySelector(${JSON.stringify(selector)}); Object.getOwnPropertyDescriptor(HTMLTextAreaElement.prototype, 'value').set.call(e, ${JSON.stringify(value)}); e.dispatchEvent(new Event('input', { bubbles: true })); })()`);
const noteTitles = () => page.eval("[...document.querySelectorAll('.mem-note .mem-title')].map(e => e.textContent)");
const notice = () => page.eval("document.querySelector('.notice-text')?.textContent ?? ''");
ok("the settings have a memory page, with the number of new notes on its tab", await page.eval("(() => { const n = [...document.querySelectorAll('.snav')].find(e => e.textContent.includes('共享记忆')); return !!n && n.classList.contains('on') && n.querySelector('.snav-badge')?.textContent === '5' && document.querySelector('.settings-body h2').textContent === '共享记忆'; })()"));
ok("the sidebar's memory button carries the same number: two new notes and three notes for the weekly review", await page.eval("document.querySelector('.foot-btn .foot-badge')?.textContent === '5'"));
ok("the switches: on, for WittyHarness, Devin and Factory, and for the two ways notes get written by the application itself", await page.eval("(() => { const on = [...document.querySelectorAll('.settings-body .field .switch')].map((e) => e.getAttribute('aria-checked')); return on.length === 7 && on.every((v) => v === 'true') && [...document.querySelectorAll('.mem-engine')].map((e) => e.textContent.trim()).join() === 'WittyHarness,Devin,Factory'; })()"));
await page.click(".mem-engine .switch", 1);
ok("an engine can be left out", await page.eval("[...document.querySelectorAll('.mem-engine .switch')].map((e) => e.getAttribute('aria-checked')).join() === 'true,false,true'"));
await page.click(".mem-engine .switch", 1);
await page.click('.switch[aria-label="新会话开头带上相关笔记"]');
ok("the notes that bear on a message can be left out of the messages", await page.eval("document.querySelector('.switch[aria-label=\"新会话开头带上相关笔记\"]').getAttribute('aria-checked') === 'false'"));
await page.click('.switch[aria-label="新会话开头带上相关笔记"]');
await page.click('.switch[aria-label="说了记住就直接记下"]');
await page.click('.switch[aria-label="自动记笔记，每周回顾一次"]');
ok("a message that says remember, and the notes the application writes by itself after a turn, can each be switched off", await page.eval("(() => { const s = [...document.querySelectorAll('.settings-body .field .switch')]; return s[5].getAttribute('aria-checked') === 'false' && s[6].getAttribute('aria-checked') === 'false' && /说了「记住」就直接记下/.test(document.querySelectorAll('.settings-body .field')[3].textContent) && /自动记笔记，每周回顾一次/.test(document.querySelectorAll('.settings-body .field')[4].textContent); })()"));
await page.click('.switch[aria-label="说了记住就直接记下"]');
await page.click('.switch[aria-label="自动记笔记，每周回顾一次"]');
await page.click(".settings-body .field .switch", 0);
ok("with the master switch off the engines are gone, and the page says the notes stay", await page.eval("!document.querySelector('.mem-engine') && document.querySelectorAll('.settings-body .field .switch').length === 1 && document.querySelector('.page-note')?.textContent.includes('已关闭') && !!document.querySelector('.mem-note')"));
await page.click(".settings-body .field .switch", 0);
ok("the weekly review waits above what agents wrote: three notes, the least sure first, the sure ones ticked", await page.eval(`(() => { const sec = document.querySelector('.mem-sec[aria-label="每周回顾"]'); const rows = [...document.querySelectorAll('.mem-rev')]; return rows.map((r) => r.querySelector('.mem-title').textContent).join() === '这次先不动样式,e2e 要整文件连跑 20 次,提交信息用中文' && rows.map((r) => r.querySelector('.switch').getAttribute('aria-checked')).join() === 'false,true,true' && sec.textContent.includes('3 条') && (sec.compareDocumentPosition(document.querySelector('.mem-sec[aria-label="最近由代理写入"]')) & Node.DOCUMENT_POSITION_FOLLOWING); })()`));
ok("a note says whose conversation it came from, why it is worth keeping, and how sure the application is", await page.eval("(() => { const m = document.querySelectorAll('.mem-rev')[2].querySelector('.mem-meta').textContent; return m.includes('WittyHarness') && m.includes('测试一下换GPT Plan') && m.includes('用户明确说了') && m.includes('把握 91%'); })()"));
ok("the button says how many stay and how many go", await page.eval("document.querySelector('.mem-rev-foot .btn.primary').textContent.replace(/\\s+/g, '') === '确认：留下2条，丢掉1条'"));
await clickText(".mem-rev-foot .btn", "确认");
await page.waitFor("!document.querySelector('.mem-rev')", 3000);
ok("confirming says what was kept and dropped; only what agents wrote is counted by the tab again", await page.eval("document.querySelector('.mem-lead')?.textContent.includes('保留 2 条，丢弃 1 条') && [...document.querySelectorAll('.snav-badge')].map((e) => e.textContent).join() === '2'"));
ok("what agents wrote lately: five changes, newest first", await page.eval("(() => { const r = [...document.querySelectorAll('.mem-change .mem-change-line')].map((e) => e.textContent.replace(/\\s+/g, ' ')); return r.length === 5 && r[0].startsWith('Devin 忘记了「旧结论') && r[1].startsWith('Devin 记住了「改了 frontend') && r[2].startsWith('Devin') && r[3].startsWith('WittyHarness'); })()"));
ok("the two the user has not looked at are marked", await page.eval("document.querySelectorAll('.mem-change.new').length === 2 && document.querySelector('.mem-new')?.textContent.includes('2 条新的')"));
ok("a change made in a conversation names it as a link", await page.eval("[...document.querySelectorAll('.mem-change .mem-link')].some((e) => e.textContent === '修复缓存')"));
ok("the notes: four in use and one forgotten", (await page.eval("document.querySelector('.mem-count').textContent.replace(/\\s+/g, '')")) === "4·已忘记1" && (await noteTitles()).length === 4);
await page.click(".mem-change .btn", 0); // undo: Devin forgot the old note
await page.waitFor("document.querySelectorAll('.mem-note').length === 5", 3000);
ok("undoing the forget brings the note back", (await noteTitles()).some((t) => t.startsWith("旧结论")) && (await notice()).includes("已撤销"));
await page.click(".mem-head .btn", 0); // 全部标为已看过
await page.waitFor("!document.querySelector('.mem-change.new')", 3000);
ok("marking them as looked at clears the badges and the button", await page.eval("!document.querySelector('.snav-badge') && !document.querySelector('.foot-badge') && !document.querySelector('.mem-new') && document.querySelector('.mem-head .btn').disabled"));

await page.click(".mem-search input");
await page.type("PowerShell");
await page.waitFor("document.querySelectorAll('.mem-note').length === 1", 3000);
ok("searching narrows the list to the matching note", (await noteTitles()).join() === "PowerShell 5.1 不支持 && 和 ||");
await setValue(".mem-search input", "");
await page.waitFor("document.querySelectorAll('.mem-note').length === 5", 3000);
await page.click(".mem-tools .segc button", 2); // 全局
await page.waitFor("document.querySelectorAll('.mem-note').length === 3", 3000);
ok("the scope filter shows only the notes for every project", await page.eval("[...document.querySelectorAll('.mem-note .mem-scope')].every((e) => e.textContent === '全局')"));
await page.click(".mem-tools .segc button", 0);
await page.waitFor("document.querySelectorAll('.mem-note').length === 5", 3000);

await clickText(".mem-note-head", "PowerShell");
ok("a note opens to its text and says where it came from", await page.eval("(() => { const b = document.querySelector('.mem-note.open .mem-note-body'); return !!b && b.textContent.includes('用分号串联') && b.textContent.includes('WittyHarness 写入') && b.textContent.includes('整理测试'); })()"));
ok("and offers 编辑 and 忘记", await page.eval("[...document.querySelectorAll('.mem-note.open .btn')].map((e) => e.textContent.trim()).join() === '编辑,忘记'"));
await clickText(".mem-note.open .btn", "编辑");
ok("the editor takes the focus (so Esc leaves it, not the settings)", await page.eval("document.activeElement?.getAttribute('aria-label') === '标题'"));
await page.key("Escape");
ok("Esc closes the editor and leaves the settings open", await page.eval("!document.querySelector('.mem-editor') && !!document.querySelector('.settings')"));
await clickText(".mem-note-head", "PowerShell");

await clickText(".mem-head .btn", "新建笔记");
ok("the new-note form starts as a note of the current project, and cannot be saved empty", await page.eval("(() => { const on = [...document.querySelectorAll('.mem-create .segc button.on')].map((e) => e.textContent); return on.includes('这个项目') && document.querySelector('.mem-create .btn.primary').disabled; })()"));
await setValue('.mem-create input[aria-label="标题"]', "提交信息用英文");
await textarea('.mem-create textarea[aria-label="内容"]', "git commit 的说明用英文，一行不超过 72 个字符。");
await clickText(".mem-create .segc button", "偏好");
await page.click(".mem-create .btn.primary");
await page.waitFor("[...document.querySelectorAll('.mem-note .mem-title')].some((e) => e.textContent === '提交信息用英文')", 3000);
ok("a saved note is in the list, open, and written by me", await page.eval("(() => { const n = [...document.querySelectorAll('.mem-note')].find((e) => e.querySelector('.mem-title').textContent === '提交信息用英文'); return n.classList.contains('open') && n.querySelector('.mem-kind').textContent === '偏好' && n.textContent.includes('我写入') && n.querySelector('.mem-scope').textContent !== '全局' && !document.querySelector('.mem-create'); })()"));
await clickText(".mem-note.open .btn", "编辑");
await setValue('.mem-note.open input[aria-label="标题"]', "提交信息用英文（72 字符）");
await clickText(".mem-editor .btn", "保存");
await page.waitFor("[...document.querySelectorAll('.mem-note .mem-title')].some((e) => e.textContent === '提交信息用英文（72 字符）')", 3000);
ok("editing saves the new title", (await notice()).includes("已保存"));
await clickText(".mem-note.open .btn", "忘记");
await page.waitFor("![...document.querySelectorAll('.mem-note .mem-title')].some((e) => e.textContent.startsWith('提交信息用英文'))", 3000);
ok("forgetting takes it out of the list, and the count remembers it", (await page.eval("document.querySelector('.mem-count').textContent.replace(/\\s+/g, '')")) === "5·已忘记1");
await page.click(".mem-toggle .switch");
await page.waitFor("!!document.querySelector('.mem-note.forgotten')", 3000);
ok("显示已忘记 lists it, struck through, with 已忘记 on it", await page.eval("(() => { const n = document.querySelector('.mem-note.forgotten'); return n.textContent.includes('提交信息用英文') && n.textContent.includes('已忘记'); })()"));
await page.click(".mem-note.forgotten .mem-note-head");
await clickText(".mem-note.open .btn", "恢复");
await page.waitFor("!document.querySelector('.mem-note.forgotten')", 3000);
ok("恢复 brings it back as a note in use", (await noteTitles()).some((t) => t.startsWith("提交信息用英文")));

// the same notes as a star map: every note is a star, a star opens its note under the map
const listed = await page.eval("document.querySelectorAll('.mem-note').length");
ok("the notes can be drawn as a star map", await page.eval("[...document.querySelectorAll('.mem-head-btns .segc button')].map((e) => e.textContent.trim()).join() === '列表,星图'"));
await clickText(".mem-head-btns .segc button", "星图");
await page.waitFor("!!document.querySelector('.sm-panel')", 3000);
ok("every note in the list is a star", await page.eval(`document.querySelectorAll('.sm-star').length === ${listed}`));
ok("the map fills the notes area, not a sliver of it", await page.eval("(() => { const r = document.querySelector('.sm-panel').getBoundingClientRect(); return r.width > 400 && r.height >= 440; })()"));
const starId = await page.eval("[...document.querySelectorAll('.sm-star')].find((s) => s.getAttribute('data-id'))?.getAttribute('data-id') ?? ''");
await page.click(`.sm-star[data-id="${starId}"]`);
await page.waitFor(`!!document.querySelector('.mem-note.open[data-id="${starId}"]')`, 3000);
ok("clicking a star rings it and opens that note under the map", await page.eval(`!!document.querySelector('.sm-star[data-id="${starId}"].sm-selected') && !!document.querySelector('.mem-note.open[data-id="${starId}"] .mem-note-body')`));
await clickText(".mem-head-btns .segc button", "列表");
await page.waitFor(`!document.querySelector('.sm-panel') && document.querySelectorAll('.mem-note').length === ${listed}`, 3000);
ok("列表 brings the plain list back", await page.eval("!document.querySelector('.sm-panel')"));
ok("no console errors (memory)", page.consoleErrors().length === 0);
ok("nothing was blocked by the Content-Security-Policy (memory)", page.cspViolations().length === 0);
await page.close();

// a review: a note the user changes before keeping it, and keeping everything
page = await openPage(`${index}?scene=done&settings=memory&theme=light&review=0`);
await clickText(".mem-rev .btn", "改一改");
ok("改一改 opens the editor with the note's words and a button that says 保存修改", await page.eval("(() => { const e = document.querySelector('.mem-rev .mem-editor'); return !!e && e.querySelector('input[aria-label=\"标题\"]').value === '这次先不动样式' && e.querySelector('.btn.primary').textContent.trim() === '保存修改' && document.activeElement?.getAttribute('aria-label') === '标题'; })()"));
await setValue('.mem-rev input[aria-label="标题"]', "这次只改逻辑");
await page.click(".mem-rev .mem-editor .btn.primary");
await page.waitFor("!document.querySelector('.mem-rev .mem-editor')", 3000);
ok("the changed words show in the review, marked as changed, and the note is ticked", await page.eval("(() => { const r = document.querySelector('.mem-rev'); return r.querySelector('.mem-title').textContent === '这次只改逻辑' && r.querySelector('.mem-meta').textContent.includes('已改') && r.querySelector('.switch').getAttribute('aria-checked') === 'true'; })()"));
await clickText(".mem-rev-foot .btn", "确认");
await page.waitFor("!document.querySelector('.mem-rev')", 3000);
ok("all three were kept: the summary says so and nothing is left waiting", await page.eval("document.querySelector('.mem-lead')?.textContent.includes('保留 3 条，丢弃 0 条') && !document.querySelector('.mem-sec[aria-label=\"每周回顾\"] .mem-new')"));
ok("no console errors (weekly review)", page.consoleErrors().length === 0);
await page.close();

// importing what other applications keep: the preview, the ticked ones, the undo
page = await openPage(`${index}?scene=done&settings=memory&theme=dark&review=0`);
await clickText(".mem-head .btn", "从其他应用导入…");
await page.waitFor("document.querySelectorAll('.imp-source').length === 4", 3000);
ok("the wizard says which apps this computer has, and how much each brings", await page.eval("(() => { const s = [...document.querySelectorAll('.imp-source')].map((e) => e.textContent.replace(/\\s+/g, ' ')); return s[0] === 'Claude Code记忆 4 条 · 会话 3 个' && s[3].includes('没找到') && document.querySelectorAll('.imp-source input:disabled').length === 1; })()"));
await page.waitFor("document.querySelectorAll('.imp-item').length === 8", 3000);
ok("their memory files come as notes to tick; the one the memory already holds is marked and left unticked", await page.eval("(() => { const items = [...document.querySelectorAll('.imp-item')]; const has = items.find((e) => e.textContent.includes('提交前跑测试')); return items.length === 8 && has.classList.contains('has') && has.querySelector('input').checked === false && items.filter((e) => e.querySelector('input').checked).length === 7; })()"));
await page.click(".imp-item.has input"); // (ticking it anyway: the wizard says the memory already holds it)
await clickText(".imp-tabs .segc button", "会话");
await page.waitFor("document.querySelectorAll('.imp-item').length === 6", 3000);
ok("their conversations are listed, the one brought in before marked", await page.eval("(() => { const items = [...document.querySelectorAll('.imp-item')]; const before = items.find((e) => e.textContent.includes('demo-sandbox 测试项目')); return items.length === 6 && !!before && before.textContent.includes('导入过') && items.filter((e) => e.querySelector('input').checked).length === 0; })()"));
await page.click(".imp-item input", 3);
await clickText(".imp-foot .btn.primary", "导入所选");
await page.waitFor("!!document.querySelector('.imp-done')", 3000);
ok("confirming makes the notes and the conversation, and says what was left out", await page.eval("(() => { const d = document.querySelector('.imp-done').textContent.replace(/\\s+/g, ' '); return d.includes('已导入 7 条笔记、1 个会话') && d.includes('跳过 1 个') && d.includes('提交前跑测试'); })()"));
await clickText(".imp-done .btn", "撤销这次导入的笔记");
await page.waitFor("!document.querySelector('.imp-done .btn')", 3000);
ok("undo takes the imported notes back (they stay in 显示已忘记)", await page.eval("(() => { const d = document.querySelector('.imp-done').textContent; return !d.includes('已导入 7') && (document.querySelector('.notice-text')?.textContent ?? '').includes('已撤销这次导入的 7 条笔记'); })()"));
ok("no console errors (import)", page.consoleErrors().length === 0);
await page.close();

// the title bar is part of the page: the window has no frame of its own
page = await openPage(`${index}?scene=live&theme=dark&review=0`);
ok("a session that has not measured a speed yet reads TPS —", await page.eval("/TPS —/.test(document.querySelector('.st-right').innerText.replace(/\\s+/g, ' '))"));
ok("the app icon and name appear once, in the title bar", await page.eval("document.querySelectorAll('.app-icon').length === 1 && !!document.querySelector('.titlebar .app-icon') && document.querySelector('.titlebar .tb-title').textContent === 'WittyCode'"));
ok("the sidebar starts with 新会话, with no second icon or name above it", await page.eval("!document.querySelector('.sidebar .brand') && document.querySelector('.sidebar').firstElementChild.classList.contains('side-top')"));
ok("the title bar is a full-width row above the app", await page.eval("(() => { const t = document.querySelector('.titlebar').getBoundingClientRect(); const a = document.querySelector('.app').getBoundingClientRect(); return Math.round(t.width) === Math.round(a.width) && t.bottom <= a.top + 1 && Math.round(t.height) === 36; })()"));
ok("the window buttons are 最小化 / 最大化 / 关闭", await page.eval("[...document.querySelectorAll('.titlebar .tb-btn')].map(e => e.getAttribute('aria-label')).join() === '最小化,最大化,关闭'"));
ok("the 审阅栏 button does not sit on the window buttons", await page.eval("document.querySelector('.review-show').getBoundingClientRect().right <= document.querySelector('.titlebar .tb-btn').getBoundingClientRect().left"));
await page.click(".titlebar .tb-drag");
ok("pressing on the empty stretch asks the system to move the window", (await page.eval("window.__moves ?? 0")) === 1);
await page.click(".titlebar .tb-menu");
ok("pressing the menu button does not", (await page.eval("window.__moves ?? 0")) === 1);
ok("the app menu is in the title bar", await page.eval("[...document.querySelectorAll('.menu-label')].map(e => e.textContent).includes('设置…')"));
await page.key("Escape");
await page.click(".titlebar .tb-btn", 1);
ok("maximize turns into 还原", await page.eval("document.querySelectorAll('.titlebar .tb-btn')[1].getAttribute('aria-label') === '还原'"));
await page.eval("document.querySelector('.titlebar .tb-drag').dispatchEvent(new MouseEvent('dblclick', { bubbles: true }))");
await page.waitFor("document.querySelectorAll('.titlebar .tb-btn')[1].getAttribute('aria-label') === '最大化'", 1000);
ok("double-clicking the title bar toggles it back", true);
await page.click(".titlebar .tb-btn", 2);
ok("closing while a session is still working asks first", await page.eval("!!document.querySelector('.dialog') && document.querySelector('.dialog').textContent.includes('退出 WittyCode')"));
await page.key("Escape");
ok("Esc keeps the window open", await page.eval("!document.querySelector('.dialog')"));
ok("no console errors (title bar)", page.consoleErrors().length === 0);
await page.close();

// a file the text names: a click shows it in the side panel, Ctrl+click uses the system's program, a right click offers the rest
page = await openPage(`${index}?scene=done&theme=light&review=0`);
const refs = "[...document.querySelectorAll('.text .pathref')].map(e => e.textContent)";
ok("paths in the text are links: inline code and a markdown link; other code is not", JSON.stringify(await page.eval(refs)) === JSON.stringify(["README.txt", "hello.txt", "docs/handoff.md:3-8", "the design"]));
ok("no panel before a click", await page.eval("!document.querySelector('.filepanel')"));
await page.click(".text .pathref", 2);
await page.waitFor("!!document.querySelector('.filepanel .fp-rows')", 4000);
ok("a click on a path opens the file in the side panel", await page.eval("document.querySelector('.fp-name').textContent === 'handoff.md'"));
ok("the panel sits to the right of the conversation and the conversation stays", await page.eval("(() => { const p = document.querySelector('.filepanel').getBoundingClientRect(), m = document.querySelector('.main').getBoundingClientRect(); return p.left >= m.right - 1 && p.width >= 340 && m.width > 300 && p.height > 300; })()"));
ok("the lines the path named are marked (3-8: six lines from the third)", await page.eval("(() => { const m = document.querySelector('.fp-mark'); return !!m && m.style.top === '48px' && m.style.height === '120px'; })()"));
ok("the gutter numbers the lines and lines up with the text", await page.eval("(() => { const g = document.querySelector('.fp-gutter'), t = document.querySelector('.fp-text'); return g.textContent.split('\\n').length === t.textContent.split('\\n').length && Math.abs(g.getBoundingClientRect().height - t.getBoundingClientRect().height) < 1; })()"));
ok("a Markdown file named with lines opens as its source, with a way to read it as a document", await page.eval("!!document.querySelector('.fp-rows') && document.querySelector('.fp-facts .linkbtn').textContent === '看文档'"));
await page.click(".fp-facts .linkbtn");
ok("看文档 shows it as a document, the path inside it still a link", await page.eval("!document.querySelector('.fp-rows') && !!document.querySelector('.fp-doc h1') && !!document.querySelector('.fp-doc .pathref')"));
await page.click(".fp-facts .linkbtn");
ok("看源码 goes back", await page.eval("!!document.querySelector('.fp-rows')"));
await page.click(".text .pathref", 0, { ctrl: true });
await page.waitFor("document.querySelector('.notice-text')?.textContent.includes('用默认程序打开：README.txt')", 3000);
ok("Ctrl+click uses the default program and leaves the panel as it was", await page.eval("document.querySelector('.fp-name').textContent === 'handoff.md'"));
await page.click(".text .pathref", 1, { right: true });
ok("a right click offers five ways to open it", await page.eval("[...document.querySelectorAll('.menu-item')].map(e => e.textContent.replace(/Ctrl\\+点击/, '').trim()).join('|') === '在右侧打开|用默认程序打开|选择打开方式…|在文件夹中显示|复制路径'"));
await page.eval("[...document.querySelectorAll('.menu-item')].find(e => e.textContent.includes('在右侧打开')).click()");
await page.waitFor("document.querySelector('.fp-name')?.textContent === 'hello.txt'", 3000);
ok("在右侧打开 replaces the file in the panel", await page.eval("!document.querySelector('.menu') && !document.querySelector('.fp-mark')"));
await page.click(".fp-head .icon-btn[aria-label='选择打开方式']");
ok("the panel's own buttons choose the program too", await page.eval("document.querySelector('.notice-text')?.textContent.includes('选择打开方式')"));
await page.key("Escape");
ok("Esc closes the panel", await page.eval("!document.querySelector('.filepanel')"));
await page.click(".text .pathref", 3);
await page.waitFor("!!document.querySelector('.filepanel .fp-name')", 3000);
await page.click(".fp-head .icon-btn[aria-label='关闭文件面板']");
ok("the close button closes it", await page.eval("!document.querySelector('.filepanel')"));
ok("no console errors (file panel)", page.consoleErrors().length === 0);
await page.close();

// the lightbox
page = await openPage(`${index}?scene=long&theme=dark&review=0`);
await page.click(".att-img");
ok("a picture opens the lightbox", await page.eval("!!document.querySelector('.lightbox img')"));
await page.key("Escape");
ok("Esc closes the lightbox", await page.eval("!document.querySelector('.lightbox')"));

// scrolling: a long answer does not stick to the end after the user scrolls up
await page.eval("document.querySelector('.scroller').scrollTop = 0");
await page.wait(200);
ok("scrolling up shows the jump button", await page.eval("!!document.querySelector('.jump')"));
await page.click(".jump");
await page.wait(600);
ok("the jump button goes to the end and hides", await page.eval("!document.querySelector('.jump')"));
await page.eval("document.querySelector('.scroller').scrollTop = 0");
await page.wait(200);
await page.click(".titlebar .tb-menu");
await clickText(".menu-item", "跳到最新消息");
await page.wait(800);
ok("跳到最新消息 in the app menu goes to the end too", await page.eval("(() => { const s = document.querySelector('.scroller'); return s.scrollHeight - s.scrollTop - s.clientHeight < 48 && !document.querySelector('.jump'); })()"));
ok("nothing was blocked by the Content-Security-Policy (long answer, pictures, fonts)", page.cspViolations().length === 0);

const errors = page.consoleErrors();
ok("no console errors", errors.length === 0);
await page.close();
await import("./check-frame-language.mjs");
await import("./check-plan.mjs");
await import("./check-usage-plan-updates.mjs");
await import("./check-turn-navigation.mjs");
await import("./check-step-limit.mjs");
await import("./check-connections.mjs");
console.log(failed ? `${failed} check(s) FAILED` : "all checks passed");
process.exit(failed ? 1 : 0);
