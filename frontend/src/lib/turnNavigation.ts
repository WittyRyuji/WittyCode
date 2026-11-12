import type { Turn } from "../types";

export interface TurnLink {
  id: string;
  label: string;
  status: Turn["status"];
}

/** One marker per actual turn, not per tool call or steered follow-up inside it. */
export function turnLinks(turns: Turn[]): TurnLink[] {
  return turns.map((turn, index) => {
    const user = turn.blocks.find((block) => block.kind === "user" && !block.steer);
    let label = "";
    if (user?.kind === "user") label = user.text.trim() || user.attachments?.map((item) => item.name).join("、") || "附件消息";
    if (!label) {
      const block = turn.blocks.find((item) => item.kind === "switch" || item.kind === "notice" || item.kind === "text" || item.kind === "error");
      if (block?.kind === "switch") label = `切换到 ${block.to.model}`;
      else if (block?.kind === "notice") label = block.text;
      else if (block?.kind === "text") label = block.markdown;
      else if (block?.kind === "error") label = block.message;
    }
    label = label.replace(/\s+/g, " ").trim() || `第 ${index + 1} 轮`;
    const chars = Array.from(label);
    return { id: turn.id, label: chars.length > 100 ? `${chars.slice(0, 100).join("")}…` : label, status: turn.status };
  });
}

/** Last turn whose top has crossed the reading line; the bottom always highlights the latest turn. */
export function activeTurnAt(tops: number[], readingLine: number, atEnd = false): number {
  if (!tops.length) return -1;
  if (atEnd) return tops.length - 1;
  let low = 0;
  let high = tops.length;
  while (low < high) {
    const middle = (low + high) >>> 1;
    if (tops[middle]! <= readingLine) low = middle + 1;
    else high = middle;
  }
  return Math.max(0, low - 1);
}
