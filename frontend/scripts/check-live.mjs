// The live app in a browser, the way WittyCode's Qt window runs it: Qt's qwebchannel.js and a scripted fake of the
// Python side behind qt.webChannelTransport (scripts/fake-backend.mjs), real mouse and keyboard events (scripts/cdp.mjs).
// `node scripts/check-live.mjs` after `npm run build`. Screenshots go to shots/live-*.png.
import { mkdirSync } from "node:fs";
import { resolve } from "node:path";
import { pathToFileURL } from "node:url";
import { isDeepStrictEqual } from "node:util";
import { openPage } from "./cdp.mjs";
import { fakeBackendScripts } from "./fake-backend.mjs";

const index = pathToFileURL(resolve("dist/index.html")).href;
/** the address Python gives the page on Windows 11: it asks for the window outline with round corners */
const pageUrl = `${index}?frame=round`;
mkdirSync(resolve("shots"), { recursive: true });
let failed = 0;
const ok = (name, v, detail) => {
  if (!v) failed++;
  console.log(v ? "PASS" : "FAIL", name, !v && detail !== undefined ? `\n     got: ${JSON.stringify(detail)}` : "");
};
/** the current conversation is open and idle (its tab's dot; the status bar may be showing a notice instead) */
const READY = "!!document.querySelector('.tab.current .dot-ready')";
const MOCK = ["File operations task", "重构 session_manager", "订单库存查询改批量", "React 迁移方案", "demo@example.com", "OpenRouter", "Ollama（本机）", "inventory-api", "（演示）", "设计审阅"];
const CTRL = 2;

const { scripts, client } = fakeBackendScripts({ initDelay: 2500, carryFiles: 1 });
console.log(`QWebChannel client: ${client}`);
const page = await openPage(pageUrl, { init: scripts, width: 1440, height: 900 });

const js = (expression) => page.eval(expression);
const calls = (method) => js(`__fake.callsOf(${JSON.stringify(method)})`);
const lastCall = async (method) => (await calls(method)).at(-1);
const waitCalls = (method, n = 1, timeout = 5000) => page.waitFor(`__fake.callsOf(${JSON.stringify(method)}).length >= ${n}`, timeout);
const emit = (name, payload) => js(`__fake.emit(${JSON.stringify(name)}, ${JSON.stringify(payload)})`);
const noMock = () => js(`(() => { const t = document.body.innerText; return ${JSON.stringify(MOCK)}.filter((m) => t.includes(m)); })()`);
const notice = () => js("document.querySelector('.notice-text')?.textContent ?? ''");
const shot = (name) => page.shot(resolve(`shots/live-${name}.png`));
const clickText = async (selector, text) => {
  const i = await js(`[...document.querySelectorAll(${JSON.stringify(selector)})].findIndex(e => e.textContent.includes(${JSON.stringify(text)}))`);
  if (i < 0) throw new Error(`no ${selector} containing "${text}"`);
  await page.click(selector, i);
};
const press = (key, code, vk, modifiers = 0) => page.key(key, { code, vk, modifiers });
const blur = () => js("document.activeElement && document.activeElement.blur()");
const setInput = (selector, value) =>
  js(`(() => { const e = document.querySelector(${JSON.stringify(selector)}); const proto = e instanceof HTMLTextAreaElement ? HTMLTextAreaElement.prototype : HTMLInputElement.prototype; Object.getOwnPropertyDescriptor(proto, 'value').set.call(e, ${JSON.stringify(value)}); e.dispatchEvent(new Event('input', { bubbles: true })); })()`);
/** a real ClipboardEvent on the message box, carrying files made in the page */
const paste = (filesJs) =>
  js(`(async () => { const files = await (${filesJs})(); const dt = new DataTransfer(); for (const f of files) dt.items.add(f); document.querySelector('.input').dispatchEvent(new ClipboardEvent('paste', { clipboardData: dt, bubbles: true, cancelable: true })); return true; })()`);
const PNG = `() => new Promise((res) => { const c = document.createElement('canvas'); c.width = 64; c.height = 40; c.getContext('2d').fillRect(0, 0, 64, 40); c.toBlob((b) => res([new File([b], 'shot.png', { type: 'image/png' })])); })`;

// ------------------------------------------------------------------------------------- 1. before app.init answers
ok("the page talks QWebChannel: app.init is its first call", (await js("__fake.calls[0]?.method")) === "app.init");
ok("Qt's client connected to backend.reply and backend.event first", await js("__fake.wire.filter(([who, type]) => who === 'page' && type === 7).length === 2"));
ok("while it connects there is only the title bar", await js("!!document.querySelector('.titlebar') && !document.querySelector('.sidebar, .statusbar, .composer, .tabbar')"));
ok("…without the app menu (nothing to offer yet)", await js("!document.querySelector('.titlebar .tb-menu') && document.querySelectorAll('.titlebar .tb-btn').length === 3"));
ok("no demo data while connecting", (await noMock()).length === 0, await noMock());
ok("the window's outline is there from the first frame (Python asked for it in the address)", await js("!!document.querySelector('.frame-line.round')"));
await shot("connecting");
await page.waitFor("!!document.querySelector('.sidebar')", 8000);

// ------------------------------------------------------------------------------------- 2. the empty app
ok("app.init was called once", (await calls("app.init")).length === 1);
ok("no review bar and no 审阅栏 button", await js("!document.querySelector('.reviewbar') && !document.querySelector('.review-show') && !document.querySelector('.review-root.reviewing')"));
ok("the project Python sent is in the sidebar", await js("document.querySelector('.sidebar').textContent.includes('demo-project') && document.querySelector('.sidebar').textContent.includes('second-project')"));
ok("nothing is open: the welcome page and 没有打开的会话", await js("!!document.querySelector('.emptypage') && document.querySelector('.statusbar').textContent.includes('没有打开的会话') && document.querySelectorAll('.tab').length === 0"));
ok("the theme and the sidebar width are the ones in Python's settings", await js("document.documentElement.dataset.theme === 'dark' && getComputedStyle(document.querySelector('.app')).getPropertyValue('--side-w').includes('300px')"));
await page.waitFor("document.querySelectorAll('.srow').length === 3", 4000);
ok("no demo data: only what Python sent", (await noMock()).length === 0, await noMock());
ok("a saved row's time comes from modifiedAt, worded by the page", await js("[...document.querySelectorAll('.srow')].find(r => r.textContent.includes('整理发布说明'))?.querySelector('.ssub').textContent === '5 分钟前'"), await js("[...document.querySelectorAll('.srow')].map(r => r.querySelector('.ssub').textContent)"));
ok("a saved Devin session says · Devin", await js("[...document.querySelectorAll('.srow')].find(r => r.textContent.includes('迁移脚本'))?.querySelector('.ssub').textContent === '40 分钟前 · Devin'"), await js("[...document.querySelectorAll('.srow')].map(r => r.querySelector('.ssub').textContent)"));
ok("Python's own wording of the time is not used", await js("!document.body.innerText.includes('Python 的旧写法')"));
await page.waitFor("document.querySelectorAll('.controls .chip-v')[0]?.textContent === 'Claude Haiku 5.5'", 4000);
ok("the defaults come from the models event", true);
await shot("empty");

// ------------------------------------------------------------------------------------- 3. the model menu
await page.click(".controls .chip", 0);
ok("the provider's models are under its own heading, with the address", await js("(() => { const h = [...document.querySelectorAll('.menu-head')].find(e => e.textContent.includes('API · GATEWAY')); return !!h && h.textContent.includes('gateway.example.com'); })()"), await js("[...document.querySelectorAll('.menu-head')].map(e => e.textContent)"));
ok("a disabled model is not offered", await js("![...document.querySelectorAll('.menu-item')].some(e => e.textContent.includes('GLM Legacy'))"));
ok("the provider's models are listed in Python's order", await js("(() => { const rows = [...document.querySelector('.menu').children].map(e => e.querySelector?.('.menu-label')?.textContent).filter(Boolean); return rows.join('|').includes('Claude Haiku 5.5|Claude Opus 5.5|GPT-5.4|Gemini 3 Pro'); })()"));
ok("the pinned footer is still there", await js("[...document.querySelectorAll('.menu-pinned .menu-label')].map(e => e.textContent).join() === '添加 API Provider…,管理账号与模型…'"));
ok("Devin is still being checked: 正在检测…, and no login yet", await js("(() => { const h = [...document.querySelectorAll('.menu-head')].find(e => e.textContent.includes('DEVIN')); return !!h && h.textContent.includes('正在检测…') && ![...document.querySelectorAll('.menu-item')].some(e => e.textContent.includes('登录 Devin')); })()"));
await shot("model-menu");
await page.key("Escape");

// reasoning levels come from the model's efforts, with words for none and dynamic
await page.click(".controls .chip", 0);
await clickText(".menu-item", "GPT-5.4");
await waitCalls("session.choose");
ok("without a session the model menu changes the defaults (session.choose without id)", isDeepStrictEqual(await lastCall("session.choose"), { field: "model", value: "api:gw:gpt-5.4" }), await lastCall("session.choose"));
await page.waitFor("document.querySelectorAll('.controls .chip-v')[0].textContent === 'GPT-5.4'", 3000);
await page.click(".controls .chip", 1);
ok("GPT-5.4 offers 无 / Low / Medium / High", (await js("[...document.querySelectorAll('.menu-label')].map(e => e.textContent).join()")) === "无,Low,Medium,High", await js("[...document.querySelectorAll('.menu-label')].map(e => e.textContent).join()"));
await page.key("Escape");
await page.click(".controls .chip", 0);
await clickText(".menu-item", "Gemini 3 Pro");
await page.waitFor("document.querySelectorAll('.controls .chip-v')[0].textContent === 'Gemini 3 Pro'", 3000);
await page.click(".controls .chip", 1);
ok("Gemini offers 动态 / Low / High", (await js("[...document.querySelectorAll('.menu-label')].map(e => e.textContent).join()")) === "动态,Low,High");
await clickText(".menu-item", "动态");
await page.waitFor("document.querySelectorAll('.controls .chip-v')[1].textContent === '动态'", 3000);
ok("and 动态 can be chosen", true);
await page.click(".controls .chip", 0);
await clickText(".menu-item", "Claude Haiku 5.5");
await page.waitFor("document.querySelectorAll('.controls .chip-v')[0].textContent === 'Claude Haiku 5.5'", 3000);

// ------------------------------------------------------------------------------------- 4. accounts
await blur();
await press(",", "Comma", 188, CTRL);
await page.waitFor("!!document.querySelector('.settings')", 2000);
await page.click(".snav", 1);
ok("Devin card: 正在检测…, no login button yet", await js("(() => { const c = document.querySelector('.pcard:nth-child(2)'); return c.textContent.includes('正在检测…') && !c.querySelector('.pcard-actions .btn'); })()"));
ok("the cards are ChatGPT Plan, Devin and the API providers: no other program's", await js("(() => { const c = [...document.querySelectorAll('.providers .pcard')]; return c.length === 3 && c[0].textContent.includes('ChatGPT Plan') && !/Factory|Codex|Droid/.test(document.querySelector('.providers').textContent); })()"));
await shot("accounts-checking");
await emit("accounts", { devin: { cli: "found", auth: "signed-out", enabled: true, version: "3000.11.238", cliPath: "C:\\Users\\dev\\AppData\\Local\\Programs\\devin\\devin.exe" } });
await page.waitFor("document.querySelector('.pcard:nth-child(2)').textContent.includes('未登录')", 2000);
ok("once Devin answers, signed out: the login button appears", await js("document.querySelector('.pcard:nth-child(2) .btn.primary')?.textContent.includes('登录 Devin')"));
await page.click(".pcard:nth-child(2) .btn.primary");
await waitCalls("devin.login");
await page.waitFor("document.querySelector('.pcard:nth-child(2)').textContent.includes('已登录 · dev@example.com · Devin Pro')", 3000);
ok("signing in goes through devin.login and shows the account", true);

