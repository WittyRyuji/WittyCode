import { memo, useCallback, useEffect, useId, useMemo, useRef, useState, type CSSProperties, type FocusEvent, type KeyboardEvent, type MouseEvent, type PointerEvent } from "react";
import { Minus, Plus } from "lucide-react";
import type { MemoryEntry } from "../types";
import { formatRelative } from "../lib/time";
import { kindLabel, sourceLine, whoConversation } from "../lib/memory";
import {
  COLOR_KEYS,
  KIND_COLORS,
  LABEL_FONT,
  NOTE_FONT,
  OTHER_KIND_COLOR,
  SOURCE_COLORS,
  arcChars,
  arcPath,
  colorKey,
  colorOfKey,
  labelScale,
  layoutStarMap,
  legendItems,
  neighbourOf,
  noteTitle,
  ringScales,
  seeded,
  snippet,
  stackPositions,
  textWidth,
  type ArcLabel,
  type ColorBy,
  type Direction,
  type FlatLabel,
  type StackLabel,
  type StarMapLayout,
  type StarNode,
  type StarRings,
} from "../lib/starmap";

export interface MemoryStarMapProps {
  /** every note to draw: the active ones, and the forgotten ones when the page shows them */
  entries: MemoryEntry[];
  /** the note shown as chosen: its star is ringed and the branch from it to the root is drawn brighter */
  selectedId: string | null;
  /** a search is on: the notes that match it; the other stars dim */
  matchIds?: ReadonlySet<string> | null;
  /** what the colours tell at first (the chart has its own switch for it) */
  colorBy?: ColorBy;
  /** a note's star was clicked, or Enter / Space was pressed on it */
  onSelect: (id: string) => void;
  /** the panel's height in CSS px; its width is the container's */
  height?: number;
}

const DEFAULT_HEIGHT = 460;
const FIRST_WIDTH = 720;
const PAD_X = 18;
const PAD_TOP = 44;
const PAD_BOTTOM = 16;
const MAX_FIT = 1.25;
const ZOOM_MIN = 0.75;
const ZOOM_MAX = 8;
const ZOOM_STEP = 1.4;
const DRAG_SLOP = 4;
const TIP_WIDTH = 290;
/** the legend's corner: a chart narrower than the panel moves right by up to this much, off it */
const LEGEND_SHIFT = 80;

interface View {
  z: number;
  x: number;
  y: number;
}

const HOME: View = { z: 1, x: 0, y: 0 };

/** The background: faint fixed stars (the same every time) and a few that twinkle, in CSS. */
const SKY = (() => {
  const rnd = seeded(20261107);
  const dots = Array.from({ length: 74 }, () => ({ x: rnd() * 100, y: rnd() * 100, r: 0.35 + rnd() * rnd() * 0.85, o: 0.1 + rnd() * rnd() * 0.42 }));
  const twinkles = Array.from({ length: 9 }, () => ({ x: 2 + rnd() * 96, y: 6 + rnd() * 88, d: 4.5 + rnd() * 5, delay: -rnd() * 9 }));
  return { dots, twinkles };
})();

/** The four-pointed star of the root. */
const SPARKLE = "M0 -10Q1 -1 10 0Q1 1 0 10Q-1 1 -10 0Q-1 -1 0 -10Z";

const round = (n: number, digits = 2) => {
  const f = 10 ** digits;
  return Math.round(n * f) / f;
};

/** The zoom (layout px to screen px) and the point that sits in the middle, for the whole chart to fit the panel. */
function fitOf(layout: StarMapLayout, w: number, h: number) {
  const b = layout.bounds;
  const k = Math.max(0.05, Math.min((w - 2 * PAD_X) / b.width, (h - PAD_TOP - PAD_BOTTOM) / b.height, MAX_FIT));
  const free = layout.nodes.length ? Math.max(0, w - 2 * PAD_X - b.width * k) : 0;
  return { k, cx: b.x + b.width / 2, cy: b.y + b.height / 2, sx: w / 2 + Math.min(LEGEND_SHIFT, free / 2), sy: PAD_TOP + (h - PAD_TOP - PAD_BOTTOM) / 2 };
}

const nodeElement = (target: EventTarget | null) => (target instanceof Element ? target.closest<SVGElement>("[data-id]") : null);

const scopeText = (e: MemoryEntry) => (e.scope === "global" ? "全局" : `项目 · ${e.projectName || "（未命名）"}`);

