import { resolve } from "node:path";
import { openPage } from "./cdp.mjs";

const firstLine = "请核对示例清单中的编号、名称与状态，并将观察到的差异记录在报告中。".repeat(8);
const brief = [firstLine, ...Array.from({ length: 12 }, (_, i) =>
  `样例 ${i + 1}：` + "逐项检查示例数据，记下预期结果与实际结果。核对完成后，整理一份便于复查的简要记录。".repeat(14))].join("\n\n");
const now = Date.now();
const agents = Array.from({ length: 12 }, (_, i) => ({
  id: `expert-${i}`, name: `检查专家 ${i + 1}`, role: "工作专家", model: "GPT-6.1-Sol", effort: "high",
  task: i === 0 ? brief : "核对一个样例。", status: i === 11 ? "done" : "working",
  startedAt: now - i, finishedAt: i === 11 ? now + 100 : null,
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

export async function checkAgentsTeam({ url, scripts, ok }) {
  const shots = process.env.WITTYCODE_AGENT_TEAM_SHOTS === "1";
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
      await page.waitFor("!!document.querySelector('#conversation-team .agent-chip')");
      await page.click("#conversation-team .agent-chip");
      await page.waitFor("document.querySelector('.agentpanel-run')?.textContent.includes('第 14 项')");
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
      if (shots && theme === "light") await page.shot(resolve(`../sandbox/agents-team-check/inspector-${theme}.png`));
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
      if (shots && theme === "dark") await page.shot(resolve(`../sandbox/agents-team-check/inspector-${theme}.png`));
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
      await page.key("Escape");
      ok(label("Escape closes the inspector without changing the conversation rail"), await js("!document.querySelector('.agentpanel') && !!document.querySelector('#conversation-team .agent-chip') && !document.querySelector('#conversation-team .agent-chip-tokens')"));
      ok(label("no console errors or CSP violations"), page.errorTexts().length === 0, page.errorTexts());
    } finally {
      await page.close();
    }
  }
}
