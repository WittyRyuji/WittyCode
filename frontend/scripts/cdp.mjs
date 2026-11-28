// A tiny Chrome DevTools Protocol driver (Node's built-in WebSocket, no dependencies) for looking at and poking the
// design page the way a user would: real mouse and keyboard events, screenshots, DOM queries.
//
//   const page = await openPage("file:///.../dist/index.html?scene=done", { width: 1500, height: 960 });
//   const live = await openPage(url, { init: [fakeBackendSource] }); // scripts that run before the page's own
//   await page.click(".newbtn");               // real mouse press / release at the element's centre
//   await page.type("hello");                  // real key events
//   console.log(await page.eval("document.title"));
//   await page.shot("out.png");
//   await page.close();

import { spawn, spawnSync } from "node:child_process";
import { mkdirSync, mkdtempSync, readFileSync, rmSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { dirname, join } from "node:path";

const EDGE = process.env.EDGE_PATH ?? "C:\\Program Files (x86)\\Microsoft\\Edge\\Application\\msedge.exe";
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
const ownedBrowsers = new Set();
// A check can throw before its page.close(). Never leave that check's own browser tree behind on an error exit.
process.once("exit", () => {
  for (const child of ownedBrowsers) if (child.pid && child.exitCode === null) {
    spawnSync("taskkill", ["/PID", String(child.pid), "/T", "/F"], { stdio: "ignore", windowsHide: true });
  }
});

/** Save the PNG returned by CDP. Kept separate so filesystem behaviour can be checked without starting a browser. */
export function writeScreenshot(file, data) {
  mkdirSync(dirname(file), { recursive: true });
  writeFileSync(file, Buffer.from(data, "base64"));
}

/** Every page reports what its Content-Security-Policy blocked as a console error, so "no console errors" also means
 *  "nothing was blocked". */
const CSP_WATCH = `document.addEventListener("securitypolicyviolation", (e) => console.error("CSP violation: " + e.violatedDirective + " " + (e.blockedURI || "")));`;

/** `init`: script sources evaluated in every new document before the page's own scripts. */
export async function openPage(url, { width = 1500, height = 960, port = 0, init = [], hideScrollbars = true } = {}) {
  const profile = mkdtempSync(join(tmpdir(), "wc-cdp-"));
  const proc = spawn(
    EDGE,
    ["--headless=new", "--disable-gpu", "--disable-background-timer-throttling", "--disable-renderer-backgrounding", "--disable-backgrounding-occluded-windows", ...(hideScrollbars ? ["--hide-scrollbars"] : []), "--no-first-run", `--user-data-dir=${profile}`, `--remote-debugging-port=${port}`, `--window-size=${width},${height}`, "about:blank"],
    { stdio: "ignore" },
  );
  ownedBrowsers.add(proc);
  proc.once("exit", () => ownedBrowsers.delete(proc));
  let targets;
  for (let i = 0; i < 80; i++) {
    try {
      const activePort = port || Number(readFileSync(join(profile, "DevToolsActivePort"), "utf8").split("\n")[0]);
      targets = await (await fetch(`http://127.0.0.1:${activePort}/json`)).json();
      if (targets.some((t) => t.type === "page")) break;
    } catch {
      /* not up yet */
    }
    await sleep(150);
  }
  const target = targets?.find((t) => t.type === "page");
  if (!target) throw new Error("the browser did not start");

  const ws = new WebSocket(target.webSocketDebuggerUrl);
  await new Promise((resolve, reject) => {
    ws.onopen = resolve;
    ws.onerror = reject;
  });
  let id = 0;
  const waiting = new Map();
  const events = [];
  const contexts = new Map();
  ws.onmessage = (m) => {
    const msg = JSON.parse(m.data);
    if (msg.id && waiting.has(msg.id)) {
      const { resolve, reject } = waiting.get(msg.id);
      waiting.delete(msg.id);
      msg.error ? reject(new Error(msg.error.message)) : resolve(msg.result);
    } else if (msg.method) {
      events.push(msg);
      if (msg.method === "Runtime.executionContextCreated") contexts.set(msg.params.context.id, msg.params.context);
      if (msg.method === "Runtime.executionContextDestroyed") contexts.delete(msg.params.executionContextId);
      if (msg.method === "Runtime.executionContextsCleared") contexts.clear();
    }
  };
  const send = (method, params = {}) =>
    new Promise((resolve, reject) => {
      const n = ++id;
      waiting.set(n, { resolve, reject });
      ws.send(JSON.stringify({ id: n, method, params }));
    });

  await send("Page.enable");
  await send("Runtime.enable");
  for (const source of [CSP_WATCH, ...init]) await send("Page.addScriptToEvaluateOnNewDocument", { source });
  await send("Emulation.setDeviceMetricsOverride", { width, height, deviceScaleFactor: 1, mobile: false });
  // A background headless target can otherwise become hidden and stop IntersectionObserver/keyboard updates.
  await send("Emulation.setFocusEmulationEnabled", { enabled: true });
  const { frameId } = await send("Page.navigate", { url });

  const resultValue = (r) => {
    if (r.exceptionDetails) throw new Error(r.exceptionDetails.exception?.description ?? r.exceptionDetails.text);
    return r.result.value;
  };
  const resultOptions = { returnByValue: true, awaitPromise: true };
  const evalJs = async (expression) => resultValue(await send("Runtime.evaluate", { expression, ...resultOptions }));
  // Only repository-owned function definitions cross as code. DOM selectors, text and fixtures are CDP values.
  const call = async (fn, ...args) => {
    if (typeof fn !== "function") throw new TypeError("CDP call requires a fixed function");
    const context = [...contexts.values()].find((c) => c.auxData?.isDefault && c.auxData.frameId === frameId);
    if (!context) throw new Error("the test page has no main execution context");
    return resultValue(await send("Runtime.callFunctionOn", {
      executionContextId: context.id, functionDeclaration: Function.prototype.toString.call(fn),
      arguments: args.map((value) => ({ value })), ...resultOptions,
    }));
  };
  const waitUntil = async (check, label, timeout) => {
    const end = Date.now() + timeout;
    while (Date.now() < end) {
      if (await check()) return true;
      await sleep(100);
    }
    throw new Error(`timed out waiting for: ${label}`);
  };
  const elementRect = (selector, index, scroll) => {
    const e = document.querySelectorAll(selector)[index];
    if (!e) return null;
    if (scroll) e.scrollIntoView({ block: "center" });
    const r = e.getBoundingClientRect();
    return { x: r.left, y: r.top, w: r.width, h: r.height };
  };

  const page = {
    browserPid: proc.pid,
    send,
    eval: evalJs, // Fixed source strings only. Use call() for any values supplied at runtime.
    call,
    async findText(selector, text, { exact = false } = {}) {
      return call((css, label, strict) => [...document.querySelectorAll(css)].findIndex((e) =>
        strict ? e.textContent.trim() === label : e.textContent.includes(label)), selector, text, exact);
    },
    async setInput(selector, value, { blur = false } = {}) {
      await call((css, text, unfocus) => {
        const e = document.querySelector(css);
        const proto = e instanceof HTMLTextAreaElement ? HTMLTextAreaElement.prototype : HTMLInputElement.prototype;
        Object.getOwnPropertyDescriptor(proto, "value").set.call(e, text);
        e.dispatchEvent(new Event("input", { bubbles: true }));
        if (unfocus) e.blur();
      }, selector, value, blur);
    },
    events,
    consoleErrors: () => events.filter((e) => e.method === "Runtime.exceptionThrown" || (e.method === "Runtime.consoleAPICalled" && e.params.type === "error")),
    /** the console errors as text, for a failure message */
    errorTexts() {
      return this.consoleErrors().map((e) =>
        e.method === "Runtime.exceptionThrown"
          ? (e.params.exceptionDetails.exception?.description ?? e.params.exceptionDetails.text)
          : e.params.args.map((a) => a.value ?? a.description ?? "").join(" "),
      );
    },
    cspViolations() {
      return this.errorTexts().filter((t) => t.startsWith("CSP violation"));
    },
    async rect(selector, index = 0) {
      await this.waitForCall((css, i) => !!document.querySelectorAll(css)[i], [selector, index]);
      const r = await call(elementRect, selector, index, true);
      if (!r) throw new Error(`no element for ${selector}[${index}]`);
      let previous = r, stable = 0;
      const end = Date.now() + 2500;
      while (Date.now() < end) {
        // Do not await renderer rAF here: an occluded headless window can stop emitting frames.
        await sleep(25);
        const current = await call(elementRect, selector, index, false);
        if (!current) throw new Error(`no element for ${selector}[${index}]`);
        stable = Object.keys(current).every(k => Math.abs(current[k] - previous[k]) < 0.1) ? stable + 1 : 0;
        if (stable >= 2 && current.w > 0 && current.h > 0) return current;
        previous = current;
      }
      throw new Error(`the target did not settle: ${selector}[${index}]`);
    },
    async mouse(type, x, y, extra = {}) {
      await send("Input.dispatchMouseEvent", { type, x, y, button: "left", clickCount: 1, ...extra });
    },
    async hover(selector, index = 0) {
      const r = await this.rect(selector, index);
      await this.mouse("mouseMoved", r.x + r.w / 2, r.y + r.h / 2, { button: "none", clickCount: 0 });
    },
    async click(selector, index = 0, { right = false, ctrl = false } = {}) {
      let r = await this.rect(selector, index);
      // Moving the real pointer can start a compositor frame or change a hovered target's layout.
      for (let attempt = 0; attempt < 3; attempt++) {
        await this.mouse("mouseMoved", r.x + r.w / 2, r.y + r.h / 2, { button: "none", clickCount: 0 });
        const next = await this.rect(selector, index);
        const settled = Object.keys(next).every(k => Math.abs(next[k] - r[k]) < 0.1);
        r = next;
        if (settled) break;
        if (attempt === 2) throw new Error(`the hovered target did not settle: ${selector}[${index}]`);
      }
      const x = r.x + r.w / 2;
      const y = r.y + r.h / 2;
      const extra = { ...(right ? { button: "right" } : {}), ...(ctrl ? { modifiers: 2 } : {}) };
      await this.mouse("mousePressed", x, y, extra);
      await this.mouse("mouseReleased", x, y, extra);
      await sleep(120);
    },
    async type(text) {
      for (const ch of text) {
        await send("Input.dispatchKeyEvent", { type: "char", text: ch });
      }
      await sleep(80);
    },
    async key(key, { code = key, vk, modifiers = 0 } = {}) {
      const map = { Enter: 13, Escape: 27, Tab: 9, ArrowDown: 40, ArrowUp: 38 };
      const windowsVirtualKeyCode = vk ?? map[key] ?? key.toUpperCase().charCodeAt(0);
      const text = key === "Enter" ? "\r" : undefined;
      await send("Input.dispatchKeyEvent", { type: "keyDown", key, code, windowsVirtualKeyCode, modifiers, text });
      await send("Input.dispatchKeyEvent", { type: "keyUp", key, code, windowsVirtualKeyCode, modifiers });
      await sleep(120);
    },
    async wait(ms) {
      await sleep(ms);
    },
    waitFor(expression, timeout = 8000) {
      return waitUntil(() => evalJs(`!!(${expression})`), expression, timeout);
    },
    waitForCall(fn, args, timeout = 8000) {
      return waitUntil(() => call(fn, ...args), Function.prototype.toString.call(fn), timeout);
    },
    async shot(file) {
      const r = await send("Page.captureScreenshot", { format: "png" });
      writeScreenshot(file, r.data);
    },
    async close() {
      try {
        await send("Browser.close");
      } catch {
        /* closing */
      }
      ws.close();
      proc.kill();
      await sleep(300);
      try {
        rmSync(profile, { recursive: true, force: true });
      } catch {
        /* still locked: the temp folder is cleaned by the system */
      }
    },
  };
  try {
    // A slow clean runner may still be loading the bundle after 1.2s. Wait for the actual React tree, not a stopwatch.
    await page.waitFor("document.readyState === 'complete' && (!document.getElementById('root') || !!document.querySelector('.titlebar, .app .input'))", 30000);
    await evalJs("document.fonts.ready");
    await evalJs("new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve)))");
    if (page.consoleErrors().length) throw new Error(page.errorTexts().join("\n"));
  } catch (error) {
    await page.close();
    throw error;
  }
  return page;
}
