import { useEffect, useRef } from "react";
import { X } from "lucide-react";
import type { AgentInfo, Session, SessionMark } from "../types";
import { currentSession, useStore } from "../store";
import { TurnView } from "./Transcript";
import { MenuButton, StateDot, type MenuItem } from "./ui";

/** The words for a sub-agent's state, and the dot it gets (the conversation tabs' own dots). */
export const AGENT_STATE: Record<AgentInfo["status"], { text: string; mark: SessionMark }> = {
  pending: { text: "待分配", mark: "ready" },
  working: { text: "运行中", mark: "running" },
  waiting: { text: "等你确认", mark: "attention" },
  done: { text: "已完成", mark: "ready" },
  stopped: { text: "已停止", mark: "ready" },
  failed: { text: "失败", mark: "dead" },
};

/** What the sub-agents of the conversation are doing: one chip each, above the composer. A click opens its run. */
export function AgentBar({ session }: { session: Session }) {
  const agents = session.agents ?? [];
  const open = useStore((s) => s.agentPanel);
  const openAgent = useStore((s) => s.openAgent);
  if (agents.length === 0) return null;
  return (
    <div className="agentbar" role="list" aria-label="子任务">
      <span className="agentbar-title">子任务</span>
      {agents.map((a) => (
        <button
          key={a.id}
          type="button"
          role="listitem"
          className={`agent-chip ${a.status}${open?.agentId === a.id && open.sessionId === session.id ? " on" : ""}`}
          title={a.task || a.name}
          onClick={() => openAgent(session.id, a.id)}
        >
          <StateDot mark={AGENT_STATE[a.status].mark} size={8} />
          <span className="agent-name">{a.name}</span>
          <span className="agent-state">{AGENT_STATE[a.status].text}</span>
        </button>
      ))}
    </div>
  );
}

/** The right-hand panel: one sub-agent's run, live, with its questions (an approval is answered here). */
export function AgentPanel() {
  const panel = useStore((s) => s.agentPanel);
  const turns = useStore((s) => s.agentTurns);
  const session = useStore(currentSession);
  const close = useStore((s) => s.closeAgent);
  const models = useStore((s) => s.models);
  const hidden = useStore((s) => s.hiddenModels);
  const openDialog = useStore((s) => s.openDialog);
  const transfer = useStore((s) => s.transferFusionExpert);
  const resume = useStore((s) => s.resumeFusionExpert);
  const scroller = useRef<HTMLDivElement>(null);
  const stick = useRef(true);

  const shown = !!panel && !!session && session.id === panel.sessionId;
  useEffect(() => {
    const el = scroller.current;
    if (el && stick.current) el.scrollTop = el.scrollHeight;
  }, [turns, shown]);
  useEffect(() => {
    if (!shown) return;
    const onKey = (e: KeyboardEvent) => e.key === "Escape" && close();
    document.addEventListener("keydown", onKey);
    return () => document.removeEventListener("keydown", onKey);
  }, [shown, close]);

  if (!panel || !session || !shown) return null;
  const agent = session.agents?.find((a) => a.id === panel.agentId);
  if (!agent) return null;
  const state = AGENT_STATE[agent.status];
  // The run is drawn like a conversation; its prompts are answered through this id (live.ts splits it).
  const run: Session = { ...session, id: `${session.id}::${agent.id}`, turns, workingLabel: state.text };
  const facts = [agent.role, agent.model, agent.effort && `推理 ${agent.effort}`].filter(Boolean).join(" · ");
  const choices = session.fusionPolicy ? [
    ...session.fusionPolicy.workers.map((model) => ({ model, role: "worker" as const })),
    { model: session.fusionPolicy.light, role: "light" as const },
  ] : [];
  const transferItems: MenuItem[] = choices.filter((choice) => !hidden.includes(choice.model) && (choice.model !== agent.fusionModel || choice.role !== agent.fusionRole)).map(({ model, role }) => {
    const name = models.find((item) => item.id === model)?.label ?? model;
    return { id: `${role}:${model}`, label: `${name} · ${role === "worker" ? "工作专家" : "轻量专家"}`, onSelect: () => openDialog({
      title: `把 ${agent.name} 转交给 ${name}？`,
      body: "请先检查工作区和原专家记录。确认后保留原记录，并在新专家首次接任务时转交上下文与证据；不会自动启动任务。",
      confirmLabel: "确认转交", noEnter: true, onConfirm: () => transfer(session.id, agent.id, model, role),
    }) };
  });
  const canManage = session.fusion && agent.fusionState !== "retired" && agent.fusionState !== "running" && session.mark === "ready";
  return (
    <aside className="agentpanel" aria-label={`子任务 ${agent.name}`}>
      <header className="agentpanel-head">
        <StateDot mark={state.mark} size={9} />
        <div className="agentpanel-title">
          <div className="agentpanel-name">{agent.name}</div>
          {facts && <div className="agentpanel-facts">{facts}</div>}
        </div>
        <span className={`agent-state ${agent.status}`}>{state.text}</span>
        <button type="button" className="icon-btn sm" aria-label="关闭子任务面板" onClick={close}>
          <X size={15} />
        </button>
      </header>
      {agent.task && <div className="agentpanel-task selectable">任务：{agent.task}</div>}
      {canManage && <div className="agentpanel-actions">
        {(agent.fusionState === "interrupted" || agent.fusionState === "failed") && <button type="button" className="btn small" onClick={() => openDialog({
          title: "允许原专家继续？", body: "上次结果可能不完整，请先检查工作区和执行记录。确认不会自动重做；之后由主代理决定下一步。",
          confirmLabel: "允许继续", noEnter: true, onConfirm: () => resume(session.id, agent.id),
        })}>允许继续</button>}
        {transferItems.length > 0 && <MenuButton className="btn small" items={transferItems} minWidth={240}>转交专家…</MenuButton>}
      </div>}
      <div
        ref={scroller}
        className="agentpanel-run"
        onScroll={() => {
          const el = scroller.current!;
          stick.current = el.scrollHeight - el.scrollTop - el.clientHeight < 48;
        }}
      >
        <div className="transcript agentpanel-transcript">
          {turns.length === 0 ? <div className="snote">{agent.fusionState === "idle" ? "等待主代理分配任务。" : "正在读取它的运行…"}</div> : turns.map((t) => <TurnView key={t.id} turn={t} session={run} />)}
        </div>
      </div>
    </aside>
  );
}
