import { memo, useEffect, useMemo, useRef, useState, type RefObject } from "react";
import { Background, BackgroundVariant, getSmoothStepPath, getViewportForBounds, Handle, Position, ReactFlow, useReactFlow, type Edge, type EdgeProps, type Node, type NodeProps, type Viewport } from "@xyflow/react";
import "@xyflow/react/dist/base.css";
import type { AgentInfo, Session } from "../types";
import { useStore } from "../store";
import { AGENT_STATE, groupAgentIdentities } from "../lib/agentTeam";
import { FLOW_NODE_WIDTH, buildAgentFlowTree, flowBranchBounds, keepExpandedGroups } from "../lib/agentFlowTree";
import { dragonNestArt } from "../lib/dragonNestArt";
import { dragonIdentity, dragonPalette, type DragonIdentity, type DragonStatus } from "../lib/dragonSprites";
import { DragonSprite } from "./DragonSprite";

type DragonNode = Node<{ identity: DragonIdentity; paletteKey: string; status: DragonStatus; name: string; caption?: string; open?: () => void }, "dragon">;
type NestNode = Node<{ identity: string; count: number; status: DragonStatus; expanded: boolean; toggle: () => void }, "nest">;
type CurrentEdge = Edge<{ status: DragonStatus; step: number }, "current">;
const FIT = { padding: 0.085, maxZoom: 1 };
const MIN_ZOOM = 0.08; // Keep wheel zoom and the explicit overview unrestricted by the readable scale.
const READABLE_FIT = 0.8;
const BRANCH_ZOOM = 0.85;
const PAN_BUTTONS = [1];
const PRO_OPTIONS = { hideAttribution: true };

function FlowDragon({ data }: NodeProps<DragonNode>) {
  const { identity, paletteKey, status, name, caption, open } = data;
  const portrait = <>
    <span className="agent-flow-ring"><DragonSprite identity={identity} palette={dragonPalette(identity, paletteKey)} status={status} scale={3} /></span>
    <span className="agent-flow-name">{name}</span>
    {caption && <span className="agent-flow-model" title={caption}>{caption}</span>}
  </>;
  return <div className={`agent-flow-node ${status}`}>
    {open ? <button type="button" className="agent-flow-character" onClick={open} aria-label={`查看专家 ${name} · ${AGENT_STATE[status].text}`} title={name}>{portrait}</button>
      : <div className="agent-flow-character">{portrait}</div>}
    <Handle type={identity === "main" ? "source" : "target"} position={identity === "main" ? Position.Right : Position.Left} isConnectable={false} />
  </div>;
}

function FlowNest({ data }: NodeProps<NestNode>) {
  const { identity, count, status, expanded, toggle } = data;
  const { gif, png } = dragonNestArt(identity);
  const action = expanded ? "收起" : "展开";
  const label = `${action} ${identity} · ${count} 位成员 · ${AGENT_STATE[status].text}`;
  return <div className={`agent-flow-node ${status}`}>
    <button type="button" className="agent-flow-character" onClick={toggle} aria-expanded={expanded} aria-label={label} title={label}>
      <span className="agent-flow-ring"><picture className="agent-flow-nest-art">
        <source media="(prefers-reduced-motion: reduce)" srcSet={png} />
        <img src={gif} width={48} height={48} alt="" draggable={false} />
      </picture></span>
      <span className="agent-flow-name">{identity}</span>
      <span className="agent-flow-model">{count} 位 · {action} {expanded ? "▾" : "▸"}</span>
    </button>
    <Handle type="target" position={Position.Left} isConnectable={false} />
    <Handle type="source" position={Position.Right} isConnectable={false} />
  </div>;
}

function FlowCurrent(props: EdgeProps<CurrentEdge>) {
  const [path] = getSmoothStepPath({ ...props, borderRadius: 0, offset: 10, stepPosition: props.data?.step });
  const status = props.data?.status ?? "pending";
  return <g className={`agent-flow-edge ${status}`}>
    <path d={path} className="agent-flow-wire" />
    {status === "working" && <>
      <path d={path} className="agent-flow-signal" />
      {/* The round dashes travel on the wire itself; there is no separate dot at an SVG origin. */}
      <path d={path} pathLength={100} className="agent-flow-particle" />
    </>}
  </g>;
}

const nodeTypes = { dragon: FlowDragon, nest: FlowNest };
const edgeTypes = { current: FlowCurrent };

