import { useEffect, useRef, useState } from "react";
import { useStore } from "../store";
import { sourceLabel } from "../lib/models";
import { effortLabel, reasoningChoices } from "../lib/vocab";
import { fusionEffort, withFusionEffort, type FusionRole } from "../lib/fusionReasoning";
import type { FusionSettingKey, SettingValue } from "../protocol";
import { Field, Select } from "./formkit";
import { fusionChoices, fusionDisabledReason } from "./fusionModels";
import { FusionStepBudget, SideRow } from "./FusionStepBudget";
import type { MenuItem } from "./ui";
import { ExpertCatalog } from "./ExpertCatalog";

function TimeoutInput({ seconds, adaptive, onSave }: { seconds: number; adaptive: boolean; onSave: (n: number, on: boolean) => void }) {
  const [draft, setDraft] = useState(String(seconds));
  const [auto, setAuto] = useState(adaptive);
  useEffect(() => { setDraft(String(seconds)); setAuto(adaptive); }, [seconds, adaptive]);
  const n = Number(draft);
  const valid = Number.isSafeInteger(n) && n >= 1 && n <= 86400;
  const remembered = valid ? n : seconds;
  const dirty = (auto || valid) && (auto !== adaptive || remembered !== seconds);
  const save = () => { if (dirty) onSave(remembered, auto); };
  return <div className="fusion-step-budget fusion-timeout" role="group" aria-label="单个专家最长运行秒数" onKeyDown={(e) => { if (e.key === "Enter" && e.target instanceof HTMLInputElement && e.target.type === "number") { e.preventDefault(); save(); } }}>
    <SideRow label="最长秒数" limit={86400} adaptive={auto} value={draft} onAdaptive={setAuto} onValue={setDraft} />
    <button type="button" className="btn step-save" disabled={!dirty} onClick={save}>保存</button>
    {!auto && !valid && <div className="field-hint err" role="alert">手填值需要 1 到 86400 的整数。</div>}
  </div>;
}

export function fusionPreferenceText(value: number): string {
  const n = Math.max(0, Math.min(100, Math.round(value)));
  if (n === 0) return "0：成本优先";
  if (n === 100) return "100：质量优先";
  return `${n}；0 为成本优先，100 为质量优先`;
}

function PreferenceInput({ value, onChange }: { value: number; onChange: (value: number) => void }) {
  const [draft, setDraft] = useState(value);
  const sent = useRef(value);
  useEffect(() => { setDraft(value); sent.current = value; }, [value]);
  const commit = (next: number) => {
    if (next !== sent.current) { sent.current = next; onChange(next); }
  };
  return <div className="fusion-preference">
    <input type="range" min={0} max={100} step={1} value={draft} aria-label="成本与质量倾向"
      aria-valuetext={fusionPreferenceText(draft)}
      onChange={(e) => setDraft(Number(e.target.value))}
      onPointerUp={(e) => commit(Number(e.currentTarget.value))}
      onKeyUp={(e) => commit(Number(e.currentTarget.value))}
      onBlur={(e) => commit(Number(e.currentTarget.value))} />
    <div className="fusion-preference-labels"><span>0 · 成本优先</span><span>100 · 质量优先</span></div>
    <div className="fusion-preference-weights">当前 {draft}</div>
  </div>;
}

