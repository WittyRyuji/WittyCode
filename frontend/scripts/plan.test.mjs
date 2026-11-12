import assert from "node:assert/strict";
import test from "node:test";
import { latestPlan } from "../src/lib/plan.ts";

const step = (text, state = "pending") => ({ text, state });
const write = (id, todos, status = "done") => ({ kind: "tool", id, name: "TodoWrite", category: "plan", status, todos });
const turn = (...entries) => ({ id: "t", status: "done", blocks: [{ kind: "activity", id: "g", entries }], prompts: [] });

test("only structured plan substeps open a panel, not thinking or a Plan mode label", () => {
  assert.equal(latestPlan([]), null);
  assert.equal(latestPlan([{ ...turn(), blocks: [{ kind: "text", id: "x", markdown: "Plan the changes" }] }]), null);
  assert.equal(latestPlan([turn({ kind: "thinking", id: "r", text: "make a plan" })]), null);
});

test("the latest plan replaces earlier snapshots and an empty update explicitly clears it", () => {
  const before = turn(write("p1", [step("read"), step("test")]));
  const next = turn(write("p2", [step("read", "completed"), step("test", "in_progress")]));
  assert.deepEqual(latestPlan([before, next]), { id: "p2", steps: next.blocks[0].entries[0].todos, completed: 1 });
  assert.equal(latestPlan([before, next, turn(write("p3", []))]), null);
  const done = latestPlan([before, turn(write("p4", [step("read", "completed"), step("test", "completed")]))]);
  assert.equal(done.completed, 2);
});

test("a failed, cancelled or partial update does not erase an accepted plan", () => {
  const before = turn(write("p1", [step("read")]));
  for (const bad of [write("p2", [], "error"), write("p3", [], "cancelled"), write("p4", [{ text: "partial", state: "unknown" }]), write("p5", [step("not started")], "pending")]) {
    assert.equal(latestPlan([before, turn(bad)]).id, "p1");
  }
});

test("an explicit current snapshot overrides stale tool rows and accepts same-id progress, text and clear updates", () => {
  const stale = turn(write("old", [step("旧步骤")]));
  const current = { ...stale, plan: { id: "current", steps: [step("检查", "completed"), step("实现", "in_progress")] } };
  assert.deepEqual(latestPlan([current]), { id: "current", steps: current.plan.steps, completed: 1 });
  const changed = { ...current, plan: { id: "current", steps: [step("新任务", "completed")] } };
  assert.deepEqual(latestPlan([changed]), { id: "current", steps: changed.plan.steps, completed: 1 });
  assert.equal(latestPlan([{ ...current, plan: { id: "clear", steps: [] } }]), null);
  assert.equal(latestPlan([stale, { ...turn(), plan: { id: "native", steps: [] } }]), null);
  assert.equal(latestPlan([{ ...stale, plan: { id: "bad", steps: [step("partial", "unknown")] } }]).id, "old");
});

test("a conversation without a plan never borrows another conversation's steps", () => {
  const withPlan = [turn(write("p1", [step("only in A")]))];
  assert.equal(latestPlan(withPlan).steps[0].text, "only in A");
  assert.equal(latestPlan([turn()]), null);
});
