import { useCallback, useEffect, useRef, useState } from "react";
import { FolderInput, Pencil, Plus, RotateCcw, Search, Undo2 } from "lucide-react";
import { useStore } from "../store";
import { bridge } from "../bridge-instance";
import { messageOf } from "../lib/errors";
import { formatRelative } from "../lib/time";
import { KINDS, changeVerb, engineName, isUnseen, kindLabel, parseTags, samePath, scopeLabel, sourceLine, whoConversation, whoName } from "../lib/memory";
import type { Engine, MemoryChange, MemoryEntry, MemoryKind, MemoryReview, MemoryReviewItem, MemoryReviewSummary, MemoryScope, MemoryState } from "../types";
import type { MemoryFilter, ReviewEdit } from "../protocol";
import { Field, Select, Seg, Stack, Switch, TextInput } from "./formkit";
import { ImportWizard } from "./ImportWizard";
import { MemoryStarMap } from "./MemoryStarMap";
import type { MenuItem } from "./ui";

const ENGINES: Engine[] = ["chatgpt", "devin"];
const MAX_TITLE = 80;
const MAX_TEXT = 2000;
const RECENT_SHOWN = 5;

const say = (text: string) => useStore.getState().say(text);

/** One call to the memory: the state it answers with goes to the store (the badge), a failure to the status bar. */
async function run<T extends MemoryState>(call: Promise<T>, done?: (result: T) => string): Promise<T | null> {
  try {
    const result = await call;
    useStore.getState().setMemoryState(result);
    if (done) say(done(result));
    return result;
  } catch (error) {
    say(messageOf(error));
    return null;
  }
}

/** The conversation a note came from: the open tab when there is one, the saved row otherwise. */
function openConversation(rowId: string): void {
  const st = useStore.getState();
  if (st.sessions[rowId]) {
    st.openSettings(null);
    return st.openSaved(rowId);
  }
  const native = rowId.replace(/^(cx|dv):/, "");
  const open = Object.values(st.sessions).find((s) => s.mark !== undefined && (s.id === rowId || s.nativeId === native));
  if (!open) return st.say("找不到这个会话：它可能已经从列表里移除了。");
  st.openSettings(null);
  st.select(open.id);
}

// ------------------------------------------------------------------------------------------------- the switches

function Switches() {
  const enabled = useStore((s) => s.memoryEnabled);
  const engines = useStore((s) => s.memoryEngines);
  const recall = useStore((s) => s.memoryRecall);
  const setEnabled = useStore((s) => s.setMemoryEnabled);
  const setRecall = useStore((s) => s.setMemoryRecall);
  const capture = useStore((s) => s.memoryCapture);
  const curate = useStore((s) => s.memoryCurate);
  const setCapture = useStore((s) => s.setMemoryCapture);
  const setCurate = useStore((s) => s.setMemoryCurate);
  const setEngines = useStore((s) => s.setMemoryEngines);
  return (
    <>
      <Field label="共享记忆" hint="打开后，WittyCode 自己的代理循环（ChatGPT Plan、API Provider）和 Devin 读写同一份笔记：你的偏好、项目的约定、踩过的坑。新会话开头会带一份标题清单，需要细节时它们自己去查。">
        <Switch on={enabled} onChange={setEnabled} label="共享记忆" />
      </Field>
      {enabled && (
        <Field label="给哪些引擎" hint="只对新开的和重新连接的会话生效，已经在跑的会话不变。">
          <div className="mem-engines">
            {ENGINES.map((engine) => (
              <label key={engine} className="mem-engine">
                <Switch on={engines.includes(engine)} onChange={(on) => setEngines(ENGINES.filter((e) => (e === engine ? on : engines.includes(e))))} label={`给 ${engineName(engine)}`} />
                {engineName(engine)}
              </label>
            ))}
          </div>
        </Field>
      )}
      {enabled && (
        <Field label="新会话开头带上相关笔记" hint="只在新会话的第一条消息：除了笔记目录，WittyCode 再在笔记里找和这条消息有关的几条（最多 3 条）连同内容一起交给模型，页面上会写明带了哪些。之后的消息不再带笔记，模型需要时会自己去查记忆，省得每一轮都占上下文。">
          <Switch on={recall} onChange={setRecall} label="新会话开头带上相关笔记" />
        </Field>
      )}
      {enabled && (
        <Field label="说了「记住」就直接记下" hint="你在消息里明确说「记住…」「以后都…」「从现在起…」，WittyCode 直接写一条笔记，并在对话里告诉你，可以随时改或忘记。只认明确的话，不会把随口一说当成要记的事；含密钥的不记。">
          <Switch on={capture} onChange={setCapture} label="说了记住就直接记下" />
        </Field>
      )}
      {enabled && (
        <Field label="自动记笔记，每周回顾一次" hint="一轮结束后，WittyCode 用同一个引擎再问一次这一轮里有没有值得长期记住的事。把握够大的直接写成笔记，马上生效，不再逐条问你；攒到每周一次，让你一次看完、一次确认。你留下和丢掉的选择会让它越来越会筛。每次是一个很小的请求：ChatGPT Plan 用它的快速模型；Devin 用便宜的模型、不留在会话列表里。">
          <Switch on={curate} onChange={setCurate} label="自动记笔记，每周回顾一次" />
        </Field>
      )}
      {!enabled && <p className="page-note">共享记忆已关闭：新会话不会再带上笔记，也不能读写它们。下面的笔记都还在。</p>}
    </>
  );
}

