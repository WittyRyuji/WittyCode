import type { AgentInfo, SessionMark } from "../types";

/** The words for a sub-agent's state, and its conversation status mark. */
export const AGENT_STATE: Record<AgentInfo["status"], { text: string; mark: SessionMark }> = {
  pending: { text: "待分配", mark: "ready" },
  working: { text: "运行中", mark: "running" },
  waiting: { text: "等你确认", mark: "attention" },
  done: { text: "已完成", mark: "ready" },
  stopped: { text: "已停止", mark: "ready" },
  failed: { text: "失败", mark: "dead" },
};

/** Live work stays above the collapsible archive; newest activity comes first within each group. */
export function groupAgents(agents: AgentInfo[]) {
  const active: AgentInfo[] = [];
  const history: AgentInfo[] = [];
  for (const agent of agents) {
    (["pending", "working", "waiting"].includes(agent.status) ? active : history).push(agent);
  }
  active.sort((a, b) => b.startedAt - a.startedAt);
  history.sort((a, b) => (b.finishedAt ?? b.startedAt) - (a.finishedAt ?? a.startedAt));
  return { active, history };
}
