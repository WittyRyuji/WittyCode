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
  const particle = $(".agent-flow-particle") ? style(".agent-flow-particle").stroke : null;
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

const setMotion = (page, value) => page.send("Emulation.setEmulatedMedia", { features: [{ name: "prefers-reduced-motion", value }] });
const flow = "#conversation-team .agent-flow";
const flowIsMoving = `(() => {
  const graph = document.querySelector('${flow}');
  const working = [...graph.querySelectorAll('.agent-flow-edge.working')];
  const waiting = graph.querySelector('.agent-flow-edge.waiting .agent-flow-wire');
  const running = (node) => node?.getAnimations().some((a) => a.playState === 'running');
  return matchMedia('(prefers-reduced-motion: no-preference)').matches && working.length > 0 && running(waiting) &&
    working.every((edge) => running(edge.querySelector('.agent-flow-signal')) && running(edge.querySelector('.agent-flow-particle')));
})()`;

// Inspect the composited browser screenshot, not just the SVG elements' declared positions.
async function flowPixels(png, { x, y, width, height }) {
  const graph = document.querySelector('#conversation-team .agent-flow');
  const image = new Image();
  image.src = `data:image/png;base64,${png}`;
  await image.decode();
  const canvas = document.createElement('canvas');
  Object.assign(canvas, { width, height });
  const ctx = canvas.getContext('2d', { willReadFrequently: true });
  ctx.drawImage(image, 0, 0);
  const pixels = ctx.getImageData(0, 0, width, height).data;
  ctx.clearRect(0, 0, width, height);
  // Mask wires with room for their round caps and antialiasing. Portraits can legitimately contain green pixels.
  ctx.lineCap = ctx.lineJoin = 'round';
  ctx.strokeStyle = 'lime';
  for (const wire of graph.querySelectorAll('.agent-flow-edge.working .agent-flow-wire')) {
    const m = wire.getScreenCTM();
    ctx.setTransform(m.a, m.b, m.c, m.d, m.e - x, m.f - y);
    ctx.lineWidth = 6 + 4 / m.a;
    ctx.stroke(new Path2D(wire.getAttribute('d')));
  }
  ctx.resetTransform();
  ctx.fillStyle = 'red';
  for (const node of graph.querySelectorAll('.agent-flow-character')) {
    const r = node.getBoundingClientRect();
    ctx.fillRect(r.left - x - 2, r.top - y - 2, r.width + 4, r.height + 4);
  }
  const mask = ctx.getImageData(0, 0, width, height).data;
  const colour = (selector) => getComputedStyle(graph.querySelector(selector)).stroke.match(/\d+/g).slice(0, 3).map(Number);
  const dot = colour('.agent-flow-particle'), signal = colour('.agent-flow-signal');
  const near = (rgb, i) => rgb.every((channel, c) => Math.abs(channel - pixels[i + c]) <= 16);
  const dotPixels = [];
  let offWire = 0;
  for (let i = 0; i < pixels.length; i += 4) {
    if (mask[i] > 0) continue;
    if (mask[i + 1] > 0 && near(dot, i)) dotPixels.push(i / 4);
    if (mask[i + 3] === 0 && (near(dot, i) || near(signal, i))) offWire++;
  }
  return { dotPixels, offWire };
}