export function MemoryStarMap({ entries, selectedId, matchIds = null, colorBy: initialColor = "kind", onSelect, height = DEFAULT_HEIGHT }: MemoryStarMapProps) {
  const uid = `sm${useId().replace(/[^A-Za-z0-9_-]/g, "")}`;
  const [colorBy, setColorBy] = useState<ColorBy>(initialColor);
  useEffect(() => setColorBy(initialColor), [initialColor]);
  const [collapsed, setCollapsed] = useState<ReadonlySet<string>>(() => new Set());
  const layout = useMemo(() => layoutStarMap(entries, { collapsed, matchIds, selectedId }), [entries, collapsed, matchIds, selectedId]);
  const byId = useMemo(() => new Map(layout.nodes.map((n) => [n.id, n])), [layout]);

  const panelRef = useRef<HTMLDivElement>(null);
  const svgRef = useRef<SVGSVGElement>(null);
  const [size, setSize] = useState({ w: FIRST_WIDTH, h: height });
  useEffect(() => {
    const el = panelRef.current;
    if (!el) return;
    const measure = () => {
      const w = el.clientWidth || FIRST_WIDTH;
      const h = el.clientHeight || height;
      setSize((s) => (s.w === w && s.h === h ? s : { w, h }));
    };
    measure();
    if (typeof ResizeObserver === "undefined") return;
    const observer = new ResizeObserver(measure);
    observer.observe(el);
    return () => observer.disconnect();
  }, [height]);

  const [view, setView] = useState<View>(HOME);
  const fit = fitOf(layout, size.w, size.h);
  const k = fit.k * view.z;
  const tx = fit.sx + view.x - fit.cx * k;
  const ty = fit.sy + view.y - fit.cy * k;
  /** stars grow more slowly than the distances between them when zoomed in */
  const starScale = round(view.z ** -0.55, 3);
  const live = useRef({ fit, size, layout });
  live.current = { fit, size, layout };

  const [hoverId, setHoverId] = useState<string | null>(null);
  const [focusId, setFocusId] = useState<string | null>(null);
  const [keyboard, setKeyboard] = useState(false);
  const [tabStop, setTabStop] = useState<string | null>(null);
  const [panning, setPanning] = useState(false);
  const drag = useRef<{ pointer: number; x: number; y: number; vx: number; vy: number; target: string | null; moved: boolean } | null>(null);
  const pendingFocus = useRef<string | null>(null);

  const stop =
    (tabStop && byId.has(tabStop) && byId.get(tabStop)!.type !== "root" ? tabStop : null) ??
    layout.nodes.find((n) => n.selected)?.id ??
    layout.nodes.find((n) => n.type === "scope")?.id ??
    null;

  const toggle = useCallback((branch: string) => {
    setCollapsed((current) => {
      const next = new Set(current);
      if (next.has(branch)) next.delete(branch);
      else next.add(branch);
      return next;
    });
  }, []);

  const zoomAround = useCallback((factor: number, px: number, py: number) => {
    setView((v) => {
      const f = live.current.fit;
      const z = Math.min(ZOOM_MAX, Math.max(ZOOM_MIN, v.z * factor));
      if (z === v.z) return v;
      const r = z / v.z;
      // the point under (px, py) stays there
      return { z, x: px - f.sx - (px - f.sx - v.x) * r, y: py - f.sy - (py - f.sy - v.y) * r };
    });
  }, []);
  const zoomBy = (factor: number) => zoomAround(factor, size.w / 2, fit.sy);

  useEffect(() => {
    const el = svgRef.current;
    if (!el) return;
    const onWheel = (e: WheelEvent) => {
      if (live.current.layout.nodes.length === 0) return;
      e.preventDefault();
      const rect = el.getBoundingClientRect();
      const delta = e.deltaMode === 1 ? e.deltaY * 16 : e.deltaMode === 2 ? e.deltaY * 400 : e.deltaY;
      zoomAround(Math.exp(-Math.max(-300, Math.min(300, delta)) * 0.0016), e.clientX - rect.left, e.clientY - rect.top);
    };
    el.addEventListener("wheel", onWheel, { passive: false });
    return () => el.removeEventListener("wheel", onWheel);
  }, [zoomAround]);

  /** Moves the view just enough for a node (reached with the keyboard) to be inside the panel. */
  const reveal = (node: StarNode) => {
    setView((v) => {
      const { fit: f, size: s } = live.current;
      const kk = f.k * v.z;
      const x = f.sx + v.x + (node.x - f.cx) * kk;
      const y = f.sy + v.y + (node.y - f.cy) * kk;
      const m = 36;
      const dx = x < m ? m - x : x > s.w - m ? s.w - m - x : 0;
      const dy = y < m + 24 ? m + 24 - y : y > s.h - m ? s.h - m - y : 0;
      return dx || dy ? { ...v, x: v.x + dx, y: v.y + dy } : v;
    });
  };

  const focusNode = (id: string) => {
    setTabStop(id);
    const el = svgRef.current?.querySelector<SVGElement>(`[data-node="${CSS.escape(id)}"]`);
    el?.focus({ preventScroll: true });
  };

  useEffect(() => {
    const id = pendingFocus.current;
    if (!id) return;
    pendingFocus.current = null;
    if (byId.has(id)) focusNode(id);
  }, [layout]);

  const activate = (node: StarNode) => {
    if (node.type === "note") onSelect(node.id);
    else if (node.type === "scope" || node.type === "kind") toggle(node.id);
    else if (node.type === "cluster" && node.branch) {
      pendingFocus.current = node.branch;
      toggle(node.branch);
    }
  };

  const nodeOf = (target: EventTarget | null) => {
    const id = nodeElement(target)?.getAttribute("data-id");
    return id ? (byId.get(id) ?? null) : null;
  };

  const onPointerDown = (e: PointerEvent<SVGSVGElement>) => {
    if (e.button !== 0) return;
    setKeyboard(false);
    drag.current = { pointer: e.pointerId, x: e.clientX, y: e.clientY, vx: view.x, vy: view.y, target: nodeOf(e.target)?.id ?? null, moved: false };
    e.currentTarget.setPointerCapture?.(e.pointerId);
  };
  const onPointerMove = (e: PointerEvent<SVGSVGElement>) => {
    const d = drag.current;
    if (!d || d.pointer !== e.pointerId) return;
    const dx = e.clientX - d.x;
    const dy = e.clientY - d.y;
    if (!d.moved && Math.hypot(dx, dy) < DRAG_SLOP) return;
    if (!d.moved) {
      d.moved = true;
      setPanning(true);
      setHoverId(null);
    }
    setView((v) => ({ ...v, x: d.vx + dx, y: d.vy + dy }));
  };
  const onPointerUp = (e: PointerEvent<SVGSVGElement>) => {
    const d = drag.current;
    if (!d || d.pointer !== e.pointerId) return;
    drag.current = null;
    setPanning(false);
    if (!d.moved && d.target) {
      const node = byId.get(d.target);
      if (node) activate(node);
    }
  };
  const onPointerCancel = () => {
    drag.current = null;
    setPanning(false);
  };
  // A click that no pointer made (assistive technology "pressing" the button) still activates it.
  const onClick = (e: MouseEvent<SVGSVGElement>) => {
    if (e.detail !== 0) return;
    const node = nodeOf(e.target);
    if (node) activate(node);
  };
  const onPointerOver = (e: PointerEvent<SVGSVGElement>) => {
    if (drag.current?.moved) return;
    const node = nodeOf(e.target);
    setHoverId(node && node.type !== "root" ? node.id : null);
  };
  const onFocus = (e: FocusEvent<SVGSVGElement>) => {
    const node = nodeOf(e.target);
    if (!node) return;
    setFocusId(node.id);
    setTabStop(node.id);
  };
  const onBlur = (e: FocusEvent<SVGSVGElement>) => {
    if (!svgRef.current?.contains(e.relatedTarget as Node | null)) setFocusId(null);
  };
  const onKeyDown = (e: KeyboardEvent<SVGSVGElement>) => {
    if (e.ctrlKey || e.metaKey || e.altKey) return;
    if (e.key === "+" || e.key === "=") {
      e.preventDefault();
      return zoomBy(ZOOM_STEP);
    }
    if (e.key === "-" || e.key === "_") {
      e.preventDefault();
      return zoomBy(1 / ZOOM_STEP);
    }
    if (e.key === "0") {
      e.preventDefault();
      return setView(HOME);
    }
    const node = nodeOf(e.target);
    if (!node) return;
    setKeyboard(true);
    if (e.key === "Enter" || e.key === " ") {
      e.preventDefault();
      return activate(node);
    }
    const dir = ({ ArrowLeft: "left", ArrowRight: "right", ArrowUp: "up", ArrowDown: "down" } as Record<string, Direction>)[e.key];
    if (!dir) return;
    e.preventDefault();
    const next = neighbourOf(layout.nodes, node.id, dir);
    if (next) {
      focusNode(next);
      reveal(byId.get(next)!);
    }
  };

  const at = (n: StarNode) => ({ x: tx + n.x * k, y: ty + n.y * k });
  const selected = layout.nodes.find((n) => n.selected) ?? null;
  const tipNode = (() => {
    const id = hoverId ?? (keyboard ? focusId : null);
    const n = id ? byId.get(id) : undefined;
    return n && (n.type === "note" || n.type === "cluster") && !panning ? n : null;
  })();
  const focusNodeShown = keyboard && focusId ? (byId.get(focusId) ?? null) : null;
  const empty = layout.nodes.length === 0;
  const forgotten = entries.some((e) => e.status === "forgotten");
  const isHome = view.z === 1 && view.x === 0 && view.y === 0;
  const kRounded = round(k, 3);
  const matched = matchIds ? layout.nodes.filter((n) => n.type === "note" && n.match).length : 0;

  return (
    <div ref={panelRef} className="sm-panel" style={{ height }}>
      <Sky />
      <svg
        ref={svgRef}
        className={`sm-svg${panning ? " sm-panning" : ""}`}
        width="100%"
        height="100%"
        viewBox={`0 0 ${size.w} ${size.h}`}
        preserveAspectRatio="xMidYMid meet"
        role="group"
        aria-label={empty ? "记忆星图：没有笔记" : `记忆星图：${layout.shown} 颗星`}
        aria-describedby={tipNode ? `${uid}-tip` : undefined}
        onPointerDown={onPointerDown}
        onPointerMove={onPointerMove}
        onPointerUp={onPointerUp}
        onPointerCancel={onPointerCancel}
        onPointerOver={onPointerOver}
        onPointerLeave={() => setHoverId(null)}
        onClick={onClick}
        onFocus={onFocus}
        onBlur={onBlur}
        onKeyDown={onKeyDown}
      >
        <Gradients uid={uid} />
        <g className="sm-world" transform={`translate(${round(tx)} ${round(ty)}) scale(${round(k, 4)})`} style={{ "--sm-s": String(starScale) } as CSSProperties}>
          <Grid rings={layout.rings} />
          {!empty && <Scene layout={layout} colorBy={colorBy} uid={uid} stop={stop} />}
          {!empty && <Labels layout={layout} k={kRounded} uid={uid} hide={selected?.id ?? null} />}
          {focusNodeShown && <circle className="sm-focus" cx={focusNodeShown.x} cy={focusNodeShown.y} r={round(focusNodeShown.r * (focusNodeShown.type === "note" ? starScale : 1) + 7 / k, 3)} />}
        </g>
      </svg>
      {selected && <Pulse node={selected} at={at(selected)} size={selected.r * k * starScale} colorBy={colorBy} />}
      {selected && <SelectedLabel node={selected} layout={layout} at={at(selected)} k={k} tx={tx} ty={ty} starScale={starScale} panel={size} />}
      {tipNode && <Tip node={tipNode} at={at(tipNode)} panel={size} colorBy={colorBy} id={`${uid}-tip`} byId={byId} />}
      {empty ? (
        <div className="sm-empty">
          <span className="sm-empty-star" aria-hidden="true" />
          还没有笔记，星图是空的
        </div>
      ) : (
        <>
          <div className="sm-cap" aria-hidden="true">
            <span className="sm-cap-t">记忆星图</span>
            <span className="sm-cap-n">
              {entries.length} 条{layout.hidden ? ` · 收起 ${layout.hidden}` : ""}
              {matchIds ? ` · 匹配 ${matched}` : ""}
            </span>
          </div>
          <div className="sm-tools">
            <div className="sm-seg" role="group" aria-label="星的颜色">
              <span className="sm-seg-k" aria-hidden="true">
                颜色
              </span>
              {(["kind", "source"] as const).map((id) => (
                <button key={id} type="button" className={colorBy === id ? "on" : ""} aria-pressed={colorBy === id} onClick={() => setColorBy(id)}>
                  {id === "kind" ? "类型" : "来源"}
                </button>
              ))}
            </div>
            <div className="sm-zoom" role="group" aria-label="缩放">
              <button type="button" aria-label="缩小" title="缩小（-）" disabled={view.z <= ZOOM_MIN + 1e-9} onClick={() => zoomBy(1 / ZOOM_STEP)}>
                <Minus size={13} />
              </button>
              <button type="button" aria-label="放大" title="放大（+）" disabled={view.z >= ZOOM_MAX - 1e-9} onClick={() => zoomBy(ZOOM_STEP)}>
                <Plus size={13} />
              </button>
              <button type="button" className="sm-reset" title="回到整张星图（0）" disabled={isHome} onClick={() => setView(HOME)}>
                重置视图
              </button>
            </div>
          </div>
          <Legend entries={entries} colorBy={colorBy} forgotten={forgotten} />
          {size.w >= 640 && isHome && <div className="sm-hint">滚轮缩放 · 拖动平移 · 点分支收起或展开</div>}
        </>
      )}
    </div>
  );
}

