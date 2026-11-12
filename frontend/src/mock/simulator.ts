// Stands in for the engine in the review page: answers a sent message with a thinking step, a read, an edit that may ask
// for approval, and a streamed answer, so the live states (spinner, pulsing dot, cursor, approval card) can be seen.

import { useStore } from "../store";
import type { ActivityBlock, ActivityEntry, Block, PermissionPrompt, ThinkingEntry, ToolItem, Turn } from "../types";
import { asksFirst, CONTEXT_DIFF, SIM_ANSWER } from "./data";
import { engineOfSession } from "../lib/models";

interface Run {
  aborted: boolean;
  turnId: string;
  settle?: (value: string) => void;
  promptId?: string;
}

const runs = new Map<string, Run>();
let n = 0;
const nid = (p: string) => `${p}-sim-${++n}`;
const sleep = (ms: number) => new Promise<void>((r) => window.setTimeout(r, ms));
const st = () => useStore.getState();

function setWorking(sessionId: string, label: string) {
  st().patchSession(sessionId, (s) => ({ ...s, workingLabel: label }));
}

function patchBlock(sessionId: string, turnId: string, blockId: string, fn: <B extends Block>(b: B) => B) {
  st().patchTurn(sessionId, turnId, (t) => ({ ...t, blocks: t.blocks.map((b) => (b.id === blockId ? fn(b) : b)) }));
}

function patchEntry(sessionId: string, turnId: string, groupId: string, entryId: string, fn: (e: ActivityEntry) => ActivityEntry) {
  patchBlock(sessionId, turnId, groupId, (b) => {
    const g = b as unknown as ActivityBlock;
    return { ...g, entries: g.entries.map((e) => (e.id === entryId ? fn(e) : e)) } as unknown as typeof b;
  });
}

function addBlock(sessionId: string, turnId: string, block: Block) {
  st().patchTurn(sessionId, turnId, (t) => ({ ...t, blocks: [...t.blocks, block] }));
}

function stats(turn: Turn) {
  return turn.blocks.reduce((sum, b) => sum + (b.kind === "activity" ? b.entries.filter((e) => e.kind === "tool").length : 0), 0);
}

