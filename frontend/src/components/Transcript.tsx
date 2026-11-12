import { useCallback, useEffect, useLayoutEffect, useMemo, useRef, useState } from "react";
import { ArrowDown, ArrowRightLeft, RotateCw, X } from "lucide-react";
import type { Block, Session, Turn } from "../types";
import { useStore } from "../store";
import { turnSummary } from "../lib/format";
import { SUGGESTIONS } from "../lib/vocab";
import { Markdown } from "./blocks/Markdown";
import { ActivityView } from "./blocks/Activity";
import { PermissionCard, QuestionCard } from "./blocks/Prompts";
import { SentAttachments } from "./Attachments";
import { TurnNav } from "./TurnNav";
import { activeTurnAt, turnLinks } from "../lib/turnNavigation";

function UserView({ block }: { block: Extract<Block, { kind: "user" }> }) {
  const long = block.text.length > 520 || block.text.split("\n").length > 9;
  const [expanded, setExpanded] = useState(false);
  return (
    <div className={`block user${long && !expanded ? " clamp" : ""}${block.steer ? " steer" : ""}${block.pending ? " pending" : ""}`}>
      <div className="who">{block.steer ? (block.pending ? "你 · 引导（等当前这一步结束后生效）" : "你 · 引导") : (block.who ?? "你")}</div>
      {block.attachments && <SentAttachments items={block.attachments} />}
      <div className="body selectable">{block.text}</div>
      {block.handoff && (
        <div className="handoff-note">
          <ArrowRightLeft size={11} /> 附带了之前 {block.handoff.carried} 条对话的文字{block.handoff.truncated ? "（节选）" : ""}
        </div>
      )}
      {long && (
        <button type="button" className="linkbtn" onClick={() => setExpanded((v) => !v)}>
          {expanded ? "收起" : "展开全文"}
        </button>
      )}
    </div>
  );
}

function ErrorView({ block, sessionId }: { block: Extract<Block, { kind: "error" }>; sessionId: string }) {
  const reconnect = useStore((s) => s.reconnect);
  return (
    <div className={`block error${block.resolved ? " resolved" : ""}`} role="alert">
      <X size={15} className="eicon" />
      <div className="etext selectable">{block.message}</div>
      {block.recoverable && !block.resolved && (
        <button type="button" className="btn small" onClick={() => reconnect(sessionId)}>
          <RotateCw size={13} /> 重新连接
        </button>
      )}
    </div>
  );
}

function BlockView({ block, turn, sessionId }: { block: Block; turn: Turn; sessionId: string }) {
  switch (block.kind) {
    case "user":
      return <UserView block={block} />;
    case "text":
      return block.markdown ? (
        <div className="block text-block">
          <Markdown source={block.markdown} streaming={block.streaming} />
        </div>
      ) : null;
    case "activity":
      return <ActivityView block={block} turnRunning={turn.status === "running"} sessionId={sessionId} />;
    case "qa":
      return (
        <div className={`block qa${block.cancelled ? " skipped" : ""}`}>
          <div className="qa-head">{block.cancelled ? "已跳过提问" : "你的回答"}</div>
          {block.pairs.map(([q, a], i) => (
            <div key={i} className="qa-pair">
              <div className="qa-q">{q}</div>
              <div className="qa-a">{a}</div>
            </div>
          ))}
        </div>
      );
    case "error":
      return <ErrorView block={block} sessionId={sessionId} />;
    case "notice":
      return <div className="block notice">{block.text}</div>;
    case "switch":
      return <SwitchView block={block} />;
  }
}

/** Where the conversation changed engine. States what went along (text) and what did not, so nobody assumes the new
 *  engine remembers the files it never saw. */
