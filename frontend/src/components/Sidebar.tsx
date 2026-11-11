import { useEffect, useRef, useState } from "react";
import { Archive, ArchiveRestore, Brain, ChevronRight, ExternalLink, FolderPlus, MoreHorizontal, Pencil, Plus, RefreshCw, Settings as Gear, SquarePen, Trash2, Copy, FolderOpen } from "lucide-react";
import { useStore } from "../store";
import type { Project, Session } from "../types";
import { MARK_TEXT } from "../lib/format";
import { findModel, sourceLabel } from "../lib/models";
import { formatRelative } from "../lib/time";
import { Menu, StateDot, type Anchor, type MenuItem } from "./ui";

function midClip(path: string, max = 36) {
  if (path.length <= max) return path;
  const keep = max - 1;
  const head = Math.ceil(keep * 0.4);
  return `${path.slice(0, head)}…${path.slice(path.length - (keep - head))}`;
}

/** Draws the sidebar again every 30 s, so that "刚刚" becomes "1 分钟前" by itself. */
function useTick(every = 30_000) {
  const [, setTick] = useState(0);
  useEffect(() => {
    const timer = window.setInterval(() => setTick((n) => n + 1), every);
    return () => window.clearInterval(timer);
  }, [every]);
}

/** The id the engine itself knows (what `droid --resume` or Codex would take). */
const nativeIdOf = (s: Session) => s.nativeId || (s.id.startsWith("cx:") || s.id.startsWith("dv:") ? s.id.slice(3) : s.id);

function SessionRow({ session, current, onMenu }: { session: Session; current: boolean; onMenu: (s: Session, a: Anchor) => void }) {
  const select = useStore((s) => s.select);
  const openSaved = useStore((s) => s.openSaved);
  const models = useStore((s) => s.models);
  const providers = useStore((s) => s.providers);
  const open = !!session.mark;
  // Sessions that are not Factory's say where they run: "已打开 · 刚刚 · Codex". A saved row does not know its model,
  // only its engine.
  const source = findModel(models, session.settings.model)?.source ?? (session.engine === "codex" || session.engine === "devin" || session.engine === "chatgpt" ? session.engine : "factory");
  const from = sourceLabel(source, providers);
  // Measured from this moment, not from the last tick: a row that has just arrived is never a tick behind.
  const when = session.modifiedAt ? formatRelative(session.modifiedAt, Date.now()) : session.modified;
  const sub = [open ? MARK_TEXT[session.mark!] : "", when, from].filter(Boolean).join(" · ");
  return (
    <div
      className={`srow${current ? " current" : ""}${open ? " open" : ""}${session.mark ? ` m-${session.mark}` : ""}`}
      role="option"
      aria-selected={current}
      tabIndex={0}
      onClick={() => (open ? select(session.id) : openSaved(session.id))}
      onDoubleClick={() => openSaved(session.id)}
      onKeyDown={(e) => {
        if (e.key === "Enter") openSaved(session.id);
      }}
      onContextMenu={(e) => {
        e.preventDefault();
        onMenu(session, { x: e.clientX, y: e.clientY });
      }}
    >
      <StateDot mark={session.mark} />
      <div className="srow-text">
        <div className="stitle">{session.title}</div>
        <div className="ssub">{sub}</div>
      </div>
      <button
        type="button"
        className="more"
        aria-label="更多"
        onClick={(e) => {
          e.stopPropagation();
          const r = e.currentTarget.getBoundingClientRect();
          onMenu(session, { x: r.left, y: r.top, h: r.height, w: r.width, alignRight: true });
        }}
      >
        <MoreHorizontal size={15} />
      </button>
    </div>
  );
}

function ProjectGroup({ project, onSessionMenu, onProjectMenu }: { project: Project; onSessionMenu: (s: Session, a: Anchor) => void; onProjectMenu: (p: Project, a: Anchor) => void }) {
  const sessions = useStore((s) => s.sessions);
  const currentId = useStore((s) => s.currentId);
  const toggle = useStore((s) => s.toggleProject);
  const newSession = useStore((s) => s.newSession);
  const list = project.sessionIds.map((id) => sessions[id]).filter((x): x is Session => !!x);
  return (
    <div className="proj">
      <div
        className="phead-row"
        onContextMenu={(e) => {
          e.preventDefault();
          onProjectMenu(project, { x: e.clientX, y: e.clientY });
        }}
      >
        <button type="button" className="proj-toggle" aria-expanded={!project.collapsed} onClick={() => toggle(project.id)}>
          <ChevronRight size={14} className={`chev${project.collapsed ? "" : " down"}`} />
          <span className="proj-text">
            <span className="pname">{project.name}</span>
            <span className="ppath" title={project.path}>
              {midClip(project.path)}
            </span>
          </span>
        </button>
        <button type="button" className="icon-btn sm" title="在此项目新建会话" aria-label="新会话" onClick={() => newSession(project.id)}>
          <Plus size={15} />
        </button>
        <button
          type="button"
          className="icon-btn sm"
          aria-label="项目菜单"
          onClick={(e) => {
            const r = e.currentTarget.getBoundingClientRect();
            onProjectMenu(project, { x: r.left, y: r.top, h: r.height, w: r.width, alignRight: true });
          }}
        >
          <MoreHorizontal size={15} />
        </button>
      </div>
      {!project.collapsed && (
        <div className="slist" role="listbox">
          {list.length === 0 ? <div className="snote">还没有会话</div> : list.map((s) => <SessionRow key={s.id} session={s} current={s.id === currentId} onMenu={onSessionMenu} />)}
        </div>
      )}
    </div>
  );
}

