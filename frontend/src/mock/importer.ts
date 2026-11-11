// What other applications have to bring, for the design page: a few memory notes and conversations in the shape Python
// sends (desktop/core/importers, AppCore `import.*`). Importing here changes nothing but the page.

import type { Bridge } from "../bridge";
import type { ImportMemory, ImportSession, ImportSource } from "../types";

const HOUR = 3_600_000;
const now = Date.now();
const PROJECT = "C:\\Users\\me\\code\\wittycode";

const sources: ImportSource[] = [
  { app: "claude", label: "Claude Code", available: true, memories: 4, sessions: 3 },
  { app: "codex", label: "Codex", available: true, memories: 3, sessions: 4 },
  { app: "devin", label: "Devin", available: true, memories: 1, sessions: 2 },
  { app: "factory", label: "Factory（Droid）", available: false, memories: 0, sessions: 0 },
];

const note = (n: number, init: Partial<ImportMemory> & Pick<ImportMemory, "app" | "title" | "text" | "origin">): ImportMemory => ({
  key: `im${n}`,
  scope: "global",
  project: "",
  projectName: "",
  kind: "fact",
  tags: [],
  exists: false,
  ...init,
});

const memories: ImportMemory[] = [
  note(1, { app: "claude", title: "回答用中文", text: "和我说话用中文；代码标识符、命令和报错原文保持英文。", kind: "preference", origin: "~/.claude/CLAUDE.md › 语言", tags: ["claude", "语言"] }),
  note(2, { app: "claude", title: "提交前跑测试", text: "提交之前必须跑完整的测试，不要只跑改到的那一个文件。", kind: "convention", origin: "~/.claude/CLAUDE.md › 工作方式", tags: ["claude", "测试"], exists: true }),
  note(3, { app: "claude", title: "构建界面", text: "改了 frontend 要先 npm run build:app，应用跑的是构建出来的单文件页面。", kind: "convention", scope: "project", project: PROJECT, projectName: "wittycode", origin: "wittycode/CLAUDE.md › 命令", tags: ["claude", "frontend"] }),
  note(4, { app: "claude", title: "e2e 偶发失败多半是时序", text: "tests/test_app_e2e.py 偶发失败时先想时序：等页面稳定再断言，不要加固定 sleep。", kind: "pitfall", scope: "project", project: PROJECT, projectName: "wittycode", origin: "~/.claude/projects/wittycode/memory/MEMORY.md › e2e", tags: ["claude", "e2e"] }),
  note(5, { app: "codex", title: "CODEX_HOME 在 E:\\tools\\codex", text: "这台机器的 Codex 数据目录是 CODEX_HOME=E:\\tools\\codex，不是 ~/.codex。", kind: "fact", origin: "E:\\tools\\codex\\memories\\MEMORY.md › 环境", tags: ["codex"] }),
  note(6, { app: "codex", title: "rollout 文件按日期分目录", text: "sessions/YYYY/MM/DD/rollout-*.jsonl，时间戳是 UTC。", kind: "fact", origin: "E:\\tools\\codex\\memories\\MEMORY.md › 文件", tags: ["codex"] }),
  note(7, { app: "codex", title: "PowerShell 5.1 不支持 && 和 ||", text: "用分号串联命令，用 $LASTEXITCODE 判断结果。", kind: "pitfall", origin: "E:\\tools\\codex\\memories\\memory_summary.md › shell", tags: ["codex", "shell"] }),
  note(8, { app: "devin", title: "swe-2 是免费模型", text: "做探针时用 swe-2，不花额度。", kind: "preference", origin: "%APPDATA%\\devin\\summaries\\witty-dream.md › 模型", tags: ["devin"] }),
];

const sessions: ImportSession[] = [
  { key: "is1", app: "claude", title: "重构 session_manager 的上下文读取", project: PROJECT, projectName: "wittycode", started: now - 30 * HOUR, updated: now - 26 * HOUR, messages: 42, imported: false },
  { key: "is2", app: "claude", title: "demo-sandbox 测试项目", project: "C:\\Users\\me\\code\\demo-sandbox", projectName: "demo-sandbox", started: now - 80 * HOUR, updated: now - 79 * HOUR, messages: 12, imported: true },
  { key: "is3", app: "claude", title: "（无标题）", project: "C:\\Users\\me", projectName: "me", started: now - 200 * HOUR, updated: now - 199 * HOUR, messages: 6, imported: false },
  { key: "is4", app: "codex", title: "修复缓存读取", project: PROJECT, projectName: "wittycode", started: now - 5 * HOUR, updated: now - 4 * HOUR, messages: 28, imported: false },
  { key: "is5", app: "codex", title: "批量导入性能分析", project: "C:\\Users\\me\\code\\inventory-api", projectName: "inventory-api", started: now - 60 * HOUR, updated: now - 50 * HOUR, messages: 64, imported: false },
  { key: "is6", app: "devin", title: "测试 SWE2 session", project: PROJECT, projectName: "wittycode", started: now - 70 * HOUR, updated: now - 69 * HOUR, messages: 9, imported: false },
];

const inApps = <T extends { app: string }>(items: T[], apps: string[]) => items.filter((i) => apps.includes(i.app));

export const mockImporter: Bridge["importer"] = {
  async scan() {
    return { sources };
  },
  async memories(apps) {
    return { items: inApps(memories, apps) };
  },
  async sessions(apps) {
    return { items: inApps(sessions, apps) };
  },
  async commit(wanted, picked) {
    const notes = memories.filter((m) => wanted.includes(m.key) && !m.exists).map((m) => ({ key: m.key, id: `imported-${m.key}`, title: m.title }));
    const skipped = memories.filter((m) => wanted.includes(m.key) && m.exists).map((m) => ({ key: m.key, title: m.title, reason: "已经有一条相同的笔记" }));
    const made = sessions.filter((s) => picked.includes(s.key)).map((s) => ({ key: s.key, id: `cg-${s.key}`, title: s.title, row: `cg:${s.key}` }));
    for (const s of sessions) if (picked.includes(s.key)) s.imported = true;
    return { notes, sessions: made, skipped, latest: 0, unseen: 0, seen: 0, pending: 0 };
  },
  async undo() {
    return { latest: 0, unseen: 0, seen: 0, pending: 0 };
  },
};
