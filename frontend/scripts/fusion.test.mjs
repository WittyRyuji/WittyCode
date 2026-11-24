import assert from "node:assert/strict";
import test from "node:test";
import { fusionChoices, fusionDisabledReason } from "../src/components/fusionModels.ts";
import { applyEvent, emptyLiveData } from "../src/liveReducer.ts";
import { buildModelMenu } from "../src/components/modelMenu.ts";
import { fusionEffort, withFusionEffort } from "../src/lib/fusionReasoning.ts";
import { toolLabel } from "../src/lib/format.ts";

test("the agent tool uses the same Sub-Agent name as the team inspector", () => {
  assert.equal(toolLabel("Task"), "Sub-Agent");
});

const models = [
  { id: "chatgpt:visible", label: "Visible", source: "chatgpt", fusionBackend: "plan" },
  { id: "api:demo:hidden", label: "Hidden", source: "api:demo", fusionBackend: "api" },
  { id: "new:guarded", label: "Guarded", source: "new", fusionBackend: "new" },
  { id: "chatgpt:standalone", label: "Not a seat", source: "chatgpt" },
];

test("Fusion choices follow catalog capabilities and honour hidden models", () => {
  assert.deepEqual(fusionChoices(models, [models[1].id]), [models[0], models[2]]);
  assert.deepEqual(fusionChoices(models, [models[2].id]), [models[0], models[1]]);
  assert.equal(fusionDisabledReason(models[2], true), "");
  assert.equal(fusionDisabledReason({ ...models[2], disabledReason: "Unavailable" }), "Unavailable");
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

test("timeout hydrates both fields, preserving explicit legacy seconds", () => {
  const initial = emptyLiveData();
  const view = (settings) => applyEvent(initial, "settings", { settings }).patch;
  assert.equal(initial.fusionMaxSecondsAdaptive, true);
  assert.equal(view({}).fusionMaxSecondsAdaptive, true);
  assert.equal(view({ fusionMaxSeconds: 7 }).fusionMaxSecondsAdaptive, false);
  const remembered = view({ fusionMaxSeconds: 7, fusionMaxSecondsAdaptive: true });
  assert.equal(remembered.fusionMaxSecondsAdaptive, true);
  assert.equal(remembered.fusionMaxSeconds, 7);
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
    models: [...models, { id: "devin:worker", label: "Worker", source: "devin" }], providers: [], current: "chatgpt:visible", hidden: [],
    devin: { enabled: true, auth: "signed-in" },
    chatgpt: { available: true, enabled: true, auth: "signed-in", planEnabled: true },
    onPick: (id) => picked.push(id), onManage: () => {},
    fusion: { selected: true, onSelect: () => picked.push("fusion"), onConfigure: () => picked.push("settings:fusion") },
  };
  const menu = buildModelMenu(input);
  assert.deepEqual(menu[0], { id: "h-wittycode", heading: true, label: "WittyCode" });
  const fusion = menu[1];
  assert.equal(fusion.id, "fusion");
  assert.equal(fusion.label, "Fusion");
  assert.equal(fusion.description, undefined);
  assert.equal(fusion.configure.label, "Fusion 设置");
  fusion.configure.onSelect();
  assert.deepEqual(picked.splice(0), ["settings:fusion"]);
  assert(!menu.some((item) => item.id === "sep-fusion"));
  assert.equal(menu[2].id, "h-chatgpt");
  assert.deepEqual(menu.filter((item) => item.checked).map((item) => item.id), ["fusion"]);
  fusion.onSelect();
  menu.find((item) => item.id === "chatgpt:visible").onSelect();
  assert.deepEqual(picked, ["fusion", "chatgpt:visible"]);
  const normal = buildModelMenu({ ...input, fusion: { ...input.fusion, selected: false } });
  assert.deepEqual(normal.filter((item) => item.checked).map((item) => item.id), ["chatgpt:visible"]);
  const existing = buildModelMenu({ ...input, fusion: { ...input.fusion, disabled: false }, engine: "chatgpt", running: false, fresh: false });
  assert.equal(existing.find((item) => item.id === "fusion").disabled, false);
  assert.equal(existing.find((item) => item.id === "chatgpt:visible").description, "退出 Fusion 并使用此普通模型");
  const busy = buildModelMenu({ ...input, fusion: { ...input.fusion, disabled: true }, engine: "chatgpt", running: true });
  const busyFusion = busy.find((item) => item.id === "fusion");
  assert.equal(busyFusion.disabled, true);
  busyFusion.configure.onSelect();
  assert.equal(picked.at(-1), "settings:fusion", "the gear remains independent of the busy model row");
  assert(busy.find((item) => item.id === "chatgpt:visible").disabled);
  assert(busy.find((item) => item.id === "devin:worker").disabled);
  const settings = buildModelMenu({ ...input, fusion: undefined });
  assert.equal(settings[0].id, "h-chatgpt");
  assert(!settings.some((item) => item.id === "fusion"));
});
