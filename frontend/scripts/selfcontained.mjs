// Does a built page stand alone? Used by copy-app.mjs (npm run build:app) and by the checks.

/** The Content-Security-Policy the production build must carry (vite.config.ts writes it). */
export const EXPECTED_CSP =
  "default-src 'none'; script-src 'unsafe-inline'; style-src 'unsafe-inline'; img-src data:; font-src data:; connect-src 'none'; base-uri 'none'; form-action 'none'";

const inline = (value) => value === "" || value.startsWith("data:") || value.startsWith("#");

/** Every reference to something outside the file: tag attributes that load or link a resource (outside the scripts),
 *  and url(...) / @import in the styles. Empty when the page is self-contained. Script contents are not looked at:
 *  the Content-Security-Policy blocks whatever they might try to load, and the checks watch for violations. */
export function externalReferences(html) {
  const out = [];
  const scripts = [];
  const styles = [];
  const markup = html
    .replace(/<script\b([^>]*)>([\s\S]*?)<\/script>/gi, (_, attrs, body) => {
      scripts.push(body);
      return `<script${attrs}></script>`;
    })
    .replace(/<style\b([^>]*)>([\s\S]*?)<\/style>/gi, (_, attrs, body) => {
      styles.push(body);
      return `<style${attrs}></style>`;
    });
  for (const tag of markup.match(/<[a-z][^>]*>/gi) ?? []) {
    for (const m of tag.matchAll(/\s(src|href|srcset|poster|data|action|formaction|xlink:href)\s*=\s*("([^"]*)"|'([^']*)'|([^\s>]+))/gi)) {
      const value = (m[3] ?? m[4] ?? m[5] ?? "").trim();
      if (!inline(value)) out.push(`${tag.slice(0, 60)}… ${m[1]}=${value.slice(0, 80)}`);
    }
  }
  for (const css of styles) {
    for (const m of css.matchAll(/url\(\s*(['"]?)([^'")]*)\1\s*\)/gi)) if (!inline(m[2].trim())) out.push(`url(${m[2].slice(0, 80)})`);
    for (const m of css.matchAll(/@import\s+([^;]+);/gi)) out.push(`@import ${m[1].slice(0, 80)}`);
  }
  return out;
}

/** The policy the page declares, or null. */
export function declaredCsp(html) {
  const m = /<meta\s+http-equiv="Content-Security-Policy"\s+content="([^"]*)"/i.exec(html);
  return m ? m[1] : null;
}

/** Whether the policy comes before every script and style (a meta policy only covers what follows it). */
export function cspComesFirst(html) {
  const at = html.search(/<meta\s+http-equiv="Content-Security-Policy"/i);
  const first = html.search(/<(script|style|link)\b/i);
  return at >= 0 && (first < 0 || at < first);
}
