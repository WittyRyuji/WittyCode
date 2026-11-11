import { useEffect, useRef, useState } from "react";
import { currentSession, useStore } from "./store";
import { Sidebar } from "./components/Sidebar";
import { Tabs } from "./components/Tabs";
import { Transcript } from "./components/Transcript";
import { AgentBar, AgentPanel } from "./components/Agents";
import { FilePanel, PathMenuHost } from "./components/FilePanel";
import { Composer } from "./components/Composer";
import { StatusBar } from "./components/StatusBar";
import { ChatGPTWelcome, DialogHost, DropOverlay, EmptyPage, LightboxHost } from "./components/Overlays";
import { ReviewBar, WIDTHS } from "./components/Review";
import { TitleBar } from "./components/TitleBar";
import { StyleGuide } from "./components/StyleGuide";
import { SettingsHost } from "./components/Settings";
import { ResizeHandles } from "./components/ResizeHandles";
import { FrameLine } from "./components/FrameLine";
import { applyUrlParams } from "./lib/params";
import { LIVE } from "./mode";

function useTheme() {
  const choice = useStore((s) => s.themeChoice);
  const scale = useStore((s) => s.fontScale);
  useEffect(() => {
    const root = document.documentElement;
    const media = window.matchMedia("(prefers-color-scheme: dark)");
    const apply = () => root.setAttribute("data-theme", choice === "system" ? (media.matches ? "dark" : "light") : choice);
    apply();
    media.addEventListener("change", apply);
    return () => media.removeEventListener("change", apply);
  }, [choice]);
  useEffect(() => {
    document.documentElement.setAttribute("data-fs", scale);
  }, [scale]);
}

function Main() {
  const session = useStore(currentSession);
  const setDragging = useStore((s) => s.setDragging);
  const say = useStore((s) => s.say);
  const attachFiles = useStore((s) => s.attachFiles);
  const depth = useRef(0);

  const hasFiles = (e: React.DragEvent) => [...e.dataTransfer.types].includes("Files");
  return (
    <main
      className="main"
      onDragEnter={(e) => {
        if (!hasFiles(e)) return;
        depth.current++;
        setDragging(true);
      }}
      onDragOver={(e) => hasFiles(e) && e.preventDefault()}
      onDragLeave={(e) => {
        if (!hasFiles(e)) return;
        depth.current = Math.max(0, depth.current - 1);
        if (depth.current === 0) setDragging(false);
      }}
      onDrop={(e) => {
        if (!hasFiles(e)) return;
        e.preventDefault();
        depth.current = 0;
        setDragging(false);
        const sess = currentSession(useStore.getState());
        const files = [...e.dataTransfer.files];
        if (!sess) {
          // the first message box: the files need a conversation, which is opened now
          void useStore.getState().adoptLanding().then((id) => void (id && attachFiles(id, files)));
          return;
        }
        if (!(sess.mark === "ready" || sess.mark === "running" || sess.mark === "attention")) return say("现在不能添加附件。");
        void attachFiles(sess.id, files);
      }}
    >
      <Tabs />
      {/* keyed: Python numbers turns and blocks per conversation, so two conversations share ids */}
      {session ? <Transcript key={session.id} session={session} /> : <EmptyPage />}
      {session && <AgentBar session={session} />}
      <Composer />
      <DropOverlay />
    </main>
  );
}

/** The shortcuts the app menu lists that only the real window has (in a browser they belong to the browser). */
function liveShortcut(e: KeyboardEvent): boolean {
  const st = useStore.getState();
  if (e.key === "F5" && !(e.ctrlKey || e.metaKey || e.altKey)) {
    e.preventDefault();
    st.refreshProjects();
    return true;
  }
  if (!(e.ctrlKey || e.metaKey) || e.altKey) return false;
  const editing = e.target instanceof HTMLElement && (e.target.isContentEditable || /^(INPUT|TEXTAREA|SELECT)$/.test(e.target.tagName));
  switch (e.key.toLowerCase()) {
    case "w":
      e.preventDefault();
      if (st.currentId) st.closeTab(st.currentId);
      return true;
    case "o":
      e.preventDefault();
      st.addProject();
      return true;
    case "l":
      e.preventDefault();
      window.dispatchEvent(new Event("wc:focus-composer"));
      return true;
    case "q":
      e.preventDefault();
      st.quit();
      return true;
    case "end":
      // In the message box Ctrl+End still moves the caret to the end as well.
      if (!editing) e.preventDefault();
      window.dispatchEvent(new Event("wc:scroll-end"));
      return true;
  }
  return false;
}