function SwitchView({ block }: { block: Extract<Block, { kind: "switch" }> }) {
  const kb = block.bytes < 1024 ? `${block.bytes} B` : `${(block.bytes / 1024).toFixed(block.bytes < 10240 ? 1 : 0)} KB`;
  return (
    <div className="block engine-switch" role="separator" aria-label={`换到 ${block.to.model}`}>
      <div className="sw-rule">
        <span className="sw-tag">
          <ArrowRightLeft size={12} /> 换引擎
        </span>
      </div>
      <div className="sw-main">
        从 <b>{block.from.model}</b> <span className="sw-eng">{block.from.engine}</span> 换到 <b>{block.to.model}</b> <span className="sw-eng">{block.to.engine}</span>
      </div>
      <div className="sw-sub">
        {block.carried > 0 ? (
          <>
            之前 {block.truncated ? `最近 ${block.carried}（共 ${block.total}）` : block.carried} 条对话的文字（约 {kb}）会随下一条消息交给它{block.truncated ? "，更早的被省略" : ""}。
          </>
        ) : (
          <>没有可交接的文字。</>
        )}
        工具调用、结果、思考和图片不会带过去；Context 从这里重新计，Token 用量接着累加。
      </div>
    </div>
  );
}

function Footer({ turn, label }: { turn: Turn; label: string }) {
  if (turn.status === "running") {
    return (
      <div className="turnfoot live">
        <span className="pulse" />
        {label}
      </div>
    );
  }
  const base = turn.seconds !== undefined || turn.steps ? turnSummary(turn.seconds, turn.steps) : "";
  const prefix = turn.status === "interrupted" ? "已停止" : turn.status === "failed" ? "本轮失败" : "";
  const text = [prefix, base].filter(Boolean).join(" · ");
  return text ? <div className={`turnfoot ${turn.status}`}>{text}</div> : null;
}

export function TurnView({ turn, session }: { turn: Turn; session: Session }) {
  return (
    <section className="turn" data-turn-id={turn.id}>
      {turn.blocks.map((b) => (
        <BlockView key={b.id} block={b} turn={turn} sessionId={session.id} />
      ))}
      {turn.prompts
        .filter((p) => p.status === "pending")
        .map((p) => (p.kind === "permission" ? <PermissionCard key={p.id} sessionId={session.id} prompt={p} /> : <QuestionCard key={p.id} sessionId={session.id} prompt={p} />))}
      <Footer turn={turn} label={session.workingLabel} />
    </section>
  );
}


