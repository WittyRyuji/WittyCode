import { useState } from "react";
import { Check, ChevronDown, ChevronUp, Circle, CircleDot, ListTodo, X } from "lucide-react";
import type { PlanView } from "../lib/plan";
import "../styles/plan.css";

const STATE = { pending: "未开始", in_progress: "进行中", completed: "已完成" } as const;

/** A conversation's latest structured plan, beside the transcript rather than repeated in every thinking group. */
export function PlanPanel({ plan, onClose }: { plan: PlanView; onClose: () => void }) {
  const [collapsed, setCollapsed] = useState(false);
  return (
    <aside id="conversation-plan" className={`planpanel${collapsed ? " collapsed" : ""}`} aria-label="当前会话计划">
      <section className="plan-card">
        <header className="plan-header">
          <button type="button" className="plan-head" aria-label={collapsed ? "展开计划步骤" : "收起计划步骤"} aria-expanded={!collapsed} onClick={() => setCollapsed(!collapsed)}>
            <ListTodo size={16} aria-hidden />
            <span className="plan-title">计划</span>
            <span className="plan-count num">{plan.completed}/{plan.steps.length}</span>
            {collapsed ? <ChevronDown size={14} aria-hidden /> : <ChevronUp size={14} aria-hidden />}
          </button>
          <button type="button" className="icon-btn sm plan-close" aria-label="关闭计划栏" title="关闭计划栏" onClick={onClose}>
            <X size={15} aria-hidden />
          </button>
        </header>
        {!collapsed && (
          <div className="plan-body selectable">
            <div className="plan-meter" role="progressbar" aria-label="计划完成进度" aria-valuemin={0} aria-valuemax={plan.steps.length} aria-valuenow={plan.completed}>
              <span style={{ width: `${plan.completed / plan.steps.length * 100}%` }} />
            </div>
            <ol className="plan-steps">
              {plan.steps.map((step, i) => (
                <li key={i} className={`plan-step st-${step.state}`}>
                  <span className="plan-state" aria-label={STATE[step.state]}>
                    {step.state === "completed" ? <Check size={14} /> : step.state === "in_progress" ? <CircleDot size={14} /> : <Circle size={14} />}
                  </span>
                  <span>{step.text}</span>
                </li>
              ))}
            </ol>
          </div>
        )}
      </section>
    </aside>
  );
}
