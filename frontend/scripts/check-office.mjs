import { resolve } from "node:path";
import { LITERAL } from "./office-fixtures.mjs";

export async function checkOffice({ page, ok, clickText, calls, lastCall, waitCalls }) {
  const js = page.eval;
  const word = "document.querySelector('.fp-word-paper')?.shadowRoot";
  const open = (name) => clickText(".text .pathref", name);
  const checkWord = async () => {
    await page.waitFor(`!!${word}?.querySelector('section.fp-docx')`, 4500);
    return js(`(() => { const root = ${word}; return root.querySelector('p')?.textContent === '项目简报' && root.querySelector('p:nth-child(2)')?.textContent === '本周完成了样例数据核对。' && [...root.querySelectorAll('td')].some(x => x.textContent === '已完成'); })()`);
  };
  const checkSheet = async () => {
    await page.waitFor("!!document.querySelector('.fp-grid td')", 4500);
    return js("document.querySelector('.fp-facts').textContent.includes('3 个工作表') && [...document.querySelectorAll('.fp-grid td')].some(x => x.textContent === '75.0%') && document.querySelector('.fp-grid td[colspan=\"2\"]').textContent === '样例数据'");
  };
  const checkSlides = async () => {
    await page.waitFor("document.querySelectorAll('.fp-slide').length === 2", 4500);
    return js("[...document.querySelectorAll('.fp-slide h3')].map(x => x.textContent).join('|') === '本周进度|下一步' && document.querySelector('.fp-facts').textContent.includes('2 张幻灯片') && [...document.querySelectorAll('.fp-slide td')].some(x => x.textContent === '已完成') && document.querySelectorAll('.fp-slide p [aria-hidden]').length === 2");
  };
  await page.send("Network.enable");
  const start = page.events.length;
  const startHref = await js("location.href");
  const csp = page.cspViolations().length;
  const linkCount = (await calls("shell.openExternal")).length;

  await open("sample.docx");
  ok("Word renders the heading, paragraph, and table cell in the document's paper", await checkWord());
  await page.waitFor(`${word}?.querySelector('img')?.naturalWidth > 0`);
  const layout = await js(`(() => { const root = ${word}; const p = root.querySelector('p span'); const paper = root.querySelector('section'); const box = document.querySelector('.fp-word'); return [getComputedStyle(p).fontSize, getComputedStyle(paper).backgroundColor, box.scrollWidth - box.clientWidth]; })()`);
  ok("Word keeps document styles and fits white paper to the panel", layout[0] === "37.3333px" && layout[1] === "rgb(255, 255, 255)" && layout[2] < 2, layout);
  await open("sample.xlsx");
  ok("Excel shows formatted cells, sheet count, and merged cells", await checkSheet());
  await clickText(".fp-tabs button", "明细");
  ok("Excel switches sheets and keeps the active sheet marked", await js("document.querySelector('.fp-tabs [aria-pressed=true]').textContent === '明细' && [...document.querySelectorAll('.fp-grid td')].some(x => x.textContent === '校对')"));
  await clickText(".fp-tabs button", "空表");
  ok("Excel gives an empty-sheet state", await js("document.querySelector('.fp-state')?.textContent.includes('空的') && !document.querySelector('.fp-grid')"));
  await open("sample.pptx");
  ok("PowerPoint puts slide titles in relationship order and renders tables and bullets", await checkSlides());
  await page.waitFor("document.querySelector('.fp-slide img')?.naturalWidth > 0");
  ok("PowerPoint loads the embedded raster image", await js("document.querySelector('.fp-slide img').src.startsWith('data:image/png;base64,')"));

  await open("hostile.docx");
  ok("Hostile Word still previews its normal text", await checkWord());
  const links = await js(`[...${word}.querySelectorAll('a')].map(a => [a.textContent, a.getAttribute('href')])`);
  ok("Word removes javascript links and active/remote content before insertion", links.some(([t, href]) => t === "Blocked link" && href === null) && await js(`!${word}.querySelector('script, iframe, img, [onclick]') && !${word}.textContent.includes('ALT CHUNK MUST NOT RENDER')`), links);
  await js(`Array.from(${word}.querySelectorAll('a')).find(a => a.textContent === 'Blocked link').click()`);
  await js(`Array.from(${word}.querySelectorAll('a')).find(a => a.textContent === 'External link').click()`);
  await waitCalls("shell.openExternal", linkCount + 1);
  ok("Only the approved Word link goes to the same Python openExternal bridge as Markdown", (await calls("shell.openExternal")).length === linkCount + 1 && (await lastCall("shell.openExternal")).url === "https://example.com/office-demo");
  await open("hostile.xlsx");
  await checkSheet();
  const sheet = await js("(() => { const grid = document.querySelector('.fp-grid'); return { cells: [...grid.querySelectorAll('td')].map(el => el.textContent), activeContent: !!grid.querySelector('a,img,script') }; })()");
  ok("Hostile Excel cells stay literal; formulas only show their saved value, and links never render", [LITERAL, "=1+1", "5"].every(text => sheet.cells.includes(text)) && !sheet.activeContent);
  await open("hostile.pptx");
  await checkSlides();
  const slides = await js("(() => { const deck = document.querySelector('.fp-slides'); return { text: deck.textContent, images: deck.querySelectorAll('img').length, activeContent: !!deck.querySelector('a,script') }; })()");
  ok("Hostile PowerPoint shows literal text and ignores both external and URL-like internal image relationships", slides.text.includes(LITERAL) && slides.text.includes("=1+1") && slides.images === 1 && !slides.activeContent);
  const observed = page.events.slice(start);
  const network = observed.filter(e => e.method === "Network.requestWillBeSent" && /^https?:/i.test(e.params.request.url)).map(e => e.params.request.url);
  ok("Office preview documents do not execute, navigate, violate CSP, or request the network", await js("!window.officePreviewMarker") && await js("location.href") === startHref && !observed.some(e => e.method === "Page.frameNavigated") && page.cspViolations().length === csp && network.length === 0, { network, errors: page.errorTexts() });

  const before = (await calls("files.page")).length;
  await js("__fake.delayReply('files.page', 1000)");
  await open("sample.docx");
  await waitCalls("files.page", before + 1);
  await open("sample.xlsx");
  await checkSheet();
  await page.wait(1050);
  ok("A late Office payload cannot replace the newly opened file", await js("!document.querySelector('.fp-word-paper') && document.querySelector('.fp-name').textContent === 'sample.xlsx' && !!document.querySelector('.fp-grid')"));
  await js("__fake.failNext('files.page', '这不是有效的 Office 文件。')");
  await open("sample.docx");
  await page.waitFor("!!document.querySelector('.fp-error')");
  ok("Backend validation errors retain their explanation and both existing actions", await js("document.querySelector('.fp-error').textContent.includes('不是有效的 Office 文件') && document.querySelectorAll('.fp-actions button').length === 2"));

  if (process.env.WITTYCODE_OFFICE_SHOTS === "1") {
    const theme = await js("document.documentElement.dataset.theme");
    try {
      for (const [name, check] of [["docx", checkWord], ["xlsx", checkSheet], ["pptx", checkSlides]]) {
        await open(`sample.${name}`);
        await check();
        for (const [size, width] of [["narrow", 1120], ["wide", 1750]]) {
          await page.send("Emulation.setDeviceMetricsOverride", { width, height: 900, deviceScaleFactor: 1, mobile: false });
          for (const mode of ["light", "dark"]) {
            await js(`document.documentElement.dataset.theme = '${mode}'`);
            await page.wait(150);
            const rect = await page.rect(".filepanel");
            console.log(`Office screenshot ${name}-${mode}-${size} (${Math.round(rect.w)} px panel)`);
            await page.shot(resolve(`../sandbox/office-preview-check/${name}-${mode}-${size}.png`));
          }
        }
      }
    } finally {
      await js(`document.documentElement.dataset.theme = ${JSON.stringify(theme)}`);
      await page.send("Emulation.setDeviceMetricsOverride", { width: 1440, height: 900, deviceScaleFactor: 1, mobile: false });
    }
  }
}