export function runTurn(sessionId: string) {
  const s = st().sessions[sessionId]!;
  const turnId = nid("turn");
  const run: Run = { aborted: false, turnId };
  runs.set(sessionId, run);
  const startedAt = performance.now();

  const attachments = s.attachments;
  const text = s.draft.trim() || "请看一下这些附件。";
  // After a change of engine this message goes out with the earlier conversation's text in front of it.
  const handoff = s.handoff ? { carried: s.handoff.carried, truncated: s.handoff.truncated } : undefined;
  const turn: Turn = {
    id: turnId,
    status: "running",
    prompts: [],
    blocks: [{ kind: "user", id: nid("u"), text, attachments: attachments.length ? attachments : undefined, handoff }],
  };
  st().patchSession(sessionId, (x) => ({
    ...x,
    mark: "running",
    workingLabel: "正在思考…",
    draft: "",
    handoff: undefined,
    attachments: [],
    title: x.turns.length === 0 && x.title === "新会话" ? text.slice(0, 40) : x.title,
    turns: [...x.turns, turn],
  }));

  void (async () => {
    const alive = () => !run.aborted;
    const g: ActivityBlock = { kind: "activity", id: nid("g"), entries: [] };
    const think: ThinkingEntry = { kind: "thinking", id: nid("th"), complete: false, text: "先看看相关文件，再决定怎么改。" };
    g.entries.push(think);
    addBlock(sessionId, turnId, g);
    await sleep(1400);
    if (!alive()) return;
    patchEntry(sessionId, turnId, g.id, think.id, (e) => ({ ...(e as ThinkingEntry), complete: true, seconds: 1.4 }));

    const read: ToolItem = {
      kind: "tool", id: nid("t"), name: "Read", category: "read", description: "desktop/core/context.py", status: "running",
      meta: { file_path: "desktop/core/context.py" },
    };
    setWorking(sessionId, "正在运行：读取 desktop/core/context.py");
    patchBlock(sessionId, turnId, g.id, (b) => ({ ...(b as unknown as ActivityBlock), entries: [...(b as unknown as ActivityBlock).entries, read] }) as unknown as typeof b);
    await sleep(900);
    if (!alive()) return;
    patchEntry(sessionId, turnId, g.id, read.id, (e) => ({ ...(e as ToolItem), status: "done", seconds: 0.9, output: "(73 行)" }));

    const lead: Block = { kind: "text", id: nid("x"), markdown: "", streaming: true };
    addBlock(sessionId, turnId, lead);
    setWorking(sessionId, "正在回答…");
    const leadText = "我先看了 `context.py`，改动很小，下面直接改。";
    for (let i = 0; i < leadText.length && alive(); i += 3) {
      patchBlock(sessionId, turnId, lead.id, (b) => ({ ...b, markdown: leadText.slice(0, i + 3) }));
      await sleep(35);
    }
    if (!alive()) return;
    patchBlock(sessionId, turnId, lead.id, (b) => ({ ...b, streaming: false }));

    const edit: ToolItem = {
      kind: "tool", id: nid("t"), name: "Edit", category: "edit", description: "desktop/core/context.py", status: "pending",
      diffs: [CONTEXT_DIFF],
    };
    const g2: ActivityBlock = { kind: "activity", id: nid("g"), entries: [edit] };
    addBlock(sessionId, turnId, g2);
    await sleep(500);
    if (!alive()) return;

    let allowed = true;
    const live = st().sessions[sessionId]!;
    if (asksFirst(engineOfSession(st().models, live), live.settings.autonomy)) {
      const prompt: PermissionPrompt = {
        kind: "permission",
        id: nid("perm"),
        status: "pending",
        actions: [{ title: "编辑文件 desktop/core/context.py", toolName: "Edit", path: "desktop/core/context.py", impact: "medium", diffs: [CONTEXT_DIFF] }],
        options: [
          { value: "proceed_once", label: "允许（仅此一次）", tone: "primary" },
          { value: "proceed_always", label: "允许，并始终允许低风险操作", tone: "normal" },
          { value: "cancel", label: "拒绝", tone: "danger" },
        ],
      };
      patchEntry(sessionId, turnId, g2.id, edit.id, (e) => ({ ...(e as ToolItem), status: "awaiting_permission" }));
      st().patchTurn(sessionId, turnId, (t) => ({ ...t, prompts: [...t.prompts, prompt] }));
      st().patchSession(sessionId, (x) => ({ ...x, mark: "attention", workingLabel: "等待你的确认" }));
      run.promptId = prompt.id;
      const answer = await new Promise<string>((resolve) => {
        run.settle = resolve;
      });
      if (!alive()) return;
      allowed = answer !== "cancel";
      patchEntry(sessionId, turnId, g2.id, edit.id, (e) => ({ ...(e as ToolItem), decision: answer }));
      st().patchSession(sessionId, (x) => ({ ...x, mark: "running" }));
    }

    if (allowed) {
      patchEntry(sessionId, turnId, g2.id, edit.id, (e) => ({ ...(e as ToolItem), status: "running" }));
      setWorking(sessionId, "正在运行：编辑 desktop/core/context.py");
      await sleep(1100);
      if (!alive()) return;
      patchEntry(sessionId, turnId, g2.id, edit.id, (e) => ({ ...(e as ToolItem), status: "done", seconds: 1.1 }));
    } else {
      patchEntry(sessionId, turnId, g2.id, edit.id, (e) => ({ ...(e as ToolItem), status: "cancelled" }));
    }

    const answer: Block = { kind: "text", id: nid("x"), markdown: "", streaming: true };
    addBlock(sessionId, turnId, answer);
    setWorking(sessionId, "正在回答…");
    const full = allowed ? SIM_ANSWER : "好的，我没有修改文件。需要的话告诉我换个做法。";
    for (let i = 0; i < full.length && alive(); i += 4) {
      patchBlock(sessionId, turnId, answer.id, (b) => ({ ...b, markdown: full.slice(0, i + 4) }));
      await sleep(30);
    }
    if (!alive()) return;
    patchBlock(sessionId, turnId, answer.id, (b) => ({ ...b, streaming: false }));
    finish(sessionId, turnId, "done", (performance.now() - startedAt) / 1000);
  })();
}

