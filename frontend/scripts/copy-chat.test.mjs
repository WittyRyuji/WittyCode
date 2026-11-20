import assert from "node:assert/strict";
import test from "node:test";
import { appendChatQuestion } from "../src/lib/copyChat.ts";

const ask = "这是什么意思，解释一下：";

test("Ask it inserts the captured text verbatim into an empty draft", () => {
  const markdown = "\n  **Title**\n\n```text\ncode\n```\n";
  assert.equal(appendChatQuestion("", markdown), ask + markdown);
});

test("Ask it preserves the existing draft and only adds a missing newline", () => {
  assert.equal(appendChatQuestion("draft  ", "part"), `draft  \n${ask}part`);
  for (const draft of ["draft\n", "draft\r\n", "draft\n\n", "\n"]) {
    assert.equal(appendChatQuestion(draft, "part"), draft + ask + "part");
  }
});

test("repeated Ask it operations append to the latest draft", () => {
  const first = appendChatQuestion("draft", "part");
  assert.equal(appendChatQuestion(first + " with a late edit", "**full**"),
    `draft\n${ask}part with a late edit\n${ask}**full**`);
});