// --------------------------------------------------------------------------------------------- the chart itself

const Sky = memo(function Sky() {
  return (
    <>
      <svg className="sm-sky" aria-hidden="true" width="100%" height="100%">
        {SKY.dots.map((d, i) => (
          <circle key={i} cx={`${round(d.x, 2)}%`} cy={`${round(d.y, 2)}%`} r={round(d.r, 2)} opacity={round(d.o, 2)} />
        ))}
      </svg>
      {SKY.twinkles.map((t, i) => (
        <span key={i} className="sm-twinkle" style={{ left: `${round(t.x, 2)}%`, top: `${round(t.y, 2)}%`, animationDuration: `${round(t.d, 2)}s`, animationDelay: `${round(t.delay, 2)}s` }} />
      ))}
    </>
  );
});

const Gradients = memo(function Gradients({ uid }: { uid: string }) {
  return (
    <defs>
      {COLOR_KEYS.map((key) => {
        const c = colorOfKey(key);
        return (
          <g key={key}>
            <radialGradient id={`${uid}-g-${key}`}>
              <stop offset="0" stopColor={c} stopOpacity="0.5" />
              <stop offset="0.35" stopColor={c} stopOpacity="0.15" />
              <stop offset="1" stopColor={c} stopOpacity="0" />
            </radialGradient>
            <radialGradient id={`${uid}-c-${key}`}>
              <stop offset="0" stopColor="#ffffff" />
              <stop offset="0.55" stopColor={c} />
              <stop offset="1" stopColor={c} />
            </radialGradient>
          </g>
        );
      })}
      <radialGradient id={`${uid}-g-root`}>
        <stop offset="0" stopColor="#ffffff" stopOpacity="0.55" />
        <stop offset="0.3" stopColor="#c9d6ff" stopOpacity="0.18" />
        <stop offset="1" stopColor="#c9d6ff" stopOpacity="0" />
      </radialGradient>
    </defs>
  );
});

