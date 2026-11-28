import assert from "node:assert/strict";
import test from "node:test";
import { applyEvent, emptyLiveData, upsertTurns } from "../src/liveReducer.ts";
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

test("tail and activity patches preserve unchanged objects, remove fields, and reorder only when requested", () => {
  const oldThinking = { kind: "thinking" as const, id: "think", text: "kept", complete: false };
  const t: Turn = { id: "turn", status: "running", prompts: [], blocks: [
    { id: "answer", kind: "text", markdown: "🙂", streaming: true },
    { id: "activity", kind: "activity", entries: [oldThinking, { ...oldThinking, id: "end" }] },
  ] };
  const after = upsertTurns([t], [{ ...t, blocks: [
    { kind: "textPatch", id: "answer", suffix: "🚀", remove: ["streaming"] },
    { kind: "activityPatch", id: "activity", entries: [{ kind: "entryPatch", id: "end", suffix: "!", fields: { complete: true } }], order: ["end", "think"] },
  ] }]);
  assert.deepEqual(after[0]!.blocks[0], { kind: "text", id: "answer", markdown: "🙂🚀" });
  const entries = after[0]!.blocks[1]!.entries;
  assert.strictEqual(entries[1], oldThinking);
  assert.deepEqual(entries[0], { ...oldThinking, id: "end", text: "kept!", complete: true });
});

test("a history page prepends without overwriting streamed turns and cannot cross a replacement", () => {
  let state = { ...emptyLiveData(), tabs: ["c1"] };
  const turn = (id: string): Turn => ({ id, status: "done", prompts: [], blocks: [] });
  const emit = (payload: object) => { state = { ...state, ...applyEvent(state, "turns", { id: "c1", ...payload }).patch }; };
  emit({ all: true, turns: [turn("new")], history: { generation: 5, more: true } });
  const current = state.sessions.c1!.turns[0];
  emit({ prepend: true, turns: [turn("wrong")], history: { generation: 4, more: true } });
  emit({ prepend: true, turns: [turn("older"), turn("new")], history: { generation: 5, more: false } });
  assert.deepEqual(state.sessions.c1!.turns.map((t) => t.id), ["older", "new"]);
  assert.strictEqual(state.sessions.c1!.turns[1], current);
  assert.equal(state.sessions.c1!.turnHistory!.more, false);
  emit({ all: false, trim: 1, turns: [turn("latest")], history: { generation: 5, more: true } });
  assert.deepEqual(state.sessions.c1!.turns.map((t) => t.id), ["new", "latest"]);
  assert.strictEqual(state.sessions.c1!.turns[0], current);
});
