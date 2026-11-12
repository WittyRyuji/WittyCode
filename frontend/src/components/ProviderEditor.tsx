import { useEffect, useMemo, useState } from "react";
import { AlertTriangle, ArrowLeft, Check, ChevronRight, Copy, Loader2, Plus, Trash2, X } from "lucide-react";
import { useStore } from "../store";
import { bridge } from "../bridge-instance";
import { KIND_INFO, PRESETS, REASONING, type Preset } from "../lib/vocab";
import { envHintOf, envNameOf, envVarText } from "../lib/models";
import { API_EFFORTS, contextProblem, imagesAfterKindChange, imagesByDefault, modelSpecification, withRemoteFigures } from "../lib/providerDefaults";
import { messageOf } from "../lib/errors";
import { formatTokens } from "../lib/format";
import type { ApiKind, ApiModel, EnvState, KeyMode, ProviderDraft, RemoteModel } from "../types";
import { Seg, Select, Stack, Switch, TextInput } from "./formkit";
import type { MenuItem } from "./ui";

/** There is no way to type a key in here: it lives in an environment variable, or comes from a command. */
const KEY_MODES: { id: KeyMode; label: string }[] = [
  { id: "env", label: "环境变量" },
  { id: "command", label: "命令获取" },
  { id: "none", label: "不需要" },
];

/** The variable a provider at this address conventionally uses, so switching to "环境变量" starts from a sensible name. */
const suggestedVariable = (baseUrl: string) => {
  const p = PRESETS.find((x) => x.envName && x.baseUrl && x.baseUrl === baseUrl);
  return p?.envName ? `\${${p.envName}}` : "${}";
};

/** What is known about the variable the user named, and what to do when it is not there. */
function EnvLine({ name, state, onRecheck }: { name: string; state: EnvState | null; onRecheck: () => void }) {
  const say = useStore((s) => s.say);
  if (state === null) return null;
  const command = `setx ${name} "你的密钥"`;
  return (
    <div className={`envline ${state}`}>
      <div className="envline-top">
        <span className="pdot" />
        {state === "set" && <span>已设置：WittyCode 读得到 <code>{name}</code>。</span>}
        {state === "pending" && <span>已写进你的用户环境变量，但 WittyCode 是在那之前启动的：<b>重启 WittyCode</b> 后才读得到。</span>}
        {state === "unset" && <span>没有找到环境变量 <code>{name}</code>。在 PowerShell 里运行下面这行（把密钥换成你的），然后重启 WittyCode：</span>}
        <button type="button" className="btn small" onClick={onRecheck}>
          重新检测
        </button>
      </div>
      {state === "unset" && (
        <div className="envline-cmd">
          <code className="selectable">{command}</code>
          <button
            type="button"
            className="icon-btn sm"
            title="复制"
            aria-label="复制命令"
            onClick={() => {
              void navigator.clipboard?.writeText(command);
              say("已复制。粘贴到 PowerShell 里，把「你的密钥」换成真正的密钥。");
            }}
          >
            <Copy size={13} />
          </button>
        </div>
      )}
    </div>
  );
}

const BEDROCK_METHODS = [
  { value: "aws-credentials", label: "AWS 凭据", hint: "环境变量、~/.aws、SSO 或指定 profile" },
  { value: "credentials-command", label: "命令导出凭据", hint: "一条命令输出 Credentials JSON" },
  { value: "environment-api-key", label: "Bedrock API Key", hint: "读环境变量 AWS_BEARER_TOKEN_BEDROCK" },
] as const;

/** Configurable API levels; the provider remains responsible for accepting the selected level. */
const ROW_EFFORTS = REASONING.filter((r) => API_EFFORTS.some((value) => value === r.value));

let rowCounter = 0;
const blankModel = (id: string, kind: ApiKind): ApiModel => ({ id, displayName: "", reasoning: "", images: imagesByDefault(kind) });

/** What the server's list says about a model fills the figures its row leaves empty; what the user typed stays. */
const withFigures = (m: ApiModel, found: Map<string, RemoteModel>): ApiModel => {
  const f = found.get(m.id.trim());
  return withRemoteFigures(m, f);
};

