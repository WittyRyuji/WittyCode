import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { defineConfig, type Plugin } from "vite";
import react from "@vitejs/plugin-react";
import { viteSingleFile } from "vite-plugin-singlefile";

/** The production page may load nothing from anywhere: everything is inlined (data: URIs for pictures and fonts), and
 *  Qt's QWebChannel transport is not network. */
const CSP =
  "default-src 'none'; script-src 'unsafe-inline'; style-src 'unsafe-inline'; img-src data:; font-src data:; connect-src 'none'; base-uri 'none'; form-action 'none'";
const ICON_HREF = 'href="/src/assets/brand/icon-256.png"';
const CHARSET = '<meta charset="UTF-8" />';

/** Only for `vite build` (the dev server needs its websocket and module URLs): the page gets its Content-Security-Policy,
 *  and the window icon becomes a data: URI, because Vite never inlines a `<link rel="icon">` whatever
 *  `assetsInlineLimit` says (it would stay a separate file next to index.html). */
function selfContained(): Plugin {
  return {
    name: "wittycode-self-contained",
    apply: "build",
    transformIndexHtml: {
      order: "pre",
      handler(html) {
        if (!html.includes(ICON_HREF) || !html.includes(CHARSET)) throw new Error("index.html changed: update selfContained() in vite.config.ts");
        const icon = readFileSync(fileURLToPath(new URL("./src/assets/brand/icon-256.png", import.meta.url))).toString("base64");
        return html
          .replace(ICON_HREF, `href="data:image/png;base64,${icon}"`)
          .replace(CHARSET, `${CHARSET}\n    <meta http-equiv="Content-Security-Policy" content="${CSP}" />`);
      },
    },
  };
}

// `npm run build` writes one self-contained dist/index.html (scripts, styles, fonts and pictures inlined): it opens from
// disk with no server and no network, which is also how the Qt WebEngine view loads it (`npm run build:app`).
export default defineConfig({
  plugins: [react(), selfContained(), viteSingleFile()],
  build: {
    target: "chrome120",
    assetsInlineLimit: 100_000_000,
    cssCodeSplit: false,
    chunkSizeWarningLimit: 4000,
  },
});
