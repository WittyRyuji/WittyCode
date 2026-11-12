import assert from "node:assert/strict";
import test from "node:test";
import { usagePercent, usageSlices } from "../src/lib/usage.ts";
import { buildModelMenu } from "../src/components/modelMenu.ts";
import { usageTotal } from "../src/lib/format.ts";

const models = [
  { id: "chatgpt:a", label: "Model A", source: "chatgpt" },
  { id: "devin:b", label: "Model B", source: "devin" },
];

test("model shares use all tokens, with missing history explicitly unknown", () => {
  const total = usageTotal({ input: 100, output: 50, cacheWrite: 50, cacheRead: 200 });
  const rows = usageSlices(total, { "chatgpt:a": 200, "devin:b": 100 }, models);
  assert.deepEqual(rows.map(({ label, tokens, share }) => [label, tokens, share]), [["Model A", 200, 0.5], ["Model B", 100, 0.25], ["未归属", 100, 0.25]]);
  assert.equal(rows.reduce((sum, r) => sum + r.share, 0), 1);
  assert.equal(usagePercent(rows[0].share), "50.0%");
  assert.equal(usagePercent(0.0001), "<0.1%");
});

test("zero usage has no invented pie and legacy or inconsistent data is unknown", () => {
  assert.deepEqual(usageSlices(0, { a: 10 }), []);
  assert.deepEqual(usageSlices(NaN), []);
  assert.deepEqual(usageSlices(100).map(({ label, share }) => [label, share]), [["未归属", 1]]);
  assert.deepEqual(usageSlices(100, { a: 101 }).map(({ label, share }) => [label, share]), [["未归属", 1]]);
  assert.deepEqual(usageSlices(100, { a: NaN, b: -1, c: Infinity }).map(({ label, share }) => [label, share]), [["未归属", 1]]);
});

test("a model that disappeared from the catalog keeps its id and a stable color", () => {
  const one = usageSlices(100, { "chatgpt:a": 100 }, models)[0];
  const unknownName = usageSlices(200, { "chatgpt:a": 100, "devin:b": 100 }) .find((r) => r.id === "chatgpt:a");
  assert.equal(one.share, 1);
  assert.equal(unknownName.label, "a");
  assert.equal(unknownName.color, one.color);
});

test("model menu drops repetitive handover notes but retains the running-turn restriction", () => {
  const input = {
    models, providers: [], current: "chatgpt:a", engine: "chatgpt", onPick() {}, onManage() {},
    devin: { enabled: true, auth: "signed-in" },
    chatgpt: { available: true, enabled: true, auth: "signed-in", planEnabled: true },
  };
  const idle = buildModelMenu(input).find((r) => r.id === "devin:b");
  assert.equal(idle.description, undefined);
  assert.equal(idle.disabled, false);
  const running = buildModelMenu({ ...input, running: true }).find((r) => r.id === "devin:b");
  assert.equal(running.disabled, true);
  assert.match(running.description, /本轮结束后/);
});
