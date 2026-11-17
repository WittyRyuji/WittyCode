import assert from "node:assert/strict";
import test from "node:test";
import { upsertTurns } from "../src/liveReducer.ts";
import type { Turn } from "../src/types.ts";

test("live deltas preserve full unchanged evidence and replace changed blocks", () => {
  const evidence = { id: "evidence", kind: "text" as const, markdown: "EVIDENCE".repeat(10000) };
  const before: Turn = { id: "turn", status: "running", prompts: [], blocks: [evidence, { id: "answer", kind: "text", markdown: "a" }] };
  const after = upsertTurns([before], [{ ...before, blocks: [{ id: "evidence", kind: "unchanged" }, { id: "answer", kind: "text", markdown: "ab" }] }]);
  assert.strictEqual(after[0]!.blocks[0], evidence);
  assert.equal(after[0]!.blocks[1]!.markdown, "ab");
  assert.equal(before.blocks[1]!.markdown, "a");
  assert.strictEqual(upsertTurns([], after)[0], after[0]);
});
