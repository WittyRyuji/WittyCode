import { useState } from "react";
import { AlertTriangle, HelpCircle } from "lucide-react";
import type { PermissionPrompt, QuestionPrompt } from "../../types";
import { useStore } from "../../store";
import { engineLabel, engineOfSession, findModel } from "../../lib/models";
import { CodeBlock } from "./Markdown";
import { Diff } from "./Diff";

const IMPACT: Record<string, string> = { low: "低", medium: "中", high: "高" };
const WHO = { devin: "Devin", chatgpt: "ChatGPT Plan", factory: "Factory" } as const;

export function PermissionCard({ sessionId, prompt }: { sessionId: string; prompt: PermissionPrompt }) {
  const answer = useStore((s) => s.answerPermission);
  const stop = useStore((s) => s.stop);
  const recovery = prompt.id.startsWith("fusion-tool-");
  return (
    <section className="prompt perm" role="alertdialog" aria-label="需要你的确认">
      <div className="phead">
        <AlertTriangle size={16} className="picon" />
        {recovery ? "Fusion 续作需要你授权" : "需要你的确认"}
      </div>
      {prompt.actions.map((a, i) => (
        <div key={i} className="action">
          <div className="atitle">{a.title}</div>
          {a.command && <CodeBlock code={a.command} language="powershell" />}
          {a.path && !a.diffs?.length && <div className="apath selectable">{a.path}</div>}
          {a.diffs?.map((d, j) => <Diff key={j} diff={d} />)}
          {(a.impact || a.reason) && (
            <div className="ameta">
              {a.impact && <>影响等级：<b className={`impact impact-${a.impact}`}>{IMPACT[a.impact]}</b></>}
              {a.impact && a.reason && " · "}
              {a.reason}
            </div>
          )}
        </div>
      ))}
      {/* the choices are answers, like a question's options: one per line, so a long one never squeezes the others */}
      <div className="pbtns choices" role="group" aria-label="选择一个答复">
        {prompt.options.map((o) => (
          <button key={o.value} type="button" className={`btn${o.tone === "primary" ? " primary" : o.tone === "danger" ? " danger" : ""}`} onClick={() => answer(sessionId, prompt.id, o.value)}>
            {o.label}
          </button>
        ))}
        {recovery && <button type="button" className="btn" onClick={() => stop(sessionId)}>取消原任务</button>}
      </div>
    </section>
  );
}

export function QuestionCard({ sessionId, prompt }: { sessionId: string; prompt: QuestionPrompt }) {
  const answer = useStore((s) => s.answerQuestion);
  const stop = useStore((s) => s.stop);
  const who = useStore((s) => {
    if (prompt.who) return prompt.who;
    const session = s.sessions[sessionId];
    if (!session) return "模型";
    const model = findModel(s.models, session.settings.model);
    const label = model ? engineLabel(model.source) : WHO[engineOfSession(s.models, session)];
    return label === "API" ? "API 模型" : label;
  });
  const [picked, setPicked] = useState<Record<number, string[]>>({});
  const [other, setOther] = useState<Record<number, string>>({});
  const [tried, setTried] = useState(false);

  const value = (i: number) => [...(picked[i] ?? []), ...(other[i]?.trim() ? [other[i]!.trim()] : [])];
  const missing = (i: number) => value(i).length === 0;
  const toggle = (i: number, opt: string, multi: boolean) =>
    setPicked((p) => {
      const cur = p[i] ?? [];
      if (!multi) return { ...p, [i]: [opt] };
      return { ...p, [i]: cur.includes(opt) ? cur.filter((x) => x !== opt) : [...cur, opt] };
    });

  const submit = () => {
    setTried(true);
    if (prompt.questions.some((_, i) => missing(i))) return;
    // A multi-select answer goes as the list of choices (the engine joins them its own way).
    answer(sessionId, prompt.id, prompt.questions.map((q, i) => (q.multiSelect ? value(i) : value(i).join("、"))));
  };

  // A plain Enter in the own-answer box is the 提交回答 button. An Enter that confirms an IME composition (isComposing,
  // or the keyCode 229 Chromium gives keys it handed to the input method), an auto-repeat, or one with modifiers is not.
  const otherKey = (e: React.KeyboardEvent<HTMLInputElement>) => {
    if (e.key !== "Enter" || e.repeat || e.ctrlKey || e.metaKey || e.altKey || e.shiftKey || e.nativeEvent.isComposing || e.nativeEvent.keyCode === 229) return;
    e.preventDefault();
    e.stopPropagation();
    submit();
  };

  return (
    <section className="prompt ask" aria-label={`${who} 的提问`}>
      <div className="phead">
        <HelpCircle size={16} className="picon" />
        {prompt.optionsOnly ? `${who} 续作需要你确认` : `${who} 想先问你几个问题`}
      </div>
      {prompt.questions.map((q, i) => (
        <div key={i} className={`q${tried && missing(i) ? " missing" : ""}`}>
          {q.topic && q.topic !== who && q.topic !== q.question && <span className="qtopic">{q.topic}</span>}
          <div className="qtext">
            {q.question} {q.multiSelect && <span className="qhint">（可多选）</span>}
          </div>
          {q.options.map((opt) => {
            const on = (picked[i] ?? []).includes(opt);
            return (
              <label key={opt} className={`opt${on ? " on" : ""}`}>
                <input type={q.multiSelect ? "checkbox" : "radio"} name={`${prompt.id}-${i}`} checked={on} onChange={() => toggle(i, opt, q.multiSelect)} />
                <span>{opt}</span>
              </label>
            );
          })}
          {!prompt.optionsOnly && (
            <label className="opt other">
              <span className="otherlabel">其他：</span>
              <input type="text" className="otherinput selectable" placeholder="自己写一个答案" value={other[i] ?? ""} onChange={(e) => setOther((o) => ({ ...o, [i]: e.target.value }))} onKeyDown={otherKey} />
            </label>
          )}
          {tried && missing(i) && <div className="qerr">请回答这个问题。</div>}
        </div>
      ))}
      <div className="pbtns">
        <button type="button" className="btn primary" onClick={submit}>
          提交回答
        </button>
        {prompt.optionsOnly ? (
          <button type="button" className="btn" onClick={() => stop(sessionId)}>取消原任务</button>
        ) : (
          <button type="button" className="btn" onClick={() => answer(sessionId, prompt.id, null)}>跳过</button>
        )}
      </div>
    </section>
  );
}
