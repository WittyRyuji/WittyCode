// Real Chromium input and QWebChannel payloads, with visible scrollbars (not the driver's usual screenshot shortcut).
import assert from "node:assert/strict";
import { resolve } from "node:path";
import { pathToFileURL } from "node:url";
import { openPage } from "./cdp.mjs";
import { fakeBackendScripts } from "./fake-backend.mjs";

const index = pathToFileURL(resolve("dist/index.html")).href;
const atEnd = "(() => { const s = document.querySelector('.scroller'); return s.scrollHeight - s.scrollTop - s.clientHeight < 4; })()";
const aligned = "(() => { const t = document.querySelector('.transcript').getBoundingClientRect(), c = document.querySelector('.composer').getBoundingClientRect(); return Math.abs(t.left - c.left) < 1 && Math.abs(t.right - c.right) < 1; })()";
const turn = (i, paragraphs = 12) => ({
  id: `turn-${i}`, status: "done", prompts: [], seconds: 12,
  blocks: [
    { kind: "user", id: `u-${i}`, text: `检查第 ${i + 1} 项布局与交互，保留现有功能。` },
    { kind: "text", id: `x-${i}`, markdown: Array.from({ length: paragraphs }, (_, p) => `第 ${p + 1} 段：正文与输入框使用同一个阅读宽度。计划独立显示，回合导航不会打断你阅读历史。`).join("\n\n") },
  ],
});
const planBlock = (completed = 1) => ({ kind: "activity", id: "plan-group", entries: [{ kind: "tool", id: "plan-tool", name: "TodoWrite", category: "plan", status: "done", todos: ["检查布局", "调整交互", "运行验证"].map((text, i) => ({ text, state: i < completed ? "completed" : i === completed ? "in_progress" : "pending" })) }] });

