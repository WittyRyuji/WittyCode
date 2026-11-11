import { FRAME } from "../mode";
import { useStore } from "../store";

/** The outline of the window: a thin line along its edge. The Qt window has no frame, and a white page next to a white
 *  window had no visible end; Windows' own drop shadow (Python: `winframe.py`) and this line mark it. Gone while the
 *  window is maximized (it then fills the screen), and where Python did not ask for it (`?frame=` in the address). */
export function FrameLine() {
  const maximized = useStore((s) => s.maximized);
  if (!FRAME || maximized) return null;
  return <div className={`frame-line ${FRAME}`} aria-hidden />;
}