const Grid = memo(function Grid({ rings }: { rings: StarRings }) {
  const ticks: string[] = [];
  for (let i = 0; i < 72; i++) {
    const a = (i * Math.PI) / 36;
    const inner = rings.frame - (i % 6 === 0 ? 10 : 5);
    ticks.push(`M${round(inner * Math.cos(a))} ${round(inner * Math.sin(a))}L${round(rings.frame * Math.cos(a))} ${round(rings.frame * Math.sin(a))}`);
  }
  const spokes: string[] = [];
  for (let i = 0; i < 12; i++) {
    const a = (i * Math.PI) / 6;
    spokes.push(`M${round(rings.scope * Math.cos(a))} ${round(rings.scope * Math.sin(a))}L${round((rings.frame - 10) * Math.cos(a))} ${round((rings.frame - 10) * Math.sin(a))}`);
  }
  return (
    <g className="sm-grid" aria-hidden="true">
      <path className="sm-spokes" d={spokes.join("")} />
      <circle className="sm-ring" r={rings.scope} />
      <circle className="sm-ring" r={rings.kind} />
      <circle className="sm-ring sm-ring-leaf" r={rings.leaf} />
      {rings.outer > rings.leaf + 1 && <circle className="sm-ring sm-ring-leaf" r={rings.outer} />}
      <circle className="sm-frame" r={rings.frame} />
      <circle className="sm-frame sm-frame-in" r={rings.frame - 5} />
      <path className="sm-ticks" d={ticks.join("")} />
    </g>
  );
});

