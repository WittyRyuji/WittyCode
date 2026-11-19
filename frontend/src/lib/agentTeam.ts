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

const GROUP_STATE_ORDER: AgentInfo["status"][] = ["waiting", "working", "pending", "failed", "stopped", "done"];
const byText = (a: string, b: string) => a < b ? -1 : a > b ? 1 : 0;

/** Model pools and workstream names do not identify an expert's assignment. */
export function groupAgentIdentities(agents: readonly AgentInfo[]) {
  const groups = new Map<string, AgentInfo[]>();
  for (const agent of agents) {
    const identity = agent.fusionIdentity?.trim() || agent.role?.trim() || "Sub-Agent";
    const members = groups.get(identity);
    if (members) members.push(agent);
    else groups.set(identity, [agent]);
  }
  return [...groups].sort(([a], [b]) => byText(a, b)).map(([identity, members]) => ({
    id: `group:${identity}`, identity,
    members: members.sort((a, b) => byText(a.id, b.id)),
    // Prefer user attention, then activity; a failure remains visible once the group is idle.
    status: GROUP_STATE_ORDER.find((status) => members.some((agent) => agent.status === status)) ?? "pending",
  }));
}

export type AgentIdentityGroup = ReturnType<typeof groupAgentIdentities>[number];
