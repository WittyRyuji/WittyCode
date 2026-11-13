import type { ModelChoice } from "../types";

export interface UsageSlice {
  id: string;
  label: string;
  tokens: number;
  share: number;
  color: string;
}

const UNKNOWN_COLOR = "#72777b";

/** Soft pastel colors inspired by warm-flame, night-fade and winter-neva palettes. */
function pastelColor(hue: number, saturation = 64, lightness = 77): string {
  const s = saturation / 100, l = lightness / 100;
  const a = s * Math.min(l, 1 - l);
  const channel = (n: number) => {
    const k = (n + hue / 30) % 12;
    return Math.round(255 * (l - a * Math.max(-1, Math.min(k - 3, 9 - k, 1)))).toString(16).padStart(2, "0");
  };
  return `#${channel(0)}${channel(8)}${channel(4)}`;
}

const COLORS = ["#ff9a9e", "#a18cd1", "#a1c4fd", "#84dcb7", "#ead58f", "#eab4de", "#91d5e3", "#b6d696"];

export function usageGradient(color: string): [string, string] {
  const lighten = (amount: number) => `#${color.slice(1).match(/../g)!.map((part) => Math.round(parseInt(part, 16) * (1 - amount) + 255 * amount).toString(16).padStart(2, "0")).join("")}`;
  return [lighten(0.42), color];
}
function modelColors(ids: string[]): Map<string, string> {
  const colors = new Map<string, string>();
  const used = new Set([UNKNOWN_COLOR]);
  let extra = 0;
  // Allocate by ID, never by token rank or catalog label. Resolve collisions instead of wrapping the palette.
  for (const id of [...ids].sort()) {
    let hash = 0;
    for (const ch of id) hash = (Math.imul(hash, 31) + ch.charCodeAt(0)) >>> 0;
    let color: string | undefined;
    for (let offset = 0; offset < COLORS.length; offset++) {
      const candidate = COLORS[(hash + offset) % COLORS.length]!;
      if (!used.has(candidate)) { color = candidate; break; }
    }
    while (!color) {
      const candidate = pastelColor((++extra * 137.508) % 360, 52 + Math.floor(extra / 36) % 3 * 6, 74 + Math.floor(extra / 108) % 3 * 3);
      if (!used.has(candidate)) color = candidate;
    }
    used.add(color);
    colors.set(id, color);
  }
  return colors;
}

/** Shares use the very same total as the status bar, including cached input. Missing history is never assigned to the
 *  selected model. An inconsistent breakdown is unknown, not rescaled into seemingly exact percentages. */
export function usageSlices(total: number, byModel: Record<string, number> = {}, models: ModelChoice[] = []): UsageSlice[] {
  if (!Number.isFinite(total) || total <= 0) return [];
  const known = Object.entries(byModel).filter(([id, n]) => id && Number.isFinite(n) && n >= 1).map(([id, n]) => [id, Math.floor(n)] as const);
  const counted = known.reduce((sum, [, n]) => sum + n, 0);
  const parts: [string, number][] = counted > total ? [] : known.map(([id, n]) => [id, n]);
  const rest = total - (counted > total ? 0 : counted);
  if (rest > 0) parts.push(["", rest]);
  const colors = modelColors(parts.map(([id]) => id).filter(Boolean));
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
    color: id ? colors.get(id)! : UNKNOWN_COLOR,
  }));
}

export const usagePercent = (share: number) => share > 0 && share < 0.001 ? "<0.1%" : `${(share * 100).toFixed(1)}%`;
