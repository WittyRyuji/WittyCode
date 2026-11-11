// How the shared memory's notes and changes read on the page. Pure functions (the unit script runs them as they are).

import type { MemoryChange, MemoryEntry, MemoryKind, MemoryWho } from "../types";

export const KINDS: { id: MemoryKind; label: string; hint: string }[] = [
  { id: "preference", label: "偏好", hint: "你喜欢怎样的回答、工具和风格" },
  { id: "convention", label: "约定", hint: "这个项目的做法：命令、目录、规范" },
  { id: "pitfall", label: "坑", hint: "踩过的坑，下次别再踩" },
  { id: "fact", label: "事实", hint: "环境和项目的事实" },
];

export const kindLabel = (kind: string) => KINDS.find((k) => k.id === kind)?.label ?? kind;

const ENGINE_NAMES: Record<string, string> = { droid: "Droid", codex: "Codex", devin: "Devin", chatgpt: "ChatGPT Plan", claude: "Claude Code", factory: "Factory Droid" };

export const engineName = (engine: string) => ENGINE_NAMES[engine] ?? (engine || "代理");

/** "我" / "Codex" / "应用整理" / "未知": who wrote or changed a note ("应用整理": WittyCode itself, after a turn). */
export function whoName(who: MemoryWho): string {
  if (who.kind === "user") return "我";
  if (who.kind === "app") return "应用整理";
  if (who.kind === "agent") return engineName(who.engine);
  return "未知";
}

/** The conversation an agent worked in when it wrote, when WittyCode knows which one it was (a row it can open). */
export function whoConversation(who: MemoryWho): { title: string; rowId: string } | null {
  return who.kind === "agent" && who.conversation && who.conversation.rowId ? { title: who.conversation.title, rowId: who.conversation.rowId } : null;
}

const VERBS: Record<MemoryChange["op"], string> = { create: "记住了", update: "修改了", forget: "忘记了", restore: "恢复了" };

export const changeVerb = (op: MemoryChange["op"]) => VERBS[op] ?? op;

/** A change the user has not looked at yet (the page marks all up to `seen` as looked at). */
export const isUnseen = (change: Pick<MemoryChange, "seq">, seen: number) => change.seq > seen;

/** Where a note applies: "全局", or the project folder's name. */
export const scopeLabel = (entry: Pick<MemoryEntry, "scope" | "projectName">) => (entry.scope === "global" ? "全局" : entry.projectName || "项目");

/** "Codex 写入" / "我写入" / "Codex 写入，我改过": the source line under a note's title. */
export function sourceLine(entry: Pick<MemoryEntry, "source" | "editor" | "rev">): string {
  const wrote = `${named(entry.source)}写入`;
  if (entry.rev <= 1) return wrote;
  const sameEditor = entry.editor.kind === entry.source.kind && (entry.editor.kind !== "agent" || (entry.source.kind === "agent" && entry.editor.engine === entry.source.engine));
  return sameEditor ? wrote : `${wrote}，${named(entry.editor)}改过`;
}

/** A Latin name in front of Chinese gets a space (`Codex 写入`), 我 does not (`我写入`). */
const named = (who: MemoryWho) => {
  const name = whoName(who);
  return /[A-Za-z]$/.test(name) ? `${name} ` : name;
};

/** The same folder, whichever way it is written (Python keeps a project's path as `d:\work\app`; the sidebar's id may
 *  carry the capitals or slashes the user typed). */
export const samePath = (a: string, b: string) => a.replace(/[\\/]+$/, "").replace(/\//g, "\\").toLowerCase() === b.replace(/[\\/]+$/, "").replace(/\//g, "\\").toLowerCase();

/** Tags typed in one field: separated by commas (Chinese ones too) or spaces, no empty ones, no repeats. */
export function parseTags(text: string): string[] {
  const out: string[] = [];
  for (const raw of text.split(/[,，、\s]+/)) {
    const tag = raw.trim();
    if (tag && !out.includes(tag)) out.push(tag);
  }
  return out;
}
