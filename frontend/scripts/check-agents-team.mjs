import { openPage } from "./cdp.mjs";

const firstLine = "请核对示例清单中的编号、名称与状态，并将观察到的差异记录在报告中。".repeat(8);
const brief = [firstLine, ...Array.from({ length: 12 }, (_, i) =>
  `样例 ${i + 1}：` + "逐项检查示例数据，记下预期结果与实际结果。核对完成后，整理一份便于复查的简要记录。".repeat(14))].join("\n\n");
const now = Date.now();
const agents = Array.from({ length: 12 }, (_, i) => ({
  id: `expert-${i}`, name: `检查专家 ${i + 1}`, role: "工作专家", model: "GPT-6.1-Sol", effort: "high",
  task: i === 0 ? brief : "核对一个样例。", status: ({ 2: "waiting", 3: "failed", 4: "stopped", 5: "pending", 11: "done" })[i] ?? "working",
  startedAt: now - i, finishedAt: [3, 4, 11].includes(i) ? now - i + 100 : null,
  tokens: i === 0 ? { input: 8500, output: 1500, cacheWrite: 2000, cacheRead: 12000 }
    : i === 1 ? null : { input: 800, output: 200, cacheWrite: null, cacheRead: 0 },
}));
const run = (id, task, messages) => [{ id, status: "done", prompts: [], blocks: [
  { kind: "user", id: `${id}-task`, text: task },
  ...messages.map((markdown, i) => ({ kind: "text", id: `${id}-${i}`, markdown, streaming: false })),
] }];
const runs = {
  "expert-0": run("first", brief, Array.from({ length: 14 }, (_, i) => `第 ${i + 1} 项：已核对样例的名称和状态，结果已记录。`)),
  "expert-1": run("second", agents[1].task, ["第二位专家的核对记录。"]),
};

// Read actual painted SVG/CSS colours; for the dark gradient use its lightest point.
export function agentFlowColours() {
  const $ = (selector) => document.querySelector(selector);
  const style = (selector) => getComputedStyle($(selector));
  const rgb = (colour) => colour.startsWith("#") ? [1, 3, 5].map((i) => parseInt(colour.slice(i, i + 2), 16)) : colour.match(/[\d.]+/g).slice(0, 3).map(Number);
  const light = (channels) => channels.map((n) => n / 255).map((n) => n < 0.04045 ? n / 12.92 : ((n + 0.055) / 1.055) ** 2.4)
    .reduce((sum, n, i) => sum + n * [0.2126, 0.7152, 0.0722][i], 0);
  const chart = style(".agent-flow");
  const background = chart.backgroundColor;
  const contrastBackground = chart.getPropertyValue("--flow-glow").trim();
  const back = rgb(contrastBackground);
  const contrast = (colour, opacity = 1) => {
    const front = rgb(colour).map((n, i) => n * opacity + back[i] * (1 - opacity));
    const a = light(front), b = light(back);
    return Number(((Math.max(a, b) + 0.05) / (Math.min(a, b) + 0.05)).toFixed(3));
  };
  const label = style(".agent-flow-name").color;
  const model = style(".agent-flow-model").color;
  const grid = style(".react-flow__background-pattern.dots").fill;
  const particle = $(".agent-flow-particle") ? style(".agent-flow-particle").fill : null;
  const edges = {};
  for (const state of ["pending", "working", "waiting", "done", "stopped", "failed"]) {
    const edge = $(`.agent-flow-edge.${state}`);
    if (!edge) continue;
    const wire = getComputedStyle(edge.querySelector(state === "working" ? ".agent-flow-signal" : ".agent-flow-wire"));
    const animation = state === "waiting" && edge.querySelector(".agent-flow-wire").getAnimations()[0];
    const opacity = Number(animation?.effect?.getKeyframes()[0]?.opacity ?? wire.opacity);
    const ring = style(`.agent-flow-node.${state} .agent-flow-ring`).borderTopColor;
    edges[state] = { stroke: wire.stroke, opacity, contrast: contrast(wire.stroke, opacity), ring, ringContrast: contrast(ring) };
  }
  return { background, contrastBackground, luminance: light(back), grid, gridContrast: contrast(grid), label, model,
    labelContrast: contrast(label), modelContrast: contrast(model), particle, particleContrast: particle && contrast(particle), edges,
    transparentCanvas: [".react-flow", ".react-flow__background"].every((selector) => style(selector).backgroundColor === "rgba(0, 0, 0, 0)") };
}

