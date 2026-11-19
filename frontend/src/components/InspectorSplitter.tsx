import { useCallback, useLayoutEffect, useRef, useState } from "react";
import { INSPECTOR_MIN, inspectorLimit, inspectorWidth } from "../lib/inspectorWidth";
import { useStore } from "../store";

export function InspectorSplitter() {
  const saved = useStore((s) => s.inspectorWidth);
  const handle = useRef<HTMLDivElement>(null);
  const drag = useRef<{ pointer: number; x: number; width: number; next: number } | null>(null);
  const [area, setArea] = useState({ width: 1440, sidebar: 288 });
  const [draft, setDraft] = useState<number | null>(null);
  const max = inspectorLimit(area.width, area.sidebar);
  const width = inspectorWidth(draft ?? saved, area.width, area.sidebar);
  const finish = useCallback((commit: boolean) => {
    const start = drag.current;
    if (!start) return;
    drag.current = null;
    document.body.classList.remove("resizing");
    if (handle.current?.hasPointerCapture(start.pointer)) handle.current.releasePointerCapture(start.pointer);
    setDraft(null);
    if (commit && start.width !== start.next) useStore.getState().setInspectorWidth(start.next);
  }, []);

  useLayoutEffect(() => {
    const app = handle.current?.parentElement;
    const main = app?.querySelector<HTMLElement>(":scope > .main");
    if (!app || !main) return;
    const measure = () => {
      const width = app.clientWidth, sidebar = main.offsetLeft;
      setArea((previous) => previous.width === width && previous.sidebar === sidebar ? previous : { width, sidebar });
      if (window.matchMedia("(max-width: 1100px)").matches) finish(false);
    };
    measure();
    const observer = new ResizeObserver(measure);
    observer.observe(app);
    observer.observe(main);
    const cancel = () => finish(false);
    const escape = (e: KeyboardEvent) => {
      if (e.key !== "Escape" || !drag.current) return;
      e.preventDefault();
      e.stopPropagation();
      cancel();
    };
    window.addEventListener("blur", cancel);
    window.addEventListener("keydown", escape, true);
    return () => {
      observer.disconnect();
      window.removeEventListener("blur", cancel);
      window.removeEventListener("keydown", escape, true);
      cancel();
      app.style.removeProperty("--inspector-w");
    };
  }, [finish]);

  useLayoutEffect(() => {
    handle.current?.parentElement?.style.setProperty("--inspector-w", `${width}px`);
  }, [width]);

  return <div ref={handle} className="inspector-splitter" role="separator" tabIndex={0} aria-label="调整右侧预览宽度"
    aria-orientation="vertical" aria-valuemin={INSPECTOR_MIN} aria-valuemax={max} aria-valuenow={width} data-dragging={draft === null ? undefined : ""}
    onPointerDown={(e) => {
      if (e.button !== 0 || !e.isPrimary) return;
      e.preventDefault();
      e.currentTarget.focus();
      drag.current = { pointer: e.pointerId, x: e.clientX, width, next: width };
      e.currentTarget.setPointerCapture(e.pointerId);
      setDraft(width);
      document.body.classList.add("resizing");
    }}
    onPointerMove={(e) => {
      const start = drag.current;
      if (start?.pointer !== e.pointerId) return;
      start.next = inspectorWidth(Math.max(INSPECTOR_MIN, start.width + start.x - e.clientX), area.width, area.sidebar);
      setDraft(start.next);
    }}
    onPointerUp={(e) => {
      const start = drag.current;
      if (start?.pointer !== e.pointerId) return;
      start.next = inspectorWidth(start.next, area.width, area.sidebar);
      finish(true);
    }}
    onPointerCancel={() => finish(false)} onLostPointerCapture={() => finish(false)}
    onKeyDown={(e) => {
      if (drag.current) return;
      const step = e.shiftKey ? 40 : 16;
      const next = e.key === "ArrowLeft" ? width + step : e.key === "ArrowRight" ? width - step : e.key === "Home" ? INSPECTOR_MIN : e.key === "End" ? max : null;
      if (next === null) return;
      e.preventDefault();
      e.stopPropagation();
      const value = inspectorWidth(next, area.width, area.sidebar);
      if (value !== width) useStore.getState().setInspectorWidth(value);
    }} />;
}
