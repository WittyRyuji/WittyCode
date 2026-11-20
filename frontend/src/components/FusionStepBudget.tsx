import { useEffect, useState, type KeyboardEvent } from "react";

interface Props {
  min: number;
  minAdaptive: boolean;
  max: number;
  maxAdaptive: boolean;
  onSave: (min: number, minAdaptive: boolean, max: number, maxAdaptive: boolean) => void;
}

function SideRow({ label, adaptive, value, onAdaptive, onValue }:
  { label: string; adaptive: boolean; value: string; onAdaptive: (on: boolean) => void; onValue: (text: string) => void }) {
  const typed = Number(value);
  const invalid = !adaptive && (!Number.isSafeInteger(typed) || typed < 1 || typed > 1000);
  return (
    <>
      <span className="step-label">{label}</span>
      <label className="step-adaptive">
        <input type="checkbox" checked={adaptive} aria-label={`${label} Adaptive`}
          onChange={(e) => onAdaptive(e.target.checked)} />
        Adaptive
      </label>
      <input className="textin num" type="number" min={1} max={1000} step={1} aria-label={label}
        disabled={adaptive} aria-invalid={invalid} value={value} onChange={(e) => onValue(e.target.value)} />
    </>
  );
}

/** Each side is adaptive or a typed number; the group is sent as one pair and only manual sides can invert it. */
export function FusionStepBudget({ min, minAdaptive, max, maxAdaptive, onSave }: Props) {
  const [loAd, setLoAd] = useState(minAdaptive);
  const [lo, setLo] = useState(String(min));
  const [hiAd, setHiAd] = useState(maxAdaptive);
  const [hi, setHi] = useState(String(max));
  useEffect(() => { setLoAd(minAdaptive); setLo(String(min)); setHiAd(maxAdaptive); setHi(String(max)); },
    [min, minAdaptive, max, maxAdaptive]);
  const low = Number(lo), high = Number(hi);
  const loBad = !loAd && (!Number.isSafeInteger(low) || low < 1 || low > 1000);
  const hiBad = !hiAd && (!Number.isSafeInteger(high) || high < 1 || high > 1000);
  const inverted = !loAd && !hiAd && low > high;
  const valid = !loBad && !hiBad && !inverted;
  // A typed number is the remembered manual value even while its side stays adaptive.
  const asInt = (text: string, fallback: number) => { const n = Number(text); return Number.isSafeInteger(n) && n >= 1 && n <= 1000 ? n : fallback; };
  const sendLo = asInt(lo, min), sendHi = asInt(hi, max);
  const dirty = loAd !== minAdaptive || hiAd !== maxAdaptive || sendLo !== min || sendHi !== max;
  const save = () => { if (valid && dirty) onSave(sendLo, loAd, sendHi, hiAd); };
  const key = (e: KeyboardEvent) => { if (e.key === "Enter") { e.preventDefault(); save(); } };
  return (
    <div className="fusion-step-budget" onKeyDown={key}>
      <SideRow label="委派最小步数" adaptive={loAd} value={lo} onAdaptive={setLoAd} onValue={setLo} />
      <SideRow label="委派最大步数" adaptive={hiAd} value={hi} onAdaptive={setHiAd} onValue={setHi} />
      <button type="button" className="btn step-save" disabled={!valid || !dirty} onClick={save}>保存</button>
      {(loBad || hiBad) && <div className="field-hint err" role="alert">手填值需要 1 到 1000 的整数。</div>}
      {inverted && <div className="field-hint err" role="alert">最小值不能大于最大值。</div>}
    </div>
  );
}
