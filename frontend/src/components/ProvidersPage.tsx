import { useEffect, useMemo, useState, type ReactNode } from "react";
import { ExternalLink, KeyRound, Loader2, Plus } from "lucide-react";
import { useStore } from "../store";
import { bridge } from "../bridge-instance";
import { KIND_INFO, PRESETS } from "../lib/vocab";
import { envNameOf, envVarText } from "../lib/models";
import { messageOf } from "../lib/errors";
import { CHATGPT_USAGE_URL, chatgptState } from "../lib/chatgpt";
import type { ApiProvider, DevinAccount, EnvState } from "../types";
import { Switch } from "./formkit";
import { ModelVisibility } from "./ModelVisibility";

function Card({ title, state, tone, children, actions }: { title: string; state: string; tone: "ok" | "warn" | "off" | "busy"; children?: ReactNode; actions?: ReactNode }) {
  return (
    <section className="pcard">
      <header className="pcard-head">
        <div>
          <h4>{title}</h4>
          <div className={`pstate ${tone}`}>
            <span className="pdot" />
            {state}
          </div>
        </div>
        {actions && <div className="pcard-actions">{actions}</div>}
      </header>
      {children}
    </section>
  );
}

function devinState(d: DevinAccount): { text: string; tone: "ok" | "warn" | "off" | "busy" } {
  if (d.auth === "checking") return { text: "正在检测…", tone: "busy" };
  if (d.cli === "missing") return { text: "没有找到 devin 命令行", tone: "warn" };
  if (d.auth === "signing-in") return { text: "正在等你在浏览器里完成登录…", tone: "busy" };
  if (d.auth === "signed-in") return { text: ["已登录", d.email, d.plan].filter(Boolean).join(" · "), tone: "ok" };
  return { text: d.error ? `读不到登录状态：${d.error}` : "未登录", tone: d.error ? "warn" : "off" };
}

function DevinCard() {
  const { devin: d, devinLogin, devinCancel, devinLogout, devinEnable, recheckDevin } = useStore();
  const st = devinState(d);
  return (
    <Card
      title="Devin 账号"
      state={st.text}
      tone={st.tone}
      actions={
        // Still asking Devin whether it is signed in: nothing to offer yet.
        d.auth === "checking" ? null : d.cli === "missing" ? (
          <button type="button" className="btn small" onClick={() => void recheckDevin()}>
            重新检测
          </button>
        ) : d.auth === "signed-out" ? (
          <button type="button" className="btn primary small" onClick={devinLogin}>
            登录 Devin
          </button>
        ) : d.auth === "signing-in" ? (
          <button type="button" className="btn small" onClick={devinCancel}>
            取消
          </button>
        ) : (
          <button type="button" className="btn small" onClick={devinLogout}>
            退出登录
          </button>
        )
      }
    >
      {d.cli === "missing" && (
        <p className="pcard-note">
          Devin 的模型由 Devin 命令行运行（<code>devin acp</code>）。安装 Devin 桌面版或 Devin CLI 后回来登录。也可以在「外部程序」页里指定它的路径。
        </p>
      )}
      {d.cli === "found" && d.auth === "signed-out" && (
        <p className="pcard-note">用 Devin 账号里的额度，不需要 API Key。点登录会打开浏览器；登录信息由 Devin 自己保存（<code>credentials.toml</code>），WittyCode 读不到令牌，Devin 桌面版和命令行共用这个登录。</p>
      )}
      {d.auth === "signing-in" && (
        <p className="pcard-note busy">
          <Loader2 size={14} className="spin" /> 浏览器已经打开。登录完成后这里会自动更新。
        </p>
      )}
      {d.auth === "signed-in" && (
        <div className="pcard-row">
          <div>
            <div className="field-label">在模型菜单里显示 Devin 模型</div>
            <div className="field-hint">关掉后只是隐藏，不会退出登录。</div>
          </div>
          <Switch on={d.enabled} onChange={devinEnable} label="显示 Devin 模型" />
        </div>
      )}
      <ModelVisibility source="devin" />
      {d.cli === "found" && (d.version || d.cliPath) && (
        <div className="pcard-meta">
          {d.version && `devin ${d.version}`}
          {d.version && d.cliPath && " · "}
          {d.cliPath && <span className="selectable">{d.cliPath}</span>}
        </div>
      )}
      <p className="pcard-note faint">Devin 有自己的会话存储（Devin 桌面版和命令行里的会话这里也能打开）。在同一个对话里换到别的引擎时，WittyCode 把之前的文字对话交给它（只带文字，不带工具调用和结果）。侧栏里 Devin 的会话会标「Devin」。</p>
    </Card>
  );
}

