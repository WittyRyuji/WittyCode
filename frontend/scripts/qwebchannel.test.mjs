import assert from "node:assert/strict";
import test from "node:test";
import { resolve } from "node:path";
import { qtWebChannelClient } from "./fake-backend.mjs";

test("CI checks never silently reuse a warm Qt cache or fall back to a different QWebChannel client", () => {
  const python = process.env.WITTYCODE_TEST_PYTHON;
  const required = process.env.WITTYCODE_REQUIRE_QT;
  try {
    process.env.WITTYCODE_TEST_PYTHON = resolve("shots/no-interpreter/python.exe");
    process.env.WITTYCODE_REQUIRE_QT = "1";
    assert.throws(() => qtWebChannelClient(), /CI parity requires/);
    delete process.env.WITTYCODE_REQUIRE_QT;
    assert.equal(qtWebChannelClient(), null, "without Qt, an explicitly non-CI design check may use its stand-in");
  } finally {
    if (python === undefined) delete process.env.WITTYCODE_TEST_PYTHON; else process.env.WITTYCODE_TEST_PYTHON = python;
    if (required === undefined) delete process.env.WITTYCODE_REQUIRE_QT; else process.env.WITTYCODE_REQUIRE_QT = required;
  }
});
