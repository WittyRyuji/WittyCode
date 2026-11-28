import assert from "node:assert/strict";
import test from "node:test";
import { groupAgentIdentities, groupAgents } from "../src/lib/agentTeam.ts";
import { FLOW_MAIN, FLOW_NODE_WIDTH, FLOW_PORTRAIT_SIZE, buildAgentFlowTree, flowBranchBounds, keepExpandedGroups } from "../src/lib/agentFlowTree.ts";
import { FLOW_SPARK_PERIOD_MS, FLOW_SPARK_SPACING, flowArrivalDelay, flowSparkPhase } from "../src/lib/agentFlowSignal.ts";

const pointsOf = (path) => path.slice(1).split(" L").map((xy) => xy.split(" ").map(Number));

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

test("old unstarted transfers stay in history even if the saved record says pending", () => {
  const transfer = { ...agent("transfer", "pending", 0), unstartedTransfer: true };
  assert.deepEqual(groupAgents([transfer]), { active: [], history: [transfer] });
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
  const exits = closed.edges.map((edge) => pointsOf(edge.route.path));
  assert.deepEqual(exits.map((points) => points.length), [4, 2, 4]);
  assert.deepEqual(exits.map((points) => points[0][1] - closed.main.y), [15, 25, 35]);
  assert.equal(exits[0][1][0], exits[2][1][0], "upper and lower turns share one axis around the straight middle wire");

  const moved = groupAgentIdentities([expert("a", "Coder"), expert("b", "Runner"), expert("c", "Observer"),
    expert("d", "Runner"), expert("e", "Runner"), expert("f", "Runner")]);
  const remaining = keepExpandedGroups(moved, open);
  assert.deepEqual([...remaining], ["group:Runner"]);
  const after = buildAgentFlowTree(moved, remaining);
  assert.equal(after.edges.find((e) => e.target === "expert:a").source, FLOW_MAIN);
  assert.equal(after.edges.find((e) => e.target === "expert:b").source, "group:Runner");
  assert.ok(!buildAgentFlowTree(groups, remaining).nodes.some((n) => n.id === "expert:a"), "a re-formed nest starts closed");
});

// Allow a corner or an endpoint meeting a trunk (T), not a positive-length overlap or a crossing.
function checkSegments(segments) {
  let tees = 0;
  for (const [i, a] of segments.entries()) for (const b of segments.slice(i + 1)) {
    const x = Math.max(a.left, b.left), right = Math.min(a.right, b.right);
    const y = Math.max(a.top, b.top), bottom = Math.min(a.bottom, b.bottom);
    if (x > right || y > bottom) continue;
    assert.ok(x === right && y === bottom && a.src === b.src, "no segment doubles back, redraws another, or touches another fan");
    const inside = (s) => s.left < x && x < s.right || s.top < y && y < s.bottom;
    assert.ok(!inside(a) || !inside(b), "no two interiors cross");
    if (inside(a) || inside(b)) tees++;
  }
  return tees;
}

const pathLength = (points) => points.slice(1).reduce((length, [x, y], i) =>
  length + Math.abs(x - points[i][0]) + Math.abs(y - points[i][1]), 0);

