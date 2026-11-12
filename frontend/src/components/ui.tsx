import { useCallback, useEffect, useLayoutEffect, useRef, useState, type ReactNode } from "react";
import { Check } from "lucide-react";
import type { SessionMark } from "../types";

// ---------------------------------------------------------------------------------------------- state dot

export function StateDot({ mark, size = 9 }: { mark?: SessionMark; size?: number }) {
  if (!mark) return <span className="dot dot-none" style={{ width: size, height: size }} />;
  return <span className={`dot dot-${mark}`} style={{ width: size, height: size }} aria-hidden />;
}

// ---------------------------------------------------------------------------------------------- menu

export interface MenuItem {
  id: string;
  label?: string;
  description?: string;
  shortcut?: string;
  icon?: ReactNode;
  danger?: boolean;
  checked?: boolean;
  disabled?: boolean;
  separator?: boolean;
  /** a group title: not clickable, the `label` is its text and `description` a second, quieter line */
  heading?: boolean;
  /** with `heading`: a small title inside a group (the makers of a source's models) */
  sub?: boolean;
  /** stays at the bottom of a long, scrolling menu */
  pinned?: boolean;
  onSelect?: () => void;
}

export interface Anchor {
  x: number;
  y: number;
  /** the trigger's height, so a menu that opens upward can sit above it */
  h?: number;
  w?: number;
  up?: boolean;
  alignRight?: boolean;
}

export function Menu({ items, anchor, onClose, minWidth = 200 }: { items: MenuItem[]; anchor: Anchor; onClose: () => void; minWidth?: number }) {
  const ref = useRef<HTMLDivElement>(null);
  const [pos, setPos] = useState<{ left: number; top: number } | null>(null);
  const [active, setActive] = useState(-1);
  const selectable = items.map((it, i) => (it.separator || it.heading || it.disabled ? -1 : i)).filter((i) => i >= 0);

  useLayoutEffect(() => {
    const el = ref.current;
    if (!el) return;
    const r = el.getBoundingClientRect();
    const pad = 8;
    let left = anchor.alignRight ? anchor.x + (anchor.w ?? 0) - r.width : anchor.x;
    let top = anchor.up ? anchor.y - r.height - 6 : anchor.y + (anchor.h ?? 0) + 6;
    if (anchor.up && top < pad) top = anchor.y + (anchor.h ?? 0) + 6;
    left = Math.max(pad, Math.min(left, window.innerWidth - r.width - pad));
    top = Math.max(pad, Math.min(top, window.innerHeight - r.height - pad));
    setPos({ left, top });
  }, [anchor, items.length]);

  useEffect(() => {
    const away = (e: MouseEvent) => {
      if (!ref.current?.contains(e.target as Node)) onClose();
    };
    const key = (e: KeyboardEvent) => {
      if (e.key === "Escape") {
        e.preventDefault();
        onClose();
      } else if (e.key === "ArrowDown" || e.key === "ArrowUp") {
        e.preventDefault();
        setActive((cur) => {
          const at = selectable.indexOf(cur);
          const next = e.key === "ArrowDown" ? (at + 1) % selectable.length : (at - 1 + selectable.length) % selectable.length;
          return selectable[next] ?? -1;
        });
      } else if (e.key === "Enter" && active >= 0) {
        e.preventDefault();
        const it = items[active];
        onClose();
        it?.onSelect?.();
      }
    };
    document.addEventListener("mousedown", away, true);
    document.addEventListener("keydown", key, true);
    window.addEventListener("blur", onClose);
    return () => {
      document.removeEventListener("mousedown", away, true);
      document.removeEventListener("keydown", key, true);
      window.removeEventListener("blur", onClose);
    };
  });

  const row = (it: MenuItem, i: number) =>
    it.separator ? (
      <div key={it.id} className="menu-sep" role="separator" />
    ) : it.heading && it.sub ? (
      <div key={it.id} className="menu-sub" role="presentation">
        {it.label}
      </div>
    ) : it.heading ? (
      <div key={it.id} className="menu-head" role="presentation">
        <span>{it.label}</span>
        {it.description && <span className="menu-head-sub">{it.description}</span>}
      </div>
    ) : (
      <button
        key={it.id}
        type="button"
        role="menuitem"
        className={`menu-item${it.danger ? " danger" : ""}${active === i ? " active" : ""}`}
        disabled={it.disabled}
        onMouseEnter={() => setActive(i)}
        onMouseDown={(e) => e.preventDefault()}
        onClick={() => {
          onClose();
          it.onSelect?.();
        }}
      >
        <span className="menu-check">{it.checked ? <Check size={14} /> : it.icon}</span>
        <span className="menu-text">
          <span className="menu-label">{it.label}</span>
          {it.description && <span className="menu-desc">{it.description}</span>}
        </span>
        {it.shortcut && <kbd className="menu-kbd">{it.shortcut}</kbd>}
      </button>
    );
  const body = items.map((it, i) => ({ it, i })).filter((x) => !x.it.pinned);
  const pinned = items.map((it, i) => ({ it, i })).filter((x) => x.it.pinned);

  return (
    <div ref={ref} className="menu" role="menu" style={{ left: pos?.left ?? -9999, top: pos?.top ?? -9999, minWidth, visibility: pos ? "visible" : "hidden" }}>
      {body.map((x) => row(x.it, x.i))}
      {pinned.length > 0 && <div className="menu-pinned">{pinned.map((x) => row(x.it, x.i))}</div>}
    </div>
  );
}

