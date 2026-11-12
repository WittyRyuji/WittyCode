import type { ModelChoice } from "../types";

export interface UsageSlice {
  id: string;
  label: string;
  tokens: number;
  share: number;
  color: string;
}

const COLORS = ["#56b4e9", "#e69f00", "#009e73", "#cc79a7", "#d55e00", "#0072b2", "#b3a600", "#7d75d8"];
const colorOf = (id: string) => {
  let hash = 0;
  for (const ch of id) hash = (Math.imul(hash, 31) + ch.charCodeAt(0)) >>> 0;
  return COLORS[hash % COLORS.length]!;
};

/** Shares use the very same total as the status bar, including cached input. Missing history is never assigned to the
 *  selected model. An inconsistent breakdown is unknown, not rescaled into seemingly exact percentages. */
export function usageSlices(total: number, byModel: Record<string, number> = {}, models: ModelChoice[] = []): UsageSlice[] {
  if (!Number.isFinite(total) || total <= 0) return [];
  const known = Object.entries(byModel).filter(([id, n]) => id && Number.isFinite(n) && n >= 1).map(([id, n]) => [id, Math.floor(n)] as const);
  const counted = known.reduce((sum, [, n]) => sum + n, 0);
  const parts: [string, number][] = counted > total ? [] : known.map(([id, n]) => [id, n]);
  const rest = total - (counted > total ? 0 : counted);
  if (rest > 0) parts.push(["", rest]);
  const labelOf = (id: string) => id ? models.find((m) => m.id === id)?.label ?? id.replace(/^(chatgpt|devin):/, "") : "未归属";
  const label = (id: string) => {
    const name = labelOf(id);
    if (!id || parts.filter(([other]) => labelOf(other) === name).length < 2) return name;
    const source = id.startsWith("devin:") ? "Devin" : id.startsWith("chatgpt:") ? "ChatGPT Plan" : id;
    return `${name} · ${source}`;
  };
  return parts.sort((a, b) => Number(!a[0]) - Number(!b[0]) || b[1] - a[1] || a[0].localeCompare(b[0])).map(([id, tokens]) => ({
    id,
    label: label(id),
    tokens,
    share: tokens / total,
    color: id ? colorOf(id) : "#8b8b8b",
  }));
}

export const usagePercent = (share: number) => share > 0 && share < 0.001 ? "<0.1%" : `${(share * 100).toFixed(1)}%`;
