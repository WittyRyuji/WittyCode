import { useEffect, useMemo, useRef, useState } from "react";
import { AlertTriangle, ArrowLeft, Check, ChevronRight, Copy, Loader2, Plus, Trash2, X } from "lucide-react";
import { useStore } from "../store";
import { bridge } from "../bridge-instance";
import { KIND_INFO, PRESETS, REASONING, type Preset } from "../lib/vocab";
import { envHintOf, envNamesOf, envVarText, sameProviderEndpoint } from "../lib/models";
import { LIVE } from "../mode";
import { API_EFFORTS, contextProblem, imagesAfterKindChange, imagesByDefault, modelSpecification, withRemoteFigures } from "../lib/providerDefaults";
import { messageOf } from "../lib/errors";
import { hasAdvancedProviderOptions, reasoningArgsProblem } from "../lib/reasoningArgs";
import { formatTokens } from "../lib/format";
import type { ApiKind, ApiModel, EnvState, KeyMode, ProviderDraft, RemoteModel } from "../types";
import { Seg, Select, Stack, Switch, TextInput } from "./formkit";
import type { MenuItem } from "./ui";

const KEY_MODES: { id: KeyMode; label: string }[] = [
  { id: "env", label: "环境变量" },
  { id: "none", label: "不需要" },
  { id: "protected", label: "直接输入（不推荐）" },
];

