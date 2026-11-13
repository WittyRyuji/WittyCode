// Unit tests of the memory star map's layout (src/lib/starmap.ts): `node scripts/starmap.test.mjs`. Plain Node, no
// dependencies (Node 24 runs the .ts file as it is, it only strips the types); the sample notes come from
// scripts/starmap-preview.mjs, the same ones the preview and the interaction check draw.
import assert from "node:assert/strict";
import test from "node:test";
import {
  ALL_TITLES,
  GLOBAL_ID,
  MIN_GAP,
  ROOT_ID,
  arcChars,
  arcPath,
  brightness,
  clusterIdOf,
  colorKey,
  fitText,
  folderKey,
  hash32,
  kindIdOf,
  layoutStarMap,
  legendItems,
  mostlyWide,
  neighbourOf,
  noteTitle,
  ringScales,
  scopeIdOf,
  seeded,
  snippet,
  sourceKey,
  stackPositions,
  starColor,
  starRadius,
  textWidth,
} from "../src/lib/starmap.ts";
import { NOW, PROJECTS, SAMPLES, fewNotes, oddNotes, synthNotes } from "./starmap-preview.mjs";

const HOUR = 3_600_000;
const note = (id, extra = {}) => ({ id, title: id, text: "内容", scope: "global", project: "", projectName: "", kind: "fact", tags: [], created: NOW - HOUR, updated: NOW - HOUR, status: "active", rev: 1, source: { kind: "user" }, editor: { kind: "user" }, ...extra });
const inProject = (path, name, extra = {}) => ({ scope: "project", project: path, projectName: name, ...extra });

const stars = (layout) => layout.nodes.filter((n) => n.type === "note" || n.type === "cluster");
const ofType = (layout, type) => layout.nodes.filter((n) => n.type === type);

function closest(layout) {
  const list = stars(layout);
  let best = Infinity;
  let pair = null;
  for (let i = 0; i < list.length; i++) {
    for (let j = i + 1; j < list.length; j++) {
      const d = Math.hypot(list[i].x - list[j].x, list[i].y - list[j].y);
      if (d < best) {
        best = d;
        pair = [list[i].id, list[j].id];
      }
    }
  }
  return { distance: best, pair };
}

const boxOf = (l) => ({ x: l.anchor === "start" ? l.x : l.anchor === "end" ? l.x - l.width : l.x - l.width / 2, y: l.y - l.height / 2, w: l.width, h: l.height });
const meet = (a, b) => a.x < b.x + b.w - 0.01 && b.x < a.x + a.w - 0.01 && a.y < b.y + b.h - 0.01 && b.y < a.y + a.h - 0.01;
const finite = (layout) => layout.nodes.every((n) => [n.x, n.y, n.r, n.angle, n.brightness].every(Number.isFinite)) && Object.values(layout.bounds).every(Number.isFinite);

// ------------------------------------------------------------------------------------------------ determinism

test("the same notes always give the same chart, whatever order they arrive in", () => {
  const notes = SAMPLES.many();
  const a = layoutStarMap(notes);
  assert.deepEqual(layoutStarMap(notes), a);
  assert.deepEqual(layoutStarMap([...notes].reverse()), a, "reversed");
  const rnd = seeded(4);
  const shuffled = [...notes].sort(() => rnd() - 0.5);
  assert.deepEqual(layoutStarMap(shuffled), a, "shuffled");
  assert.deepEqual(layoutStarMap(notes, { collapsed: [GLOBAL_ID] }), layoutStarMap(notes, { collapsed: new Set([GLOBAL_ID]) }), "a set or a list of collapsed ids");
});

test("the scatter comes from the ids, not from chance", () => {
  assert.equal(hash32("m1"), hash32("m1"));
  assert.notEqual(hash32("m1"), hash32("m2"));
  const a = seeded(7);
  const b = seeded(7);
  assert.deepEqual([a(), a(), a()], [b(), b(), b()]);
  const one = layoutStarMap(synthNotes(40, { seed: 2 }));
  const renamed = layoutStarMap(synthNotes(40, { seed: 2 }).map((e) => ({ ...e, id: `x-${e.id}` })));
  assert.notDeepEqual(
    stars(one).map((n) => [n.x, n.y]),
    stars(renamed).map((n) => [n.x, n.y]),
    "other ids scatter differently",
  );
});

