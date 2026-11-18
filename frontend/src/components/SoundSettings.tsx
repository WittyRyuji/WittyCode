import { useStore } from "../store";
import type { SoundKey, SoundKind } from "../protocol";
import { Field, Switch } from "./formkit";

const SOUNDS: { kind: SoundKind; key: SoundKey; label: string; hint: string }[] = [
  { kind: "question", key: "soundQuestion", label: "问题", hint: "模型提问或等待权限确认时" },
  { kind: "complete", key: "soundComplete", label: "完成", hint: "本轮最终结果返回时" },
  { kind: "error", key: "soundError", label: "错误", hint: "本轮失败或连接出错时" },
];

export function SoundSettings() {
  const s = useStore();
  return (
    <section className="sound-settings" aria-label="提示音设置">
      <Field label="提示音" hint="问题、完成和错误使用不同声音。关闭后全部静音；下面仍显示当前文件路径。支持本机 PCM WAV 和 MP3 文件。">
        <Switch on={s.soundEnabled} onChange={s.setSoundEnabled} label="提示音总开关" />
      </Field>
      {SOUNDS.map(({ kind, key, label, hint }) => (
        <div className="sound-setting" key={kind}>
          <div className="sound-heading"><label className="field-label" htmlFor={`sound-${kind}`}>{label}</label><span className="field-hint">{hint}</span></div>
          <div className="sound-path-row">
            <input id={`sound-${kind}`} className="textin selectable" aria-label={`${label}提示音路径`} title={s[key]} value={s[key]} readOnly />
            <button type="button" className="btn small" onClick={() => s.pickSound(kind)} aria-label={`选择${label}提示音`}>选择…</button>
            <button type="button" className="btn small" onClick={() => s.setSoundPath(key, "")} aria-label={`恢复默认${label}提示音`}>默认</button>
            <button type="button" className="btn small" disabled={!s.soundEnabled} onClick={() => s.previewSound(kind)} aria-label={`试听${label}提示音`}>试听</button>
          </div>
        </div>
      ))}
    </section>
  );
}
