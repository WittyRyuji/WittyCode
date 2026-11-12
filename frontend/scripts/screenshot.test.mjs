import assert from "node:assert/strict";
import test from "node:test";
import { existsSync, mkdtempSync, readFileSync, rmSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { writeScreenshot } from "./cdp.mjs";

const bytes = Buffer.from([137, 80, 78, 71, 13, 10, 26, 10, 0, 255, 128, 0]);

function temporary(check) {
  const folder = mkdtempSync(join(tmpdir(), "wc-shot-test-"));
  try {
    check(folder);
  } finally {
    rmSync(folder, { recursive: true, force: true });
  }
}

test("screenshots create missing parent directories in a clean checkout", () => temporary((folder) => {
  const file = join(folder, "shots", "nested", "image.png");
  assert.equal(existsSync(join(folder, "shots")), false);
  writeScreenshot(file, bytes.toString("base64"));
  assert.deepEqual(readFileSync(file), bytes);
}));

test("screenshots still overwrite an existing file without changing the PNG bytes", () => temporary((folder) => {
  const file = join(folder, "image.png");
  writeFileSync(file, "old screenshot");
  writeScreenshot(file, bytes.toString("base64"));
  assert.deepEqual(readFileSync(file), bytes);
}));

test("screenshots report real filesystem failures and leave a blocking file intact", () => temporary((folder) => {
  const blocker = join(folder, "not-a-folder");
  writeFileSync(blocker, "keep this file");
  assert.throws(() => writeScreenshot(join(blocker, "image.png"), bytes.toString("base64")));
  assert.equal(readFileSync(blocker, "utf8"), "keep this file");
}));
