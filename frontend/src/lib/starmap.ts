// The shared memory as a star chart (components/MemoryStarMap.tsx draws it): a tree with the root 「记忆」 in the centre,
// the scopes (全局 and one node per project folder) on the first ring, the kinds of note under each on the second, and
// the notes themselves as stars on rows of arcs outside them. Pure and deterministic: no clock and no Math.random (the
// small scatter of the stars comes from a hash of the note's id), so scripts/starmap.test.mjs runs it in Node as it is.

import type { MemoryEntry, MemoryWho } from "../types";
import { KINDS, engineName, kindLabel } from "./memory.ts";

export type ColorBy = "kind" | "source";
export type SourceKey = "user" | "chatgpt" | "devin" | "factory" | "other" | "unknown";
export type StarType = "root" | "scope" | "kind" | "note" | "cluster";
export type Direction = "left" | "right" | "up" | "down";

/** Text along a ring around the centre, centred on its node. `reverse`: drawn the other way round (the lower half of the
 *  chart), so that it still reads from left to right. Angles are radians with y pointing down (-π/2 is the top). */
export interface ArcLabel {
  shape: "arc";
  text: string;
  count: string;
  r: number;
  from: number;
  to: number;
  reverse: boolean;
  /** the text's width at LABEL_FONT, and the most it may take along its ring (its branch's whole arc) */
  width: number;
  room: number;
  /** mostly Chinese (or Japanese, Korean): its characters stand upright along the ring (arcChars) instead of lying
   *  along it, which only reads well for Latin letters */
  upright: boolean;
}

/** Level text: (x, y) is where it is anchored, on the text's middle line. */
export interface FlatLabel {
  shape: "flat";
  text: string;
  count: string;
  x: number;
  y: number;
  anchor: "start" | "middle" | "end";
  width: number;
  height: number;
}

/** Upright characters one after another along the radius, inward from a kind node (the kinds' names are one or two
 *  characters): around the ring it takes no more room than one character. `chars` are in reading order. */
export interface StackLabel {
  shape: "stack";
  text: string;
  chars: string[];
  /** the radius it runs along (its node's angle), and where it starts next to the node */
  angle: number;
  r0: number;
  /** the distance between two characters at LABEL_FONT */
  step: number;
  /** read from the inner end (top to bottom, left to right) */
  reverse: boolean;
  width: number;
  /** the most it may grow: the room on both sides and toward the scope ring */
  maxScale: number;
}

export type StarLabel = ArcLabel | FlatLabel | StackLabel;

export interface StarNode {
  id: string;
  type: StarType;
  parent: string | null;
  /** the scope node it is under (a scope node: itself; the root: null) */
  scope: string | null;
  /** a cluster: the collapsed branch it stands for (activating it opens that branch again) */
  branch: string | null;
  depth: number;
  x: number;
  y: number;
  r: number;
  angle: number;
  /** the whole name: a note's title, a project's name and folder (tooltips, <title>, aria-label) */
  name: string;
  label: StarLabel | null;
  entry: MemoryEntry | null;
  /** the kind of note: kind nodes, notes, and clusters of one kind */
  kind: string | null;
  source: SourceKey | null;
  /** notes at or under it */
  count: number;
  /** 1 for the newest note, down to 0.45 for the oldest */
  brightness: number;
  forgotten: boolean;
  collapsed: boolean;
  /** a search is on and nothing at or under it matches */
  dim: boolean;
  match: boolean;
  selected: boolean;
  /** on the branch from the selected note (or the cluster that holds it) to the root */
  onPath: boolean;
}

export interface StarEdge {
  id: string;
  from: string;
  to: string;
  x1: number;
  y1: number;
  x2: number;
  y2: number;
  /** from a kind to one of its notes: the thinnest and most numerous lines */
  leaf: boolean;
  dim: boolean;
  onPath: boolean;
}

export interface StarRings {
  scope: number;
  kind: number;
  /** the first row of stars */
  leaf: number;
  /** the last row of stars that has any */
  outer: number;
  /** the graduated circle around the chart */
  frame: number;
  rows: number;
  rowGap: number;
}

export interface StarMapLayout {
  nodes: StarNode[];
  edges: StarEdge[];
  /** everything drawn, labels included, in layout px (centre of the chart at 0, 0) */
  bounds: { x: number; y: number; width: number; height: number };
  rings: StarRings;
  /** notes drawn as stars, and notes folded into clusters */
  shown: number;
  hidden: number;
  /** every star has its title written next to it (few stars) */
  allTitles: boolean;
}

export interface StarMapOptions {
  /** ids of the branch nodes (scopes, kinds) that are folded into one star */
  collapsed?: ReadonlySet<string> | readonly string[];
  /** a search is on: the notes that match it (the others dim) */
  matchIds?: ReadonlySet<string> | null;
  selectedId?: string | null;
  /** the closest two stars may be, in layout px; default MIN_GAP */
  minGap?: number;
  /** write every star's title; default: when at most ALL_TITLES stars are drawn */
  allTitles?: boolean;
}

const TAU = Math.PI * 2;
/** The closest two stars may be, in layout px (the chart's px at zoom 1). */
export const MIN_GAP = 14;
/** Up to this many stars, every one has its title written next to it. */
export const ALL_TITLES = 25;
/** The label sizes the layout keeps room for; the chart never draws a label bigger than this (labelScale). */
export const LABEL_FONT = 12;
export const NOTE_FONT = 11;
const LINE = 1.3;
export const ROOT_R = 8.5;
export const SCOPE_R = 4.4;
export const KIND_R = 3.4;
export const CLUSTER_R = 5.8;
/** Stars start on slots this much further apart than the minimum, then each strays up to SCATTER px from its slot
 *  (seeded by its id), a move kept only if every star stays MIN_GAP from every other: rows and columns no longer show. */
const SLACK = 4;
const SCATTER = 5.5;
const SCATTER_PASSES = 2;
const SCOPE_RING = 64;
const RING_STEP = 64;
const LEAF_STEP = 36;
const SCOPE_LABEL_OUT = 15;
const FRAME_GAP = 20;
/** the share of a ring its names may take (the rest stays free, so that the branches still get angle in proportion to
 *  their notes) */
const SCOPE_LABEL_SHARE = 0.6;
const KIND_LABEL_SHARE = 0.55;
/** a kind's name: starts this far inside its node, characters this far apart (times LABEL_FONT), at most two of them */
const KIND_LABEL_GAP = KIND_R + 4;
const KIND_STEP = 1.08;
const KIND_CHARS = 2;
/** a kind's name needs this much room around the ring at its inner end */
const KIND_ROOM = LABEL_FONT + 5;
/** a ring's names never grow past this (the rings are this far apart) */
export const MAX_ARC_SCALE = 1.3;
const MAX_ROWS = 64;
export const GLOBAL_ID = "s:global";
export const ROOT_ID = "root";

