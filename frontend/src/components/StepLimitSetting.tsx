import { useEffect, useState } from "react";
import { useStore } from "../store";
import { Field } from "./formkit";

/** Do not save each typed digit: an intermediate "2" while typing "200" could stop a running turn. */
export function StepLimitSetting() {
  const saved = useStore((s) => s.maxSteps);
  const setLimit = useStore((s) => s.setMaxSteps);
  const [draft, setDraft] = useState(String(saved));
  useEffect(() => setDraft(String(saved)), [saved]);
  const value = Number(draft);
  const valid = Number.isSafeInteger(value) && value >= 1;
  const save = () => { if (valid && value !== saved) setLimit(value); };
  return (
    <Field label="单轮步数上限" hint="限制单轮连续执行的模型决策次数，不是工具调用次数。默认 80，可填写更大的正整数；保存后下一步生效，不打断正在运行的工具。适用于 ChatGPT Plan 和 API Provider，Devin 由外部引擎控制。">
      <div className="step-limit-setting">
        <input className="textin num" type="number" min={1} max={Number.MAX_SAFE_INTEGER} step={1} aria-label="单轮步数上限" aria-invalid={!valid} value={draft}
          onChange={(e) => setDraft(e.target.value)} onKeyDown={(e) => { if (e.key === "Enter") { e.preventDefault(); save(); } }} />
        <button type="button" className="btn" disabled={!valid || value === saved} onClick={save}>保存</button>
      </div>
      {!valid && <div className="field-hint err" role="alert">请填写大于或等于 1 的整数。</div>}
    </Field>
  );
}
