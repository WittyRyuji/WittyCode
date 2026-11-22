import { useCallback, useEffect, useLayoutEffect, useMemo, useRef, useState } from "react";
import { ArrowLeft, ChevronRight, UsersRound, X } from "lucide-react";
import type { AgentInfo, Session, Usage } from "../types";
import { currentSession, useStore } from "../store";
import { AGENT_STATE, groupAgents } from "../lib/agentTeam";
import { dragonIdentity, dragonPalette } from "../lib/dragonSprites";
import { formatTokens, usageTotal } from "../lib/format";
import { AgentFlow } from "./AgentFlow";
import { DragonSprite } from "./DragonSprite";
import { TurnView } from "./Transcript";
import { MenuButton, StateDot, type MenuItem } from "./ui";

/** The conversation's team in the right rail and in the inspector. */
export function AgentBar(props: { session: Session; onClose?: () => void; inspector?: boolean }) {
  return <AgentTeam key={props.session.id} {...props} />;
}

function AgentTeam({ session, onClose, inspector }: { session: Session; onClose?: () => void; inspector?: boolean }) {
  const agents = session.agents;
  const [historyOpen, setHistoryOpen] = useState(false);
  // Keep the graph inputs stable when the session changes only because a draft was edited.
  const { active, history, flowAgents } = useMemo(() => {
    const { active, history } = groupAgents(agents ?? []);
    return { active, history, flowAgents: [...active, ...history.filter((agent) => !agent.unstartedTransfer)] };
  }, [agents]);
  const open = useStore((s) => s.agentPanel);
  const openAgent = useStore((s) => s.openAgent);
  const openFlowAgent = useCallback((id: string) => openAgent(session.id, id), [openAgent, session.id]);
  if (!agents?.length) return null;
  const modelId = session.settings.model === "fusion" ? session.context.model : session.settings.model;
  const teamTokens = inspector ? agents.reduce<number | null>((total, a) => a.tokens ? (total ?? 0) + usageTotal(a.tokens) : total, null) : null;
  const list = (items: AgentInfo[], label: string) => (
      <ul className="agent-list" aria-label={label}>
        {items.map((a) => (
          <li key={a.id}>
            <button
              type="button"
              className={`agent-chip ${a.status}${open?.agentId === a.id && open.sessionId === session.id ? " on" : ""}`}
              aria-label={`查看 Sub-Agent ${a.name} · ${AGENT_STATE[a.status].text}`}
              aria-pressed={open?.agentId === a.id && open.sessionId === session.id}
              title={a.task.split(/\r?\n/, 1)[0].trim().slice(0, 200) || a.name}
              onClick={() => openAgent(session.id, a.id)}
            >
              <DragonSprite identity={dragonIdentity(a.fusionRole)} palette={dragonPalette(dragonIdentity(a.fusionRole), a.id)} status={a.status} scale={2} />
              <span className="agent-summary">
                <span className="agent-name">{a.name}</span>
                <span className="agent-caption">{a.role || "Sub-Agent"}{a.model ? ` · ${a.model}` : ""}</span>
              </span>
              {inspector && a.tokens && <span className="agent-chip-tokens num">{formatTokens(usageTotal(a.tokens))} Token</span>}
              <span className="agent-state">{a.unstartedTransfer ? "未启动" : AGENT_STATE[a.status].text}</span>
            </button>
          </li>
        ))}
      </ul>
  );
  return (
    <section className="agentbar" aria-label="Agents Team">
      <header className="agentbar-head">
        <UsersRound size={16} aria-hidden />
        <h3 className="agentbar-title">Agents Team</h3>
        <span className="agentbar-count num">{flowAgents.length}</span>
        {teamTokens !== null && <span className="agentbar-tokens num" title="已知专家的累计 Token 用量">{formatTokens(teamTokens)} Token</span>}
        {onClose && <button type="button" className="icon-btn sm" aria-label="关闭 Agents Team" onClick={onClose}><X size={15} aria-hidden /></button>}
      </header>
      {!inspector && <AgentFlow sessionId={session.id} modelId={modelId} mark={session.mark} agents={flowAgents} onOpen={openFlowAgent} />}
      {active.length > 0 && list(active, "当前 Sub-Agents")}
      {history.length > 0 && <div className="agent-history">
        <button type="button" className="agent-history-toggle" aria-expanded={historyOpen} onClick={() => setHistoryOpen(!historyOpen)}>
          <ChevronRight size={14} className={`chev${historyOpen ? " down" : ""}`} aria-hidden />
          <span>历史专家</span><span className="num">{history.length}</span>
        </button>
        {historyOpen && list(history, "历史 Sub-Agents")}
      </div>}
    </section>
  );
}

