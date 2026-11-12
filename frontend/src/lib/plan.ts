import type { TodoEntry, Turn } from "../types";

export interface PlanView {
  id: string;
  steps: TodoEntry[];
  completed: number;
}

/** Only structured substeps are a plan. Read the latest accepted update across this conversation, not a mention of
 *  Plan mode or an old checklist from a different tab. An explicit empty update clears the panel. */
export function latestPlan(turns: Turn[]): PlanView | null {
  for (let t = turns.length - 1; t >= 0; t--) {
    const blocks = turns[t]!.blocks;
    for (let b = blocks.length - 1; b >= 0; b--) {
      const block = blocks[b]!;
      if (block.kind !== "activity") continue;
      for (let i = block.entries.length - 1; i >= 0; i--) {
        const item = block.entries[i]!;
        if (item.kind !== "tool" || !Array.isArray(item.todos) || (item.status !== "done" && item.status !== "running")) continue;
        if (item.todos.length === 0) return null;
        const steps = item.todos.filter((s) => s && typeof s.text === "string" && s.text.trim() && ["pending", "in_progress", "completed"].includes(s.state));
        if (steps.length !== item.todos.length) continue; // incomplete streamed input must not replace the previous plan
        return { id: item.id, steps, completed: steps.filter((s) => s.state === "completed").length };
      }
    }
  }
  return null;
}