// ChatGPT Plan: the user's plan run by WittyCode's own harness. The account came with app.init (read from files, no waiting).
ok("ChatGPT Plan card: signed out, with OpenAI's wording on the button", await js("(() => { const c = document.querySelector('.pcard:nth-child(1)'); return c.textContent.includes('ChatGPT Plan') && c.textContent.includes('未登录') && c.querySelector('.btn.chatgpt-btn')?.textContent.trim() === 'Continue with ChatGPT'; })()"));
await page.key("Escape");
await page.click(".controls .chip", 0);
ok("the menu has a CHATGPT PLAN group that offers Continue with ChatGPT instead of models", await js("(() => { const h = [...document.querySelectorAll('.menu-head')].find(e => e.textContent.includes('CHATGPT PLAN')); const items = [...document.querySelectorAll('.menu-item')].map(e => e.textContent); return !!h && h.textContent.includes('未登录') && items.some(t => t.includes('Continue with ChatGPT')) && !items.some(t => t.includes('GPT-6.1-Sol')); })()"));
await clickText(".menu-item", "Continue with ChatGPT");
await waitCalls("chatgpt.login");
ok("the menu's item starts chatgpt.login without forcing the consent screen", isDeepStrictEqual(await lastCall("chatgpt.login"), { forceConsent: false }), await lastCall("chatgpt.login"));
await page.waitFor("!!document.querySelector('.chatgpt-welcome')", 3000);
ok("the first sign-in is met with 'You're using your ChatGPT Plan' and a Manage usage link", await js("(() => { const d = document.querySelector('.chatgpt-welcome'); return d.textContent.includes(\"You're using your ChatGPT Plan\") && d.querySelector('a')?.getAttribute('href') === 'https://chatgpt.com/settings/usage'; })()"));
await page.click(".chatgpt-welcome .btn.primary");
await waitCalls("chatgpt.welcomed");
ok("Got it tells Python, and the note is gone", await js("!document.querySelector('.chatgpt-welcome')"));
await page.click(".controls .chip", 0);
ok("now the menu lists the plan's models under the account", await js("(() => { const h = [...document.querySelectorAll('.menu-head')].find(e => e.textContent.includes('CHATGPT PLAN')); return !!h && h.textContent.includes('dev@example.com') && [...document.querySelectorAll('.menu-item')].some(e => e.textContent.includes('GPT-6.1-Sol')); })()"), await js("[...document.querySelectorAll('.menu-head')].map(e => e.textContent)"));
ok("with no session the controls are the defaults: no 'Using ChatGPT Plan' yet", await js("!document.querySelector('.plan-badge')"));
await page.key("Escape");
await blur();
await press(",", "Comma", 188, CTRL);
await page.waitFor("!!document.querySelector('.settings')", 2000);
await page.click(".snav", 1);
ok("the card shows the account and where to manage the usage", await js("(() => { const c = document.querySelector('.pcard:nth-child(1)'); return c.textContent.includes('已登录 · dev@example.com') && c.querySelector('a.inline-link')?.getAttribute('href') === 'https://chatgpt.com/settings/usage'; })()"));
await clickText(".pcard:nth-child(1) .btn", "退出登录");
await waitCalls("chatgpt.logout");
ok("signing out is chatgpt.logout, for the active account", isDeepStrictEqual(await lastCall("chatgpt.logout"), {}), await lastCall("chatgpt.logout"));
await page.waitFor("document.querySelector('.pcard:nth-child(1)').textContent.includes('未登录')", 3000);
ok("and the card is signed out again", true);
await page.key("Escape");

// ------------------------------------------------------------------------------------- 5. the first message box: no conversation until a message is sent
ok("the message box is there from the start, with the project chosen on the line under the box", await js("(() => { const t = document.querySelector('.input'); return !t.disabled && [...document.querySelectorAll('.controls .chip-k')].map(e => e.textContent).join(',') === '模式' && document.querySelector('.hint .chip-k').textContent === '项目'; })()"));
ok("the project chip names the first project (nothing was used before)", await js("document.querySelector('.hint .chip-v').textContent === 'demo-project'"));
ok("no context meter before there is a conversation", await js("!document.querySelector('.ctxmeter')"));
await page.click(".newbtn");
ok("新会话 only shows the message box: no session.new, no tab", (await calls("session.new")).length === 0 && (await js("document.querySelectorAll('.tab').length")) === 0, await calls("session.new"));
await page.click(".input");
await page.type("hello");
ok("what is typed stays in the first message box", (await js("document.querySelector('.input').value")) === "hello" && (await calls("session.new")).length === 0);
await page.key("Enter");
await waitCalls("session.new");
ok("the first message opens the conversation, in the project (session.new with its projectId)", isDeepStrictEqual(await lastCall("session.new"), { projectId: "D:\\Work\\demo-project" }), await lastCall("session.new"));
await page.waitFor("document.querySelectorAll('.tab').length === 1 && !!document.querySelector('.tab.current')", 3000);
ok("the tab appears and is shown", true);

// ------------------------------------------------------------------------------------- 6. send, stream, approve
await waitCalls("session.send");
ok("the composer sends session.send with the text", isDeepStrictEqual(await lastCall("session.send"), { id: "c1", text: "hello", attachmentIds: [] }), await lastCall("session.send"));
await page.waitFor("document.querySelector('.input').value === ''", 3000);
ok("and the message box is empty again", true);
await page.waitFor("[...document.querySelectorAll('.block.user .body')].some(e => e.textContent === 'hello')", 3000);
ok("the message shows in the conversation", true);
await page.waitFor("document.querySelector('.text.streaming')?.textContent.length > 0", 3000);
const early = await js("document.querySelector('.text.streaming').textContent.length");
await page.wait(350);
const later = await js("document.querySelector('.text.streaming, .block.text-block .text')?.textContent.length ?? 0");
ok("the answer streams in: the text grows while the turn runs", later > early, [early, later]);
await page.waitFor("!!document.querySelector('.prompt.perm')", 5000);
ok("the permission card appears", await js("document.querySelector('.prompt.perm').textContent.includes('创建文件 hello.txt')"));
ok(
  "its answers are one per line",
  await js("(() => { const r = [...document.querySelectorAll('.prompt.perm .pbtns .btn')].map(e => e.getBoundingClientRect()); return r.length > 1 && r.every((b, i) => i === 0 || (b.top >= r[i - 1].bottom - 1 && Math.abs(b.left - r[0].left) < 1)); })()"),
  await js("[...document.querySelectorAll('.prompt.perm .pbtns .btn')].map(e => { const b = e.getBoundingClientRect(); return [e.textContent, Math.round(b.left), Math.round(b.top)]; })"),
);
ok("the turn and the tab say it is waiting", await js("document.querySelector('.turnfoot.live')?.textContent.includes('等待你的确认') && !!document.querySelector('.tab.current .dot-attention')"), await js("[document.querySelector('.turnfoot')?.textContent, document.querySelector('.tab.current').innerHTML]"));
await shot("permission");
const promptId = await js("__fake.events.filter(([n]) => n === 'turns').flatMap(([, p]) => p.turns).flatMap(t => t.prompts).at(-1).id");
await page.click(".prompt.perm .btn.primary");
await waitCalls("session.answerPermission");
ok("the card answers through session.answerPermission", isDeepStrictEqual(await lastCall("session.answerPermission"), { id: "c1", promptId, value: "proceed_once" }), await lastCall("session.answerPermission"));
await page.waitFor(READY, 6000);
ok("the turn finishes: the card is gone, the answer and the footer are there", await js("!document.querySelector('.prompt.perm') && document.querySelector('.transcript').textContent.includes('改好了') && /用时/.test(document.querySelector('.turnfoot')?.textContent ?? '')"));
ok("the tab title is the first message", await js("document.querySelector('.tab.current').textContent.includes('hello')"));
ok("Context 12K/— with an empty bar while the window is not known", await js("document.querySelector('.ctxmeter').innerText.replace(/\\s+/g, '') === 'Context12K/—' && !document.querySelector('.ctxmeter .meter-fill')"), await js("document.querySelector('.ctxmeter').innerText"));
ok("TPS from Python's speed, between Context and Token", await js("(() => { const t = document.querySelector('.st-right').innerText.replace(/\\s+/g, ' '); return /Context .*TPS ≈43\\.3 .*Token 合计/.test(t); })()"), await js("document.querySelector('.st-right').innerText"));
ok("Token 合计 / In / Out from Python's usage", await js("/Token 合计\\s*6\\.5K\\s*In\\s*1\\.2K\\s*Out\\s*0\\.3K/.test(document.querySelector('.usage').innerText.replace(/\\s+/g, ' '))"), await js("document.querySelector('.usage').innerText"));
await js(`__fake.patch("c1", { context: { used: 12345, window: 872000, model: "api:gw:claude-haiku-5-5", compacted: false } })`);
await page.waitFor("document.querySelector('.ctxmeter').innerText.replace(/\\s+/g, '') === 'Context12K/872K'", 3000);
ok("…and 12K/872K with a bar once it is", await js("!!document.querySelector('.ctxmeter .meter-fill')"));
await shot("conversation");

// ------------------------------------------------------------------------------------- 7. links
await page.click(".text a[href^='https']");
await waitCalls("shell.openExternal");
ok("an https link goes to the system browser (shell.openExternal)", isDeepStrictEqual(await lastCall("shell.openExternal"), { url: "https://example.com/docs" }));
await clickText(".text a", "notepad");
await page.wait(300);
ok("a file: link opens nothing and the page stays where it is", (await calls("shell.openExternal")).length === 1 && (await js("location.href")) === pageUrl);

// ------------------------------------------------------------------------------------- 8. a question
await page.click(".input");
await page.type("second");
await page.key("Enter");
await page.waitFor("!!document.querySelector('.prompt.ask')", 5000);
ok("an older question without an explicit asker falls back to the API session", await js("document.querySelector('.prompt.ask .phead').textContent.trim() === 'API 模型 想先问你几个问题'"));
// A card belongs to the engine that asked, even if the selected model is from another source.
const questionUpdate = await js("structuredClone(__fake.events.filter(([n]) => n === 'turns').at(-1)[1])");
const questionCard = questionUpdate.turns.at(-1).prompts[0];
questionCard.who = "Devin";
questionCard.questions[0].topic = "Devin";
questionCard.questions[1].topic = questionCard.questions[1].question;
await emit("turns", questionUpdate);
await page.waitFor("document.querySelector('.prompt.ask .phead').textContent.includes('Devin')", 5000);
ok("a question names its actual asker, not WittyCode or the selected model", await js("document.querySelector('.prompt.ask').getAttribute('aria-label') === 'Devin 的提问' && document.querySelector('.prompt.ask .phead').textContent.trim() === 'Devin 想先问你几个问题'"));
ok("the asker and question are not repeated as topic badges", await js("document.querySelectorAll('.prompt.ask .qtopic').length === 0 && document.querySelectorAll('.prompt.ask .qtext').length === 2"));
await shot("devin-question-dark");
await js("document.documentElement.dataset.theme = 'light'");
await shot("devin-question-light");
await js("document.documentElement.dataset.theme = 'dark'");
await page.click(".prompt.ask .opt input[type=radio]", 1);
await page.click(".prompt.ask .opt input[type=checkbox]", 0);
await page.click(".prompt.ask .opt input[type=checkbox]", 2);
await page.click(".prompt.ask .btn.primary");
await waitCalls("session.answerQuestion");
const answered = await lastCall("session.answerQuestion");
ok("a multi-select answer goes as the list of choices", isDeepStrictEqual(answered.answers, ["方式 B", ["界面", "测试"]]) && answered.id === "c1", answered);
await page.waitFor(READY, 6000);
ok("the answers come back as a Q&A block", await js("[...document.querySelectorAll('.block.qa .qa-a')].map(e => e.textContent).join('|') === '方式 B|界面, 测试'"));