function draftFrom(editId: string, existing: ProviderDraft | undefined): ProviderDraft {
  if (existing) return existing;
  const presetId = editId.startsWith("new:") ? editId.slice(4) : "custom";
  const p: Preset = PRESETS.find((x) => x.id === presetId) ?? PRESETS[PRESETS.length - 1]!;
  return {
    id: `p${Date.now().toString(36)}${++rowCounter}`,
    name: p.id === "custom" ? "" : p.name,
    kind: p.kind,
    baseUrl: p.baseUrl,
    keyMode: p.keyMode,
    keyHint: p.envName ? `\${${p.envName}}` : "",
    bearer: p.bearer || undefined,
    models: p.models
      ? p.models.map((m) => ({ id: m.id, displayName: m.displayName ?? "", contextWindow: m.contextWindow, reasoning: "", images: m.images }))
      : [blankModel(p.example && p.id !== "lmstudio" && p.id !== "custom" ? p.example : "", p.kind)],
    headers: [],
    extraArgs: "",
    bedrock: p.kind === "bedrock-converse" ? { method: "aws-credentials", region: "us-west-2", profile: "" } : undefined,
  };
}

function validate(d: ProviderDraft): Record<string, string> {
  const e: Record<string, string> = {};
  if (!d.name.trim()) e.name = "给它起个名字，模型菜单里按它分组。";
  if (d.kind !== "bedrock-converse") {
    if (!d.baseUrl.trim()) e.baseUrl = "必填。";
    else if (!/^https?:\/\/\S+$/.test(d.baseUrl.trim())) e.baseUrl = "要以 http:// 或 https:// 开头。";
  }
  if (d.keyMode === "env" && !envNameOf(d.keyHint)) e.key = "只写变量名，例如 OPENROUTER_API_KEY（字母、数字、下划线，不以数字开头）。这里填的是变量名，不是密钥本身。";
  if (d.keyMode === "command" && !d.keyHint.trim()) e.key = "填一条会输出密钥的命令。";
  if (d.models.length === 0 || d.models.every((m) => !m.id.trim())) e.models = "至少要有一个模型 ID。";
  else if (new Set(d.models.map((m) => m.id.trim()).filter(Boolean)).size < d.models.filter((m) => m.id.trim()).length) e.models = "有重复的模型 ID。";
  if (!e.models) {
    const problem = d.models.filter((m) => m.id.trim()).map(contextProblem).find(Boolean);
    if (problem) e.models = problem;
  }
  if (d.extraArgs.trim()) {
    try {
      const v = JSON.parse(d.extraArgs);
      if (typeof v !== "object" || v === null || Array.isArray(v)) e.extraArgs = "要是一个 JSON 对象，例如 {\"temperature\": 0.7}。";
    } catch {
      e.extraArgs = "不是合法的 JSON。";
    }
  }
  return e;
}