// ------------------------------------------------------------------------------------------- spacing and speed

test("no two stars are closer than MIN_GAP, from one note to 500, and in lopsided memories", () => {
  const cases = {
    one: [note("a")],
    two: [note("a"), note("b")],
    "two kinds": [note("a"), note("b", { kind: "pitfall" })],
    "25": SAMPLES.mid(),
    "100": synthNotes(100, { seed: 21 }),
    "300": SAMPLES.many(),
    "500": synthNotes(500, { seed: 3 }),
    "500 in one kind": Array.from({ length: 500 }, (_, i) => note(`k${i}`, { updated: NOW - i * HOUR })),
    "480 in one branch, 20 elsewhere": synthNotes(500, { seed: 8 }).map((e, i) => (i < 480 ? { ...e, ...inProject("D:\\big", "big"), kind: "fact" } : e)),
    "60 projects of one note": synthNotes(60, { seed: 5 }).map((e, i) => ({ ...e, ...inProject(`D:\\p${i}`, `project-${i}`) })),
    odd: oddNotes(),
  };
  for (const [name, notes] of Object.entries(cases)) {
    const layout = layoutStarMap(notes);
    assert.equal(stars(layout).length, new Set(notes.map((e) => e.id)).size, `${name}: one star per note`);
    const { distance, pair } = closest(layout);
    assert.ok(distance >= MIN_GAP - 1e-9, `${name}: ${pair} are ${distance.toFixed(2)} apart`);
    assert.ok(finite(layout), `${name}: finite numbers`);
  }
});

test("the minimum gap is an option, and folded branches keep it too", () => {
  const notes = synthNotes(200, { seed: 6 });
  const wide = layoutStarMap(notes, { minGap: 22 });
  assert.ok(closest(wide).distance >= 22 - 1e-9);
  const scope = scopeIdOf(notes.find((e) => e.scope === "project"));
  const folded = layoutStarMap(notes, { collapsed: [scope, kindIdOf(GLOBAL_ID, "fact")] });
  assert.ok(closest(folded).distance >= MIN_GAP - 1e-9);
});

test("500 notes are laid out in well under 30 ms", () => {
  const notes = synthNotes(500, { seed: 3 });
  for (let i = 0; i < 3; i++) layoutStarMap(notes);
  const times = [];
  for (let i = 0; i < 9; i++) {
    const t = performance.now();
    layoutStarMap(notes, { selectedId: notes[i].id });
    times.push(performance.now() - t);
  }
  times.sort((a, b) => a - b);
  const median = times[4];
  console.log(`  500 notes: median ${median.toFixed(2)} ms, slowest ${times[8].toFixed(2)} ms`);
  assert.ok(median < 30, `median ${median.toFixed(1)} ms`);
});

// ------------------------------------------------------------------------------------------------- the tree

