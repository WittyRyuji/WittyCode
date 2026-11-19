import { memo, useEffect, useRef, type RefObject } from "react";
import { Background, BackgroundVariant, getSmoothStepPath, getViewportForBounds, Handle, Position, ReactFlow, useReactFlow, type Edge, type EdgeProps, type Node, type NodeProps } from "@xyflow/react";
import "@xyflow/react/dist/base.css";
import type { AgentInfo, Session } from "../types";
import { useStore } from "../store";
import { AGENT_STATE } from "../lib/agentTeam";
import { dragonIdentity, dragonPalette, type DragonIdentity, type DragonStatus } from "../lib/dragonSprites";
import { DragonSprite } from "./DragonSprite";

type DragonNode = Node<{ identity: DragonIdentity; paletteKey: string; status: DragonStatus; name: string; model?: string; open?: () => void }, "dragon">;
type CurrentEdge = Edge<{ status: DragonStatus }, "current">;
const MAIN = "team-main";
const ROW = 70;
const NODE_WIDTH = 86;
const NODE_HEIGHT = 68;
const MAIN_HEIGHT = 84;
const FIT = { padding: 0.085, maxZoom: 1 };
const MIN_ZOOM = 0.08;
const PAN_BUTTONS = [1];
const PRO_OPTIONS = { hideAttribution: true };

function FlowDragon({ data }: NodeProps<DragonNode>) {
  const { identity, paletteKey, status, name, model, open } = data;
  const portrait = <>
    <span className="agent-flow-ring"><DragonSprite identity={identity} palette={dragonPalette(identity, paletteKey)} status={status} scale={3} /></span>
    <span className="agent-flow-name">{name}</span>
    {model && <span className="agent-flow-model" title={model}>{model}</span>}
  </>;
  return <div className={`agent-flow-node ${status}`}>
    {open ? <button type="button" className="agent-flow-character" onClick={open} aria-label={`查看专家 ${name} · ${AGENT_STATE[status].text}`} title={name}>{portrait}</button>
      : <div className="agent-flow-character">{portrait}</div>}
    <Handle type={identity === "main" ? "source" : "target"} position={identity === "main" ? Position.Right : Position.Left} isConnectable={false} />
  </div>;
}

function FlowCurrent(props: EdgeProps<CurrentEdge>) {
  const [path] = getSmoothStepPath({ ...props, borderRadius: 0 });
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

const nodeTypes = { dragon: FlowDragon };
const edgeTypes = { current: FlowCurrent };

function RefitFlow({ chart, layoutKey }: { chart: RefObject<HTMLDivElement | null>; layoutKey: string }) {
  const { getNodes, getNodesBounds, setViewport } = useReactFlow();
  useEffect(() => {
    const panel = chart.current;
    if (!panel) return;
    let frame = 0;
    const refit = () => {
      cancelAnimationFrame(frame);
      frame = requestAnimationFrame(() => {
        const bounds = getNodesBounds(getNodes().map((node) => node.id));
        if (!bounds.width || !bounds.height || !panel.clientWidth || !panel.clientHeight) return;
        void setViewport(getViewportForBounds(bounds, panel.clientWidth, panel.clientHeight, MIN_ZOOM, FIT.maxZoom, FIT.padding));
      });
    };
    const observer = new ResizeObserver(refit);
    observer.observe(panel);
    refit();
    return () => { observer.disconnect(); cancelAnimationFrame(frame); };
  }, [chart, layoutKey, getNodes, getNodesBounds, setViewport]);
  return null;
}

export const AgentFlow = memo(function AgentFlow({ sessionId, modelId, mark, agents, onOpen }: {
  sessionId: string; modelId: string; mark: Session["mark"]; agents: AgentInfo[]; onOpen: (id: string) => void;
}) {
  const chart = useRef<HTMLDivElement>(null);
  const label = useStore((s) => s.models.find((model) => model.id === modelId)?.label) || modelId;
  const rows = agents.length > 6 ? Math.ceil(agents.length / 2) : agents.length;
  const mainStatus = mark === "attention" ? "waiting" : mark === "running" ? "working" : mark === "dead" ? "failed" : "pending";
  const nodes: DragonNode[] = [{ id: MAIN, type: "dragon", width: NODE_WIDTH, height: MAIN_HEIGHT, position: { x: 0, y: (rows - 1) * ROW / 2 - 7 },
    data: { identity: "main", paletteKey: sessionId, status: mainStatus, name: "主代理", model: label } }];
  const edges: CurrentEdge[] = [];
  agents.forEach((agent, i) => {
    const nodeId = `expert:${agent.id}`;
    nodes.push({ id: nodeId, type: "dragon", width: NODE_WIDTH, height: NODE_HEIGHT, position: { x: 150 + Math.floor(i / rows) * 94, y: i % rows * ROW },
      data: { identity: dragonIdentity(agent.fusionRole), paletteKey: agent.id, status: agent.status, name: agent.name, open: () => onOpen(agent.id) } });
    edges.push({ id: nodeId, type: "current", source: MAIN, target: nodeId, data: { status: agent.status } });
  });
  return <div ref={chart} className="agent-flow" role="region" aria-label="专家流程图" title="滚轮缩放，按住鼠标中键拖动">
    <ReactFlow nodes={nodes} edges={edges} nodeTypes={nodeTypes} edgeTypes={edgeTypes} proOptions={PRO_OPTIONS}
      nodesDraggable={false} nodesConnectable={false} elementsSelectable={false} nodesFocusable={false} edgesFocusable={false}
      disableKeyboardA11y deleteKeyCode={null} zoomOnScroll preventScrolling zoomOnDoubleClick={false} panOnDrag={PAN_BUTTONS}
      minZoom={MIN_ZOOM} maxZoom={2.5} fitView fitViewOptions={FIT}>
      <Background variant={BackgroundVariant.Dots} gap={18} size={1} color="var(--flow-grid)" />
      <RefitFlow chart={chart} layoutKey={JSON.stringify(agents.map((agent) => agent.id))} />
    </ReactFlow>
  </div>;
});
