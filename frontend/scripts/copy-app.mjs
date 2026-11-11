// `npm run build:app`: after `npm run build`, the app's copy of the page (desktop/ui_dist/index.html, loaded by
// desktop/ui/shell.py), once it is certain that the page stands alone and carries its Content-Security-Policy.
import { copyFileSync, mkdirSync, readFileSync, statSync } from "node:fs";
import { dirname, resolve } from "node:path";
import { EXPECTED_CSP, cspComesFirst, declaredCsp, externalReferences } from "./selfcontained.mjs";

const from = resolve("dist/index.html");
const to = resolve("../desktop/ui_dist/index.html");
const html = readFileSync(from, "utf8");

const problems = externalReferences(html).map((r) => `refers to a file outside the page: ${r}`);
if (declaredCsp(html) !== EXPECTED_CSP) problems.push(`the Content-Security-Policy is ${JSON.stringify(declaredCsp(html))}, expected ${JSON.stringify(EXPECTED_CSP)}`);
if (!cspComesFirst(html)) problems.push("the Content-Security-Policy does not come before the scripts and styles");
if (problems.length) {
  for (const p of problems) console.error(`build:app: ${p}`);
  process.exit(1);
}
mkdirSync(dirname(to), { recursive: true });
copyFileSync(from, to);
console.log(`build:app: wrote ${to} (${statSync(to).size.toLocaleString("en")} bytes, self-contained, CSP set)`);