test("the tree: root, then 全局 and one node per project folder, then the kinds, then the notes", () => {
  const notes = SAMPLES.many();
  const layout = layoutStarMap(notes);
  const byId = new Map(layout.nodes.map((n) => [n.id, n]));
  assert.equal(ofType(layout, "root").length, 1);
  assert.equal(layout.nodes[0].id, ROOT_ID);
  const scopes = ofType(layout, "scope");
  assert.deepEqual(new Set(scopes.map((s) => s.id)), new Set(notes.map(scopeIdOf)));
  assert.equal(scopes.length, 1 + PROJECTS.length, "全局 and the six folders (two of them called app)");
  assert.equal(scopes[0].id, GLOBAL_ID, "全局 comes first, at the top");
  assert.ok(Math.abs(scopes[0].angle + Math.PI / 2) < 1e-3);
  for (const s of scopes) {
    const kinds = new Set(notes.filter((e) => scopeIdOf(e) === s.id).map((e) => e.kind));
    assert.deepEqual(new Set(ofType(layout, "kind").filter((k) => k.parent === s.id).map((k) => k.kind)), kinds, `${s.id}: a kind node only where it has notes`);
  }
  for (const n of ofType(layout, "note")) {
    const kind = byId.get(n.parent);
    assert.equal(kind.type, "kind");
    assert.equal(kind.kind, n.entry.kind);
    assert.equal(byId.get(kind.parent).type, "scope");
    assert.equal(byId.get(kind.parent).parent, ROOT_ID);
    assert.equal(n.scope, kind.parent);
  }
  assert.equal(layout.edges.length, layout.nodes.length - 1, "one line from each node to its parent");
  assert.ok(layout.edges.every((e) => byId.get(e.to).parent === e.from));
});

test("two projects with the same name stay two nodes, told apart by their folder", () => {
  const layout = layoutStarMap([note("a", inProject("D:\\Work\\app", "app")), note("b", inProject("D:\\Old\\app", "app")), note("c", inProject("d:/work/app/", "app"))]);
  const scopes = ofType(layout, "scope");
  assert.equal(scopes.length, 2, "D:\\Work\\app and d:/work/app/ are one folder");
  assert.deepEqual(scopes.map((s) => s.label.text).sort(), ["app · Old", "app · Work"]);
  assert.ok(scopes.every((s) => s.name.includes("\\app") || s.name.includes("/app")), "the whole folder is in the name (the <title>)");
  assert.equal(folderKey("D:\\Work\\App\\"), folderKey("d:/work/app"));
  const same = layoutStarMap([note("a", inProject("D:\\a\\app", "app")), note("b", inProject("\\\\?\\app", "app"))]);
  assert.equal(new Set(ofType(same, "scope").map((s) => s.label.text)).size, 2, "without a parent folder to tell them apart, a number does");
});

test("a branch with 80 notes gets more of the circle than one with 2, and does not crowd it", () => {
  const big = Array.from({ length: 80 }, (_, i) => note(`b${i}`, { ...inProject("D:\\big", "big"), updated: NOW - i * HOUR }));
  const small = [note("s1", inProject("D:\\small", "small")), note("s2", inProject("D:\\small", "small"))];
  const layout = layoutStarMap([...big, ...small]);
  const spread = (scope) => {
    const angles = ofType(layout, "note").filter((n) => n.scope === scope).map((n) => n.angle);
    return Math.max(...angles) - Math.min(...angles);
  };
  assert.ok(spread(scopeIdOf(big[0])) > 5 * spread(scopeIdOf(small[0])));
  assert.ok(closest(layout).distance >= MIN_GAP);
  assert.ok(layout.rings.rows <= 2, "82 notes take two rows, not a deep spike");
  const two = ofType(layout, "note").filter((n) => n.scope === scopeIdOf(small[0]));
  assert.equal(two.length, 2);
  assert.ok(Math.hypot(two[0].x - two[1].x, two[0].y - two[1].y) >= MIN_GAP, "the small branch's two stars keep their distance");
});

test("collapsing a project folds its notes into one star with their count; opening it again restores the chart", () => {
  const notes = SAMPLES.mid();
  const open = layoutStarMap(notes);
  const scope = ofType(open, "scope").find((s) => s.id.startsWith("s:p:"));
  const count = notes.filter((e) => scopeIdOf(e) === scope.id).length;
  const folded = layoutStarMap(notes, { collapsed: [scope.id] });
  assert.equal(folded.nodes.filter((n) => n.scope === scope.id && (n.type === "note" || n.type === "kind")).length, 0);
  const cluster = folded.nodes.find((n) => n.id === clusterIdOf(scope.id));
  assert.equal(cluster.type, "cluster");
  assert.equal(cluster.count, count);
  assert.equal(cluster.branch, scope.id);
  assert.equal(cluster.parent, scope.id);
  assert.equal(cluster.label.text, `+${count}`);
  assert.equal(folded.nodes.find((n) => n.id === scope.id).collapsed, true);
  assert.equal(folded.hidden, count);
  assert.equal(folded.shown, notes.length - count);
  assert.equal(ofType(folded, "note").length, notes.length - count, "the other branches keep their stars");
  assert.deepEqual(layoutStarMap(notes, { collapsed: [] }), open);
  assert.deepEqual(layoutStarMap(notes, { collapsed: ["no such branch"] }), open, "an unknown id is ignored");
});

