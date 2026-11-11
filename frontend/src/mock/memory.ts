// The shared memory of the design page: a few notes and changes in the shape Python sends (desktop/core/memory/bridge.py),
// and the same rules in small: nothing is deleted, forgetting marks a note, every change has a number to undo it by.

import type { Bridge } from "../bridge";
import type { MemoryChange, MemoryEntry, MemoryKind, MemoryScope, MemoryReviewItem, MemoryState, MemoryWho } from "../types";

const MIN = 60_000;
const HOUR = 60 * MIN;
const now = Date.now();

const user: MemoryWho = { kind: "user" };
const agent = (engine: string, title?: string, rowId = "s-file-ops"): MemoryWho => ({
  kind: "agent",
  engine,
  ...(title ? { conversation: { title, rowId, projectId: "p-wittycode" } } : {}),
});

const PROJECT = "C:\\Users\\me\\code\\wittycode";

function seed(n: number, init: Partial<MemoryEntry> & Pick<MemoryEntry, "title" | "text" | "kind" | "source" | "created">): MemoryEntry {
  const scope: MemoryScope = init.scope ?? "global";
  return {
    id: `m${n}`,
    scope,
    project: scope === "project" ? PROJECT : "",
    projectName: scope === "project" ? "wittycode" : "",
    tags: [],
    updated: init.created,
    status: "active",
    rev: 1,
    editor: init.source,
    ...init,
  };
}

let entries: MemoryEntry[] = [
  seed(1, { title: "回答用中文，代码标识符保持英文", text: "和我说话用中文；变量名、函数名、命令、报错原文保持英文，不要翻译。", kind: "preference", source: user, created: now - 72 * HOUR }),
  seed(2, { title: "旧结论：Codex 数据在 ~/.codex", text: "这是旧目录。现在的 Codex 数据目录是 CODEX_HOME=E:\\tools\\codex。", kind: "fact", scope: "project", source: agent("codex", "修复缓存"), created: now - 48 * HOUR, status: "forgotten", updated: now - 10 * MIN, rev: 2 }),
  seed(3, { title: "PowerShell 5.1 不支持 && 和 ||", text: "这台机器的 shell 是 Windows PowerShell 5.1：用分号串联命令，用 $LASTEXITCODE 判断结果。", kind: "pitfall", source: agent("droid", "整理测试", "s-tests"), created: now - 26 * HOUR, tags: ["shell", "windows"] }),
  seed(4, { title: "Codex 的 rollout 文件在 E:\\tools\\codex\\sessions", text: "读线程文件要用 thread/list 给的 path，别去猜目录。时间戳是 UTC。", kind: "fact", source: agent("devin"), created: now - 3 * HOUR }),
  seed(5, { title: "改了 frontend 要先 npm run build:app", text: "界面是单文件页面，不构建的话应用和 e2e 跑的都是旧页面。构建后再跑 node scripts/check.mjs。", kind: "convention", scope: "project", source: agent("codex", "修复缓存"), created: now - 40 * MIN, tags: ["frontend"] }),
];

interface Logged {
  seq: number;
  op: MemoryChange["op"];
  id: string;
  by: MemoryWho;
  at: number;
  before: MemoryEntry | null;
}

let journal: Logged[] = [
  { seq: 1, op: "create", id: "m1", by: user, at: now - 72 * HOUR, before: null },
  { seq: 2, op: "create", id: "m2", by: agent("codex", "修复缓存"), at: now - 48 * HOUR, before: null },
  { seq: 3, op: "create", id: "m3", by: agent("droid", "整理测试", "s-tests"), at: now - 26 * HOUR, before: null },
  { seq: 4, op: "create", id: "m4", by: agent("devin"), at: now - 3 * HOUR, before: null },
  { seq: 5, op: "create", id: "m5", by: agent("codex", "修复缓存"), at: now - 40 * MIN, before: null },
  { seq: 6, op: "forget", id: "m2", by: agent("codex", "修复缓存"), at: now - 10 * MIN, before: { ...entries[1]!, status: "active", rev: 1, updated: entries[1]!.created } },
];

