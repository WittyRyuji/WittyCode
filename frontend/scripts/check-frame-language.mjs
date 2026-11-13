// Codex-like layout must not replace the app's square, monochrome frame language.
import assert from "node:assert/strict";
import { resolve } from "node:path";
import { pathToFileURL } from "node:url";
import { openPage } from "./cdp.mjs";

const index = pathToFileURL(resolve("dist/index.html")).href;
for (const theme of ["light", "dark"]) for (const width of [1500, 900]) {
  const page = await openPage(`${index}?scene=ask&theme=${theme}&review=0`, { width, height: 960, hideScrollbars: false });
  try {
    await page.waitFor("!!document.querySelector('.prompt.ask') && !!document.querySelector('.srow.current')");
    await page.waitFor("getComputedStyle(document.querySelector('.srow.current')).backgroundColor === getComputedStyle(document.querySelector('.prompt.ask .phead')).backgroundColor");
    const frames = await page.eval(`(() => {
      const selectors = ['.newbtn', '.srow.current', '.composer', '.composer .chip', '.send', '.block.user', '.block.activity', '.prompt.ask', '.prompt.ask .otherinput', '.prompt.ask .btn'];
      return selectors.map(selector => {
        const e = document.querySelector(selector), s = getComputedStyle(e);
        return {selector, corners: [s.borderTopLeftRadius, s.borderTopRightRadius, s.borderBottomLeftRadius, s.borderBottomRightRadius]};
      });
    })()`);
    assert(frames.every(f => f.corners.every(c => parseFloat(c) === 0)), `frames keep square corners: ${JSON.stringify(frames)}`);
    assert(await page.eval(`(() => {
      const c = getComputedStyle(document.querySelector('.composer')), u = getComputedStyle(document.querySelector('.block.user'));
      const q = getComputedStyle(document.querySelector('.prompt.ask')), h = getComputedStyle(document.querySelector('.prompt.ask .phead'));
      const selected = getComputedStyle(document.querySelector('.srow.current'));
      return c.borderTopWidth === '2px' && u.borderLeftWidth === '4px' && q.borderTopWidth === '2px'
        && q.boxShadow.includes('4px 4px 0px') && h.backgroundColor === selected.backgroundColor;
    })()`), "input, user message and question keep their original borders and inverted heading");
    const aligned = `(() => {
      return [...document.querySelectorAll('.proj:not(.archived)')].every(project => {
        const h = project.querySelector('.phead-row').getBoundingClientRect();
        return [...project.querySelectorAll('.srow')].every(row => {
          const r = row.getBoundingClientRect();
          return Math.abs(r.left - h.left) < 1 && Math.abs(r.right - h.right) < 1;
        });
      });
    })()`;
    assert(await page.eval(aligned), "project headers, hovered rows and selected rows share both outer edges");
    assert(await page.eval(`(() => {
      const left = document.querySelector('.newbtn').getBoundingClientRect().left;
      return ['.phead-row', '.srow.current', '.foot-btn'].every(s => Math.abs(document.querySelector(s).getBoundingClientRect().left - left) < 1);
    })()`), "sidebar entry points share one left gutter");
    const before = await page.rect('.srow.current');
    await page.hover('.srow:not(.current)');
    await page.wait(150);
    assert.deepEqual(await page.rect('.srow.current'), before, "hover does not move or resize the current row");
    assert(await page.eval(`(async () => {
      const row = document.querySelector('.srow:not(.current)'), before = getComputedStyle(row).backgroundColor;
      row.classList.add('current');
      await new Promise(resolve => requestAnimationFrame(resolve));
      const selected = getComputedStyle(row).backgroundColor === getComputedStyle(document.querySelector('.prompt.ask .phead')).backgroundColor;
      row.classList.remove('current');
      await new Promise(resolve => requestAnimationFrame(resolve));
      return selected && getComputedStyle(row).backgroundColor === before;
    })()`), "selection inversion is immediate: its text never appears on the old, same-colour background");
    await page.click('.input');
    await page.waitFor("getComputedStyle(document.querySelector('.composer')).boxShadow.includes('4px 4px 0px')");
    await page.eval("document.querySelector('.prompt.ask').scrollIntoView({block: 'start'})");
    await page.shot(resolve(`shots/frame-language-${theme}-${width}.png`));
    await page.eval(`(() => {
      const list = document.querySelector('.slist'), row = list.querySelector('.srow');
      for (let i = 0; i < 30; i++) list.append(row.cloneNode(true));
      row.querySelector('.stitle').textContent = 'A long conversation title '.repeat(40);
    })()`);
    assert(await page.eval(aligned), "scrollbars and long lists do not reintroduce a second row inset");
    assert(await page.eval("(() => { const s = document.querySelector('.side-scroll'), t = document.querySelector('.stitle'); return s.scrollHeight > s.clientHeight && s.scrollWidth <= s.clientWidth && t.scrollWidth > t.clientWidth; })()"), "long titles truncate without horizontal sidebar overflow");
    assert(await page.eval("['.phead-row', '.srow .more'].every(s => parseFloat(getComputedStyle(document.querySelector(s)).transitionDuration) > 0)"), "hover feedback remains subtle, without animating row geometry or inverted selection colours");
    await page.send("Emulation.setEmulatedMedia", { features: [{ name: "prefers-reduced-motion", value: "reduce" }] });
    assert(await page.eval("['.phead-row', '.srow .more'].every(s => getComputedStyle(document.querySelector(s)).transitionDuration.split(',').every(v => parseFloat(v) < 0.001))"), "subtle sidebar feedback respects reduced motion");
    assert.deepEqual(page.errorTexts(), []);
    console.log(`PASS square frame language and sidebar alignment: ${theme}, ${width}px, overflow, focus and reduced motion`);
  } finally {
    await page.close();
  }
}