test("both levels keep fixed mid-gap axes, unique trunks and status-aware signals at high fan-out", () => {
  const group = (identity, n) => Array.from({ length: n }, (_, i) => expert(`${identity}-${i}`, identity,
    i === 0 ? "failed" : i === 1 && n > 1 ? "waiting" : "working"));
  const groups = groupAgentIdentities([...group("Coder", 12), ...group("QualitySampler", 6), ...group("Runner", 5),
    ...Array.from({ length: 8 }, (_, i) => group(`Role-${i}`, 1)).flat()]);
  for (const expanded of [[], ["group:Coder"], groups.map((g) => g.id)]) {
    const { main, nodes, edges } = buildAgentFlowTree(groups, new Set(expanded));
    const nodeOf = new Map([main, ...nodes].map((node) => [node.id, node]));
    const inset = (FLOW_NODE_WIDTH - FLOW_PORTRAIT_SIZE) / 2;
    const trunks = edges.filter(({ route }) => route.trunk).map(({ source, route }) => ({ source, points: pointsOf(route.trunk) }));
    const base = [], signals = [];
    let clippedBranch = false, phasedBranch = false;
    for (const { source, target, status, route } of edges) {
      const src = nodeOf.get(source), dst = nodeOf.get(target);
      const sx = src.x + FLOW_NODE_WIDTH - inset, ex = dst.x + inset, ey = dst.y + FLOW_PORTRAIT_SIZE / 2;
      const axis = (sx + ex) / 2;
      assert.equal(axis, source === FLOW_MAIN ? 110 : 230);
      assert.equal(status, dst.kind === "nest" ? dst.group.status : dst.agent.status);
      const collect = (points, into) => {
        for (let i = 1; i < points.length; i++) {
          const [x, y] = points[i - 1], [u, v] = points[i];
          assert.ok((x === u && x === axis && y !== v) || (y === v && x < u));
          into.push({ src: source, left: x, right: u, top: Math.min(y, v), bottom: Math.max(y, v) });
        }
      };
      const points = pointsOf(route.path);
      const trunk = trunks.find((t) => t.source === source && route.spur &&
        (ey - t.points[1][1]) * (ey - t.points[2][1]) <= 0);
      const start = trunk ? trunk.points[0] : points[0];
      assert.equal(start[0], sx);
      assert.ok(Math.abs(start[1] - (src.y + FLOW_PORTRAIT_SIZE / 2)) <= 10);
      assert.deepEqual(points.at(-1), [ex, ey]);
      assert.deepEqual([route.endX, route.endY], [ex, ey]);
      assert.equal(!!trunk, route.spur);
      if (trunk) assert.deepEqual(points, [[axis, ey], [ex, ey]]);
      const full = trunk ? [...trunk.points.slice(0, 2), [axis, ey], [ex, ey]] : points;
      assert.equal(route.length, pathLength(full));
      collect(points, base);
      if (route.trunk) collect(pointsOf(route.trunk), base);
      assert.equal(!!route.signal, status === "working");
      if (route.signal) {
        const { path, prefix } = route.signal;
        const drawn = pointsOf(path);
        assert.deepEqual(drawn, prefix ? points : full);
        assert.equal(prefix + pathLength(drawn), route.length);
        collect(drawn, signals);
        phasedBranch ||= prefix > 0;
        clippedBranch ||= !!trunk && prefix === 0 && ey !== trunk.points[2][1];
      }
    }
    assert.ok(checkSegments(base) >= 6, "high fan-out really has T junctions");
    checkSegments(signals);
    if (expanded.length) assert.ok(phasedBranch && clippedBranch, "working taps share a phase but do not animate a failed outer tap");
  }
});

test("sparks use one 57px beat for short and long arcs, with the flash timed at the target", () => {
  assert.equal(FLOW_SPARK_SPACING, 57);
  const speed = FLOW_SPARK_SPACING / (FLOW_SPARK_PERIOD_MS / 1000);
  assert.ok(Math.abs(speed - 105.56) < 0.01);
  for (const prefix of [0, 38, 72, 140.5, 465]) {
    const tail = 35, length = prefix + tail;
    const delay = flowArrivalDelay(length);
    assert.ok(delay >= 0 && delay < FLOW_SPARK_PERIOD_MS);
    const lead = Math.floor(length / 57) * 57;
    assert.ok(Math.abs(lead + speed * delay / 1000 - length) < 1e-9);
    // The dash clock on the spur locates a head at its endpoint at the same beat.
    assert.ok(Math.abs((tail + flowSparkPhase(prefix) - speed * delay / 1000) % 57) < 1e-9);
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