test("collapsing a kind folds only that kind", () => {
  const notes = SAMPLES.mid();
  const kind = ofType(layoutStarMap(notes), "kind")[0];
  const count = notes.filter((e) => kindIdOf(scopeIdOf(e), e.kind) === kind.id).length;
  const folded = layoutStarMap(notes, { collapsed: [kind.id] });
  assert.equal(ofType(folded, "note").filter((n) => n.parent === kind.id).length, 0);
  const cluster = folded.nodes.find((n) => n.id === clusterIdOf(kind.id));
  assert.equal(cluster.count, count);
  assert.equal(cluster.parent, kind.id);
  assert.equal(cluster.kind, kind.kind);
  assert.equal(ofType(folded, "kind").length, ofType(layoutStarMap(notes), "kind").length, "the kind node itself stays");
  assert.equal(ofType(folded, "note").length, notes.length - count);
  const both = layoutStarMap(notes, { collapsed: [kind.id, kind.parent] });
  assert.ok(!both.nodes.some((n) => n.id === clusterIdOf(kind.id)), "inside a folded project the kind's own fold does not show");
  assert.equal(both.nodes.find((n) => n.id === clusterIdOf(kind.parent)).count, notes.filter((e) => scopeIdOf(e) === kind.parent).length);
});

// -------------------------------------------------------------------------------------------- 0, 1, 2 entries

test("no notes: an empty chart with its rings, nothing to draw", () => {
  for (const empty of [[], null, undefined]) {
    const layout = layoutStarMap(empty);
    assert.deepEqual([layout.nodes.length, layout.edges.length, layout.shown, layout.hidden], [0, 0, 0, 0]);
    assert.ok(layout.rings.scope < layout.rings.kind && layout.rings.kind < layout.rings.leaf && layout.rings.leaf <= layout.rings.frame);
    assert.ok(layout.bounds.width > 0 && layout.bounds.height > 0);
  }
});

test("one note and two notes are complete trees", () => {
  const one = layoutStarMap([note("a")]);
  assert.deepEqual(one.nodes.map((n) => n.type), ["root", "scope", "kind", "note"]);
  assert.equal(one.edges.length, 3);
  assert.ok(one.allTitles && one.nodes.every((n) => n.label), "every node has its name, the note its title");
  assert.ok(finite(one));
  const two = layoutStarMap([note("a"), note("b", inProject("D:\\x", "x", { kind: "pitfall" }))]);
  assert.deepEqual(two.nodes.map((n) => n.type).sort(), ["kind", "kind", "note", "note", "root", "scope", "scope"]);
  const [s1, s2] = ofType(two, "scope");
  assert.ok(Math.abs(Math.abs(s1.angle - s2.angle) - Math.PI) < 0.05, "two branches of one note each sit opposite each other");
});

// --------------------------------------------------------------------------------------------- odd notes, text