export function ProviderEditor({ editId }: { editId: string }) {
  const existing = useStore((s) => s.providers.find((p) => p.id === editId));
  const { setProviderEdit, saveProvider, removeProvider, say } = useStore.getState();
  const [d, setD] = useState<ProviderDraft>(() => draftFrom(editId, existing ? { ...existing, status: undefined } : undefined));
  const [envState, setEnvState] = useState<EnvState | null>(null);
  const [recheck, setRecheck] = useState(0);
  const envName = d.keyMode === "env" ? envNameOf(d.keyHint) : null;
  useEffect(() => {
    setEnvState(null);
    if (!envName) return;
    let live = true;
    bridge.providers
      .envStatus([envName])
      .then((r) => live && setEnvState(r[envName] ?? "unset"))
      .catch((error) => say(messageOf(error)));
    return () => {
      live = false;
    };
  }, [envName, recheck, say]);
  const [tried, setTried] = useState(false);
  const [test, setTest] = useState<{ busy: boolean; ok?: boolean; text?: string }>({ busy: false });
  const [suggest, setSuggest] = useState<RemoteModel[] | null>(null);
  const [advanced, setAdvanced] = useState(!!existing && (existing.headers.length > 0 || !!existing.extraArgs));
  const [saving, setSaving] = useState(false);
  const isNew = !existing;
  const errors = useMemo(() => validate(d), [d]);
  const shown = (k: string) => (tried ? errors[k] : undefined);
  const set = <K extends keyof ProviderDraft>(k: K, v: ProviderDraft[K]) => setD((x) => ({ ...x, [k]: v }));
  const setModel = (i: number, patch: Partial<ApiModel>) => setD((x) => ({ ...x, models: x.models.map((m, j) => (j === i ? { ...m, ...patch } : m)) }));

  const presetItems: MenuItem[] = PRESETS.map((p) => ({
    id: p.id,
    label: p.name,
    description: p.local ? "本机服务，不需要密钥" : p.baseUrl.replace(/^https?:\/\//, "") || undefined,
    onSelect: () => setD(draftFrom(`new:${p.id}`, undefined)),
  }));
  const kindItems: MenuItem[] = (Object.keys(KIND_INFO) as ApiKind[]).map((k) => ({
    id: k,
    label: KIND_INFO[k].label,
    description: KIND_INFO[k].hint,
    checked: d.kind === k,
    onSelect: () =>
      setD((x) => ({
        ...x,
        kind: k,
        models: x.models.map((m) => ({ ...m, images: imagesAfterKindChange(m.images, x.kind, k) })),
        bearer: k === "anthropic" ? x.bearer : undefined,
        bedrock: k === "bedrock-converse" ? (x.bedrock ?? { method: "aws-credentials", region: "us-west-2", profile: "" }) : undefined,
        keyMode: k === "bedrock-converse" ? "none" : x.keyMode,
      })),
  }));

  const runTest = async () => {
    setTest({ busy: true });
    try {
      const r = await bridge.providers.test(d);
      setTest({ busy: false, ok: r.ok, text: r.text });
    } catch (error) {
      setTest({ busy: false });
      say(messageOf(error));
    }
  };
  const fetchModels = async () => {
    setSuggest(null);
    setTest({ busy: true });
    try {
      const found = await bridge.providers.listModels(d);
      const byId = new Map(found.map((m) => [m.id, m]));
      const filled = d.models.filter((m) => {
        const next = withFigures(m, byId);
        return next.contextWindow !== m.contextWindow || next.maxOutput !== m.maxOutput;
      }).length;
      if (filled) setD((x) => ({ ...x, models: x.models.map((m) => withFigures(m, byId)) }));
      const serverWindows = found.filter((m) => m.contextWindow && m.contextSource !== "catalog").length;
      const catalogWindows = found.filter((m) => m.contextSource === "catalog").length;
      const text = !found.length
        ? "这个服务没有列出模型：自己填模型 ID 和上下文窗口。"
        : `服务端列出了 ${found.length} 个模型；${serverWindows} 个窗口来自服务端，${catalogWindows} 个由内置规格表补全` +
          "。其余未知模型需填写上下文窗口，不使用统一猜测值" +
          (filled ? `；已补上 ${filled} 行的空白项` : "");
      setTest({ busy: false, ok: found.length > 0, text });
      setSuggest(found);
    } catch (error) {
      setTest({ busy: false });
      say(messageOf(error));
    }
  };
  const save = async () => {
    setTried(true);
    if (Object.keys(errors).length) return;
    setSaving(true);
    const ok = await saveProvider({ ...d, name: d.name.trim(), baseUrl: d.baseUrl.trim(), models: d.models.filter((m) => m.id.trim()).map((m) => ({ ...m, id: m.id.trim() })) });
    setSaving(false);
    if (ok) setProviderEdit(null);
  };

  const isBedrock = d.kind === "bedrock-converse";
  const kindInfo = KIND_INFO[d.kind];

  return (
    <div className="pe">
      <button type="button" className="back" onClick={() => setProviderEdit(null)}>
        <ArrowLeft size={14} /> 账号与模型
      </button>
      <h3 className="pe-title">{isNew ? "添加 API Provider" : `编辑「${existing!.name}」`}</h3>

      <section className="pe-sec">
        <h4>基本</h4>
        {isNew && (
          <Stack label="从预设开始" hint="选一个会填好接口类型和 Base URL；之后都能改。">
            <Select value={PRESETS.find((p) => p.name === d.name)?.name ?? "选择预设…"} items={presetItems} minWidth={300} />
          </Stack>
        )}
        <Stack label="名称" error={shown("name")} hint="模型菜单里按这个名字分组。">
          <TextInput label="名称" value={d.name} onChange={(v) => set("name", v)} placeholder="例如 OpenRouter" invalid={!!shown("name")} />
        </Stack>
        <Stack label="接口类型" hint={kindInfo.hint}>
          <Select value={kindInfo.label} items={kindItems} minWidth={380} />
        </Stack>
        {!isBedrock && (
          <Stack label="Base URL" error={shown("baseUrl")} hint="不带末尾的 /chat/completions。">
            <TextInput label="Base URL" mono value={d.baseUrl} onChange={(v) => set("baseUrl", v)} placeholder="https://api.example.com/v1" invalid={!!shown("baseUrl")} />
          </Stack>
        )}
      </section>

      <section className="pe-sec">
        <h4>{isBedrock ? "AWS 凭据" : "密钥"}</h4>
        {isBedrock ? (
          <>
            <Stack label="凭据来源" hint={BEDROCK_METHODS.find((m) => m.value === d.bedrock?.method)?.hint}>
              <Select
                value={BEDROCK_METHODS.find((m) => m.value === d.bedrock?.method)?.label ?? ""}
                items={BEDROCK_METHODS.map((m) => ({ id: m.value, label: m.label, description: m.hint, checked: d.bedrock?.method === m.value, onSelect: () => set("bedrock", { ...d.bedrock!, method: m.value }) }))}
                minWidth={340}
              />
            </Stack>
            <div className="pe-row">
              <Stack label="区域">
                <TextInput label="AWS 区域" mono value={d.bedrock?.region ?? ""} onChange={(v) => set("bedrock", { ...d.bedrock!, region: v })} placeholder="us-west-2" width={180} />
              </Stack>
              <Stack label="Profile（可选）">
                <TextInput label="AWS profile" mono value={d.bedrock?.profile ?? ""} onChange={(v) => set("bedrock", { ...d.bedrock!, profile: v })} placeholder="my-aws-profile" width={220} />
              </Stack>
            </div>
          </>
        ) : (
          <>
            {d.keyMode === "inline" && (
              <div className="pe-warn" role="note">
                <AlertTriangle size={15} />
                <div>
                  这个 Provider 的密钥现在以<b>明文</b>写在 settings.json 里（<code>{d.keyHint}</code>）。WittyCode 不再往配置里写密钥：选「环境变量」，填变量名，保存后配置里的明文密钥会被删掉。不改的话它照旧能用。
                </div>
              </div>
            )}
            <Seg
              value={d.keyMode}
              options={KEY_MODES}
              onChange={(m) => setD((x) => ({ ...x, keyMode: m, keyHint: m === "env" ? suggestedVariable(x.baseUrl) : m === "command" ? "" : "" }))}
            />
            {d.keyMode === "env" && (
              <>
                <Stack label="环境变量" error={shown("key")} hint="只写变量名，不用加 ${}。密钥放在这个环境变量里，配置文件里只记变量名，不记密钥。">
                  <TextInput label="环境变量" mono value={envVarText(d.keyHint)} onChange={(v) => set("keyHint", envHintOf(v))} placeholder="OPENROUTER_API_KEY" invalid={!!shown("key")} />
                </Stack>
                {envName && <EnvLine name={envName} state={envState} onRecheck={() => setRecheck((n) => n + 1)} />}
              </>
            )}
            {d.keyMode === "command" && (
              <>
                <Stack label="命令" error={shown("key")} hint="命令输出的内容就是密钥，WittyCode 在请求时运行它并缓存。适合发短期令牌的网关。">
                  <TextInput label="命令" mono value={d.keyHint} onChange={(v) => set("keyHint", v)} placeholder="C:\tools\mint-gateway-token.cmd" invalid={!!shown("key")} />
                </Stack>
                <Stack label="刷新间隔（毫秒）" hint="默认 300000（5 分钟）；收到 401 时会立刻再取一次。">
                  <TextInput label="刷新间隔" type="number" mono value={String(d.keyTtlMs ?? "")} onChange={(v) => set("keyTtlMs", v ? Number(v) : undefined)} placeholder="300000" width={160} />
                </Stack>
              </>
            )}
            {d.keyMode === "none" && <p className="pe-note">本机服务（Ollama、LM Studio）或靠别的方式认证的网关。有的服务要求密钥非空：用「环境变量」，随便设一个占位值。</p>}
            {d.kind === "anthropic" && d.keyMode !== "none" && (
              <div className="pe-check">
                <Switch on={!!d.bearer} onChange={(v) => set("bearer", v)} label="用 Bearer 发送密钥" />
                <span>
                  用 <code>Authorization: Bearer</code> 发送密钥 <span className="muted-text">（默认是 x-api-key；有些 Anthropic 兼容网关要 Bearer）</span>
                </span>
              </div>
            )}
          </>
        )}
      </section>

      <section className="pe-sec">
        <h4>模型</h4>
        {shown("models") && <div className="stack-err">{shown("models")}</div>}
        <div className="mtable" role="table">
          <div className="mrow mhead" role="row">
            <span>模型 ID</span>
            <span>显示名</span>
            <span>上下文窗口</span>
            <span>最大输出</span>
            <span>推理</span>
            <span>图片</span>
            <span />
          </div>
          {d.models.map((m, i) => (
            <div key={i} className="mrow" role="row">
              <TextInput label="模型 ID" mono value={m.id} onChange={(v) => setModel(i, { id: v })} placeholder="发给接口的模型名" />
              <TextInput label="显示名" value={m.displayName} onChange={(v) => setModel(i, { displayName: v })} placeholder={m.id || "菜单里的名字"} />
              <span title={m.contextWindow ? "已填写的窗口（优先于内置规格）" : modelSpecification(m.id) ? `内置规格：${modelSpecification(m.id)!.contextWindow} tokens · ${modelSpecification(m.id)!.source}` : "窗口未知，必须填写；不使用统一猜测值"}>
                <TextInput label="上下文窗口" type="number" mono width="100%" value={String(m.contextWindow ?? "")} onChange={(v) => setModel(i, { contextWindow: v ? Number(v) : undefined })} placeholder={modelSpecification(m.id) ? String(modelSpecification(m.id)!.contextWindow) : "未知，必填"} invalid={tried && !!m.id.trim() && !!contextProblem(m)} />
              </span>
              <TextInput label="最大输出" type="number" mono value={String(m.maxOutput ?? "")} onChange={(v) => setModel(i, { maxOutput: v ? Number(v) : undefined })} placeholder="接口默认" />
              <Select
                value={m.reasoning ? (ROW_EFFORTS.find((r) => r.value === m.reasoning)?.label ?? m.reasoning) : "无"}
                minWidth={160}
                items={[
                  { id: "none", label: "无", description: "不提供推理档位", checked: !m.reasoning, onSelect: () => setModel(i, { reasoning: "" }) },
                  ...ROW_EFFORTS.map((r) => ({ id: r.value, label: r.label, description: `默认 ${r.label}；实际支持以服务商为准`, checked: m.reasoning === r.value, onSelect: () => setModel(i, { reasoning: r.value }) })),
                ]}
              />
              <Switch on={m.images} onChange={(v) => setModel(i, { images: v })} label="支持图片" />
              <button type="button" className="icon-btn sm" aria-label="删除这个模型" title="删除这个模型" onClick={() => set("models", d.models.filter((_, j) => j !== i))}>
                <Trash2 size={14} />
              </button>
            </div>
          ))}
        </div>
        <div className="pe-btns">
          <button type="button" className="btn small" onClick={() => set("models", [...d.models, blankModel("", d.kind)])}>
            <Plus size={13} /> 添加模型
          </button>
          {!isBedrock && (
            <button type="button" className="btn small" disabled={test.busy} onClick={fetchModels}>
              从服务端获取列表
            </button>
          )}
        </div>
        {suggest && suggest.length > 0 && (
          <div className="suggest-ids">
            <span className="muted-text">点一下添加：</span>
            {suggest.map((s) => {
              const have = d.models.some((m) => m.id === s.id);
              return (
                <button
                  key={s.id}
                  type="button"
                  className={`id-chip${have ? " have" : ""}`}
                  disabled={have}
                  title={s.contextWindow ? `上下文窗口 ${formatTokens(s.contextWindow)}${s.maxOutput ? `，最大输出 ${formatTokens(s.maxOutput)}` : ""}` : undefined}
                  onClick={() => set("models", [...d.models.filter((m) => m.id.trim()), withRemoteFigures(blankModel(s.id, d.kind), s)])}
                >
                  {have && <Check size={11} />} {s.id}
                  {s.contextWindow ? <span className="muted-text"> · {formatTokens(s.contextWindow)}</span> : null}
                </button>
              );
            })}
          </div>
        )}
        <p className="pe-note">图片：图片输入默认只对 Anthropic 和 OpenAI 格式的模型打开；其他格式要自己确认模型支持再打开。推理：不认识的模型要在这里设一个默认档位，才会有推理菜单。</p>
        <p className="pe-note">上下文窗口：获取列表时优先读取服务端数据；服务端未提供时，按完整模型 ID 查内置规格表（灰色占位数字，悬停看来源），不猜测同系列或自定义别名。填写的值优先，网关可能比官方规格更小。仍未知的模型必须填写窗口才能使用，不再静默等到超限。窗口用于状态栏和自动压缩；默认在 85% 压缩，可在设置 › 会话调整。最大输出留空仍用接口默认值。</p>
      </section>

      <section className="pe-sec">
        <button type="button" className="disclose" aria-expanded={advanced} onClick={() => setAdvanced((v) => !v)}>
          <ChevronRight size={14} className={advanced ? "down" : ""} /> 高级
        </button>
        {advanced && (
          <div className="pe-adv">
            <Stack label="额外请求头" hint="每个请求都会带上，例如网关要求的标识。">
              {d.headers.map((h, i) => (
                <div key={i} className="hrow">
                  <TextInput label="请求头名" mono value={h.k} onChange={(v) => set("headers", d.headers.map((x, j) => (j === i ? { ...x, k: v } : x)))} placeholder="X-Custom-Header" />
                  <TextInput label="请求头值" mono value={h.v} onChange={(v) => set("headers", d.headers.map((x, j) => (j === i ? { ...x, v } : x)))} placeholder="value" />
                  <button type="button" className="icon-btn sm" aria-label="删除这个请求头" onClick={() => set("headers", d.headers.filter((_, j) => j !== i))}>
                    <X size={14} />
                  </button>
                </div>
              ))}
              <div className="pe-btns">
                <button type="button" className="btn small" onClick={() => set("headers", [...d.headers, { k: "", v: "" }])}>
                  <Plus size={13} /> 添加请求头
                </button>
              </div>
            </Stack>
            <Stack label="额外参数（JSON）" error={shown("extraArgs")} hint="并进每个请求体，例如温度。">
              <textarea className="textin selectable mono area" rows={3} spellCheck={false} aria-label="额外参数" value={d.extraArgs} onChange={(e) => set("extraArgs", e.target.value)} placeholder='{"temperature": 0.7, "top_p": 0.9}' />
            </Stack>
          </div>
        )}
      </section>

      <p className="pe-note">保存到 WittyCode 自己的 <code>models.json</code>（<code>customModels</code> 格式）。这些模型由 WittyCode 直接请求你的接口，计费走你自己的账号；Bedrock 用 AWS 凭据签名请求（或 Bedrock API Key）。</p>

      <footer className="pe-foot">
        <button type="button" className="btn" disabled={test.busy} onClick={runTest}>
          {test.busy ? <Loader2 size={14} className="spin" /> : null} 测试连接
        </button>
        {test.text && !test.busy && <span className={`test-res ${test.ok ? "ok" : "bad"}`}>{test.ok ? <Check size={14} /> : <X size={14} />} {test.text}</span>}
        <span className="spacer" />
        {!isNew && (
          <button type="button" className="btn danger" onClick={() => removeProvider(editId)}>
            删除
          </button>
        )}
        <button type="button" className="btn" onClick={() => setProviderEdit(null)}>
          取消
        </button>
        <button type="button" className="btn primary" disabled={saving} onClick={save}>
          保存
        </button>
      </footer>
    </div>
  );
}
