// The commands typed into the message box with a slash. Pure: the box, the menu and Python's `session.compact` / `session.goal`
// are in Composer.tsx, store.ts and live.ts. A line is a command only when it is one word after a slash that names one of
// these (`/compact`, `/goal fix the tests`): a path such as `/usr/bin` or any other text is an ordinary message.
//
// The user's skills and custom commands (`skills.list`) are in the menu too, after these. They are not run by the page: picking
// one writes `/name ` into the box, and the message that names it goes out as any message, with the instructions of the skill
// added by Python (on every engine).

import type { Engine, SkillItem } from "../types";

export interface SlashCommand {
  name: string;
  /** shown after the name when the command takes text */
  args?: string;
  /** what it does, in a line */
  hint: string;
  /** a skill or a custom command of the user's (the others are the page's own) */
  kind?: "skill" | "command";
  /** where the user keeps it: `project` | `personal` | `plugin` */
  source?: string;
}

export const COMMANDS: SlashCommand[] = [
  { name: "compact", hint: "现在就压缩上下文：更早的对话变成摘要，完整记录还在（Devin 自己管上下文）" },
  { name: "plan", hint: "在 Auto 和 Plan 之间切换：Plan 先出方案，批准后再动手" },
  { name: "goal", args: "<目标>", hint: "给模型一个目标，让它一轮轮做下去，直到完成或需要你" },
];

export interface Slash {
  /** the word after the slash, as typed */
  name: string;
  /** the text after it (`/goal fix it` -> `fix it`) */
  args: string;
  /** still typing the name: nothing but the name so far */
  typing: boolean;
}

/** `/co` -> typing a name; `/goal fix it` -> a name and its text; `null`: not a command-shaped line. */
export function parseSlash(draft: string): Slash | null {
  const match = /^\/([A-Za-z0-9-]*)(\s[\s\S]*)?$/.exec(draft);
  if (!match) return null;
  return { name: match[1]!, args: (match[2] ?? "").trim(), typing: match[2] === undefined };
}

/** What the user's skills and custom commands (`skills.list`) look like in the menu. */
export function fromSkills(items: readonly SkillItem[]): SlashCommand[] {
  return items.map((item) => ({ name: item.name, args: item.hint || undefined, hint: item.description, kind: item.kind, source: item.source }));
}

/** The commands the menu offers for what is typed so far: those whose name starts with it, the page's own first. A skill or a
 *  command of the user's that has the name of one of the page's own is not offered: that name is the page's. */
export function suggestions(draft: string, extra: readonly SlashCommand[] = []): SlashCommand[] {
  const slash = parseSlash(draft);
  if (!slash || !slash.typing) return [];
  const typed = slash.name.toLowerCase();
  const taken = new Set(COMMANDS.map((c) => c.name));
  return [...COMMANDS, ...extra.filter((c) => !taken.has(c.name))].filter((c) => c.name.startsWith(typed));
}

export function commandNamed(name: string): SlashCommand | undefined {
  return COMMANDS.find((c) => c.name === name.toLowerCase());
}

/** What a command depends on. `engine`: the open session's, or the one the next new session would use. */
export interface Scene {
  session: boolean;
  engine: Engine;
  running: boolean;
  /** the mode is Plan: nothing may be changed, so there is no goal to work toward */
  plan: boolean;
}

/** `/goal off` and its synonyms end the goal in force instead of setting one (the same words as `goal.STOP_WORDS` in Python). */
const STOP = /^\s*(off|stop|clear|cancel|关闭|停止|取消|清除)\s*$/i;

/** Why a command cannot be run now (a sentence for the user), or `null` when it can. */
export function unavailable(name: string, scene: Scene, args = ""): string | null {
  switch (name) {
    case "compact":
      if (!scene.session) return "先打开一个会话，再压缩它的上下文。";
      if (scene.engine === "devin") return "Devin 没有提供手动压缩：它自己管上下文。";
      if (scene.running) return "本轮还在运行，结束后再压缩。";
      return null;
    case "plan":
      if (scene.engine === "codex") return "Codex 在 WittyCode 里没有 Plan 模式。";
      return null;
    case "goal":
      if (!scene.session) return "先打开一个会话，再给它一个目标。";
      if (STOP.test(args)) return null;
      if (scene.running) return "本轮还在运行，结束后再设目标。";
      if (scene.plan) return "Plan 模式只出方案、不动手，做不了目标：先用 /plan 回到 Auto。";
      if (!args.trim()) return "用法：/goal 目标，例如 /goal 让所有测试通过。";
      return null;
    default:
      return null;
  }
}
