import assert from "node:assert/strict";
import test from "node:test";
import { fusionChoices, fusionDisabledReason } from "../src/components/fusionModels.ts";

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