const edgePath = (edges: StarMapLayout["edges"]) => edges.map((e) => `M${e.x1} ${e.y1}L${e.x2} ${e.y2}`).join("");

const Scene = memo(function Scene({ layout, colorBy, uid, stop }: { layout: StarMapLayout; colorBy: ColorBy; uid: string; stop: string | null }) {
  const lines = layout.edges;
  const groups: [string, string][] = [
    ["sm-edge sm-leaf sm-dim", edgePath(lines.filter((e) => e.leaf && e.dim && !e.onPath))],
    ["sm-edge sm-dim", edgePath(lines.filter((e) => !e.leaf && e.dim && !e.onPath))],
    ["sm-edge sm-leaf", edgePath(lines.filter((e) => e.leaf && !e.dim && !e.onPath))],
    ["sm-edge", edgePath(lines.filter((e) => !e.leaf && !e.dim && !e.onPath))],
    ["sm-edge sm-on", edgePath(lines.filter((e) => e.onPath))],
  ];
  return (
    <>
      <g className="sm-edges" aria-hidden="true">
        {groups.map(([cls, d]) => d && <path key={cls} className={cls} d={d} />)}
      </g>
      <g className="sm-nodes">
        {layout.nodes.map((n) => (
          <Node key={n.id} node={n} colorBy={colorBy} uid={uid} stop={stop === n.id} />
        ))}
      </g>
    </>
  );
});

