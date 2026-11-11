import { useEffect, useRef } from "react";
import { X } from "lucide-react";
import type { AgentInfo, Session, SessionMark } from "../types";
import { currentSession, useStore } from "../store";
import { TurnView } from "./Transcript";
import { StateDot } from "./ui";

/** The words for a sub-agent's state, and the dot it gets (the conversation tabs' own dots). */
export const AGENT_STATE: Record<AgentInfo["status"], { text: string; mark: SessionMark }> = {
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
      <div
        ref={scroller}
        className="agentpanel-run"
        onScroll={() => {
          const el = scroller.current!;
          stick.current = el.scrollHeight - el.scrollTop - el.clientHeight < 48;
        }}
      >
        <div className="transcript agentpanel-transcript">
          {turns.length === 0 ? <div className="snote">正在读取它的运行…</div> : turns.map((t) => <TurnView key={t.id} turn={t} session={run} />)}
        </div>
      </div>
    </aside>
  );
}