test("odd notes do not trip the layout: no title, a huge title, unknown engines and kinds, missing fields, repeats", () => {
  const weird = [
    ...oddNotes(),
    note("o-empty"),
    note("dup", { title: "first" }),
    note("dup", { title: "second" }),
    null,
    { id: "bare" },
    note("o-kind", { kind: "workflow" }),
    note("o-nokind", { kind: "" }),
    note("o-time", { updated: Number.NaN, created: Number.NaN }),
    note("o-scope", { scope: "somewhere", project: "", projectName: "" }),
  ];
  const layout = layoutStarMap(weird);
  assert.ok(finite(layout));
  const ids = ofType(layout, "note").map((n) => n.id);
  assert.equal(ids.filter((id) => id === "dup").length, 1, "a repeated id is drawn once (the first)");
  assert.equal(layout.nodes.find((n) => n.id === "dup").name, "first");
  assert.ok(ids.includes("bare"), "a note with nothing but an id is still a star");
  assert.equal(layout.nodes.find((n) => n.id === "o-empty").name, "（无标题）", "the first of two notes with one id is the one drawn");
  assert.equal(layoutStarMap([note("x", { title: "  " })]).nodes.find((n) => n.id === "x").name, "（无标题）");
  assert.equal(noteTitle({ title: "" }), "（无标题）");
  const huge = layout.nodes.find((n) => n.id === "o-huge");
  assert.ok(huge.name.length > 1000 && huge.r <= 5, "the whole title is kept for the tooltip, the star stays in its size range");
  const kind = ofType(layout, "kind").find((k) => k.kind === "workflow");
  assert.equal(kind.label.text, "其他", "a kind the page does not know is drawn as 其他");
  assert.equal(layout.nodes.find((n) => n.id === "o-engine").source, "other", "an agent the page does not know is another agent");
  const folder = ofType(layout, "scope").find((s) => s.id === scopeIdOf({ scope: "project", project: "", projectName: "" }));
  assert.equal(folder.label.text, "项目", "a project note without a folder hangs from 项目");
});

test("titles are cut on whole characters: emoji, joined emoji, accents, other scripts", () => {
  const family = "👩‍👩‍👧‍👦";
  const cut = fitText(family.repeat(40), 80, 11);
  assert.ok(cut.endsWith("…"));
  const kept = cut.slice(0, -1);
  assert.ok(family.repeat(40).startsWith(kept) && kept.length % family.length === 0, "no family is split");
  assert.equal(fitText("短标题", 100, 11), "短标题");
  assert.equal(fitText("  多个   空格\n换行  ", 200, 11), "多个 空格 换行");
  assert.ok(textWidth(fitText("很长的标题".repeat(400), 150, 11), 11) <= 150);
  assert.ok(fitText("Ünïcödé ".repeat(30), 60, 12).endsWith("…"));
  assert.equal(snippet("一".repeat(200), 80).length, 81);
  assert.equal(snippet("a\n\n b"), "a b");
  assert.ok(mostlyWide("全局") && mostlyWide("项目") && !mostlyWide("wittycode") && !mostlyWide("app · Work"));
  const rtl = layoutStarMap([note("r", { title: "مرحبا بالعالم — Ελληνικά — Ünïcödé" })]);
  assert.ok(rtl.nodes.find((n) => n.id === "r").label.text.length > 0);
});

// ------------------------------------------------------------------------------------- what the stars tell

test("a star's size follows the length of its text; its brightness how recently it changed", () => {
  assert.equal(starRadius(""), 2.2);
  assert.equal(starRadius("x".repeat(5000)), 5);
  assert.ok(starRadius("x".repeat(80)) < starRadius("x".repeat(400)));
  assert.equal(brightness(NOW, NOW, NOW - 90 * 24 * HOUR), 1);
  assert.equal(brightness(NOW - 90 * 24 * HOUR, NOW, NOW - 90 * 24 * HOUR), 0.45);
  assert.ok(brightness(NOW - HOUR, NOW, NOW - 900 * HOUR) > brightness(NOW - 24 * HOUR, NOW, NOW - 900 * HOUR));
  const layout = layoutStarMap(SAMPLES.many());
  const notes = ofType(layout, "note");
  assert.ok(notes.every((n) => n.r >= 2.2 && n.r <= 5 && n.brightness >= 0.45 && n.brightness <= 1));
  const newest = notes.reduce((a, b) => (b.entry.updated > a.entry.updated ? b : a));
  const oldest = notes.reduce((a, b) => (b.entry.updated < a.entry.updated ? b : a));
  assert.equal(newest.brightness, 1);
  assert.equal(oldest.brightness, 0.45);
});