/** The variable a provider at this address conventionally uses, so switching to "环境变量" starts from a sensible name. */
const suggestedVariable = (baseUrl: string) => {
  const p = PRESETS.find((x) => x.envName && x.baseUrl && x.baseUrl === baseUrl);
  return p?.envName ?? "";
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
type KeyRow = { id: number; name: string; storedIndex?: number };
type EditableModel = ApiModel & { envKey?: string };
type EditorDraft = Omit<ProviderDraft, "models"> & { models: EditableModel[] };
const keyRow = (name = "", storedIndex?: number): KeyRow => ({ id: ++rowCounter, name, storedIndex });
const initialKeys = (existing?: ProviderDraft): KeyRow[] => existing?.keyMode === "protected" && existing.availableKeys?.length
  ? existing.availableKeys.map(({ name }, index) => keyRow(name, index))
  : [keyRow()];
const keyLabel = (name: string, index: number) => name.trim() || `密钥 ${index + 1}`;
const keyIndex = (model: ApiModel, count: number) => (model.selectedKeyIndex ?? 0) < count ? (model.selectedKeyIndex ?? 0) : 0;
const blankModel = (id: string, kind: ApiKind): ApiModel => ({ id, displayName: "", reasoning: "", images: imagesByDefault(kind) });

/** What the server's list says about a model fills the figures its row leaves empty; what the user typed stays. */
const withFigures = (m: ApiModel, found: Map<string, RemoteModel>): ApiModel => {
  const f = found.get(m.id.trim());
  return withRemoteFigures(m, f);
};

function draftFrom(editId: string, existing: ProviderDraft | undefined): EditorDraft {
  if (existing) {
    if (existing.keyMode !== "env") return existing;
    const names = envNamesOf(existing.keyHint);
    return { ...existing, keyHint: envVarText(existing.keyHint), models: existing.models.map((m) => ({
      ...m, envKey: names[m.selectedKeyIndex ?? 0],
    })) };
  }
  const presetId = editId.startsWith("new:") ? editId.slice(4) : "custom";
  const p: Preset = PRESETS.find((x) => x.id === presetId) ?? PRESETS[PRESETS.length - 1]!;
  return {
    id: `p${Date.now().toString(36)}${++rowCounter}`,
    name: p.id === "custom" ? "" : p.name,
    kind: p.kind,
    baseUrl: p.baseUrl,
    keyMode: p.keyMode,
    keyHint: p.envName ?? "",
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
  if (d.keyMode === "env" && !envNamesOf(envHintOf(d.keyHint)).length) e.key = "只写变量名，不是密钥本身。变量名由字母、数字、下划线组成，不能以数字开头；多个用逗号分隔。";
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
  const reasoningProblem = d.kind === "bedrock-converse" ? "" : reasoningArgsProblem(d.reasoningArgs);
  if (reasoningProblem) e.reasoningArgs = reasoningProblem;
  return e;
}

export function ProviderEditor({ editId }: { editId: string }) {
  const existing = useStore((s) => s.providers.find((p) => p.id === editId));
  const { setProviderEdit, saveProvider, removeProvider, say } = useStore.getState();
  const [d, setD] = useState<EditorDraft>(() => draftFrom(editId, existing ? { ...existing, status: undefined } : undefined));
  const [envStates, setEnvStates] = useState<Record<string, EnvState>>({});
  const [recheck, setRecheck] = useState(0);
  const envNames = useMemo(() => d.keyMode === "env" ? envNamesOf(envHintOf(d.keyHint)) : [], [d.keyMode, d.keyHint]);
  useEffect(() => {
    setEnvStates({});
    if (!envNames.length) return;
    let live = true;
    bridge.providers
      .envStatus([...new Set(envNames)])
      .then((r) => live && setEnvStates(r))
      .catch((error) => say(messageOf(error)));
    return () => {
      live = false;
    };
  }, [envNames, recheck, say]);
  const [tried, setTried] = useState(false);
  const [test, setTest] = useState<{ busy: boolean; ok?: boolean; text?: string }>({ busy: false });
  const [suggest, setSuggest] = useState<RemoteModel[] | null>(null);
  const [advanced, setAdvanced] = useState(hasAdvancedProviderOptions(existing));
  const [saving, setSaving] = useState(false);
  const [keyProblem, setKeyProblem] = useState("");
  const [keyRows, setKeyRows] = useState(() => initialKeys(existing));
  const keyInputs = useRef(new Map<number, HTMLInputElement>());
  const connectionRevision = useRef(0);
  const isNew = !existing;
  const sameEndpoint = !!existing && sameProviderEndpoint(d, existing);
  const keepKey = sameEndpoint && existing?.keyMode === d.keyMode;
  const keyLabels = d.keyMode === "env" ? envNames : d.keyMode === "protected" ? keyRows.map((row, i) => keyLabel(row.name, i)) : [];
  const hasKeyChoice = keyLabels.length > 1;
  const selection = (model: EditableModel) => d.keyMode === "env" ? Math.max(0, envNames.indexOf(model.envKey ?? "")) : keyIndex(model, keyLabels.length);
  const errors = useMemo(() => validate(d), [d]);
  const shown = (k: string) => (tried ? errors[k] : undefined);
  const resetConnection = (clearKey = false) => {
    if (clearKey) keyInputs.current.forEach((input) => { input.value = ""; });
    connectionRevision.current++;
    setKeyProblem("");
    setTest({ busy: false });
    setSuggest(null);
  };
  const requestDraft = (): ProviderDraft | null => {
    if (d.keyMode === "env" && !envNames.length) {
      setTried(true);
      return null;
    }
    const draft = { ...d, keyHint: d.keyMode === "env" ? envHintOf(d.keyHint) : d.keyHint,
      models: d.models.map((model) => {
        const { envKey: _, ...m } = model;
        return { ...m, selectedKeyIndex: selection(model) || undefined };
      }) };
    if (d.keyMode === "protected") {
      const newApiKeys = keyRows.map(({ id, name, storedIndex }) => {
        const value = keyInputs.current.get(id)?.value.trim();
        return { name: name.trim(), key: value ? (LIVE ? value : "demo-key") : undefined, storedIndex: keepKey ? storedIndex : undefined };
      });
      if (newApiKeys.some(({ key, storedIndex }) => !key && storedIndex === undefined)) {
        setKeyProblem(existing?.keyMode === "protected" && !sameEndpoint
          ? "接口类型或 Base URL 已更改，请为新接口重新输入 API Key。"
          : "请输入 API Key，或删除空行。");
        return null;
      }
      return { ...draft, newApiKeys };
    } else if (d.keyMode === "inline" && !keepKey) {
      setKeyProblem("接口类型或 Base URL 已更改，请选择其他密钥来源。");
      return null;
    }
    return draft;
  };
  const set = <K extends keyof ProviderDraft>(k: K, v: ProviderDraft[K]) => {
    if (["baseUrl", "keyHint", "bearer", "headers"].includes(k)) resetConnection(k === "baseUrl");
    setD((x) => ({ ...x, [k]: v }));
  };
  const setModel = (i: number, patch: Partial<EditableModel>) => setD((x) => ({ ...x, models: x.models.map((m, j) => (j === i ? { ...m, ...patch } : m)) }));
  const removeKey = (index: number) => {
    resetConnection();
    setKeyRows((rows) => rows.filter((_, i) => i !== index));
    setD((x) => ({ ...x, models: x.models.map((m) => {
      const selected = keyIndex(m, keyRows.length);
      return { ...m, selectedKeyIndex: selected === index ? 0 : selected > index ? selected - 1 : selected };
    }) }));
  };

  const presetItems: MenuItem[] = PRESETS.map((p) => ({
    id: p.id,
    label: p.name,
    description: p.local ? "本机服务，不需要密钥" : p.baseUrl.replace(/^https?:\/\//, "") || undefined,
    onSelect: () => {
      resetConnection(true);
      setKeyRows(initialKeys());
      setD(draftFrom(`new:${p.id}`, undefined));
    },
  }));
  const kindItems: MenuItem[] = (Object.keys(KIND_INFO) as ApiKind[]).map((k) => ({
    id: k,
    label: KIND_INFO[k].label,
    description: KIND_INFO[k].hint,
    checked: d.kind === k,
    onSelect: () => {
      if (k === d.kind) return;
      resetConnection(true);
      setD((x) => ({
        ...x,
        kind: k,
        models: x.models.map((m) => ({ ...m, images: imagesAfterKindChange(m.images, x.kind, k), selectedKeyIndex: k === "bedrock-converse" ? undefined : m.selectedKeyIndex })),
        bearer: k === "anthropic" ? x.bearer : undefined,
        reasoningArgs: k === "bedrock-converse" ? undefined : x.reasoningArgs,
        reasoningArgsMixed: k === "bedrock-converse" ? undefined : x.reasoningArgsMixed,
        bedrock: k === "bedrock-converse" ? (x.bedrock ?? { method: "aws-credentials", region: "us-west-2", profile: "" }) : undefined,
        keyMode: k === "bedrock-converse" ? "none" : x.kind === "bedrock-converse" ? "env" : x.keyMode,
        keyHint: x.kind === "bedrock-converse" ? suggestedVariable(x.baseUrl) : x.keyHint,
      }));
    },
  }));

  const runTest = async () => {
    const draft = requestDraft();
    if (!draft) return;
    const revision = ++connectionRevision.current;
    setTest({ busy: true });
    try {
      const r = await bridge.providers.test(draft);
      if (revision === connectionRevision.current) setTest({ busy: false, ok: r.ok, text: r.text });
    } catch (error) {
      if (revision !== connectionRevision.current) return;
      setTest({ busy: false });
      say(messageOf(error));
    }
  };
  const fetchModels = async () => {
    const draft = requestDraft();
    if (!draft) return;
    const revision = ++connectionRevision.current;
    setSuggest(null);
    setTest({ busy: true });
    try {
      const found = await bridge.providers.listModels(draft);
      if (revision !== connectionRevision.current) return;
      const byId = new Map(found.map((m) => [m.id, m]));
      const filled = d.models.filter((m) => {
        const next = withFigures(m, byId);
        return next.contextWindow !== m.contextWindow || next.maxOutput !== m.maxOutput;
      }).length;
      if (filled) setD((x) => ({ ...x, models: x.models.map((m) => withFigures(m, byId)) }));
      const serverWindows = found.filter((m) => m.contextWindow && m.contextSource !== "catalog").length;
      const catalogWindows = found.filter((m) => m.contextSource === "catalog").length;
      const text = !found.length
        ? "没有取得模型列表。可用「测试连接」查看原因，或手动填模型 ID 和上下文窗口。"
        : `服务端列出了 ${found.length} 个模型；${serverWindows} 个窗口来自服务端，${catalogWindows} 个由内置规格表补全` +
          "。其余未知模型需填写上下文窗口，不使用统一猜测值" +
          (filled ? `；已补上 ${filled} 行的空白项` : "");
      setTest({ busy: false, ok: found.length > 0, text });
      setSuggest(found);
    } catch (error) {
      if (revision !== connectionRevision.current) return;
      setTest({ busy: false });
      say(messageOf(error));
    }
  };
  const save = async () => {
    setTried(true);
    const draft = requestDraft();
    if (!draft || Object.keys(errors).length) return;
    setSaving(true);
    const ok = await saveProvider({ ...draft, name: d.name.trim(), baseUrl: d.baseUrl.trim(), models: draft.models.filter((m) => m.id.trim()).map((m) => ({ ...m, id: m.id.trim() })) });
    setSaving(false);
    if (ok) {
      keyInputs.current.forEach((input) => { input.value = ""; });
      setProviderEdit(null);
    }
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
                  这个 Provider 的旧密钥以<b>明文</b>写在 models.json 里（<code>{d.keyHint}</code>）。不改接口可以继续使用；建议切换到「环境变量」。保存其他密钥来源后，模型配置中的明文会被移除。{keyProblem && <div className="stack-err">{keyProblem}</div>}
                </div>
              </div>
            )}
            <Seg
              value={d.keyMode}
              options={KEY_MODES}
              onChange={(m) => {
                if (m === d.keyMode) return;
                resetConnection(true);
                const hint = m === "env"
                  ? (existing?.keyMode === "env" && sameEndpoint ? envVarText(existing.keyHint) : suggestedVariable(d.baseUrl))
                  : "";
                if (m === "protected") setKeyRows(initialKeys(existing));
                const savedModels = sameEndpoint && existing?.keyMode === m ? draftFrom(editId, existing).models : [];
                setD((x) => ({ ...x, keyMode: m, keyHint: hint, models: x.models.map((model) => {
                  const saved = savedModels.find((row) => row.id === model.id);
                  return { ...model, selectedKeyIndex: saved?.selectedKeyIndex, envKey: saved?.envKey };
                }) }));
              }}
            />
            {d.keyMode === "protected" && (
              <>
                <div className="pe-warn" role="note">
                  <AlertTriangle size={15} />
                  <div><b>不推荐直接输入。</b>优先用环境变量。保存时由当前 Windows 用户加密；换用户或电脑后可能需要重新输入。请只为你信任的接口提供密钥，远程服务建议使用 HTTPS。{!LIVE && "演示页请勿输入真实密钥。"}</div>
                </div>
                <Stack label="API Key" error={keyProblem} hint={keepKey
                  ? "已保存的密钥留空保留，输入新值可替换。更改接口类型或 Base URL 后必须重新输入。"
                  : "可按名称区分同一接口的多个密钥。名称仅用于显示，不要把密钥填进名称。"}>
                  {keyRows.map((row, i) => (
                    <div key={row.id} className="hrow">
                      <TextInput label={`密钥名称 ${i + 1}`} value={row.name} placeholder={`密钥 ${i + 1}（名称可选）`} onChange={(name) => setKeyRows((rows) => rows.map((x) => x.id === row.id ? { ...x, name } : x))} />
                      <TextInput label={i === 0 ? "API Key" : `API Key ${i + 1}`} type="password" mono
                        placeholder={keepKey && row.storedIndex !== undefined ? "已保存，留空保留" : "输入 API Key"}
                        inputRef={(input) => { if (input) keyInputs.current.set(row.id, input); else keyInputs.current.delete(row.id); }}
                        onChange={() => resetConnection()} />
                      <button type="button" className="icon-btn sm" aria-label={`删除密钥 ${i + 1}`} title="删除密钥" disabled={keyRows.length === 1} onClick={() => removeKey(i)}>
                        <Trash2 size={14} />
                      </button>
                    </div>
                  ))}
                  <div className="pe-btns">
                    <button type="button" className="btn small" onClick={() => { resetConnection(); setKeyRows((rows) => [...rows, keyRow()]); }}>
                      <Plus size={13} /> 添加 Key
                    </button>
                  </div>
                </Stack>
              </>
            )}
            {d.keyMode === "env" && (
              <>
                <Stack label="环境变量" error={shown("key")} hint="只写变量名，多个用逗号分隔，不用加 ${}。配置文件只记变量名，不记密钥。">
                  <TextInput label="环境变量" mono value={d.keyHint}
                    onChange={(v) => set("keyHint", v.replace(/(^|,)(\s*)\$\{([A-Za-z_][A-Za-z0-9_]*)\}(?=\s*(?:,|$))/g, "$1$2$3"))}
                    placeholder="OPENROUTER_API_KEY, OPENROUTER_API_KEY_2" invalid={!!shown("key")} />
                </Stack>
                {[...new Set(envNames)].map((name) => <EnvLine key={name} name={name} state={Object.hasOwn(envStates, name) ? envStates[name]! : null} onRecheck={() => setRecheck((n) => n + 1)} />)}
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
        {hasKeyChoice && <p className="pe-note">每个模型可单独选择密钥；测试连接和获取模型列表使用第一把。</p>}
        <div className={`mtable${hasKeyChoice ? " with-keys" : ""}`} role="table">
          <div className="mrow mhead" role="row">
            <span>模型 ID</span>
            <span>显示名</span>
            <span>上下文窗口</span>
            <span>最大输出</span>
            <span>推理</span>
            {hasKeyChoice && <span>API Key</span>}
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
              {hasKeyChoice && <Select
                ariaLabel={`模型 ${m.id || i + 1} 的 API Key`}
                value={keyLabels[selection(m)] ?? ""}
                minWidth={160}
                items={keyLabels.map((label, index) => ({
                  id: String(index), label, checked: index === selection(m),
                  onSelect: () => setModel(i, d.keyMode === "env" ? { envKey: label } : { selectedKeyIndex: index }),
                }))}
              />}
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
            {!isBedrock && <Stack label="思考档位参数（JSON，可选）" error={shown("reasoningArgs")} hint={d.reasoningArgsMixed
              ? "这些模型的映射不同；未编辑时保留各自原值，输入后统一覆盖，填写 {} 可清除全部。"
              : "按本轮档位覆盖顶层字段，null 删除字段；额外参数优先。此处保存到本 Provider 的全部模型；服务支持哪些字段和档位，以其文档为准。"}>
              <textarea className="textin selectable mono area" rows={4} spellCheck={false} aria-label="思考档位参数" value={d.reasoningArgs ?? ""} onChange={(e) => set("reasoningArgs", e.target.value)} placeholder='{"off": {"thinking": {"type": "disabled"}}}' />
            </Stack>}
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