type Bounds = ReturnType<typeof buildAgentFlowTree>["bounds"];
function RefitFlow({ chart, bounds: { x, y, width, height }, branch }: { chart: RefObject<HTMLDivElement | null>; bounds: Bounds; branch: Bounds | null }) {
  const { getViewport, getNodesBounds, setViewport } = useReactFlow();
  const [overview, setOverview] = useState<Viewport | null>(null);
  const { x: bx, y: by, width: bw, height: bh } = branch ?? {};
  useEffect(() => {
    const panel = chart.current;
    if (!panel) return;
    let frame = 0;
    const refit = () => {
      cancelAnimationFrame(frame);
      frame = requestAnimationFrame(() => {
        const { clientWidth: w, clientHeight: h } = panel;
        if (!w || !h) return;
        const full = getViewportForBounds({ x, y, width, height }, w, h, MIN_ZOOM, FIT.maxZoom, FIT.padding);
        if (full.zoom < READABLE_FIT && bx !== undefined && by !== undefined && bw !== undefined && bh !== undefined) {
          const focus = getViewportForBounds({ x: bx, y: by, width: bw, height: bh }, w, h, BRANCH_ZOOM, BRANCH_ZOOM, 0);
          void setViewport(focus);
          setOverview(full);
        } else {
          void setViewport(full);
          setOverview(null);
        }
      });
    };
    const observer = new ResizeObserver(refit);
    observer.observe(panel);
    refit();
    return () => { observer.disconnect(); cancelAnimationFrame(frame); };
  }, [chart, x, y, width, height, bx, by, bw, bh, setViewport]);

  useEffect(() => {
    const panel = chart.current;
    if (!panel) return;
    const reveal = ({ target }: FocusEvent) => {
      if (!(target instanceof HTMLButtonElement) || !target.matches(".agent-flow-character:focus-visible")) return;
      const id = target.closest<HTMLElement>(".react-flow__node")?.dataset.id;
      if (!id) return;
      // Use flow coordinates, not the browser's scroll-to-focus position inside an overflow container.
      const node = getNodesBounds([id]);
      const { x, y, zoom } = getViewport();
      const { clientWidth: w, clientHeight: h } = panel;
      const left = x + node.x * zoom, top = y + node.y * zoom;
      if (left >= 0 && top >= 0 && left + node.width * zoom <= w && top + node.height * zoom <= h) return;
      void setViewport(getViewportForBounds(node, w, h, zoom, zoom, 0));
    };
    panel.addEventListener("focusin", reveal);
    return () => panel.removeEventListener("focusin", reveal);
  }, [chart, getNodesBounds, getViewport, setViewport]);

  return overview && <button type="button" className="agent-flow-overview" title="缩小以查看完整脑图" onClick={() => void setViewport(overview)}>全图</button>;
}

type FlowProps = { sessionId: string; modelId: string; mark: Session["mark"]; agents: AgentInfo[]; onOpen: (id: string) => void };

export const AgentFlow = memo(function AgentFlow(props: FlowProps) {
  return <SessionFlow key={props.sessionId} {...props} />;
});

function SessionFlow({ sessionId, modelId, mark, agents, onOpen }: FlowProps) {
  const chart = useRef<HTMLDivElement>(null);
  const label = useStore((s) => s.models.find((model) => model.id === modelId)?.label) || modelId;
  const groups = useMemo(() => groupAgentIdentities(agents), [agents]);
  const [expanded, setExpanded] = useState<ReadonlySet<string>>(() => new Set());
  const [lastToggled, setLastToggled] = useState<string | null>(null);
  useEffect(() => setExpanded((current) => keepExpandedGroups(groups, current)), [groups]);
  const tree = useMemo(() => buildAgentFlowTree(groups, expanded), [groups, expanded]);
  const branch = useMemo(() => flowBranchBounds(tree, lastToggled), [tree, lastToggled]);
  const toggle = (id: string) => {
    setLastToggled(id);
    setExpanded((current) => {
      const next = new Set(current);
      if (!next.delete(id)) next.add(id);
      return next;
    });
  };
  const mainStatus = mark === "attention" ? "waiting" : mark === "running" ? "working" : mark === "dead" ? "failed" : "pending";
  const nodes: (DragonNode | NestNode)[] = [{ id: tree.main.id, type: "dragon", width: FLOW_NODE_WIDTH, height: tree.main.height,
    position: { x: tree.main.x, y: tree.main.y },
    data: { identity: "main", paletteKey: sessionId, status: mainStatus, name: "主代理", caption: label } }];
  for (const node of tree.nodes) {
    const place = { id: node.id, width: FLOW_NODE_WIDTH, height: node.height, position: { x: node.x, y: node.y } };
    if (node.kind === "nest") {
      nodes.push({ ...place, type: "nest", data: { identity: node.group.identity, count: node.group.members.length, status: node.group.status,
        expanded: node.expanded, toggle: () => toggle(node.id) } });
    } else {
      const { agent, caption } = node;
      nodes.push({ ...place, type: "dragon", data: { identity: dragonIdentity(agent.fusionRole), paletteKey: agent.id, status: agent.status,
        name: agent.name, caption, open: () => onOpen(agent.id) } });
    }
  }
  const edges: CurrentEdge[] = tree.edges.map(({ status, step, ...edge }) => ({ ...edge, type: "current", data: { status, step } }));
  return <div ref={chart} className="agent-flow" role="region" aria-label="专家流程图" title="滚轮缩放，按住鼠标中键拖动">
    <ReactFlow nodes={nodes} edges={edges} nodeTypes={nodeTypes} edgeTypes={edgeTypes} proOptions={PRO_OPTIONS}
      nodesDraggable={false} nodesConnectable={false} elementsSelectable={false} nodesFocusable={false} edgesFocusable={false}
      disableKeyboardA11y deleteKeyCode={null} zoomOnScroll preventScrolling zoomOnDoubleClick={false} panOnDrag={PAN_BUTTONS}
      minZoom={MIN_ZOOM} maxZoom={2.5} fitView fitViewOptions={FIT}>
      <Background variant={BackgroundVariant.Dots} gap={18} size={1} color="var(--flow-grid)" />
      <RefitFlow chart={chart} bounds={tree.bounds} branch={branch} />
    </ReactFlow>
  </div>;
}
