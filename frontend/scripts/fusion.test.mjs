import assert from "node:assert/strict";
import test from "node:test";
import { fusionChoices, fusionDisabledReason, fusionSelectedModels } from "../src/components/fusionModels.ts";
import { applyEvent, emptyLiveData } from "../src/liveReducer.ts";
import { buildModelMenu } from "../src/components/modelMenu.ts";

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

test("Fusion is the first composer model choice and the only checked item when selected", () => {
  const picked = [];
  const input = {
    models, providers: [], current: "chatgpt:visible", hidden: [],
    devin: { enabled: true, auth: "signed-in" },
    chatgpt: { available: true, enabled: true, auth: "signed-in", planEnabled: true },
    onPick: (id) => picked.push(id), onManage: () => {},
    fusion: { selected: true, description: "主模型：Visible", onSelect: () => picked.push("fusion") },
  };
  const menu = buildModelMenu(input);
  assert.equal(menu[0].id, "fusion");
  assert.equal(menu[0].label, "Fusion");
  assert.equal(menu[0].description, "主模型：Visible");
  assert.equal(menu[1].separator, true);
  assert.equal(menu[2].id, "h-chatgpt");
  assert.deepEqual(menu.filter((item) => item.checked).map((item) => item.id), ["fusion"]);
  menu[0].onSelect();
  menu.find((item) => item.id === "chatgpt:visible").onSelect();
  assert.deepEqual(picked, ["fusion", "chatgpt:visible"]);
  const normal = buildModelMenu({ ...input, fusion: { ...input.fusion, selected: false } });
  assert.deepEqual(normal.filter((item) => item.checked).map((item) => item.id), ["chatgpt:visible"]);
  const existing = buildModelMenu({ ...input, fusion: { ...input.fusion, disabled: true }, engine: "chatgpt" });
  assert.equal(existing[0].disabled, true);
  const settings = buildModelMenu({ ...input, fusion: undefined });
  assert.equal(settings[0].id, "h-chatgpt");
  assert(!settings.some((item) => item.id === "fusion"));
});
