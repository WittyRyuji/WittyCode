import { useState } from "react";
import { LIVE } from "../mode";
import { liveCall } from "../liveBridge";
import { messageOf } from "../lib/errors";
import { Field } from "./formkit";

const STDIO = '[mcp_servers.demo]\ncommand = "npx"\nargs = ["-y", "your-mcp-package"]\n# env_vars = ["YOUR_SERVICE_TOKEN"]';
const HTTP = '[mcp_servers.demo]\nurl = "https://mcp.example.com/mcp"\n# bearer_token_env_var = "YOUR_SERVICE_TOKEN"\n# [mcp_servers.demo.env_http_headers]\n# "X-Service-Key" = "YOUR_SERVICE_KEY"';

export function AddMcp({ projectId, onDone, onCancel }: { projectId: string; onDone: () => void; onCancel: () => void }) {
  const [name, setName] = useState("");
  const [text, setText] = useState(STDIO);
  const [confirmed, setConfirmed] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const save = async () => {
    setBusy(true); setError("");
    try {
      if (LIVE) await liveCall("connections.addMcp", { projectId, name: name.trim(), configText: text, confirmed });
      onDone();
    } catch (e) { setError(messageOf(e)); }
    finally { setBusy(false); }
  };
  return <section className="connection-editor">
    <h4>添加自定义 MCP</h4>
    <p className="field-hint">粘贴 Codex 风格 TOML 或 JSON；每次添加一个服务器。支持 stdio 和 Streamable HTTP。</p>
    <Field label="名称" hint="完整配置已含名称时可留空。"><input className="textin selectable" aria-label="新 MCP 名称" placeholder="demo" value={name} onChange={(e) => setName(e.target.value)} /></Field>
    <div className="row-btns"><button type="button" className="btn small" disabled={busy} onClick={() => { setText(STDIO); setConfirmed(false); }}>stdio 示例</button><button type="button" className="btn small" disabled={busy} onClick={() => { setText(HTTP); setConfirmed(false); }}>HTTP 示例</button></div>
    <textarea className="rules-editor selectable" aria-label="MCP JSON 或 TOML 配置" value={text} disabled={busy} onChange={(e) => { setText(e.target.value); setConfirmed(false); }} spellCheck={false} />
    <p className="field-hint">凭据只用环境变量引用；HTTP 可使用 Bearer 和环境变量请求头。不支持的配置项会报错。</p>
    <label className="connection-consent"><input type="checkbox" checked={confirmed} onChange={(e) => setConfirmed(e.target.checked)} />我信任此命令或服务，允许之后启动并连接。</label>
    {error && <p className="connection-error" role="alert">{error}</p>}
    <div className="row-btns"><button type="button" className="btn primary" disabled={busy || !confirmed || !text.trim()} onClick={() => void save()}>添加并启用</button><button type="button" className="btn" disabled={busy} onClick={onCancel}>取消</button></div>
  </section>;
}
