import type { AgentInfo } from "../types";

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
