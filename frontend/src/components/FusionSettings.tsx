import { useEffect, useRef, useState } from "react";
import { useStore } from "../store";
import { sourceLabel } from "../lib/models";
import { effortLabel, reasoningChoices } from "../lib/vocab";
import { fusionEffort, withFusionEffort, type FusionRole } from "../lib/fusionReasoning";
import type { FusionSettingKey, SettingValue } from "../protocol";
import { Field, Select } from "./formkit";
import { fusionChoices, fusionDisabledReason } from "./fusionModels";
import type { MenuItem } from "./ui";

function BudgetInput({ label, value, max, onChange }: { label: string; value: number; max: number; onChange: (value: number) => void }) {
  const [draft, setDraft] = useState(String(value));
  useEffect(() => setDraft(String(value)), [value]);
  const commit = () => {
    const next = Number(draft);
    if (!Number.isSafeInteger(next) || next < 1 || next > max) setDraft(String(value));
    else if (next !== value) onChange(next);
  };
  return <input className="textin selectable" type="number" min={1} max={max} value={draft} aria-label={label} style={{ width: 130 }}
                onChange={(e) => setDraft(e.target.value)} onBlur={commit} onKeyDown={(e) => e.key === "Enter" && e.currentTarget.blur()} />;
}

function PreferenceInput({ value, onChange }: { value: number; onChange: (value: number) => void }) {
  const [draft, setDraft] = useState(value);
  const sent = useRef(value);
  useEffect(() => { setDraft(value); sent.current = value; }, [value]);
  const commit = (next: number) => {
    if (next !== sent.current) { sent.current = next; onChange(next); }
  };
  return <div className="fusion-preference">
    <input type="range" min={0} max={100} step={5} value={draft} aria-label="成本与效率倾向"
      aria-valuetext={`成本权重 ${(1.5 - draft / 100).toFixed(2)}，效率权重 ${(0.5 + draft / 100).toFixed(2)}`}
      onChange={(e) => setDraft(Number(e.target.value))}
      onPointerUp={(e) => commit(Number(e.currentTarget.value))}
      onKeyUp={(e) => commit(Number(e.currentTarget.value))}
      onBlur={(e) => commit(Number(e.currentTarget.value))} />
    <div className="fusion-preference-labels"><span>偏向成本</span><span>偏向效率</span></div>
    <div className="fusion-preference-weights">成本 ×{(1.5 - draft / 100).toFixed(2)} · 效率 ×{(0.5 + draft / 100).toFixed(2)}</div>
  </div>;
}

export function FusionSettings() {
  const s = useStore();
  const name = (id: string) => s.models.find((model) => model.id === id)?.label ?? (id || "请选择");
  const items = (selected: string[], choose: (id: string) => void, main = false): MenuItem[] => fusionChoices(s.models, s.hiddenModels, true).map((model) => ({
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
        <Select value={id ? name(id) : role === "worker" ? "请选择一个或多个" : "请选择"} items={modelItems} ariaLabel={`${label}选择：${name(id)}`} />
      </div>
      <div className="fusion-effort-picker" title={`${value}；思考等级按角色和模型独立保存，同一模型的其他角色不受影响。默认保留原有行为。`}>
        <Select value={value} disabled={!effort && !choices.length} minWidth={160} ariaLabel={`${label}思考等级：${value}`} items={[
          { id: "default", label: "默认", checked: !effort, onSelect: () => setReasoning(role, id, "") },
          ...choices.map((choice) => ({ id: choice.value, label: choice.label, checked: effort === choice.value, onSelect: () => setReasoning(role, id, choice.value) })),
        ]} />
      </div>
    </div>;
  };
  const workerItems = items(s.fusionWorkers, (id) => set("fusionWorkers", s.fusionWorkers.includes(id) ? s.fusionWorkers.filter((item) => item !== id) : [...s.fusionWorkers, id]));
  return (
    <>
      <h3 className="settings-section-title">自有 Fusion</h3>
      <p className="page-note">在输入框的模型菜单选择 Fusion；已有会话也可在空闲时切换。这里设置首次启用的默认配置，已有或暂停的 Fusion 会话保留自身配置。</p>
      <Field label="主模型" hint="任务分析、分配和最终验收由同一主代理完成；Fusion 启用时通过「更换主模型」确认调整。">
        {modelRow(s.fusionMain, "main", items([s.fusionMain], (id) => set("fusionMain", id), true))}
      </Field>
      <Field label="工作模型池" hint="允许主代理调用的模型；点模型框可增减，再次选择可取消。专家任务目前串行执行。">
        <div className="fusion-model-list">
          {(s.fusionWorkers.length ? s.fusionWorkers : [""]).map((id) => modelRow(id, "worker", workerItems))}
        </div>
      </Field>
      <Field label="轻量模型" hint="供简单任务使用，不会为了保活定时调用。">
        {modelRow(s.fusionLight, "light", items([s.fusionLight], (id) => set("fusionLight", id)))}
      </Field>
      <Field label="成本与效率倾向" hint="作为主代理选择轻量模型或工作模型的相对权重，不是价格、速度或费用上限的保证。">
        <PreferenceInput value={s.fusionPreference} onChange={(v) => set("fusionPreference", v)} />
      </Field>
      <Field label="单个专家最长运行秒数" hint="超时会停止并保留记录；未确认的任务不会自动重做。">
        <BudgetInput label="单个专家最长运行秒数" value={s.fusionMaxSeconds} max={86400} onChange={(v) => set("fusionMaxSeconds", v)} />
      </Field>
      <p className="page-note">主代理决定委派次数；首次启用不设调用次数或 Token 上限，实际费用取决于模型和用量。已隐藏的模型不会出现在选择菜单；已选模型仍保留。</p>
      <p className="page-note">主代理和专家共用 WittyCode 权限、Plan 审批与证据保存。Devin 通过独立 ACP 工具桥接入，启动时必须确认原生工具保护已加载；不兼容的 CLI 会拒绝运行。权限不是操作系统沙箱，独立 Devin 会话不受影响。</p>
    </>
  );
}
