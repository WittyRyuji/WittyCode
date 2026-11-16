import { useState } from "react";
import type { Session } from "../types";
import { useStore } from "../store";
import { Markdown } from "./blocks/Markdown";

/** A plan is a specific version: neither editing the draft nor changing modes approves it. */
export function PlanApproval({ session, onRevise }: { session: Session; onRevise: () => void }) {
  const approve = useStore((s) => s.approvePlan);
  const [busy, setBusy] = useState(false);
  const plan = session.pendingPlan;
  if (!plan) return null;
  const hasDraft = !!session.draft.trim() || session.attachments.length > 0;
  const disabled = busy || session.mark !== "ready" || hasDraft;
  return (
    <section className="prompt plan-approval" aria-label="等待批准的计划">
      <div className="phead">计划已就绪，等待你的决定</div>
      {plan.warning && <div className="plan-approval-warning" role="alert">{plan.warning}</div>}
      <div className="plan-approval-body"><Markdown source={plan.text} /></div>
      <div className="pbtns">
        <button type="button" className="btn primary" disabled={disabled} title={hasDraft ? "请先发送或清空输入框里的内容和附件" : undefined} onClick={() => {
          setBusy(true);
          void approve(session.id, plan.id).finally(() => setBusy(false));
        }}>执行方案</button>
        <button type="button" className="btn" disabled={busy} onClick={onRevise}>继续修改</button>
      </div>
    </section>
  );
}
