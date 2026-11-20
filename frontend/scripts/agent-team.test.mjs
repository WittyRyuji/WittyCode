import assert from "node:assert/strict";
import test from "node:test";
import { groupAgentIdentities, groupAgents } from "../src/lib/agentTeam.ts";
import { FLOW_MAIN, FLOW_NODE_WIDTH, FLOW_PORTRAIT_SIZE, buildAgentFlowTree, flowBranchBounds, keepExpandedGroups } from "../src/lib/agentFlowTree.ts";
import { FLOW_SPARK_PERIOD_MS, FLOW_SPARK_SPACING, flowArrivalDelay } from "../src/lib/agentFlowSignal.ts";

const pointsOf = (edge) => edge.route.path.slice(1).split(" L").map((xy) => xy.split(" ").map(Number));

const agent = (id, status, startedAt, finishedAt = null) => ({ id, status, startedAt, finishedAt });

test("team shows active experts above newest-first history without mutating the source", () => {
  const input = [agent("old", "done", 1, 2), agent("running", "working", 7), agent("stopped", "stopped", 3, 5),
    agent("waiting", "waiting", 8), agent("failed", "failed", 6, 9), agent("pending", "pending", 0)];
  const { active, history } = groupAgents(input);
  assert.deepEqual(active.map(a => a.id), ["waiting", "running", "pending"]);
  assert.deepEqual(history.map(a => a.id), ["failed", "stopped", "old"]);
  assert.equal(input[0].id, "old");
  assert.equal(new Set([...active, ...history]).size, input.length);
});

test("a reused historical expert returns to the active team and completed work moves back", () => {
  const a = agent("owner", "done", 1, 2);
  assert.equal(groupAgents([a]).active.length, 0);
  a.status = "working"; a.startedAt = 3; a.finishedAt = null;
  assert.equal(groupAgents([a]).history.length, 0);
  a.status = "done"; a.finishedAt = 4;
  assert.deepEqual(groupAgents([a]).history, [a]);
  assert.deepEqual(groupAgents([]), { active: [], history: [] });
});

const expert = (id, fusionIdentity, status = "working", extra = {}) => ({
  ...agent(id, status, 0), name: "Runner is only part of this name", role: "工作专家", fusionRole: "worker", model: "Sol", fusionIdentity, ...extra,
});

test("identity wins over model, pool, name and status; legacy records use only their explicit role", () => {
  const input = [expert("b", "Coder", "waiting", { model: "Luna" }), expert("a", "Coder"), expert("r", "Runner"),
    expert("o", "Observer", "done", { fusionRole: "light" }), expert("q", "QualitySampler"),
    expert("legacy", null), expert("plain", undefined, "working", { role: "  " })];
  const expected = [
    ["Coder", ["a", "b"], "waiting"], ["Observer", ["o"], "done"], ["QualitySampler", ["q"], "working"],
    ["Runner", ["r"], "working"], ["Sub-Agent", ["plain"], "working"], ["工作专家", ["legacy"], "working"],
  ];
  const shape = (agents) => groupAgentIdentities(agents).map(({ identity, members, status }) => [identity, members.map(({ id }) => id), status]);
  assert.deepEqual(shape(input), expected);
  assert.deepEqual(shape(input.toReversed()), expected);
  assert.equal(input[0].id, "b");
  assert.equal(groupAgentIdentities([expert("a", "Coder", "failed"), expert("b", "Coder", "working")])[0].status, "working");
  assert.equal(groupAgentIdentities([expert("a", "Coder", "failed"), expert("b", "Coder", "done")])[0].status, "failed");
});

test("singletons connect directly; expanded nests fan out in disjoint bands to stable expert ids", () => {
  const groups = groupAgentIdentities([expert("a", "Coder"), expert("b", "Coder"), expert("c", "Observer"),
    expert("d", "Runner"), expert("e", "Runner"), expert("f", "Runner")]);
  const closed = buildAgentFlowTree(groups, new Set());
  assert.deepEqual(closed.nodes.map((n) => n.id), ["group:Coder", "expert:c", "group:Runner"]);
  assert.ok(closed.edges.every((e) => e.source === FLOW_MAIN));
  const open = new Set(["group:Coder", "group:Runner"]);
  const { main, nodes, edges, bounds } = buildAgentFlowTree(groups, open);
  assert.deepEqual(edges.map(({ source, target }) => [source, target]), [
    [FLOW_MAIN, "group:Coder"], ["group:Coder", "expert:a"], ["group:Coder", "expert:b"], [FLOW_MAIN, "expert:c"],
    [FLOW_MAIN, "group:Runner"], ["group:Runner", "expert:d"], ["group:Runner", "expert:e"], ["group:Runner", "expert:f"],
  ]);
  for (const n of [main, ...nodes]) {
    assert.ok(n.x >= 0 && n.y >= 0 && n.x + FLOW_NODE_WIDTH <= bounds.width && n.y + n.height <= bounds.height);
    assert.ok(nodes.filter((other) => other !== n).every((other) => n.x + FLOW_NODE_WIDTH <= other.x || other.x + FLOW_NODE_WIDTH <= n.x ||
      n.y + n.height <= other.y || other.y + other.height <= n.y));
  }
  const find = (id) => nodes.find((n) => n.id === id);
  assert.ok(main.x < find("group:Coder").x && find("group:Coder").x < find("expert:a").x);
  assert.equal(find("expert:a").x, find("expert:f").x);
  assert.ok(find("expert:b").y + find("expert:b").height < find("expert:d").y);
  const bendTo = (id) => pointsOf(edges.find((e) => e.target === id))[1][0];
  assert.ok(bendTo("group:Coder") < bendTo("expert:c"), "the outer branch bends earlier than the inner branch");

  const moved = groupAgentIdentities([expert("a", "Coder"), expert("b", "Runner"), expert("c", "Observer"),
    expert("d", "Runner"), expert("e", "Runner"), expert("f", "Runner")]);
  const remaining = keepExpandedGroups(moved, open);
  assert.deepEqual([...remaining], ["group:Runner"]);
  const after = buildAgentFlowTree(moved, remaining);
  assert.equal(after.edges.find((e) => e.target === "expert:a").source, FLOW_MAIN);
  assert.equal(after.edges.find((e) => e.target === "expert:b").source, "group:Runner");
  assert.ok(!buildAgentFlowTree(groups, remaining).nodes.some((n) => n.id === "expert:a"), "a re-formed nest starts closed");
});