test("forgotten notes are stars flagged forgotten", () => {
  const layout = layoutStarMap([note("a"), note("b", { status: "forgotten" })]);
  assert.equal(layout.nodes.find((n) => n.id === "b").forgotten, true);
  assert.equal(layout.nodes.find((n) => n.id === "a").forgotten, false);
});

test("who wrote a note: the user, an engine, another agent, or unknown", () => {
  assert.deepEqual(
    [{ kind: "user" }, { kind: "agent", engine: "chatgpt" }, { kind: "agent", engine: "devin" }, { kind: "agent", engine: "factory" }, { kind: "agent", engine: "cursor" }, { kind: "unknown" }, undefined].map(sourceKey),
    ["user", "chatgpt", "devin", "factory", "other", "unknown", "unknown"],
  );
});

test("colours: four hues for the kinds, six for the sources, silver for the branches", () => {
  const kinds = ["preference", "convention", "pitfall", "fact"].map((kind) => starColor({ type: "note", kind, source: "user" }, "kind"));
  assert.equal(new Set(kinds).size, 4);
  const sources = ["user", "chatgpt", "devin", "factory", "other", "unknown"].map((source) => starColor({ type: "note", kind: "fact", source }, "source"));
  assert.equal(new Set(sources).size, 6);
  assert.equal(colorKey({ type: "scope", kind: null, source: null }, "kind"), "branch");
  assert.equal(colorKey({ type: "kind", kind: "pitfall", source: null }, "kind"), "k-pitfall", "a kind node in its kind's hue");
  assert.equal(colorKey({ type: "kind", kind: "pitfall", source: null }, "source"), "branch", "…and silver when the colours tell the source");
  assert.equal(colorKey({ type: "note", kind: "workflow", source: "user" }, "kind"), "k-other");
  const legend = legendItems(fewNotes(), "kind").map((i) => i.label);
  assert.deepEqual(legend, ["偏好", "约定", "坑"], "only the kinds there are, in their usual order");
  assert.deepEqual(legendItems(fewNotes(), "source").map((i) => i.label), ["我", "WittyHarness", "Devin"]);
});

// ---------------------------------------------------------------------------------- search, selection, titles

test("a search dims the stars that do not match, and the branches with nothing that matches", () => {
  const notes = SAMPLES.mid();
  const hits = new Set(notes.slice(0, 3).map((e) => e.id));
  const layout = layoutStarMap(notes, { matchIds: hits });
  for (const n of ofType(layout, "note")) {
    assert.equal(n.match, hits.has(n.id));
    assert.equal(n.dim, !hits.has(n.id));
  }
  const live = new Set(notes.filter((e) => hits.has(e.id)).map((e) => scopeIdOf(e)));
  for (const s of ofType(layout, "scope")) assert.equal(s.dim, !live.has(s.id), s.id);
  assert.equal(layout.nodes[0].dim, false, "the root holds a match");
  assert.ok(layout.edges.every((e) => e.dim === layout.nodes.find((n) => n.id === e.to).dim));
  assert.ok(layoutStarMap(notes, { matchIds: null }).nodes.every((n) => !n.dim && !n.match), "no search, nothing dims");
  assert.ok(layoutStarMap(notes, { matchIds: new Set() }).nodes.every((n) => n.dim), "a search that finds nothing dims everything");
  const scope = ofType(layout, "scope").find((s) => s.id.startsWith("s:p:") && !s.dim);
  const folded = layoutStarMap(notes, { matchIds: hits, collapsed: [scope.id] });
  assert.equal(folded.nodes.find((n) => n.id === clusterIdOf(scope.id)).dim, false, "a folded branch with a match inside is not dimmed");
});