/** A trigger that opens a Menu next to itself. The click never takes the typing focus away from the composer. */
export function MenuButton({
  items,
  children,
  className,
  title,
  up,
  alignRight,
  disabled,
  minWidth,
  ariaLabel,
}: {
  items: MenuItem[];
  children: ReactNode;
  className?: string;
  title?: string;
  /** the button's name for assistive technology, when its text alone does not say what it is */
  ariaLabel?: string;
  up?: boolean;
  alignRight?: boolean;
  disabled?: boolean;
  minWidth?: number;
}) {
  const [anchor, setAnchor] = useState<Anchor | null>(null);
  const ref = useRef<HTMLButtonElement>(null);
  const toggle = useCallback(() => {
    if (anchor) return setAnchor(null);
    const r = ref.current!.getBoundingClientRect();
    setAnchor({ x: r.left, y: r.top, h: r.height, w: r.width, up, alignRight });
  }, [anchor, up, alignRight]);
  return (
    <>
      <button
        ref={ref}
        type="button"
        className={`${className ?? ""}${anchor ? " open" : ""}`}
        title={title}
        disabled={disabled}
        aria-label={ariaLabel}
        aria-haspopup="menu"
        aria-expanded={!!anchor}
        onMouseDown={(e) => e.preventDefault()}
        onClick={toggle}
      >
        {children}
      </button>
      {anchor && <Menu items={items} anchor={anchor} onClose={() => setAnchor(null)} minWidth={minWidth} />}
    </>
  );
}

// ---------------------------------------------------------------------------------------------- tooltip

export function Tip({ text, children, className, side = "top" }: { text: ReactNode; children: ReactNode; className?: string; side?: "top" | "bottom" }) {
  const [at, setAt] = useState<{ x: number; y: number } | null>(null);
  const timer = useRef<number | undefined>(undefined);
  const ref = useRef<HTMLSpanElement>(null);
  const show = () => {
    window.clearTimeout(timer.current);
    timer.current = window.setTimeout(() => {
      const r = ref.current?.getBoundingClientRect();
      if (r) setAt({ x: r.left + r.width / 2, y: side === "top" ? r.top : r.bottom });
    }, 350);
  };
  const hide = () => {
    window.clearTimeout(timer.current);
    setAt(null);
  };
  return (
    <span ref={ref} className={className} onMouseEnter={show} onMouseLeave={hide} onMouseDown={hide}>
      {children}
      {at && (
        <span className={`tip tip-${side}`} style={{ left: Math.max(150, Math.min(at.x, window.innerWidth - 150)), top: at.y }}>
          {text}
        </span>
      )}
    </span>
  );
}

// ---------------------------------------------------------------------------------------------- misc

export function Spinner({ size = 14 }: { size?: number }) {
  return <span className="spinner" style={{ width: size, height: size }} aria-hidden />;
}