/** The live app until app.init has answered: only the title bar, so the window can already be moved and closed. */
function Boot() {
  const error = useStore((s) => s.bootError);
  return (
    <div className="boot" role="status">
      {error && <p>{error}</p>}
    </div>
  );
}

export default function App() {
  useTheme();
  const sidebarOpen = useStore((s) => s.sidebarOpen);
  const setSidebarOpen = useStore((s) => s.setSidebarOpen);
  const reviewOpen = useStore((s) => s.reviewOpen);
  const setReviewOpen = useStore((s) => s.setReviewOpen);
  const ready = useStore((s) => s.ready);
  const sideW = useStore((s) => s.sidebarWidth);
  const agentOpen = useStore((s) => (s.agentPanel !== null && s.agentPanel.sessionId === s.currentId) || (s.filePanel !== null && s.filePanel.sessionId === s.currentId));
  const [initial] = useState<{ width?: string; guide?: boolean }>(() => (LIVE ? {} : applyUrlParams()));
  const [width, setWidth] = useState(initial.width ?? "auto");
  const [guide, setGuide] = useState(!!initial.guide);
  const resizing = useRef(false);

  useEffect(() => {
    const key = (e: KeyboardEvent) => {
      if (LIVE && (!useStore.getState().ready || liveShortcut(e))) return;
      if (!(e.ctrlKey || e.metaKey)) return;
      if (e.key.toLowerCase() === "b") {
        e.preventDefault();
        setSidebarOpen(!useStore.getState().sidebarOpen);
      } else if (e.key === ",") {
        e.preventDefault();
        useStore.getState().openSettings("appearance");
      } else if (e.key.toLowerCase() === "n") {
        e.preventDefault();
        useStore.getState().newSession();
      }
    };
    const move = (e: MouseEvent) => {
      if (!resizing.current) return;
      const left = document.querySelector(".winframe")?.getBoundingClientRect().left ?? 0;
      useStore.getState().setSidebarWidth(Math.max(220, Math.min(440, e.clientX - left)));
    };
    const up = () => {
      if (!resizing.current) return;
      resizing.current = false;
      document.body.classList.remove("resizing");
      const st = useStore.getState();
      st.setSidebarWidth(st.sidebarWidth, true);
    };
    document.addEventListener("keydown", key);
    window.addEventListener("mousemove", move);
    window.addEventListener("mouseup", up);
    return () => {
      document.removeEventListener("keydown", key);
      window.removeEventListener("mousemove", move);
      window.removeEventListener("mouseup", up);
    };
  }, [setSidebarOpen]);

  if (LIVE && !ready) {
    return (
      <div className="review-root">
        <div className="winwrap">
          <div className="winframe">
            <TitleBar menu={false} />
            <Boot />
          </div>
        </div>
        <ResizeHandles />
        <FrameLine />
      </div>
    );
  }

  const w = WIDTHS.find((x) => x.id === width)?.width ?? 0;
  return (
    <div className={`review-root${reviewOpen ? " reviewing" : ""}`}>
      {!LIVE &&
        (reviewOpen ? (
          <ReviewBar width={width} onWidth={setWidth} onGuide={() => setGuide(true)} />
        ) : (
          <button type="button" className="review-show" onClick={() => setReviewOpen(true)}>
            审阅栏
          </button>
        ))}
      <div className="winwrap">
        <div className="winframe" style={reviewOpen && w ? { width: w } : undefined}>
          <TitleBar />
          {/* The sidebar never takes more than a third of a narrow window. */}
          <div
            className="app"
            style={{ "--side-w": `min(${sideW}px, 36%)`, gridTemplateColumns: `${sidebarOpen ? "var(--side-w)" : "0"} minmax(0, 1fr)${agentOpen ? " minmax(340px, 42%)" : ""}` } as React.CSSProperties}
          >
            {sidebarOpen ? <Sidebar /> : <div />}
            {sidebarOpen && (
              <div
                className="splitter"
                style={{ left: "calc(var(--side-w) - 3px)" }}
                onMouseDown={() => {
                  resizing.current = true;
                  document.body.classList.add("resizing");
                }}
              />
            )}
            <Main />
            <AgentPanel />
            <FilePanel />
            <StatusBar />
          </div>
        </div>
      </div>
      {LIVE && <ResizeHandles />}
      {LIVE && <FrameLine />}
      <SettingsHost />
      <DialogHost />
      <ChatGPTWelcome />
      <LightboxHost />
      <PathMenuHost />
      {guide && <StyleGuide onClose={() => setGuide(false)} />}
    </div>
  );
}
