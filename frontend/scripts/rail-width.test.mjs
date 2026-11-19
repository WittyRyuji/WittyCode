import assert from "node:assert/strict";
import test from "node:test";
import { railWidth, railLimit } from "../src/lib/railWidth.ts";

test("right rail clamps dragging while keeping a usable conversation and narrow overlay", () => {
  assert.equal(railWidth(900, 1200), 600);
  assert.equal(railWidth(80, 1200), 240);
  assert.equal(railWidth(600, 1000), 400);
  assert.equal(railLimit(900), 600);
  assert.equal(railWidth(500, 520), 464);
  assert.equal(railWidth(350.6, 1200), 351);
  assert.equal(railWidth(600, 1600), 600);
});
