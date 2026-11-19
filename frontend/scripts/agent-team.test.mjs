import assert from "node:assert/strict";
import test from "node:test";
import { groupAgentIdentities, groupAgents } from "../src/lib/agentTeam.ts";
import { FLOW_MAIN, FLOW_NODE_WIDTH, buildAgentFlowTree, flowBranchBounds, keepExpandedGroups } from "../src/lib/agentFlowTree.ts";

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
  const stepTo = (id) => edges.find((e) => e.target === id).step;
  assert.ok(stepTo("group:Coder") < stepTo("expert:c"), "the outer branch bends earlier than the inner branch");

  const moved = groupAgentIdentities([expert("a", "Coder"), expert("b", "Runner"), expert("c", "Observer"),
    expert("d", "Runner"), expert("e", "Runner"), expert("f", "Runner")]);
  const remaining = keepExpandedGroups(moved, open);
  assert.deepEqual([...remaining], ["group:Runner"]);
  const after = buildAgentFlowTree(moved, remaining);
  assert.equal(after.edges.find((e) => e.target === "expert:a").source, FLOW_MAIN);
  assert.equal(after.edges.find((e) => e.target === "expert:b").source, "group:Runner");
  assert.ok(!buildAgentFlowTree(groups, remaining).nodes.some((n) => n.id === "expert:a"), "a re-formed nest starts closed");
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