async function checkFlow(page, theme, label, ok) {
  const js = page.eval;
  await page.waitFor(`document.querySelectorAll('#conversation-team .agent-flow-edge').length === ${agents.length} && document.documentElement.dataset.theme === '${theme}'`);
  const layout = await js(`(() => {
    const graph = document.querySelector('#conversation-team .agent-flow');
    const rail = graph.closest('.conversation-rail-content');
    const {top, bottom, width, height} = graph.getBoundingClientRect();
    const inset = getComputedStyle(rail);
    return { underTitle: graph.previousElementSibling.matches('.agentbar-head') && top >= graph.previousElementSibling.getBoundingClientRect().bottom,
      aboveList: graph.nextElementSibling.matches('.agent-list') && bottom <= graph.nextElementSibling.getBoundingClientRect().top,
      width, available: rail.clientWidth - parseFloat(inset.paddingLeft) - parseFloat(inset.paddingRight), height,
      nodes: graph.querySelectorAll('.react-flow__node').length, edges: graph.querySelectorAll('.agent-flow-edge').length };
  })()`);
  ok(label("the flow sits beneath the title, fills the rail width, uses 25% of the window and includes the whole team"), layout.underTitle && layout.aboveList &&
    Math.abs(layout.width - layout.available) <= 1 && Math.abs(layout.height - await js("innerHeight / 4")) <= 1 &&
    layout.nodes === agents.length + 1 && layout.edges === agents.length, layout);

  const colours = await js(`(${agentFlowColours.toString()})()`);
  ok(label("theme surfaces and labels match; state edges and rings have at least 3:1 contrast"), colours.transparentCanvas &&
    (theme === "light" ? colours.luminance > 0.85 : colours.luminance < 0.08) && colours.labelContrast >= 4.5 && colours.modelContrast >= 4.5 &&
    colours.gridContrast > 1.5 && colours.particleContrast >= 3 && Object.keys(colours.edges).length === 6 &&
    Object.values(colours.edges).every((edge) => edge.contrast >= 3 && edge.ringContrast >= 3), colours);
  const wires = await js("[...document.querySelectorAll('#conversation-team .agent-flow-wire')].map((wire) => wire.getAttribute('d'))");
  ok(label("edges are right-angled polylines, not curves"), wires.length === agents.length && wires.every((d) => {
    // React Flow writes a corner of radius 0 as a quadratic whose control point is its end point: still a sharp corner.
    const commands = [...d.matchAll(/([A-Za-z])([^A-Za-z]*)/g)].map((m) => [m[1], (m[2].match(/-?\d+(?:\.\d+)?/g) ?? []).map(Number)]);
    const sharp = commands.every(([name, n]) => name === "M" || name === "L" ? n.length === 2
      : name === "Q" && n.length === 4 && Math.abs(n[0] - n[2]) < 0.01 && Math.abs(n[1] - n[3]) < 0.01);
    const points = commands.map(([, n]) => n.slice(-2));
    return sharp && points.length >= 4 && points.every(([x, y], i) => i === 0 || Math.abs(x - points[i - 1][0]) < 0.01 || Math.abs(y - points[i - 1][1]) < 0.01);
  }), wires.slice(0, 3));
  ok(label("working edges move, waiting edges pulse, and done edges stay still"), await js(`(() => {
    const $ = (s) => document.querySelector(s);
    return $('.agent-flow-edge.working .agent-flow-signal').getAnimations().some((a) => a.playState === 'running') &&
      $('.agent-flow-edge.working').querySelectorAll('animateMotion').length === 2 &&
      $('.agent-flow-edge.waiting .agent-flow-wire').getAnimations().some((a) => a.playState === 'running') &&
      $('.agent-flow-edge.done .agent-flow-wire').getAnimations().length === 0 && !$('.agent-flow-edge.done animateMotion');
  })()`));
  await page.send("Emulation.setEmulatedMedia", { features: [{ name: "prefers-reduced-motion", value: "reduce" }] });
  await page.waitFor("matchMedia('(prefers-reduced-motion: reduce)').matches && !document.querySelector('.agent-flow animateMotion')");
  ok(label("reduced motion removes moving particles and freezes wires and sprites"), await js(`(() => {
    const rail = document.querySelector('#conversation-team');
    return rail.querySelectorAll('.dragon-sprite.playing').length > 0 && rail.getAnimations({ subtree: true }).length === 0 &&
      [...rail.querySelectorAll('.dragon-sprite.playing')].every((sprite) => getComputedStyle(sprite).backgroundPosition === '0px 0px');
  })()`));
  await page.send("Emulation.setEmulatedMedia", { features: [{ name: "prefers-reduced-motion", value: "no-preference" }] });
  await page.waitFor("!!document.querySelector('.agent-flow animateMotion')");

  const viewport = () => js(`(() => {
    const rail = document.querySelector('.conversation-rail-content');
    const {x, y, width} = rail.querySelector('.agent-flow').getBoundingClientRect();
    const matrix = new DOMMatrixReadOnly(getComputedStyle(rail.querySelector('.react-flow__viewport')).transform);
    return { x: matrix.e, y: matrix.f, zoom: matrix.a, scroll: rail.scrollTop, maxScroll: rail.scrollHeight - rail.clientHeight, cursorX: x + width * 0.48, cursorY: y + 38 };
  })()`);
  await js("document.querySelector('.conversation-rail-content').scrollTop = 16");
  const before = await viewport();
  const { cursorX: x, cursorY: y } = before;
  await page.send("Input.dispatchMouseEvent", { type: "mouseWheel", x, y, deltaX: 0, deltaY: -150 });
  await page.wait(250);
  const wheel = await viewport();
  ok(label("a real wheel zooms the flow without scrolling the scrollable rail"), wheel.zoom > before.zoom && before.scroll > 0 &&
    before.scroll < before.maxScroll && wheel.scroll === before.scroll, { before, wheel });
  await page.mouse("mousePressed", x, y, { button: "middle", buttons: 4 });
  await page.mouse("mouseMoved", x + 17, y + 12, { button: "middle", buttons: 4 });
  await page.mouse("mouseReleased", x + 17, y + 12, { button: "middle", buttons: 0 });
  await page.wait(150);
  const drag = await viewport();
  ok(label("a real middle-button drag pans the canvas without changing zoom"), Math.abs(drag.x - wheel.x - 17) < 1 &&
    Math.abs(drag.y - wheel.y - 12) < 1 && Math.abs(drag.zoom - wheel.zoom) < 0.0001, { wheel, drag });
}