// ------------------------------------------------------------------------------------- 8b. commands typed with a slash
const command = async (text) => {
  await page.click(".input");
  await setInput(".input", text);
  await page.key("Enter");
};
const draftIs = (text) => js(`document.querySelector('.input').value === ${JSON.stringify(text)}`);
const sendsAtSkills = (await calls("session.send")).length;
// /compact and /goal go to Python on any engine (this conversation is the own loop's); what Python says is shown
await command("/compact");
await waitCalls("session.compact");
ok("/compact in an own-loop conversation is session.compact, what Python says is shown, the box is empty", isDeepStrictEqual(await lastCall("session.compact"), { id: "c1" }) && (await notice()).includes("已压缩") && (await draftIs("")), await notice());
await page.click(".input");
await setInput(".input", "/goal");
await page.key("Enter");
ok("/goal waits for its text: the menu pick leaves '/goal ' in the box and asks for nothing", (await calls("session.goal")).length === 0 && (await draftIs("/goal ")));
await page.click(".input");
await setInput(".input", "/goal fix the tests");
await page.key("Enter");
await waitCalls("session.goal");
ok("with its text /goal is session.goal there too, and the box is empty", isDeepStrictEqual(await lastCall("session.goal"), { id: "c1", text: "fix the tests" }) && (await notice()).includes("目标已设置") && (await draftIs("")), await lastCall("session.goal"));
await command("/goal off");
await waitCalls("session.goal", 2);
ok("/goal off is session.goal with the word, which Python reads as cancelling", isDeepStrictEqual(await lastCall("session.goal"), { id: "c1", text: "off" }), await lastCall("session.goal"));
// Devin has no way to compact: the page says so and asks Python for nothing
const compacts = (await calls("session.compact")).length;
await js(`__fake.patch("c1", { engine: "devin" })`);
await page.wait(300);
await command("/compact");
ok("/compact in a Devin conversation says Devin manages its own context and keeps the text", (await calls("session.compact")).length === compacts && (await notice()).includes("Devin 没有提供手动压缩") && (await draftIs("/compact")), await notice());
await setInput(".input", "");
await js(`__fake.patch("c1", { engine: "chatgpt" })`);
await page.wait(300);
// the skills and commands of the user's: asked for when a slash is started, listed after the page's own, written by picking, never run by the page
await page.click(".input");
await setInput(".input", "/");
await waitCalls("skills.list");
ok("a slash asks Python for the skills of the conversation's project", isDeepStrictEqual(await lastCall("skills.list"), { id: "c1" }), await lastCall("skills.list"));
await page.waitFor("document.querySelectorAll('.slash-item').length === 5", 3000);
ok("they are in the menu after the three, behind a line: a skill and a command, each with its kind", await js("document.querySelectorAll('.slash-sep').length === 1 && [...document.querySelectorAll('.slash-item')].slice(3).map(e => e.querySelector('.sl-name').textContent + ':' + e.querySelector('.sl-kind').textContent).join() === '/weekly-report:技能,/deploy:命令'"));
await setInput(".input", "/wee");
await page.key("Enter");
ok("picking the skill writes its name and a space, and sends nothing", (await draftIs("/weekly-report ")) && (await calls("session.send")).length === sendsAtSkills);
await setInput(".input", "");
// Plan mode: /plan flips the mode through session.choose
const chooses = (await calls("session.choose")).length;
await command("/plan");
await waitCalls("session.choose", chooses + 1);
ok("/plan chooses the Plan mode through session.choose and empties the box", isDeepStrictEqual(await lastCall("session.choose"), { id: "c1", field: "mode", value: "spec" }) && (await draftIs("")), await lastCall("session.choose"));
await page.waitFor("document.querySelector('.chip-mode .chip-v')?.textContent === 'Plan'", 3000);
ok("and the mode chip says Plan", true);
await command("/goal anything");
ok("in Plan mode a goal is refused by the page, with the reason", (await calls("session.goal")).length === 2 && (await notice()).includes("Plan 模式只出方案") && (await draftIs("/goal anything")));
await setInput(".input", "");
await command("/plan");
await waitCalls("session.choose", chooses + 2);
ok("/plan again goes back to Auto", isDeepStrictEqual(await lastCall("session.choose"), { id: "c1", field: "mode", value: "auto" }), await lastCall("session.choose"));

// ------------------------------------------------------------------------------------- 9. attachments
await page.click(".input");
await paste(PNG);
await waitCalls("attachments.add");
const upload = (await lastCall("attachments.add")).files[0];
ok("a pasted picture goes to Python as base64 (attachments.add)", upload.name === "shot.png" && upload.type === "image/png" && upload.data.startsWith("iVBOR"), { ...upload, data: upload.data.slice(0, 12) });
await page.waitFor("document.querySelectorAll('.att-row .att').length === 1", 3000);
ok("the card is Python's view of it, with its note", await js("document.querySelector('.att-row .fmeta').textContent.includes('已压缩') && document.querySelector('.att-row img').src.startsWith('data:image/png')"));
await paste(`() => [new File(['x'], 'tool.bin', { type: 'application/octet-stream' })]`);
await waitCalls("attachments.add", 2);
await page.waitFor("document.querySelector('.notice-text')?.textContent.includes('tool.bin')", 3000);
ok("what Python refuses is said in the status bar", (await notice()).includes("不是能附加的文件"));
await paste(`() => [new File([new Uint8Array(65 * 1024 * 1024)], 'huge.iso')]`);
await page.waitFor("document.querySelector('.notice-text')?.textContent.includes('超过 64 MiB')", 3000);
ok("a file over 64 MiB is refused before it is read: no call", (await calls("attachments.add")).length === 2);
await js(`(() => { const dt = new DataTransfer(); dt.items.add(new File(['notes'], 'notes.txt', { type: 'text/plain' })); document.querySelector('.main').dispatchEvent(new DragEvent('drop', { dataTransfer: dt, bubbles: true, cancelable: true })); })()`);
await waitCalls("attachments.add", 3);
ok("a dropped file goes through attachments.add too", (await lastCall("attachments.add")).files[0].name === "notes.txt");
await page.waitFor("document.querySelectorAll('.att-row .att').length === 2", 3000);
await page.click(".composer .attach");
await waitCalls("attachments.pick");
ok("the paperclip opens Python's file dialog (attachments.pick)", isDeepStrictEqual(await lastCall("attachments.pick"), { id: "c1" }));
await page.waitFor("document.querySelectorAll('.att-row .att').length === 3", 3000);
ok("what was picked shows as a card", await js("document.querySelector('.att-row').textContent.includes('picked.pdf')"));
await shot("attachments");
await page.click(".att-row .att-main");
ok("a picture opens the lightbox at once, on its thumbnail", await js("document.querySelector('.lightbox img')?.src.startsWith('data:image/png') && document.querySelector('.lightbox figcaption').textContent.includes('64×40')"));
await waitCalls("attachments.full");
ok("…and asks Python for the whole picture", isDeepStrictEqual(await lastCall("attachments.full"), { id: "c1", attachmentId: "d1" }), await lastCall("attachments.full"));
await page.waitFor("document.querySelector('.lightbox figcaption')?.textContent.includes('300×200')", 3000);
ok("which replaces the thumbnail when it arrives", true);
await page.key("Escape");
await page.click(".att-row .att-x", 2);
await waitCalls("attachments.remove");
ok("removing a card tells Python (attachments.remove)", isDeepStrictEqual(await lastCall("attachments.remove"), { id: "c1", attachmentId: "d3" }) && (await js("document.querySelectorAll('.att-row .att').length")) === 2);
await page.click(".input");
await page.type("with files");
await page.key("Enter");
await waitCalls("session.send", 3);
ok("sending passes the attachments' ids", isDeepStrictEqual((await lastCall("session.send")).attachmentIds, ["d1", "d2"]), await lastCall("session.send"));
await page.waitFor("!document.querySelector('.att-row')", 3000);
ok("and the composer's cards are gone", true);
await page.waitFor(READY, 6000);
ok("the sent message shows Python's thumbnail", await js("[...document.querySelectorAll('.block.user')].pop().querySelector('.att-img img')?.src.startsWith('data:image/png')"));

// ------------------------------------------------------------------------------------- 10. notice and restore
await emit("notice", { text: "已自动关闭最久未使用的会话「旧的」以腾出位置，可在侧边栏重新打开。" });
await page.waitFor("document.querySelector('.notice-text')?.textContent.includes('已自动关闭最久未使用的会话')", 2000);
ok("a notice event shows in the status bar", true);
await emit("restore", { id: "c1", text: "再试一次", attachments: [{ id: "d9", kind: "text", name: "again.txt", size: 12 }] });
await page.waitFor("document.querySelector('.input').value === '再试一次'", 2000);
ok("restore puts the failed message back into the composer, with its attachments", await js("document.querySelector('.att-row')?.textContent.includes('again.txt')"));
await page.click(".att-row .att-x", 0);
await waitCalls("attachments.remove", 2);
await setInput(".input", "");

// ------------------------------------------------------------------------------------- 11. change engine
// a scanned PDF joins the conversation (the fake reads a PDF named "scan…" as one without words), so the question has
// a picture, a text file and a PDF that no engine can read
await paste(`() => [new File(['%PDF-1.4'], 'scan.pdf', { type: 'application/pdf' })]`);
await page.waitFor("document.querySelector('.att-row')?.textContent.includes('scan.pdf')", 3000);
await page.click(".input");
await page.type("and a scan");
await page.key("Enter");
await waitCalls("session.send", 4);
ok("the scan goes with a message of its own", isDeepStrictEqual((await lastCall("session.send")).attachmentIds, ["d4"]), await lastCall("session.send"));
await page.waitFor(READY, 6000);

const menuTo = async (label) => {
  await page.click(".controls .chip", 0);
  await clickText(".menu-item", label);
};
const rows = () => js("[...document.querySelectorAll('.dialog .carry-list li')].map(li => ({ name: li.querySelector('.carry-name').textContent, kind: li.querySelector('.carry-kind').textContent, how: li.querySelector('.carry-how').textContent, checked: li.querySelector('input').checked, disabled: li.querySelector('input').disabled, reason: li.querySelector('.carry-reason')?.textContent ?? '' }))");
const confirmLabel = () => js("document.querySelector('.dialog .dialog-btns .btn:last-child').textContent.trim()");
const tick = async (name) => {
  const i = await js(`[...document.querySelectorAll('.dialog .carry-list li')].findIndex(li => li.querySelector('.carry-name').textContent === ${JSON.stringify(name)})`);
  await page.click(".dialog .carry-list li input", i);
};