// ------------------------------------------------------------------------------------------------------ colours

export const KIND_COLORS: Record<string, string> = {
  preference: "#ff9bd2",
  convention: "#7cc3ff",
  pitfall: "#ffb35c",
  fact: "#8fe3a0",
};
export const OTHER_KIND_COLOR = "#c7cfe2";
export const SOURCE_COLORS: Record<SourceKey, string> = {
  user: "#ffd66b",
  chatgpt: "#b99dff",
  devin: "#ff8fae",
  factory: "#84dcb7",
  other: "#86b4ff",
  unknown: "#a9b2c6",
};
export const BRANCH_COLOR = "#e4eaf7";
export const SOURCES: { id: SourceKey; label: string }[] = [
  { id: "user", label: "我" },
  { id: "chatgpt", label: engineName("chatgpt") },
  { id: "devin", label: engineName("devin") },
  { id: "factory", label: engineName("factory") },
  { id: "other", label: "其他代理" },
  { id: "unknown", label: "未知" },
];

const AGENTS = new Set<string>(["devin", "chatgpt", "factory"]);

/** Who wrote a note, as a colour of the chart: the user, one of the engines, another agent (a note brought in by an import,
 *  or written by an engine of an earlier version), or unknown. */
export function sourceKey(who: MemoryWho | null | undefined): SourceKey {
  if (!who) return "unknown";
  if (who.kind === "user") return "user";
  if (who.kind === "agent") return AGENTS.has(who.engine) ? (who.engine as SourceKey) : "other";
  return "unknown";
}

const kindKey = (kind: string | null) => (kind && KIND_COLORS[kind] ? kind : "other");

/** The colour a node is drawn in, as a key (the chart has one glow gradient per key) and as a colour. */
export function colorKey(node: Pick<StarNode, "type" | "kind" | "source">, by: ColorBy): string {
  if (node.type === "note") return by === "kind" ? `k-${kindKey(node.kind)}` : `s-${node.source ?? "unknown"}`;
  if ((node.type === "kind" || node.type === "cluster") && node.kind && by === "kind") return `k-${kindKey(node.kind)}`;
  return "branch";
}

export function colorOfKey(key: string): string {
  if (key.startsWith("k-")) return KIND_COLORS[key.slice(2)] ?? OTHER_KIND_COLOR;
  if (key.startsWith("s-")) return SOURCE_COLORS[key.slice(2) as SourceKey] ?? SOURCE_COLORS.unknown;
  return BRANCH_COLOR;
}

export const starColor = (node: Pick<StarNode, "type" | "kind" | "source">, by: ColorBy) => colorOfKey(colorKey(node, by));

/** Every key colorKey can give. */
export const COLOR_KEYS: string[] = [...Object.keys(KIND_COLORS).map((k) => `k-${k}`), "k-other", ...SOURCES.map((s) => `s-${s.id}`), "branch"];

export interface LegendItem {
  key: string;
  label: string;
  color: string;
}

/** What the colours mean, for the notes there are: the kinds in their usual order, or who wrote them. */
export function legendItems(entries: readonly MemoryEntry[], by: ColorBy): LegendItem[] {
  if (by === "kind") {
    const present = new Set(entries.map((e) => kindKey(String(e.kind ?? ""))));
    const items: LegendItem[] = KINDS.filter((k) => present.has(k.id)).map((k) => ({ key: `k-${k.id}`, label: k.label, color: KIND_COLORS[k.id] }));
    if (present.has("other")) items.push({ key: "k-other", label: "其他", color: OTHER_KIND_COLOR });
    return items;
  }
  const present = new Set(entries.map((e) => sourceKey(e.source)));
  return SOURCES.filter((s) => present.has(s.id)).map((s) => ({ key: `s-${s.id}`, label: s.label, color: SOURCE_COLORS[s.id] }));
}

// -------------------------------------------------------------------------------------------------------- text

const NARROW = new Set(Array.from("il.,:;'|!`()[]"));
const WIDE = new Set(Array.from("mwMW@%"));

/** About how wide a character is, in em, in the chart's fonts (Inter, and Microsoft YaHei UI for Chinese). On the generous
 *  side: a label along a ring is cut off where its path ends, so the path must not come out shorter than the text. */
export function charWidth(ch: string): number {
  const c = ch.codePointAt(0) ?? 0;
  if (c >= 0x1f000) return 1.25;
  if (c >= 0x2e80 || (c >= 0x1100 && c <= 0x11ff)) return 1;
  if (ch === " ") return 0.3;
  if (NARROW.has(ch)) return 0.34;
  if (WIDE.has(ch)) return 0.88;
  if (c >= 0x41 && c <= 0x5a) return 0.72;
  if (c >= 0x30 && c <= 0x39) return 0.64;
  if (c < 0x80) return 0.6;
  return 0.7;
}

let segmenter: Intl.Segmenter | null | undefined;

/** The text's characters as a reader sees them: an emoji made of several code points, or a letter with its accent, is one. */
function graphemes(text: string): string[] {
  if (segmenter === undefined) segmenter = typeof Intl !== "undefined" && typeof Intl.Segmenter === "function" ? new Intl.Segmenter(undefined, { granularity: "grapheme" }) : null;
  return segmenter ? Array.from(segmenter.segment(text), (s) => s.segment) : Array.from(text);
}

export function textWidth(text: string, size: number): number {
  let w = 0;
  for (const g of graphemes(text)) w += charWidth(g);
  return w * size;
}

/** The text on one line, cut to `max` px with an ellipsis; whole characters only. */
export function fitText(text: string, max: number, size: number): string {
  const clean = String(text ?? "").replace(/\s+/g, " ").trim();
  const head = clean.length > 300 ? clean.slice(0, 300) : clean;
  const parts = graphemes(head);
  let total = 0;
  for (const p of parts) total += charWidth(p) * size;
  if (head === clean && total <= max) return clean;
  const room = max - charWidth("…") * size;
  let w = 0;
  let out = "";
  for (const p of parts) {
    const cw = charWidth(p) * size;
    if (w + cw > room) break;
    out += p;
    w += cw;
  }
  return `${out.trimEnd()}…`;
}

/** The start of a note's text on one line, for the tooltip. */
export function snippet(text: string, max = 80): string {
  const clean = String(text ?? "").replace(/\s+/g, " ").trim();
  const parts = graphemes(clean.length > max * 4 ? clean.slice(0, max * 4) : clean);
  return parts.length > max ? `${parts.slice(0, max).join("").trimEnd()}…` : parts.join("");
}