const flags = (n: StarNode) => `${n.dim ? " sm-dim" : ""}${n.match ? " sm-match" : ""}${n.onPath ? " sm-on" : ""}${n.selected ? " sm-selected" : ""}${n.forgotten ? " sm-forgotten" : ""}${n.collapsed ? " sm-collapsed" : ""}`;

function Node({ node: n, colorBy, uid, stop }: { node: StarNode; colorBy: ColorBy; uid: string; stop: boolean }) {
  const key = colorKey(n, colorBy);
  const color = colorOfKey(key);
  const place = `translate(${n.x} ${n.y})`;
  const focusable = { "data-id": n.id, "data-node": n.id, "data-type": n.type, tabIndex: stop ? 0 : -1, role: "button" as const };
  if (n.type === "root") {
    return (
      <g className={`sm-root${flags(n)}`} transform={place} aria-hidden="true">
        <circle className="sm-halo" r={24} fill={`url(#${uid}-g-root)`} />
        <path className="sm-sparkle" d={SPARKLE} />
        <circle className="sm-root-core" r={2.2} />
      </g>
    );
  }
  if (n.type === "note") {
    const glow = round(n.brightness * (n.dim ? 0.16 : 1), 3);
    return (
      <g
        className={`sm-star${flags(n)}`}
        transform={place}
        {...focusable}
        data-scope={n.scope ?? ""}
        data-kind={n.kind ?? ""}
        data-source={n.source ?? ""}
        aria-label={`${n.name}，${kindLabel(n.kind ?? "") || "其他"}${n.forgotten ? "，已忘记" : ""}`}
        aria-pressed={n.selected}
      >
        {!n.forgotten && <circle className="sm-halo" r={round(n.r * 3.2)} fill={`url(#${uid}-g-${key})`} fillOpacity={glow} />}
        {n.forgotten ? (
          <circle className="sm-core" r={n.r} fill="none" stroke={color} strokeOpacity={round(glow * 0.85, 3)} />
        ) : (
          <circle className="sm-core" r={n.r} fill={`url(#${uid}-c-${key})`} fillOpacity={glow} />
        )}
        {n.match && !n.selected && <circle className="sm-mark" r={round(n.r + 3.4)} />}
        {n.selected && <circle className="sm-sel" r={round(n.r + 4.5)} />}
        <circle className="sm-hit" r={6.5} />
      </g>
    );
  }
  if (n.type === "cluster") {
    return (
      <g className={`sm-cluster${flags(n)}`} transform={place} {...focusable} data-branch={n.branch ?? ""} aria-expanded={false} aria-label={`${n.name}，按 Enter 展开`}>
        <circle className="sm-halo" r={17} fill={`url(#${uid}-g-${key})`} />
        <circle className="sm-cl-ring" r={9.5} />
        <circle className="sm-core" r={n.r} fill={`url(#${uid}-c-${key})`} />
        <circle className="sm-hit" r={10} />
      </g>
    );
  }
  const title = n.type === "kind" ? `${n.name}：${n.count} 条笔记` : `${n.name.split("\n")[0]}：${n.count} 条笔记${n.name.includes("\n") ? `\n${n.name.split("\n").slice(1).join("\n")}` : ""}`;
  return (
    <g
      className={`sm-node sm-${n.type}${flags(n)}`}
      transform={place}
      {...focusable}
      aria-expanded={!n.collapsed}
      aria-label={`${n.name.split("\n")[0]}，${n.count} 条笔记，${n.collapsed ? "已收起，按 Enter 展开" : "按 Enter 收起"}`}
    >
      <title>{title}</title>
      <circle className="sm-orbit" r={n.type === "scope" ? 8.5 : 6.4} />
      <circle className="sm-body" r={n.r} fill={n.type === "kind" ? color : undefined} />
      <circle className="sm-hit" r={n.type === "scope" ? 11 : 8.5} />
    </g>
  );
}