await page.click(".controls .chip", 0);
ok("Devin is signed in now: its models are in the menu, under their makers", await js("[...document.querySelectorAll('.menu-item')].some(e => e.textContent.includes('SWE-2')) && [...document.querySelectorAll('.menu .menu-sub')].map(e => e.textContent).join() === 'Cognition,Anthropic,OpenAI'"), await js("[...document.querySelectorAll('.menu .menu-sub')].map(e => e.textContent)"));
await clickText(".menu-item", "SWE-2");
await waitCalls("session.choose", 7);
ok("picking a Devin model asks Python to move the conversation", isDeepStrictEqual(await lastCall("session.choose"), { id: "c1", field: "model", value: "devin:swe-2-high" }), await lastCall("session.choose"));
// the conversation has attachments: Python asks which go along before it changes anything
await page.waitFor("!!document.querySelector('.dialog')", 3000);
ok("the conversation has attachments: the page asks, and nothing has changed yet", await js("!document.querySelector('.block.engine-switch') && document.querySelectorAll('.controls .chip')[0].textContent.includes('Claude Haiku 5.5')"));
ok("the question names the model and says what taking them costs and how pictures go", await js("(() => { const t = document.querySelector('.dialog').textContent; return t.includes('SWE-2') && t.includes('3 个附件') && t.includes('输入 token') && t.includes('随下一条消息一起发出'); })()"));
const first = await rows();
ok("every attachment is listed, in the order sent, none ticked", isDeepStrictEqual(first.map((r) => [r.name, r.kind, r.checked]), [["shot.png", "图片", false], ["notes.txt", "文本", false], ["scan.pdf", "PDF", false]]), first);
ok("a picture goes with the next message, a text file's words with the conversation", first[0].how.startsWith("图片随下一条消息发出") && first[1].how.startsWith("文字随对话一起交给它"), first);
ok("what Devin cannot take is listed with the reason and cannot be ticked", first[2].disabled && !first[0].disabled && !first[1].disabled && first[2].how === "带不过去" && first[2].reason.includes("只能读 PDF 里的文字"), first);
ok("with nothing ticked the button says so", (await confirmLabel()) === "都不带，换过去" && (await js("[...document.querySelectorAll('.dialog .dialog-btns .btn')].map(b => b.textContent.trim()).join('|')")) === "取消|都不带，换过去");
await shot("carry-list");
await page.key("Enter");
ok("Enter does not decide for the user: no second call, same engine", (await calls("session.choose")).length === 7 && (await js("!document.querySelector('.block.engine-switch')")));
await menuTo("SWE-2");
await waitCalls("session.choose", 8);
await page.waitFor("!!document.querySelector('.dialog')", 3000);
await page.key("Escape");
ok("Esc is a cancel: no answer, same engine", (await calls("session.choose")).length === 8 && (await js("!document.querySelector('.dialog') && !document.querySelector('.block.engine-switch')")));

// the picture and the text file go, the scan stays behind
await menuTo("SWE-2");
await waitCalls("session.choose", 9);
await page.waitFor("!!document.querySelector('.dialog')", 3000);
await tick("shot.png");
await tick("notes.txt");
ok("ticking is by attachment, and the button counts them", isDeepStrictEqual((await rows()).map((r) => r.checked), [true, true, false]) && (await confirmLabel()) === "带上选中的 2 个，换过去", await rows());
await page.click(".dialog .carry-list li input", 2);
ok("what cannot go cannot be ticked", isDeepStrictEqual((await rows()).map((r) => r.checked), [true, true, false]));
await clickText(".dialog .btn", "带上选中的 2 个");
await waitCalls("session.choose", 10);
ok("the ids ticked go back to Python, in the order of the list", isDeepStrictEqual(await lastCall("session.choose"), { id: "c1", field: "model", value: "devin:swe-2-high", attachments: ["d1", "d2"] }), await lastCall("session.choose"));
await page.waitFor("!!document.querySelector('.block.engine-switch')", 3000);
ok("the status bar says what went along", (await notice()).includes("1 个文本附件的内容、1 张图片一起带过去"), await notice());
ok("the divider: from Claude Haiku 5.5 (API) to SWE-2 (Devin)", await js("(() => { const d = document.querySelector('.block.engine-switch').textContent; return d.includes('Claude Haiku 5.5') && d.includes('API') && d.includes('SWE-2') && d.includes('Devin'); })()"));
ok("same tab, the model chip says SWE-2 with 订阅", await js("document.querySelectorAll('.tab').length === 1 && document.querySelectorAll('.controls .chip')[0].textContent.includes('SWE-2') && document.querySelectorAll('.controls .chip')[0].querySelector('.src-tag')?.textContent === '订阅'"));
ok("Devin's permission reads 写代码 (Python's value code)", (await js("document.querySelectorAll('.controls .chip-v')[2].textContent")) === "写代码");
ok("the hint says the next message carries the earlier text and the 2 attachments", await js("/下一条消息会带上之前 \\d+ 条对话的文字和 2 个附件，交给 SWE-2/.test(document.querySelector('.hint').textContent)"), await js("document.querySelector('.hint').textContent"));
ok("and the status bar has no Limit segment", await js("!document.querySelector('.st-right').innerText.includes('Limit')"));
await shot("switched");

// back to the own loop: the scan cannot go there either
await menuTo("Claude Haiku 5.5");
await waitCalls("session.choose", 11);
await page.waitFor("!!document.querySelector('.dialog')", 3000);
const back = await rows();
ok("asked again each time, and the scan cannot go to the own loop either", isDeepStrictEqual(back.map((r) => [r.name, r.disabled]), [["shot.png", false], ["notes.txt", false], ["scan.pdf", true]]) && back[2].reason.includes("只能读 PDF 里的文字"), back);
const overText = () => js("document.querySelector('.dialog .carry-over')?.textContent ?? ''");
await tick("shot.png");
ok("one picture is the usual amount here: no warning, the plain button", (await overText()) === "" && (await confirmLabel()) === "带上选中的 1 个，换过去", await confirmLabel());
await clickText(".dialog .btn", "带上选中的 1 个");
await waitCalls("session.choose", 12);
ok("confirming sends the id ticked", isDeepStrictEqual(await lastCall("session.choose"), { id: "c1", field: "model", value: "api:gw:claude-haiku-5-5", attachments: ["d1"] }), await lastCall("session.choose"));
await page.waitFor("document.querySelectorAll('.block.engine-switch').length === 2", 3000);
ok("the status bar says the picture went along", (await notice()).includes("1 张图片一起带过去"), await notice());

// and to Devin with none: an empty list is an answer too
await menuTo("SWE-2");
await waitCalls("session.choose", 13);
await page.waitFor("!!document.querySelector('.dialog')", 3000);
await tick("notes.txt");
await tick("notes.txt");
await clickText(".dialog .btn", "都不带");
await waitCalls("session.choose", 14);
ok("none picked is an empty list, not a missing one", isDeepStrictEqual(await lastCall("session.choose"), { id: "c1", field: "model", value: "devin:swe-2-high", attachments: [] }), await lastCall("session.choose"));
await page.waitFor("document.querySelectorAll('.block.engine-switch').length === 3", 3000);
ok("no files note in the status bar", !(await notice()).includes("一起带过去"), await notice());

// what the message box already holds goes with the next message too: it takes its share of the limit (1 file here)
await page.click(".input");
await paste(PNG);
await page.waitFor("document.querySelectorAll('.att-row .att').length === 1", 3000);
await menuTo("Claude Haiku 5.5");
await waitCalls("session.choose", 15);
await page.waitFor("!!document.querySelector('.dialog')", 3000);
ok("the question says the box holds an attachment already", await js("document.querySelector('.dialog').textContent.includes('输入框里已有 1 个附件')"));
await tick("notes.txt");
ok("a text file's words are no file: that is within the usual amount", (await overText()) === "" && (await confirmLabel()) === "带上选中的 1 个，换过去");
await tick("notes.txt");
await tick("shot.png");
const inBoxOver = await overText();
ok("a picture on top of it is one file more than a message usually holds: the page says by how much, the user may go on", inBoxOver.includes("这条消息有 2 个附件") && inBoxOver.includes("多 1 个文件") && (await js("document.querySelector('.dialog .dialog-btns .btn:last-child').disabled")) === false, inBoxOver);
ok("…it is the user's call: the button says it is over", (await confirmLabel()) === "超出了，仍然带上选中的 1 个，换过去", await confirmLabel());
await clickText(".dialog .btn", "超出了，仍然带上选中的 1 个");
await waitCalls("session.choose", 16);
ok("going on anyway sends the id and that the user accepted being over (overLimit)", isDeepStrictEqual(await lastCall("session.choose"), { id: "c1", field: "model", value: "api:gw:claude-haiku-5-5", attachments: ["d1"], overLimit: true }), await lastCall("session.choose"));
await page.waitFor("document.querySelectorAll('.block.engine-switch').length === 4", 3000);
await page.click(".att-row .att-x", 0);
await page.waitFor("!document.querySelector('.att-row')", 3000);

// ------------------------------------------------------------------------------------- 11b. more attachments than a message usually holds, at send
await js(`__fake.setSendFiles(1)`);
const sendsBefore = (await calls("session.send")).length;
await page.click(".input");
await paste(PNG);
await page.waitFor("document.querySelectorAll('.att-row .att').length === 1", 3000);
await paste(`() => [new File(['notes'], 'more.txt', { type: 'text/plain' })]`);
await page.waitFor("document.querySelectorAll('.att-row .att').length === 2", 3000);
await page.waitFor("document.querySelector('.notice-text')?.textContent.includes('发送时会让你确认')", 3000);
ok("past what a message usually holds the box still takes the file, and says by how much", (await notice()).startsWith("现在比一条消息通常带的量多 1 个文件"), await notice());
await page.click(".input");
await page.type("two files");
await page.key("Enter");
await waitCalls("session.send", sendsBefore + 1);
await page.waitFor("!!document.querySelector('.dialog')", 3000);
const askedSend = await js("(() => { const d = document.querySelector('.dialog'); return { title: d.querySelector('h2').textContent, text: d.querySelector('p').textContent, buttons: [...d.querySelectorAll('.dialog-btns .btn')].map((b) => b.textContent.trim()) }; })()");
ok("sending asks: it says by how much, in Python's words, and offers to cancel or send anyway", askedSend.title === "附件比一条消息通常带的多" && askedSend.text.startsWith("比一条消息通常带的量多 1 个文件（这条消息有 2 个附件、") && askedSend.text.endsWith("超出了也可以发，要发吗？") && isDeepStrictEqual(askedSend.buttons, ["取消", "仍然发送"]), askedSend);
ok("nothing was sent as far as the page knows: the message and its two cards are still in the box", (await js("document.querySelector('.input').value")) === "two files" && (await js("document.querySelectorAll('.att-row .att').length")) === 2);
await page.key("Enter");
ok("Enter does not decide for the user: it is the focused cancel button that takes it, nothing is sent", (await js("!document.querySelector('.dialog')")) && (await calls("session.send")).length === sendsBefore + 1 && (await js("document.querySelector('.input').value")) === "two files");
await page.click(".send");
await waitCalls("session.send", sendsBefore + 2);
await page.waitFor("!!document.querySelector('.dialog')", 3000);
await clickText(".dialog .btn", "仍然发送");
await waitCalls("session.send", sendsBefore + 3);
const sentCalls = await calls("session.send");
ok("sending anyway repeats the same message with the user's word (confirmOver)", isDeepStrictEqual(sentCalls.at(-1), { ...sentCalls.at(-3), confirmOver: true }) && sentCalls.at(-3).text === "two files" && sentCalls.at(-3).attachmentIds.length === 2, sentCalls.slice(-3));
await page.waitFor("!document.querySelector('.att-row')", 3000);
ok("and then the message went: the box is empty", (await js("document.querySelector('.input').value")) === "");
await js(`__fake.setSendFiles(10)`);
await page.waitFor(READY, 8000);

