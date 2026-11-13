import type { ReactNode } from "react";
import { ChevronDown } from "lucide-react";
import { MenuButton, type MenuItem } from "./ui";

/** A settings row: the name and a hint on the left, the control on the right. */
export function Field({ label, hint, children }: { label: string; hint?: ReactNode; children: ReactNode }) {
  return (
    <div className="field">
      <div className="field-text">
        <div className="field-label">{label}</div>
        {hint && <div className="field-hint">{hint}</div>}
      </div>
      <div className="field-ctl">{children}</div>
    </div>
  );
}

/** A labelled control on its own lines (the provider editor). */
export function Stack({ label, hint, children, error }: { label: string; hint?: ReactNode; children: ReactNode; error?: string }) {
  return (
    <div className="stack">
      <div className="stack-label">{label}</div>
      {children}
      {error ? <div className="stack-err">{error}</div> : hint ? <div className="stack-hint">{hint}</div> : null}
    </div>
  );
}

export function Seg<T extends string>({ value, options, onChange }: { value: T; options: { id: T; label: string }[]; onChange: (v: T) => void }) {
  return (
    <div className="segc" role="group">
      {options.map((o) => (
        <button key={o.id} type="button" className={value === o.id ? "on" : ""} aria-pressed={value === o.id} onClick={() => onChange(o.id)}>
          {o.label}
        </button>
      ))}
    </div>
  );
}

export function Select({ value, items, disabled, minWidth = 220 }: { value: string; items: MenuItem[]; disabled?: boolean; minWidth?: number }) {
  return (
    <MenuButton className="selectbox" items={items} minWidth={minWidth} disabled={disabled}>
      <span>{value}</span>
      <ChevronDown size={14} />
    </MenuButton>
  );
}

/** An on/off switch: a square with a block that moves, like the rest of the squared-off design. */
export function Switch({ on, onChange, label, disabled }: { on: boolean; onChange: (v: boolean) => void; label: string; disabled?: boolean }) {
  return (
    <button type="button" role="switch" disabled={disabled} aria-checked={on} aria-label={label} className={`switch${on ? " on" : ""}`} onClick={() => onChange(!on)}>
      <span />
    </button>
  );
}

export function TextInput({
  value,
  onChange,
  placeholder,
  mono,
  type = "text",
  label,
  invalid,
  width,
  autoFocus,
  onEnter,
}: {
  value: string;
  onChange: (v: string) => void;
  placeholder?: string;
  mono?: boolean;
  type?: "text" | "password" | "number";
  label: string;
  invalid?: boolean;
  width?: number | string;
  autoFocus?: boolean;
  /** Enter in the field (not while an input method is composing) */
  onEnter?: () => void;
}) {
  return (
    <input
      autoFocus={autoFocus}
      onKeyDown={onEnter ? (e) => e.key === "Enter" && !e.nativeEvent.isComposing && onEnter() : undefined}
      onFocus={autoFocus ? (e) => e.currentTarget.select() : undefined}
      className={`textin selectable${mono ? " mono" : ""}${invalid ? " invalid" : ""}`}
      type={type}
      value={value}
      placeholder={placeholder}
      aria-label={label}
      spellCheck={false}
      autoComplete="off"
      style={width ? { width } : undefined}
      onChange={(e) => onChange(e.target.value)}
    />
  );
}