export const noteTitle = (entry: Pick<MemoryEntry, "title">) => String(entry.title ?? "").replace(/\s+/g, " ").trim() || "（无标题）";

// --------------------------------------------------------------------------------------------------- the stars

/** A star's radius: from 2.2 for a line of text to 5 for a long note (the scale is logarithmic). */
export function starRadius(text: string): number {
  const n = Math.max(1, String(text ?? "").length);
  const t = (Math.log(n) - Math.log(20)) / (Math.log(1500) - Math.log(20));
  return round(2.2 + 2.8 * Math.min(1, Math.max(0, t)), 2);
}

/** How bright a note is: 1 for the newest, 0.45 for the oldest; on a logarithmic scale of hours, so the notes of the last
 *  days stand apart from each other, not only from last year's. */
export function brightness(updated: number, newest: number, oldest: number): number {
  const span = newest - oldest;
  if (!(span > 0) || !Number.isFinite(updated)) return 1;
  const age = Math.min(span, Math.max(0, newest - updated));
  const t = Math.log1p(age / 3_600_000) / Math.log1p(span / 3_600_000);
  return round(1 - 0.55 * t, 3);
}

/** FNV-1a with a final mix: ids that differ in one character still scatter. */
export function hash32(text: string): number {
  let h = 0x811c9dc5;
  for (let i = 0; i < text.length; i++) {
    h ^= text.charCodeAt(i);
    h = Math.imul(h, 0x01000193);
  }
  h ^= h >>> 16;
  h = Math.imul(h, 0x85ebca6b);
  h ^= h >>> 13;
  h = Math.imul(h, 0xc2b2ae35);
  h ^= h >>> 16;
  return h >>> 0;
}

/** A small seeded generator (mulberry32): the same seed gives the same numbers in [0, 1). */
export function seeded(seed: number): () => number {
  let a = seed >>> 0;
  return () => {
    a = (a + 0x6d2b79f5) >>> 0;
    let t = a;
    t = Math.imul(t ^ (t >>> 15), t | 1);
    t ^= t + Math.imul(t ^ (t >>> 7), t | 61);
    return ((t ^ (t >>> 14)) >>> 0) / 4294967296;
  };
}

