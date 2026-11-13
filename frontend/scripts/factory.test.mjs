import assert from "node:assert/strict";
import test from "node:test";
import { engineLabel, engineOfSource, sourceLabel, sourceTag } from "../src/lib/models.ts";
import { permissionsFor, startingLevel } from "../src/lib/vocab.ts";
import { buildModelMenu } from "../src/components/modelMenu.ts";

const model = { id: "factory:demo-model", label: "Factory Demo", source: "factory", efforts: ["low", "medium"], defaultEffort: "medium", window: 0 };
const input = { models: [model], providers: [], devin: { cli: "missing", auth: "signed-out", enabled: false }, chatgpt: { auth: "signed-out", available: false, enabled: false, accounts: [] }, current: model.id, onPick() {}, onManage() {} };

test("optional Factory models are never routed into WittyHarness or an API provider", () => {
  assert.equal(engineOfSource("factory"), "factory");
  assert.equal(engineLabel("factory"), "Factory");
  assert.equal(sourceLabel("factory"), "Factory");
  assert.equal(sourceTag("factory"), "订阅");
  assert.equal(startingLevel("factory"), "off");
  assert.deepEqual(permissionsFor("factory").map((p) => p.value), ["off", "low", "medium", "high"]);
  const menu = buildModelMenu(input);
  assert(menu.some((i) => i.heading && i.label === "FACTORY"));
  assert(!menu.find((i) => i.id === model.id).disabled);
  assert(buildModelMenu({ ...input, engine: "chatgpt", running: true }).find((i) => i.id === model.id).disabled);
  assert(!buildModelMenu({ ...input, engine: "factory", running: true }).find((i) => i.id === model.id).disabled);
});