// ------------------------------------------------------------------------------------- 12. open a saved session
const tabsBefore = await js("document.querySelectorAll('.tab').length");
await page.click(".srow:not(.open)", 0);
await waitCalls("session.openSaved");
ok("a saved row opens through session.openSaved", isDeepStrictEqual(await lastCall("session.openSaved"), { id: "s-saved-1", projectId: "D:\\Work\\demo-project", title: "整理发布说明" }), await lastCall("session.openSaved"));
await page.waitFor("document.querySelector('.tab.current')?.textContent.includes('整理发布说明') && document.querySelectorAll('.main .transcript').length === 1 && document.querySelector('.transcript')?.textContent.includes('上次的回答')", 3000);
ok("its tab is shown with the restored history", await js("document.querySelector('.transcript').textContent.includes('上次的回答')"));
// a path in the restored answer: the page asks Python for the file (files.*), nothing else reads the disk
ok("the paths in the answer are links", (await js("[...document.querySelectorAll('.text .pathref')].map(e => e.textContent).join()")) === "docs/notes.md:5,docs/report.pdf,docs/long.pdf,docs/broken.pdf,shots/ui.png");
await page.click(".text .pathref");
await waitCalls("files.read");
const readCall = await lastCall("files.read");
ok("a click reads the file through files.read, with the path as the model wrote it", readCall.path === "docs/notes.md:5" && typeof readCall.id === "string" && readCall.id.length > 0, readCall);
await page.waitFor("!!document.querySelector('.filepanel .fp-rows')", 3000);
ok("the panel shows what Python sent, its lines marked", (await js("document.querySelector('.fp-name').textContent")) === "notes.md" && (await js("document.querySelector('.fp-mark').style.top")) === "88px");
await page.click(".text .pathref", 0, { ctrl: true });
await waitCalls("files.open");
ok("Ctrl+click asks Python to open it with the default program", isDeepStrictEqual(await lastCall("files.open"), { id: readCall.id, path: "docs/notes.md:5", how: "default" }), await lastCall("files.open"));
await page.click(".text .pathref", 0, { right: true });
await clickText(".menu-item", "选择打开方式");
await waitCalls("files.open", 2);
ok("the menu's 选择打开方式 asks for how: choose", (await lastCall("files.open")).how === "choose");
await page.click(".text .pathref", 0, { right: true });
await clickText(".menu-item", "在文件夹中显示");
await waitCalls("files.open", 3);
ok("and 在文件夹中显示 for how: reveal", (await lastCall("files.open")).how === "reveal");
await page.click(".text .pathref", 0, { right: true });
await clickText(".menu-item", "复制路径");
await waitCalls("files.resolve");
ok("复制路径 asks Python where the file is (files.resolve)", isDeepStrictEqual(await lastCall("files.resolve"), { id: readCall.id, path: "docs/notes.md:5" }), await lastCall("files.resolve"));
await page.key("Escape");
ok("Esc closes the panel", await js("!document.querySelector('.filepanel')"));
// a PDF the answer names: the panel shows its pages, drawn by Python (files.page), the ones near the visible part first
await page.click(".text .pathref", 1);
await page.waitFor("document.querySelectorAll('.filepanel .fp-sheet img').length >= 1", 4000);
ok("a PDF opens as pages, not as text", await js("document.querySelectorAll('.fp-sheet').length === 5 && !document.querySelector('.fp-rows') && document.querySelector('.fp-name').textContent === 'report.pdf'"));
ok("the facts line says how many pages and how big", await js("document.querySelector('.fp-facts').textContent.includes('5 页 · 179.1 KB')"));
ok("the sheets keep each page's shape before it is drawn (the square one is square)", await js("(() => { const r = [...document.querySelectorAll('.fp-sheet')].map((e) => e.getBoundingClientRect()); return Math.abs(r[0].height / r[0].width - 842 / 595) < 0.01 && Math.abs(r[2].height / r[2].width - 1) < 0.01; })()"));
const firstPages = await calls("files.page");
ok("the pages come from files.page with the path as written, the page number and a width that suits the panel", firstPages.length >= 1 && firstPages.every((c) => c.path === "docs/report.pdf" && Number.isInteger(c.page) && c.width >= 700 && c.width <= 1800 && c.width % 100 === 0) && firstPages[0].page === 0, firstPages);
ok("the first page is drawn", await js("document.querySelector('.fp-sheet img').src.startsWith('data:image/svg+xml')"));
ok("the panel is a scroll area of its own and shows where you are", await js("(() => { const b = document.querySelector('.fp-pages'); return getComputedStyle(b).overflowY === 'auto' && b.scrollHeight > b.clientHeight && document.querySelector('.fp-pagepill').textContent.trim() === '1 / 5'; })()"));
await js("(() => { const b = document.querySelector('.fp-pages'); b.scrollTop = b.scrollHeight; })()");
await page.waitFor("document.querySelector('.fp-pagepill')?.textContent.trim() === '5 / 5'", 3000);
await page.waitFor("!!document.querySelectorAll('.fp-sheet')[4].querySelector('img')", 3000);
ok("scrolling to the end says page 5 of 5 and draws it", true);
ok("a page that was jumped over is not drawn until it comes near", await js("!document.querySelectorAll('.fp-sheet')[2].querySelector('img')"));
await js("document.querySelectorAll('.fp-sheet')[2].scrollIntoView()");
await page.waitFor("!!document.querySelectorAll('.fp-sheet')[2].querySelector('img')", 3000);
ok("scrolling back to it draws it, and the number follows", await js("document.querySelector('.fp-pagepill').textContent.trim() === '3 / 5'"));
const pageNumbers = (await calls("files.page")).map((c) => c.page).sort();
ok("every page was asked for once and none twice", isDeepStrictEqual(pageNumbers, [0, 1, 2, 3, 4]), pageNumbers);
await shot("pdf-panel");
await page.click(".text .pathref", 2);
await page.waitFor("document.querySelector('.fp-name')?.textContent === 'long.pdf'", 3000);
await page.waitFor("document.querySelectorAll('.fp-sheet img').length >= 1", 3000);
await page.wait(300);
const drawn = await js("document.querySelectorAll('.fp-sheet img').length");
ok("a 40-page PDF draws only the pages near the visible part, not all of them", drawn >= 1 && drawn < 40, drawn);
ok("and has a sheet for every page", await js("document.querySelectorAll('.fp-sheet').length === 40"));
await page.click(".text .pathref", 3);
await page.waitFor("document.querySelector('.fp-name')?.textContent === 'broken.pdf'", 3000);
await page.waitFor("!!document.querySelector('.fp-sheet .fp-wait.linkbtn')", 3000);
ok("a page Python could not draw says so and offers a retry, the others are drawn", await js("document.querySelectorAll('.fp-sheet .fp-wait.linkbtn').length === 1 && document.querySelectorAll('.fp-sheet img').length >= 1"));
await page.click(".fp-sheet .fp-wait.linkbtn");
await page.waitFor("(__fake.callsOf('files.page').filter((c) => c.path === 'docs/broken.pdf' && c.page === 1).length) >= 2", 3000);
ok("the retry asks for the same page again", true);
await page.click(".text .pathref", 4);
await page.waitFor("!!document.querySelector('.fp-picture img')", 3000);
ok("a picture is shown fitted in the panel", await js("(() => { const i = document.querySelector('.fp-picture img'); return !!i && !document.querySelector('.fp-sheet') && !document.querySelector('.fp-rows') && i.src.startsWith('data:image/svg+xml') && document.querySelector('.fp-facts').textContent.includes('KB'); })()"));
await page.click(".fp-picture img");
ok("a click shows it at its own size", await js("document.querySelector('.fp-picture').classList.contains('actual')"));
ok("no console errors (file previews)", page.consoleErrors().length === 0, page.consoleErrors());
await page.key("Escape");
ok("one more tab, and its saved row became the open one", (await js("document.querySelectorAll('.tab').length")) === tabsBefore + 1 && (await js("[...document.querySelectorAll('.srow')].filter(r => r.textContent.includes('整理发布说明')).length")) === 1);
await page.waitFor(READY, 3000);
const opened = await js("document.querySelectorAll('.tab').length");
const row = await js("[...document.querySelectorAll('.srow:not(.open)')].findIndex(r => r.textContent.includes('迁移脚本'))");
const r = await page.rect(".srow:not(.open)", row);
for (let i = 1; i <= 2; i++) {
  await page.mouse("mousePressed", r.x + 20, r.y + r.h / 2, { clickCount: i });
  await page.mouse("mouseReleased", r.x + 20, r.y + r.h / 2, { clickCount: i });
}
await page.waitFor("document.querySelector('.tab.current')?.textContent.includes('迁移脚本')", 3000);
await page.wait(400);
ok("a double click opens a saved row once", (await js("document.querySelectorAll('.tab').length")) === opened + 1);

// a slow open answers late: a user who went to another tab meanwhile is not taken back to the new one
const tabsAtOpen = opened + 1;
await js(`__fake.delayReply("session.new", 900)`);
await page.click(".newbtn");
await page.click(".input");
await page.type("quick: late");
await page.key("Enter");
await page.waitFor(`document.querySelectorAll('.tab').length === ${tabsAtOpen + 1} && [...document.querySelectorAll('.tab')].at(-1).classList.contains('current')`, 3000);
await page.click(".tab", 0);
const stayedOn = await js("document.querySelector('.tab.current').textContent");
await page.wait(1400); // Python's answer to session.new has come by now
ok("the late answer does not take the user back to the new tab", !(await js("[...document.querySelectorAll('.tab')].at(-1).classList.contains('current')")) && (await js("document.querySelector('.tab.current').textContent")) === stayedOn, await js("document.querySelector('.tab.current').textContent"));
ok("and the message still goes out in the conversation it opened", (await lastCall("session.send")).text === "quick: late", await lastCall("session.send"));
// and the new tab is closed again, so that the rest of the run has the tabs it always had
await page.click(".tab", tabsAtOpen);
await page.waitFor("!document.querySelector('.turnfoot.live') && !!document.querySelector('.block.user')", 4000);
await press("w", "KeyW", 87, CTRL);
await waitCalls("session.close");
ok("Ctrl+W closes the current conversation (session.close)", typeof (await lastCall("session.close")).id === "string", await lastCall("session.close"));
await page.waitFor(`document.querySelectorAll('.tab').length === ${tabsAtOpen}`, 3000);
ok("its neighbour is shown", await js("!!document.querySelector('.tab.current')"));