function AgentTask({ task }: { task: string }) {
  const text = useRef<HTMLDivElement>(null);
  const [long, setLong] = useState(false);
  const [expanded, setExpanded] = useState(false);
  useLayoutEffect(() => {
    const el = text.current!;
    const measure = () => {
      const over = el.scrollHeight > 3 * parseFloat(getComputedStyle(el).lineHeight) + 1;
      setLong(over);
      if (!over) setExpanded(false);
    };
    measure();
    const observer = new ResizeObserver(measure);
    observer.observe(el);
    return () => observer.disconnect();
  }, [task, expanded]);
  return (
    <div className="agentpanel-task">
      <div id="agentpanel-task-text" ref={text} className={`agentpanel-task-text selectable ${expanded ? "expanded" : "clamped"}`}>任务：{task}</div>
      {long && <button type="button" className="linkbtn agentpanel-task-toggle" aria-expanded={expanded} aria-controls="agentpanel-task-text" onClick={() => setExpanded(!expanded)}>{expanded ? "收起" : "展开"}</button>}
    </div>
  );
}

function AgentTokens({ tokens }: { tokens: Usage }) {
  return (
    <div className="agentpanel-tokens num" aria-label="累计 Token 用量">
      <span>合计 {formatTokens(usageTotal(tokens))} Token</span>
      <span>输入 {tokens.input.toLocaleString()}</span><span>输出 {tokens.output.toLocaleString()}</span>
      {tokens.cacheWrite !== null && <span>缓存写入 {tokens.cacheWrite.toLocaleString()}</span>}
      <span>缓存读取 {tokens.cacheRead.toLocaleString()}</span>
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
  const agentId = panel?.agentId;
  const sessionId = panel?.sessionId;
  useEffect(() => {
    stick.current = true;
  }, [sessionId, agentId]);
  useEffect(() => {
    const el = scroller.current;
    if (el && stick.current) el.scrollTop = el.scrollHeight;
  }, [turns, shown, sessionId, agentId]);
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
    <aside className="agentpanel" aria-label={`Sub-Agent ${agent.name}`}>
      <div className="agentpanel-back">
        <button type="button" className="linkbtn" aria-label="切回主对话" onClick={close}><ArrowLeft size={14} aria-hidden /> 主对话</button>
        <span>Sub-Agent</span>
        <button type="button" className="icon-btn sm" aria-label="关闭 Sub-Agent 面板" onClick={close}><X size={15} aria-hidden /></button>
      </div>
      <AgentBar session={session} inspector />
      <header className="agentpanel-head">
        <StateDot mark={state.mark} size={9} />
        <div className="agentpanel-title">
          <div className="agentpanel-name">{agent.name}</div>
          {facts && <div className="agentpanel-facts">{facts}</div>}
          {agent.tokens && <AgentTokens tokens={agent.tokens} />}
        </div>
        <span className={`agent-state ${agent.status}`}>{state.text}</span>
      </header>
      {agent.task && <AgentTask key={`${session.id}::${agent.id}`} task={agent.task} />}
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
          {turns.length === 0 ? <div className="snote">{agent.fusionState === "idle" ? "等待主代理分配任务。" : agent.fusionState === "interrupted" ? "尚无可显示的运行记录；继续前请检查原专家记录与工作区。" : "正在读取它的运行…"}</div> : turns.map((t) => <TurnView key={t.id} turn={t} session={run} />)}
        </div>
      </div>
    </aside>
  );
}
