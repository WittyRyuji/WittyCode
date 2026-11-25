import { useEffect, useState } from "react";
import { LIVE } from "../mode";
import { liveCall } from "../liveBridge";
import { messageOf } from "../lib/errors";
import { useStore } from "../store";
import type { ExpertCatalogView, ExpertDetail, ExpertDraft, ExpertSummary } from "../protocol";
import { Select, Stack, Switch } from "./formkit";

const DESIGN_WORK_TYPES = [
  { id: "execute", label: "执行" },
  { id: "manage", label: "管理" },
  { id: "review", label: "审查" },
  { id: "research", label: "研究" },
  { id: "verify", label: "验证" },
  { id: "observe", label: "观察" },
];

function designExpert(stableId: string, name: string, workType: string, tags: string[], enabled = true): ExpertSummary {
  return {
    stableId, name, workType, tags, enabled, version: 1,
    mdPath: `design/${stableId}.md`, contentHash: stableId, bodyTokens: 180,
    role: workType, created: 1, updated: 1, builtin: true,
  };
}

let designCatalog = {
  revision: 1,
  entries: [
    designExpert("builtin-coder", "执行专家", "execute", ["coder", "implement"]),
    designExpert("builtin-runner", "运行专家", "research", ["runner", "inspect"]),
    designExpert("builtin-reviewer", "审查专家", "review", ["quality", "verify"]),
  ],
  markdown: {
    "builtin-coder": "---\nname: 执行专家\nwork_type: execute\npermission: Coder\n---\n按目标完成实现，并保持改动可验证。",
    "builtin-runner": "---\nname: 运行专家\nwork_type: research\npermission: Runner\n---\n运行命令、收集证据并回报事实。",
    "builtin-reviewer": "---\nname: 审查专家\nwork_type: review\npermission: QualitySampler\n---\n核对实现是否满足验收条件。",
  } as Record<string, string>,
};

const clone = <T,>(value: T): T => JSON.parse(JSON.stringify(value)) as T;
const viewOf = () => ({ revision: designCatalog.revision, experts: clone(designCatalog.entries), workTypes: DESIGN_WORK_TYPES });
const splitTags = (text: string) => [...new Set(text.split(/\s+/).map((tag) => tag.trim()).filter(Boolean))];

async function designCall(method: string, params: Record<string, unknown>): Promise<unknown> {
  if (method === "experts.list") return viewOf();
  if (method === "experts.get") {
    const expert = designCatalog.entries.find((entry) => entry.stableId === params.stableId);
    if (!expert) throw new Error("没有找到这个专家。");
    return { expert: clone(expert), markdown: designCatalog.markdown[expert.stableId] ?? "" };
  }
  const id = String(params.stableId ?? "").trim();
  if (method === "experts.setEnabled") {
    const expert = designCatalog.entries.find((entry) => entry.stableId === id);
    if (!expert || expert.builtin) throw new Error(expert?.builtin ? "内置专家身份是只读的。" : "没有找到这个专家。");
    expert.enabled = params.enabled === true;
    expert.version += 1;
    designCatalog.revision += 1;
    return viewOf();
  }
  if (method === "experts.delete") {
    const expert = designCatalog.entries.find((entry) => entry.stableId === id);
    if (!expert || expert.builtin) throw new Error(expert?.builtin ? "内置专家身份是只读的。" : "没有找到这个专家。");
    designCatalog.entries = designCatalog.entries.filter((entry) => entry.stableId !== id);
    delete designCatalog.markdown[id];
    designCatalog.revision += 1;
    return viewOf();
  }
  if (method === "experts.create" || method === "experts.update") {
    const name = String(params.name ?? "").trim();
    const workType = String(params.workType ?? "");
    const markdown = String(params.markdown ?? "");
    if (!id || !name || !markdown.trim()) throw new Error("专家设置不合法。");
    const tags = splitTags(String(params.tags ?? ""));
    if (method === "experts.create") {
      if (designCatalog.entries.some((entry) => entry.stableId === id)) throw new Error("这个专家标识已经存在。");
      const expert: ExpertSummary = { ...designExpert(id, name, workType, tags, params.enabled !== false), builtin: false, created: Date.now() / 1000, updated: Date.now() / 1000 };
      designCatalog.entries.push(expert);
      designCatalog.markdown[id] = markdown;
    } else {
      const expert = designCatalog.entries.find((entry) => entry.stableId === id);
      if (!expert || expert.builtin) throw new Error(expert?.builtin ? "内置专家身份是只读的。" : "没有找到这个专家。");
      Object.assign(expert, { name, workType, tags, enabled: params.enabled !== false, updated: Date.now() / 1000 });
      expert.version += 1;
      designCatalog.markdown[id] = markdown;
    }
    designCatalog.revision += 1;
    return viewOf();
  }
  throw new Error("没有这个操作。");
}