// ------------------------------------------------------------------------------------- 13. shortcuts
await blur();
await press("l", "KeyL", 76, CTRL);
ok("Ctrl+L puts the cursor in the message box", await js("document.activeElement === document.querySelector('.input')"));
await page.click(".tab", 0);
await js("document.querySelector('.scroller').scrollTop = 0");
await page.wait(200);
// (not a fixed wait: right after a build the first scroll is slow, and the page has to finish it before it is measured; the
// key can also arrive while the message box takes the focus back after the tab was clicked, so it is pressed again)
for (let attempt = 0; attempt < 3; attempt++) {
  await blur();
  await press("End", "End", 35, CTRL);
  const there = await page.waitFor("(() => { const s = document.querySelector('.scroller'); return s.scrollHeight - s.scrollTop - s.clientHeight < 48; })()", 3000).then(() => true, () => false);
  if (there) break;
}
ok("Ctrl+End jumps to the latest message", await js("(() => { const s = document.querySelector('.scroller'); return s.scrollHeight - s.scrollTop - s.clientHeight < 48; })()"));
await press("F5", "F5", 116);
await waitCalls("project.refresh");
ok("F5 refreshes the session lists (project.refresh)", isDeepStrictEqual(await lastCall("project.refresh"), {}) && (await js("location.href")) === pageUrl);
await press("o", "KeyO", 79, CTRL);
await waitCalls("project.add");
ok("Ctrl+O adds a project (project.add, Python's folder dialog)", isDeepStrictEqual(await lastCall("project.add"), {}));
await press("b", "KeyB", 66, CTRL);
ok("Ctrl+B hides the sidebar", await js("!document.querySelector('.sidebar')"));
await press("b", "KeyB", 66, CTRL);
ok("…and shows it again", await js("!!document.querySelector('.sidebar')"));
await press(",", "Comma", 188, CTRL);
ok("Ctrl+, opens the settings", await js("!!document.querySelector('.settings')"));
await page.key("Escape");
const before = await js("document.querySelectorAll('.tab').length");
const newCalls = (await calls("session.new")).length;
await press("n", "KeyN", 78, CTRL);
await page.waitFor("!document.querySelector('.tab.current')", 2000);
ok("Ctrl+N shows the first message box (in the current conversation's project) and creates nothing", (await calls("session.new")).length === newCalls && (await js("document.querySelectorAll('.tab').length")) === before && (await js("document.querySelector('.hint .chip-v').textContent")) === "demo-project" && (await js("document.activeElement === document.querySelector('.input')")));
await page.click(".tab", 0);
ok("a conversation is shown again", await js("!!document.querySelector('.tab.current')"));

// ------------------------------------------------------------------------------------- 14. settings go to Python
await blur();
await press(",", "Comma", 188, CTRL);
await page.click(".snav", 0);
await page.click(".segc button", 1);
await waitCalls("settings.set");
ok("the theme is saved through settings.set", isDeepStrictEqual(await lastCall("settings.set"), { key: "theme", value: "light" }));
await page.waitFor("document.documentElement.dataset.theme === 'light'", 2000);
ok("and comes back as Python's settings", true);
await page.click(".segc button", 5);
await waitCalls("settings.set", 2);
ok("the text size too", isDeepStrictEqual(await lastCall("settings.set"), { key: "fontScale", value: "lg" }) && (await js("document.documentElement.dataset.fs")) === "lg");
await page.click(".snav", 3);
await page.click(".stepper button", 1);
await waitCalls("settings.set", 3);
ok("the compaction percent goes to Python in whole points (compactAt)", isDeepStrictEqual(await lastCall("settings.set"), { key: "compactAt", value: 90 }));
await page.waitFor("document.querySelector('.stepper span').textContent === '90%'", 2000);
ok("and comes back as Python's settings", true);
await page.click(".stepper button", 1);
await waitCalls("settings.set", 4);
await page.waitFor("document.querySelector('.stepper span').textContent === '95%'", 2000);
ok("it stops at 95 (the button is disabled)", isDeepStrictEqual(await lastCall("settings.set"), { key: "compactAt", value: 95 }) && (await js("document.querySelectorAll('.stepper button')[1].disabled")));
await page.click(".settings-body .field .switch", 0);
await waitCalls("settings.set", 5);
ok("the switch for automatic compaction is saved (compactAuto)", isDeepStrictEqual(await lastCall("settings.set"), { key: "compactAuto", value: false }));
await page.waitFor("document.querySelectorAll('.stepper').length === 1", 2000);
ok("with it off the percent is not offered", true);
await page.click(".settings-body .field .switch", 0);
await waitCalls("settings.set", 6);
ok("and back on", isDeepStrictEqual(await lastCall("settings.set"), { key: "compactAuto", value: true }));
await page.waitFor("document.querySelectorAll('.stepper').length === 2 && document.querySelector('.stepper span').textContent === '95%'", 2000);
ok("the percent is still what it was", true);
await page.click(".stepper button", 3);
await waitCalls("settings.set", 7);
ok("the session limit too", isDeepStrictEqual(await lastCall("settings.set"), { key: "maxOpen", value: 5 }));
await page.waitFor("document.querySelectorAll('.stepper span')[1].textContent === '5'", 2000);
await page.click(".snav", 5);
ok("the 外部程序 page has only the Devin CLI, and no 浏览… (there is no such call)", await js("[...document.querySelectorAll('.settings-body .field-label')].map(e => e.textContent).join() === 'Devin 命令行' && ![...document.querySelectorAll('.settings-body .btn')].some(b => b.textContent.includes('浏览'))"));
await page.click(".settings-body .segc button", 1); // Devin 命令行: 指定路径 starts from the one that was found
await waitCalls("settings.set", 8);
const devinInput = 'input[aria-label="Devin 命令行 路径"]';
await page.waitFor(`document.querySelector('${devinInput}')?.value.endsWith('devin.exe')`, 2000);
await setInput(devinInput, "");
await page.click(devinInput);
await page.type("D:\\tools\\devin.exe");
await page.key("Enter");
await waitCalls("settings.set", 9);
const devinPaths = (await calls("settings.set")).filter((c) => c.key === "devinPath").map((c) => c.value);
ok("the Devin CLI path is saved when it is entered, not on every key", isDeepStrictEqual(devinPaths, ["C:\\Users\\dev\\AppData\\Local\\Programs\\devin\\devin.exe", "D:\\tools\\devin.exe"]), devinPaths);
await page.click(".snav", 8);
ok("the About page offers no log folder button (there is no such call)", await js("!document.querySelector('.settings-body .about .btn')"));
await page.key("Escape");
const sp = await page.rect(".splitter");
await page.mouse("mouseMoved", sp.x + 3, sp.y + 200, { button: "none", clickCount: 0 });
await page.mouse("mousePressed", sp.x + 3, sp.y + 200);
for (let x = sp.x; x <= sp.x + 60; x += 15) await page.mouse("mouseMoved", x, sp.y + 200, { button: "left", clickCount: 0 });
await page.mouse("mouseReleased", sp.x + 60, sp.y + 200);
await waitCalls("settings.set", 10);
await page.wait(300);
ok("dragging the splitter saves the width once, when it is let go", (await calls("settings.set")).filter((c) => c.key === "sidebarWidth").length === 1 && (await lastCall("settings.set")).value > 300, await lastCall("settings.set"));

// ------------------------------------------------------------------------------------- 14b. which models the menu offers
const mvis = (source) => `.mvis[data-source="${source}"]`;
const mvisCount = (source) => js(`document.querySelector('${mvis(source)} .mvis-count').textContent.replace(/\\s+/g, '')`);
const rowSwitch = async (source, label) => {
  const i = await js(`[...document.querySelectorAll('${mvis(source)} .mvis-row')].findIndex(e => e.textContent.includes(${JSON.stringify(label)}))`);
  if (i < 0) throw new Error(`no ${source} model "${label}"`);
  await page.click(`${mvis(source)} .mvis-row .switch`, i);
};
const menuItems = async () => {
  await page.click(".controls .chip", 0);
  const items = await js("[...document.querySelectorAll('.menu-item')].map(e => e.textContent).join('|')");
  await page.key("Escape");
  return items;
};
await blur();
await press(",", "Comma", 188, CTRL);
await page.waitFor("!!document.querySelector('.settings')", 2000);
await page.click(".snav", 1);
ok("the provider's card counts the models Python listed (the disabled one is not one to choose)", (await mvisCount("api:gw")) === "4/4", await mvisCount("api:gw"));
ok("the Devin card has its own list", (await mvisCount("devin")) === "3/3", await mvisCount("devin"));
await page.click(`${mvis("devin")} .disclose`);
ok("Devin's models sit under their makers, like the menu", await js(`[...document.querySelectorAll('${mvis("devin")} .mvis-maker')].map(e => e.textContent).join() === 'Cognition,Anthropic,OpenAI'`));
await page.click(`${mvis("devin")} .disclose`);
const using = await js("document.querySelectorAll('.controls .chip-v')[0].textContent");
const hide = using === "Gemini 3 Pro" ? "GPT-5.4" : "Gemini 3 Pro";
await page.click(`${mvis("api:gw")} .disclose`);
await rowSwitch("api:gw", hide);
await waitCalls("models.setVisible");
ok("a switch goes to Python (models.setVisible)", isDeepStrictEqual(await lastCall("models.setVisible"), { ids: [hide === "GPT-5.4" ? "api:gw:gpt-5.4" : "api:gw:gemini-3-pro"], visible: false }), await lastCall("models.setVisible"));
await page.waitFor(`document.querySelector('${mvis("api:gw")} .mvis-count').textContent.replace(/\\s+/g, '') === '3/4'`, 2000);
ok("the count follows Python's settings event", true);
await page.key("Escape");
ok("the model menu leaves it out", !(await menuItems()).includes(hide), await menuItems());
await blur();
await press(",", "Comma", 188, CTRL);
await page.click(".snav", 1);
await page.click(`${mvis("devin")} .disclose`);
await clickText(`${mvis("devin")} .btn`, "全部隐藏");
await waitCalls("models.setVisible", 2);
ok("hide all sends the ids of the whole list", isDeepStrictEqual(await lastCall("models.setVisible"), { ids: ["devin:swe-2-high", "devin:claude-opus-5-5-medium", "devin:gpt-6-sol-medium"], visible: false }), await lastCall("models.setVisible"));
await page.waitFor(`document.querySelector('${mvis("devin")} .mvis-count').textContent.replace(/\\s+/g, '') === '0/3'`, 2000);
await page.key("Escape");
const usingNow = await js("document.querySelectorAll('.controls .chip-v')[0].textContent");
const afterHideAll = await menuItems();
ok(
  usingNow === "SWE-2"
    ? "a conversation on a Devin model keeps it in the menu even when the group is hidden, and says why"
    : "an emptied group says so in the menu",
  usingNow === "SWE-2" ? afterHideAll.includes("SWE-2已在设置里隐藏，正在使用所以还显示") : afterHideAll.includes("没有显示的模型"),
  { usingNow, afterHideAll },
);
await blur();
await press(",", "Comma", 188, CTRL);
await page.click(".snav", 1);
await page.click(`${mvis("api:gw")} .disclose`);
await clickText(`${mvis("api:gw")} .btn`, "全部显示");
await page.click(`${mvis("devin")} .disclose`);
await clickText(`${mvis("devin")} .btn`, "全部显示");
await waitCalls("models.setVisible", 4);
await page.waitFor(`document.querySelector('${mvis("api:gw")} .mvis-count').textContent.replace(/\\s+/g, '') === '4/4' && document.querySelector('${mvis("devin")} .mvis-count').textContent.replace(/\\s+/g, '') === '3/3'`, 2000);
ok("show all puts everything back", true);
await page.key("Escape");
ok("and the menu is whole again", await (async () => { const t = await menuItems(); return t.includes(hide) && t.includes("SWE-2") && !t.includes("没有显示的模型"); })());

