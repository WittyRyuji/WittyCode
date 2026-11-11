import { useState } from "react";
import { ChevronRight } from "lucide-react";
import { useStore } from "../store";
import type { ModelChoice } from "../types";
import { Switch } from "./formkit";

/** A Factory model of one maker under that maker's name (like the model menu); the other sources have no makers. */
function byMaker(models: ModelChoice[]): [string | null, ModelChoice[]][] {
  if (!models.some((m) => m.group)) return [[null, models]];
  const groups = new Map<string, ModelChoice[]>();
  for (const m of models) groups.set(m.group ?? "其他", [...(groups.get(m.group ?? "其他") ?? []), m]);
  return [...groups];
}

/** Which of one source's models the conversation's model menu offers: a switch for each, and show / hide all. Folded
 *  away by default (Factory alone lists a few dozen). Renders nothing while the source has no models to choose from. */
export function ModelVisibility({ source }: { source: string }) {
  const models = useStore((s) => s.models);
  const hidden = useStore((s) => s.hiddenModels);
  const setVisible = useStore((s) => s.setModelsVisible);
  const [open, setOpen] = useState(false);
  const own = models.filter((m) => m.source === source && !m.disabled);
  if (own.length === 0) return null;
  const ids = own.map((m) => m.id);
  const shown = own.filter((m) => !hidden.includes(m.id)).length;
  return (
    <div className="mvis" data-source={source}>
      <button type="button" className="disclose" aria-expanded={open} onClick={() => setOpen((v) => !v)}>
        <ChevronRight size={14} className={open ? "down" : ""} /> 对话页显示的模型
        <span className="mvis-count num">
          {shown} / {own.length}
        </span>
      </button>
      {open && (
        <div className="mvis-body">
          <p className="mvis-hint">关掉的模型不会出现在输入框下面的模型菜单里。正在用的模型照常显示。</p>
          <div className="mvis-bulk">
            <button type="button" className="btn small" disabled={shown === own.length} onClick={() => setVisible(ids, true)}>
              全部显示
            </button>
            <button type="button" className="btn small" disabled={shown === 0} onClick={() => setVisible(ids, false)}>
              全部隐藏
            </button>
          </div>
          {byMaker(own).map(([maker, list]) => (
            <div key={maker ?? "all"} className="mvis-group">
              {maker && <div className="mvis-maker">{maker}</div>}
              {list.map((m) => (
                <div key={m.id} className="mvis-row">
                  <div className="mvis-name">
                    <span>{m.label}</span>
                    {m.note && <span className="mvis-note">{m.note}</span>}
                  </div>
                  <Switch on={!hidden.includes(m.id)} onChange={(v) => setVisible([m.id], v)} label={`在对话页显示 ${m.label}`} />
                </div>
              ))}
            </div>
          ))}
        </div>
      )}
    </div>
  );
}