for (const [theme, width] of [["light", 1500], ["dark", 900]]) {
  const page = await openPage(index, { width, height: 960, init: fakeBackendScripts().scripts, hideScrollbars: false });
  try {
    await page.waitFor("document.querySelectorAll('.srow').length >= 2 && !document.querySelector('.boot')");
    await page.click(".srow", 0);
    await page.waitFor("!!document.querySelector('.tab.current .dot-ready')");
    const id = await page.eval("__fake.events.findLast(([name, payload]) => name === 'tabs' && payload.select)[1].select");
    await page.eval(`__fake.emit('settings', {settings: {maxOpen: 4, theme: ${JSON.stringify(theme)}, fontScale: 'md', sidebarWidth: 300}})`);
    const emit = (turns, all = false) => page.eval(`__fake.emit('turns', ${JSON.stringify({ id, turns, all })})`);
    const turns = Array.from({ length: 6 }, (_, i) => turn(i, i === 5 ? 2 : 12));
    await emit(turns, true);
    await page.waitFor("document.querySelectorAll('.turn-marker').length === 6");
    await page.waitFor(atEnd);
    assert(await page.eval(aligned));
    await page.waitFor("document.querySelectorAll('.turn-marker[aria-current=step]').length === 1 && document.querySelectorAll('.turn-marker')[5].getAttribute('aria-current') === 'step'");
    assert(await page.eval("document.querySelector('.turn-nav').getBoundingClientRect().right < document.querySelector('.transcript').getBoundingClientRect().left"), "the outline belongs in the left gutter, not on top of the messages");
    assert(await page.eval("(() => { const a = [...document.querySelectorAll('.turn-marker')].map(e => e.getBoundingClientRect()); return a.every((r, i) => r.height <= 12 && (!i || r.top - a[i - 1].top <= 12)); })()"), "markers use a compact 12px pitch");
    const navTop = await page.eval("document.querySelector('.turn-nav').getBoundingClientRect().top");
    const fillDraft = (text) => page.eval(`(() => { const e = document.querySelector('.input'); Object.getOwnPropertyDescriptor(HTMLTextAreaElement.prototype, 'value').set.call(e, ${JSON.stringify(text)}); e.dispatchEvent(new Event('input', {bubbles: true})); })()`);
    await fillDraft("输入多行草稿，导航保持固定。\n".repeat(12));
    await page.wait(180);
    assert(Math.abs(await page.eval("document.querySelector('.turn-nav').getBoundingClientRect().top") - navTop) < 1, "growing the input does not move the outline");
    assert(await page.eval(atEnd), "growing the input preserves following latest");
    await fillDraft("");
    await page.wait(150);
    await page.click('.srow', 1);
    await page.waitFor("document.querySelectorAll('.tab').length === 2 && !!document.querySelector('.tab.current .dot-ready')");
    await page.click('.tab', 0);
    await page.waitFor("document.querySelectorAll('.turn-marker').length === 6");
    assert(await page.eval(atEnd), "returning to a session keeps latest-follow mode");
    await page.hover(".turn-marker", 0);
    await page.waitFor("!!document.querySelector('.turn-preview')");
    assert((await page.eval("document.querySelector('.turn-preview').textContent")).includes("检查第 1 项布局"));
    await page.click(".turn-marker", 0);
    await page.waitFor("document.querySelector('.scroller').scrollTop < 2");
    assert(await page.eval("!!document.querySelector('.jump') && document.querySelector('.turn-marker').getAttribute('aria-current') === 'step'"));
    await page.key("ArrowDown");
    await page.waitFor("(() => { const s = document.querySelector('.scroller').getBoundingClientRect(), t = document.querySelectorAll('.transcript > .turn')[1].getBoundingClientRect(); return Math.abs(t.top - s.top - 28) < 2; })()");
    assert.equal(await page.eval("document.activeElement.dataset.turnId"), "turn-1");
    const historicalTop = await page.eval("document.querySelector('.scroller').scrollTop");
    await page.click('.tab', 1);
    await page.click('.tab', 0);
    await page.waitFor("document.querySelectorAll('.turn-marker').length === 6");
    assert(Math.abs(await page.eval("document.querySelector('.scroller').scrollTop") - historicalTop) < 2, "each session restores its historical reading position");
    assert.equal(await page.eval("document.querySelector('.turn-marker[aria-current=step]')?.dataset.turnId"), "turn-1");
    await fillDraft("继续输入多行，不抢历史位置。\n".repeat(10));
    await page.wait(150);
    assert(Math.abs(await page.eval("document.querySelector('.scroller').scrollTop") - historicalTop) < 2);
    assert(Math.abs(await page.eval("document.querySelector('.turn-nav').getBoundingClientRect().top") - navTop) < 1);
    await fillDraft("");
    await page.eval("document.querySelector('.turn-marker[aria-current=step]').focus({preventScroll: true})");
    await page.key("Home", { vk: 36 });
    await page.waitFor("document.querySelector('.scroller').scrollTop < 2");
    turns[5].blocks[1].markdown += "\n\n" + "后台仍在持续输出，但历史阅读位置不应跳走。\n\n".repeat(50);
    await emit([turns[5]]);
    await page.wait(250);
    assert(await page.eval("document.querySelector('.scroller').scrollTop < 2 && document.querySelector('.turn-marker').getAttribute('aria-current') === 'step'"), "streaming the latest turn never steals a historical navigation position");
    await page.eval("(() => { const s = document.querySelector('.scroller'), t = document.querySelectorAll('.transcript > .turn')[3]; s.scrollTop += t.getBoundingClientRect().top - s.getBoundingClientRect().top - 28; })()");
    await page.waitFor("document.querySelectorAll('.turn-marker')[3].getAttribute('aria-current') === 'step'");
    await page.click(".jump");
    await page.waitFor(atEnd);
    await page.waitFor("!document.querySelector('.jump')");
    assert(await page.eval("document.querySelectorAll('.turn-marker')[5].getAttribute('aria-current') === 'step'"));
    turns[5].blocks[1].markdown = "最新一轮的简短回答。";
    await emit([turns[5]]);
    await page.waitFor(atEnd);
    await page.eval("document.querySelector('.scroller').scrollTop -= 24");
    await page.wait(150);
    assert(await page.eval("!document.querySelector('.jump') && document.querySelectorAll('.turn-marker')[5].getAttribute('aria-current') === 'step'"), "near-bottom follow and the active marker use the same boundary, without jumping to an older turn");
    await page.eval("window.dispatchEvent(new Event('wc:scroll-end'))");
    await page.waitFor(atEnd);
    assert(await page.eval("(() => { const e = document.querySelector('.scroller'); return getComputedStyle(e).scrollbarWidth === 'auto' && getComputedStyle(e, '::-webkit-scrollbar').width === '8px' && getComputedStyle(e, '::-webkit-scrollbar-button').display === 'none' && parseFloat(getComputedStyle(e, '::-webkit-scrollbar-thumb').borderRadius) >= 8; })()"), "the native scrollbar is narrow, rounded and has no arrow buttons");
    const bar = await page.eval("(() => { const e = document.querySelector('.scroller'), r = e.getBoundingClientRect(); return {x: r.right - 4, top: r.top, bottom: r.bottom, thumb: Math.max(18, e.clientHeight * e.clientHeight / e.scrollHeight), maximum: e.scrollHeight - e.clientHeight}; })()");
    assert((await page.eval(`document.elementFromPoint(${bar.x}, ${bar.bottom - bar.thumb / 2})?.className`)).includes("scroller"), "the window resize handle must not intercept the scrollbar");
    await page.mouse("mouseMoved", bar.x, bar.bottom - bar.thumb / 2, { button: "none", clickCount: 0 });
    await page.mouse("mousePressed", bar.x, bar.bottom - bar.thumb / 2);
    await page.mouse("mouseMoved", bar.x, bar.top + 100, { buttons: 1 });
    await page.mouse("mouseReleased", bar.x, bar.top + 100);
    await page.waitFor(`document.querySelector('.scroller').scrollTop < ${bar.maximum * 0.75}`);
    await page.hover(".titlebar");
    await page.wait(1000);
    assert.equal(await page.eval("getComputedStyle(document.querySelector('.scroller'), '::-webkit-scrollbar-thumb').backgroundColor"), "rgba(0, 0, 0, 0)", "the scrollbar fades out when idle");

    turns[5].blocks.push(planBlock());
    await emit([turns[5]]);
    await page.waitFor("!!document.querySelector('.planpanel')");
    assert(await page.eval(aligned));
    await page.click(".plan-close");
    turns[5].blocks[2] = planBlock(2);
    await emit([turns[5]]);
    await page.waitFor("document.querySelector('.plan-toggle').textContent.includes('2/3')");
    assert(await page.eval("!document.querySelector('.planpanel')"), "progress does not reopen a dismissed rail");
    await page.click(".newbtn");
    assert(await page.eval("!document.querySelector('.turn-nav, .planpanel, .plan-toggle') && !document.querySelector('.main .transcript')"));
    await page.click(".tab", 0);
    await page.waitFor("document.querySelectorAll('.turn-marker').length === 6");
    assert(await page.eval("!document.querySelector('.planpanel') && document.querySelectorAll('.main .transcript').length === 1"));
    await page.click(".plan-toggle");
    await page.waitFor("document.querySelector('.plan-count')?.textContent === '2/3'");
    assert(await page.eval(aligned));
    await page.shot(resolve(`shots/conversation-navigation-${theme}-${width}.png`));
    await page.click(".plan-toggle");
    turns[5].blocks[2].entries[0].todos = [];
    await emit([turns[5]]);
    await page.waitFor("!document.querySelector('.plan-toggle')");

    await page.eval("window.dispatchEvent(new Event('wc:scroll-end'))");
    await page.waitFor(atEnd);
    await emit(Array.from({ length: 80 }, (_, i) => turn(i, 1)), true);
    await page.waitFor("document.querySelectorAll('.turn-marker').length === 80");
    await page.waitFor(atEnd);
    assert(await page.eval("(() => { const n = document.querySelector('.turn-nav'), s = document.querySelector('.stage').getBoundingClientRect(), r = n.getBoundingClientRect(); return n.scrollHeight > n.clientHeight && r.top >= s.top && r.bottom <= s.bottom && document.querySelectorAll('.main .transcript').length === 1; })()"));
    await page.eval("document.querySelector('.turn-marker[aria-current=step]').focus()");
    await page.key("Home", { vk: 36 });
    await page.waitFor("document.querySelector('.scroller').scrollTop < 2 && document.querySelector('.turn-nav').scrollTop < 2");
    await page.send("Emulation.setEmulatedMedia", { features: [{ name: "prefers-reduced-motion", value: "reduce" }] });
    await page.key("End", { vk: 35 });
    await page.waitFor(atEnd, 1000);
    assert.equal(await page.eval("document.activeElement.dataset.turnId"), "turn-79");
    await emit(Array.from({ length: 80 }, (_, i) => ({ ...turn(i, 1), id: `restored-${i}` })), true);
    await page.waitFor("document.querySelector('.turn-marker[aria-current=step]')?.dataset.turnId === 'restored-79'", 2000);
    assert(await page.eval("document.querySelectorAll('.turn-marker[tabindex=\"0\"]').length === 1"), "a same-size restored snapshot still has an active, keyboard-reachable marker");
    assert.equal(page.consoleErrors().length, 0, page.errorTexts().join("\n"));
    assert.equal(page.cspViolations().length, 0);
    console.log(`PASS conversation navigation: ${theme}, ${width}px, alignment, hover, keyboard, manual scroll, streaming, draggable fading scrollbar, plan dismissal, 80 turns, same-size history replacement, reduced motion`);
  } finally {
    await page.close();
  }
}