const chipsHaveSprites = (selector) => `(() => {
  const chips = [...document.querySelectorAll('${selector} .agent-chip')];
  return chips.length === ${agents.length} && chips.every((chip) => {
    const sprite = chip.querySelector('.dragon-sprite[role=img]');
    const rect = sprite?.getBoundingClientRect();
    return rect?.width === 32 && rect.height === 32 && getComputedStyle(sprite).imageRendering === 'pixelated' &&
      sprite.getAttribute('aria-label') === chip.querySelector('.agent-state').textContent;
  });
})()`;

export async function checkAgentsTeam({ url, scripts, ok }) {
  for (const [width, height, theme] of [[1440, 900, "light"], [1100, 600, "dark"]]) {
    const page = await openPage(url, { width, height, init: scripts, hideScrollbars: false });
    const label = (text) => `Agents Team ${width}×${height}: ${text}`;
    const js = page.eval;
    const selected = ".agentpanel .agent-chip.on";
    const toggle = ".agentpanel-task-toggle";
    const task = "document.querySelector('.agentpanel-task-text')";
    const paneSizes = () => js(`(() => {
      const panel = document.querySelector('.agentpanel');
      const team = panel.querySelector('.agentbar');
      const head = team.querySelector('.agentbar-head');
      const chip = team.querySelector('.agent-chip');
      const run = panel.querySelector('.agentpanel-run');
      return { panel: panel.clientHeight, overflow: panel.scrollHeight - panel.clientHeight,
        team: team.clientHeight, teamScroll: team.scrollHeight, min: head.offsetHeight + chip.offsetHeight,
        run: run.clientHeight, runScroll: run.scrollHeight, overlay: getComputedStyle(panel).position === 'absolute' };
    })()`);
    try {
      await js("__fake.releaseInit()");
      await page.waitFor("!!document.querySelector('.srow')");
      await page.click(".srow");
      await page.waitFor("!!document.querySelector('.tab.current .dot-ready')");
      const id = await js("__fake.events.findLast(([name, payload]) => name === 'tabs' && payload.select)[1].select");
      const patch = (changes) => js(`__fake.patch(${JSON.stringify(id)}, ${JSON.stringify(changes)})`);
      await js(`__fake.emit('settings', { settings: { maxOpen: 4, theme: '${theme}', fontScale: 'md', sidebarWidth: 300 } })`);
      await patch({ agents, agentRuns: runs });
      await checkFlow(page, theme, label, ok);
      await page.click("#conversation-team .agent-history-toggle");
      ok(label("every rail chip has a labelled 32×32 pixel dragon"), await js(chipsHaveSprites("#conversation-team")));
      await page.click('.agent-flow button[aria-label="查看专家 检查专家 1 · 运行中"]');
      await page.waitFor("document.querySelector('.agentpanel-run')?.textContent.includes('第 14 项')");
      ok(label("clicking an expert node opens its run in the inspector, with no second chart"), await js(`
        document.querySelector('${selected} .agent-name')?.textContent === '检查专家 1' && !document.querySelector('.agentpanel .agent-flow')`));
      const dims = await paneSizes();
      ok(label("the inspector fits, with a team cap and usable scrollable run"), dims.overflow <= 1 &&
        dims.team >= dims.min && dims.team <= Math.max(128, dims.panel * 0.32) + 1 && dims.teamScroll > dims.team &&
        dims.run >= (height === 900 ? 160 : 1) && dims.runScroll > dims.run && dims.overlay === (width <= 1100), dims);
      ok(label("scrolling the team moves its chips but not its header"), await js(`(() => {
        const team = document.querySelector('.agentpanel .agentbar');
        const head = team.querySelector('.agentbar-head'), chip = team.querySelector('.agent-chip');
        team.scrollTop = 0;
        const y = head.getBoundingClientRect().top, chipY = chip.getBoundingClientRect().top;
        team.scrollTop = 90;
        const scrolled = team.scrollTop > 0 && Math.abs(head.getBoundingClientRect().top - y) <= 1 && chip.getBoundingClientRect().top < chipY;
        team.scrollTop = 0;
        return scrolled;
      })()`));
      await page.waitFor("(() => { const r = document.querySelector('.agentpanel-run'); return r.scrollTop > 0 && r.scrollHeight - r.clientHeight - r.scrollTop <= 1; })()");
      ok(label("the run opens at the bottom and scrolls independently"), await js(`(() => {
        const r = document.querySelector('.agentpanel-run'), team = document.querySelector('.agentpanel .agentbar');
        const bottom = r.scrollTop, teamTop = team.scrollTop;
        r.scrollTop = 0;
        const scrolled = r.scrollTop < bottom && team.scrollTop === teamTop;
        r.scrollTop = bottom;
        return scrolled;
      })()`));
      ok(label("a 6,000+ character brief defaults to three lines and a short tooltip"), await js(`(() => {
        const t = ${task}, button = document.querySelector('${toggle}'), chip = document.querySelector('${selected}');
        return t.textContent.length >= 6000 && t.clientHeight <= 3 * parseFloat(getComputedStyle(t).lineHeight) + 1 &&
          t.scrollHeight > t.clientHeight && button?.textContent === '展开' && chip.title === ${JSON.stringify(firstLine.slice(0, 200))};
      })()`));
      ok(label("known totals include the full breakdown and history; unknown chips are blank"), await js(`(() => {
        const panel = document.querySelector('.agentpanel');
        const pieces = [...panel.querySelectorAll('.agentpanel-tokens span')].map(e => e.textContent);
        const chips = panel.querySelectorAll('.agent-chip');
        return panel.querySelector('.agentbar-count').textContent === '12' && panel.querySelector('.agentbar-tokens').textContent === '34K Token' &&
          chips[0].querySelector('.agent-chip-tokens')?.textContent === '24K Token' && !chips[1].querySelector('.agent-chip-tokens') &&
          pieces.join('|') === ['合计 24K Token', '输入 ' + (8500).toLocaleString(), '输出 ' + (1500).toLocaleString(),
            '缓存写入 ' + (2000).toLocaleString(), '缓存读取 ' + (12000).toLocaleString()].join('|');
      })()`));
      await page.click(toggle);
      const expanded = await paneSizes();
      ok(label("the expanded brief keeps its paragraphs and has a separate capped scrollbar"), await js(`(() => {
        const t = ${task};
        t.scrollTop = 80;
        const scrolls = t.scrollTop > 0;
        t.scrollTop = 0;
        return document.querySelector('${toggle}')?.textContent === '收起' && scrolls && t.clientHeight <= Math.min(innerHeight * .16, 144) + 1 &&
          getComputedStyle(t).whiteSpace === 'pre-wrap' && t.textContent.includes('\\n\\n');
      })()`) && expanded.overflow <= 1 && expanded.team === dims.team && expanded.run >= (height === 900 ? 160 : 1) && expanded.runScroll > expanded.run, expanded);
      await page.click(toggle);
      ok(label("the brief collapses and returns the run's space"), await js(`document.querySelector('${toggle}').getAttribute('aria-expanded') === 'false'`) && (await paneSizes()).run === dims.run);
      await page.click(toggle);
      await page.click(".agentpanel .agent-chip", 1);
      await page.waitFor("document.querySelector('.agentpanel-run')?.textContent.includes('第二位专家的核对记录')");
      ok(label("switching chips opens the other run; no toggle for a short brief or tokens for unknown usage"), await js(`
        document.querySelector('${selected} .agent-name')?.textContent === '检查专家 2' &&
        !document.querySelector('${toggle}, .agentpanel-tokens') && !document.querySelector('.agentpanel-run').textContent.includes('第 14 项')`));
      await page.click(".agentpanel .agent-chip");
      await page.waitFor("document.querySelector('.agentpanel-run')?.textContent.includes('第 14 项')");
      ok(label("returning to the original agent starts collapsed"), await js(`document.querySelector('${toggle}')?.getAttribute('aria-expanded') === 'false'`));
      await patch({ agents: [{ ...agents[0], tokens: { ...agents[0].tokens, cacheRead: 13000 } }, ...agents.slice(1)] });
      await page.waitFor("document.querySelector('.agentpanel-tokens')?.textContent.includes('合计 25K Token')");
      ok(label("a live usage patch updates the chip, header and open agent"), await js(`
        document.querySelector('${selected} .agent-chip-tokens')?.textContent === '25K Token' &&
        document.querySelector('.agentbar-tokens')?.textContent === '35K Token'`));
      await page.click(".agentpanel .agent-history-toggle");
      ok(label("every inspector chip has a labelled 32×32 pixel dragon"), await js(chipsHaveSprites(".agentpanel")));
      await page.key("Escape");
      ok(label("Escape closes the inspector without changing the conversation rail"), await js("!document.querySelector('.agentpanel') && !!document.querySelector('#conversation-team .agent-chip') && !document.querySelector('#conversation-team .agent-chip-tokens')"));
      ok(label("no console errors or CSP violations"), page.errorTexts().length === 0, page.errorTexts());
    } finally {
      await page.close();
    }
  }
}