// ------------------------------------------------------------------------------- the weekly review of what the application wrote

/** A note of the review as the note editor takes it. */
function asEntry(item: MemoryReviewItem, edit?: ReviewEdit): MemoryEntry {
  const who = { kind: "agent", engine: item.engine } as const;
  return {
    id: item.id,
    title: edit?.title ?? item.title,
    text: edit?.text ?? item.text,
    scope: edit?.scope ?? item.scope,
    project: item.project,
    projectName: item.projectName,
    kind: edit?.kind ?? item.kind,
    tags: edit?.tags ?? item.tags,
    created: item.at,
    updated: item.at,
    status: "active",
    rev: 1,
    source: who,
    editor: who,
  };
}

const dayOf = (ms: number) => {
  const d = new Date(ms);
  return `${d.getMonth() + 1} 月 ${d.getDate()} 日`;
};

/** The notes the application wrote by itself since the last review. They are in the memory already; the user looks at them
 *  once, in one go: what is ticked stays, the rest is forgotten, and the choice teaches the filter what to write next. */
function WeeklyReview() {
  const latest = useStore((s) => s.memory.latest);
  const waiting = useStore((s) => s.memory.pending);
  const [review, setReview] = useState<MemoryReview | null>(null);
  const [asked, setAsked] = useState(false);
  const [later, setLater] = useState(false);
  const [kept, setKept] = useState<Record<string, boolean>>({});
  const [edits, setEdits] = useState<Record<string, ReviewEdit>>({});
  const [editId, setEditId] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [done, setDone] = useState<MemoryReviewSummary | null>(null);
  useEffect(() => {
    let alive = true;
    bridge.memory
      .review()
      .then((r) => {
        if (!alive) return;
        setReview(r);
        // (what the user already ticked or cleared stays; a note seen for the first time starts as the filter suggests)
        setKept((was) => Object.fromEntries(r.items.map((i) => [i.id, was[i.id] ?? i.suggestKeep])));
      })
      .catch((error) => {
        if (alive) say(messageOf(error));
      });
    return () => {
      alive = false;
    };
  }, [waiting, latest]);
  if (!review || (review.pending === 0 && !done)) return null;
  const open = review.pending > 0 && (asked || (review.due && !later));
  const ticked = review.items.filter((i) => kept[i.id]);
  const rest = review.items.length - ticked.length;
  const apply = async () => {
    if (busy) return;
    setBusy(true);
    try {
      const keep = ticked.map((i) => i.id);
      const wanted = Object.fromEntries(keep.filter((id) => edits[id]).map((id) => [id, edits[id]!]));
      const r = await run(bridge.memory.reviewApply({ keep, drop: review.items.filter((i) => !kept[i.id]).map((i) => i.id), edits: wanted }));
      if (!r) return;
      setDone(r);
      setEdits({});
      setEditId(null);
      setAsked(false);
      setReview(null);
    } finally {
      setBusy(false);
    }
  };
  return (
    <section className="mem-sec" aria-label="每周回顾">
      <header className="mem-head">
        <h3>
          每周回顾 {review.pending > 0 && <span className="mem-new">{review.pending} 条</span>}
        </h3>
        {review.pending > 0 && !open && (
          <button type="button" className="btn small" onClick={() => setAsked(true)}>
            {review.due ? "回顾" : "现在回顾"}
          </button>
        )}
      </header>
      {done && (
        <p className="mem-lead" role="status">
          上次回顾：保留 {done.kept} 条，丢弃 {done.dropped} 条
          {done.autoDropped + done.autoKept > 0 && `；按你的选择，又自动丢掉 ${done.autoDropped} 条不像你想要的，留下 ${done.autoKept} 条很像的`}。已从你的 {done.labels} 次选择里学习，之后少写你不要的那类。
        </p>
      )}
      {review.pending > 0 && !open && (
        <p className="mem-lead">
          这一周 WittyCode 自己记下了 {review.pending} 条笔记，已经生效。{review.due ? "到回顾的时候了：一次看完，不要的丢掉。" : review.next ? `下一次回顾在 ${dayOf(review.next)}，也可以现在就看。` : ""}
        </p>
      )}
      {open && (
        <>
          <p className="mem-lead">
            这些笔记已经生效，代理现在就读得到。勾上的留下，没勾的丢掉（可以在记忆列表里找回）；最没把握的排在前面。你的选择会教 WittyCode 之后少记你不要的
            {review.labels > 0 ? `（已学习 ${review.labels} 次选择）` : "（这是第一次，它还没有学过）"}。
            {review.total > review.items.length && `这次先看 ${review.items.length} 条，还有 ${review.total - review.items.length} 条留到下一次。`}
            {review.skipped > 0 && `这段时间有 ${review.skipped} 条把握太小，没有写。`}
          </p>
          <ul className="mem-sugs">
            {review.items.map((item) => {
              const edit = edits[item.id];
              const on = !!kept[item.id];
              return (
                <li key={item.id} className={`mem-sug mem-rev${on ? "" : " dropped"}`}>
                  {editId === item.id ? (
                    <NoteEditor
                      initial={asEntry(item, edit)}
                      saveLabel="保存修改"
                      onSave={async (values) => {
                        setEdits((was) => ({ ...was, [item.id]: { title: values.title.trim(), text: values.text.trim(), kind: values.kind, scope: values.scope, tags: parseTags(values.tags) } }));
                        setKept((was) => ({ ...was, [item.id]: true }));
                        setEditId(null);
                        return true;
                      }}
                      onCancel={() => setEditId(null)}
                    />
                  ) : (
                    <div className="mem-rev-row">
                      <Switch on={on} onChange={(v) => setKept((was) => ({ ...was, [item.id]: v }))} label={`保留「${edit?.title ?? item.title}」`} />
                      <div className="mem-rev-body">
                        <div className="mem-sug-head">
                          <span className={`mem-kind k-${edit?.kind ?? item.kind}`}>{kindLabel(edit?.kind ?? item.kind)}</span>
                          <span className="mem-title">{edit?.title ?? item.title}</span>
                          <span className="mem-scope">{(edit?.scope ?? item.scope) === "global" ? "所有项目" : item.projectName || "项目"}</span>
                        </div>
                        <div className="mem-text selectable">{edit?.text ?? item.text}</div>
                        <div className="mem-meta">
                          {engineName(item.engine)}
                          {item.conversation && (
                            <>
                              {" · 会话 "}
                              {item.row ? (
                                <button type="button" className="mem-link" onClick={() => openConversation(item.row)}>
                                  {item.conversation}
                                </button>
                              ) : (
                                item.conversation
                              )}
                            </>
                          )}
                          {" · "}
                          {formatRelative(item.at)}
                          {item.why && ` · ${item.why}`}
                          {" · "}
                          <span className="mem-score" title={item.explain.length ? `和你以前的选择相比：${item.explain.join("、")}` : "还没有可比的选择"}>
                            把握 {Math.round(item.score * 100)}%
                          </span>
                          {edit && " · 已改"}
                        </div>
                      </div>
                      <button type="button" className="btn small" onClick={() => setEditId(item.id)}>
                        <Pencil size={13} />
                        改一改
                      </button>
                    </div>
                  )}
                </li>
              );
            })}
          </ul>
          <div className="row-btns mem-rev-foot">
            <button type="button" className="btn primary" disabled={busy || editId !== null} onClick={() => void apply()}>
              确认：留下 {ticked.length} 条{rest > 0 ? `，丢掉 ${rest} 条` : ""}
            </button>
            <button type="button" className="btn" disabled={busy || rest === 0} onClick={() => setKept(Object.fromEntries(review.items.map((i) => [i.id, true])))}>
              全部留下
            </button>
            <button
              type="button"
              className="btn"
              disabled={busy}
              onClick={() => {
                setAsked(false);
                setLater(true);
              }}
            >
              稍后再看
            </button>
          </div>
        </>
      )}
    </section>
  );
}

