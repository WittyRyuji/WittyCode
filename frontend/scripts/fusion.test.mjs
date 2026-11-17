import assert from "node:assert/strict";
import test from "node:test";
import { fusionChoices, fusionDisabledReason } from "../src/components/fusionModels.ts";
import { applyEvent, emptyLiveData } from "../src/liveReducer.ts";
import { buildModelMenu } from "../src/components/modelMenu.ts";
import { fusionEffort, withFusionEffort } from "../src/lib/fusionReasoning.ts";

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

test("live settings hydrate and explicitly clear Fusion effort overrides, including older senders", () => {
  const initial = emptyLiveData();
  assert.deepEqual(initial.fusionReasoning, {});
  const levels = { "chatgpt:visible": "high", "devin:worker": "medium", "api:demo:hidden": "off" };
  const updated = { ...initial, ...applyEvent(initial, "settings", { settings: { fusionReasoning: levels } }).patch };
  assert.deepEqual(updated.fusionReasoning, levels);
  assert.deepEqual(applyEvent(updated, "settings", { settings: { fusionReasoning: {} } }).patch.fusionReasoning, {});
  assert.deepEqual(applyEvent(updated, "settings", { settings: {} }).patch.fusionReasoning, {});
});

test("Fusion efforts are independent by role and legacy defaults split without changing other roles", () => {
  const model = "chatgpt:visible";
  let values = { [model]: "high", "worker:api:demo:hidden": "off" };
  assert.equal(fusionEffort(values, "worker", model), "high");
  values = withFusionEffort(values, "light", model, "low");
  assert.equal(fusionEffort(values, "main", model), "high");
  assert.equal(fusionEffort(values, "worker", model), "high");
  assert.equal(fusionEffort(values, "light", model), "low");
  assert(!Object.hasOwn(values, model));
  values = withFusionEffort(values, "worker", model, "");
  assert.equal(fusionEffort(values, "worker", model), "");
  assert.equal(fusionEffort(values, "main", model), "high");
  assert.equal(fusionEffort(values, "light", model), "low");
  assert.equal(values["worker:api:demo:hidden"], "off");
  assert.equal(fusionEffort(values, "worker", "chatgpt:other"), "");
  const explicit = withFusionEffort({ [model]: "high", [`main:${model}`]: "medium" }, "worker", model, "low");
  assert.equal(fusionEffort(explicit, "main", model), "medium");
});

test("Fusion is the first composer model choice and the only checked item when selected", () => {
  const picked = [];
  const input = {
    models, providers: [], current: "chatgpt:visible", hidden: [],
    devin: { enabled: true, auth: "signed-in" },
    chatgpt: { available: true, enabled: true, auth: "signed-in", planEnabled: true },
    onPick: (id) => picked.push(id), onManage: () => {},
    fusion: { selected: true, onSelect: () => picked.push("fusion"), onConfigure: () => picked.push("settings:fusion") },
  };
  const menu = buildModelMenu(input);
  assert.equal(menu[0].id, "fusion");
  assert.equal(menu[0].label, "Fusion");
  assert.equal(menu[0].description, undefined);
  assert.equal(menu[0].configure.label, "Fusion 设置");
  menu[0].configure.onSelect();
  assert.deepEqual(picked.splice(0), ["settings:fusion"]);
  assert.equal(menu[1].separator, true);
  assert.equal(menu[2].id, "h-chatgpt");
  assert.deepEqual(menu.filter((item) => item.checked).map((item) => item.id), ["fusion"]);
  menu[0].onSelect();
  menu.find((item) => item.id === "chatgpt:visible").onSelect();
  assert.deepEqual(picked, ["fusion", "chatgpt:visible"]);
  const normal = buildModelMenu({ ...input, fusion: { ...input.fusion, selected: false } });
  assert.deepEqual(normal.filter((item) => item.checked).map((item) => item.id), ["chatgpt:visible"]);
  const existing = buildModelMenu({ ...input, fusion: { ...input.fusion, disabled: false }, engine: "chatgpt", running: false, fresh: false });
  assert.equal(existing[0].disabled, false);
  assert.equal(existing.find((item) => item.id === "chatgpt:visible").description, "退出 Fusion 并使用此普通模型");
  const busy = buildModelMenu({ ...input, fusion: { ...input.fusion, disabled: true }, engine: "chatgpt", running: true });
  assert.equal(busy[0].disabled, true);
  assert(busy.find((item) => item.id === "chatgpt:visible").disabled);
  assert(busy.find((item) => item.id === "devin:worker").disabled);
  const settings = buildModelMenu({ ...input, fusion: undefined });
  assert.equal(settings[0].id, "h-chatgpt");
  assert(!settings.some((item) => item.id === "fusion"));
});
