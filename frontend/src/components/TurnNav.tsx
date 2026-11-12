import { useEffect, useRef, useState } from "react";
import type { TurnLink } from "../lib/turnNavigation";

const STATUS = { running: "进行中", done: "已完成", interrupted: "已停止", failed: "本轮失败" } as const;

function revealMarker(nav: HTMLElement, marker: HTMLElement) {
  const padding = parseFloat(getComputedStyle(nav).paddingTop);
  if (marker.offsetTop - padding < nav.scrollTop) nav.scrollTop = marker.offsetTop - padding;
  else if (marker.offsetTop + marker.offsetHeight + padding > nav.scrollTop + nav.clientHeight) nav.scrollTop = marker.offsetTop + marker.offsetHeight + padding - nav.clientHeight;
}

/** A quiet conversation outline: hover to recognise a message, click or use the arrow keys to return to it. */
export function TurnNav({ links, activeId, onSelect }: { links: TurnLink[]; activeId: string | null; onSelect: (id: string) => void }) {
  const list = useRef<HTMLElement>(null);
  const [preview, setPreview] = useState<{ id: string; left: number; top: number } | null>(null);
  useEffect(() => {
    const nav = list.current;
    const current = nav?.querySelector<HTMLElement>("[aria-current='step']");
    if (!nav || !current) return;
    revealMarker(nav, current);
  }, [activeId]);
  const show = (id: string, button: HTMLButtonElement) => {
    if (list.current) revealMarker(list.current, button);
    const rect = button.getBoundingClientRect();
    setPreview({ id, left: rect.right + 10, top: Math.max(8, Math.min(rect.top - 12, window.innerHeight - 140)) });
  };
  if (!links.length) return null;
  const shown = links.findIndex((link) => link.id === preview?.id);
  const link = links[shown];
  return (
    <>
      <nav
        ref={list}
        className="turn-nav"
        aria-label="回合快速跳转"
        onMouseLeave={() => setPreview(null)}
        onKeyDown={(event) => {
          const buttons = [...(list.current?.querySelectorAll<HTMLButtonElement>("button") ?? [])];
          const index = buttons.indexOf(event.target as HTMLButtonElement);
          if (index < 0) return;
          let next: number;
          if (event.key === "ArrowDown") next = Math.min(index + 1, links.length - 1);
          else if (event.key === "ArrowUp") next = Math.max(index - 1, 0);
          else if (event.key === "Home") next = 0;
          else if (event.key === "End") next = links.length - 1;
          else return;
          event.preventDefault();
          buttons[next]?.focus({ preventScroll: true });
          onSelect(links[next]!.id);
        }}
      >
        {links.map((item, index) => (
          <button
            key={item.id}
            type="button"
            className={`turn-marker st-${item.status}`}
            data-turn-id={item.id}
            aria-label={`第 ${index + 1} 轮：${item.label}`}
            aria-current={item.id === activeId ? "step" : undefined}
            aria-describedby={preview?.id === item.id ? "turn-preview" : undefined}
            tabIndex={item.id === (activeId ?? links[0]?.id) ? 0 : -1}
            onMouseEnter={(event) => show(item.id, event.currentTarget)}
            onFocus={(event) => show(item.id, event.currentTarget)}
            onBlur={() => setPreview(null)}
            onClick={() => onSelect(item.id)}
          >
            <span aria-hidden />
          </button>
        ))}
      </nav>
      {preview && link && (
        <div id="turn-preview" className="turn-preview" role="tooltip" style={{ left: preview.left, top: preview.top }}>
          <div className="turn-preview-head"><span>第 {shown + 1} 轮</span><span>{STATUS[link.status]}</span></div>
          <div className="turn-preview-text">{link.label}</div>
        </div>
      )}
    </>
  );
}
