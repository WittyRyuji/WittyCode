import assert from "node:assert/strict";
import test from "node:test";
import { INSPECTOR_MIN, inspectorLimit, inspectorWidth } from "../src/lib/inspectorWidth.ts";
import { looksLikePath } from "../src/lib/pathref.ts";

test("The inspection column defaults to 42%, preserves 360px for the conversation, and clamps saved widths", () => {
  assert.equal(inspectorWidth(0, 1440, 288), 605);
  assert.equal(inspectorWidth(0, 1120, 440), INSPECTOR_MIN);
  assert.equal(inspectorWidth(450, 1440, 288), 450);
  assert.equal(inspectorWidth(339, 1440, 288), INSPECTOR_MIN);
  assert.equal(inspectorLimit(1440, 288), 792);
  assert.equal(inspectorWidth(780, 1200, 288), 552);
  assert.equal(inspectorLimit(9000, 0), 4000);
});

test("Bare picture and video names mentioned by the assistant become file links", () => {
  for (const file of ["a.GIF", "a.apng", "a.avif", "a.tiff", "a.heic", "a.mp4", "a.webm", "a.m2ts"]) assert.ok(looksLikePath(file), file);
});
