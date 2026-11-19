import { resolve } from "node:path";
import { pathToFileURL } from "node:url";
import { openPage } from "./cdp.mjs";

const firstLine = "请核对示例清单中的编号、名称与状态，并将观察到的差异记录在报告中。".repeat(8);
const brief = [firstLine, ...Array.from({ length: 12 }, (_, i) =>
  `样例 ${i + 1}：` + "逐项检查示例数据，记下预期结果与实际结果。核对完成后，整理一份便于复查的简要记录。".repeat(14))].join("\n\n");
const now = Date.now();
export const agents = Array.from({ length: 12 }, (_, i) => ({
  id: `expert-${i}`, name: `检查专家 ${i + 1}`, role: "工作专家", fusionRole: "worker",
  fusionIdentity: i === 11 ? "Observer" : [0, 1, 3, 4, 5].includes(i) ? "Coder" : "Runner",
  model: i === 1 ? "GPT-6-Luna" : "GPT-6.1-Sol", effort: "high",
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
const nestButton = (identity) => `${flow} [data-id="group:${identity}"] button`;
const nests = ["Coder", "Runner"];
const overviewButton = `${flow} .agent-flow-overview`;
const closedTree = { "group:Coder": "team-main", "group:Runner": "team-main", "expert:expert-11": "team-main" };
const fullTree = { ...closedTree, ...Object.fromEntries(agents.filter((a) => a.fusionIdentity !== "Observer")
  .map((a) => [`expert:${a.id}`, `group:${a.fusionIdentity}`])) };
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
  for (const node of graph.querySelectorAll('.agent-flow-character, .agent-flow-overview')) {
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
  // In the focused Runner branch, expert-7 and its working wire are actually on screen.
  const probe = '.react-flow__edge[data-id="expert:expert-7"] .agent-flow-particle';
  await page.click("textarea.input");
  const before = await js(`(() => {
    const graph = document.querySelector('${flow}');
    const dot = graph.querySelector('${probe}');
    const r = graph.getBoundingClientRect(), m = dot.getScreenCTM();
    const { x, y } = dot.getPointAtLength(dot.getTotalLength() / 2).matrixTransform(m);
    if (x <= r.left || x >= r.right || y <= r.top || y >= r.bottom) throw new Error('working wire is outside the draft probe');
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
    const animation = graph.querySelector('${probe}').getAnimations()[0];
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

async function flowTopology(page, parents) {
  const edges = Object.entries(parents);
  await page.waitFor(`(() => {
    const graph = document.querySelector('${flow}');
    return graph?.querySelectorAll('.agent-flow-wire').length === ${edges.length} && ${JSON.stringify(Object.keys(parents))}.every((dst) =>
      graph.querySelector('.react-flow__edge[data-id=' + CSS.escape(dst) + '] .agent-flow-wire'));
  })()`);
  return page.eval(`(() => {
    const graph = document.querySelector('${flow}');
    const rect = (el) => el.getBoundingClientRect();
    const nodes = [...graph.querySelectorAll('.react-flow__node')];
    const rings = new Map(nodes.map((n) => [n.dataset.id, rect(n.querySelector('.agent-flow-ring'))]));
    const boxes = nodes.map(rect);
    const chart = rect(graph);
    const fits = boxes.every((b) => b.left >= chart.left && b.right <= chart.right && b.top >= chart.top && b.bottom <= chart.bottom);
    const disjoint = boxes.every((a, i) => boxes.slice(i + 1).every((b) => a.right <= b.left || b.right <= a.left || a.bottom <= b.top || b.bottom <= a.top));
    const expected = ${JSON.stringify(edges)};
    const wires = expected.map(([dst, src]) => {
      const path = graph.querySelector('.react-flow__edge[data-id=' + CSS.escape(dst) + '] .agent-flow-wire');
      const from = rings.get(src), to = rings.get(dst);
      if (!path || !from || !to) return { src, dst, correct: false };
      const matrix = path.getScreenCTM();
      const start = path.getPointAtLength(0).matrixTransform(matrix), end = path.getPointAtLength(path.getTotalLength()).matrixTransform(matrix);
      const tolerance = 5 * matrix.a + 0.3;
      const centered = (y, box) => Math.abs(y - (box.top + box.height / 2)) < 0.5;
      return { src, dst, correct: from.right < to.left && Math.abs(start.x - from.right) <= tolerance && Math.abs(end.x - to.left) <= tolerance &&
        centered(start.y, from) && centered(end.y, to) };
    });
    return { ids: nodes.map((n) => n.dataset.id), fits, disjoint, wires,
      correct: nodes.length === expected.length + 1 && disjoint && wires.every((e) => e.correct) };
  })()`);
}

export function agentFlowScreen() {
  const graph = document.querySelector('.agent-flow');
  const rect = graph.getBoundingClientRect();
  const chart = { x: rect.x, y: rect.y, width: rect.width, height: rect.height };
  const zoom = new DOMMatrixReadOnly(getComputedStyle(graph.querySelector('.react-flow__viewport')).transform).a;
  const nodes = Object.fromEntries([...graph.querySelectorAll('.react-flow__node')].map((node) => {
    const r = node.getBoundingClientRect(), name = node.querySelector('.agent-flow-name');
    const visible = r.left >= rect.left + 0.5 && r.right <= rect.right - 0.5 && r.top >= rect.top + 0.5 && r.bottom <= rect.bottom - 0.5;
    return [node.dataset.id, { x: r.x + r.width / 2, y: r.y + r.height / 2, visible, name: name.textContent,
      namePx: zoom * parseFloat(getComputedStyle(name).fontSize), focused: node.contains(document.activeElement) }];
  }));
  return { chart, zoom, nodes, overview: !!graph.querySelector('.agent-flow-overview') };
}

const screenOf = (page) => page.eval(`(${agentFlowScreen.toString()})()`);
const visibleNames = (screen, identity) => [screen.nodes[`group:${identity}`], ...agents.filter((a) => a.fusionIdentity === identity)
  .map((a) => screen.nodes[`expert:${a.id}`])].filter((node) => node?.visible);
const readableBranch = (screen, identity) => screen.zoom >= 0.8 && screen.nodes[`group:${identity}`].visible &&
  visibleNames(screen, identity).filter((node) => node.namePx >= 10).length >= 2;

async function panToExpert(page, id) {
  const first = await screenOf(page);
  for (let i = 0; i < 10; i++) {
    const { chart, nodes, zoom } = await screenOf(page);
    const node = nodes[id];
    if (node.visible) return { from: first, to: { chart, zoom, node } };
    const clamp = (delta, size) => Math.max(-size * 0.7, Math.min(delta, size * 0.7));
    const dx = clamp(chart.x + chart.width / 2 - node.x, chart.width);
    const dy = clamp(chart.y + chart.height / 2 - node.y, chart.height);
    const x = chart.x + (chart.width - dx) / 2, y = chart.y + (chart.height - dy) / 2;
    await page.mouse('mousePressed', x, y, { button: 'middle', buttons: 4 });
    await page.mouse('mouseMoved', x + dx, y + dy, { button: 'middle', buttons: 4 });
    await page.mouse('mouseReleased', x + dx, y + dy, { button: 'middle', buttons: 0 });
    await page.wait(60);
  }
  throw new Error(`could not pan to ${id}`);
}

async function checkFlow(page, theme, label, ok) {
  const js = page.eval;
  await page.waitFor(`document.documentElement.dataset.theme === '${theme}'`);
  const closed = await flowTopology(page, closedTree);
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
  ok(label("the flow sits beneath the title, fills the rail width and uses 25% of the window"), layout.underTitle && layout.aboveList &&
    Math.abs(layout.width - layout.available) <= 1 && Math.abs(layout.height - await js("innerHeight / 4")) <= 1, layout);
  const summary = await js(`(() => {
    const graph = document.querySelector('${flow}');
    return { nests: [...graph.querySelectorAll('.react-flow__node-nest button')].map((b) => [
      b.querySelector('.agent-flow-name').textContent, b.querySelector('.agent-flow-model').textContent, b.getAttribute('aria-expanded'),
    ]), singleton: graph.querySelector('[data-id="expert:expert-11"] button .agent-flow-model')?.textContent };
  })()`);
  ok(label("real identities fold across models, separate same-model experts and leave a singleton as a directly clickable dragon"),
    closed.correct && closed.fits && summary.singleton === "Observer" && JSON.stringify(summary.nests) === JSON.stringify([
      ["Coder", "5 位 · 展开 ▸", "false"], ["Runner", "6 位 · 展开 ▸", "false"],
    ]), { closed, summary });

  const img = `${nestButton("Coder")} img`;
  const selectedImage = (mime) => `(() => { const img = document.querySelector('${img}'); return img?.complete && img.naturalWidth === 48 && img.naturalHeight === 48 && img.currentSrc.startsWith('data:image/${mime};'); })()`;
  await page.waitFor(selectedImage("gif"));
  await page.waitFor(flowIsMoving);
  await setMotion(page, "reduce");
  await page.waitFor(selectedImage("png"));
  await page.waitFor("matchMedia('(prefers-reduced-motion: reduce)').matches && document.querySelector('#conversation-team').getAnimations({ subtree: true }).length === 0");
  ok(label("reduced motion chooses the PNG nest, hides the dots, retains static working wires and freezes waiting wires and sprites"), await js(`(() => {
    const rail = document.querySelector('#conversation-team');
    const dots = [...rail.querySelectorAll('.agent-flow-particle')], signals = [...rail.querySelectorAll('.agent-flow-signal')];
    return ${selectedImage("png")} && dots.length > 0 && dots.every((dot) => getComputedStyle(dot).display === 'none') &&
      signals.length === dots.length && signals.every((signal) => getComputedStyle(signal).display !== 'none') &&
      rail.querySelectorAll('.dragon-sprite.playing').length > 0 &&
      [...rail.querySelectorAll('.dragon-sprite.playing')].every((sprite) => getComputedStyle(sprite).backgroundPosition === '0px 0px');
  })()`));
  await setMotion(page, "no-preference");
  await page.waitFor(selectedImage("gif"));

  const expanded = (identity, state) => `document.querySelector('${nestButton(identity)}').getAttribute('aria-expanded') === '${state}'`;
  const watched = await js("__fake.callsOf('agents.watch').length");
  await page.click(nestButton("Coder"));
  await page.waitFor(expanded("Coder", true));
  const clickedCoder = await screenOf(page);
  await page.key(" ", { code: "Space", vk: 32 });
  await page.waitFor(expanded("Coder", false));
  const viaSpace = await flowTopology(page, closedTree);
  await page.key("Enter");
  await page.waitFor(expanded("Coder", true));
  const coder = await screenOf(page);
  const coderFirst = 'expert:expert-0';
  await page.key("Tab");
  const viaTab = await screenOf(page);
  ok(label("Tab brings the off-canvas first expert into view without changing zoom or opening the inspector"),
    !coder.nodes[coderFirst].visible && viaTab.nodes[coderFirst].visible && viaTab.nodes[coderFirst].focused && viaTab.zoom === coder.zoom, viaTab.nodes[coderFirst]);
  await page.click(overviewButton);
  const oneTree = Object.fromEntries(Object.entries(fullTree).filter(([, src]) => src !== 'group:Runner'));
  const overview = await flowTopology(page, oneTree);
  await page.click(nestButton("Runner"));
  await page.waitFor(expanded("Runner", true));
  const openTree = await flowTopology(page, fullTree);
  const runner = await screenOf(page);
  const branch = (screen, identity) => ({ chart: screen.chart, zoom: screen.zoom, visible: visibleNames(screen, identity).map(({ name, namePx }) => ({ name, namePx })) });
  const metrics = { coder: branch(clickedCoder, 'Coder'), runner: branch(runner, 'Runner') };
  ok(label("a single and then a second expanded group frame readable names and adjacent members at native viewport size"),
    readableBranch(clickedCoder, 'Coder') && readableBranch(coder, 'Coder') && readableBranch(runner, 'Runner') && !openTree.fits &&
    (theme !== 'dark' || (runner.chart.width === 234 && runner.chart.height === 150)), metrics);
  console.log(label(`focus metrics ${JSON.stringify(metrics)}`));
  ok(label("click and native Space/Enter toggle only the nests; overview fits and all opened branches have their own space and wire endpoints"),
    viaSpace.correct && viaSpace.fits && overview.correct && overview.fits && openTree.correct &&
    await js(`!document.querySelector('.agentpanel') && __fake.callsOf('agents.watch').length === ${watched}`), { viaSpace, overview, openTree });
  await checkDraft(page, label, ok);

  const portraits = await js(`(() => {
    const graph = document.querySelector('#conversation-team .agent-flow');
    const zoom = new DOMMatrixReadOnly(getComputedStyle(graph.querySelector('.react-flow__viewport')).transform).a;
    const near = (a, b) => Math.abs(a - b) < 0.01;
    return [...graph.querySelectorAll('.react-flow__node')].map((node) => {
      const id = node.dataset.id;
      const rings = node.querySelectorAll('.agent-flow-ring');
      const ring = rings[0];
      const nest = node.matches('.react-flow__node-nest');
      const sprite = ring?.querySelector(nest ? '.agent-flow-nest-art img' : '.dragon-sprite');
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
      const fullAsset = nest ? sprite.naturalWidth === 48 && sprite.naturalHeight === 48 && image.imageRendering === 'pixelated'
        : image.backgroundImage !== 'none' && image.backgroundSize === sheetWidth + 'px 48px';
      const fullFrame = fullAsset && frameSize.every((size) => near(size, 48));
      const fits = near(boxSize[0], boxSize[1]) && radii.every((r) => r === '0px') && border.every((w) => w > 0) && unclipped && fullFrame &&
        clearance.every((gap) => gap >= -0.01) && near(clearance[0], clearance[2]) && near(clearance[1], clearance[3]);
      return { id, fits, boxSize, frameSize, radii, border, clearance, unclipped, backgroundSize: image.backgroundSize };
    });
  })()`);
  ok(label("every main, nest and expert portrait has a sharp square border around its full centered 48×48 image"),
    portraits.length === agents.length + 1 + nests.length && portraits.every((portrait) => portrait.fits), portraits);

  await page.waitFor(flowIsMoving);
  const colours = await js(`(${agentFlowColours.toString()})()`);
  ok(label("theme surfaces and labels match; state edges and rings have at least 3:1 contrast"), colours.transparentCanvas &&
    (theme === "light" ? colours.luminance > 0.85 : colours.luminance < 0.08) && colours.labelContrast >= 4.5 && colours.modelContrast >= 4.5 &&
    colours.gridContrast > 1.5 && colours.particleContrast >= 3 && Object.keys(colours.edges).length === 6 &&
    Object.values(colours.edges).every((edge) => edge.contrast >= 3 && edge.ringContrast >= 3), colours);
  const wires = await js("[...document.querySelectorAll('#conversation-team .agent-flow-wire')].map((wire) => wire.getAttribute('d'))");
  ok(label("edges are right-angled polylines, not curves"), wires.length === Object.keys(fullTree).length && wires.every((d) => {
    // React Flow writes a corner of radius 0 as a quadratic whose control point is its end point: still a sharp corner.
    const commands = [...d.matchAll(/([A-Za-z])([^A-Za-z]*)/g)].map((m) => [m[1], (m[2].match(/-?\d+(?:\.\d+)?/g) ?? []).map(Number)]);
    const sharp = commands.every(([name, n]) => name === "M" || name === "L" ? n.length === 2
      : name === "Q" && n.length === 4 && Math.abs(n[0] - n[2]) < 0.01 && Math.abs(n[1] - n[3]) < 0.01);
    const points = commands.map(([, n]) => n.slice(-2));
    // Aligned handles may only need a straight horizontal segment.
    return sharp && points.length >= 2 && points.every(([x, y], i) => i === 0 || Math.abs(x - points[i - 1][0]) < 0.01 || Math.abs(y - points[i - 1][1]) < 0.01);
  }), wires.slice(0, 3));
  ok(label("working edges move, waiting edges pulse, and done edges stay still"), await js(`(${flowIsMoving}) && (() => {
    const done = document.querySelector('${flow} .agent-flow-edge.done');
    return done.getAnimations({ subtree: true }).length === 0 && !done.querySelector('.agent-flow-particle');
  })()`));
  const xOut = runner.chart.x + runner.chart.width / 2, yOut = runner.chart.y + runner.chart.height / 2;
  await page.send('Input.dispatchMouseEvent', { type: 'mouseWheel', x: xOut, y: yOut, deltaX: 0, deltaY: 240 });
  await page.wait(200);
  const zoomedOut = await screenOf(page);
  ok(label('wheel zoom can go below the automatic readable scale'), zoomedOut.zoom < 0.8, { before: runner.zoom, after: zoomedOut.zoom });
  await page.click(nestButton("Runner"));
  await page.waitFor(expanded("Runner", false));

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

async function checkGroupUpdates(page, patch, sessionId, label, ok) {
  const js = page.eval;
  const [first, second, runner, observer] = [agents[0], agents[1], agents[2], agents[11]];
  const compact = [first, { ...second, fusionIdentity: "Runner" }, runner, observer];
  await patch({ agents: compact });
  const shrunk = await flowTopology(page, { "expert:expert-0": "team-main", "group:Runner": "team-main", "expert:expert-11": "team-main" });
  await page.click(nestButton("Runner"));
  const moved = await flowTopology(page, {
    "expert:expert-0": "team-main", "group:Runner": "team-main", "expert:expert-11": "team-main",
    "expert:expert-1": "group:Runner", "expert:expert-2": "group:Runner",
  });
  const reform = [first, second, runner, observer];
  await patch({ agents: reform });
  const restored = await flowTopology(page, { "group:Coder": "team-main", "expert:expert-2": "team-main", "expert:expert-11": "team-main" });
  ok(label("identity/count updates move the same expert id; shrinking shows the singleton and re-forming starts closed"),
    shrunk.correct && shrunk.fits && moved.correct && restored.correct && restored.fits && await js(`document.querySelector('${nestButton("Coder")}').getAttribute('aria-expanded') === 'false'`),
    { shrunk, moved, restored });

  await page.click(nestButton("Coder"));
  await page.waitFor(`document.querySelector('${nestButton("Coder")}').getAttribute('aria-expanded') === 'true'`);
  const otherRow = await js("[...document.querySelectorAll('.srow')].findIndex((row) => row.textContent.includes('Devin 会话'))");
  await page.click(".srow", otherRow);
  const lastSelected = "__fake.events.findLast(([name, payload]) => name === 'tabs' && payload.select)[1].select";
  await page.waitFor(`${lastSelected} !== ${JSON.stringify(sessionId)} && !!document.querySelector('.tab.current .dot-ready')`);
  const otherSession = await js(lastSelected);
  // Same identity and member ids must still start collapsed in a different conversation.
  await js(`__fake.patch(${JSON.stringify(otherSession)}, ${JSON.stringify({ agents: reform })})`);
  const other = await flowTopology(page, { "group:Coder": "team-main", "expert:expert-2": "team-main", "expert:expert-11": "team-main" });
  ok(label("an expanded nest never leaks into another session"), other.correct && other.fits &&
    await js(`document.querySelector('${nestButton("Coder")}').getAttribute('aria-expanded') === 'false'`), other);

  if (await js('innerWidth === 1440')) {
    await js("__fake.emit('settings', { settings: { maxOpen: 4, theme: 'light', fontScale: 'md', sidebarWidth: 300, conversationWidth: 420 } })");
    await js(`__fake.patch(${JSON.stringify(otherSession)}, ${JSON.stringify({ agents: [first, second, runner] })})`);
    await page.waitFor(`document.querySelector('${flow}').clientWidth >= 350 && !document.querySelector('${flow} [data-id="expert:expert-11"]')`);
    await page.click(nestButton('Coder'));
    const wide = await flowTopology(page, { 'group:Coder': 'team-main', 'expert:expert-2': 'team-main',
      'expert:expert-0': 'group:Coder', 'expert:expert-1': 'group:Coder' });
    const screen = await screenOf(page);
    ok(label('when a wider canvas can fit the open tree legibly, it still fits the whole tree'), wide.correct && wide.fits &&
      screen.zoom >= 0.8 && !screen.overview, { zoom: screen.zoom, chart: screen.chart });
  }
}

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
      await js("document.querySelector('.conversation-rail-content').scrollTop = 0");
      const panned = await panToExpert(page, 'expert:expert-0');
      ok(label('middle-button panning reaches an initially off-canvas expert without zooming out'),
        !panned.from.nodes['expert:expert-0'].visible && panned.to.node.visible && panned.to.node.namePx >= 10 &&
        panned.from.zoom === panned.to.zoom, panned.to);
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
      ok(label("Escape closes the inspector and returns to the conversation rail"), await js("!document.querySelector('.agentpanel') && !!document.querySelector('#conversation-team .agent-chip') && !document.querySelector('#conversation-team .agent-chip-tokens')"));
      // The inspector replaces the rail, which remounts with its disclosures closed on return.
      if (await js(`document.querySelector('${nestButton("Coder")}')?.getAttribute('aria-expanded') === 'false'`)) await page.click(nestButton("Coder"));
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
      await checkGroupUpdates(page, patch, id, label, ok);
      ok(label("no console errors or CSP violations"), page.errorTexts().length === 0, page.errorTexts());
    } finally {
      await page.close();
    }
  }
}

// Standalone focused check: pass a freshly built index.html under the managed test runner.
if (process.argv[1] && import.meta.url === pathToFileURL(resolve(process.argv[1])).href) {
  const work = process.env.WITTYCODE_TEST_WORK;
  if (!work || !process.argv[2]) throw new Error("Use test_runs.py -- node frontend/scripts/check-agents-team.mjs <built index.html>");
  const url = pathToFileURL(resolve(process.argv[2])).href;
  process.chdir(work);  // Qt client extraction and the browser profile are disposable.
  const { fakeBackendScripts } = await import("./fake-backend.mjs");
  const { scripts, client } = fakeBackendScripts({ initHold: true });
  console.log(`QWebChannel client: ${client}`);
  let failed = 0;
  await checkAgentsTeam({ url, scripts, ok: (name, pass, detail) => {
    if (!pass) failed++;
    console.log(pass ? "PASS" : "FAIL", name, !pass && detail !== undefined ? `\n     got: ${JSON.stringify(detail)}` : "");
  } });
  if (failed) process.exitCode = 1;
}
