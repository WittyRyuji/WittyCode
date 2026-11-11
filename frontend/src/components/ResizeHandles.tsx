import { bridge } from "../bridge-instance";
import { messageOf } from "../lib/errors";
import type { Edge } from "../protocol";
import { useStore } from "../store";

const EDGES: Edge[] = ["n", "s", "e", "w", "nw", "ne", "sw", "se"];

/** The frameless window's borders, invisible: 6 px strips along the sides, 10 px squares in the corners. Pressing one
 *  hands the resize to Windows (Python: QWindow.startSystemResize), so snapping keeps working. Gone while maximized. */
export function ResizeHandles() {
  const maximized = useStore((s) => s.maximized);
  const say = useStore((s) => s.say);
  if (maximized) return null;
  return (
    <div className="rz-handles" aria-hidden>
      {EDGES.map((edge) => (
        <div
          key={edge}
          className={`rz rz-${edge}`}
          data-edge={edge}
          onMouseDown={(e) => {
            if (e.button !== 0) return;
            e.preventDefault();
            bridge.window.startResize(edge).catch((error) => say(messageOf(error)));
          }}
        />
      ))}
    </div>
  );
}
