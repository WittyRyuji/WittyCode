import { readFileSync, writeFileSync } from "node:fs";
const svg = readFileSync(process.argv[2], "utf8");
const paths = [...svg.matchAll(/<path d="([^"]+)"/g)].map((m) => m[1]);
console.log("paths:", paths.length, paths.map((p) => p.length).join(", "));
const view = /viewBox="(3\.4574[^"]+)"/.exec(svg)?.[1];
console.log("dragon viewBox:", view);
const out = `// Extracted from WITTY LOONG 白色LOGO.svg (the dragon icon and the two wordmark lines). Single fill, so the marks follow
// the text colour of the page: white on the dark theme, black on the light one.
export const DRAGON_VIEWBOX = ${JSON.stringify(view)};
export const DRAGON_PATH = ${JSON.stringify(paths[0])};
export const WORD_TOP_PATH = ${JSON.stringify(paths[1])};
export const WORD_BOTTOM_PATH = ${JSON.stringify(paths[2])};
`;
writeFileSync(process.argv[3], out, "utf8");