test("the chosen note is marked with the branch from it to the root; inside a fold, the fold is", () => {
  const notes = SAMPLES.mid();
  const chosen = notes[5];
  const layout = layoutStarMap(notes, { selectedId: chosen.id });
  const star = layout.nodes.find((n) => n.id === chosen.id);
  assert.equal(star.selected, true);
  assert.equal(layout.nodes.filter((n) => n.selected).length, 1);
  const path = layout.nodes.filter((n) => n.onPath).map((n) => n.id);
  assert.deepEqual(path, [ROOT_ID, scopeIdOf(chosen), kindIdOf(scopeIdOf(chosen), chosen.kind), chosen.id]);
  assert.equal(layout.edges.filter((e) => e.onPath).length, 3);
  assert.ok(star.label, "its title is written");
  const kind = kindIdOf(scopeIdOf(chosen), chosen.kind);
  const folded = layoutStarMap(notes, { selectedId: chosen.id, collapsed: [kind] });
  assert.equal(folded.nodes.filter((n) => n.selected).length, 0);
  assert.equal(folded.nodes.find((n) => n.id === clusterIdOf(kind)).onPath, true);
  assert.equal(layoutStarMap(notes, { selectedId: "missing" }).nodes.filter((n) => n.onPath).length, 0);
});

test("choosing a star or searching never moves anything or rescales the chart", () => {
  for (const notes of [SAMPLES.few(), SAMPLES.mid(), SAMPLES.many()]) {
    const plain = layoutStarMap(notes);
    const where = (l) => ({ bounds: l.bounds, rings: l.rings, at: l.nodes.map((n) => [n.id, n.x, n.y]) });
    for (const e of notes.slice(0, 6)) assert.deepEqual(where(layoutStarMap(notes, { selectedId: e.id })), where(plain), `choosing ${e.id}`);
    assert.deepEqual(where(layoutStarMap(notes, { matchIds: new Set(notes.slice(0, 2).map((e) => e.id)) })), where(plain), "searching");
  }
});

test("up to ALL_TITLES stars every title is written; past that only the chosen star's", () => {
  const few = layoutStarMap(SAMPLES.mid());
  assert.ok(SAMPLES.mid().length <= ALL_TITLES && few.allTitles);
  assert.ok(ofType(few, "note").filter((n) => n.label).length >= 18, "most of them: a title that would cover another is left to the tooltip");
  const many = layoutStarMap(SAMPLES.many(), { selectedId: SAMPLES.many()[0].id });
  assert.equal(many.allTitles, false);
  assert.deepEqual(ofType(many, "note").filter((n) => n.label).map((n) => n.id), [SAMPLES.many()[0].id]);
  assert.equal(layoutStarMap(SAMPLES.many(), { allTitles: true }).allTitles, true, "an option can ask for them anyway");
});

test("names and titles do not overlap: titles with each other and with the names along the rings", () => {
  for (const notes of [SAMPLES.few(), SAMPLES.mid(), oddNotes()]) {
    const layout = layoutStarMap(notes);
    const flats = layout.nodes.filter((n) => n.label?.shape === "flat").map((n) => ({ id: n.id, box: boxOf(n.label) }));
    for (let i = 0; i < flats.length; i++) for (let j = i + 1; j < flats.length; j++) assert.ok(!meet(flats[i].box, flats[j].box), `${flats[i].id} / ${flats[j].id}`);
    const chars = layout.nodes.filter((n) => n.label?.shape === "stack").flatMap((n) => stackPositions(n.label).map((c) => ({ x: c.x - 5, y: c.y - 5, w: 10, h: 10 })));
    for (const f of flats) for (const c of chars) assert.ok(!meet(f.box, c), `${f.id} covers a kind's name`);
  }
  for (const notes of [SAMPLES.mid(), SAMPLES.many(), synthNotes(500, { seed: 3 }), oddNotes()]) {
    const layout = layoutStarMap(notes);
    const arcs = ofType(layout, "scope").map((s) => s.label);
    for (let i = 0; i < arcs.length; i++) {
      for (let j = i + 1; j < arcs.length; j++) {
        const [a, b] = [arcs[i], arcs[j]];
        const apart = Math.abs(Math.atan2(Math.sin((a.from + a.to) / 2 - (b.from + b.to) / 2), Math.cos((a.from + a.to) / 2 - (b.from + b.to) / 2)));
        assert.ok(apart >= (a.to - a.from) / 2 + (b.to - b.from) / 2 - 1e-6, `${a.text} / ${b.text} along the scope ring`);
      }
    }
    const kinds = ofType(layout, "kind").sort((a, b) => a.angle - b.angle);
    for (let i = 0; i < kinds.length; i++) {
      const a = kinds[i];
      const b = kinds[(i + 1) % kinds.length];
      if (a === b) continue;
      const inner = a.label.r0 - a.label.chars.length * a.label.step;
      const gap = Math.abs(Math.atan2(Math.sin(a.angle - b.angle), Math.cos(a.angle - b.angle))) * inner;
      assert.ok(gap >= 12, `${a.id} / ${b.id}: names ${gap.toFixed(1)} apart`);
    }
  }
});

