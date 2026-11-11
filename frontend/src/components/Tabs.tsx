import { useEffect, useRef } from "react";
import { PanelLeft, PanelLeftClose, X } from "lucide-react";
import { useStore } from "../store";
import { StateDot } from "./ui";

export function Tabs() {
  const tabs = useStore((s) => s.tabs);
  const sessions = useStore((s) => s.sessions);
  const currentId = useStore((s) => s.currentId);
  const select = useStore((s) => s.select);
  const closeTab = useStore((s) => s.closeTab);
  const sidebarOpen = useStore((s) => s.sidebarOpen);
  const setSidebarOpen = useStore((s) => s.setSidebarOpen);
  const strip = useRef<HTMLDivElement>(null);

  // The tab you select is kept in view when there are more tabs than room.
  useEffect(() => {
    strip.current?.querySelector<HTMLElement>(".tab.current")?.scrollIntoView({ block: "nearest", inline: "nearest" });
  }, [currentId, tabs.length]);

  return (
    <div className="tabbar">
      <button type="button" className="icon-btn" title={sidebarOpen ? "收起侧栏 (Ctrl+B)" : "展开侧栏 (Ctrl+B)"} aria-label="切换侧栏" onClick={() => setSidebarOpen(!sidebarOpen)}>
        {sidebarOpen ? <PanelLeftClose size={17} /> : <PanelLeft size={17} />}
      </button>
      <div
        ref={strip}
        className="tabs"
        role="tablist"
        onWheel={(e) => {
          if (strip.current && Math.abs(e.deltaY) > Math.abs(e.deltaX)) strip.current.scrollLeft += e.deltaY;
        }}
      >
        {tabs.map((id) => {
          const s = sessions[id];
          if (!s) return null;
          const current = id === currentId;
          return (
            <div
              key={id}
              role="tab"
              aria-selected={current}
              tabIndex={0}
              className={`tab${current ? " current" : ""}`}
              title={s.title}
              onClick={() => select(id)}
              onAuxClick={(e) => e.button === 1 && closeTab(id)}
              onKeyDown={(e) => e.key === "Enter" && select(id)}
            >
              <StateDot mark={s.mark} size={8} />
              <span className="tab-title">{s.title}</span>
              <button
                type="button"
                className="tab-x"
                aria-label={`关闭 ${s.title}`}
                onClick={(e) => {
                  e.stopPropagation();
                  closeTab(id);
                }}
              >
                <X size={13} />
              </button>
            </div>
          );
        })}
      </div>
    </div>
  );
}
