import { useState } from "react";
import { AlertTriangle, HelpCircle } from "lucide-react";
import type { PermissionPrompt, QuestionPrompt } from "../../types";
import { useStore } from "../../store";
import { engineLabel, engineOfSession, findModel } from "../../lib/models";
import { CodeBlock } from "./Markdown";
import { Diff } from "./Diff";

const IMPACT: Record<string, string> = { low: "低", medium: "中", high: "高" };
const WHO = { droid: "Droid", codex: "Codex", devin: "Devin", chatgpt: "ChatGPT Plan" } as const;

export function PermissionCard({ sessionId, prompt }: { sessionId: string; prompt: PermissionPrompt }) {
  const answer = useStore((s) => s.answerPermission);
  return (
    <section className="prompt perm" role="alertdialog" aria-label="需要你的确认">
      <div className="phead">
        <AlertTriangle size={16} className="picon" />
        需要你的确认
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
      </div>
    </section>
  );
}

export function QuestionCard({ sessionId, prompt }: { sessionId: string; prompt: QuestionPrompt }) {
  const answer = useStore((s) => s.answerQuestion);
  const who = useStore((s) => {
    const session = s.sessions[sessionId];
    if (!session) return "Droid";
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
    // A multi-select answer goes as the list of choices (Droid joins them its own way).
    answer(sessionId, prompt.id, prompt.questions.map((q, i) => (q.multiSelect ? value(i) : value(i).join("、"))));
  };

  return (
    <section className="prompt ask" aria-label={`${who} 的提问`}>
      <div className="phead">
        <HelpCircle size={16} className="picon" />
        {who} 想先问你几个问题
      </div>
      {prompt.questions.map((q, i) => (
        <div key={i} className={`q${tried && missing(i) ? " missing" : ""}`}>
          <span className="qtopic">{q.topic}</span>
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
          <label className="opt other">
            <span className="otherlabel">其他：</span>
            <input type="text" className="otherinput selectable" placeholder="自己写一个答案" value={other[i] ?? ""} onChange={(e) => setOther((o) => ({ ...o, [i]: e.target.value }))} />
          </label>
          {tried && missing(i) && <div className="qerr">请回答这个问题。</div>}
        </div>
      ))}
      <div className="pbtns">
        <button type="button" className="btn primary" onClick={submit}>
          提交回答
        </button>
        <button type="button" className="btn" onClick={() => answer(sessionId, prompt.id, null)}>
          跳过
        </button>
      </div>
    </section>
  );
}
