// The screenshots of README.md, from the page with its demo data (no real conversation, no real account):
// `npm run build` first, then `node scripts/readme-shots.mjs [filter]`. They are written to ../docs/screenshots/readme-*.png.
import { mkdirSync } from "node:fs";
import { resolve } from "node:path";
import { pathToFileURL } from "node:url";
import { openPage } from "./cdp.mjs";

const out = resolve("../docs/screenshots");
mkdirSync(out, { recursive: true });
const index = pathToFileURL(resolve("dist/index.html")).href;
const only = process.argv[2];

const scenes = [
  { id: "conversation", q: "scene=done&expand=groups&theme=light&review=0", scroll: "top" },
  { id: "model-menu", q: "scene=done&theme=light&review=0", after: async (p) => { await p.click(".controls .chip", 0); } },
  {
    id: "file-panel",
    q: "scene=done&expand=groups&theme=light&review=0",
    scroll: "top",
    after: async (p) => {
      await p.eval(`[...document.querySelectorAll('.pathref')].find((e) => e.textContent.includes('handoff.md')).click()`);
      await p.wait(600);
    },
  },
  {
    id: "memory-starmap",
    q: "scene=done&settings=memory&theme=dark&review=0",
    h: 1000,
    after: async (p) => {
      await p.eval(`(() => { const b = [...document.querySelectorAll('.mem-head-btns .segc button')].find((e) => e.textContent.trim() === '星图'); b.click(); })()`);
      await p.wait(400);
      await p.eval(`document.querySelector('.mem-map').scrollIntoView({ block: 'start' })`);
    },
  },
  {
    id: "import",
    q: "scene=done&settings=memory&theme=light&review=0",
    h: 1000,
    after: async (p) => {
      await p.eval(`(() => { const b = [...document.querySelectorAll('.mem-head .btn')].find((e) => e.textContent.includes('从其他应用导入')); b.click(); })()`);
      await p.wait(500);
      await p.eval(`document.querySelector('.imp').scrollIntoView({ block: 'start' })`);
    },
  },
];

for (const s of scenes) {
  const name = `readme-${s.id}`;
  if (only && !name.includes(only)) continue;
  const page = await openPage(`${index}?${s.q}`, { width: 1360, height: s.h ?? 820 });
  // (the button that brings back the design review bar belongs to the design page, not to the application)
  await page.eval(`[...document.querySelectorAll("button")].filter((b) => b.textContent.trim() === "审阅栏").forEach((b) => b.remove())`);
  if (s.scroll) await page.eval(`(() => { const el = document.querySelector(".scroller"); if (el) el.scrollTop = ${s.scroll === "top" ? 0 : "el.scrollHeight"}; })()`);
  await page.wait(300);
  if (s.after) await s.after(page);
  await page.wait(300);
  await page.shot(resolve(out, `${name}.png`));
  const errors = page.consoleErrors();
  console.log(name, errors.length ? `ERRORS ${errors.length}` : "ok");
  await page.close();
}
