// Renders a Markdown file (the README by default) to one self-contained HTML page that looks like the file on a repository
// page, so it can be looked at before it is pushed: `node scripts/readme-preview.mjs [input.md] [output.html]`.
// Pictures are inlined, relative links point at the files on disk, nothing is fetched from the network.
import { readFileSync, writeFileSync, existsSync, mkdirSync } from "node:fs";
import { dirname, extname, isAbsolute, resolve } from "node:path";
import { pathToFileURL } from "node:url";
import { createElement as h } from "react";
import { renderToStaticMarkup } from "react-dom/server";
import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";
import hljs from "highlight.js/lib/core";
import powershell from "highlight.js/lib/languages/powershell";
import python from "highlight.js/lib/languages/python";
import json from "highlight.js/lib/languages/json";
import bash from "highlight.js/lib/languages/bash";
import javascript from "highlight.js/lib/languages/javascript";
import typescript from "highlight.js/lib/languages/typescript";
import xml from "highlight.js/lib/languages/xml";
import yaml from "highlight.js/lib/languages/yaml";

for (const [name, grammar] of Object.entries({ powershell, python, json, bash, javascript, typescript, xml, yaml })) hljs.registerLanguage(name, grammar);
const ALIASES = { ps1: "powershell", pwsh: "powershell", sh: "bash", shell: "bash", js: "javascript", ts: "typescript", py: "python", html: "xml", yml: "yaml" };

const input = resolve(process.argv[2] ?? "../README.md");
const output = resolve(process.argv[3] ?? "../../outputs/wittycode-readme-preview.html");
const base = dirname(input);
const source = readFileSync(input, "utf8");

