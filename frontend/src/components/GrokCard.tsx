import { useEffect, useState } from "react";
import { bridge } from "../bridge-instance";
import { messageOf } from "../lib/errors";
import { GROK_SUBSCRIPTION_SOURCE } from "../lib/models";
import type { GrokAccount } from "../protocol";
import { useStore } from "../store";
import { ModelVisibility } from "./ModelVisibility";

export function GrokCard() {
  const [account, setAccount] = useState<GrokAccount | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const hasModels = useStore((s) => s.models.some((m) => m.source === GROK_SUBSCRIPTION_SOURCE));
  const auth = account?.auth;

  useEffect(() => {
    if (busy) return;
    let active = true;
    let timer: ReturnType<typeof setTimeout> | undefined;
    async function read() {
      try {
        const next = await bridge.grok.account();
        if (!active) return;
        setAccount(next);
        if (next?.auth === "signing-in") timer = setTimeout(read, 750);
      } catch (e) {
        if (active) setError(messageOf(e));
      }
    }
    void read();
    return () => { active = false; clearTimeout(timer); };
  }, [auth, busy, hasModels]);

  const run = async (method: "login" | "reopen" | "cancelLogin" | "logout") => {
    setError("");
    setBusy(true);
    try {
      setAccount(await bridge.grok[method]());
    } catch (e) {
      setError(messageOf(e));
    } finally {
      setBusy(false);
    }
  };

  if (!account) return null;
  const waiting = auth === "signing-in";
  const signedIn = auth === "signed-in";
  return (
    <section className="pcard grok-card">
      <header className="pcard-head">
        <div>
          <h4>Grok 订阅（实验性）</h4>
          <div className={`pstate ${signedIn ? "ok" : waiting ? "busy" : account.error ? "warn" : "off"}`}>
            <span className="pdot" />{signedIn ? "已登录" : waiting ? "等待在浏览器中授权…" : "未登录"}
          </div>
        </div>
        <div className="pcard-actions">
          {waiting ? <>
            {account.canOpen && <button type="button" className="btn small" disabled={busy} onClick={() => void run("reopen")}>打开登录页</button>}
            <button type="button" className="btn small" disabled={busy} onClick={() => void run("cancelLogin")}>取消</button>
          </> : signedIn
            ? <button type="button" className="btn small" disabled={busy} onClick={() => void run("logout")}>退出登录</button>
            : <button type="button" className="btn primary small" disabled={busy} onClick={() => void run("login")}>登录 Grok 订阅</button>}
        </div>
      </header>
      {account.code && <p className="pcard-note">设备代码：<code className="selectable">{account.code}</code>。如果登录页要求输入代码，请使用它。</p>}
      {(error || account.error) && <p className="pcard-note" role="alert">{error || account.error}</p>}
      <p className="pcard-note">使用 Grok 订阅额度，由 WittyCode Harness 运行，不需要安装 CLI，也不会自动改用 API Key。授权页可能显示 Grok Build；可用模型取决于 xAI 的订阅权限，接口可能变动。</p>
      {signedIn && <ModelVisibility source={GROK_SUBSCRIPTION_SOURCE} />}
    </section>
  );
}
