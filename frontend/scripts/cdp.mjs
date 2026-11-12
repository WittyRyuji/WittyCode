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

import { spawn } from "node:child_process";
import { mkdirSync, mkdtempSync, rmSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { dirname, join } from "node:path";

const EDGE = process.env.EDGE_PATH ?? "C:\\Program Files (x86)\\Microsoft\\Edge\\Application\\msedge.exe";
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

/** Save the PNG returned by CDP. Kept separate so filesystem behaviour can be checked without starting a browser. */
export function writeScreenshot(file, data) {
  mkdirSync(dirname(file), { recursive: true });
  writeFileSync(file, Buffer.from(data, "base64"));
}

/** Every page reports what its Content-Security-Policy blocked as a console error, so "no console errors" also means
 *  "nothing was blocked". */
const CSP_WATCH = `document.addEventListener("securitypolicyviolation", (e) => console.error("CSP violation: " + e.violatedDirective + " " + (e.blockedURI || "")));`;

/** `init`: script sources evaluated in every new document before the page's own scripts. */
export async function openPage(url, { width = 1500, height = 960, port = 9333 + Math.floor(Math.random() * 400), init = [] } = {}) {
  const profile = mkdtempSync(join(tmpdir(), "wc-cdp-"));
  const proc = spawn(
    EDGE,
    ["--headless=new", "--disable-gpu", "--hide-scrollbars", "--no-first-run", `--user-data-dir=${profile}`, `--remote-debugging-port=${port}`, `--window-size=${width},${height}`, "about:blank"],
    { stdio: "ignore" },
  );
  let targets;
  for (let i = 0; i < 80; i++) {
    try {
      targets = await (await fetch(`http://127.0.0.1:${port}/json`)).json();
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
  ws.onmessage = (m) => {
    const msg = JSON.parse(m.data);
    if (msg.id && waiting.has(msg.id)) {
      const { resolve, reject } = waiting.get(msg.id);
      waiting.delete(msg.id);
      msg.error ? reject(new Error(msg.error.message)) : resolve(msg.result);
    } else if (msg.method) events.push(msg);
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
  await send("Page.navigate", { url });
  await sleep(1200);

  const evalJs = async (expression) => {
    const r = await send("Runtime.evaluate", { expression, returnByValue: true, awaitPromise: true });
    if (r.exceptionDetails) throw new Error(r.exceptionDetails.exception?.description ?? r.exceptionDetails.text);
    return r.result.value;
  };

  const page = {
    send,
    eval: evalJs,
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
      const r = await evalJs(
        `(() => { const e = document.querySelectorAll(${JSON.stringify(selector)})[${index}]; if (!e) return null; e.scrollIntoView({block: "center"}); const r = e.getBoundingClientRect(); return {x: r.left, y: r.top, w: r.width, h: r.height}; })()`,
      );
      if (!r) throw new Error(`no element for ${selector}[${index}]`);
      return r;
    },
    async mouse(type, x, y, extra = {}) {
      await send("Input.dispatchMouseEvent", { type, x, y, button: "left", clickCount: 1, ...extra });
    },
    async hover(selector, index = 0) {
      const r = await this.rect(selector, index);
      await this.mouse("mouseMoved", r.x + r.w / 2, r.y + r.h / 2, { button: "none", clickCount: 0 });
    },
    async click(selector, index = 0, { right = false, ctrl = false } = {}) {
      const r = await this.rect(selector, index);
      const x = r.x + r.w / 2;
      const y = r.y + r.h / 2;
      const extra = { ...(right ? { button: "right" } : {}), ...(ctrl ? { modifiers: 2 } : {}) };
      await this.mouse("mouseMoved", x, y, { button: "none", clickCount: 0 });
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
    async waitFor(expression, timeout = 8000) {
      const end = Date.now() + timeout;
      while (Date.now() < end) {
        if (await evalJs(`!!(${expression})`)) return true;
        await sleep(100);
      }
      throw new Error(`timed out waiting for: ${expression}`);
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
  return page;
}
