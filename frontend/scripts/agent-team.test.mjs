import assert from "node:assert/strict";
import test from "node:test";
import { groupAgents } from "../src/lib/agentTeam.ts";

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
