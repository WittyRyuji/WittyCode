// From frontend/: node scripts/preview-dragon-gifs.mjs
import { mkdirSync, writeFileSync } from "node:fs";
import { resolve } from "node:path";
import { DRAGON_FPS, DRAGON_SIZE, dragonColours, dragonFrames } from "../src/lib/dragonSprites.ts";

const output = resolve("../outputs/agents-flow-preview");
mkdirSync(output, { recursive: true });
const statuses = ["pending", "working", "waiting", "done", "stopped", "failed"];
const variants = [
  ["main", "main"], ["worker", "green"], ["worker", "purple"], ["worker", "orange"],
  ["worker", "teal"], ["light", "blue"], ["light", "pink"],
];
const first = [variants[0], variants[1], variants[5]];

/** GIF LZW. Clear before the 9-bit dictionary fills, keeping the bit width fixed. */
function lzw(pixels) {
  const clear = 256, end = 257;
  const dict = new Map();
  let next = 258, hold = 0, bits = 0;
  const bytes = [];
  const write = (code) => {
    hold |= code << bits;
    bits += 9;
    while (bits >= 8) {
      bytes.push(hold & 255);
      hold >>= 8;
      bits -= 8;
    }
  };
  write(clear);
  let prefix = pixels[0];
  for (let i = 1; i < pixels.length; i++) {
    const pixel = pixels[i];
    const pair = (prefix << 8) | pixel;
    const found = dict.get(pair);
    if (found !== undefined) {
      prefix = found;
    } else {
      write(prefix);
      if (next === 511) {
        write(clear);
        dict.clear();
        next = 258;
      } else dict.set(pair, next++);
      prefix = pixel;
    }
  }
  write(prefix);
  write(end);
  if (bits) bytes.push(hold & 255);
  return Buffer.from(bytes);
}

const word = (n) => [n & 255, n >> 8];
function gif(file, width, height, colours, frames, transparent) {
  const table = Buffer.alloc(256 * 3);
  colours.forEach((hex, i) => { if (hex) Buffer.from(hex.slice(1), "hex").copy(table, i * 3); });
  const chunks = [Buffer.from("GIF89a"), Buffer.from([...word(width), ...word(height), 0xf7, 0, 0]), table,
    Buffer.from([0x21, 0xff, 11]), Buffer.from("NETSCAPE2.0"), Buffer.from([3, 1, 0, 0, 0])];
  let tick = 0;
  for (const pixels of frames) {
    const delay = Math.round((tick + 1) * 100 / DRAGON_FPS) - Math.round(tick++ * 100 / DRAGON_FPS);
    // Full frames; clear each transparent frame back to the transparent canvas before drawing the next.
    chunks.push(Buffer.from([0x21, 0xf9, 4, transparent ? 9 : 4, ...word(delay), 0, 0]));
    chunks.push(Buffer.from([0x2c, 0, 0, 0, 0, ...word(width), ...word(height), 0, 8]));
    const compressed = lzw(pixels);
    for (let at = 0; at < compressed.length; at += 255) {
      const block = compressed.subarray(at, at + 255);
      chunks.push(Buffer.from([block.length]), block);
    }
    chunks.push(Buffer.from([0]));
  }
  chunks.push(Buffer.from([0x3b]));
  writeFileSync(resolve(output, file), Buffer.concat(chunks));
}

function render(file, rows, columns, scale, padding, ticks, background) {
  const cell = (DRAGON_SIZE * scale) + 2 * padding;
  const width = cell * columns.length, height = cell * rows.length;
  const cells = rows.flatMap(([identity, palette], y) => columns.map((status, x) => ({
    x: x * cell + padding, y: y * cell + padding, frames: dragonFrames(identity, status), colours: dragonColours(palette, status),
  })));
  const colours = [...new Set([background, ...cells.flatMap((c) => Object.values(c.colours))])];
  function* frames() {
    for (let tick = 0; tick < ticks; tick++) {
      const pixels = new Uint8Array(width * height);
      for (const tile of cells) {
        const frame = tile.frames[tick % tile.frames.length];
        frame.forEach((row, y) => [...row].forEach((c, x) => {
          if (c === ".") return;
          const index = colours.indexOf(tile.colours[c]);
          for (let dy = 0; dy < scale; dy++) {
            const at = (tile.y + y * scale + dy) * width + tile.x + x * scale;
            pixels.fill(index, at, at + scale);
          }
        }));
      }
      yield pixels;
    }
  }
  gif(file, width, height, colours, frames(), background === null);
}

render("sheet-light.gif", variants, statuses, 6, 12, 24, "#FFFFFF");
render("sheet-dark.gif", variants, statuses, 6, 12, 24, "#16181D");
const text = [];
for (const [identity, palette] of first) {
  for (const status of statuses) {
    const frames = dragonFrames(identity, status);
    render(`dragon-${identity}-${status}.gif`, [[identity, palette]], [status], 8, 0, frames.length, null);
    frames.forEach((frame, i) => text.push(`${identity} ${status} ${i + 1}/${frames.length}\n${frame.join("\n")}`));
  }
}
writeFileSync(resolve(output, "sprites.txt"), `${text.join("\n\n")}\n`);
console.log(`Wrote ${2 + first.length * statuses.length} GIFs and sprites.txt`);
