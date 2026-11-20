import type { AgentInfo } from "../types";
import type { AgentIdentityGroup } from "./agentTeam";

export const FLOW_MAIN = "team-main";
export const FLOW_NODE_WIDTH = 100;
export const FLOW_PORTRAIT_SIZE = 50;
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
export type FlowRoute = {
  path: string;
  spur: boolean;
  trunk?: string;
  length: number;
  endX: number;
  endY: number;
  signal?: { path: string; prefix: number; epoch: string };
};

function routeEdges(edges: BranchEdge[], placements: Placement[]) {
  const byId = new Map(placements.map((node) => [node.id, node]));
  const bySource = new Map<string, BranchEdge[]>();
  for (const edge of edges) {
    const siblings = bySource.get(edge.source);
    if (siblings) siblings.push(edge);
    else bySource.set(edge.source, [edge]);
  }
  const inset = (FLOW_NODE_WIDTH - FLOW_PORTRAIT_SIZE) / 2;
  const centerY = (node: Placement) => node.y + FLOW_PORTRAIT_SIZE / 2;
  const routes = new Map<string, FlowRoute>();
  for (const [id, siblings] of bySource) {
    const source = byId.get(id)!;
    const startX = source.x + FLOW_NODE_WIDTH - inset;
    const endX = byId.get(siblings[0].target)!.x + inset; // All destinations are in the next column.
    const axis = (startX + endX) / 2;
    const midY = centerY(source);
    const aims = siblings.map((edge) => ({ edge, endY: centerY(byId.get(edge.target)!) }));
    for (const side of [-1, 0, 1]) {
      const taps = aims.filter(({ endY }) => Math.sign(endY - midY) === side)
        .sort((a, b) => side * (a.endY - b.endY));
      if (!taps.length) continue;
      // Three ports for the typical up/straight/down fan. A higher fan-out on one
      // side uses a single monotone trunk with a horizontal tap at each target row.
      const startY = midY + side * Math.min(10, Math.abs(taps[0].endY - midY) / 2);
      const start = `M${startX} ${startY}`;
      const toTap = (endY: number) => `${start} L${axis} ${startY} L${axis} ${endY}`;
      const spur = taps.length > 1;
      const live = taps.filter(({ edge }) => edge.status === "working");
      const lead = live.at(-1); // One signal owns the common prefix, up to the last working tap only.
      // Restart this side's CSS clock together if a working path is added or removed.
      const epoch = JSON.stringify(live.map(({ edge }) => edge.id));
      for (const tap of taps) {
        const { edge, endY } = tap;
        const end = ` L${endX} ${endY}`;
        const full = (side ? toTap(endY) : start) + end;
        const branch = `M${axis} ${endY}${end}`;
        routes.set(edge.id, {
          path: spur ? branch : full, spur,
          trunk: spur && tap === taps.at(-1) ? toTap(endY) : undefined,
          length: endX - startX + Math.abs(endY - startY), endX, endY,
          signal: edge.status === "working" ? { path: tap === lead ? full : branch,
            prefix: tap === lead ? 0 : axis - startX + Math.abs(endY - startY), epoch } : undefined,
        });
      }
    }
  }
  return edges.map((edge) => ({ ...edge, route: routes.get(edge.id)! }));
}

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
  return { main, nodes, edges: routeEdges(edges, [main, ...nodes]), bounds };
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
