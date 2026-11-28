import { useRef, useState } from "react";
import { liveCall } from "../liveBridge";
import { messageOf } from "../lib/errors";
import { useStore } from "../store";
import type { TurnHistory } from "../types";

/** Loading is explicit user intent; a short first page never auto-fetches the entire history on mount. */
export function useOlderTurns(id: string | undefined, agentId: string | undefined, history: TurnHistory | undefined, before: string | undefined) {
  const pending = useRef<symbol | null>(null);
  const [loading, setLoading] = useState(false);
  const canLoad = !!(id && history?.more && before);
  const load = (saveAnchor: () => void) => {
    if (!id || !history?.more || !before || pending.current) return;
    saveAnchor();
    const request = Symbol();
    pending.current = request;
    setLoading(true);
    void liveCall("session.olderTurns", { id, agentId, generation: history.generation, before })
      .catch((error) => useStore.getState().say(messageOf(error)))
      .finally(() => {
        if (pending.current === request) {
          pending.current = null;
          setLoading(false);
        }
      });
  };
  return { canLoad, loading, load };
}

export function OlderTurns({ loading, onLoad }: { loading: boolean; onLoad: () => void }) {
  return <div className="history-more"><button type="button" className="linkbtn" disabled={loading} onClick={onLoad}>
    {loading ? "正在加载更早的对话…" : "加载更早的对话"}
  </button></div>;
}