// ------------------------------------------------------------------------------------------ drawing helpers

test("ring names: the path is long enough, upright characters read in order, stacked ones top to bottom", () => {
  const layout = layoutStarMap(SAMPLES.mid());
  const scope = ofType(layout, "scope")[0];
  assert.match(arcPath(scope.label), /^M-?[\d.]+ -?[\d.]+A[\d.]+ [\d.]+ 0 [01] [01] -?[\d.]+ -?[\d.]+$/);
  const top = arcChars({ ...scope.label, text: "全局", from: -Math.PI / 2 - 0.2, to: -Math.PI / 2 + 0.2 });
  assert.ok(top[0].x < top[1].x, "at the top: left to right");
  const bottom = arcChars({ ...scope.label, text: "项目", from: Math.PI / 2 - 0.2, to: Math.PI / 2 + 0.2 });
  assert.ok(bottom[0].x < bottom[1].x, "at the bottom too");
  const side = arcChars({ ...scope.label, text: "项目", from: -0.2, to: 0.2 });
  assert.ok(side[0].y < side[1].y, "at the side: top to bottom");
  for (const k of ofType(layout, "kind")) {
    const at = stackPositions(k.label);
    if (at.length < 2) continue;
    const [a, b] = at;
    assert.ok(Math.abs(a.y - b.y) > Math.abs(a.x - b.x) * 0.5 ? a.y < b.y : a.x < b.x, `${k.label.text} reads in order`);
  }
});

test("labels grow to be read when the chart is shown small, never past their room, and shrink when zoomed in", () => {
  const layout = layoutStarMap(SAMPLES.many());
  const small = ringScales(layout, 0.6);
  const near = ringScales(layout, 3);
  for (const n of layout.nodes.filter((x) => x.label && x.label.shape !== "flat")) {
    const room = n.label.shape === "arc" ? n.label.room / n.label.width : n.label.maxScale;
    assert.ok(small.get(n.id) >= 1 && small.get(n.id) <= Math.max(1, room) + 1e-9, n.id);
    assert.ok(near.get(n.id) < 1, `${n.id} stays readable, not huge, zoomed in`);
  }
});

test("arrow keys find the nearest node in that direction", () => {
  const layout = layoutStarMap(SAMPLES.mid());
  const from = ofType(layout, "note")[0];
  for (const dir of ["left", "right", "up", "down"]) {
    const id = neighbourOf(layout.nodes, from.id, dir);
    if (!id) continue;
    const to = layout.nodes.find((n) => n.id === id);
    const dx = to.x - from.x;
    const dy = to.y - from.y;
    assert.ok({ left: -dx, right: dx, up: -dy, down: dy }[dir] > 0, dir);
    assert.notEqual(to.type, "root");
  }
  const rightmost = layout.nodes.filter((n) => n.type !== "root").reduce((a, b) => (b.x > a.x ? b : a));
  assert.equal(neighbourOf(layout.nodes, rightmost.id, "right"), null);
  assert.equal(neighbourOf(layout.nodes, "missing", "left"), null);
});