const MIME = { ".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".gif": "image/gif", ".svg": "image/svg+xml", ".webp": "image/webp" };
const missing = [];

function localFile(target) {
  if (!target || /^[a-z][a-z0-9+.-]*:/i.test(target) || target.startsWith("#")) return null;
  const path = isAbsolute(target) ? target : resolve(base, decodeURIComponent(target.split("#")[0]));
  return path;
}

function Pre({ children }) {
  const code = Array.isArray(children) ? children[0] : children;
  const text = String(code?.props?.children ?? "").replace(/\n$/, "");
  const wanted = /language-([\w+-]+)/.exec(code?.props?.className ?? "")?.[1] ?? "";
  const language = ALIASES[wanted.toLowerCase()] ?? wanted.toLowerCase();
  const html = hljs.getLanguage(language) ? hljs.highlight(text, { language, ignoreIllegals: true }).value : null;
  return h("pre", null, html === null ? h("code", null, text) : h("code", { className: "hljs", dangerouslySetInnerHTML: { __html: html } }));
}

const components = {
  pre: Pre,
  img({ src, alt, title }) {
    const path = localFile(src);
    if (path === null) return h("img", { src, alt, title });
    if (!existsSync(path)) {
      missing.push(src);
      return h("span", { className: "broken" }, `[图片不存在：${src}]`);
    }
    const data = readFileSync(path).toString("base64");
    return h("img", { src: `data:${MIME[extname(path).toLowerCase()] ?? "application/octet-stream"};base64,${data}`, alt, title });
  },
  a({ href, children }) {
    const path = localFile(href);
    if (path === null) return h("a", { href }, children);
    if (!existsSync(path)) missing.push(href);
    return h("a", { href: pathToFileURL(path).href + (href.includes("#") ? `#${href.split("#")[1]}` : "") }, children);
  },
  table({ children }) {
    return h("div", { className: "table-wrap" }, h("table", null, children));
  },
};

const body = renderToStaticMarkup(h(ReactMarkdown, { remarkPlugins: [remarkGfm], components }, source));
const title = /^#\s+(.+)$/m.exec(source)?.[1] ?? "README";

const css = `
:root { color-scheme: light dark; --bg: #fff; --fg: #1f2328; --muted: #59636e; --line: #d1d9e0; --soft: #f6f8fa; --link: #0969da; --desk: #f6f8fa;
  --k: #cf222e; --s: #0a3069; --c: #6e7781; --n: #0550ae; --t: #8250df; }
@media (prefers-color-scheme: dark) { :root { --bg: #0d1117; --fg: #f0f6fc; --muted: #9198a1; --line: #3d444d; --soft: #151b23; --link: #4493f8; --desk: #010409;
  --k: #ff7b72; --s: #a5d6ff; --c: #9198a1; --n: #79c0ff; --t: #d2a8ff; } }
* { box-sizing: border-box; }
body { margin: 0; background: var(--desk); color: var(--fg); font: 16px/1.5 -apple-system, "Segoe UI", "Microsoft YaHei UI", "Noto Sans CJK SC", Helvetica, Arial, sans-serif; }
.page { max-width: 1012px; margin: 32px auto; padding: 0 16px; }
.box { background: var(--bg); border: 1px solid var(--line); border-radius: 6px; }
.box-head { display: flex; align-items: center; gap: 8px; padding: 10px 16px; border-bottom: 1px solid var(--line); background: var(--soft); border-radius: 6px 6px 0 0; font-size: 14px; font-weight: 600; }
.box-head svg { fill: var(--muted); flex: none; }
article { padding: 32px 40px 40px; word-wrap: break-word; }
article > :first-child { margin-top: 0; }
article > :last-child { margin-bottom: 0; }
h1, h2, h3, h4 { margin: 24px 0 16px; font-weight: 600; line-height: 1.25; }
h1 { font-size: 2em; padding-bottom: .3em; border-bottom: 1px solid var(--line); }
h2 { font-size: 1.5em; padding-bottom: .3em; border-bottom: 1px solid var(--line); }
h3 { font-size: 1.25em; }
p, ul, ol, table, pre, blockquote, .table-wrap { margin: 0 0 16px; }
ul, ol { padding-left: 2em; }
li + li { margin-top: .25em; }
li > ul, li > ol { margin: .25em 0 0; }
a { color: var(--link); text-decoration: none; }
a:hover { text-decoration: underline; }
img { max-width: 100%; height: auto; border: 1px solid var(--line); border-radius: 6px; background: var(--soft); }
.broken { color: #cf222e; }
code { font: 85% ui-monospace, SFMono-Regular, "Cascadia Mono", Consolas, "Liberation Mono", monospace; background: color-mix(in srgb, var(--muted) 20%, transparent); padding: .2em .4em; border-radius: 6px; }
pre { background: var(--soft); border-radius: 6px; padding: 16px; overflow: auto; font-size: 85%; line-height: 1.45; }
pre code { background: none; padding: 0; font-size: 100%; white-space: pre; }
.table-wrap { overflow-x: auto; }
table { border-collapse: collapse; display: block; width: max-content; max-width: 100%; overflow: auto; }
th, td { padding: 6px 13px; border: 1px solid var(--line); }
th { font-weight: 600; }
tr:nth-child(2n) td { background: var(--soft); }
blockquote { padding: 0 1em; color: var(--muted); border-left: .25em solid var(--line); }
hr { border: 0; border-top: 1px solid var(--line); margin: 24px 0; }
.hljs-keyword, .hljs-built_in, .hljs-literal { color: var(--k); }
.hljs-string, .hljs-regexp { color: var(--s); }
.hljs-comment { color: var(--c); }
.hljs-number, .hljs-attr, .hljs-variable, .hljs-params { color: var(--n); }
.hljs-title, .hljs-function, .hljs-name, .hljs-section { color: var(--t); }
@media (max-width: 700px) { article { padding: 20px 16px 28px; } }
`;

const icon = `<svg viewBox="0 0 16 16" width="16" height="16" aria-hidden="true"><path d="M2 1.75C2 .784 2.784 0 3.75 0h6.586c.464 0 .909.184 1.237.513l2.914 2.914c.329.328.513.773.513 1.237v9.586A1.75 1.75 0 0 1 13.25 16h-9.5A1.75 1.75 0 0 1 2 14.25Zm1.75-.25a.25.25 0 0 0-.25.25v12.5c0 .138.112.25.25.25h9.5a.25.25 0 0 0 .25-.25V6h-2.75A1.75 1.75 0 0 1 9 4.25V1.5Zm6.75.062V4.25c0 .138.112.25.25.25h2.688l-.011-.013-2.914-2.914-.013-.011Z"/></svg>`;
const page = `<!doctype html><html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>${title.replace(/</g, "&lt;")}</title><style>${css}</style></head><body><div class="page"><div class="box"><div class="box-head">${icon}<span>${input.split(/[\\/]/).pop()}</span></div><article>${body}</article></div></div></body></html>`;

mkdirSync(dirname(output), { recursive: true });
writeFileSync(output, page, "utf8");
console.log(`wrote ${output} (${(page.length / 1024).toFixed(0)} KB)`);
if (missing.length) console.log(`missing targets: ${[...new Set(missing)].join(", ")}`);
