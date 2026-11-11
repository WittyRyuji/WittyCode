import type { Turn } from "../types";

/** How much of the earlier conversation goes along when it changes engine. A starting point (an earlier client's
 *  `context_recovery` uses the same figures); the real numbers are Python's to set. */
export const HANDOFF_MAX_MESSAGES = 200;
export const HANDOFF_MAX_BYTES = 64 * 1024;

export interface Handoff {
  /** messages in the conversation: the user's and the assistant's text */
  total: number;
  /** messages that fit, newest first */
  carried: number;
  bytes: number;
  truncated: boolean;
}

/** What the new engine is given in front of the next message. Only text goes: the user's messages and the assistant's
 *  answers. Tool calls and their results, thinking, pictures and files stay behind, because neither engine can read
 *  the other's own records of them. The newest messages are kept when the budget runs out. */
export function handoffOf(turns: Turn[]): Handoff {
  const sizes: number[] = [];
  const enc = new TextEncoder();
  for (const t of turns)
    for (const b of t.blocks) {
      const text = b.kind === "user" ? b.text : b.kind === "text" ? b.markdown : "";
      if (text) sizes.push(enc.encode(text).length + 32); // 32: the role and the JSON around each message
    }
  let carried = 0;
  let bytes = 0;
  for (let i = sizes.length - 1; i >= 0; i--) {
    if (carried === HANDOFF_MAX_MESSAGES || bytes + sizes[i]! > HANDOFF_MAX_BYTES) break;
    bytes += sizes[i]!;
    carried++;
  }
  return { total: sizes.length, carried, bytes, truncated: carried < sizes.length };
}
