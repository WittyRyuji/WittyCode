import { useCallback, useEffect, useMemo, useState } from "react";
import { ChevronDown, ChevronRight, FolderInput, Loader2, Undo2, X } from "lucide-react";
import { useStore } from "../store";
import { bridge } from "../bridge-instance";
import { messageOf } from "../lib/errors";
import { formatRelative } from "../lib/time";
import { kindLabel } from "../lib/memory";
import type { ImportApp, ImportMemory, ImportResult, ImportSession, ImportSource } from "../types";
import { Seg } from "./formkit";

const say = (text: string) => useStore.getState().say(text);

type Tab = "memories" | "sessions";

/** Bring in what other applications keep. The memory files become notes (split by heading, previewed and ticked first), the
 *  conversations become conversations of the ChatGPT plan engine that can be continued. Nothing is written before the click. */
export function ImportWizard({ onClose }: { onClose: () => void }) {
  const [sources, setSources] = useState<ImportSource[] | null>(null);
  const [apps, setApps] = useState<ImportApp[]>([]);
  const [tab, setTab] = useState<Tab>("memories");
  const [memories, setMemories] = useState<ImportMemory[] | null>(null);
  const [sessions, setSessions] = useState<ImportSession[] | null>(null);
  const [ticked, setTicked] = useState<Set<string>>(new Set());
  const [open, setOpen] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [done, setDone] = useState<ImportResult | null>(null);

  useEffect(() => {
    let alive = true;
    bridge.importer
      .scan()
      .then((r) => {
        if (!alive) return;
        setSources(r.sources);
        setApps(r.sources.filter((s) => s.available).map((s) => s.app));
      })
      .catch((error) => {
        if (!alive) return;
        setSources([]);
        say(messageOf(error));
      });
    return () => {
      alive = false;
    };
  }, []);

  const key = apps.join(",");
  useEffect(() => {
    if (!sources || apps.length === 0) {
      setMemories(sources ? [] : null);
      return;
    }
    let alive = true;
    setMemories(null);
    bridge.importer
      .memories(apps)
      .then((r) => {
        if (!alive) return;
        setMemories(r.items);
        setTicked((t) => new Set([...[...t].filter((k) => !k.startsWith("im:")), ...r.items.filter((m) => !m.exists).map((m) => `im:${m.key}`)]));
      })
      .catch((error) => {
        if (!alive) return;
        setMemories([]);
        say(messageOf(error));
      });
    return () => {
      alive = false;
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [sources, key]);

  useEffect(() => {
    if (tab !== "sessions" || !sources) return;
    if (apps.length === 0) return setSessions([]);
    let alive = true;
    setSessions(null);
    bridge.importer
      .sessions(apps)
      .then((r) => alive && setSessions(r.items))
      .catch((error) => {
        if (!alive) return;
        setSessions([]);
        say(messageOf(error));
      });
    return () => {
      alive = false;
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [tab, sources, key]);

  const toggle = useCallback((id: string, on?: boolean) => {
    setTicked((t) => {
      const next = new Set(t);
      if (on ?? !next.has(id)) next.add(id);
      else next.delete(id);
      return next;
    });
  }, []);

  const pickedNotes = useMemo(() => (memories ?? []).filter((m) => ticked.has(`im:${m.key}`)), [memories, ticked]);
  const pickedSessions = useMemo(() => (sessions ?? []).filter((s) => ticked.has(`is:${s.key}`)), [sessions, ticked]);

  const commit = async () => {
    if (busy || (pickedNotes.length === 0 && pickedSessions.length === 0)) return;
    setBusy(true);
    try {
      const r = await bridge.importer.commit(
        pickedNotes.map((m) => m.key),
        pickedSessions.map((s) => s.key),
      );
      useStore.getState().setMemoryState(r);
      setDone(r);
      setTicked(new Set());
      const [m, s] = await Promise.all([bridge.importer.memories(apps), tab === "sessions" || pickedSessions.length ? bridge.importer.sessions(apps) : Promise.resolve(null)]);
      setMemories(m.items);
      if (s) setSessions(s.items);
    } catch (error) {
      say(messageOf(error));
    } finally {
      setBusy(false);
    }
  };

  const undo = async () => {
    if (!done || done.notes.length === 0) return;
    try {
      const r = await bridge.importer.undo(done.notes.map((n) => n.id));
      useStore.getState().setMemoryState(r);
      say(`已撤销这次导入的 ${done.notes.length} 条笔记（在「显示已忘记」里可以恢复）。`);
      setDone({ ...done, notes: [] });
      setMemories((await bridge.importer.memories(apps)).items);
    } catch (error) {
      say(messageOf(error));
    }
  };

  const byApp = (list: ImportMemory[] | ImportSession[]) => apps.map((app) => ({ app, items: (list as { app: ImportApp }[]).filter((i) => i.app === app) })).filter((g) => g.items.length > 0);
  const label = (app: ImportApp) => sources?.find((s) => s.app === app)?.label ?? app;
  const total = pickedNotes.length + pickedSessions.length;

  return (
    <section className="imp" aria-label="从其他应用导入">
      <header className="imp-head">
        <h3>
          <FolderInput size={14} /> 从其他应用导入
        </h3>
        <button type="button" className="icon-btn sm" title="关闭" aria-label="关闭导入" onClick={onClose}>
          <X size={15} />
        </button>
      </header>
      {!sources && (
        <div className="imp-wait">
          <Loader2 size={14} className="spin" /> 正在找这台电脑上的应用…
        </div>
      )}
      {sources && (
        <>
          <div className="imp-sources">
            {sources.map((s) => (
              <label key={s.app} className={`imp-source${s.available ? "" : " off"}`} title={s.error ?? (s.available ? "" : "这台电脑上没找到它的数据")}>
                <input type="checkbox" disabled={!s.available} checked={apps.includes(s.app)} onChange={(e) => setApps((a) => (e.target.checked ? sources.filter((x) => x.available && (x.app === s.app || a.includes(x.app))).map((x) => x.app) : a.filter((x) => x !== s.app)))} />
                <b>{s.label}</b>
                <span className="imp-count">{s.available ? `记忆 ${s.memories} 条 · 会话 ${s.sessions} 个` : s.error ? "读不了" : "没找到"}</span>
              </label>
            ))}
          </div>
          <div className="imp-tabs">
            <Seg<Tab>
              value={tab}
              onChange={setTab}
              options={[
                { id: "memories", label: `记忆${memories ? `（${memories.length}）` : ""}` },
                { id: "sessions", label: `会话${sessions ? `（${sessions.length}）` : ""}` },
              ]}
            />
            <span className="imp-help">{tab === "memories" ? "记忆文件按标题拆成笔记，勾上的才会导入，导入后标着来源。" : "导入后成为 ChatGPT Plan 的会话：只带你说的话、回答和每个工具调用的一行摘要，可以接着聊。"}</span>
          </div>
          {tab === "memories" && (
            <div className="imp-list">
              {memories === null && (
                <div className="imp-wait">
                  <Loader2 size={14} className="spin" /> 正在读记忆文件…
                </div>
              )}
              {memories && memories.length === 0 && <div className="mem-empty">没有可导入的记忆。{apps.length === 0 ? "先在上面选一个应用。" : "这些应用里没有找到记忆文件。"}</div>}
              {memories &&
                byApp(memories).map((g) => (
                  <div key={g.app} className="imp-group">
                    <div className="imp-group-head">
                      <b>{label(g.app)}</b>
                      <button type="button" className="mem-link" onClick={() => (g.items as ImportMemory[]).forEach((m) => toggle(`im:${m.key}`, !m.exists))}>
                        全选
                      </button>
                      <button type="button" className="mem-link" onClick={() => (g.items as ImportMemory[]).forEach((m) => toggle(`im:${m.key}`, false))}>
                        全不选
                      </button>
                    </div>
                    <ul className="imp-items">
                      {(g.items as ImportMemory[]).map((m) => (
                        <li key={m.key} className={`imp-item${m.exists ? " has" : ""}`}>
                          <div className="imp-row">
                            <input type="checkbox" aria-label={`导入「${m.title}」`} checked={ticked.has(`im:${m.key}`)} onChange={() => toggle(`im:${m.key}`)} />
                            <button type="button" className="imp-main" aria-expanded={open === m.key} onClick={() => setOpen(open === m.key ? null : m.key)}>
                              {open === m.key ? <ChevronDown size={13} /> : <ChevronRight size={13} />}
                              <span className={`mem-kind k-${m.kind}`}>{kindLabel(m.kind)}</span>
                              <span className="mem-title">{m.title}</span>
                              <span className="mem-scope">{m.scope === "global" ? "所有项目" : m.projectName || "项目"}</span>
                              {m.exists && <span className="mem-tag">已有</span>}
                            </button>
                          </div>
                          <div className="mem-meta imp-origin">{m.origin}</div>
                          {open === m.key && <div className="mem-text selectable imp-text">{m.text}</div>}
                        </li>
                      ))}
                    </ul>
                  </div>
                ))}
            </div>
          )}
          {tab === "sessions" && (
            <div className="imp-list">
              {sessions === null && (
                <div className="imp-wait">
                  <Loader2 size={14} className="spin" /> 正在列出会话…
                </div>
              )}
              {sessions && sessions.length === 0 && <div className="mem-empty">没有可导入的会话。</div>}
              {sessions &&
                byApp(sessions).map((g) => (
                  <div key={g.app} className="imp-group">
                    <div className="imp-group-head">
                      <b>{label(g.app)}</b>
                      <span className="imp-count">{g.items.length} 个，新的在前</span>
                    </div>
                    <ul className="imp-items">
                      {(g.items as ImportSession[]).map((s) => (
                        <li key={s.key} className="imp-item">
                          <label className="imp-row">
                            <input type="checkbox" aria-label={`导入会话「${s.title}」`} checked={ticked.has(`is:${s.key}`)} onChange={() => toggle(`is:${s.key}`)} />
                            <span className="mem-title">{s.title || "（无标题）"}</span>
                            <span className="mem-scope">{s.projectName || "—"}</span>
                            <span className="mem-scope">{s.messages} 条消息</span>
                            <span className="mem-when">{formatRelative(s.updated)}</span>
                            {s.imported && <span className="mem-tag">导入过</span>}
                          </label>
                        </li>
                      ))}
                    </ul>
                  </div>
                ))}
            </div>
          )}
          {done && (
            <div className="imp-done" role="status">
              <div>
                已导入 <b>{done.notes.length}</b> 条笔记、<b>{done.sessions.length}</b> 个会话
                {done.skipped.length > 0 && `；跳过 ${done.skipped.length} 个`}。
                {done.sessions.length > 0 && "会话在侧栏里（ChatGPT Plan），打开后可以接着聊。"}
              </div>
              {done.skipped.length > 0 && <div className="mem-meta">{done.skipped.slice(0, 4).map((s) => `「${s.title}」${s.reason}`).join("；")}{done.skipped.length > 4 ? " …" : ""}</div>}
              {done.notes.length > 0 && (
                <button type="button" className="btn small" onClick={() => void undo()}>
                  <Undo2 size={13} />
                  撤销这次导入的笔记
                </button>
              )}
            </div>
          )}
          <div className="row-btns imp-foot">
            <button type="button" className="btn primary" disabled={busy || total === 0} onClick={() => void commit()}>
              {busy ? "正在导入…" : total === 0 ? "导入所选" : `导入所选（${pickedNotes.length} 条笔记 · ${pickedSessions.length} 个会话）`}
            </button>
            <button type="button" className="btn" onClick={onClose}>
              关闭
            </button>
          </div>
        </>
      )}
    </section>
  );
}
