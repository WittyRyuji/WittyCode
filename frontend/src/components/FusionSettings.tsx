import { useEffect, useState } from "react";
import { useStore } from "../store";
import { sourceLabel } from "../lib/models";
import type { FusionSettingKey } from "../protocol";
import { Field, Select } from "./formkit";
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

export function FusionSettings() {
  const s = useStore();
  const own = s.models.filter((model) => model.source === "chatgpt" || model.source.startsWith("api:"));
  const name = (id: string) => own.find((model) => model.id === id)?.label ?? (id || "请选择");
  const items = (selected: string[], choose: (id: string) => void): MenuItem[] => own.map((model) => ({
    id: model.id, label: model.label, description: model.disabledReason || sourceLabel(model.source, s.providers),
    checked: selected.includes(model.id), disabled: model.disabled, onSelect: () => choose(model.id),
  }));
  const set = (key: FusionSettingKey, value: string | string[] | number) => s.setFusionSetting(key, value);
  const workers = s.fusionWorkers.map(name).join("、") || "请选择一个或多个";
  return (
    <>
      <h3 className="settings-section-title">自有 Fusion</h3>
      <p className="page-note">在新会话输入框选择 Fusion 后使用。设置只影响此后新建的 Fusion 会话，不改变已有会话。</p>
      <Field label="主模型" hint="任务分析、分配和最终验收由同一主代理完成；会话中只能明确确认更换模型。">
        <Select value={name(s.fusionMain)} items={items([s.fusionMain], (id) => set("fusionMain", id))} />
      </Field>
      <Field label="工作模型池" hint="允许主代理调用的模型；再次选择可取消。专家任务目前串行执行。">
        <Select value={workers} items={items(s.fusionWorkers, (id) => set("fusionWorkers", s.fusionWorkers.includes(id) ? s.fusionWorkers.filter((item) => item !== id) : [...s.fusionWorkers, id]))} />
      </Field>
      <Field label="轻量模型" hint="供简单任务使用，不会为了保活定时调用。">
        <Select value={name(s.fusionLight)} items={items([s.fusionLight], (id) => set("fusionLight", id))} />
      </Field>
      <Field label="专家调用次数" hint="整个 Fusion 会话的累计上限。">
        <BudgetInput label="专家调用次数" value={s.fusionMaxCalls} max={1000} onChange={(v) => set("fusionMaxCalls", v)} />
      </Field>
      <Field label="Token 预算" hint="累计主代理和专家的已报告用量；下一次请求按估算预留。单次实际用量可能超过估算，未知用量会暂停。">
        <BudgetInput label="Token 预算" value={s.fusionMaxTokens} max={Number.MAX_SAFE_INTEGER} onChange={(v) => set("fusionMaxTokens", v)} />
      </Field>
      <Field label="单个专家最长运行秒数" hint="超时会停止并保留记录；未确认的任务不会自动重做。">
        <BudgetInput label="单个专家最长运行秒数" value={s.fusionMaxSeconds} max={86400} onChange={(v) => set("fusionMaxSeconds", v)} />
      </Field>
      <p className="page-note">专家继承会话权限，必要时仍会向你询问。权限不是操作系统沙箱；本版本使用 WittyCode 自有模型适配，不依赖外部 CLI。</p>
    </>
  );
}