test("closed and expanded fans leave distinct, ordered ports with no shared wire segments or crossings", () => {
  const group = (identity, n) => Array.from({ length: n }, (_, i) => expert(`${identity}-${i}`, identity));
  const groups = groupAgentIdentities([...group("Coder", 6), ...group("QualitySampler", 5), ...group("Runner", 6)]);
  for (const expanded of [[], ["group:Coder"], groups.map((g) => g.id)]) {
    const { main, nodes, edges } = buildAgentFlowTree(groups, new Set(expanded));
    const nodeOf = new Map([main, ...nodes].map((node) => [node.id, node]));
    const inset = (FLOW_NODE_WIDTH - FLOW_PORTRAIT_SIZE) / 2;
    const ports = new Map();
    const segments = [];
    for (const edge of edges) {
      const points = pointsOf(edge);
      const [sx, sy] = points[0], [ex, ey] = points.at(-1);
      const src = nodeOf.get(edge.source), dst = nodeOf.get(edge.target);
      assert.equal(sx, src.x + FLOW_NODE_WIDTH - inset);
      assert.ok(sy >= src.y + 7 && sy <= src.y + FLOW_PORTRAIT_SIZE - 7);
      assert.deepEqual([ex, ey], [dst.x + inset, dst.y + FLOW_PORTRAIT_SIZE / 2]);
      assert.equal(edge.route.endX, ex);
      assert.equal(edge.route.endY, ey);
      const siblings = ports.get(src.id) ?? [];
      siblings.push([sy, ey]);
      ports.set(src.id, siblings);
      let length = 0;
      for (let i = 1; i < points.length; i++) {
        const [x, y] = points[i - 1], [u, v] = points[i];
        assert.ok((x === u && y !== v) || (y === v && x < u), "each segment is nonzero and never backtracks horizontally");
        segments.push({ id: edge.id, x, right: u, y: Math.min(y, v), bottom: Math.max(y, v) });
        length += Math.abs(u - x) + Math.abs(v - y);
      }
      assert.ok(Math.abs(edge.route.length - length) < 1e-9);
    }
    for (const siblings of ports.values()) {
      siblings.sort((a, b) => a[1] - b[1]);
      assert.ok(siblings.every(([sy], i) => i === 0 || sy > siblings[i - 1][0]), "ports follow target row order");
    }
    for (const [i, a] of segments.entries()) for (const b of segments.slice(i + 1)) {
      if (a.id === b.id) continue;
      assert.ok(a.right < b.x || b.right < a.x || a.bottom < b.y || b.bottom < a.y, `${a.id} intersects ${b.id}`);
    }
  }
});

test("sparks use one 57px beat for short and long arcs, with the flash timed at the target", () => {
  assert.equal(FLOW_SPARK_SPACING, 57);
  const speed = FLOW_SPARK_SPACING / (FLOW_SPARK_PERIOD_MS / 1000);
  assert.ok(Math.abs(speed - 105.56) < 0.01);
  for (const length of [70, 114, 140.5, 500]) {
    const delay = flowArrivalDelay(length);
    assert.ok(delay >= 0 && delay < FLOW_SPARK_PERIOD_MS);
    const lead = Math.floor(length / 57) * 57;
    assert.ok(Math.abs(lead + speed * delay / 1000 - length) < 1e-9);
  }
});

test('the focused bounds track the toggled nest and fit its nearest member rows at readable scale', () => {
  const group = (identity, n) => Array.from({ length: n }, (_, i) => expert(`${identity}-${i}`, identity));
  const groups = groupAgentIdentities([...group('Coder', 5), ...group('Runner', 6), ...group('Observer', 1)]);
  const open = buildAgentFlowTree(groups, new Set(['group:Coder', 'group:Runner']));
  const boxed = (tree, box) => tree.nodes.filter((n) => n.x >= box.x && n.y >= box.y &&
    n.x + FLOW_NODE_WIDTH <= box.x + box.width && n.y + n.height <= box.y + box.height).map((n) => n.id);
  for (const identity of ['Coder', 'Runner']) {
    const bounds = flowBranchBounds(open, `group:${identity}`);
    assert.ok(bounds.width * 0.85 < 232 && bounds.height * 0.85 < 148);
    assert.deepEqual(boxed(open, bounds), [`group:${identity}`, `expert:${identity}-2`, `expert:${identity}-3`]);
  }
  assert.equal(flowBranchBounds(buildAgentFlowTree(groups, new Set()), 'group:Runner'), null);
  const coderOnly = buildAgentFlowTree(groups, new Set(['group:Coder']));
  assert.deepEqual(boxed(coderOnly, flowBranchBounds(coderOnly, 'group:Runner')), ['group:Runner']);
  assert.deepEqual(flowBranchBounds(coderOnly, 'missing'), flowBranchBounds(coderOnly, 'group:Coder'));
});
