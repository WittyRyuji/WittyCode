import type { ActivityBlock, Category, ToolItem } from "../types";

// Same wording and rounding as desktop/core/toolinfo.py, so the page and the old Qt UI read alike.

export const CATEGORY_LABELS: Record<Category, string> = {
  read: "读取",
  edit: "编辑",
  command: "命令",
  plan: "计划",
  ask: "提问",
  memory: "记忆",
  other: "其他",
};
const CATEGORY_ORDER: Category[] = ["read", "edit", "command", "plan", "ask", "memory", "other"];

export const TOOL_LABELS: Record<string, string> = {
  Read: "读取",
  LS: "列目录",
  Glob: "找文件",
  Grep: "搜索",
  Execute: "命令",
  Edit: "编辑",
  MultiEdit: "多处编辑",
  Create: "创建",
  ApplyPatch: "补丁",
  TodoWrite: "计划",
  AskUser: "提问",
  FetchUrl: "抓取网页",
  WebSearch: "网络搜索",
  Task: "子任务",
  Skill: "技能",
  ExitSpecMode: "提交方案",
};

export const STATUS_LABELS: Record<ToolItem["status"], string> = {
  pending: "准备中",
  awaiting_permission: "等待确认",
  running: "运行中",
  done: "完成",
  error: "失败",
  cancelled: "已取消",
};

/** The shared memory's tools: the engines each name an MCP tool their own way (`wittycode-memory___memory_write`,
 *  `wittycode-memory/memory_write`, `mcp__wittycode-memory__memory_write`), so it is the end of the name that counts. */
const MEMORY_LABELS: [string, string][] = [
  ["memory_search", "回忆"],
  ["memory_read", "读取记忆"],
  ["memory_write", "记住"],
  ["memory_update", "修改记忆"],
  ["memory_forget", "忘记"],
];

export const toolLabel = (name: string) => TOOL_LABELS[name] ?? (name.includes("wittycode-memory") ? MEMORY_LABELS.find(([tool]) => name.endsWith(tool))?.[1] : undefined) ?? name;

export function groupStats(block: ActivityBlock) {
  const tools = block.entries.filter((e): e is ToolItem => e.kind === "tool");
  const counts: Partial<Record<Category, number>> = {};
  for (const t of tools) counts[t.category] = (counts[t.category] ?? 0) + 1;
  const active = tools.find((t) => t.status === "running" || t.status === "awaiting_permission" || t.status === "pending");
  return {
    steps: tools.length,
    counts,
    errors: tools.filter((t) => t.status === "error").length,
    cancelled: tools.filter((t) => t.status === "cancelled").length,
    thinking: block.entries.filter((e) => e.kind === "thinking").length,
    active,
    thinkingNow: block.entries.some((e) => e.kind === "thinking" && !e.complete),
  };
}

export function groupSummary(block: ActivityBlock): string {
  const s = groupStats(block);
  const detail = CATEGORY_ORDER.filter((c) => s.counts[c]).map((c) => `${CATEGORY_LABELS[c]} ${s.counts[c]}`).join(" · ");
  let text = s.steps ? (detail ? `${s.steps} 步 · ${detail}` : `${s.steps} 步`) : s.thinking ? "思考" : "无操作";
  if (s.errors) text += ` · ${s.errors} 个失败`;
  if (s.cancelled) text += ` · ${s.cancelled} 个已取消`;
  return text;
}

export function formatDuration(seconds: number | undefined): string {
  if (seconds === undefined || !Number.isFinite(seconds) || seconds < 0) return "—";
  if (seconds < 9.95) return `${seconds.toFixed(1)} 秒`;
  const total = Math.round(seconds);
  if (total < 60) return `${total} 秒`;
  if (total < 3600) return `${Math.floor(total / 60)} 分 ${String(total % 60).padStart(2, "0")} 秒`;
  return `${Math.floor(total / 3600)} 小时 ${String(Math.floor((total % 3600) / 60)).padStart(2, "0")} 分`;
}

export function turnSummary(seconds: number | undefined, steps: number | undefined): string {
  const t = `用时 ${formatDuration(seconds)}`;
  return steps ? `${t} · ${steps} 步` : t;
}

/** The token figures of the status bar: 0.4K, 4K, 17.6K, 91K, 1.2M (below 100 the number itself). */
export function formatCount(v: number): string {
  const scaled = (n: number, d: number, suffix: string) => {
    const x = n / d;
    return (x < 100 ? x.toFixed(1) : x.toFixed(0)).replace(/\.0$/, "") + suffix;
  };
  if (v >= 1e6) return scaled(v, 1e6, "M");
  if (v >= 100) return scaled(v, 1e3, "K");
  return String(v);
}

/** Tokens per second: 4.2, 42.3, 428 (one decimal below 100). */
export function formatSpeed(v: number): string {
  return v >= 100 ? v.toFixed(0) : v.toFixed(1);
}

/** 25K, 872K, 1M: the figures of the context meter */
export function formatTokens(v: number): string {
  if (v >= 1_000_000 && v % 1_000_000 === 0) return `${v / 1_000_000}M`;
  if (v >= 1000) return `${Math.round(v / 1000)}K`;
  return String(v);
}

/** Every token the conversation has used: what was sent (new, cache written, cache read) and what came back. */
export function usageTotal(u: { input: number; output: number; cacheWrite: number | null; cacheRead: number }): number {
  return u.input + u.output + (u.cacheWrite ?? 0) + u.cacheRead;
}

/** Share of the input that came from the cache: Cache Read / (In + Cache Write + Cache Read). */
export function cacheHitRate(u: { input: number; cacheWrite: number | null; cacheRead: number }): number | null {
  const total = u.input + (u.cacheWrite ?? 0) + u.cacheRead;
  return total > 0 ? u.cacheRead / total : null;
}

export function formatBytes(n: number): string {
  if (n < 1024) return `${n} B`;
  if (n < 1024 * 1024) return `${(n / 1024).toFixed(1)} KB`;
  return `${(n / 1024 / 1024).toFixed(1)} MB`;
}

export function clip(text: string, max: number): string {
  return text.length > max ? `${text.slice(0, max - 1)}…` : text;
}

export const MARK_TEXT: Record<string, string> = {
  ready: "已打开",
  running: "运行中",
  attention: "等待你确认",
  dead: "已断开",
  opening: "连接中",
};
