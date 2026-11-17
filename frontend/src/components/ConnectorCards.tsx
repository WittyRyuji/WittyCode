import { useCallback, useEffect, useRef, useState } from "react";
import { LIVE } from "../mode";
import { liveCall } from "../liveBridge";
import { messageOf } from "../lib/errors";
import { useStore } from "../store";
import type { ConnectorView } from "../protocol";
import { Switch } from "./formkit";

const DEMO: ConnectorView[] = [
  { id: "github", name: "GitHub", clientId: "", clientSecretEnv: "", port: 8766, enabled: false, connected: false, signingIn: false, available: true, userCode: "", scopes: "repo read:user", helpUrl: "https://docs.github.com/en/apps/oauth-apps/building-oauth-apps/authorizing-oauth-apps", callbackUrl: "http://127.0.0.1:8766/oauth/callback" },
  { id: "google-workspace", name: "Google Workspace", clientId: "", clientSecretEnv: "", port: 8767, enabled: false, connected: false, signingIn: false, available: true, userCode: "", scopes: "Drive、Gmail、Calendar 只读", helpUrl: "https://developers.google.com/identity/protocols/oauth2/native-app", callbackUrl: "http://127.0.0.1:8767/" },
];

function ConnectorCard({ view, update, refresh }: { view: ConnectorView; update: (rows: ConnectorView[]) => void; refresh: () => Promise<void> }) {
  const [action, setAction] = useState("");
  const [error, setError] = useState("");
  const waiting = action === "login" || view.signingIn;
  const busy = !!action || waiting;
  useEffect(() => {
    if (!waiting || !LIVE) return;
    const timer = window.setInterval(() => void refresh(), 500);
    return () => window.clearInterval(timer);
  }, [waiting, refresh]);
  const configure = async (enabled: boolean) => {
    setAction("save"); setError("");
    try {
      update(LIVE ? await liveCall("connectors.configure", { provider: view.id, config: { enabled } }) : DEMO.map((r) => r.id === view.id ? Object.assign(r, { enabled }) : r));
    } catch (e) { setError(messageOf(e)); }
    finally { setAction(""); }
  };
  const act = async (name: "login" | "cancel" | "disconnect") => {
    setAction(name); setError("");
    try {
      const result = LIVE ? await liveCall(`connectors.${name}`, { provider: view.id }) : DEMO.map((r) => r.id === view.id ? Object.assign(r, { connected: name === "login", enabled: name === "login" }) : r);
      update(result);
    } catch (e) { setError(messageOf(e)); }
    finally { setAction(""); }
  };
  const disconnect = () => useStore.getState().openDialog({ title: `断开 ${view.name}？`, body: "删除本地凭据，不撤销服务方授权。可在账号的应用授权页撤销。", confirmLabel: "断开连接", danger: true, onConfirm: () => void act("disconnect") });
  return <section className="connector-card" data-connector={view.id}>
    <header><div><b>{view.name}</b><span className="connection-state">{waiting ? "等待浏览器授权…" : view.connected ? view.enabled ? "已连接 · 已启用" : "已连接 · 已停用" : "未连接"}</span></div><Switch on={view.connected && view.enabled} label={`启用 ${view.name}`} disabled={busy || !view.connected} onChange={(enabled) => void configure(enabled)} /></header>
    <p className="page-note">{view.id === "github" ? "搜索代码与 Issue、读取仓库文件。GitHub 的 repo 授权包含写权限；这里的工具只读。" : "搜索和读取 Drive 文档、邮件、日历，只申请只读授权。"}</p>
    {view.userCode && <p className="page-note" role="status">请在 <a href="https://github.com/login/device" target="_blank" rel="noreferrer">GitHub 授权页</a>输入验证码：<strong className="selectable">{view.userCode}</strong></p>}
    {!view.available && !waiting && <p className="page-note">暂未开放：WittyCode 完成服务方应用注册后即可连接。</p>}
    {error && <p className="connection-error" role="alert">{error}</p>}
    <div className="row-btns">{waiting ? <button type="button" className="btn" disabled={action === "cancel"} onClick={() => void act("cancel")}>取消授权</button> : <><button type="button" className="btn primary" disabled={busy || !view.available} onClick={() => void act("login")}>{view.connected ? "重新授权" : "浏览器授权并连接"}</button>{view.connected && <button type="button" className="btn" disabled={busy} onClick={disconnect}>断开连接</button>}</>}</div>
  </section>;
}

export function ConnectorCards() {
  const [rows, setRows] = useState<ConnectorView[]>([]);
  const [error, setError] = useState("");
  const revision = useRef(0);
  const loading = useRef(false);
  const update = useCallback((next: ConnectorView[]) => { revision.current++; setRows(next); setError(""); }, []);
  const load = useCallback(async () => {
    if (loading.current) return;
    loading.current = true;
    const before = revision.current;
    try {
      const next = LIVE ? await liveCall("connectors.list") : DEMO.map((r) => ({ ...r }));
      if (revision.current === before) { setRows(next); setError(""); }
    } catch (e) { if (revision.current === before) setError(messageOf(e)); }
    finally { loading.current = false; }
  }, []);
  useEffect(() => { void load(); }, [load]);
  return <div className="connector-cards">{error && <p role="alert" className="connection-error">{error}<button type="button" className="btn small" onClick={() => void load()}>重试</button></p>}{rows.map((row) => <ConnectorCard key={row.id} view={row} update={update} refresh={load} />)}<p className="page-note">账号凭据由 Windows 加密保存；启用后，新开或重连会话即可使用。</p></div>;
}
