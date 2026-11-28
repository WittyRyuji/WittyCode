import { useEffect, useState } from "react";
import { DEFAULT_RECONNECT_INTERVAL, DEFAULT_RECONNECT_LIMIT } from "../liveReducer";
import { useStore } from "../store";
import { Field } from "./formkit";

function RetryNumber({ label, hint, saved, min, max, onSave }: {
  label: string; hint: string; saved: number; min: number; max: number; onSave: (n: number) => void;
}) {
  const [draft, setDraft] = useState(String(saved));
  useEffect(() => setDraft(String(saved)), [saved]);
  const value = Number(draft);
  const valid = draft !== "" && Number.isSafeInteger(value) && value >= min && value <= max;
  return (
    <Field label={label} hint={hint}>
      <form className="step-limit-setting" onSubmit={(e) => { e.preventDefault(); if (valid && value !== saved) onSave(value); }}>
        <input className="textin num" type="number" min={min} max={max} step={1} aria-label={label} aria-invalid={!valid}
          value={draft} onChange={(e) => setDraft(e.target.value)} />
        <button className="btn" type="submit" disabled={!valid || value === saved}>保存</button>
      </form>
      {!valid && <div className="field-hint err" role="alert">请填写 {min}–{max} 的整数。</div>}
    </Field>
  );
}

export function ReconnectSettings() {
  const interval = useStore((s) => s.reconnectInterval);
  const limit = useStore((s) => s.reconnectLimit);
  const setInterval = useStore((s) => s.setReconnectInterval);
  const setLimit = useStore((s) => s.setReconnectLimit);
  return (
    <section className="reconnect-settings">
      <h3 className="settings-section-title">重连设置</h3>
      <p className="page-note">仅适用于正文请求遇到 HTTP 502 后的自动重试，保存后从下一轮生效。</p>
      <RetryNumber label="重连间隔" hint={`每次重试前等待的秒数，默认 ${DEFAULT_RECONNECT_INTERVAL} 秒。`}
        saved={interval} min={1} max={3600} onSave={setInterval} />
      <RetryNumber label="重连上限" hint={`不含首次请求，最多 ${DEFAULT_RECONNECT_LIMIT} 次；0 表示不重试。`}
        saved={limit} min={0} max={DEFAULT_RECONNECT_LIMIT} onSave={setLimit} />
    </section>
  );
}
