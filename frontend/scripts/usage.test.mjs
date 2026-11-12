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

test("model colors do not collide, including the reported Sol and Terra pair and more models than the palette", () => {
  const ids = ["chatgpt:gpt-6-astra", "chatgpt:gpt-6.1-sol", "chatgpt:gpt-6.1-terra", ...Array.from({ length: 48 }, (_, i) => `api:demo:model-${i}`)];
  const colors = usageSlices(ids.length + 1, Object.fromEntries(ids.map((id) => [id, 1]))).map((row) => row.color);
  assert.equal(new Set(colors).size, ids.length + 1, "each model and the unknown balance need different actual colors");
});

test("usage colors are subdued rather than bright or highly saturated", () => {
  const rows = usageSlices(64, Object.fromEntries(Array.from({ length: 64 }, (_, i) => [`model-${i}`, 1])));
  for (const { color } of rows) {
    assert.match(color, /^#[a-f0-9]{6}$/i);
    const rgb = color.match(/[a-f0-9]{2}/gi).map((channel) => parseInt(channel, 16) / 255);
    const maximum = Math.max(...rgb), minimum = Math.min(...rgb);
    const lightness = (maximum + minimum) / 2;
    const saturation = maximum === minimum ? 0 : (maximum - minimum) / (1 - Math.abs(2 * lightness - 1));
    const linear = rgb.map((value) => value <= 0.04045 ? value / 12.92 : ((value + 0.055) / 1.055) ** 2.4);
    const luminance = linear[0] * 0.2126 + linear[1] * 0.7152 + linear[2] * 0.0722;
    assert(lightness >= 0.34 && lightness <= 0.49 && saturation <= 0.4 && luminance < 0.24, color);
  }
});

test("model colors stay stable as token rankings, catalog labels and object insertion order change", () => {
  const first = usageSlices(600, { sol: 100, terra: 200, astra: 300 });
  const second = usageSlices(600, { astra: 50, terra: 50, sol: 500 }, [{ id: "sol", label: "Renamed" }]);
  assert.deepEqual(Object.fromEntries(first.map((row) => [row.id, row.color])), Object.fromEntries(second.map((row) => [row.id, row.color])));
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