async function expertCall(method: "experts.list", params?: Record<string, never>): Promise<ExpertCatalogView>;
async function expertCall(method: "experts.get", params: { stableId: string }): Promise<ExpertDetail>;
async function expertCall(method: "experts.create" | "experts.update" | "experts.setEnabled" | "experts.delete", params: Record<string, unknown>): Promise<ExpertCatalogView>;
async function expertCall(method: string, params: Record<string, unknown> = {}): Promise<unknown> {
  if (!LIVE) return designCall(method, params);
  switch (method) {
    case "experts.list": return liveCall("experts.list");
    case "experts.get": return liveCall("experts.get", params as { stableId: string });
    case "experts.create": return liveCall("experts.create", params as unknown as Required<ExpertDraft>);
    case "experts.update": return liveCall("experts.update", params as unknown as ExpertDraft & { stableId: string });
    case "experts.setEnabled": return liveCall("experts.setEnabled", params as { stableId: string; enabled: boolean });
    default: return liveCall("experts.delete", params as { stableId: string });
  }
}

/** Versioned expert catalog editor. It edits application data directly (not session state), so it intentionally talks to
 *  the application API instead of adding another copy to the live-data reducer. */
export function ExpertCatalog() {
  const s = useStore();
  const [view, setView] = useState<ExpertCatalogView | null>(null);
  const [detail, setDetail] = useState<ExpertDetail | null>(null);
  const [creating, setCreating] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [result, setResult] = useState("");
  const [draft, setDraft] = useState({ stableId: "", name: "", workType: "execute", tags: "", enabled: true, markdown: "" });

  const load = async (selectId?: string) => {
    setBusy(true); setError("");
    try {
      const next = await expertCall("experts.list");
      setView(next);
      const id = selectId ?? detail?.expert.stableId ?? next.experts[0]?.stableId;
      if (id) await open(id, true);
    } catch (e) { setError(messageOf(e)); }
    finally { setBusy(false); }
  };

  const open = async (stableId: string, silent = false) => {
    if (!silent) { setBusy(true); setError(""); setResult(""); }
    try {
      const next = await expertCall("experts.get", { stableId });
      setDetail(next); setCreating(false);
      setDraft({
        stableId: next.expert.stableId,
        name: next.expert.name,
        workType: next.expert.workType,
        tags: next.expert.tags.join(" "),
        enabled: next.expert.enabled,
        markdown: next.markdown,
      });
    } catch (e) { setError(messageOf(e)); }
    finally { if (!silent) setBusy(false); }
  };

  useEffect(() => { void load(); }, []);

  const workItems = (view?.workTypes ?? DESIGN_WORK_TYPES).map((type) => ({ id: type.id, label: `${type.label} · ${type.id}`, checked: draft.workType === type.id, onSelect: () => setDraft((cur) => ({ ...cur, workType: type.id })) }));
  const selected = detail?.expert;
  const readOnly = Boolean(selected?.builtin) && !creating;
  const tags = splitTags(draft.tags);
  const dirty = Boolean(detail) && (
    draft.name !== detail!.expert.name || draft.workType !== detail!.expert.workType ||
    tags.join(" ") !== detail!.expert.tags.join(" ") || draft.enabled !== detail!.expert.enabled || draft.markdown !== detail!.markdown
  );

  const choose = (stableId: string) => {
    if (!dirty || creating) return void open(stableId);
    s.openDialog({ title: "放弃未保存的专家更改？", body: "当前表单还没有保存。", confirmLabel: "放弃并切换", danger: true, onConfirm: () => void open(stableId) });
  };
  const startCreate = () => {
    setCreating(true); setDetail(null); setError(""); setResult("");
    setDraft({ stableId: "", name: "", workType: "execute", tags: "", enabled: true, markdown: "---\ndescription: \ntools: read\n---\n在这里写专家指令。" });
  };
  const save = async () => {
    setBusy(true); setError(""); setResult("");
    try {
      const method = creating ? "experts.create" : "experts.update";
      const next = await expertCall(method, { ...draft, tags: draft.tags });
      setView(next); setCreating(false);
      await open(draft.stableId, true);
      setResult(creating ? "专家已创建。" : `已保存版本 ${detail?.expert.version ? detail.expert.version + 1 : ""}。`);
    } catch (e) { setError(messageOf(e)); }
    finally { setBusy(false); }
  };
  const setEnabled = async (enabled: boolean) => {
    if (!selected) return;
    setBusy(true); setError(""); setResult("");
    try {
      setView(await expertCall("experts.setEnabled", { stableId: selected.stableId, enabled }));
      await open(selected.stableId, true);
      setResult(enabled ? "专家已启用。" : "专家已停用；已冻结的历史版本仍可追踪。");
    } catch (e) { setError(messageOf(e)); }
    finally { setBusy(false); }
  };
  const remove = () => {
    if (!selected) return;
    s.openDialog({
      title: `删除「${selected.name}」？`,
      body: "目录会留下不可运行的删除记录；已冻结的历史版本仍留在版本库中。",
      confirmLabel: "删除", danger: true,
      onConfirm: () => void (async () => {
        setBusy(true); setError(""); setResult("");
        try {
          const next = await expertCall("experts.delete", { stableId: selected.stableId });
          setView(next); setDetail(null); setCreating(false);
          if (next.experts[0]) await open(next.experts[0].stableId, true);
          setResult("专家已删除。");
        } catch (e) { setError(messageOf(e)); }
        finally { setBusy(false); }
      })(),
    });
  };

  return <section className="expert-catalog providers" aria-label="Fusion 专家目录">
    <h3 className="settings-section-title">专家目录</h3>
    <p className="page-note">专家身份按版本保存。这里改的是全局目录；会话运行时接入后按稳定标识与哈希冻结引用。</p>
    <div className="pcard">
      <div className="pcard-head">
        <div>
          <h4>目录</h4>
          <div className="pstate">{(view?.experts.length ?? 0)} 个身份 · 修订 {view?.revision ?? "—"}</div>
        </div>
        <div className="pcard-actions">
          <button type="button" className="btn small" disabled={busy} onClick={() => void load()}>刷新</button>
          <button type="button" className="btn small primary" disabled={busy} onClick={startCreate}>新建专家</button>
        </div>
      </div>
      <div className="pcard-row expert-picker-row">
        <Select
          value={creating ? "新建专家" : selected ? selected.name : "选择专家"}
          ariaLabel="专家目录下拉"
          disabled={busy || !view?.experts.length}
          items={(view?.experts ?? []).map((expert) => ({
            id: expert.stableId,
            label: `${expert.enabled ? "" : "已停用 · "}${expert.name}`,
            description: `${expert.workType} · v${expert.version}${expert.builtin ? " · 内置" : ""}${expert.tags.length ? ` · ${expert.tags.join(" ")}` : ""}`,
            checked: selected?.stableId === expert.stableId,
            onSelect: () => choose(expert.stableId),
          }))}
        />
      </div>
    </div>

    {(detail || creating) && <div className="pcard expert-editor">
      <div className="pcard-head">
        <div>
          <h4>{creating ? "新建专家" : draft.name || selected?.name}</h4>
          <div className="pstate">{creating ? "保存后进入版本目录" : `v${selected?.version} · ${selected?.enabled ? "启用" : "停用"}${selected?.builtin ? " · 内置只读" : ""}`}</div>
        </div>
        {!creating && <div className="pcard-actions">
          <button type="button" className="btn small" disabled={busy || readOnly} onClick={() => void setEnabled(!selected?.enabled)}>{selected?.enabled ? "停用" : "启用"}</button>
          <button type="button" className="btn small danger" disabled={busy || readOnly} onClick={remove}>删除</button>
        </div>}
      </div>
      {readOnly && <p className="pcard-note">内置身份只读；可以复制其内容后新建自定义专家。</p>}
      <div className="pe-row">
        <div style={{ flex: "1 1 240px" }}><Stack label="稳定标识" hint="创建后不可改；运行时按它和版本哈希冻结引用。"><input className="textin mono selectable" value={draft.stableId} aria-label="专家稳定标识" placeholder="例如 review-security" disabled={busy || !creating} onChange={(e) => setDraft({ ...draft, stableId: e.target.value.trim() })} /></Stack></div>
        <div style={{ flex: "1 1 240px" }}><Stack label="显示名"><input className="textin selectable" value={draft.name} aria-label="专家显示名" disabled={busy || readOnly} onChange={(e) => setDraft({ ...draft, name: e.target.value })} /></Stack></div>
      </div>
      <div className="pe-row">
        <div style={{ flex: "1 1 240px" }}><Stack label="工作类型" hint="类型决定权限上限；Markdown 只能收紧，不能抬高。"><Select value={workItems.find((item) => item.checked)?.label ?? draft.workType} ariaLabel="专家工作类型" disabled={busy || readOnly} items={workItems} /></Stack></div>
        <div style={{ flex: "0 0 auto" }}><Stack label="启用"><div className="pe-check"><Switch on={draft.enabled} disabled={busy || readOnly} label="专家启用" onChange={(enabled) => setDraft({ ...draft, enabled })} /><span>{draft.enabled ? "启用" : "停用"}</span></div></Stack></div>
      </div>
      <Stack label="Tags" hint="用空格分隔；保存时去重，大小写不敏感但保留第一次的写法。">
        <input className="textin selectable" value={draft.tags} aria-label="专家 Tags" placeholder="security review api" disabled={busy || readOnly} onChange={(e) => setDraft({ ...draft, tags: e.target.value })} />
        <div className="expert-tag-chips">{tags.map((tag) => <span key={tag} className="src-tag">{tag}</span>)}</div>
      </Stack>
      <Stack label="Markdown 指令" hint="正文是运行时注入的专家说明；目录字段决定名称、工作类型和标签，front matter 只能收紧工具、角色或复用权限。">
        <textarea className="rules-editor selectable" rows={13} value={draft.markdown} aria-label="专家 Markdown 指令" disabled={busy || readOnly} spellCheck={false} onChange={(e) => setDraft({ ...draft, markdown: e.target.value })} />
      </Stack>
      {selected && <div className="pcard-meta selectable">{selected.mdPath} · sha256:{selected.contentHash.slice(0, 16)} · 约 {selected.bodyTokens} tokens</div>}
      {!readOnly && <div className="pe-btns">
        <button type="button" className="btn primary" disabled={busy || (!creating && !dirty) || !draft.stableId.trim() || !draft.name.trim() || !draft.markdown.trim()} onClick={() => void save()}>{creating ? "创建专家" : "保存新版本"}</button>
        {creating && <button type="button" className="btn" disabled={busy} onClick={() => { setCreating(false); if (view?.experts[0]) void open(view.experts[0].stableId); }}>取消</button>}
        {!creating && dirty && <span className="field-hint">有未保存的更改；保存会追加一个新版本。</span>}
      </div>}
    </div>}
    {error && <p role="alert" className="connection-error">{error}</p>}
    {result && <p role="status" className="connection-result">{result}</p>}
  </section>;
}
