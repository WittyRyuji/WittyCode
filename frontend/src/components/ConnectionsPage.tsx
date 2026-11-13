import { useEffect, useState } from "react";
import { LIVE } from "../mode";
import { liveCall } from "../liveBridge";
import { useStore } from "../store";
import { messageOf } from "../lib/errors";
import type { ConnectionEntry, ConnectionPreview, ConnectionScan } from "../protocol";
import { Switch } from "./formkit";
import "../styles/connections.css";

const KINDS = [["skill", "Skills"], ["plugin", "Plugins"], ["hook", "Hooks"], ["mcp", "MCP"]] as const;
const DEMO: ConnectionEntry[] = [
  { id: "demo-skill", kind: "skill", name: "summarize-diff", source: "C:\\Users\\me\\.agents\\skills\\summarize-diff\\SKILL.md", project: "", config: { path: "C:\\Users\\me\\.agents\\skills\\summarize-diff\\SKILL.md" }, enabled: true, imported: false, changed: false },
  { id: "demo-mcp", kind: "mcp", name: "demo-tools", source: "C:\\Users\\me\\.codex\\config.toml", project: "", config: { command: "python", args: ["-m", "demo_mcp"] }, enabled: false, imported: false, changed: false },
];

export function ConnectionsPage() {
  const projects = useStore((s) => s.projects);
  const [projectId, setProject] = useState("");
  const [kind, setKind] = useState<ConnectionEntry["kind"]>("skill");
  const [scan, setScan] = useState<ConnectionScan>({ items: [], errors: [], hookEvents: [] });
  const [path, setPath] = useState("");
  const [preview, setPreview] = useState<ConnectionPreview | null>(null);
  const [confirmed, setConfirmed] = useState(false);
  const [editing, setEditing] = useState<ConnectionEntry | null>(null);
  const [draft, setDraft] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [result, setResult] = useState("");
  const load = async () => {
    setBusy(true); setError("");
    try { setScan(LIVE ? await liveCall("connections.list", { projectId }) : { items: DEMO.map((e) => ({ ...e })), errors: [], hookEvents: ["UserPromptSubmit", "PreToolUse", "PostToolUse"] }); }
    catch (e) { setError(messageOf(e)); }
    finally { setBusy(false); }
  };
  useEffect(() => { void load(); setPreview(null); setEditing(null); setConfirmed(false); }, [projectId]);
  const openPreview = async (source: string) => {
    setBusy(true); setError(""); setConfirmed(false); setEditing(null);
    try {
      setPreview(LIVE ? await liveCall("connections.preview", { path: source, projectId }) : { preview: "demo", items: DEMO.filter((e) => e.source === source), commands: DEMO.filter((e) => e.source === source && e.kind === "mcp").map((e) => ({ name: e.name, kind: e.kind, command: String(e.config.command), args: e.config.args as string[] })) });
    } catch (e) { setError(messageOf(e)); }
    finally { setBusy(false); }
  };
  const commit = async () => {
    if (!preview || !confirmed) return;
    setBusy(true); setError("");
    try {
      if (LIVE) await liveCall("connections.import", { preview: preview.preview, confirmed: true });
      else for (const e of preview.items) { const row = DEMO.find((x) => x.id === e.id); if (row) { row.enabled = true; row.imported = true; } }
      setPreview(null); setConfirmed(false); setResult("已导入并启用。MCP 对新开／重连的会话生效，Hooks 在 WittyHarness 下一次对应事件生效。");
      await load();
    } catch (e) { setError(messageOf(e)); }
    finally { setBusy(false); }
  };
  const update = async (entry: ConnectionEntry, enabled: boolean, config?: Record<string, unknown>) => {
    const executable = entry.kind !== "skill";
    if (executable && enabled && (entry.changed || !entry.imported)) { await openPreview(entry.source); return; }
    if (executable && config !== undefined && !confirmed) {
      setEditing(entry); setDraft(JSON.stringify(entry.config, null, 2)); setConfirmed(false); setPreview(null);
      return;
    }
    setBusy(true); setError("");
    try {
      if (LIVE) await liveCall("connections.update", { id: entry.id, projectId, enabled, config, confirmed: executable && confirmed });
      else { const row = DEMO.find((e) => e.id === entry.id); if (row) Object.assign(row, { enabled, imported: true, ...(config ? { config } : {}) }); }
      setEditing(null); setConfirmed(false); await load();
    } catch (e) { setError(messageOf(e)); }
    finally { setBusy(false); }
  };
  const test = async (entry: ConnectionEntry) => {
    setBusy(true); setError("");
    try { const value = LIVE ? await liveCall("connections.test", { id: entry.id, projectId }) : { ok: true, text: "（演示）连接成功 · 2 个工具", tools: ["read", "search"] }; setResult(`${value.text}${value.tools.length ? `：${value.tools.join("、")}` : ""}`); }
    catch (e) { setError(messageOf(e)); }
    finally { setBusy(false); }
  };
  const entries = scan.items.filter((e) => e.kind === kind);
  return (
    <div className="connections">
      <p className="page-note">管理本地 Skills、Plugins、Hooks 和 MCP。兼容 Codex 的 <code>SKILL.md</code>、<code>.codex-plugin/plugin.json</code>、<code>hooks.json</code>、<code>config.toml [mcp_servers]</code> 与 <code>.mcp.json</code>。只读发现，不修改其他客户端的配置。</p>
      <div className="connection-toolbar"><label>作用范围 <select className="selectbox" aria-label="连接作用范围" disabled={busy} value={projectId} onChange={(e) => setProject(e.target.value)}><option value="">全局</option>{projects.map((p) => <option key={p.id} value={p.path}>{p.name}</option>)}</select></label><button type="button" className="btn small" onClick={() => void load()} disabled={busy}>刷新发现</button></div>
      <div className="connection-tabs" role="tablist" aria-label="连接类型">{KINDS.map(([id, label]) => <button type="button" role="tab" aria-selected={id === kind} key={id} onClick={() => setKind(id)}>{label}<span className="num">{scan.items.filter((e) => e.kind === id).length}</span></button>)}</div>
      <div className="connection-import"><input className="textin selectable" aria-label="导入配置或插件路径" placeholder="粘贴 SKILL.md、配置文件或本地插件目录的路径" value={path} onChange={(e) => setPath(e.target.value)} /><button type="button" className="btn" onClick={() => void openPreview(path)} disabled={busy || !path.trim()}>预览导入…</button></div>
      {error && <p className="connection-error" role="alert">{error}</p>}
      {result && <p className="connection-result" role="status">{result}</p>}
      {scan.errors.map((e) => <p className="connection-error" key={e.source}>{e.source}：{e.text}</p>)}
      {preview && <section className="connection-preview"><h4>确认本次导入 · {preview.items.length} 项</h4><ul>{preview.items.map((e) => <li key={e.id}>{e.kind} · {e.name}</li>)}</ul>{preview.commands.length > 0 && <><p>下列命令会在之后自动启用，导入不会立即执行它们：</p><pre className="out selectable">{preview.commands.map((c) => `${c.kind} · ${c.name}\n${c.command} ${c.args.map((a) => JSON.stringify(a)).join(" ")}`).join("\n\n")}</pre></>}<label className="connection-consent"><input type="checkbox" checked={confirmed} onChange={(e) => setConfirmed(e.target.checked)} />我确认这些来源与命令，允许本次导入的可执行配置之后自动启用。</label><div className="row-btns"><button type="button" className="btn primary" disabled={busy || !confirmed} onClick={() => void commit()}>确认导入并启用</button><button type="button" className="btn" onClick={() => setPreview(null)}>取消</button></div></section>}
      {editing && <section className="connection-editor"><h4>配置 · {editing.name}</h4><p className="field-hint">保存到 WittyCode 自己的连接配置，不覆盖来源文件。环境值只填写 <code>{"${变量名}"}</code>。</p><textarea className="textin selectable" aria-label="连接 JSON 配置" rows={9} value={draft} onChange={(e) => setDraft(e.target.value)} />{editing.kind !== "skill" && <label className="connection-consent"><input type="checkbox" checked={confirmed} onChange={(e) => setConfirmed(e.target.checked)} />我确认以上命令和配置，保存后允许启用。</label>}<div className="row-btns"><button type="button" className="btn primary" disabled={busy || (editing.kind !== "skill" && !confirmed)} onClick={() => { try { const value: unknown = JSON.parse(draft); if (!value || Array.isArray(value) || typeof value !== "object") throw new Error("配置必须是 JSON 对象。"); void update(editing, true, value as Record<string, unknown>); } catch (e) { setError(messageOf(e)); } }}>保存并启用</button><button type="button" className="btn" onClick={() => setEditing(null)}>取消</button></div></section>}
      <div className="connection-list">{entries.length === 0 && <p className="snote">没有发现这类连接。可以粘贴本地配置路径后预览导入。</p>}{entries.map((entry) => <section key={entry.id} className="connection-row" data-kind={entry.kind} data-connection={entry.id}><div className="connection-info"><b>{entry.name}</b><span className="connection-state">{entry.changed ? "来源已变化 · 需要重新导入确认" : entry.imported ? entry.enabled ? "已导入 · 已启用" : "已导入 · 已停用" : entry.kind === "skill" ? "已发现" : "已发现 · 尚未信任"}</span><div className="connection-source selectable">{entry.source}</div></div><div className="connection-actions">{!entry.imported && entry.kind !== "skill" && <button type="button" className="btn small" disabled={busy} onClick={() => void openPreview(entry.source)}>导入…</button>}<button type="button" className="btn small" disabled={busy} onClick={() => { if (entry.kind === "plugin") { void openPreview(entry.source); return; } setEditing(entry); setDraft(JSON.stringify(entry.config, null, 2)); setConfirmed(false); setPreview(null); }}>编辑</button>{entry.kind === "mcp" && <button type="button" className="btn small" disabled={busy || !entry.enabled || !entry.imported || entry.changed} onClick={() => void test(entry)}>测试连接</button>}<Switch on={entry.enabled && !entry.changed} label={`${entry.enabled ? "停用" : "启用"} ${entry.name}`} disabled={busy} onChange={(enabled) => void update(entry, enabled)} /></div></section>)}</div>
      <p className="page-note">Hooks 当前作用于 WittyHarness；MCP 可用于 WittyHarness、Devin 与 Factory 的新开／重连会话。本批支持本地 command Hooks 和 stdio MCP；插件商店、远程 MCP 与 OAuth 后续接入。不支持的格式会明确报错，不会悄悄忽略后声称已兼容。</p>
    </div>
  );
}