// ------------------------------------------------------------------------------------------ what agents wrote lately

function Recent() {
  const latest = useStore((s) => s.memory.latest);
  const seen = useStore((s) => s.memory.seen);
  const unseen = useStore((s) => s.memory.unseen);
  const [changes, setChanges] = useState<MemoryChange[] | null>(null);
  const [all, setAll] = useState(false);
  useEffect(() => {
    let alive = true;
    bridge.memory
      .changes()
      .then((r) => alive && setChanges(r.changes))
      .catch((error) => {
        if (!alive) return;
        setChanges([]);
        say(messageOf(error));
      });
    return () => {
      alive = false;
    };
  }, [latest]);
  const shown = changes ? (all ? changes : changes.slice(0, RECENT_SHOWN)) : [];
  return (
    <section className="mem-sec" aria-label="最近由代理写入">
      <header className="mem-head">
        <h3>
          最近由代理写入 {unseen > 0 && <span className="mem-new">{unseen} 条新的</span>}
        </h3>
        <button type="button" className="btn small" disabled={unseen === 0} onClick={() => void run(bridge.memory.markSeen())}>
          全部标为已看过
        </button>
      </header>
      {changes && changes.length === 0 && <div className="mem-empty">代理还没有写过笔记。它们觉得值得记下来的东西，会出现在这里，你可以撤销。</div>}
      <ul className="mem-changes">
        {shown.map((c) => {
          const conversation = whoConversation(c.by);
          return (
            <li key={c.seq} className={`mem-change${isUnseen(c, seen) ? " new" : ""}`}>
              <div className="mem-change-main">
                <div className="mem-change-line">
                  <b>{whoName(c.by)}</b> {changeVerb(c.op)}「{c.title || "（已不在）"}」
                </div>
                <div className="mem-meta">
                  {c.scope === "global" ? "全局" : c.projectName || (c.scope ? "项目" : "")}
                  {c.scope ? " · " : ""}
                  {formatRelative(c.at)}
                  {conversation && (
                    <>
                      {" · 会话 "}
                      <button type="button" className="mem-link" onClick={() => openConversation(conversation.rowId)}>
                        {conversation.title || "（无标题）"}
                      </button>
                    </>
                  )}
                </div>
              </div>
              <button
                type="button"
                className="btn small"
                title="把这次改动撤回去"
                onClick={() => void run(bridge.memory.undo(c.seq), () => `已撤销：${whoName(c.by)}${changeVerb(c.op)}「${c.title}」。`)}
              >
                <Undo2 size={13} />
                撤销
              </button>
            </li>
          );
        })}
      </ul>
      {changes && changes.length > RECENT_SHOWN && (
        <button type="button" className="mem-more" onClick={() => setAll((v) => !v)}>
          {all ? "收起" : `显示全部 ${changes.length} 条`}
        </button>
      )}
    </section>
  );
}

