import type { AgentInfo } from "../types";
import type { AgentIdentityGroup } from "./agentTeam";

export const FLOW_MAIN = "team-main";
export const FLOW_NODE_WIDTH = 100;
const PORTRAIT_ROW = 68;
const CAPTION_ROW = 84;
const COLUMN = 120;
const MEMBER_STEP = 74;
const BRANCH_STEP = 92;

type Placement = { id: string; x: number; y: number; height: number };
type BranchNode = Placement & (
  | { kind: "nest"; group: AgentIdentityGroup; expanded: boolean }
  | { kind: "expert"; agent: AgentInfo; caption?: string }
);
type BranchEdge = { id: string; source: string; target: string; status: AgentInfo["status"] };

/** Forget groups that ceased to exist or became singletons; new nests start closed. */
export function keepExpandedGroups(groups: readonly AgentIdentityGroup[], expanded: ReadonlySet<string>): ReadonlySet<string> {
  const next = new Set(groups.filter((group) => group.members.length > 1 && expanded.has(group.id)).map((group) => group.id));
  return next.size === expanded.size ? expanded : next;
}

export function buildAgentFlowTree(groups: readonly AgentIdentityGroup[], expanded: ReadonlySet<string>) {
  const nodes: BranchNode[] = [];
  const edges: BranchEdge[] = [];
  function connect(source: string, target: string, status: AgentInfo["status"]) {
    edges.push({ id: target, source, target, status });
  }
  function addExpert(agent: AgentInfo, source: string, x: number, y: number, caption?: string) {
    const id = `expert:${agent.id}`;
    nodes.push({ kind: "expert", agent, caption, id, x, y, height: caption ? CAPTION_ROW : PORTRAIT_ROW });
    connect(source, id, agent.status);
  }

  // Each top-level branch owns a vertical band. An expanded nest is centered on its
  // members, which occupy a separate column and never encroach on another band.
  let firstY = 0;
  for (const group of groups) {
    if (group.members.length === 1) {
      addExpert(group.members[0], FLOW_MAIN, COLUMN, firstY, group.identity);
      firstY += BRANCH_STEP;
    } else {
      const open = expanded.has(group.id);
      const span = open ? (group.members.length - 1) * MEMBER_STEP : 0;
      nodes.push({ kind: "nest", group, expanded: open, id: group.id, x: COLUMN, y: firstY + span / 2, height: CAPTION_ROW });
      connect(FLOW_MAIN, group.id, group.status);
      if (open) group.members.forEach((agent, i) => addExpert(agent, group.id, COLUMN * 2, firstY + i * MEMBER_STEP));
      firstY += span + BRANCH_STEP;
    }
  }
  const main: Placement = { id: FLOW_MAIN, x: 0, y: Math.max(0, firstY - BRANCH_STEP) / 2, height: CAPTION_ROW };
  const bounds = { x: 0, y: 0, width: FLOW_NODE_WIDTH, height: main.y + main.height };
  for (const node of nodes) {
    bounds.width = Math.max(bounds.width, node.x + FLOW_NODE_WIDTH);
    bounds.height = Math.max(bounds.height, node.y + node.height);
  }
  const yOf = new Map([main, ...nodes].map((node) => [node.id, node.y]));
  const distance = ({ source, target }: BranchEdge) => Math.abs(yOf.get(source)! - yOf.get(target)!);
  const reach = new Map<string, number>();
  for (const edge of edges) reach.set(edge.source, Math.max(reach.get(edge.source) ?? 0, distance(edge)));
  // Farther branches turn sooner, so siblings share the start of the wire but not a
  // vertical segment. The colour/current of one branch won't cover another branch.
  const routedEdges = edges.map((edge) => ({ ...edge, step: 0.8 - 0.6 * distance(edge) / (reach.get(edge.source) || 1) }));
  return { main, nodes, edges: routedEdges, bounds };
}

/** A readable scene around the last toggled nest, with its nearest rows when open. */
export function flowBranchBounds(tree: ReturnType<typeof buildAgentFlowTree>, preferred: string | null) {
  const nests = tree.nodes.filter((node) => node.kind === "nest");
  const open = nests.find((node) => node.expanded);
  if (!open) return null;
  // Keep a just-closed nest in view too, so it can be reopened without finding it again.
  const nest = nests.find((node) => node.id === preferred) ?? open;
  const children = new Set(tree.edges.filter((edge) => edge.source === nest.id).map((edge) => edge.target));
  const centerY = nest.y + nest.height / 2;
  const distance = (node: Placement) => Math.abs(node.y + node.height / 2 - centerY);
  const near = tree.nodes.filter((node) => children.has(node.id)).sort((a, b) => distance(a) - distance(b)).slice(0, 2);
  const x = nest.x;
  const y = Math.min(nest.y, ...near.map((node) => node.y));
  const right = Math.max(x, ...near.map((node) => node.x)) + FLOW_NODE_WIDTH;
  const bottom = Math.max(nest.y + nest.height, ...near.map((node) => node.y + node.height));
  return { x, y, width: right - x, height: bottom - y };
}
