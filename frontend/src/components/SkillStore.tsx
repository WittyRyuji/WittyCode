import { useEffect, useRef, useState } from "react";
import { LIVE } from "../mode";
import { liveCall } from "../liveBridge";
import { messageOf } from "../lib/errors";
import type { MarketSkill, SkillInstallPreview, SkillSource } from "../protocol";

export function SkillStore({ onInstalled }: { onInstalled: () => void }) {
  const [source, setSource] = useState<SkillSource>("openai");
  const [items, setItems] = useState<MarketSkill[]>([]);
  const [query, setQuery] = useState("");
  const [preview, setPreview] = useState<SkillInstallPreview | null>(null);
  const [confirmed, setConfirmed] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [note, setNote] = useState("");
  const pending = useRef<string | null>(null);
  const alive = useRef(true);
  const load = async (refresh = false) => {
    setBusy(true); setError("");
    try {
      const result = LIVE ? await liveCall("marketplace.list", { source, refresh }) : { items: ["pdf", "playwright", "screenshot"].map((name) => ({ name, source, commit: "0".repeat(40), url: "https://github.com/" + (source === "openai" ? "openai/skills" : "anthropics/skills"), installed: false })), warning: "演示目录；桌面版从官方仓库读取。" };
      setItems(result.items); setNote(result.warning);
    } catch (e) { setError(messageOf(e)); }
    finally { setBusy(false); }
  };
  useEffect(() => { void load(); }, [source]);
  useEffect(() => { alive.current = true; return () => { alive.current = false; if (LIVE && pending.current) void liveCall("marketplace.cancel", { preview: pending.current }).catch(() => undefined); }; }, []);
  const inspect = async (item: MarketSkill) => {
    setBusy(true); setError(""); setNote(""); setConfirmed(false);
    try {
      const result = LIVE ? await liveCall("marketplace.preview", { source, name: item.name }) : { ...item, preview: "demo", description: "官方公开技能。", license: "请查看随附许可。", licenseText: "", files: 3 };
      if (!alive.current) { if (LIVE) await liveCall("marketplace.cancel", { preview: result.preview }); return; }
      pending.current = result.preview; setPreview(result);
    } catch (e) { setError(messageOf(e)); }
    finally { setBusy(false); }
  };
  const cancel = async () => {
    if (!preview) return;
    setBusy(true); setError("");
    try { if (LIVE) await liveCall("marketplace.cancel", { preview: preview.preview }); pending.current = null; setPreview(null); }
    catch (e) { setError(messageOf(e)); }
    finally { setBusy(false); }
  };
  const install = async () => {
    if (!preview || !confirmed) return;
    setBusy(true); setError(""); pending.current = null;
    try {
      if (LIVE) await liveCall("marketplace.install", { preview: preview.preview, confirmed: true });
      setItems((rows) => rows.map((r) => r.name === preview.name ? { ...r, installed: true } : r));
      setNote(`已安装 ${preview.name}，可在会话中用 $${preview.name} 调用。`);
      setPreview(null); onInstalled();
    } catch (e) { pending.current = preview.preview; setError(messageOf(e)); }
    finally { setBusy(false); }
  };
  return <div className="skill-store">
    <p className="page-note">安装两家官方公开仓库的 Skills，不接入账号内的专属商店。不会自动运行安装脚本。</p>
    <div className="connection-toolbar"><div className="segc">{(["openai", "anthropic"] as const).map((id) => <button type="button" key={id} className={source === id ? "on" : ""} disabled={busy || !!preview} onClick={() => { setSource(id); setItems([]); setQuery(""); }}>{id === "openai" ? "OpenAI" : "Anthropic"}</button>)}</div><button type="button" className="btn small" disabled={busy || !!preview} onClick={() => void load(true)}>刷新目录</button></div>
    {error && <p role="alert" className="connection-error">{error}</p>}{note && <p role="status" className="connection-result">{note}</p>}
    {busy && <p className="snote" role="status">正在处理…</p>}
    {preview ? <section className="connection-preview"><h4>安装 {preview.name}</h4><p>{preview.description}</p><a href={preview.url} target="_blank" rel="noreferrer">查看官方来源</a><p className="field-hint">固定版本 {preview.commit.slice(0, 12)} · {preview.files} 个文件</p><p>{preview.license}</p>{preview.licenseText && <details><summary>许可内容</summary><pre className="skill-license selectable">{preview.licenseText}</pre></details>}<label className="connection-consent"><input type="checkbox" checked={confirmed} onChange={(e) => setConfirmed(e.target.checked)} />我确认来源与许可，了解技能在使用时可能执行其脚本。</label><div className="row-btns"><button type="button" className="btn primary" disabled={busy || !confirmed} onClick={() => void install()}>确认安装</button><button type="button" className="btn" disabled={busy} onClick={() => void cancel()}>取消</button></div></section> : <><input className="textin selectable skill-search" aria-label="搜索商店技能" placeholder="搜索技能名称…" value={query} onChange={(e) => setQuery(e.target.value)} /><div className="skill-catalog">{items.filter((i) => i.name.toLowerCase().includes(query.toLowerCase())).map((item) => <section className="skill-card" key={item.name}><b>{item.name}</b><button type="button" className="btn small" disabled={busy || item.installed} onClick={() => void inspect(item)}>{item.installed ? "已安装" : "安装…"}</button></section>)}</div>{!busy && !items.length && <p className="snote">目录暂不可用，请刷新重试。</p>}</>}
  </div>;
}