// ------------------------------------------------------------------------------------------------------- the editor

interface Values {
  title: string;
  text: string;
  kind: MemoryKind;
  scope: MemoryScope;
  project: string;
  tags: string;
}

function NoteEditor({ initial, saveLabel, onSave, onCancel }: { initial?: MemoryEntry; saveLabel?: string; onSave: (values: Values) => Promise<boolean>; onCancel: () => void }) {
  const projects = useStore((s) => s.projects);
  const here = useStore((s) => s.sessions[s.currentId ?? ""]?.projectId ?? "");
  const [v, setV] = useState<Values>(() => {
    if (initial) {
      const own = projects.find((p) => samePath(p.id, initial.project));
      return { title: initial.title, text: initial.text, kind: initial.kind, scope: initial.scope, project: own?.id ?? initial.project, tags: initial.tags.join(", ") };
    }
    const first = here || projects[0]?.id || "";
    return { title: "", text: "", kind: "fact", scope: first ? "project" : "global", project: first, tags: "" };
  });
  const [busy, setBusy] = useState(false);
  const set = (patch: Partial<Values>) => setV((x) => ({ ...x, ...patch }));
  const titleError = v.title.length > MAX_TITLE ? `标题 ${v.title.length} 个字，请缩到 ${MAX_TITLE} 个字以内。` : undefined;
  const textError = v.text.length > MAX_TEXT ? `内容 ${v.text.length} 个字，请缩到 ${MAX_TEXT} 个字以内。` : undefined;
  const ready = !!v.title.trim() && !!v.text.trim() && !titleError && !textError && (v.scope === "global" || !!v.project);
  const submit = async () => {
    if (!ready || busy) return;
    setBusy(true);
    try {
      await onSave(v);
    } finally {
      setBusy(false);
    }
  };
  const projectName = projects.find((p) => p.id === v.project)?.name ?? (v.project ? (initial?.projectName ?? v.project) : "选择项目");
  const projectItems: MenuItem[] = projects.map((p) => ({ id: p.id, label: p.name, description: p.path, checked: p.id === v.project, onSelect: () => set({ project: p.id }) }));
  return (
    <div
      className="mem-editor"
      onKeyDown={(e) => {
        if (e.key === "Escape") {
          e.stopPropagation();
          onCancel();
        } else if (e.key === "Enter" && (e.ctrlKey || e.metaKey)) void submit();
      }}
    >
      <Stack label="标题" error={titleError} hint={`一句话说清是什么，${MAX_TITLE} 个字以内。`}>
        <TextInput value={v.title} onChange={(title) => set({ title })} label="标题" invalid={!!titleError} autoFocus />
      </Stack>
      <Stack label="内容" error={textError} hint={`${v.text.length} / ${MAX_TEXT}。写结论和做法，不要写密钥或密码。`}>
        <textarea className={`textin area selectable${textError ? " invalid" : ""}`} rows={5} value={v.text} aria-label="内容" spellCheck={false} onChange={(e) => set({ text: e.target.value })} />
      </Stack>
      <div className="pe-row">
        <Stack label="类型">
          <Seg<MemoryKind> value={v.kind} onChange={(kind) => set({ kind })} options={KINDS.map((k) => ({ id: k.id, label: k.label }))} />
        </Stack>
        <Stack label="适用范围">
          <Seg<MemoryScope>
            value={v.scope}
            onChange={(scope) => set({ scope, ...(scope === "project" && !v.project ? { project: here || projects[0]?.id || "" } : {}) })}
            options={[
              { id: "project", label: "这个项目" },
              { id: "global", label: "所有项目" },
            ]}
          />
        </Stack>
        {v.scope === "project" && (
          <Stack label="项目">
            <Select value={projectName} items={projectItems} minWidth={240} disabled={projects.length === 0} />
          </Stack>
        )}
      </div>
      <Stack label="标签（可选）" hint="用逗号分隔，方便搜索。">
        <TextInput value={v.tags} onChange={(tags) => set({ tags })} label="标签" />
      </Stack>
      <div className="row-btns">
        <button type="button" className="btn primary" disabled={!ready || busy} onClick={() => void submit()}>
          {saveLabel ?? (initial ? "保存" : "记下来")}
        </button>
        <button type="button" className="btn" onClick={onCancel}>
          取消
        </button>
      </div>
    </div>
  );
}

