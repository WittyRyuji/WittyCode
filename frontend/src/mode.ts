// Which page this is, decided once before the first render: the real app inside WittyCode (Qt WebEngine, talking to
// Python through QWebChannel), or the design review with mock data. `?live=1` asks for the live app in a plain browser;
// the live checks use it together with a fake transport.

declare global {
  interface Window {
    qt?: { webChannelTransport?: unknown };
  }
}

const query = new URLSearchParams(window.location.search);

export const LIVE: boolean = !!window.qt?.webChannelTransport || query.get("live") === "1";

/** The outline of the window, which only Python knows (`?frame=round|square`, see `desktop/ui/winframe.py`): the Qt
 *  window has no frame, so the page draws a thin line along its edge, with the corners Windows gives the window (round
 *  on Windows 11). Null: no outline (the design review, a plain browser, a platform without the system's shadow). */
export const FRAME: "round" | "square" | null = ((f) => (f === "round" || f === "square" ? f : null))(query.get("frame"));

/** The theme and text size to start with in the live app, before Python has said which ones the user chose. Python can
 *  pass them in the page's URL (`?theme=dark&fs=md`) so that the first frame already has the right colours. */
export function liveSeed(): { theme: "system" | "light" | "dark"; fontScale: "sm" | "md" | "lg" } {
  const theme = query.get("theme");
  const fs = query.get("fs");
  return {
    theme: theme === "light" || theme === "dark" ? theme : "system",
    fontScale: fs === "sm" || fs === "lg" ? fs : "md",
  };
}
