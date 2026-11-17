import { useEffect, useState } from "react";
import { LIVE } from "../mode";
import { liveCall } from "../liveBridge";
import { messageOf } from "../lib/errors";
import { useStore } from "../store";
import type { Language, RulesDocument } from "../protocol";
import { Field, Seg } from "./formkit";

export function CustomizationPage() {
  const s = useStore();
  const [project, setProject] = useState("");
  const [document, setDocument] = useState<RulesDocument | null>(null);
  const [text, setText] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [result, setResult] = useState("");
  const dirty = document !== null && text !== document.text;
  useEffect(() => { useStore.getState().setSettingsDirty(dirty); return () => useStore.getState().setSettingsDirty(false); }, [dirty]);
  const load = async (scope: string) => {
    setBusy(true); setError(""); setResult(""); setDocument(null);
    try {
      const value = LIVE ? await liveCall("customization.read", { projectId: scope }) : { path: `${scope || "C:\\Users\\me\\AppData\\Roaming\\WittyCode\\customization"}\\AGENTS.md`, text: "", revision: "demo", exists: false };
      setDocument(value); setText(value.text);
    } catch (e) { setError(messageOf(e)); }
    finally { setBusy(false); }
  };
  useEffect(() => { void load(project); }, [project]);
  const save = async () => {
    if (!document) return;
    useStore.getState().setSettingsSaving(true);
    setBusy(true); setError(""); setResult("");
    try {
      const value = LIVE ? await liveCall("customization.save", { projectId: project, text, revision: document.revision }) : { ...document, text, exists: true };
      setDocument(value); setText(value.text); setResult("已保存，下一次发送消息时使用。");
    } catch (e) { setError(messageOf(e)); }
    finally { useStore.getState().setSettingsSaving(false); setBusy(false); }
  };
  const changeScope = (value: string) => {
    if (!dirty) return setProject(value);
    s.openDialog({ title: "放弃未保存的规则？", body: "当前编辑尚未保存。", confirmLabel: "放弃并切换", danger: true, onConfirm: () => setProject(value) });
  };
  const options = [{ id: "auto" as const, label: "跟随提问" }, { id: "zh-CN" as const, label: "简体中文" }, { id: "en" as const, label: "English" }];
  return <div className="customization-page">
    <Field label="回复语言"><Seg<Language> value={s.answerLanguage} options={options} onChange={(v) => s.setLanguage("answerLanguage", v)} /></Field>
    <Field label="思考与进度语言" hint="模型或服务端不一定完全遵循语言偏好。"><Seg<Language> value={s.thinkingLanguage} options={options} onChange={(v) => s.setLanguage("thinkingLanguage", v)} /></Field>
    <h3 className="settings-section-title">AGENTS.md</h3>
    <div className="connection-toolbar"><label>作用范围 <select className="selectbox" aria-label="规则作用范围" value={project} disabled={busy} onChange={(e) => changeScope(e.target.value)}><option value="">WittyCode 全局</option>{s.projects.map((p) => <option value={p.path} key={p.id}>{p.name}</option>)}</select></label><button type="button" className="btn small" disabled={busy} onClick={() => dirty ? s.openDialog({ title: "重新加载规则？", body: "将放弃当前未保存的更改。", confirmLabel: "放弃并重新加载", danger: true, onConfirm: () => void load(project) }) : void load(project)}>重新加载</button></div>
    <p className="page-note">项目规则优先于全局规则；只修改上面选定的文件。上限 32 KiB。</p>
    {document && <><div className="pathbox selectable">{document.path}</div><textarea className="rules-editor selectable" aria-label="AGENTS.md 内容" value={text} disabled={busy} onChange={(e) => setText(e.target.value)} spellCheck={false} /><div className="row-btns"><button type="button" className="btn primary" disabled={busy || !dirty} onClick={() => void save()}>保存规则</button>{dirty && <span className="field-hint">有未保存的更改</span>}</div></>}
    {error && <p role="alert" className="connection-error">{error}</p>}{result && <p role="status" className="connection-result">{result}</p>}
  </div>;
}
