// StorageSettings.tsx's exported pure helpers, run as they are written (the .tsx cannot be
// imported directly: Node strips types from .ts only, not JSX).
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";

const src = readFileSync(new URL("../src/components/StorageSettings.tsx", import.meta.url), "utf8");
function pure(name) {
  const m = src.match(new RegExp(`export function ${name}\\b[\\s\\S]*?\\n}`, "m"));
  assert.ok(m, `StorageSettings.tsx does not export ${name}`);
  let code = m[0].replace("export function", "function");
  // Strip the signature's type annotations; helper signatures use no parenthesised types on purpose.
  const brace = code.indexOf("{");
  const sig = code.slice(0, brace).replace(/: [^,()]+(?=[,)])/g, "").replace(/(\))\s*:[\s\S]+$/, "$1");
  code = sig + code.slice(brace);
  // Module constants the helper closes over, taken from the source so they cannot drift.
  const env = [...src.matchAll(/^const (GIB|SHARE_SCALE) = [^;]+;/gm)].map((c) => c[0]).join("\n");
  return new Function(`${env}\n${code}; return ${name};`)();
}
const formatBytes = pure("formatBytes");
const shareValue = pure("shareValue");
const sharesComplete = pure("sharesComplete");
const sharesTotal = pure("sharesTotal");

test("byte sizes carry binary unit names, not decimal ones", () => {
  assert.equal(formatBytes(8 * 1024 ** 3), "8.0 GiB");
  assert.equal(formatBytes(1.5 * 1024 ** 3), "1.5 GiB");
  assert.equal(formatBytes(40 * 1024 ** 2), "40.0 MiB");
  assert.equal(formatBytes(36 * 1024), "36.0 KiB");
  assert.equal(formatBytes(512), "512 B");
  for (const text of [formatBytes(1024 ** 3), formatBytes(1024 ** 2), formatBytes(1024)]) {
    assert.ok(!text.endsWith(" GB") && !text.endsWith(" MB") && !text.endsWith(" KB"), `${text} must not claim decimal units`);
  }
});

test("a share input is a 0-100 percent or nothing", () => {
  assert.equal(shareValue("60"), 60);
  assert.equal(shareValue("33.3"), 33.3);
  assert.equal(shareValue("0"), 0);
  assert.equal(shareValue(""), null);
  assert.equal(shareValue("abc"), null);
  assert.equal(shareValue("-1"), null);
  assert.equal(shareValue("101"), null);
});

test("the split may be saved only at exactly 100", () => {
  assert.ok(sharesComplete([60, 15, 15, 10]));
  assert.ok(sharesComplete([33.3, 33.3, 33.4, 0]));
  assert.ok(!sharesComplete([59.5, 15, 15, 10]), "99.5% is not 100%");
  assert.ok(!sharesComplete([60, 15, 15, 11]));
  assert.ok(!sharesComplete([60, 15, 15, null]), "an unusable field blocks the save");
});

test("the running total reports real hundredths", () => {
  assert.equal(sharesTotal([60, 15, 15, 10]), 100);
  assert.equal(sharesTotal([33.35, 33.35, 33.3, 0]), 100);
  assert.equal(sharesTotal([59.5, 15, 15, 10]), 99.5);
  assert.equal(sharesTotal([60, null, 15, 10]), 85, "an empty field adds nothing, never silently 100");
});

test("the page states only the cleanup that really exists", () => {
  assert.ok(src.includes("自动清理最老的合格对象"), "the autoclean choice names the real behaviour");
  assert.ok(src.includes("只有转储的清理已实现"), "the hint scopes the working cleanup to dumps");
  assert.ok(src.includes('c.id === "dump"'), "dump is the implemented category");
  assert.ok(src.includes("disabled={!implemented}"), "unimplemented categories stay disabled, not toggleable");
  assert.ok(src.includes("（清理未实现）"), "unimplemented rows are marked in place");
  assert.ok(src.includes("拒写"), "the cap's actual behaviour — refuse writes — is stated");
  assert.ok(!src.includes("都可以自动清理") && !src.includes("（未实现）`}"), "no blanket promise remains");
});