// What the application wrote by itself after turns (desktop/core/memory/review): already notes, in use, and waiting for
// the weekly review, the least sure first.
const DAY = 24 * HOUR;
const reviewOf = (id: string, title: string, text: string, kind: MemoryKind, score: number, why: string, explain: string[]): MemoryReviewItem => ({
  id, title, text, scope: "project", project: PROJECT, projectName: "wittycode", kind, tags: [], engine: "chatgpt", conversation: "测试一下换GPT Plan", row: "s-chatgpt", why, score, explain, at: now - 6 * MIN, suggestKeep: score >= 0.5,
});
// (the design page keeps the review apart from the list of notes below: it only shows how the review reads)
let reviewItems: MemoryReviewItem[] = [
  reviewOf("r1", "提交信息用中文", "这个项目的提交信息用中文写，标题一行，正文说明为什么改。", "convention", 0.91, "用户明确说了", ["提交", "中文"]),
  reviewOf("r2", "e2e 要整文件连跑 20 次", "改了界面或后台后，tests/test_app_e2e.py 要整文件连跑 20 次都过才算过，偶发失败多半是时序。", "pitfall", 0.74, "踩过的坑", ["坑", "e2e"]),
  reviewOf("r3", "这次先不动样式", "这个任务只改逻辑，样式先不动。", "fact", 0.31, "只对这次任务有用", ["样式"]),
];let labels = 18;
let nextReview = now + 7 * DAY;
export const MOCK_PENDING = reviewItems.length;

export const MOCK_MEMORY_LATEST = journal[journal.length - 1]!.seq;
let seen = MOCK_MEMORY_LATEST - 2;
let counter = entries.length;

const find = (id: string) => {
  const entry = entries.find((e) => e.id === id);
  if (!entry) throw new Error("没有这条记忆。");
  return entry;
};
const replace = (next: MemoryEntry) => (entries = entries.map((e) => (e.id === next.id ? next : e)));

function state(): MemoryState {
  const latest = journal[journal.length - 1]?.seq ?? 0;
  return { latest, seen, unseen: journal.filter((c) => c.seq > seen && c.by.kind === "agent").length, pending: reviewItems.length };
}

function record(op: MemoryChange["op"], before: MemoryEntry | null, after: MemoryEntry): MemoryEntry {
  journal = [...journal, { seq: (journal[journal.length - 1]?.seq ?? 0) + 1, op, id: after.id, by: user, at: Date.now(), before }];
  return after;
}

const changed = (entry: MemoryEntry) => ({ entry, ...state() });

function check(title: string, text: string): void {
  if (!title.trim()) throw new Error("记忆需要一个标题。");
  if (!text.trim()) throw new Error("记忆需要内容。");
  if (title.length > 80) throw new Error(`标题 ${title.length} 个字，请缩到 80 个字以内。`);
  if (text.length > 2000) throw new Error(`内容 ${text.length} 个字，请缩到 2000 个字以内。`);
}