/** The same folder however it is written (as lib/memory.ts samePath compares them). */
export const folderKey = (path: string) => String(path ?? "").replace(/[\\/]+$/, "").replace(/\//g, "\\").toLowerCase();

/** The scope node a note hangs from: the one global node, or its project's folder (two projects that share a name are
 *  still two nodes). */
export function scopeIdOf(entry: Pick<MemoryEntry, "scope" | "project" | "projectName">): string {
  if (entry.scope === "global") return GLOBAL_ID;
  const key = folderKey(entry.project);
  return key ? `s:p:${key}` : `s:p:?${String(entry.projectName ?? "").trim().toLowerCase()}`;
}

export const kindIdOf = (scopeId: string, kind: string) => `${scopeId}/k:${kind}`;
export const clusterIdOf = (branch: string) => `${branch}/+`;

/** How big labels are drawn at zoom `k` (layout px per screen px): as laid out up to zoom 1.08, then smaller in the
 *  chart's own units so they stay 13 px on the screen. Never bigger than the size the layout made room for. */
export const labelScale = (k: number) => Math.min(1, 13 / (LABEL_FONT * Math.max(k, 1e-6)));

const round = (n: number, digits = 2) => {
  const f = 10 ** digits;
  return Math.round(n * f) / f;
};

// ------------------------------------------------------------------------------------------------------ layout

interface KindGroup {
  id: string;
  kind: string;
  entries: MemoryEntry[];
  collapsed: boolean;
  leaves: number;
  text: string;
  count: string;
  width: number;
}

interface ScopeGroup {
  id: string;
  global: boolean;
  name: string;
  path: string;
  display: string;
  entries: MemoryEntry[];
  kinds: KindGroup[];
  collapsed: boolean;
  leaves: number;
  text: string;
  count: string;
  width: number;
}

interface Plan {
  rows: number;
  steps: number[];
  scopeSpans: number[];
  kindSpans: number[][];
  gS: number;
  gK: number;
}

/** Shares `total` out in proportion to `weights`, none below its minimum (water-filling); the caller makes sure the
 *  minimums fit. */
export function share(weights: readonly number[], mins: readonly number[], total: number): number[] {
  const fixed = weights.map(() => false);
  for (;;) {
    let free = 0;
    let rest = total;
    for (let i = 0; i < weights.length; i++) {
      if (fixed[i]) rest -= mins[i];
      else free += weights[i];
    }
    const lambda = free > 0 ? Math.max(0, rest) / free : 0;
    let changed = false;
    for (let i = 0; i < weights.length; i++) {
      if (!fixed[i] && lambda * weights[i] < mins[i]) {
        fixed[i] = true;
        changed = true;
      }
    }
    if (!changed) return weights.map((w, i) => (fixed[i] ? mins[i] : lambda * w));
  }
}

const KIND_ORDER: string[] = KINDS.map((k) => k.id);
const compare = (a: string, b: string) => (a < b ? -1 : a > b ? 1 : 0);
const countWidth = (count: string) => (count ? (count.length * 0.64 + 0.45) * LABEL_FONT * 0.82 : 0);

function uniqueNotes(entries: readonly MemoryEntry[]): MemoryEntry[] {
  const seen = new Set<string>();
  const out: MemoryEntry[] = [];
  for (const e of entries ?? []) {
    if (!e || typeof e.id !== "string" || seen.has(e.id)) continue;
    seen.add(e.id);
    out.push(e);
  }
  return out;
}

const timeOf = (e: MemoryEntry) => (Number.isFinite(e.updated) ? e.updated : Number.isFinite(e.created) ? e.created : 0);

function parentFolder(path: string): string {
  const parts = path.replace(/[\\/]+$/, "").split(/[\\/]+/).filter(Boolean);
  return parts.length >= 2 ? parts[parts.length - 2] : "";
}

/** Group the notes into scopes and kinds, in the chart's order: 全局 first, then the projects by name; the kinds in their
 *  usual order; the notes newest first (the first row of stars is the newest). */
function groups(notes: MemoryEntry[], collapsed: Set<string>): ScopeGroup[] {
  const byId = new Map<string, ScopeGroup>();
  for (const e of notes) {
    const id = scopeIdOf(e);
    let s = byId.get(id);
    if (!s) {
      s = { id, global: id === GLOBAL_ID, name: "", path: "", display: "", entries: [], kinds: [], collapsed: false, leaves: 0, text: "", count: "", width: 0 };
      byId.set(id, s);
    }
    if (!s.global) {
      if (!s.name && e.projectName) s.name = String(e.projectName).replace(/\s+/g, " ").trim();
      if (!s.path && e.project) s.path = String(e.project);
    }
    s.entries.push(e);
  }
  const scopes = [...byId.values()].sort((a, b) => (a.global !== b.global ? (a.global ? -1 : 1) : compare(a.name.toLowerCase(), b.name.toLowerCase()) || compare(a.id, b.id)));
  for (const s of scopes) s.display = s.global ? "全局" : s.name || "项目";
  // Two projects with the same name: each gets its parent folder, or failing that a number.
  const byName = new Map<string, ScopeGroup[]>();
  for (const s of scopes) if (!s.global) byName.set(s.display.toLowerCase(), [...(byName.get(s.display.toLowerCase()) ?? []), s]);
  for (const same of byName.values()) {
    if (same.length < 2) continue;
    const parents = same.map((s) => parentFolder(s.path));
    const distinct = parents.every(Boolean) && new Set(parents.map((p) => p.toLowerCase())).size === same.length;
    same.forEach((s, i) => (s.display = `${s.display} · ${distinct ? parents[i] : i + 1}`));
  }
  const maxScopeLabel = Math.max(40, Math.min(84, 600 / Math.max(1, scopes.length)));
  for (const s of scopes) {
    s.collapsed = collapsed.has(s.id);
    const kinds = new Map<string, KindGroup>();
    for (const e of s.entries) {
      const kind = typeof e.kind === "string" && e.kind ? e.kind : "fact";
      let k = kinds.get(kind);
      if (!k) {
        k = { id: kindIdOf(s.id, kind), kind, entries: [], collapsed: false, leaves: 0, text: "", count: "", width: 0 };
        kinds.set(kind, k);
      }
      k.entries.push(e);
    }
    s.kinds = [...kinds.values()].sort((a, b) => {
      const ia = KIND_ORDER.indexOf(a.kind);
      const ib = KIND_ORDER.indexOf(b.kind);
      return (ia < 0 ? 99 : ia) - (ib < 0 ? 99 : ib) || compare(a.kind, b.kind);
    });
    for (const k of s.kinds) {
      k.entries.sort((a, b) => timeOf(b) - timeOf(a) || compare(a.id, b.id));
      k.collapsed = !s.collapsed && collapsed.has(k.id);
      k.leaves = k.collapsed ? 1 : k.entries.length;
      k.text = KIND_ORDER.includes(k.kind) ? kindLabel(k.kind) : "其他";
      k.count = String(k.entries.length);
      k.width = LABEL_FONT;
    }
    s.leaves = s.collapsed ? 1 : s.kinds.reduce((n, k) => n + k.leaves, 0);
    s.text = fitText(s.display, maxScopeLabel, LABEL_FONT);
    s.count = String(s.entries.length);
    s.width = textWidth(s.text, LABEL_FONT);
  }
  return scopes;
}

/** Room around the chart for `rows` rows of stars from radius r3 outward: the angle between two neighbours on each row. */
function rowSteps(r3: number, rows: number, cell: number, rowGap: number): number[] {
  const steps: number[] = [];
  for (let j = 0; j < rows; j++) steps.push(2 * Math.asin(Math.min(1, cell / (2 * (r3 + j * rowGap)))));
  return steps;
}

function capacity(span: number, steps: readonly number[]): number {
  let n = 0;
  for (const step of steps) n += Math.floor(span / step + 1e-9);
  return n;
}

/** The narrowest angle that holds n stars on the rows (and at least one on the first row). */
function leafMin(n: number, steps: readonly number[]): number {
  if (n <= 0) return 0;
  let lo = steps[0];
  if (capacity(lo, steps) >= n) return lo;
  let hi = n * steps[0];
  for (let i = 0; i < 48; i++) {
    const mid = (lo + hi) / 2;
    if (capacity(mid, steps) >= n) hi = mid;
    else lo = mid;
  }
  return hi;
}

/** The fewest rows of stars with which every branch fits around the circle, and the angle each branch gets. */
function plan(scopes: ScopeGroup[], r1: number, r2: number, r3: number, cell: number, rowGap: number): Plan | null {
  const gS = (cell * 0.9) / r3;
  const gK = (cell * 0.3) / r3;
  const rho1 = r1 + SCOPE_LABEL_OUT;
  const rho2 = kindInner(r2);
  const available = TAU - scopes.length * gS;
  if (available <= 0) return null;
  const allSteps = rowSteps(r3, MAX_ROWS, cell, rowGap);
  const minsAt = (rows: number) => {
    const steps = allSteps.slice(0, rows);
    const cache = new Map<number, number>();
    const leaf = (n: number) => {
      let v = cache.get(n);
      if (v === undefined) cache.set(n, (v = leafMin(n, steps)));
      return v;
    };
    const kindMins = scopes.map((s) => (s.collapsed ? [] : s.kinds.map((k) => Math.max(KIND_ROOM / rho2, leaf(k.leaves)))));
    const scopeMins = scopes.map((s, i) => {
      const own = (s.width + 16) / rho1;
      const inner = s.collapsed ? (cell * 2.2) / r2 : kindMins[i].reduce((a, b) => a + b, 0) + (s.kinds.length - 1) * gK;
      return Math.max(own, inner);
    });
    return { steps, kindMins, scopeMins };
  };
  const fits = (rows: number) => minsAt(rows).scopeMins.reduce((a, b) => a + b, 0) <= available + 1e-12;
  if (!fits(MAX_ROWS)) return null;
  let lo = 1;
  let hi = MAX_ROWS;
  while (lo < hi) {
    const mid = (lo + hi) >> 1;
    if (fits(mid)) hi = mid;
    else lo = mid + 1;
  }
  const { steps, kindMins, scopeMins } = minsAt(lo);
  const scopeSpans = share(
    scopes.map((s) => s.leaves),
    scopeMins,
    available,
  );
  const kindSpans = scopes.map((s, i) =>
    s.collapsed
      ? []
      : share(
          s.kinds.map((k) => k.leaves),
          kindMins[i],
          scopeSpans[i] - (s.kinds.length - 1) * gK,
        ),
  );
  return { rows: lo, steps, scopeSpans, kindSpans, gS, gK };
}

/** The radius of a kind's name's inner end. */
const kindInner = (r2: number) => r2 - KIND_LABEL_GAP - KIND_CHARS * KIND_STEP * LABEL_FONT;

function emptyLayout(): StarMapLayout {
  const rings: StarRings = { scope: SCOPE_RING, kind: SCOPE_RING + RING_STEP, leaf: SCOPE_RING + RING_STEP + LEAF_STEP, outer: SCOPE_RING + RING_STEP + LEAF_STEP, frame: SCOPE_RING + RING_STEP + LEAF_STEP + FRAME_GAP, rows: 0, rowGap: 0 };
  const f = rings.frame + 8;
  return { nodes: [], edges: [], bounds: { x: -f, y: -f, width: 2 * f, height: 2 * f }, rings, shown: 0, hidden: 0, allTitles: false };
}

interface Box {
  x: number;
  y: number;
  w: number;
  h: number;
}

const boxesMeet = (a: Box, b: Box) => a.x < b.x + b.w && b.x < a.x + a.w && a.y < b.y + b.h && b.y < a.y + a.h;

function boxMeetsCircle(b: Box, cx: number, cy: number, r: number): boolean {
  const dx = cx - Math.max(b.x, Math.min(cx, b.x + b.w));
  const dy = cy - Math.max(b.y, Math.min(cy, b.y + b.h));
  return dx * dx + dy * dy < r * r;
}

function flatBox(l: FlatLabel): Box {
  const x = l.anchor === "start" ? l.x : l.anchor === "end" ? l.x - l.width : l.x - l.width / 2;
  return { x, y: l.y - l.height / 2, w: l.width, h: l.height };
}

/** A label along a ring, as a few level boxes (good enough to keep other text off it). */
function arcBoxes(l: ArcLabel, height: number): Box[] {
  const length = Math.abs(l.to - l.from) * l.r;
  const n = Math.max(1, Math.ceil(length / 12));
  const out: Box[] = [];
  for (let i = 0; i < n; i++) {
    const a = l.from + ((i + 0.5) / n) * (l.to - l.from);
    const x = l.r * Math.cos(a);
    const y = l.r * Math.sin(a);
    const s = length / n + height * 0.5;
    out.push({ x: x - s / 2, y: y - s / 2, w: s, h: s });
  }
  return out;
}

const r2Offset = () => KIND_LABEL_GAP + KIND_CHARS * KIND_STEP * LABEL_FONT;

function stackLabel(text: string, r2: number, r1: number, angle: number, span: number): StackLabel {
  const chars = graphemes(text).slice(0, KIND_CHARS);
  const r0 = r2 - KIND_LABEL_GAP;
  const step = KIND_STEP * LABEL_FONT;
  const inner = r0 - chars.length * step;
  const ux = -Math.cos(angle);
  const uy = -Math.sin(angle);
  // reading order: top to bottom where the radius is steep, left to right where it is level
  const reverse = Math.abs(uy) >= 0.45 ? uy < 0 : ux < 0;
  const around = (span * inner - 4) / LABEL_FONT;
  const inward = (r0 - (r1 + SCOPE_LABEL_OUT + LABEL_FONT * 0.65 * MAX_ARC_SCALE + 4)) / (chars.length * step);
  return { shape: "stack", text, chars, angle: round(angle, 4), r0: round(r0), step: round(step, 3), reverse, width: LABEL_FONT, maxScale: round(Math.max(1, Math.min(MAX_ARC_SCALE, around, inward)), 3) };
}

/** Where the characters of a stacked name go at `scale` times LABEL_FONT, in reading order. */
export function stackPositions(l: StackLabel, scale = 1): { ch: string; x: number; y: number }[] {
  const n = l.chars.length;
  return l.chars.map((ch, j) => {
    const slot = l.reverse ? n - 1 - j : j;
    const r = l.r0 - (slot + 0.5) * l.step * scale;
    return { ch, x: round(r * Math.cos(l.angle)), y: round(r * Math.sin(l.angle)) };
  });
}

/** Most of the text's characters are Chinese, Japanese or Korean. */
export function mostlyWide(text: string): boolean {
  const parts = graphemes(text).filter((g) => g.trim());
  const wide = parts.filter((g) => {
    const c = g.codePointAt(0) ?? 0;
    return c >= 0x2e80 && c < 0x1f000;
  }).length;
  return parts.length > 0 && wide * 2 >= parts.length;
}

function arcLabel(text: string, width: number, r: number, angle: number, span: number): ArcLabel {
  const upright = mostlyWide(text);
  const w = upright ? width * 1.08 : width;
  const half = (w + 10) / 2 / r;
  return { shape: "arc", text, count: "", r: round(r), from: round(angle - half, 4), to: round(angle + half, 4), reverse: Math.sin(angle) > 1e-6, width: round(w, 1), room: round(Math.max(w, span * r - 12), 1), upright };
}

/** Where the upright characters of a ring label go at `scale` times LABEL_FONT: one after another along the ring, in
 *  reading order (left to right where the ring runs level, top to bottom where it runs steep). */
export function arcChars(l: ArcLabel, scale = 1): { ch: string; x: number; y: number }[] {
  const chars = graphemes(l.text);
  const size = LABEL_FONT * scale;
  const widths = chars.map((c) => charWidth(c) * size);
  const space = size * 0.08;
  const total = widths.reduce((a, b) => a + b, 0) + space * Math.max(0, chars.length - 1);
  const mid = (l.from + l.to) / 2;
  const tx = -Math.sin(mid);
  const ty = Math.cos(mid);
  const forward = Math.abs(tx) >= Math.abs(ty) ? tx > 0 : ty > 0;
  let s = -total / 2;
  return chars.map((ch, i) => {
    const offset = s + widths[i] / 2;
    s += widths[i] + space;
    const a = mid + (forward ? offset : -offset) / l.r;
    return { ch, x: round(l.r * Math.cos(a)), y: round(l.r * Math.sin(a)) };
  });
}

interface Obstacles {
  circles: { x: number; y: number; r: number; weight: number }[];
  boxes: Box[];
}

/** Where a level label goes next to a node: on the far side from the centre first, then the other sides; the place that
 *  covers the least (other labels and branch nodes weigh more than stars). */
function placeFlat(node: StarNode, text: string, count: string, size: number, obstacles: Obstacles): { label: FlatLabel; score: number } {
  const width = textWidth(text, size) + (count ? countWidth(count) * (size / LABEL_FONT) : 0);
  const height = size * LINE;
  const pad = node.r + 4;
  const c = Math.cos(node.angle);
  const s = Math.sin(node.angle);
  const right = (): FlatLabel => ({ shape: "flat", text, count, x: node.x + pad, y: node.y, anchor: "start", width, height });
  const left = (): FlatLabel => ({ shape: "flat", text, count, x: node.x - pad, y: node.y, anchor: "end", width, height });
  const above = (): FlatLabel => ({ shape: "flat", text, count, x: node.x, y: node.y - pad - height / 2 + 2, anchor: "middle", width, height });
  const below = (): FlatLabel => ({ shape: "flat", text, count, x: node.x, y: node.y + pad + height / 2 - 2, anchor: "middle", width, height });
  const order = c >= 0.38 ? [right, above, below, left] : c <= -0.38 ? [left, above, below, right] : s < 0 ? [above, c >= 0 ? right : left, c >= 0 ? left : right, below] : [below, c >= 0 ? right : left, c >= 0 ? left : right, above];
  let best: { label: FlatLabel; score: number } | null = null;
  for (const make of order) {
    const label = make();
    const box = flatBox(label);
    let score = 0;
    for (const o of obstacles.circles) if (o.weight > 0 && boxMeetsCircle(box, o.x, o.y, o.r)) score += o.weight;
    for (const b of obstacles.boxes) if (boxesMeet(box, b)) score += 8;
    if (!best || score < best.score) best = { label, score };
    if (score === 0) break;
  }
  const { label, score } = best!;
  return { label: { ...label, x: round(label.x), y: round(label.y), width: round(label.width, 1), height: round(label.height, 1) }, score };
}

/** Lets every note's star stray a little from its slot (see SCATTER). The chart starts with every pair further apart than
 *  `gap` and a move is kept only if that stays true, so it stays true. Clusters stay where they are. */
function scatter(leaves: StarNode[], wedges: Map<string, { from: number; to: number }>, gap: number, rMin: number, rMax: number): void {
  const grid = new Map<string, StarNode[]>();
  const cellOf = (x: number, y: number) => `${Math.floor(x / gap)},${Math.floor(y / gap)}`;
  const add = (key: string, n: StarNode) => {
    const list = grid.get(key);
    if (list) list.push(n);
    else grid.set(key, [n]);
  };
  for (const n of leaves) add(cellOf(n.x, n.y), n);
  const clear = (self: StarNode, x: number, y: number) => {
    const gx = Math.floor(x / gap);
    const gy = Math.floor(y / gap);
    for (let i = -1; i <= 1; i++) {
      for (let j = -1; j <= 1; j++) {
        for (const o of grid.get(`${gx + i},${gy + j}`) ?? []) {
          if (o !== self && Math.hypot(o.x - x, o.y - y) < gap + 0.05) return false;
        }
      }
    }
    return true;
  };
  for (let pass = 0; pass < SCATTER_PASSES; pass++) {
    for (const n of leaves) {
      const wedge = wedges.get(n.id);
      if (!wedge) continue;
      const rnd = seeded(hash32(n.id) + pass * 7919);
      for (let attempt = 0; attempt < 3; attempt++) {
        const a = rnd() * TAU;
        const d = SCATTER * Math.sqrt(rnd());
        const x = round(n.x + d * Math.cos(a));
        const y = round(n.y + d * Math.sin(a));
        const r = Math.hypot(x, y);
        if (r < rMin || r > rMax) continue;
        const margin = 5 / r;
        let angle = Math.atan2(y, x);
        while (angle < wedge.from - Math.PI) angle += TAU;
        while (angle > wedge.from + Math.PI) angle -= TAU;
        if (angle < wedge.from + margin || angle > wedge.to - margin) continue;
        if (!clear(n, x, y)) continue;
        const before = cellOf(n.x, n.y);
        const after = cellOf(x, y);
        if (before !== after) {
          const list = grid.get(before)!;
          list.splice(list.indexOf(n), 1);
          add(after, n);
        }
        n.x = x;
        n.y = y;
        n.angle = round(Math.atan2(y, x), 4);
        break;
      }
    }
  }
}

/** The star chart of `entries`: every node with its place, size, label and flags, the lines between them, and the box
 *  around it all. The same input always gives the same chart. */
export function layoutStarMap(entries: readonly MemoryEntry[], options: StarMapOptions = {}): StarMapLayout {
  const gap = Math.max(4, Number.isFinite(options.minGap) ? (options.minGap as number) : MIN_GAP);
  const collapsed = new Set<string>(options.collapsed ?? []);
  const matchIds = options.matchIds ?? null;
  const selectedId = options.selectedId ?? null;
  const notes = uniqueNotes(entries);
  if (notes.length === 0) return emptyLayout();

  let newest = -Infinity;
  let oldest = Infinity;
  for (const e of notes) {
    const t = timeOf(e);
    if (t > newest) newest = t;
    if (t < oldest) oldest = t;
  }
  const scopes = groups(notes, collapsed);
  const cell = gap + SLACK;
  const rowGap = gap + SLACK * 0.6;

  const scopeLabels = scopes.reduce((n, s) => n + s.width + 16, 0);
  const kindLabels = scopes.reduce((n, s) => n + (s.collapsed ? 0 : s.kinds.length * KIND_ROOM), 0);
  let r1 = Math.max(SCOPE_RING, scopeLabels / (TAU * SCOPE_LABEL_SHARE) - SCOPE_LABEL_OUT);
  let r2 = Math.max(r1 + RING_STEP, kindLabels / (TAU * KIND_LABEL_SHARE) + r2Offset());
  let r3 = r2 + LEAF_STEP;
  let p: Plan | null = null;
  for (let attempt = 0; attempt < 40 && !p; attempt++) {
    p = plan(scopes, r1, r2, r3, cell, rowGap);
    if (!p) {
      r1 *= 1.2;
      r2 *= 1.2;
      r3 = Math.max(r2 + LEAF_STEP, r3 * 1.2);
    }
  }
  if (!p) return emptyLayout();

  const nodes: StarNode[] = [];
  const blank = (id: string, type: StarType): StarNode => ({
    id,
    type,
    parent: null,
    scope: null,
    branch: null,
    depth: 0,
    x: 0,
    y: 0,
    r: 0,
    angle: 0,
    name: "",
    label: null,
    entry: null,
    kind: null,
    source: null,
    count: 0,
    brightness: 1,
    forgotten: false,
    collapsed: false,
    dim: false,
    match: false,
    selected: false,
    onPath: false,
  });
  const at = (node: StarNode, radius: number, angle: number) => {
    node.x = round(radius * Math.cos(angle));
    node.y = round(radius * Math.sin(angle));
    node.angle = round(angle, 4);
  };
  const brightest = (list: MemoryEntry[]) => list.reduce((b, e) => Math.max(b, brightness(timeOf(e), newest, oldest)), 0.45);

  const root = blank(ROOT_ID, "root");
  root.r = ROOT_R;
  root.name = "记忆";
  root.count = notes.length;
  root.angle = -Math.PI / 2;
  nodes.push(root);

  /** where each note is drawn: its own star, or the cluster that holds it */
  const shownAs = new Map<string, string>();
  const leaves: StarNode[] = [];
  let theta = -Math.PI / 2 - p.scopeSpans[0] / 2 - p.gS / 2;
  let maxRow = 0;
  /** the angles each note's star must stay between (its kind's sector) */
  const wedges = new Map<string, { from: number; to: number }>();
  let hidden = 0;

  scopes.forEach((s, si) => {
    theta += p.gS / 2;
    const a0 = theta;
    const span = p.scopeSpans[si];
    theta += span + p.gS / 2;
    const scope = blank(s.id, "scope");
    scope.parent = ROOT_ID;
    scope.scope = s.id;
    scope.depth = 1;
    scope.r = SCOPE_R;
    scope.count = s.entries.length;
    scope.collapsed = s.collapsed;
    scope.name = s.global ? "全局：所有项目都适用的笔记" : s.path ? `${s.display}\n${s.path}` : s.display;
    at(scope, r1, a0 + span / 2);
    scope.label = arcLabel(s.text, s.width, r1 + SCOPE_LABEL_OUT, a0 + span / 2, span);
    nodes.push(scope);
    if (s.collapsed) {
      const cluster = blank(clusterIdOf(s.id), "cluster");
      cluster.parent = s.id;
      cluster.scope = s.id;
      cluster.branch = s.id;
      cluster.depth = 2;
      cluster.r = CLUSTER_R;
      cluster.count = s.entries.length;
      cluster.brightness = brightest(s.entries);
      cluster.name = `${s.display}：${s.entries.length} 条笔记已收起`;
      at(cluster, r2, a0 + span / 2);
      nodes.push(cluster);
      leaves.push(cluster);
      for (const e of s.entries) shownAs.set(e.id, cluster.id);
      hidden += s.entries.length;
      return;
    }
    let phi = a0;
    s.kinds.forEach((k, ki) => {
      const kspan = p.kindSpans[si][ki];
      const k0 = phi;
      phi += kspan + p.gK;
      const kind = blank(k.id, "kind");
      kind.parent = s.id;
      kind.scope = s.id;
      kind.depth = 2;
      kind.r = KIND_R;
      kind.kind = k.kind;
      kind.count = k.entries.length;
      kind.collapsed = k.collapsed;
      kind.name = `${s.display} · ${kindLabel(k.kind) || "其他"}`;
      at(kind, r2, k0 + kspan / 2);
      kind.label = stackLabel(k.text, r2, r1, k0 + kspan / 2, kspan);
      nodes.push(kind);
      if (k.collapsed) {
        const cluster = blank(clusterIdOf(k.id), "cluster");
        cluster.parent = k.id;
        cluster.scope = s.id;
        cluster.branch = k.id;
        cluster.depth = 3;
        cluster.r = CLUSTER_R;
        cluster.kind = k.kind;
        cluster.count = k.entries.length;
        cluster.brightness = brightest(k.entries);
        cluster.name = `${kind.name}：${k.entries.length} 条笔记已收起`;
        at(cluster, r3, k0 + kspan / 2);
        nodes.push(cluster);
        leaves.push(cluster);
        for (const e of k.entries) shownAs.set(e.id, cluster.id);
        hidden += k.entries.length;
        return;
      }
      let index = 0;
      for (let row = 0; index < k.entries.length; row++) {
        const radius = r3 + row * rowGap;
        const step = row < p.steps.length ? p.steps[row] : 2 * Math.asin(Math.min(1, cell / (2 * radius)));
        const room = Math.floor(kspan / step + 1e-9);
        const m = Math.min(Math.max(row === 0 ? 1 : 0, room), k.entries.length - index);
        for (let i = 0; i < m; i++, index++) {
          const e = k.entries[index];
          const star = blank(e.id, "note");
          star.parent = k.id;
          star.scope = s.id;
          star.depth = 3;
          star.entry = e;
          star.kind = k.kind;
          star.source = sourceKey(e.source);
          star.count = 1;
          star.r = starRadius(e.text);
          star.brightness = brightness(timeOf(e), newest, oldest);
          star.forgotten = e.status === "forgotten";
          star.name = noteTitle(e);
          at(star, radius, k0 + ((i + 0.5) * kspan) / m);
          nodes.push(star);
          leaves.push(star);
          wedges.set(star.id, { from: k0, to: k0 + kspan });
          shownAs.set(e.id, star.id);
        }
        if (m > 0) maxRow = Math.max(maxRow, row);
      }
    });
  });

  scatter(leaves, wedges, gap, r3 - 5, r3 + maxRow * rowGap + 5);

  // search, selection and the branch to the root
  const byId = new Map(nodes.map((n) => [n.id, n]));
  if (matchIds) {
    const hits = new Set<string>();
    for (const e of notes) {
      if (!matchIds.has(e.id)) continue;
      let id: string | null = shownAs.get(e.id) ?? null;
      while (id) {
        hits.add(id);
        id = byId.get(id)?.parent ?? null;
      }
    }
    for (const n of nodes) {
      n.match = hits.has(n.id);
      n.dim = !n.match;
    }
  }
  const visibleSelected = selectedId !== null ? (shownAs.get(selectedId) ?? null) : null;
  if (visibleSelected) {
    const own = byId.get(visibleSelected)!;
    own.selected = own.type === "note";
    let id: string | null = visibleSelected;
    while (id) {
      const n = byId.get(id);
      if (!n) break;
      n.onPath = true;
      id = n.parent;
    }
  }

  // lines from each node to its children, trimmed to the circles at both ends
  const edges: StarEdge[] = [];
  for (const n of nodes) {
    if (!n.parent) continue;
    const from = byId.get(n.parent)!;
    const dx = n.x - from.x;
    const dy = n.y - from.y;
    const length = Math.hypot(dx, dy);
    const a = from.r + (from.type === "root" ? 5 : 3);
    const b = n.r + 3;
    if (length <= a + b + 1) continue;
    const ux = dx / length;
    const uy = dy / length;
    edges.push({
      id: `${from.id}>${n.id}`,
      from: from.id,
      to: n.id,
      x1: round(from.x + ux * a),
      y1: round(from.y + uy * a),
      x2: round(n.x - ux * b),
      y2: round(n.y - uy * b),
      leaf: n.type === "note" || (n.type === "cluster" && from.type === "kind"),
      dim: n.dim,
      onPath: n.onPath && from.onPath,
    });
  }

  // labels: the branches' along their rings (made room for above), level ones for the root, clusters and notes
  const obstacles: Obstacles = { circles: [], boxes: [] };
  for (const n of nodes) obstacles.circles.push({ x: n.x, y: n.y, r: n.r + 1.5, weight: n.type === "note" ? 1 : 6 });
  for (const n of nodes) {
    if (n.label?.shape === "arc") obstacles.boxes.push(...arcBoxes(n.label, LABEL_FONT * LINE));
    if (n.label?.shape === "stack") for (const c of stackPositions(n.label, 1)) obstacles.boxes.push({ x: c.x - LABEL_FONT * 0.6, y: c.y - LABEL_FONT * 0.6, w: LABEL_FONT * 1.2, h: LABEL_FONT * 1.2 });
  }
  root.label = { shape: "flat", text: "记忆", count: String(notes.length), x: 0, y: round(ROOT_R + 13), anchor: "middle", width: round(textWidth("记忆", LABEL_FONT) + countWidth(String(notes.length)), 1), height: LABEL_FONT * LINE };
  obstacles.boxes.push(flatBox(root.label));
  for (const n of leaves) {
    if (n.type !== "cluster") continue;
    const placed = placeFlat(n, `+${n.count}`, "", LABEL_FONT, obstacles);
    n.label = placed.label;
    obstacles.boxes.push(flatBox(placed.label));
  }
  // Every title, when there are few stars: placed newest first, whatever is chosen or searched, so that neither moves
  // anything (a search only dims the titles it does not match).
  const notesShown = leaves.filter((n) => n.type === "note");
  const allTitles = options.allTitles ?? notesShown.length <= ALL_TITLES;
  if (allTitles) {
    const width = notesShown.length <= 8 ? 150 : 104;
    for (const n of [...notesShown].sort((a, b) => b.brightness - a.brightness || compare(a.id, b.id))) {
      const placed = placeFlat(n, fitText(n.name, width, NOTE_FONT), "", NOTE_FONT, obstacles);
      if (placed.score >= 6) continue;
      n.label = placed.label;
      obstacles.boxes.push(flatBox(placed.label));
    }
  }

  // what is drawn, and the box around it
  const outer = Math.max(r3, r3 + maxRow * rowGap);
  const frame = outer + FRAME_GAP;
  let minX = -frame - 6;
  let minY = -frame - 6;
  let maxX = frame + 6;
  let maxY = frame + 6;
  for (const n of nodes) {
    const halo = n.r * 2.6;
    minX = Math.min(minX, n.x - halo);
    maxX = Math.max(maxX, n.x + halo);
    minY = Math.min(minY, n.y - halo);
    maxY = Math.max(maxY, n.y + halo);
    if (n.label?.shape === "flat") {
      const b = flatBox(n.label);
      minX = Math.min(minX, b.x - 4);
      maxX = Math.max(maxX, b.x + b.w + 4);
      minY = Math.min(minY, b.y - 4);
      maxY = Math.max(maxY, b.y + b.h + 4);
    }
  }
  // The chosen star's title (where it was already written, or the best place left) is not part of the box: choosing a
  // star never rescales the chart. The chart draws it on top, inside the panel.
  const chosen = notesShown.find((n) => n.selected);
  if (chosen && !chosen.label) chosen.label = placeFlat(chosen, fitText(chosen.name, 150, NOTE_FONT), "", NOTE_FONT, obstacles).label;
  return {
    nodes,
    edges,
    bounds: { x: round(minX), y: round(minY), width: round(maxX - minX), height: round(maxY - minY) },
    rings: { scope: round(r1), kind: round(r2), leaf: round(r3), outer: round(outer), frame: round(frame), rows: maxRow + 1, rowGap: round(rowGap) },
    shown: notesShown.length,
    hidden,
    allTitles,
  };
}

/** The node to move to from `fromId` with an arrow key: the nearest one in that direction, preferring those straight ahead. */
export function neighbourOf(nodes: readonly StarNode[], fromId: string, dir: Direction): string | null {
  const from = nodes.find((n) => n.id === fromId);
  if (!from) return null;
  let best: StarNode | null = null;
  let bestScore = Infinity;
  for (const n of nodes) {
    if (n.id === from.id || n.type === "root") continue;
    const dx = n.x - from.x;
    const dy = n.y - from.y;
    const ahead = dir === "right" ? dx : dir === "left" ? -dx : dir === "down" ? dy : -dy;
    const aside = dir === "right" || dir === "left" ? Math.abs(dy) : Math.abs(dx);
    if (ahead <= 0.5 || aside > ahead * 2) continue;
    const score = ahead + aside * 2.5;
    if (score < bestScore - 1e-9 || (Math.abs(score - bestScore) <= 1e-9 && best && n.id < best.id)) {
      best = n;
      bestScore = score;
    }
  }
  return best?.id ?? null;
}

/** The SVG path a ring label runs along (left to right as read), long enough for the text drawn `scale` times LABEL_FONT. */
export function arcPath(l: ArcLabel, scale = 1): string {
  const mid = (l.from + l.to) / 2;
  const half = Math.min(Math.PI * 0.98, (l.width * scale + 10) / 2 / l.r);
  const a = l.reverse ? mid + half : mid - half;
  const b = l.reverse ? mid - half : mid + half;
  const large = half * 2 > Math.PI ? 1 : 0;
  const f = (n: number) => round(n);
  return `M${f(l.r * Math.cos(a))} ${f(l.r * Math.sin(a))}A${f(l.r)} ${f(l.r)} 0 ${large} ${l.reverse ? 0 : 1} ${f(l.r * Math.cos(b))} ${f(l.r * Math.sin(b))}`;
}

/** How big each ring's names are drawn at zoom `k` (layout px per screen px), as a multiple of LABEL_FONT: aiming at
 *  `target` screen px, the same for the names of one ring where they have the room, never past their own room or
 *  MAX_ARC_SCALE, and never below what the layout made room for unless zoomed in. */
export function ringScales(layout: Pick<StarMapLayout, "nodes">, k: number, target = 11.5): Map<string, number> {
  const want = target / (LABEL_FONT * Math.max(k, 1e-6));
  const out = new Map<string, number>();
  const fitOf = (l: StarLabel) => (l.shape === "arc" ? l.room / Math.max(1, l.width) : l.shape === "stack" ? l.maxScale : 1);
  for (const type of ["scope", "kind"] as const) {
    const ring = layout.nodes.filter((n) => n.type === type && n.label && n.label.shape !== "flat");
    const fits = ring.map((n) => fitOf(n.label!)).sort((a, b) => a - b);
    const common = Math.min(want, MAX_ARC_SCALE, Math.max(1, fits[Math.floor(fits.length * 0.25)] ?? 1));
    // rounded down: never past a label's room
    for (const n of ring) out.set(n.id, Math.floor(Math.min(common, Math.max(1, fitOf(n.label!)), want < 1 ? want : Infinity) * 1000) / 1000);
  }
  return out;
}
