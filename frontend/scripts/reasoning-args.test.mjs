import assert from "node:assert/strict";
import test from "node:test";
import { hasAdvancedProviderOptions, reasoningArgsProblem } from "../src/lib/reasoningArgs.ts";

test("per-effort body overrides validate the effort map but preserve nested null values", () => {
  for (const text of [undefined, "", "{}", '{"off":{"thinking":{"type":"disabled"},"include":null}}', '{"low":{"output_config":{"effort":"low"}}}']) {
    assert.equal(reasoningArgsProblem(text), "");
  }
  for (const text of ["{", "null", "[]", "42", '{"unknown":{}}', '{"off":null}', '{"off":[]}', '{"high":false}', '{"low":{"value":1e999}}']) {
    assert.match(reasoningArgsProblem(text), /思考档位参数/);
  }
});

test("saved and mixed effort mappings open the advanced section without changing old defaults", () => {
  const plain = { headers: [], extraArgs: "" };
  assert.equal(hasAdvancedProviderOptions(undefined), false);
  assert.equal(hasAdvancedProviderOptions(plain), false);
  for (const extra of [{ headers: [{}] }, { extraArgs: "{}" }, { reasoningArgs: '{"off":{}}' }, { reasoningArgsMixed: true }]) {
    assert.equal(hasAdvancedProviderOptions({ ...plain, ...extra }), true);
  }
});