export function Transcript({ session }: { session: Session }) {
  const scroller = useRef<HTMLDivElement>(null);
  const inner = useRef<HTMLDivElement>(null);
  const stick = useRef(true);
  const jumpTarget = useRef<number | null>(null);
  const scheduleActive = useRef(() => {});
  const scrollTimer = useRef<number | undefined>(undefined);
  const [away, setAway] = useState(false);
  const [scrolling, setScrolling] = useState(false);
  const [activeId, setActiveId] = useState<string | null>(null);
  const links = useMemo(() => turnLinks(session.turns), [session.turns]);
  const setDraft = useStore((s) => s.setDraft);

  const behavior = (smooth: boolean): ScrollBehavior => smooth && !window.matchMedia("(prefers-reduced-motion: reduce)").matches ? "smooth" : "auto";
  const toEnd = (smooth = false) => {
    const el = scroller.current;
    jumpTarget.current = null;
    if (el) el.scrollTo({ top: el.scrollHeight, behavior: behavior(smooth) });
  };
  const syncActive = useCallback(() => {
    const el = scroller.current;
    if (!el || !inner.current || jumpTarget.current !== null) return;
    const turns = [...inner.current.querySelectorAll<HTMLElement>(":scope > .turn")];
    const origin = el.getBoundingClientRect().top - el.scrollTop;
    const tops = turns.map((turn) => turn.getBoundingClientRect().top - origin);
    const index = activeTurnAt(tops, el.scrollTop + 40, el.scrollHeight - el.scrollTop - el.clientHeight < 4);
    setActiveId(turns[index]?.dataset.turnId ?? null);
  }, []);
  const jumpTo = (id: string) => {
    const el = scroller.current;
    const turn = [...(inner.current?.querySelectorAll<HTMLElement>(":scope > .turn") ?? [])].find((item) => item.dataset.turnId === id);
    if (!el || !turn) return;
    const top = Math.max(0, Math.min(el.scrollHeight - el.clientHeight, el.scrollTop + turn.getBoundingClientRect().top - el.getBoundingClientRect().top - 28));
    jumpTarget.current = Math.abs(el.scrollTop - top) < 2 ? null : top;
    stick.current = jumpTarget.current === null && el.scrollHeight - top - el.clientHeight < 48;
    setActiveId(id);
    setAway(el.scrollHeight - top - el.clientHeight >= 48);
    el.scrollTo({ top, behavior: behavior(true) });
  };

  // A different session starts at its end.
  useLayoutEffect(() => {
    stick.current = true;
    setAway(false);
    toEnd();
  }, [session.id]);

  // Follow streaming only at the bottom. Re-measure the outline after scrolling, wrapping or resizing the pane.
  useEffect(() => {
    const el = inner.current;
    const viewport = scroller.current;
    if (!el || !viewport) return;
    let frame = 0;
    scheduleActive.current = () => {
      if (frame) return;
      frame = requestAnimationFrame(() => {
        frame = 0;
        syncActive();
      });
    };
    const ro = new ResizeObserver(() => {
      if (stick.current) toEnd();
      scheduleActive.current();
    });
    ro.observe(el);
    ro.observe(viewport);
    scheduleActive.current();
    return () => {
      ro.disconnect();
      cancelAnimationFrame(frame);
      window.clearTimeout(scrollTimer.current);
    };
  }, [session.id, syncActive]);

  // Restored snapshots can replace turn IDs without changing any measured height.
  useLayoutEffect(() => scheduleActive.current(), [session.turns]);

  // 跳到最新消息 (the app menu, Ctrl+End)
  useEffect(() => {
    const end = () => {
      stick.current = true;
      setAway(false);
      toEnd(true);
    };
    window.addEventListener("wc:scroll-end", end);
    return () => window.removeEventListener("wc:scroll-end", end);
  }, []);

  const onScroll = () => {
    const el = scroller.current!;
    const atEnd = el.scrollHeight - el.scrollTop - el.clientHeight < 48;
    if (jumpTarget.current !== null && Math.abs(el.scrollTop - jumpTarget.current) < 2) jumpTarget.current = null;
    stick.current = atEnd && jumpTarget.current === null;
    setAway(!atEnd);
    if (jumpTarget.current === null) scheduleActive.current();
    setScrolling(true);
    window.clearTimeout(scrollTimer.current);
    scrollTimer.current = window.setTimeout(() => setScrolling(false), 800);
  };

  const empty = session.turns.length === 0 && !session.queued;
  return (
    <div className="stage">
      <TurnNav links={links} activeId={activeId} onSelect={jumpTo} />
      <div ref={scroller} className={`scroller${scrolling ? " is-scrolling" : ""}`} onScroll={onScroll} onWheel={() => { jumpTarget.current = null; }} onPointerDown={() => { jumpTarget.current = null; }}>
        <div ref={inner} className="transcript">
          {empty ? (
            <div className="welcome">
              <div className="welcome-title">会话已就绪</div>
              <p>在下方输入消息，按 Enter 发送，Shift+Enter 换行。可以粘贴图片，或把文件拖进来。</p>
              <div className="suggest">
                {SUGGESTIONS.map((s) => (
                  <button key={s} type="button" className="chip-suggest" onClick={() => setDraft(session.id, s)}>
                    {s}
                  </button>
                ))}
              </div>
            </div>
          ) : (
            <>
              {session.turns.map((t) => (
                <TurnView key={t.id} turn={t} session={session} />
              ))}
            </>
          )}
        </div>
      </div>
      {away && (
        <button
          type="button"
          className="jump"
          onClick={() => {
            stick.current = true;
            toEnd(true);
          }}
        >
          <ArrowDown size={14} /> 跳到最新
        </button>
      )}
    </div>
  );
}