function ChatGPTCard() {
  const { chatgpt: a, chatgptLogin, chatgptCancel, chatgptLogout, chatgptUse, chatgptEnable } = useStore();
  if (!a.available) return null;
  const st = chatgptState(a);
  const signedIn = a.auth === "signed-in";
  return (
    <Card
      title="ChatGPT Plan"
      state={st.text}
      tone={st.tone}
      actions={
        a.auth === "signing-in" ? (
          <button type="button" className="btn small" onClick={chatgptCancel}>
            取消
          </button>
        ) : signedIn && a.planEnabled ? (
          <button type="button" className="btn small" onClick={() => void chatgptLogout()}>
            退出登录
          </button>
        ) : signedIn ? (
          <button type="button" className="btn primary small" onClick={() => void chatgptLogin({ forceConsent: true })}>
            允许使用 ChatGPT 套餐
          </button>
        ) : (
          <button type="button" className="btn chatgpt-btn small" onClick={() => void chatgptLogin()}>
            Continue with ChatGPT
          </button>
        )
      }
    >
      {!signedIn && a.auth !== "signing-in" && (
        <p className="pcard-note">
          用 ChatGPT 套餐（Plus、Pro）里的额度，由 WittyCode 自己的代理循环运行，不需要 API Key，也不需要装别的程序。点 Continue with ChatGPT 会打开浏览器，按 OpenAI 的「Sign in with ChatGPT」授权；令牌只保存在 WittyCode 自己的文件夹里。
        </p>
      )}
      {a.auth === "signing-in" && (
        <p className="pcard-note busy">
          <Loader2 size={14} className="spin" /> 浏览器已经打开。登录并同意之后这里会自动更新。
        </p>
      )}
      {signedIn && !a.planEnabled && (
        <p className="pcard-note">登录成功了，但授权时没有同意让 WittyCode 使用你的套餐，所以还不能用它的模型。点右上角重新授权，在浏览器里同意即可。</p>
      )}
      {signedIn && a.planEnabled && (
        <>
          <div className="pcard-row">
            <div>
              <div className="field-label">在模型菜单里显示 ChatGPT Plan 的模型</div>
              <div className="field-hint">关掉后只是隐藏，不会退出登录。</div>
            </div>
            <Switch on={a.enabled} onChange={chatgptEnable} label="显示 ChatGPT Plan 的模型" />
          </div>
          <p className="pcard-note">
            用量算在你套餐已含的额度里（Plus 的 5 小时额度由所有用套餐的应用共用）。可以在 ChatGPT 里给 WittyCode 单独设上限：
            <a className="inline-link" href={CHATGPT_USAGE_URL}>
              Manage usage
            </a>
            。
          </p>
        </>
      )}
      {a.accounts.length > 1 && (
        <div className="chatgpt-accounts">
          {a.accounts.map((x) => (
            <div key={x.clientId} className="chatgpt-account">
              <span className="selectable">{x.label}</span>
              <span className="faint">{x.active ? "正在使用" : x.signedIn ? "已登录" : "已退出"}</span>
              {!x.active && x.signedIn && (
                <button type="button" className="btn small" onClick={() => void chatgptUse(x.clientId)}>
                  使用这个
                </button>
              )}
            </div>
          ))}
        </div>
      )}
      {signedIn && (
        <div className="pcard-row">
          <div className="field-hint">想换一个 ChatGPT 账号：先添加，再在上面选用。</div>
          <button type="button" className="btn small" onClick={() => void chatgptLogin({ add: true })}>
            添加另一个账号
          </button>
        </div>
      )}
      <ModelVisibility source="chatgpt" />
      <p className="pcard-note faint">
        这个引擎不经过别的程序：WittyCode 直接请求 OpenAI，自己运行读写文件、搜索和命令（权限档：询问 / 写代码 / 完全访问，没有操作系统沙箱）。会话保存在 WittyCode 自己的文件夹里；在同一个对话里换到别的引擎时，只交接文字。侧栏里它的会话会标「ChatGPT Plan」。
      </p>
    </Card>
  );
}

const ENV_TEXT: Record<EnvState, string> = { set: "已设置", pending: "重启后生效", unset: "没有设置" };