// ------------------------------------------------------------------------------------- 14c. the shared memory
const titles = () => js("[...document.querySelectorAll('.mem-note .mem-title')].map(e => e.textContent)");
await blur();
await press(",", "Comma", 188, CTRL);
await clickText(".snav", "共享记忆");
await page.waitFor("document.querySelectorAll('.mem-empty').length === 2", 3000);
ok("the memory page asks Python for the notes and for what agents wrote", isDeepStrictEqual((await calls("memory.list")).at(-1), { status: "active" }) && (await calls("memory.changes")).length > 0);
ok("with nothing in it, it says so", await js("document.querySelectorAll('.mem-empty').length === 2 && !document.querySelector('.mem-change, .mem-note') && !document.querySelector('.snav-badge, .foot-badge')"));
await page.click(".settings-body .field .switch", 0);
await waitCalls("settings.set", 11);
ok("the master switch is saved through settings.set", isDeepStrictEqual(await lastCall("settings.set"), { key: "memoryEnabled", value: false }));
await page.waitFor("!document.querySelector('.mem-engine')", 2000);
await page.click(".settings-body .field .switch", 0);
await page.waitFor("document.querySelectorAll('.mem-engine').length === 2", 2000);
await page.click(".mem-engine .switch", 1);
await waitCalls("settings.set", 13);
ok("an engine left out is saved as the list of those that stay", isDeepStrictEqual(await lastCall("settings.set"), { key: "memoryEngines", value: ["chatgpt"] }));
await page.click(".mem-engine .switch", 1);
await waitCalls("settings.set", 14);
ok("and put back in the order of the engines", isDeepStrictEqual(await lastCall("settings.set"), { key: "memoryEngines", value: ["chatgpt", "devin"] }));
await page.click(".settings-body .field .switch", 3);
await waitCalls("settings.set", 15);
ok("the switch is about the start of a new conversation, not every message", await js("[...document.querySelectorAll('.settings-body .field')].some((f) => f.querySelector('.field-label')?.textContent === '新会话开头带上相关笔记' && /之后的消息不再带笔记/.test(f.textContent)) && ![...document.querySelectorAll('.settings-body .field')].some((f) => f.textContent.includes('每条消息带上'))"));
ok("leaving the notes that bear on the first message out of it is saved (memoryRecall)", isDeepStrictEqual(await lastCall("settings.set"), { key: "memoryRecall", value: false }));
await page.waitFor("document.querySelectorAll('.settings-body .field .switch')[3].getAttribute('aria-checked') === 'false'", 2000);
await page.click(".settings-body .field .switch", 3);
await waitCalls("settings.set", 16);
ok("and putting them back", isDeepStrictEqual(await lastCall("settings.set"), { key: "memoryRecall", value: true }));
await page.click(".settings-body .field .switch", 4);
await waitCalls("settings.set", 17);
ok("leaving out the notes the application writes when a message says remember is saved (memoryCapture)", isDeepStrictEqual(await lastCall("settings.set"), { key: "memoryCapture", value: false }));
await page.click(".settings-body .field .switch", 4);
await waitCalls("settings.set", 18);
await page.click(".settings-body .field .switch", 5);
await waitCalls("settings.set", 19);
ok("and the suggestions after a turn (memoryCurate)", isDeepStrictEqual(await lastCall("settings.set"), { key: "memoryCurate", value: false }));
await page.click(".settings-body .field .switch", 5);
await waitCalls("settings.set", 20);
ok("both come back on", isDeepStrictEqual(await lastCall("settings.set"), { key: "memoryCurate", value: true }) && (await js("[...document.querySelectorAll('.settings-body .field .switch')].every((e) => e.getAttribute('aria-checked') === 'true')")));

await clickText(".mem-head .btn", "新建笔记");
await setInput('.mem-create input[aria-label="标题"]', "提交信息用英文");
await setInput('.mem-create textarea[aria-label="内容"]', "git commit 的说明用英文，一行不超过 72 个字符。");
await clickText(".mem-create .segc button", "偏好");
await page.click(".mem-create .btn.primary");
await waitCalls("memory.create");
const made = await lastCall("memory.create");
ok("a note written by the user goes to memory.create with its scope, kind and project", made.title === "提交信息用英文" && made.scope === "project" && made.kind === "preference" && typeof made.project === "string" && made.project !== "" && isDeepStrictEqual(made.tags, []), made);
await page.waitFor("document.querySelectorAll('.mem-note').length === 1", 3000);
ok("it is listed, open, as written by me; the form is gone", await js("!!document.querySelector('.mem-note.open') && document.querySelector('.mem-note.open').textContent.includes('我写入') && !document.querySelector('.mem-create')"));
ok("a change the user made is not news to them: no badge", await js("!document.querySelector('.snav-badge, .foot-badge')"));

await js(`__fake.agentWrote("devin", "PowerShell 5.1 没有 &&", "用分号串联命令，用 $LASTEXITCODE 判断结果。")`);
await page.waitFor("document.querySelectorAll('.mem-note').length === 2", 3000);
ok("an agent's write arrives as the memory event: the list, the changes and both badges follow", await js("document.querySelectorAll('.mem-change').length === 1 && document.querySelector('.snav-badge')?.textContent === '1' && document.querySelector('.foot-badge')?.textContent === '1' && !!document.querySelector('.mem-change.new')"));
ok("the change names who wrote what, and in which conversation", await js("(() => { const c = document.querySelector('.mem-change'); return c.textContent.includes('Devin') && c.textContent.includes('记住了') && c.textContent.includes('PowerShell 5.1 没有 &&') && c.querySelector('.mem-link')?.textContent === '整理发布说明'; })()"));
await shot("memory");
await page.click(".mem-change .mem-link");
await waitCalls("session.openSaved");
ok("the conversation link opens that conversation (session.openSaved) and gets the settings out of the way", (await lastCall("session.openSaved")).id === "s-saved-1" && (await js("!document.querySelector('.settings')")));
await page.waitFor("[...document.querySelectorAll('.tab')].some(t => t.textContent.includes('整理发布说明'))", 4000);
await blur();
await press(",", "Comma", 188, CTRL);
await clickText(".snav", "共享记忆");
await page.waitFor("document.querySelectorAll('.mem-note').length === 2", 3000);
await page.click(".mem-change .btn");
await waitCalls("memory.undo");
const seqUndone = (await lastCall("memory.undo")).seq;
ok("撤销 sends the number of the change", seqUndone === 2, seqUndone);
await page.waitFor("document.querySelectorAll('.mem-note').length === 1", 3000);
ok("the note an agent wrote is gone from the list after the undo", (await titles()).join() === "提交信息用英文" && (await notice()).includes("已撤销"));
await js(`__fake.agentWrote("devin", "Devin 的权限提示用它自己的选项", "回答时用 options[0].value。", "")`);
await page.waitFor("document.querySelector('.foot-badge')?.textContent === '2'", 3000);
await clickText(".mem-head .btn", "全部标为已看过");
await waitCalls("memory.markSeen");
await page.waitFor("!document.querySelector('.snav-badge, .foot-badge, .mem-new')", 3000);
ok("全部标为已看过 (memory.markSeen) clears the badges", isDeepStrictEqual(await lastCall("memory.markSeen"), {}));
ok("a change made outside any conversation names none", await js("(() => { const c = [...document.querySelectorAll('.mem-change')].find((e) => e.textContent.includes('Devin')); return !!c && !c.querySelector('.mem-link'); })()"));

// what the application writes by itself after a turn: the note is in the memory at once, and the week's notes are looked at in one go
await js(`__fake.autoNote("提交信息用中文", "提交信息用中文写，标题一行。", 0.9)`);
await js(`__fake.autoNote("先写测试", "先写测试再写代码。", 0.7)`);
await js(`__fake.autoNote("这次别动样式", "只改逻辑。", 0.3)`);
await page.waitFor("document.querySelectorAll('.mem-rev').length === 3", 3000);
await waitCalls("memory.review");
ok("the notes are in the memory list already, the review waits above the changes, and both badges count them", await js("document.querySelector('.snav-badge')?.textContent === '3' && document.querySelector('.foot-badge')?.textContent === '3' && [...document.querySelectorAll('.mem-note .mem-title')].some((e) => e.textContent === '先写测试')"));
ok("the review lists the least sure first; the ones it is sure of are ticked", await js("(() => { const rows = [...document.querySelectorAll('.mem-rev')]; return rows.map((r) => r.querySelector('.mem-title').textContent).join() === '这次别动样式,先写测试,提交信息用中文' && rows.map((r) => r.querySelector('.switch').getAttribute('aria-checked')).join() === 'false,true,true'; })()"));
ok("each says how sure the application is, and why", await js("(() => { const r = document.querySelector('.mem-rev'); return r.querySelector('.mem-score').textContent === '把握 30%' && r.querySelector('.mem-meta').textContent.includes('用户明确说了'); })()"));
ok("the button says how many stay and how many go", await js("document.querySelector('.mem-rev-foot .btn.primary').textContent.replace(/\\s+/g, '') === '确认：留下2条，丢掉1条'"));
await clickText(".mem-rev .btn", "改一改");
await setInput('.mem-rev input[aria-label="标题"]', "这次别动样式，只改逻辑");
await clickText(".mem-rev .mem-editor .btn", "保存修改");
await page.waitFor("!document.querySelector('.mem-rev .mem-editor')", 3000);
ok("an edited note shows its new words, says so, and is ticked", await js("(() => { const r = document.querySelector('.mem-rev'); return r.querySelector('.mem-title').textContent === '这次别动样式，只改逻辑' && r.querySelector('.mem-meta').textContent.includes('已改') && r.querySelector('.switch').getAttribute('aria-checked') === 'true'; })()"));
await page.click(".mem-rev .switch", 1);
await clickText(".mem-rev-foot .btn", "确认");
await waitCalls("memory.reviewApply");
const verdict = await lastCall("memory.reviewApply");
ok("确认 sends the ticked notes as keep, the others as drop, and the edit of a kept one", verdict.keep.length === 2 && verdict.drop.length === 1 && Object.keys(verdict.edits).length === 1 && verdict.edits[verdict.keep[0]].title === "这次别动样式，只改逻辑" && verdict.edits[verdict.keep[0]].kind === "convention" && verdict.edits[verdict.keep[0]].scope === "project" && Array.isArray(verdict.edits[verdict.keep[0]].tags), verdict);
await page.waitFor("!document.querySelector('.mem-rev') && document.querySelector('.mem-lead')?.textContent.includes('保留 2 条，丢弃 1 条')", 3000);
ok("the review is done: the summary says what was kept and dropped, the badges are gone", await js("!document.querySelector('.snav-badge, .foot-badge, .mem-new')"));
ok("a dropped note left the list (it can be found again among the forgotten ones), the kept ones are there", await js("(() => { const t = [...document.querySelectorAll('.mem-note .mem-title')].map((e) => e.textContent); return !t.includes('先写测试') && t.includes('提交信息用中文') && t.includes('这次别动样式，只改逻辑'); })()"));
await js(`__fake.autoNote("甲", "第一条的内容。", 0.8)`);
await js(`__fake.autoNote("乙", "第二条的内容。", 0.2)`);
await page.waitFor("document.querySelectorAll('.mem-rev').length === 2", 3000);
await clickText(".mem-rev-foot .btn", "稍后再看");
await page.waitFor("!document.querySelector('.mem-rev')", 3000);
ok("稍后再看 folds the list; the section stays, with a button to come back", await js("!!document.querySelector('.mem-sec[aria-label=\"每周回顾\"]') && [...document.querySelectorAll('.mem-sec .btn')].some((b) => b.textContent.trim() === '回顾')"));
await clickText(".mem-sec .btn", "回顾");
await page.waitFor("document.querySelectorAll('.mem-rev').length === 2", 3000);
await clickText(".mem-rev-foot .btn", "全部留下");
ok("全部留下 ticks every note", await js("[...document.querySelectorAll('.mem-rev .switch')].every((s) => s.getAttribute('aria-checked') === 'true')"));
await clickText(".mem-rev-foot .btn", "稍后再看");
await js(`__fake.reviewDue(false)`);
await page.waitFor("!document.querySelector('.snav-badge, .foot-badge')", 3000);
await page.waitFor("[...document.querySelectorAll('.mem-sec .btn')].some((b) => b.textContent.trim() === '现在回顾')", 3000);
ok("before the week is up nothing is counted as waiting; the section is folded and offers 现在回顾", await js("!document.querySelector('.mem-rev') && [...document.querySelectorAll('.mem-sec .btn')].some((b) => b.textContent.trim() === '现在回顾')"));
await clickText(".mem-sec .btn", "现在回顾");
await page.waitFor("document.querySelectorAll('.mem-rev').length === 2", 3000);
await clickText(".mem-rev-foot .btn", "确认");
await page.waitFor("!document.querySelector('.mem-rev')", 3000);
ok("a review done early is the same call", (await lastCall("memory.reviewApply")).keep.length === 2);
await js(`__fake.reviewDue(true)`);
await page.key("Escape");