const Labels = memo(function Labels({ layout, k, uid, hide }: { layout: StarMapLayout; k: number; uid: string; hide: string | null }) {
  const scales = ringScales(layout, k);
  const flat = labelScale(k);
  const arcs = layout.nodes.filter((n) => n.label?.shape === "arc");
  const stacks = layout.nodes.filter((n) => n.label?.shape === "stack");
  const flats = layout.nodes.filter((n) => n.label?.shape === "flat" && n.id !== hide);
  return (
    <g className="sm-labels">
      <defs>
        {arcs.map((n, i) => (
          <path key={n.id} id={`${uid}-a${i}`} d={arcPath(n.label as ArcLabel, scales.get(n.id) ?? 1)} />
        ))}
      </defs>
      {arcs.map((n, i) => {
        const l = n.label as ArcLabel;
        const scale = scales.get(n.id) ?? 1;
        const common = { className: `sm-lb sm-lb-${n.type}${flags(n)}`, "data-id": n.id, fontSize: round(LABEL_FONT * scale, 2), dominantBaseline: "central" as const, "aria-hidden": true };
        const full = n.type === "scope" ? <title>{n.name}</title> : null;
        return l.upright ? (
          <text key={n.id} {...common} textAnchor="middle">
            {full}
            {arcChars(l, scale).map((c, j) => (
              <tspan key={j} x={c.x} y={c.y}>
                {c.ch}
              </tspan>
            ))}
          </text>
        ) : (
          <text key={n.id} {...common}>
            {full}
            <textPath href={`#${uid}-a${i}`} startOffset="50%" textAnchor="middle">
              {l.text}
            </textPath>
          </text>
        );
      })}
      {stacks.map((n) => {
        const scale = scales.get(n.id) ?? 1;
        return (
          <text key={n.id} className={`sm-lb sm-lb-${n.type}${flags(n)}`} data-id={n.id} fontSize={round(LABEL_FONT * scale, 2)} textAnchor="middle" dominantBaseline="central" aria-hidden="true">
            {stackPositions(n.label as StackLabel, scale).map((c, i) => (
              <tspan key={i} x={c.x} y={c.y}>
                {c.ch}
              </tspan>
            ))}
          </text>
        );
      })}
      {flats.map((n) => {
        const l = n.label as FlatLabel;
        const base = n.type === "note" ? NOTE_FONT : LABEL_FONT;
        const grow = n.type === "root" ? Math.min(1.4, Math.max(1, 11.5 / (LABEL_FONT * k))) : 1;
        return (
          <text
            key={n.id}
            className={`sm-lb sm-lb-${n.type}${flags(n)}`}
            data-id={n.type === "cluster" || n.type === "note" ? n.id : undefined}
            x={l.x}
            y={n.type === "root" ? round(n.y + (l.y - n.y) * grow, 2) : l.y}
            textAnchor={l.anchor}
            fontSize={round(base * (n.type === "root" ? Math.max(flat, grow) : flat), 2)}
            dominantBaseline="central"
            aria-hidden="true"
          >
            {l.text}
            {l.count && <tspan className="sm-count">{` ${l.count}`}</tspan>}
          </text>
        );
      })}
    </g>
  );
});

// ------------------------------------------------------------------------------------- around the chart (HTML)

function Pulse({ node, at, size, colorBy }: { node: StarNode; at: { x: number; y: number }; size: number; colorBy: ColorBy }) {
  const d = round(Math.max(14, size * 2 + 10), 1);
  return <span className="sm-pulse" aria-hidden="true" style={{ left: round(at.x, 1), top: round(at.y, 1), width: d, height: d, color: colorOfKey(colorKey(node, colorBy)) }} />;
}

type Side = "right" | "left" | "above" | "below";

/** The chosen star's title, the same size on the screen however far the chart is zoomed: on the side the layout chose
 *  for it, unless that side runs out of the panel or over another title, then on the side that does least of that. */
function SelectedLabel({ node, layout, at, k, tx, ty, starScale, panel }: { node: StarNode; layout: StarMapLayout; at: { x: number; y: number }; k: number; tx: number; ty: number; starScale: number; panel: { w: number; h: number } }) {
  const l = node.label?.shape === "flat" ? node.label : null;
  const text = node.name;
  const gap = node.r * k * starScale + 7;
  const w = Math.min(240, textWidth(text, 12) * 1.05 + 16);
  const h = 20;
  const preferred: Side = !l || l.anchor === "start" ? "right" : l.anchor === "end" ? "left" : l.y < node.y ? "above" : "below";
  const scale = labelScale(k);
  const others = layout.nodes
    .filter((n) => n.type === "note" && n !== node && n.label?.shape === "flat")
    .map((n) => {
      const o = n.label as FlatLabel;
      const width = o.width * scale * k;
      const height = o.height * scale * k;
      const x = tx + o.x * k - (o.anchor === "start" ? 0 : o.anchor === "end" ? width : width / 2);
      return { x, y: ty + o.y * k - height / 2, w: width, h: height };
    });
  const boxOf = (side: Side) =>
    side === "right"
      ? { x: at.x + gap, y: at.y - h / 2 }
      : side === "left"
        ? { x: at.x - gap - w, y: at.y - h / 2 }
        : side === "above"
          ? { x: at.x - w / 2, y: at.y - gap - h }
          : { x: at.x - w / 2, y: at.y + gap };
  const cost = (side: Side) => {
    const b = boxOf(side);
    const outside = b.x < 6 || b.y < 40 || b.x + w > panel.w - 6 || b.y + h > panel.h - 6 ? 100 : 0;
    return outside + 10 * others.filter((o) => b.x < o.x + o.w && o.x < b.x + w && b.y < o.y + o.h && o.y < b.y + h).length;
  };
  const sides: Side[] = [preferred, ...(["right", "left", "above", "below"] as Side[]).filter((s) => s !== preferred)];
  const side = sides.reduce((best, s) => (cost(s) < cost(best) ? s : best), preferred);
  const style: CSSProperties =
    side === "right"
      ? { left: at.x + gap, top: at.y, transform: "translate(0, -50%)" }
      : side === "left"
        ? { left: at.x - gap, top: at.y, transform: "translate(-100%, -50%)" }
        : { left: at.x, top: side === "above" ? at.y - gap : at.y + gap, transform: `translate(-50%, ${side === "above" ? "-100%" : "0"})` };
  return (
    <div className="sm-sel-label" style={style} aria-hidden="true">
      {text}
    </div>
  );
}