function ProviderRow({ p, env }: { p: ApiProvider; env?: EnvState }) {
  const { setProviderEdit, testProvider } = useStore.getState();
  const name = p.keyMode === "env" ? envNameOf(p.keyHint) : null;
  const keyText =
    p.keyMode === "env"
      ? `环境变量 ${envVarText(p.keyHint)}${name && env ? `（${ENV_TEXT[env]}）` : ""}`
      : p.keyMode === "inline"
        ? "密钥明文在 settings.json 里，建议改用环境变量"
        : p.keyMode === "command"
          ? "命令获取密钥"
          : p.kind === "bedrock-converse"
            ? "AWS 凭据"
            : "不需要密钥";
  const keyBad = p.keyMode === "inline" || (p.keyMode === "env" && env === "unset");
  const busy = p.status?.text === "正在测试…";
  return (
    <div className="prov">
      <div className="prov-main">
        <div className="prov-top">
          <span className="prov-name">{p.name}</span>
          <span className="prov-kind">{KIND_INFO[p.kind].label}</span>
        </div>
        <div className="prov-url selectable">{p.kind === "bedrock-converse" ? `AWS ${p.bedrock?.region ?? ""}` : p.baseUrl}</div>
        <div className="prov-meta">
          <KeyRound size={12} /> <span className={keyBad ? "warn-text" : ""}>{keyText}</span> · {p.models.length} 个模型：{p.models.map((m) => m.displayName || m.id).join("、")}
        </div>
        {p.status && (
          <div className={`prov-status ${busy ? "busy" : p.status.ok ? "ok" : "bad"}`}>
            {busy ? <Loader2 size={12} className="spin" /> : <span className="pdot" />} {p.status.text}
          </div>
        )}
        <ModelVisibility source={`api:${p.id}`} />
      </div>
      <div className="prov-actions">
        <button type="button" className="btn small" disabled={busy} onClick={() => testProvider(p.id)}>
          测试
        </button>
        <button type="button" className="btn small" onClick={() => setProviderEdit(p.id)}>
          编辑
        </button>
      </div>
    </div>
  );
}

export function ProvidersPage() {
  const providers = useStore((s) => s.providers);
  const setProviderEdit = useStore((s) => s.setProviderEdit);
  const say = useStore((s) => s.say);
  const [envs, setEnvs] = useState<Record<string, EnvState>>({});
  const names = useMemo(() => providers.map((p) => (p.keyMode === "env" ? envNameOf(p.keyHint) : null)).filter((n): n is string => !!n), [providers]);
  useEffect(() => {
    let live = true;
    bridge.providers
      .envStatus(names)
      .then((r) => live && setEnvs(r))
      .catch((error) => say(messageOf(error)));
    return () => {
      live = false;
    };
  }, [names, say]);
  const quick = PRESETS.filter((p) => ["openai", "anthropic", "openrouter", "ollama"].includes(p.id));
  return (
    <div className="providers">
      <ChatGPTCard />
      <DevinCard />
      <section className="pcard">
        <header className="pcard-head">
          <div>
            <h4>API Provider</h4>
            <div className="pstate off">{providers.length ? `${providers.length} 个 Provider · ${providers.reduce((n, p) => n + p.models.length, 0)} 个模型` : "还没有"}</div>
          </div>
          <div className="pcard-actions">
            <button type="button" className="btn primary small" onClick={() => setProviderEdit("new")}>
              <Plus size={13} /> 添加 Provider
            </button>
          </div>
        </header>
        <p className="pcard-note">用你自己的 API Key，或本机、公司网关上的模型。<b>WittyCode 自己的代理循环直接请求你的接口</b>。配置保存在 WittyCode 自己的 <code>models.json</code> 里（<code>customModels</code> 格式）。<b>密钥只放在环境变量里</b>，不会写进配置文件；设置之后马上能用，不用重启。</p>
        {providers.length === 0 ? (
          <div className="prov-empty">
            <p>常用的可以直接开始：</p>
            <div className="quick">
              {quick.map((p) => (
                <button key={p.id} type="button" className="chip-suggest" onClick={() => setProviderEdit(`new:${p.id}`)}>
                  {p.name}
                </button>
              ))}
            </div>
          </div>
        ) : (
          <div className="provs">
            {providers.map((p) => (
              <ProviderRow key={p.id} p={p} env={p.keyMode === "env" ? envs[envNameOf(p.keyHint) ?? ""] : undefined} />
            ))}
          </div>
        )}
        <div className="pcard-meta">
          <ExternalLink size={12} /> 支持 Anthropic、OpenAI、OpenRouter、Fireworks、Groq、Gemini 兼容接口、Ollama、LM Studio、AWS Bedrock，以及任何 OpenAI 兼容的服务。
        </div>
      </section>
    </div>
  );
}