// ------------------------------------------------------------------------------------- 14c. rename and archive
const rowIndex = (text) => js(`[...document.querySelectorAll('.srow')].findIndex(r => r.textContent.includes(${JSON.stringify(text)}))`);
await page.click(".srow", await rowIndex("旧的会话"), { right: true });
await clickText(".menu-item", "重命名…");
await page.waitFor("!!document.querySelector('.dialog input')", 2000);
ok("重命名… opens a dialog with the present name filled in", (await js("document.querySelector('.dialog input').value")) === "旧的会话");
await page.type("三天前的会话");
await page.key("Enter");
await waitCalls("session.rename");
ok("the new name goes to Python (session.rename)", isDeepStrictEqual(await lastCall("session.rename"), { id: "s-saved-2", title: "三天前的会话" }), await lastCall("session.rename"));
await page.waitFor("[...document.querySelectorAll('.srow .stitle')].some(e => e.textContent === '三天前的会话')", 3000);
ok("and the row shows the name Python sends back", true);
await page.click(".srow", await rowIndex("三天前的会话"), { right: true });
await clickText(".menu-item", "归档");
await waitCalls("session.archive");
ok("归档 is session.archive for the row", isDeepStrictEqual(await lastCall("session.archive"), { id: "s-saved-2" }), await lastCall("session.archive"));
await page.waitFor("document.querySelector('.archived .pname')?.textContent === '已归档 · 1'", 3000);
ok("the row leaves its project and the archived list counts it", await js("![...document.querySelectorAll('.srow:not(.archived-row) .stitle')].some(e => e.textContent === '三天前的会话')"));
await page.click(".archived .proj-toggle");
ok("the archived list shows it by its new name", await js("document.querySelector('.archived-row .stitle')?.textContent === '三天前的会话'"));
await page.click(".archived-row .more");
await waitCalls("session.unarchive");
ok("取消归档 is session.unarchive", isDeepStrictEqual(await lastCall("session.unarchive"), { id: "s-saved-2" }), await lastCall("session.unarchive"));
await page.waitFor("!document.querySelector('.archived') && [...document.querySelectorAll('.srow .stitle')].some(e => e.textContent === '三天前的会话')", 3000);
ok("and the row is back in its project under its new name, the archived list gone", true);

// ------------------------------------------------------------------------------------- 15. failed calls
await js(`__fake.failNext("project.remove", "测试：这个项目现在不能移除。")`);
await page.click(".phead-row", 1, { right: true });
await clickText(".menu-item", "从列表中移除");
await page.click(".dialog .danger-solid");
await page.waitFor("document.querySelector('.notice-text')?.textContent.includes('不能移除')", 3000);
ok("a refused call says why in the status bar, and nothing else happens", await js("document.querySelector('.sidebar').textContent.includes('second-project')"));
await js(`__fake.failNext("session.new", "最多同时打开 4 个会话；请先停止或关闭一个。")`);
const tabsNow = await js("document.querySelectorAll('.tab').length");
await press("n", "KeyN", 78, CTRL);
await page.click(".input");
await page.type("still here");
await page.key("Enter");
await page.waitFor("document.querySelector('.notice-text')?.textContent.includes('最多同时打开')", 3000);
ok("a session that cannot start opens no tab, and what was typed is still in the message box", (await js("document.querySelectorAll('.tab').length")) === tabsNow && (await js("document.querySelector('.input').value")) === "still here");

// ------------------------------------------------------------------------------------- 16. the window
const handles = await js("[...document.querySelectorAll('.rz')].map(e => e.dataset.edge + ':' + getComputedStyle(e).cursor).join()");
ok("the outline is a 1 px line along the whole window, with the corners Windows 11 gives it, and takes no clicks", await js("(() => { const f = document.querySelector('.frame-line'); const s = getComputedStyle(f); const r = f.getBoundingClientRect(); return s.position === 'fixed' && s.pointerEvents === 'none' && s.borderTopWidth === '1px' && s.borderTopLeftRadius === '8px' && r.left === 0 && r.top === 0 && r.width === innerWidth && r.height === innerHeight; })()"));
ok("eight invisible resize handles with the system's resize cursors", handles === "n:ns-resize,s:ns-resize,e:ew-resize,w:ew-resize,nw:nwse-resize,ne:nesw-resize,sw:nesw-resize,se:nwse-resize", handles);
ok("6 px along the edges, 10 px in the corners", await js("(() => { const r = (e) => document.querySelector('.rz-' + e).getBoundingClientRect(); return r('n').height === 6 && r('e').width === 6 && r('nw').width === 10 && r('se').height === 10 && r('s').bottom === innerHeight && r('e').right === innerWidth; })()"));
for (const edge of ["n", "s", "e", "w", "nw", "ne", "sw", "se"]) {
  const h = await page.rect(`.rz-${edge}`);
  await page.mouse("mouseMoved", h.x + h.w / 2, h.y + h.h / 2, { button: "none", clickCount: 0 });
  await page.mouse("mousePressed", h.x + h.w / 2, h.y + h.h / 2);
  await page.mouse("mouseReleased", h.x + h.w / 2, h.y + h.h / 2);
}
await waitCalls("window.startResize", 8);
ok("pressing a handle asks Python to resize from that edge", (await calls("window.startResize")).map((c) => c.edge).join() === "n,s,e,w,nw,ne,sw,se", await calls("window.startResize"));
await page.click(".titlebar .tb-drag");
await waitCalls("window.startMove");
ok("pressing the title bar moves the window (window.startMove)", true);
await page.click(".titlebar .tb-btn", 1);
await waitCalls("window.toggleMaximize");
await page.waitFor("document.querySelectorAll('.titlebar .tb-btn')[1].getAttribute('aria-label') === '还原'", 2000);
ok("maximized: the button says 还原 and the handles are gone", await js("document.querySelectorAll('.rz').length === 0"));
ok("…and so is the outline (a maximized window fills the screen)", await js("!document.querySelector('.frame-line')"));
await emit("window", { maximized: false });
await page.waitFor("document.querySelectorAll('.rz').length === 8", 2000);
ok("the window event (restored from outside) brings them back", await js("document.querySelectorAll('.titlebar .tb-btn')[1].getAttribute('aria-label') === '最大化'"));
ok("…and the outline", await js("!!document.querySelector('.frame-line.round')"));
await page.click(".titlebar .tb-btn", 0);
await waitCalls("window.minimize");
ok("minimize goes to Python", true);
await js(`__fake.patch("c1", { mark: "running", workingLabel: "正在运行…" })`);
await page.waitFor("!!document.querySelector('.tab .dot-running')", 2000);
await emit("closeRequested", {});
await page.waitFor("!!document.querySelector('.dialog')", 2000);
ok("closed from outside while a session works: the page asks first", await js("document.querySelector('.dialog').textContent.includes('退出 WittyCode') && document.querySelector('.dialog').textContent.includes('1 个会话还在工作')"));
ok("…and does not close yet", (await calls("window.close")).length === 0);
await page.click(".dialog .danger-solid");
await waitCalls("window.close");
ok("confirming closes the window (window.close)", true);
await js(`__fake.patch("c1", { mark: "ready", workingLabel: "就绪" })`);
await page.waitFor("!document.querySelector('.tab .dot-running')", 2000);
await emit("closeRequested", {});
await waitCalls("window.close", 2);
ok("with nothing working it closes at once", await js("!document.querySelector('.dialog')"));
await blur();
await press("q", "KeyQ", 81, CTRL);
await waitCalls("window.close", 3);
ok("Ctrl+Q runs the same quit", true);

// ------------------------------------------------------------------------------------- 17. the clock
await js(`__fake.addSaved("s-clock", __fake.OTHER, "时钟测试", Date.now() - 58000)`);
await page.waitFor("[...document.querySelectorAll('.srow')].some(r => r.textContent.includes('时钟测试'))", 3000);
const clockRow = "[...document.querySelectorAll('.srow')].find(r => r.textContent.includes('时钟测试')).querySelector('.ssub').textContent";
ok("a row from 58 s ago reads 刚刚", (await js(clockRow)) === "刚刚", await js(clockRow));
// (a page that has been in the background for minutes may run its timers only once a minute)
await page.waitFor(`${clockRow} === '1 分钟前'`, 75000);
ok("…and 1 分钟前 within 30 s, with no new event from Python", true);

const errors = page.errorTexts();
ok("no console errors, no CSP violations", errors.length === 0, errors);
await page.close();

// ------------------------------------------------------------------------------------- without Python
const lonely = await openPage(`${index}?live=1`, { width: 1000, height: 700 });
ok("?live=1 without a transport: the title bar and why it cannot connect", await lonely.eval("!!document.querySelector('.titlebar') && document.querySelector('.boot').textContent.includes('没有连接到 WittyCode 后台') && !document.querySelector('.sidebar')"));
ok("no demo data there either", await lonely.eval(`(() => { const t = document.body.innerText; return !${JSON.stringify(MOCK)}.some((m) => t.includes(m)); })()`));
ok("and no outline: only Python asks for one (?frame=)", await lonely.eval("!document.querySelector('.frame-line')"));
await lonely.close();
const squareCorners = await openPage(`${index}?live=1&frame=square`, { width: 1000, height: 700 });
ok("?frame=square (Windows 10): the same line with square corners", await squareCorners.eval("(() => { const f = document.querySelector('.frame-line'); return !!f && !f.classList.contains('round') && getComputedStyle(f).borderTopLeftRadius === '0px'; })()"));
await squareCorners.close();

console.log(failed ? `${failed} check(s) FAILED` : "all live checks passed");
process.exit(failed ? 1 : 0);
