import { useLayoutEffect, useRef, useState, type ReactNode } from "react";
import { useStore } from "../store";
import { railLimit, railWidth, RAIL_MIN } from "../lib/railWidth";

export function ConversationRail({ children }: { children: ReactNode }) {
  const saved = useStore((s) => s.conversationWidth);
  const setWidth = useStore((s) => s.setConversationWidth);
  const rail = useRef<HTMLElement>(null);
  const handle = useRef<HTMLDivElement>(null);
  const drag = useRef<{ x: number; width: number; saved: number; pointer: number } | null>(null);
  const [available, setAvailable] = useState(1200);
  const max = railLimit(available);
  const width = railWidth(saved, available);

  const finish = (commit: boolean) => {
    const start = drag.current;
    if (!start) return;
    drag.current = null;
    document.body.classList.remove("resizing");
    if (handle.current?.hasPointerCapture(start.pointer)) handle.current.releasePointerCapture(start.pointer);
    const state = useStore.getState();
    state.setConversationWidth(commit ? state.conversationWidth : start.saved, commit);
  };
  useLayoutEffect(() => {
    const parent = rail.current?.parentElement;
    if (!parent) return;
    const measure = () => setAvailable(parent.getBoundingClientRect().width);
    measure();
    const observer = new ResizeObserver(measure);
    observer.observe(parent);
    const cancel = () => finish(false);
    window.addEventListener("blur", cancel);
    return () => {
      observer.disconnect();
      window.removeEventListener("blur", cancel);
      cancel();
    };
  }, []);

  return <aside ref={rail} className="conversation-rail" aria-label="会话计划与 Agents Team" style={{ width, flexBasis: width }}>
    <div ref={handle} className="conversation-splitter" role="separator" tabIndex={0} aria-label="调整右侧栏宽度"
      aria-orientation="vertical" aria-valuemin={RAIL_MIN} aria-valuemax={max} aria-valuenow={Math.round(width)}
      onPointerDown={(event) => {
        if (event.button !== 0 || !event.isPrimary) return;
        event.preventDefault();
        event.currentTarget.focus();
        drag.current = { x: event.clientX, width, saved, pointer: event.pointerId };
        event.currentTarget.setPointerCapture(event.pointerId);
        document.body.classList.add("resizing");
      }}
      onPointerMove={(event) => {
        const start = drag.current;
        if (start && event.pointerId === start.pointer) setWidth(railWidth(start.width + start.x - event.clientX, available));
      }}
      onPointerUp={() => finish(true)} onPointerCancel={() => finish(false)} onLostPointerCapture={() => finish(false)}
      onKeyDown={(event) => {
        if (event.key === "Escape") { finish(false); return; }
        const delta = event.shiftKey ? 40 : 16;
        const next = event.key === "ArrowLeft" ? width + delta : event.key === "ArrowRight" ? width - delta
          : event.key === "Home" ? RAIL_MIN : event.key === "End" ? max : null;
        if (next !== null) { event.preventDefault(); setWidth(railWidth(next, available), true); }
      }} />
    <div className="conversation-rail-content">{children}</div>
  </aside>;
}
