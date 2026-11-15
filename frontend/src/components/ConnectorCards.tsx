import { useEffect, useState } from "react";
import { LIVE } from "../mode";
import { liveCall } from "../liveBridge";
import { messageOf } from "../lib/errors";
import { useStore } from "../store";
import type { ConnectorConfig, ConnectorView } from "../protocol";
import { Field, Switch } from "./formkit";

const DEMO: ConnectorView[] = [
  { id: "github", name: "GitHub", clientId: "", clientSecretEnv: "", port: 8766, enabled: false, connected: false, signingIn: false, scopes: "repo read:user", helpUrl: "https://docs.github.com/en/apps/oauth-apps/building-oauth-apps/authorizing-oauth-apps", callbackUrl: "http://127.0.0.1:8766/oauth/callback" },
  { id: "google-workspace", name: "Google Workspace", clientId: "", clientSecretEnv: "", port: 8767, enabled: false, connected: false, signingIn: false, scopes: "Drive、Gmail、Calendar 只读", helpUrl: "https://developers.google.com/identity/protocols/oauth2/native-app", callbackUrl: "http://127.0.0.1:8767/" },
];

function ConnectorCard({ view, update }: { view: ConnectorView; update: (rows: ConnectorView[]) => void }) {
  const [config, setConfig] = useState<ConnectorConfig>({ clientId: view.clientId, clientSecretEnv: view.clientSecretEnv, port: view.port, enabled: view.enabled });
  const [action, setAction] = useState("");
  const [error, setError] = useState("");
  useEffect(() => { setConfig({ clientId: view.clientId, clientSecretEnv: view.clientSecretEnv, port: view.port, enabled: view.enabled }); }, [view.clientId, view.clientSecretEnv, view.port, view.enabled]);
  const waiting = action === "login" || view.signingIn;
  const busy = !!action || waiting;
  const dirty = config.clientId !== view.clientId || config.clientSecretEnv !== view.clientSecretEnv || config.port !== view.port;
  const configure = async (draft: Partial<ConnectorConfig>) => {
    setAction("save"); setError("");
    try {
      update(LIVE ? await liveCall("connectors.configure", { provider: view.id, config: draft }) : DEMO.map((r) => r.id === view.id ? Object.assign(r, draft) : r));
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
    <header><div><b>{view.name}</b><span className="connection-state">{waiting ? "等待浏览器授权…" : view.connected ? view.enabled ? "已连接 · 已启用" : "已连接 · 已停用" : "未连接"}</span></div><Switch on={view.enabled} label={`启用 ${view.name}`} disabled={busy || dirty} onChange={(enabled) => void configure({ enabled })} /></header>
    <p className="page-note">{view.id === "github" ? "搜索代码与 Issue、读取仓库文件。GitHub 的 repo 授权包含写权限；这里的工具只读。" : "搜索和读取 Drive 文档、邮件、日历，只申请只读授权。需在 Google Cloud 启用对应 API。"}</p>
    <details><summary>OAuth 客户端配置</summary>
      <Field label="Client ID"><input className="textin selectable" aria-label={`${view.name} Client ID`} value={config.clientId} disabled={busy} onChange={(e) => setConfig({ ...config, clientId: e.target.value.trim() })} /></Field>
      <Field label="客户端密钥环境变量" hint={view.id === "github" ? "必填，只填变量名。" : "桌面客户端若提供密钥，填写其环境变量名。"}><input className="textin selectable" aria-label={`${view.name} 客户端密钥环境变量`} placeholder="OAUTH_CLIENT_SECRET" value={config.clientSecretEnv} disabled={busy} onChange={(e) => setConfig({ ...config, clientSecretEnv: e.target.value.trim() })} /></Field>
      <Field label="本地回调端口"><input type="number" min={1024} max={65535} className="textin" aria-label={`${view.name} 回调端口`} value={config.port} disabled={busy} onChange={(e) => setConfig({ ...config, port: Number(e.target.value) })} /></Field>
      <p className="pathbox selectable">{view.callbackUrl.replace(/:\d+\//, `:${config.port}/`)}</p>
      <p className="page-note">{view.id === "github" ? "创建 OAuth App，并配置上面的回调地址。" : "使用桌面应用 OAuth 客户端；测试状态的应用需添加测试用户。"} <a href={view.helpUrl} target="_blank" rel="noreferrer">官方配置文档</a></p>
      <div className="row-btns"><button type="button" className="btn" disabled={busy || !dirty} onClick={() => void configure(config)}>{view.connected && dirty ? "保存并断开旧连接" : "保存客户端配置"}</button></div>
    </details>
    {error && <p className="connection-error" role="alert">{error}</p>}
    <div className="row-btns">{waiting ? <button type="button" className="btn" disabled={action === "cancel"} onClick={() => void act("cancel")}>取消授权</button> : <><button type="button" className="btn primary" disabled={busy || dirty || !view.clientId} onClick={() => void act("login")}>{view.connected ? "重新授权" : "浏览器授权并连接"}</button>{view.connected && <button type="button" className="btn" disabled={busy} onClick={disconnect}>断开连接</button>}</>}</div>
  </section>;
}

export function ConnectorCards() {
  const [rows, setRows] = useState<ConnectorView[]>([]);
  const [error, setError] = useState("");
  const load = async () => { try { setRows(LIVE ? await liveCall("connectors.list") : DEMO.map((r) => ({ ...r }))); setError(""); } catch (e) { setError(messageOf(e)); } };
  useEffect(() => { void load(); }, []);
  const waiting = rows.some((r) => r.signingIn);
  useEffect(() => { if (!waiting) return; const timer = window.setInterval(() => void load(), 1000); return () => window.clearInterval(timer); }, [waiting]);
  return <div className="connector-cards">{error && <p role="alert" className="connection-error">{error}<button type="button" className="btn small" onClick={() => void load()}>重试</button></p>}{rows.map((row) => <ConnectorCard key={row.id} view={row} update={setRows} />)}<p className="page-note">账号凭据由 Windows 加密保存；密钥使用环境变量。启用后，新开或重连会话即可使用。</p></div>;
}