/** The conversations the user archived: hidden from the project lists, one click brings each back. */
function ArchivedGroup() {
  const archived = useStore((s) => s.archived);
  const projects = useStore((s) => s.projects);
  const unarchive = useStore((s) => s.unarchiveSession);
  const [open, setOpen] = useState(false);
  if (archived.length === 0) return null;
  return (
    <div className="proj archived">
      <div className="phead-row">
        <button type="button" className="proj-toggle" aria-expanded={open} onClick={() => setOpen(!open)}>
          <ChevronRight size={14} className={`chev${open ? " down" : ""}`} />
          <span className="proj-text">
            <span className="pname">已归档 · {archived.length}</span>
          </span>
        </button>
      </div>
      {open && (
        <div className="slist" role="list">
          {archived.map((row) => {
            const project = projects.find((p) => p.id === row.projectId);
            const when = row.modifiedAt ? formatRelative(row.modifiedAt, Date.now()) : row.modified;
            return (
              <div key={row.id} className="srow archived-row" role="listitem">
                <div className="srow-text">
                  <div className="stitle">{row.title}</div>
                  <div className="ssub">{[project?.name, when].filter(Boolean).join(" · ")}</div>
                </div>
                <button type="button" className="more" aria-label="取消归档" title="取消归档" onClick={() => unarchive(row.id)}>
                  <ArchiveRestore size={15} />
                </button>
              </div>
            );
          })}
        </div>
      )}
    </div>
  );
}

export function Sidebar() {
  const projects = useStore((s) => s.projects);
  const addProject = useStore((s) => s.addProject);
  const newSession = useStore((s) => s.newSession);
  const openSaved = useStore((s) => s.openSaved);
  const renameSession = useStore((s) => s.renameSession);
  const archiveSession = useStore((s) => s.archiveSession);
  const removeProject = useStore((s) => s.removeProject);
  const refreshProjects = useStore((s) => s.refreshProjects);
  const revealProject = useStore((s) => s.revealProject);
  const say = useStore((s) => s.say);
  const openSettings = useStore((s) => s.openSettings);
  // (what agents wrote that the user has not looked at, and the suggestions that wait for the user's word)
  const unseen = useStore((s) => s.memory.unseen + s.memory.pending);
  const [menu, setMenu] = useState<{ items: MenuItem[]; anchor: Anchor } | null>(null);
  const ref = useRef<HTMLElement>(null);
  useTick();

  const onSessionMenu = (s: Session, anchor: Anchor) =>
    setMenu({
      anchor,
      items: [
        { id: "open", label: "打开", onSelect: () => openSaved(s.id) },
        { id: "rename", label: "重命名…", icon: <Pencil size={14} />, onSelect: () => renameSession(s.id) },
        { id: "copy", label: "复制会话 ID", icon: <Copy size={14} />, onSelect: () => { void navigator.clipboard?.writeText(nativeIdOf(s)); say("已复制会话 ID。"); } },
        { id: "sep", separator: true },
        { id: "archive", label: "归档", icon: <Archive size={14} />, onSelect: () => archiveSession(s.id) },
      ],
    });
  const onProjectMenu = (p: Project, anchor: Anchor) =>
    setMenu({
      anchor,
      items: [
        { id: "new", label: "新会话", icon: <SquarePen size={14} />, onSelect: () => newSession(p.id) },
        { id: "refresh", label: "刷新会话列表", icon: <RefreshCw size={14} />, onSelect: () => refreshProjects(p.id) },
        { id: "reveal", label: "在资源管理器中打开", icon: <FolderOpen size={14} />, onSelect: () => revealProject(p.id) },
        { id: "sep", separator: true },
        { id: "remove", label: "从列表中移除", icon: <Trash2 size={14} />, danger: true, onSelect: () => removeProject(p.id) },
      ],
    });

  return (
    <aside ref={ref} className="sidebar" aria-label="项目和会话">
      <div className="side-top">
        <button type="button" className="newbtn" onClick={() => newSession()}>
          <SquarePen size={15} />
          新会话
          <kbd>Ctrl+N</kbd>
        </button>
      </div>
      <div className="side-head">
        <span>项目</span>
        <div className="side-head-actions">
          <button type="button" className="icon-btn sm" title="添加项目文件夹" aria-label="添加项目文件夹" onClick={addProject}>
            <FolderPlus size={15} />
          </button>
          <button type="button" className="icon-btn sm" title="刷新会话列表" aria-label="刷新会话列表" onClick={() => refreshProjects()}>
            <RefreshCw size={14} />
          </button>
        </div>
      </div>
      <div className="side-scroll">
        {projects.length === 0 ? (
          <div className="side-empty">
            <ExternalLink size={18} />
            还没有项目。添加一个文件夹，才能开始会话。
          </div>
        ) : (
          projects.map((p) => <ProjectGroup key={p.id} project={p} onSessionMenu={onSessionMenu} onProjectMenu={onProjectMenu} />)
        )}
        <ArchivedGroup />
      </div>
      <div className="side-foot">
        <button type="button" className="foot-btn" onClick={() => openSettings("memory")} title="Droid、Codex、Devin 和 ChatGPT Plan 共享的笔记">
          <Brain size={15} />
          共享记忆
          {unseen > 0 && (
            <span className="foot-badge" aria-label={`${unseen} 条新的`}>
              {unseen}
            </span>
          )}
        </button>
        <button type="button" className="foot-btn" onClick={() => openSettings("appearance")}>
          <Gear size={15} />
          设置
          <kbd>Ctrl+,</kbd>
        </button>
      </div>
      {menu && <Menu items={menu.items} anchor={menu.anchor} onClose={() => setMenu(null)} />}
    </aside>
  );
}
