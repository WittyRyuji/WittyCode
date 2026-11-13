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
export function FactorySettings({ program = false }: { program?: boolean }) {
  const [state, setState] = useState<FactoryState>(DEMO);
  const [path, setPath] = useState("");
  const [busy, setBusy] = useState(false);
  const say = useStore((s) => s.say);
  const load = () => {
    setBusy(true);
    void (LIVE ? liveCall("factory.account") : Promise.resolve(DEMO)).then((value) => { setState(value); setPath(value.path); }).catch((e) => say(messageOf(e))).finally(() => setBusy(false));
  };
  useEffect(load, []);
  const save = async (enabled: boolean) => {
    setBusy(true);
    try {
      const value = LIVE ? await liveCall("factory.configure", { path: path.trim(), enabled }) : { ...state, path, enabled };
      setState(value);
      say(enabled ? "已启用可选 Factory CLI，正在刷新模型。" : "已停用 Factory 模型；已打开的会话不被中断。");
    } catch (e) { say(messageOf(e)); }
    finally { setBusy(false); }
  };
  return (
    <section className={program ? "factory-program" : "pcard factory-card"}>
      <header className="pcard-head"><div><h4>Factory CLI（可选）</h4><div className={`pstate ${state.cli === "found" ? "ok" : "off"}`}>{state.cli === "found" ? "已检测到 CLI · 使用 Factory 自己的登录" : "尚未检测到 droid · 不影响 WittyHarness 使用"}</div></div><button type="button" className="btn small" onClick={load} disabled={busy}>重新检测</button></header>
      <p className="pcard-note">使用你在 Factory CLI 中已登录的账号，通过 <code>stream-jsonrpc</code> 连接，不依赖 Factory SDK。未登录时先在终端运行 <code>droid</code> 完成登录；WittyCode 不读取或复制其令牌和 API 模型配置。</p>
      <div className="pcard-row"><label htmlFor="factory-path">droid 程序路径（留空则从 PATH 检测）</label></div>
      <div className="row-btns"><input id="factory-path" className="textin selectable" aria-label="Factory CLI 路径" disabled={busy} value={path} onChange={(e) => setPath(e.target.value)} placeholder="自动检测 droid" /><button type="button" className="btn small" disabled={busy} onClick={() => void save(state.enabled)}>保存路径</button></div>
      <div className="pcard-row"><span>在模型菜单中启用 Factory</span><Switch on={state.enabled} onChange={(on) => void save(on)} label="启用 Factory CLI" disabled={busy} /></div>
      <ModelVisibility source="factory" />
      <p className="pcard-note faint">Factory 会话由外部 CLI 执行，使用它自己的权限策略。WittyCode 只记录从本应用打开的 Factory 会话；引导暂按排队处理，不伪造已采用回显。</p>
    </section>
  );
}