// ------------------------------------------------------------------------------------------------------ the notes

function NoteRow({ entry, open, editing, onToggle, onEdit, onEdited }: { entry: MemoryEntry; open: boolean; editing: boolean; onToggle: () => void; onEdit: (on: boolean) => void; onEdited: () => void }) {
  const conversation = whoConversation(entry.source);
  const forgotten = entry.status === "forgotten";
  const save = async (v: Values): Promise<boolean> => {
    const projectChanged = v.scope === "project" && !samePath(v.project, entry.project);
    const tags = parseTags(v.tags);
    const r = await run(
      bridge.memory.update({
        id: entry.id,
        ...(v.title.trim() !== entry.title ? { title: v.title.trim() } : {}),
        ...(v.text.trim() !== entry.text ? { text: v.text.trim() } : {}),
        ...(v.kind !== entry.kind ? { kind: v.kind } : {}),
        ...(tags.join("\n") !== entry.tags.join("\n") ? { tags } : {}),
        ...(v.scope !== entry.scope || projectChanged ? { scope: v.scope, ...(v.scope === "project" ? { project: v.project } : {}) } : {}),
      }),
      () => `已保存「${v.title.trim()}」。`,
    );
    if (r) onEdited();
    return !!r;
  };
  return (
    <li className={`mem-note${open ? " open" : ""}${forgotten ? " forgotten" : ""}`} data-id={entry.id}>
      <button type="button" className="mem-note-head" aria-expanded={open} onClick={onToggle}>
        <span className={`mem-kind k-${entry.kind}`}>{kindLabel(entry.kind)}</span>
        <span className="mem-title">{entry.title}</span>
        {forgotten && <span className="mem-tag">已忘记</span>}
        <span className="mem-scope">{scopeLabel(entry)}</span>
        <span className="mem-when">{formatRelative(entry.updated)}</span>
      </button>
      {open && !editing && (
        <div className="mem-note-body">
          <div className="mem-text selectable">{entry.text}</div>
          {entry.tags.length > 0 && (
            <div className="mem-tags">
              {entry.tags.map((t) => (
                <span key={t} className="mem-tag">
                  {t}
                </span>
              ))}
            </div>
          )}
          <div className="mem-meta">
            {sourceLine(entry)}
            {conversation && (
              <>
                {" · 会话 "}
                <button type="button" className="mem-link" onClick={() => openConversation(conversation.rowId)}>
                  {conversation.title || "（无标题）"}
                </button>
              </>
            )}
            {" · "}
            {formatRelative(entry.created)}
          </div>
          <div className="row-btns">
            {forgotten ? (
              <button type="button" className="btn small" onClick={() => void run(bridge.memory.restore(entry.id), () => `已恢复「${entry.title}」。`)}>
                <RotateCcw size={13} />
                恢复
              </button>
            ) : (
              <>
                <button type="button" className="btn small" onClick={() => onEdit(true)}>
                  <Pencil size={13} />
                  编辑
                </button>
                <button type="button" className="btn small danger" onClick={() => void run(bridge.memory.forget(entry.id), () => `已忘记「${entry.title}」。打开「显示已忘记」可以恢复它。`)}>
                  忘记
                </button>
              </>
            )}
          </div>
        </div>
      )}
      {open && editing && (
        <div className="mem-note-body">
          <NoteEditor initial={entry} onSave={save} onCancel={() => onEdit(false)} />
        </div>
      )}
    </li>
  );
}