async function checkDraft(page, label, ok) {
  const js = page.eval;
  await page.click("textarea.input");
  const before = await js(`(() => {
    const graph = document.querySelector('${flow}');
    const dot = graph.querySelector('.agent-flow-particle');
    window.draftFlowAnimation = dot.getAnimations()[0];
    return { time: draftFlowAnimation.currentTime, sends: __fake.callsOf('session.send').length };
  })()`);
  const frames = [];
  for (const text of ["Unsent ", "sample ", "draft ", "edit"]) {
    await page.type(text);
    const clip = await js(`(() => {
      const { left, top, right, bottom } = document.querySelector('${flow}').getBoundingClientRect();
      const x = Math.ceil(left), y = Math.ceil(top);
      return { x, y, width: Math.floor(right) - x, height: Math.floor(bottom) - y };
    })()`);
    const { data } = await page.send("Page.captureScreenshot", { format: "png", clip: { ...clip, scale: 1 } });
    frames.push(await js(`(${flowPixels.toString()})(${JSON.stringify(data)}, ${JSON.stringify(clip)})`));
  }
  await page.key("Backspace", { vk: 8 });
  const after = await js(`(() => {
    const graph = document.querySelector('${flow}');
    const animation = graph.querySelector('.agent-flow-particle').getAnimations()[0];
    const particles = [...graph.querySelectorAll('.agent-flow-particle')];
    return { value: document.querySelector('textarea.input').value, time: animation?.currentTime, sameAnimation: animation === draftFlowAnimation,
      sends: __fake.callsOf('session.send').length, attribution: !!graph.querySelector('.react-flow__attribution') || graph.textContent.includes('React Flow'),
      attached: particles.length === graph.querySelectorAll('.agent-flow-edge.working').length && !graph.querySelector('animateMotion') &&
        particles.every((p) => p.matches('.working > path') && p.getAttribute('d') === p.parentNode.querySelector('.agent-flow-wire').getAttribute('d')) };
  })()`);
  ok(label("real draft edits do not send, interrupt the working animation, or reveal React Flow attribution"),
    after.value === "Unsent sample draft edi" && after.sends === before.sends && after.sameAnimation && after.time > before.time && !after.attribution, { before, after });
  ok(label("current dots move along working wires without spilling into the empty canvas while typing"),
    after.attached && frames.every((f) => f.offWire === 0) &&
    new Set(frames.filter((f) => f.dotPixels.length > 0).map((f) => f.dotPixels.join(","))).size > 1, frames);
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

  const portraits = await js(`(() => {
    const graph = document.querySelector('#conversation-team .agent-flow');
    const zoom = new DOMMatrixReadOnly(getComputedStyle(graph.querySelector('.react-flow__viewport')).transform).a;
    const near = (a, b) => Math.abs(a - b) < 0.01;
    return [...graph.querySelectorAll('.react-flow__node')].map((node) => {
      const id = node.dataset.id;
      const rings = node.querySelectorAll('.agent-flow-ring');
      const ring = rings[0];
      const sprite = ring?.querySelector('.dragon-sprite');
      if (rings.length !== 1 || !sprite) return { id, rings: rings.length, hasSprite: !!sprite };
      const style = getComputedStyle(ring), image = getComputedStyle(sprite);
      const box = ring.getBoundingClientRect(), frame = sprite.getBoundingClientRect();
      const boxSize = [box.width / zoom, box.height / zoom], frameSize = [frame.width / zoom, frame.height / zoom];
      const radii = ['TopLeft', 'TopRight', 'BottomRight', 'BottomLeft'].map((corner) => style['border' + corner + 'Radius']);
      const border = ['Top', 'Right', 'Bottom', 'Left'].map((side) => parseFloat(style['border' + side + 'Width']));
      const clearance = [frame.top - box.top, box.right - frame.right, box.bottom - frame.bottom, frame.left - box.left]
        .map((gap, i) => gap / zoom - border[i]);
      const unclipped = style.overflowX === 'visible' && style.overflowY === 'visible' && style.clipPath === 'none' && image.clipPath === 'none';
      // Animation shifts the strip, so the same full 48×48 frame fits at every step.
      const sheetWidth = 48 * Number(image.getPropertyValue('--dragon-frames'));
      const fullFrame = image.backgroundImage !== 'none' && image.backgroundSize === sheetWidth + 'px 48px' && frameSize.every((size) => near(size, 48));
      const fits = near(boxSize[0], boxSize[1]) && radii.every((r) => r === '0px') && border.every((w) => w > 0) && unclipped && fullFrame &&
        clearance.every((gap) => gap >= -0.01) && near(clearance[0], clearance[2]) && near(clearance[1], clearance[3]);
      return { id, fits, boxSize, frameSize, radii, border, clearance, unclipped, backgroundSize: image.backgroundSize };
    });
  })()`);
  ok(label("every main and expert portrait has a sharp square border around its full centered 48×48 sprite"),
    portraits.length === agents.length + 1 && portraits.every((portrait) => portrait.fits), portraits);

  await page.waitFor(flowIsMoving);
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
  ok(label("working edges move, waiting edges pulse, and done edges stay still"), await js(`(${flowIsMoving}) && (() => {
    const done = document.querySelector('${flow} .agent-flow-edge.done');
    return done.getAnimations({ subtree: true }).length === 0 && !done.querySelector('.agent-flow-particle');
  })()`));
  await checkDraft(page, label, ok);
  await setMotion(page, "reduce");
  await page.waitFor("matchMedia('(prefers-reduced-motion: reduce)').matches && document.querySelector('#conversation-team').getAnimations({ subtree: true }).length === 0");
  ok(label("reduced motion hides the dots, retains static working wires and freezes waiting wires and sprites"), await js(`(() => {
    const rail = document.querySelector('#conversation-team');
    const dots = [...rail.querySelectorAll('.agent-flow-particle')], signals = [...rail.querySelectorAll('.agent-flow-signal')];
    return dots.length > 0 && dots.every((dot) => getComputedStyle(dot).display === 'none') &&
      signals.length === dots.length && signals.every((signal) => getComputedStyle(signal).display !== 'none') &&
      rail.querySelectorAll('.dragon-sprite.playing').length > 0 &&
      [...rail.querySelectorAll('.dragon-sprite.playing')].every((sprite) => getComputedStyle(sprite).backgroundPosition === '0px 0px');
  })()`));
  await setMotion(page, "no-preference");
  await page.waitFor(flowIsMoving);

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
      // Headless runners can inherit the host's reduced-motion setting; start every viewport in the same mode.
      await setMotion(page, "no-preference");
      await js("__fake.releaseInit()");
      await page.waitFor("!!document.querySelector('.srow')");
      await page.click(".srow");
      await page.waitFor("!!document.querySelector('.tab.current .dot-ready')");
      const id = await js("__fake.events.findLast(([name, payload]) => name === 'tabs' && payload.select)[1].select");
      const patch = (changes) => js(`__fake.patch(${JSON.stringify(id)}, ${JSON.stringify(changes)})`);
      await js(`__fake.emit('settings', { settings: { maxOpen: 4, theme: '${theme}', fontScale: 'md', sidebarWidth: 300 } })`);
      await patch({ mark: "running", agents, agentRuns: runs });
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
      const firstEdge = `${flow} .react-flow__edge[data-id="expert:${agents[0].id}"] .agent-flow-edge`;
      await patch({ agents: [{ ...agents[0], status: "waiting" }, ...agents.slice(1)] });
      await page.waitFor(`!!document.querySelector('${firstEdge}.waiting')`);
      ok(label("a working expert that starts waiting loses the dots and pulses amber"), await js(`(() => {
        const edge = document.querySelector('${firstEdge}');
        return !edge.querySelector('.agent-flow-particle, .agent-flow-signal') && edge.querySelector('.agent-flow-wire').getAnimations().some((a) => a.playState === 'running') &&
          !!document.querySelector('${flow} button[aria-label="查看专家 检查专家 1 · 等你确认"]');
      })()`));
      await patch({ mark: "ready", agents: agents.map((agent) => ["working", "waiting"].includes(agent.status) ? { ...agent, status: "done", finishedAt: Date.now() } : agent) });
      await page.waitFor(`!!document.querySelector('${firstEdge}.done') && !document.querySelector('${flow} .agent-flow-edge.working, ${flow} .agent-flow-edge.waiting')`);
      await page.click("textarea.input");
      await page.key("End", { vk: 35 });
      await page.type(" - idle edit");
      const idle = await js(`(() => {
        const graph = document.querySelector('${flow}');
        return { value: document.querySelector('textarea.input').value, sends: __fake.callsOf('session.send').length,
          current: !!graph.querySelector('.agent-flow-signal, .agent-flow-particle'),
          animatedEdges: [...graph.querySelectorAll('.agent-flow-edge')].filter((edge) => edge.getAnimations({ subtree: true }).length > 0).length };
      })()`);
      ok(label("typing in an idle team leaves its wires static and the draft unsent"),
        idle.value === "Unsent sample draft edi - idle edit" && idle.sends === 0 && !idle.current && idle.animatedEdges === 0, idle);
      ok(label("no console errors or CSP violations"), page.errorTexts().length === 0, page.errorTexts());
    } finally {
      await page.close();
    }
  }
}