export function FusionSettings() {
  const s = useStore();
  const name = (id: string) => s.models.find((model) => model.id === id)?.label ?? (id || "请选择");
  const items = (selected: string[], choose: (id: string) => void, main = false, current = ""): MenuItem[] => fusionChoices(s.models, s.hiddenModels).map((model) => ({
    id: model.id,
    label: model.label,
    description: fusionDisabledReason(model, main) || sourceLabel(model.source, s.providers),
    checked: selected.includes(model.id),
    disabled: model.disabled || (model.id !== current && selected.includes(model.id)),
    onSelect: () => choose(model.id),
  }));
  const set = (key: FusionSettingKey, value: SettingValue) => s.setFusionSetting(key, value);
  const setReasoning = (role: FusionRole, id: string, effort: string) => set("fusionReasoning", withFusionEffort(s.fusionReasoning, role, id, effort));

  const modelRow = (id: string, role: FusionRole, modelItems: MenuItem[], index = 0, remove?: () => void) => {
    const base = role === "main" ? "主模型" : role === "worker" ? "工作模型" : "轻量模型";
    const label = role === "main" ? base : `${base} ${index + 1}`;
    const model = s.models.find((item) => item.id === id);
    const effort = fusionEffort(s.fusionReasoning, role, id);
    const choices = model ? reasoningChoices(model) : [];
    const unsupported = !!effort && !choices.some((item) => item.value === effort);
    const value = !id ? "思考等级" : effort ? `${effortLabel(effort)}${unsupported ? "（已不支持）" : ""}` : !model ? "模型不可用" : choices.length ? "默认" : "不支持";
    return <div className="fusion-model-row" key={`${role}:${index}:${id || "empty"}`} data-role={role} data-model={id} role="group" aria-label={`${label} ${name(id)}`} style={remove ? { gridTemplateColumns: "minmax(0,1fr) 160px auto" } : undefined}>
      <div className="fusion-model-picker" title={name(id)}>
        <Select value={id ? name(id) : "请选择"} items={modelItems} ariaLabel={`${label}选择：${name(id)}`} />
      </div>
      <div className="fusion-effort-picker" title={`${value}；思考等级按角色和模型独立保存，同一模型的其他角色不受影响。默认保留原有行为。`}>
        <Select value={value} disabled={!id || (!effort && !choices.length)} minWidth={160} ariaLabel={`${label}思考等级：${value}`} items={[
          { id: "default", label: "默认", checked: !effort, onSelect: () => setReasoning(role, id, "") },
          ...choices.map((choice) => ({ id: choice.value, label: choice.label, checked: effort === choice.value, onSelect: () => setReasoning(role, id, choice.value) })),
        ]} />
      </div>
      {remove && <button type="button" className="btn small danger" aria-label={`删除${label} ${name(id)}`} onClick={remove}>删除</button>}
    </div>;
  };

  const replacePool = (role: "worker" | "light", pool: string[], index: number, id: string) => {
    if (id && pool.includes(id)) return;
    const next = [...pool];
    if (!id) next.splice(index, 1);
    else if (index < next.length) next[index] = id;
    else next.push(id);
    set(role === "worker" ? "fusionWorkers" : "fusionLights", next);
  };

  const poolRows = (role: "worker" | "light") => {
    const pool = role === "worker" ? s.fusionWorkers : s.fusionLights;
    const rows = pool.length ? pool : [""];
    const key = role === "worker" ? "fusionWorkers" : "fusionLights";
    const label = role === "worker" ? "工作模型" : "轻量模型";
    return <div className="fusion-model-list" data-pool={role}>
      {rows.map((id, index) => modelRow(
        id,
        role,
        items(pool, (next) => replacePool(role, pool, index, next), false, id),
        index,
        id ? () => replacePool(role, pool, index, "") : undefined,
      ))}
      <div className="fusion-model-add">
        <Select
          value={`添加${label}`}
          ariaLabel={`添加${label}`}
          minWidth={180}
          disabled={!fusionChoices(s.models, s.hiddenModels).some((model) => !pool.includes(model.id) && !model.disabled)}
          items={fusionChoices(s.models, s.hiddenModels).filter((model) => !pool.includes(model.id)).map((model) => ({
            id: model.id,
            label: model.label,
            description: fusionDisabledReason(model) || sourceLabel(model.source, s.providers),
            disabled: model.disabled,
            onSelect: () => set(key, [...pool, model.id]),
          }))}
        />
      </div>
    </div>;
  };

  return (
    <>
      <h3 className="settings-section-title">自有 Fusion</h3>
      <p className="page-note">在输入框的模型菜单选择 Fusion；已有会话也可在空闲时切换。修改配置后，Fusion 会话在下一轮发送时应用，不打断正在运行的主代理或专家。</p>
      <Field label="主模型" hint="主代理负责任务分析、分配和最终验收；L1 固定选择一个模型。更换主模型时，下一轮发送前自动交接上下文，历史记录保留。">
        {modelRow(s.fusionMain, "main", items([s.fusionMain], (id) => set("fusionMain", id), true, s.fusionMain))}
      </Field>
      <Field label="工作模型" hint="供 Sub-Agent 执行工作任务使用；可以添加多位候选，主代理按任务和偏好选择。">
        {poolRows("worker")}
      </Field>
      <Field label="轻量模型" hint="供简单任务使用，不会为了保活定时调用；顺序保留优先级，第一个仍写入旧版兼容字段。">
        {poolRows("light")}
      </Field>
      <Field label="成本与质量倾向" hint="影响候选模型排序，并参与派工与审核时的偏好判断：0 是成本优先，100 是质量优先。它只表达偏好，不承诺价格、结果质量、费用上限或账单金额。">
        <PreferenceInput value={s.fusionPreference} onChange={(v) => set("fusionPreference", v)} />
      </Field>
      <Field label="单个专家最长运行秒数" hint="Adaptive 按每次委派估时，不受灰色数字限制。取消勾选可设置每次运行的硬上限；任务书或计划可再缩短。验收检查可有限次重试，不会重做写入任务。">
        <TimeoutInput seconds={s.fusionMaxSeconds} adaptive={s.fusionMaxSecondsAdaptive}
          onSave={(seconds, adaptive) => set("fusionMaxSeconds", { seconds, adaptive })} />
      </Field>
      <Field label="委派步数范围" hint="对每次专家委派生效；勾选 Adaptive 的一侧由 Fusion 按任务规模计算，取消勾选手填（数字会记住）。主代理请求的步数夹在区间内，全局单轮步数上限始终优先；专家可提前交报告。">
        <FusionStepBudget min={s.fusionStepsMin} minAdaptive={s.fusionStepsMinAdaptive} max={s.fusionStepsMax} maxAdaptive={s.fusionStepsMaxAdaptive}
          onSave={(lo, loAd, hi, hiAd) => set("fusionSteps", { min: lo, minAdaptive: loAd, max: hi, maxAdaptive: hiAd })} />
      </Field>
      <ExpertCatalog />
      <p className="page-note">主代理决定委派次数；首次启用不设调用次数或 Token 上限，实际费用取决于模型和用量。已隐藏的模型不会出现在选择菜单；已选模型仍保留。</p>
      <p className="page-note">主代理和专家共用 WittyCode 权限、Plan 审批与证据保存。Devin 和 Factory 通过受控工具桥接入。Factory 还需要兼容的 CLI、机器托管策略和独立鉴权；无法确认时拒绝运行，不会改用其他模型。</p>
    </>
  );
}
