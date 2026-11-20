import { useMemo } from "react";
import katex from "katex";

/** Chat formulas are untrusted model output: safe defaults — no trusted commands (`\href`, `\includegraphics`, …),
 *  limited macro expansion, and a broken or half-typed formula shows its source instead of throwing. `mathml` output
 *  is drawn by Qt WebEngine's Chromium natively, so no KaTeX fonts are bundled. */
const OPTIONS = {
  output: "mathml",
  throwOnError: false,
  strict: "ignore",
  trust: false,
  maxExpand: 1000,
  maxSize: 500,
} as const;

const MAX_TEX = 2000; // a formula this long is almost never meant to be: it degrades to its source

export function MathFormula({ tex, display = false }: { tex: string; display?: boolean }) {
  const html = useMemo(() => {
    if (tex.length > MAX_TEX) return null;
    try {
      return katex.renderToString(tex, { ...OPTIONS, displayMode: display });
    } catch {
      return null;
    }
  }, [tex, display]);
  if (html === null) {
    const source = <code className="math-source">{tex}</code>;
    return display ? <div className="math-formula display">{source}</div> : source;
  }
  return display
    ? <div className="math-formula display" dangerouslySetInnerHTML={{ __html: html }} />
    : <span className="math-formula" dangerouslySetInnerHTML={{ __html: html }} />;
}
