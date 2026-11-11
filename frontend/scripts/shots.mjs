// Screenshots of every scene in both themes, for the review and for looking at the page: `node scripts/shots.mjs [filter]`.
import { mkdirSync } from "node:fs";
import { resolve } from "node:path";
import { pathToFileURL } from "node:url";
import { openPage } from "./cdp.mjs";

const out = resolve("shots");
mkdirSync(out, { recursive: true });
const index = pathToFileURL(resolve("dist/index.html")).href;
const only = process.argv[2];

const scenes = [
  { id: "done", q: "scene=done&expand=groups", scroll: "top" },
  { id: "long-top", q: "scene=long", scroll: "top", h: 960 },
  { id: "long-end", q: "scene=long", scroll: "end", h: 960 },
  { id: "live", q: "scene=live&expand=all", scroll: "end" },
  { id: "approve", q: "scene=approve&expand=all", scroll: "end", h: 1000 },
  { id: "ask", q: "scene=ask", scroll: "end" },
  { id: "dead", q: "scene=dead" },
  { id: "empty-session", q: "scene=empty" },
  { id: "no-session", q: "scene=none" },
  { id: "narrow", q: "scene=done&expand=groups&width=800", scroll: "end" },
  { id: "dialog", q: "scene=live&dialog=1" },
  { id: "lightbox", q: "scene=long&lightbox=1" },
  { id: "drop", q: "scene=empty&drop=1&notice=1" },
  { id: "settings-appearance", q: "scene=done&settings=appearance" },
  { id: "settings-defaults", q: "scene=done&settings=defaults" },
  { id: "settings-droid", q: "scene=done&settings=droid" },
  { id: "settings-about", q: "scene=done&settings=about" },
  { id: "settings-shortcuts", q: "scene=done&settings=shortcuts" },
  { id: "settings-memory", q: "scene=done&settings=memory", h: 960 },
  { id: "settings-memory-open", q: "scene=done&settings=memory", h: 960, after: async (p) => { await p.click(".mem-note-head", 1); } },
  { id: "settings-memory-new", q: "scene=done&settings=memory", h: 960, after: async (p) => { await p.click(".mem-head .btn", 1); } },
  { id: "accounts", q: "scene=done&settings=providers", h: 960 },
  { id: "accounts-codex-signed-out", q: "scene=done&settings=providers&codex=signed-out", h: 960 },
  { id: "accounts-codex-missing-no-providers", q: "scene=done&settings=providers&codex=missing&providers=none", h: 960 },
  { id: "provider-new", q: "scene=done&settings=providers&provider=new:openrouter", h: 960 },
  { id: "provider-edit", q: "scene=done&settings=providers&provider=openrouter", h: 960 },
  { id: "provider-env-unset", q: "scene=done&settings=providers&provider=new:groq", h: 960 },
  { id: "provider-plaintext-key", q: "scene=done&settings=providers&provider=anthropic-direct", h: 960 },
  { id: "provider-bedrock", q: "scene=done&settings=providers&provider=new:bedrock", h: 960 },
  { id: "title-bar-menu", q: "scene=done", after: async (p) => { await p.click(".titlebar .tb-menu"); } },
  { id: "bare-window", q: "scene=done&review=0" },
  { id: "switched-to-codex", q: "scene=done&switch=codex", scroll: "end" },
  { id: "switched-back", q: "scene=done&switch=droid", scroll: "end", h: 1000 },
  { id: "session-codex", q: "scene=codex" },
  { id: "session-api", q: "scene=api" },
  { id: "guide", q: "scene=done&guide=1", h: 1000 },
  // interactions: a menu or a tooltip opened with the real mouse
  { id: "menu-reasoning-opus", q: "scene=approve", scroll: "end", after: async (p) => { await p.click(".controls .chip", 1); } },
  { id: "menu-permission", q: "scene=done", after: async (p) => { await p.click(".controls .chip", 2); } },
  { id: "menu-mode", q: "scene=done", after: async (p) => { await p.click(".controls .chip", 3); } },
  { id: "menu-model", q: "scene=done", after: async (p) => { await p.click(".controls .chip", 0); } },
  { id: "menu-model-tall", q: "scene=done", h: 1000, after: async (p) => { await p.click(".controls .chip", 0); } },
  { id: "menu-model-in-codex", q: "scene=done&switch=codex", scroll: "end", h: 1000, after: async (p) => { await p.click(".controls .chip", 0); } },
  { id: "tip-token", q: "scene=review", after: async (p) => { await p.hover(".usage"); await p.wait(700); } },
  { id: "tip-codex-limit", q: "scene=codex", after: async (p) => { await p.hover(".usage", 1); await p.wait(700); } },
  { id: "tip-context", q: "scene=long", after: async (p) => { await p.hover(".ctxmeter"); await p.wait(700); } },
];

for (const theme of ["dark", "light"]) {
  for (const s of scenes) {
    const name = `${theme}-${s.id}`;
    if (only && !name.includes(only)) continue;
    const q = s.q === "scene=review" ? "scene=long" : s.q;
    const page = await openPage(`${index}?${q}&theme=${theme}`, { width: 1500, height: s.h ?? 900 });
    if (s.scroll) await page.eval(`(() => { const el = document.querySelector(".scroller"); if (el) el.scrollTop = ${s.scroll === "top" ? 0 : "el.scrollHeight"}; })()`);
    await page.wait(250);
    if (s.after) await s.after(page);
    await page.wait(200);
    await page.shot(resolve(out, `${name}.png`));
    const errors = page.consoleErrors();
    console.log(name, errors.length ? `ERRORS ${errors.length}` : "ok");
    await page.close();
  }
}
