import assert from "node:assert/strict";
import test from "node:test";
import { fusionChoices, fusionDisabledReason, fusionSelectedModels } from "../src/components/fusionModels.ts";
import { applyEvent, emptyLiveData } from "../src/liveReducer.ts";

const models = [
  { id: "chatgpt:visible", label: "Visible", source: "chatgpt" },
  { id: "api:demo:hidden", label: "Hidden API", source: "api:demo" },
  { id: "devin:worker", label: "Devin Worker", source: "devin" },
  { id: "factory:other", label: "Factory", source: "factory" },
];

test("Fusion choices honour hidden models and include guarded ACP models", () => {
  const hidden = ["api:demo:hidden"];
  assert.deepEqual(fusionChoices(models, hidden, false).map((m) => m.id), ["chatgpt:visible"]);
  assert.deepEqual(fusionChoices(models, hidden, true).map((m) => m.id), ["chatgpt:visible", "devin:worker"]);
  assert.equal(fusionDisabledReason(models[2]), "");
  assert.equal(fusionDisabledReason(models[2], true), "");
  assert.equal(fusionDisabledReason({ ...models[2], disabledReason: "Unavailable" }), "Unavailable");
  assert.equal(fusionDisabledReason(models[0]), "");
  assert.deepEqual(fusionChoices(models, [...hidden, "devin:worker"], true).map((m) => m.id), ["chatgpt:visible"]);
});

test("selected Fusion models share one effort across roles without losing hidden or unavailable selections", () => {
  assert.deepEqual(fusionSelectedModels("", [], ""), []);
  assert.deepEqual(fusionSelectedModels("chatgpt:visible", ["api:demo:hidden", "chatgpt:visible", "api:demo:hidden"], "api:missing"), [
    { id: "chatgpt:visible", roles: ["主模型", "工作模型"] },
    { id: "api:demo:hidden", roles: ["工作模型"] },
    { id: "api:missing", roles: ["轻量模型"] },
  ]);
  assert.deepEqual(fusionSelectedModels("devin:worker", ["devin:worker"], "devin:worker"), [
    { id: "devin:worker", roles: ["主模型", "工作模型", "轻量模型"] },
  ]);
});

test("live settings hydrate and explicitly clear Fusion effort overrides, including older senders", () => {
  const initial = emptyLiveData();
  assert.deepEqual(initial.fusionReasoning, {});
  const levels = { "chatgpt:visible": "high", "devin:worker": "medium", "api:demo:hidden": "off" };
  const updated = { ...initial, ...applyEvent(initial, "settings", { settings: { fusionReasoning: levels } }).patch };
  assert.deepEqual(updated.fusionReasoning, levels);
  assert.deepEqual(applyEvent(updated, "settings", { settings: { fusionReasoning: {} } }).patch.fusionReasoning, {});
  assert.deepEqual(applyEvent(updated, "settings", { settings: {} }).patch.fusionReasoning, {});
});