function finish(sessionId: string, turnId: string, status: Turn["status"], seconds: number) {
  runs.delete(sessionId);
  st().patchTurn(sessionId, turnId, (t) => ({
    ...t,
    status,
    seconds: Math.round(seconds * 10) / 10,
    steps: stats(t),
    blocks: t.blocks.map((b) =>
      b.kind === "activity"
        ? {
            ...b,
            entries: b.entries.map((e) =>
              e.kind === "tool" && (e.status === "running" || e.status === "pending" || e.status === "awaiting_permission")
                ? { ...e, status: "cancelled" as const }
                : e.kind === "thinking" && !e.complete
                  ? { ...e, complete: true }
                  : e,
            ),
          }
        : b.kind === "text"
          ? { ...b, streaming: false }
          : b,
    ),
    prompts: t.prompts.map((p) => (p.status === "pending" ? { ...p, status: "resolved" as const } : p)),
  }));
  st().patchSession(sessionId, (x) => {
    const used = (x.context.used ?? 14_000) + 3_400;
    // The first turn of a session, or of the new engine after a switch, starts with a cold cache: all of the input is
    // new, none of it is read back. Devin reports no cache writes at all.
    const cold = x.usage === null;
    const engine = engineOfSession(st().models, x);
    const u = x.usage ?? { input: 0, output: 0, cacheWrite: engine === "devin" ? 0 : null, cacheRead: 0 };
    return {
      ...x,
      mark: "ready",
      workingLabel: "就绪",
      modified: "刚刚",
      context: { ...x.context, used },
      speed: { tps: 51.8, tokens: 498, seconds: 9.6, first: 1.2, genTps: 59.2 },
      usage: {
        input: u.input + (cold ? 9_800 : 394),
        output: u.output + 498,
        cacheWrite: u.cacheWrite === null ? null : u.cacheWrite + (cold ? 6_100 : 1_150),
        cacheRead: u.cacheRead + (cold ? 0 : 18_400),
      },
    };
  });
}

export function cancelRun(sessionId: string, interrupt = false) {
  const run = runs.get(sessionId);
  if (!run) return;
  run.aborted = true;
  run.settle?.("cancel");
  if (interrupt) st().say("已停止当前任务。");
  finish(sessionId, run.turnId, "interrupted", 4);
}

export function settlePrompt(sessionId: string, promptId: string, value: string) {
  const run = runs.get(sessionId);
  st().patchSession(sessionId, (s) => ({
    ...s,
    turns: s.turns.map((t) => ({
      ...t,
      prompts: t.prompts.map((p) =>
        p.id === promptId ? (p.kind === "permission" ? { ...p, status: "resolved" as const, selected: value } : { ...p, status: "resolved" as const }) : p,
      ),
    })),
  }));
  if (run?.promptId === promptId) run.settle?.(value);
  else {
    // The prompts that are part of the fixed demo sessions: let the session carry on.
    st().patchSession(sessionId, (s) => ({ ...s, mark: "running", workingLabel: "正在运行…" }));
    window.setTimeout(() => {
      st().patchSession(sessionId, (s) => ({ ...s, mark: "ready", workingLabel: "就绪" }));
      st().patchSession(sessionId, (s) => ({
        ...s,
        turns: s.turns.map((t) =>
          t.prompts.some((p) => p.id === promptId)
            ? { ...t, status: "done" as const, seconds: 6, blocks: t.blocks.map((b) => (b.kind === "activity" ? { ...b, entries: b.entries.map((e) => (e.kind === "tool" && e.status === "awaiting_permission" ? { ...e, status: "done" as const } : e)) } : b)) }
            : t,
        ),
      }));
    }, 1200);
  }
}
