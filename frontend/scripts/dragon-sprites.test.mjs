import assert from "node:assert/strict";
import test from "node:test";
import { DRAGON_SHAPES, DRAGON_SIZE, dragonFrames, dragonPalette } from "../src/lib/dragonSprites.ts";

const ticks = { pending: 6, working: 6, waiting: 6, done: 8, stopped: 1, failed: 1 };

test("every base and composed dragon frame is a 16×16 palette map", () => {
  function check(frame) {
    assert.equal(frame.length, DRAGON_SIZE);
    for (const row of frame) {
      assert.equal(row.length, DRAGON_SIZE, row);
      assert.match(row, /^[.KBLA]+$/);
    }
  }
  for (const [identity, poses] of Object.entries(DRAGON_SHAPES)) {
    for (const frame of Object.values(poses)) check(frame);
    for (const [status, count] of Object.entries(ticks)) {
      const frames = dragonFrames(identity, status);
      assert.equal(frames.length, count, `${identity}/${status}`);
      frames.forEach(check);
    }
  }
});

test("the mirrored worker stands facing left, including the open eye", () => {
  assert.deepEqual(dragonFrames("worker", "pending")[0], [
    "...........KK...",
    "...KKKKK..KLK...",
    "..KBBBBBKKLK....",
    ".KBBBBBBBLK.....",
    ".KBBKLLBBBK.....",
    "KBBBKLLBBBK.....",
    "KBBBBBBBBBK.....",
    ".KKKKKBBBBK.....",
    "..KBBBBBBKK.....",
    "...KKLLLBBBK....",
    "...KKLLLBBBBK.K.",
    "...KBLLLBBBBKKBK",
    "...KKLLLBBBBKBBK",
    "....KLLLBBBBBBK.",
    "....KBBKKBBKKK..",
    "...KKKK.KKKK....",
  ]);
});

test("status frames keep their poses and eyes; right-hand glyphs only use transparent body pixels", () => {
  const alert = [".AA", ".AA", ".AA", ".AA", "...", ".AA"];
  const sleep = ["AAA", ".A.", "AAA"];
  const fail = ["A.A", ".A.", "A.A"];
  const sequences = {
    pending: [["stand", "KLL", "KLL", 5], ["stand", "BBB", "KKK", 1]],
    working: [...Array.from({ length: 5 }, (_, i) => [i % 2 === 0 ? "stand" : "bob", "KLL", "KLL", 1]), ["bob", "BBB", "KKK", 1]],
    waiting: [["stand", "KLL", "KLL", 3, 2, alert], ["bob", "KLL", "KLL", 3]],
    done: [["stand", "BBB", "KKK", 4, 5, sleep], ["bob", "BBB", "KKK", 4, 2, sleep]],
    stopped: [["stand", "BBB", "KKK", 1]],
    failed: [["stand", "KBK", "BKB", 1, 4, fail]],
  };
  for (const [identity, poses] of Object.entries(DRAGON_SHAPES)) {
    for (const [status, sequence] of Object.entries(sequences)) {
      const frames = dragonFrames(identity, status);
      let index = 0;
      for (const [pose, eyeTop, eyeBottom, count, glyphTop = 0, glyph = []] of sequence) {
        const body = poses[pose];
        const expected = [...body];
        const eye = pose === "stand" ? 4 : 5;
        expected[eye] = body[eye].slice(0, 4) + eyeTop + body[eye].slice(7);
        expected[eye + 1] = body[eye + 1].slice(0, 4) + eyeBottom + body[eye + 1].slice(7);
        glyph.forEach((pixels, row) => {
          const y = glyphTop + row;
          const bodyPixels = [...expected[y]];
          [...pixels].forEach((pixel, col) => {
            if (pixel !== "A") return;
            const x = 13 + col;
            assert.equal(body[y][x], ".", `${identity}/${status} glyph overlaps body at ${x},${y}`);
            bodyPixels[x] = pixel;
          });
          expected[y] = bodyPixels.join("");
        });
        for (let i = 0; i < count; i++) assert.deepEqual(frames[index++], expected, `${identity}/${status} tick ${index}`);
      }
    }
  }
});

test("an expert id keeps the same identity-specific palette", () => {
  assert.equal(dragonPalette("worker", "expert-0"), "green");
  assert.equal(dragonPalette("light", "expert-0"), "blue");
  assert.equal(dragonPalette("main", "expert-0"), "main");
  assert.notEqual(dragonPalette("worker", "expert-1"), dragonPalette("worker", "expert-0"));
  assert.equal(dragonPalette("worker", "expert-0"), "green", "looking up another expert does not change the first colour");
});
