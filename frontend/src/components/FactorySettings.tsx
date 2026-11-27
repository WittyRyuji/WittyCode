import { useEffect, useState } from "react";
import { LIVE } from "../mode";
import { liveCall } from "../liveBridge";
import { messageOf } from "../lib/errors";
import { useStore } from "../store";
import type { FactoryState } from "../protocol";
import { Switch } from "./formkit";
import { ModelVisibility } from "./ModelVisibility";

const DEMO: FactoryState = { cli: "missing", cliPath: "", path: "", enabled: false };

/** Factory owns the sign-in; this optional adapter does not read credentials or import Factory's settings. */
export function FactorySettings() {
  const [state, setState] = useState<FactoryState>(DEMO);
  const [busy, setBusy] = useState(false);
  const say = useStore((s) => s.say);
  const load = async (refresh = false) => {
    setBusy(true);
    try {
      const value = LIVE ? await liveCall("factory.account", refresh ? { refresh: true } : {}) : DEMO;
      setState(value);
      if (refresh && value.enabled && value.cli === "found") say("已检测到 Factory CLI，正在刷新模型。");
    } catch (e) { say(messageOf(e)); }
    finally { setBusy(false); }
  };
  useEffect(() => { void load(); }, []);
  const save = async (enabled: boolean) => {
    setBusy(true);
    try {
      const value = LIVE ? await liveCall("factory.configure", { enabled }) : { ...state, enabled };
      setState(value);
      say(!enabled ? "已停用 Factory 模型；已打开的会话不被中断。" : value.cli === "found" ? "已启用 Factory CLI，正在刷新模型。" : "已启用 Factory，但没有找到 droid。");
    } catch (e) { say(messageOf(e)); }
    finally { setBusy(false); }
  };
  const found = state.cli === "found";
  const status = found ? "已检测到 CLI · 使用 Factory 自己的登录" : state.path ? "已保存的路径不可用 · 请在「外部程序」中切换为自动检测" : "尚未检测到 droid · 不影响 WittyHarness 使用";
  return (
    <section className="pcard factory-card">
      <header className="pcard-head"><div><h4>Factory CLI（可选）</h4><div className={`pstate ${found ? "ok" : "off"}`}>{busy ? "正在检测…" : status}</div></div><button type="button" className="btn small" onClick={() => void load(true)} disabled={busy}>重新检测</button></header>
      <p className="pcard-note">使用你在 Factory CLI 中已登录的账号，通过 <code>stream-jsonrpc</code> 连接，不依赖 Factory SDK。未登录时先在终端运行 <code>droid</code> 完成登录；WittyCode 不读取或复制其令牌和 API 模型配置。</p>
      <p className="pcard-note faint">可以在「外部程序」中查看或指定 droid 的路径。</p>
      <div className="pcard-row"><span>在模型菜单中启用 Factory</span><Switch on={state.enabled} onChange={(on) => void save(on)} label="启用 Factory CLI" disabled={busy} /></div>
      <ModelVisibility source="factory" />
      <p className="pcard-note faint">Factory 会话由外部 CLI 执行，使用它自己的权限策略。WittyCode 只记录从本应用打开的 Factory 会话；引导暂按排队处理，不伪造已采用回显。</p>
    </section>
  );
}