export const mockMemory: Bridge["memory"] = {
  async list(filter = {}) {
    const words = (filter.query ?? "").toLowerCase().split(/\s+/).filter(Boolean);
    const status = filter.status ?? "active";
    const shown = entries
      .filter((e) => status === "all" || e.status === status)
      .filter((e) => !filter.scope || e.scope === filter.scope)
      .filter((e) => !filter.kind || e.kind === filter.kind)
      .filter((e) => !filter.project || e.project === filter.project)
      .filter((e) => !filter.engine || (e.source.kind === "agent" ? e.source.engine : "user") === filter.engine)
      .filter((e) => words.every((w) => `${e.title} ${e.text} ${e.tags.join(" ")}`.toLowerCase().includes(w)))
      .sort((a, b) => b.updated - a.updated);
    const counts: Record<string, number> = {};
    for (const e of entries) counts[e.status] = (counts[e.status] ?? 0) + 1;
    return { entries: shown, counts, ...state() };
  },
  async changes() {
    const out: MemoryChange[] = journal
      .filter((c) => c.by.kind === "agent")
      .reverse()
      .map((c) => {
        const entry = entries.find((e) => e.id === c.id);
        return { seq: c.seq, op: c.op, id: c.id, title: entry?.title ?? "", scope: entry?.scope ?? "", projectName: entry?.projectName ?? "", by: c.by, at: c.at };
      });
    return { changes: out, ...state() };
  },
  async create(note) {
    check(note.title, note.text);
    if (note.scope === "project" && !note.project) throw new Error("项目笔记要指明是哪个项目。");
    const at = Date.now();
    const entry: MemoryEntry = {
      id: `m${++counter}`,
      title: note.title.trim(),
      text: note.text.trim(),
      scope: note.scope,
      project: note.scope === "project" ? (note.project ?? "") : "",
      projectName: note.scope === "project" ? (note.project ?? "").split(/[\\/]/).pop() ?? "" : "",
      kind: note.kind ?? "fact",
      tags: note.tags ?? [],
      created: at,
      updated: at,
      status: "active",
      rev: 1,
      source: user,
      editor: user,
    };
    entries = [entry, ...entries];
    return changed(record("create", null, entry));
  },
  async update(edit) {
    const before = find(edit.id);
    const next: MemoryEntry = { ...before, ...(edit.title !== undefined ? { title: edit.title.trim() } : {}), ...(edit.text !== undefined ? { text: edit.text.trim() } : {}) };
    if (edit.kind) next.kind = edit.kind as MemoryKind;
    if (edit.tags) next.tags = edit.tags;
    if (edit.scope) {
      next.scope = edit.scope;
      next.project = edit.scope === "project" ? (edit.project ?? before.project) : "";
      next.projectName = next.project.split(/[\\/]/).pop() ?? "";
    }
    check(next.title, next.text);
    next.updated = Date.now();
    next.rev = before.rev + 1;
    next.editor = user;
    replace(next);
    return changed(record("update", before, next));
  },
  async forget(id) {
    const before = find(id);
    const next: MemoryEntry = { ...before, status: "forgotten", updated: Date.now(), rev: before.rev + 1, editor: user };
    replace(next);
    return changed(record("forget", before, next));
  },
  async restore(id) {
    const before = find(id);
    const next: MemoryEntry = { ...before, status: "active", updated: Date.now(), rev: before.rev + 1, editor: user };
    replace(next);
    return changed(record("restore", before, next));
  },
  async undo(seq) {
    const change = journal.find((c) => c.seq === seq);
    if (!change) throw new Error("没有这次改动。");
    const current = find(change.id);
    // A creation is taken back by forgetting the note; every other change by putting the note back as it was.
    const back: MemoryEntry = change.op !== "create" && change.before ? { ...change.before, updated: Date.now(), rev: current.rev + 1, editor: user } : { ...current, status: "forgotten", updated: Date.now(), rev: current.rev + 1, editor: user };
    replace(back);
    return changed(record(change.op === "create" ? "forget" : change.op === "forget" ? "restore" : change.op === "restore" ? "forget" : "update", current, back));
  },
  async markSeen(upTo) {
    const latest = journal[journal.length - 1]?.seq ?? 0;
    seen = upTo === undefined ? latest : Math.max(0, Math.min(upTo, latest));
    return state();
  },
  async review() {
    return { items: [...reviewItems].sort((a, b) => a.score - b.score), total: reviewItems.length, due: true, pending: reviewItems.length, last: now - 7 * DAY, next: now, labels, size: 50, skipped: 4 };
  },
  async reviewApply({ keep, drop }) {
    const decided = new Set([...keep, ...drop]);
    reviewItems = reviewItems.filter((i) => !decided.has(i.id));
    labels += decided.size;
    nextReview = Date.now() + 7 * DAY;
    return { kept: keep.length, dropped: drop.length, autoKept: 0, autoDropped: 0, labels, left: reviewItems.length, next: nextReview, ...state() };
  },
};
