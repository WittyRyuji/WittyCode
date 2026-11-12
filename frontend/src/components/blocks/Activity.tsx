import { AlertCircle, Check, ChevronRight, Minus, X } from "lucide-react";
import type { ActivityBlock, ActivityEntry, ThinkingEntry, ToolItem } from "../../types";
import { openKey, useStore } from "../../store";
import { clip, formatDuration, groupStats, groupSummary, STATUS_LABELS, toolLabel } from "../../lib/format";
import { Spinner } from "../ui";
import { CodeBlock } from "./Markdown";
import { Diff } from "./Diff";

const DECISIONS: Record<string, string> = {
  proceed_once: "允许（仅此一次）",
  proceed_always: "允许，并始终允许低风险操作",
  allow_once: "允许（仅此一次）",
  allow_session: "允许，本会话内同类操作不再询问",
  allow_always: "允许，以后一律允许同类操作",
  allow_always_global: "允许，所有项目里以后一律允许同类操作",
  switch_bypass: "允许，并切换到完全访问",
  cancel: "拒绝",
};

function StatusIcon({ status }: { status: ToolItem["status"] | "thinking" | "thinking-live" }) {
  switch (status) {
    case "running":
    case "thinking-live":
      return <Spinner size={13} />;
    case "done":
      return <Check size={13} className="ic ok" />;
    case "error":
      return <X size={13} className="ic err" />;
    case "cancelled":
      return <Minus size={13} className="ic muted" />;
    case "awaiting_permission":
      return <AlertCircle size={13} className="ic warn" />;
    case "thinking":
      return <span className="ic-dot" />;
    default:
      return <span className="ic-dot" />;
  }
}

function Detail({ item }: { item: ToolItem }) {
  const has = item.meta || item.command || item.output || item.diffs?.length || item.todos?.length || item.decision;
  if (!has) return <div className="idetail muted-text">没有更多内容。</div>;
  const content = (
    <>
      {item.decision && <div className="imeta">审批：{DECISIONS[item.decision] ?? item.decision}</div>}
      {item.command && <CodeBlock code={item.command} language="powershell" />}
      {item.meta && !item.command && (
        <dl className="kv">
          {Object.entries(item.meta).map(([k, v]) => (
            <div key={k} className="kv-row">
              <dt>{k}</dt>
              <dd>{v}</dd>
            </div>
          ))}
        </dl>
      )}
      {item.todos && (
        <ul className="todos">
          {item.todos.map((t, i) => (
            <li key={i} className={`todo st-${t.state}`}>
              <span className="box" />
              {t.text}
            </li>
          ))}
        </ul>
      )}
      {item.diffs?.map((d, i) => <Diff key={i} diff={d} />)}
      {item.output && (
        <div className="sec">
          <div className="seclabel">输出</div>
          <pre className={`out${item.outputError ? " err" : ""}`}>{item.output}</pre>
        </div>
      )}
    </>
  );
  return (
    <div className="idetail selectable">
      {item.todos ? <details className="plan-record"><summary>计划更新记录</summary>{content}</details> : content}
    </div>
  );
}

function ToolRow({ item, sessionId }: { item: ToolItem; sessionId: string }) {
  const key = openKey(sessionId, item.id);
  const open = useStore((s) => !!s.openItems[key]);
  const toggle = useStore((s) => s.toggleItem);
  const right = item.status === "done" ? (item.seconds !== undefined ? formatDuration(item.seconds) : "") : STATUS_LABELS[item.status];
  return (
    <div className={`item st-${item.status}${open ? " open" : ""}`}>
      <button type="button" className="irow" aria-expanded={open} onClick={() => toggle(key)}>
        <ChevronRight size={13} className="chev" />
        <span className="sticon">
          <StatusIcon status={item.status} />
        </span>
        <span className="tname">{toolLabel(item.name)}</span>
        <span className="tdesc">{item.description}</span>
        <span className="tstat num">{right}</span>
      </button>
      {open && <Detail item={item} />}
    </div>
  );
}

function ThinkingRow({ entry, sessionId }: { entry: ThinkingEntry; sessionId: string }) {
  const key = openKey(sessionId, entry.id);
  const open = useStore((s) => !!s.openItems[key]);
  const toggle = useStore((s) => s.toggleItem);
  return (
    <div className={`item thinking st-${entry.complete ? "done" : "running"}${open ? " open" : ""}`}>
      <button type="button" className="irow" aria-expanded={open} onClick={() => toggle(key)}>
        <ChevronRight size={13} className="chev" />
        <span className="sticon">{entry.complete ? <span className="ic-dot" /> : <Spinner size={13} />}</span>
        <span className="tname">思考</span>
        <span className="tdesc">{clip(entry.text, 80)}</span>
        <span className="tstat num">{entry.complete ? formatDuration(entry.seconds) : "思考中"}</span>
      </button>
      {open && (
        <div className="idetail selectable">
          <div className="think">{entry.text}</div>
        </div>
      )}
    </div>
  );
}

function Entry({ e, sessionId }: { e: ActivityEntry; sessionId: string }) {
  return e.kind === "tool" ? <ToolRow item={e} sessionId={sessionId} /> : <ThinkingRow entry={e} sessionId={sessionId} />;
}

/** One quiet row per run of tool calls; click it for the steps, click a step for its input and output. */
export function ActivityView({ block, turnRunning, sessionId }: { block: ActivityBlock; turnRunning: boolean; sessionId: string }) {
  const key = openKey(sessionId, block.id);
  const open = useStore((s) => !!s.openGroups[key]);
  const toggle = useStore((s) => s.toggleGroup);
  const s = groupStats(block);
  const live = turnRunning && (!!s.active || s.thinkingNow);
  const awaiting = s.active?.status === "awaiting_permission";
  const cls = ["block", "activity", open && "open", live && "live", awaiting && "awaiting", s.errors && "has-error", s.cancelled && "has-cancel"].filter(Boolean).join(" ");
  // The worst outcome wins; a plain check mark is only for groups where every step went through.
  const icon = awaiting ? <AlertCircle size={14} className="ic warn" /> : live ? <Spinner size={14} /> : s.errors ? <AlertCircle size={14} className="ic err" /> : s.cancelled ? <Minus size={14} className="ic warn" /> : <Check size={14} className="ic muted" />;
  const now = !live ? null : s.active ? `${s.active.status === "awaiting_permission" ? "等待确认：" : s.active.status === "running" ? "正在运行：" : "准备："}${clip(`${toolLabel(s.active.name)} ${s.active.description}`.trim(), 90)}` : "正在思考…";
  return (
    <div className={cls}>
      <button type="button" className="gsum" aria-expanded={open} onClick={() => toggle(key)}>
        <ChevronRight size={14} className="chev" />
        <span className="gicon">{icon}</span>
        <span className="gtext">{groupSummary(block)}</span>
        {now && <span className="gnow">{now}</span>}
      </button>
      {open && (
        <div className="gbody">
          {block.entries.map((e) => (
            <Entry key={e.id} e={e} sessionId={sessionId} />
          ))}
        </div>
      )}
    </div>
  );
}
