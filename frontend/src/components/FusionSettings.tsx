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

function PreferenceInput({ value, onChange }: { value: number; onChange: (value: number) => void }) {
  const [draft, setDraft] = useState(value);
  const sent = useRef(value);
  useEffect(() => { setDraft(value); sent.current = value; }, [value]);
  const commit = (next: number) => {
    if (next !== sent.current) { sent.current = next; onChange(next); }
  };
  return <div className="fusion-preference">
    <input type="range" min={0} max={100} step={5} value={draft} aria-label="成本与质量倾向"
      aria-valuetext={`成本权重 ${(1.5 - draft / 100).toFixed(2)}，质量权重 ${(0.5 + draft / 100).toFixed(2)}`}
      onChange={(e) => setDraft(Number(e.target.value))}
      onPointerUp={(e) => commit(Number(e.currentTarget.value))}
      onKeyUp={(e) => commit(Number(e.currentTarget.value))}
      onBlur={(e) => commit(Number(e.currentTarget.value))} />
    <div className="fusion-preference-labels"><span>偏向成本</span><span>偏向质量</span></div>
    <div className="fusion-preference-weights">成本 ×{(1.5 - draft / 100).toFixed(2)} · 质量 ×{(0.5 + draft / 100).toFixed(2)}</div>
  </div>;
}

export function FusionSettings() {
  const s = useStore();
  const name = (id: string) => s.models.find((model) => model.id === id)?.label ?? (id || "请选择");
  const items = (selected: string[], choose: (id: string) => void, main = false): MenuItem[] => fusionChoices(s.models, s.hiddenModels).map((model) => ({
    id: model.id, label: model.label, description: fusionDisabledReason(model, main) || sourceLabel(model.source, s.providers),
    checked: selected.includes(model.id), disabled: model.disabled, onSelect: () => choose(model.id),
  }));
  const set = (key: FusionSettingKey, value: SettingValue) => s.setFusionSetting(key, value);
  const setReasoning = (role: FusionRole, id: string, effort: string) => set("fusionReasoning", withFusionEffort(s.fusionReasoning, role, id, effort));
  const modelRow = (id: string, role: FusionRole, modelItems: MenuItem[]) => {
    const label = role === "main" ? "主模型" : role === "worker" ? "工作模型" : "轻量模型";
    const model = s.models.find((item) => item.id === id);
    const effort = fusionEffort(s.fusionReasoning, role, id);
    const choices = model ? reasoningChoices(model) : [];
    const unsupported = !!effort && !choices.some((item) => item.value === effort);
    const value = !id ? "思考等级" : effort ? `${effortLabel(effort)}${unsupported ? "（已不支持）" : ""}` : !model ? "模型不可用" : choices.length ? "默认" : "不支持";
    return <div className="fusion-model-row" key={`${role}:${id}`} data-role={role} data-model={id} role="group" aria-label={`${label} ${name(id)}`}>
      <div className="fusion-model-picker" title={name(id)}>
        <Select value={id ? name(id) : "请选择"} items={modelItems} ariaLabel={`${label}选择：${name(id)}`} />
      </div>
      <div className="fusion-effort-picker" title={`${value}；思考等级按角色和模型独立保存，同一模型的其他角色不受影响。默认保留原有行为。`}>
        <Select value={value} disabled={!effort && !choices.length} minWidth={160} ariaLabel={`${label}思考等级：${value}`} items={[
          { id: "default", label: "默认", checked: !effort, onSelect: () => setReasoning(role, id, "") },
          ...choices.map((choice) => ({ id: choice.value, label: choice.label, checked: effort === choice.value, onSelect: () => setReasoning(role, id, choice.value) })),
        ]} />
      </div>
    </div>;
  };
  const worker = s.fusionWorkers[0] ?? "";
  const workerItems = items([worker], (id) => set("fusionWorkers", [id]));
  return (
    <>
      <h3 className="settings-section-title">自有 Fusion</h3>
      <p className="page-note">在输入框的模型菜单选择 Fusion；已有会话也可在空闲时切换。修改配置后，Fusion 会话在下一轮发送时应用，不打断正在运行的主代理或专家。</p>
      <Field label="主模型" hint="主代理负责任务分析、分配和最终验收；更换主模型时，下一轮发送前自动交接上下文，历史记录保留。">
        {modelRow(s.fusionMain, "main", items([s.fusionMain], (id) => set("fusionMain", id), true))}
      </Field>
      <Field label="工作模型" hint="供 Sub-Agent 执行工作任务使用；固定选择一个模型。">
        {modelRow(worker, "worker", workerItems)}
      </Field>
      <Field label="轻量模型" hint="供简单任务使用，不会为了保活定时调用。">
        {modelRow(s.fusionLight, "light", items([s.fusionLight], (id) => set("fusionLight", id)))}
      </Field>
      <Field label="成本与质量倾向" hint="作为主代理选择轻量模型或工作模型的相对权重，不是价格、结果质量或费用上限的保证。">
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
      <p className="page-note">主代理决定委派次数；首次启用不设调用次数或 Token 上限，实际费用取决于模型和用量。已隐藏的模型不会出现在选择菜单；已选模型仍保留。</p>
      <p className="page-note">主代理和专家共用 WittyCode 权限、Plan 审批与证据保存。Devin 和 Factory 通过受控工具桥接入。Factory 还需要兼容的 CLI、机器托管策略和独立鉴权；无法确认时拒绝运行，不会改用其他模型。</p>
    </>
  );
}