function Tip({ node, at, panel, colorBy, id, byId }: { node: StarNode; at: { x: number; y: number }; panel: { w: number; h: number }; colorBy: ColorBy; id: string; byId: Map<string, StarNode> }) {
  const right = at.x + 16 + TIP_WIDTH < panel.w - 8 || at.x < panel.w / 2;
  const style: CSSProperties = {
    left: right ? Math.max(8, at.x + 16) : Math.min(panel.w - 8, at.x - 16),
    top: Math.min(Math.max(at.y, 64), panel.h - 64),
    transform: `translate(${right ? "0" : "-100%"}, -50%)`,
  };
  if (node.type === "cluster") {
    const branch = node.branch ? byId.get(node.branch) : undefined;
    return (
      <div className="sm-tip" role="tooltip" id={id} style={style}>
        <div className="sm-tip-title">{node.count} 条笔记已收起</div>
        <div className="sm-tip-meta">{branch ? branch.name.split("\n")[0] : ""}</div>
        <div className="sm-tip-hint">点一下或按 Enter 展开</div>
      </div>
    );
  }
  const e = node.entry!;
  const conversation = whoConversation(e.source);
  const text = snippet(e.text, 80);
  const kindColor = KIND_COLORS[e.kind] ?? OTHER_KIND_COLOR;
  const sourceColor = SOURCE_COLORS[node.source ?? "unknown"];
  return (
    <div className="sm-tip" role="tooltip" id={id} style={style}>
      <div className="sm-tip-title">{noteTitle(e)}</div>
      <div className="sm-tip-meta">
        <span className="sm-tip-i">
          <i className="sm-tip-dot" style={{ background: kindColor, opacity: colorBy === "kind" ? 1 : 0.6 }} />
          {kindLabel(e.kind) || "其他"}
        </span>
        <span>{scopeText(e)}</span>
      </div>
      <div className="sm-tip-meta">
        <span className="sm-tip-i">
          <i className="sm-tip-dot" style={{ background: sourceColor, opacity: colorBy === "source" ? 1 : 0.6 }} />
          {sourceLine(e)}
          {conversation ? ` · 会话「${conversation.title || "（无标题）"}」` : ""}
        </span>
        <span className="num">{formatRelative(e.updated)}</span>
      </div>
      {text && <div className="sm-tip-text">{text}</div>}
      {node.forgotten && <div className="sm-tip-flag">已忘记</div>}
    </div>
  );
}

const Legend = memo(function Legend({ entries, colorBy, forgotten }: { entries: MemoryEntry[]; colorBy: ColorBy; forgotten: boolean }) {
  const items = legendItems(entries, colorBy);
  return (
    <div className="sm-legend" aria-label="图例">
      <div className="sm-leg-row">
        <span className="sm-leg-k">颜色</span>
        {items.map((item) => (
          <span key={item.key} className="sm-leg-i">
            <i className="sm-leg-dot" style={{ background: item.color, color: item.color }} />
            {item.label}
          </span>
        ))}
      </div>
      <div className="sm-leg-row">
        <span className="sm-leg-k">大小</span>
        <span className="sm-leg-sizes" aria-hidden="true">
          <i />
          <i />
          <i />
        </span>
        内容越长星越大
      </div>
      <div className="sm-leg-row">
        <span className="sm-leg-k">亮度</span>
        <span className="sm-leg-glow" aria-hidden="true">
          <i />
          <i />
          <i />
        </span>
        越新越亮
      </div>
      {forgotten && (
        <div className="sm-leg-row">
          <span className="sm-leg-k">空心</span>
          <span className="sm-leg-ring" aria-hidden="true" />
          已忘记的笔记
        </div>
      )}
    </div>
  );
});