function Notes({ onImport }: { onImport: () => void }) {
  const latest = useStore((s) => s.memory.latest);
  const [query, setQuery] = useState("");
  const [typed, setTyped] = useState("");
  const [scope, setScope] = useState<"all" | MemoryScope>("all");
  const [kind, setKind] = useState<"all" | MemoryKind>("all");
  const [engine, setEngine] = useState("all");
  const [withForgotten, setWithForgotten] = useState(false);
  const [view, setView] = useState<"list" | "map">("list");
  const [data, setData] = useState<{ entries: MemoryEntry[]; counts: Record<string, number> } | null>(null);
  const [creating, setCreating] = useState(false);
  const [openId, setOpenId] = useState<string | null>(null);
  const [editId, setEditId] = useState<string | null>(null);
  const ticket = useRef(0);

  useEffect(() => {
    const timer = window.setTimeout(() => setQuery(typed.trim()), 200);
    return () => window.clearTimeout(timer);
  }, [typed]);

  useEffect(() => {
    const mine = ++ticket.current;
    const filter: MemoryFilter = {
      status: withForgotten ? "all" : "active",
      ...(query ? { query } : {}),
      ...(scope !== "all" ? { scope } : {}),
      ...(kind !== "all" ? { kind } : {}),
      ...(engine !== "all" ? { engine } : {}),
    };
    bridge.memory
      .list(filter)
      .then((r) => {
        if (ticket.current !== mine) return;
        setData({ entries: r.entries, counts: r.counts });
        // A note that left the list (forgotten, or filtered out) does not stay open behind it.
        setOpenId((id) => (id !== null && r.entries.some((e) => e.id === id) ? id : null));
      })
      .catch((error) => {
        if (ticket.current !== mine) return;
        setData({ entries: [], counts: {} });
        say(messageOf(error));
      });
  }, [query, scope, kind, engine, withForgotten, latest]);

  const create = useCallback(async (v: Values): Promise<boolean> => {
    const r = await run(
      bridge.memory.create({ title: v.title.trim(), text: v.text.trim(), scope: v.scope, kind: v.kind, tags: parseTags(v.tags), ...(v.scope === "project" ? { project: v.project } : {}) }),
      () => `已记下「${v.title.trim()}」。`,
    );
    if (r) {
      setCreating(false);
      setOpenId(r.entry.id);
    }
    return !!r;
  }, []);

  const kindItems: MenuItem[] = [{ id: "all", label: "所有类型" }, ...KINDS.map((k) => ({ id: k.id, label: k.label, description: k.hint }))].map((k) => ({ ...k, checked: kind === k.id, onSelect: () => setKind(k.id as "all" | MemoryKind) }));
  const present = new Set((data?.entries ?? []).map((e) => (e.source.kind === "agent" ? e.source.engine : e.source.kind === "app" ? "app" : "user")).filter(Boolean));
  if (engine !== "all") present.add(engine); // (the chosen one stays in the menu while its notes are all there is)
  const sources: { id: string; label: string }[] = [
    { id: "all", label: "所有来源" },
    { id: "user", label: "我" },
    { id: "app", label: "应用整理" },
    ...ENGINES.map((e) => ({ id: e as string, label: engineName(e) })),
    // (what an import brought in: whatever other application the files came from)
    ...[...present].filter((id) => id !== "user" && id !== "app" && !ENGINES.some((known) => known === id)).sort().map((id) => ({ id, label: engineName(id) })),
  ];
  const sourceItems: MenuItem[] = sources.map((s) => ({ id: s.id, label: s.label, checked: engine === s.id, onSelect: () => setEngine(s.id) }));
  const active = data?.counts.active ?? 0;
  const forgotten = data?.counts.forgotten ?? 0;
  const filtering = query !== "" || scope !== "all" || kind !== "all" || engine !== "all";
  return (
    <section className="mem-sec" aria-label="笔记">
      <header className="mem-head">
        <h3>
          笔记 <span className="mem-count num">{data ? `${active}${forgotten ? ` · 已忘记 ${forgotten}` : ""}` : ""}</span>
        </h3>
        <div className="mem-head-btns">
          <Seg<"list" | "map">
            value={view}
            onChange={setView}
            options={[
              { id: "list", label: "列表" },
              { id: "map", label: "星图" },
            ]}
          />
          <button type="button" className="btn small" onClick={onImport}>
            <FolderInput size={13} />
            从其他应用导入…
          </button>
          <button type="button" className="btn small" onClick={() => setCreating((v) => !v)}>
            <Plus size={13} />
            新建笔记
          </button>
        </div>
      </header>
      {creating && (
        <div className="mem-create">
          <NoteEditor onSave={create} onCancel={() => setCreating(false)} />
        </div>
      )}
      <div className="mem-tools">
        <label className="mem-search">
          <Search size={14} />
          <input className="selectable" value={typed} placeholder="搜索标题、内容、标签" aria-label="搜索笔记" spellCheck={false} onChange={(e) => setTyped(e.target.value)} />
        </label>
        <Seg<"all" | MemoryScope>
          value={scope}
          onChange={setScope}
          options={[
            { id: "all", label: "全部" },
            { id: "project", label: "项目" },
            { id: "global", label: "全局" },
          ]}
        />
        <Select value={kind === "all" ? "所有类型" : kindLabel(kind)} items={kindItems} minWidth={150} />
        <Select value={sources.find((s) => s.id === engine)?.label ?? "所有来源"} items={sourceItems} minWidth={130} />
        <label className="mem-toggle">
          <Switch on={withForgotten} onChange={setWithForgotten} label="显示已忘记" />
          显示已忘记
        </label>
      </div>
      {view === "map" && (
        <div className="mem-map">
          <MemoryStarMap
            entries={data?.entries ?? []}
            selectedId={openId}
            onSelect={(id) => {
              setOpenId(id);
              setEditId(null);
            }}
          />
        </div>
      )}
      {view === "list" && data && data.entries.length === 0 && (
        <div className="mem-empty">{filtering ? "没有符合条件的笔记。" : "还没有笔记。让代理在对话里记下来，或者点「新建笔记」自己写一条。"}</div>
      )}
      {(view === "list" || openId !== null) && (
        <ul className="mem-notes">
          {(view === "list" ? data?.entries : data?.entries.filter((e) => e.id === openId))?.map((entry) => (
            <NoteRow
              key={entry.id}
              entry={entry}
              open={view === "map" || openId === entry.id}
              editing={editId === entry.id}
              onToggle={() => {
                setOpenId(openId === entry.id ? null : entry.id);
                setEditId(null);
              }}
              onEdit={(on) => setEditId(on ? entry.id : null)}
              onEdited={() => setEditId(null)}
            />
          ))}
        </ul>
      )}
    </section>
  );
}

export function MemoryPage() {
  const [importing, setImporting] = useState(false);
  return (
    <>
      <Switches />
      <WeeklyReview />
      <Recent />
      {importing && <ImportWizard onClose={() => setImporting(false)} />}
      <Notes onImport={() => setImporting(true)} />
    </>
  );
}
