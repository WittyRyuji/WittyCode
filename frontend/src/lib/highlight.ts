import hljs from "highlight.js/lib/core";
import python from "highlight.js/lib/languages/python";
import javascript from "highlight.js/lib/languages/javascript";
import typescript from "highlight.js/lib/languages/typescript";
import json from "highlight.js/lib/languages/json";
import bash from "highlight.js/lib/languages/bash";
import powershell from "highlight.js/lib/languages/powershell";
import cpp from "highlight.js/lib/languages/cpp";
import css from "highlight.js/lib/languages/css";
import xml from "highlight.js/lib/languages/xml";
import yaml from "highlight.js/lib/languages/yaml";
import markdown from "highlight.js/lib/languages/markdown";
import diff from "highlight.js/lib/languages/diff";
import sql from "highlight.js/lib/languages/sql";

// Registered by hand: the full bundle is ~1 MB, these cover what an agent usually writes.
const languages = { python, javascript, typescript, json, bash, powershell, cpp, css, xml, yaml, markdown, diff, sql };
for (const [name, grammar] of Object.entries(languages)) hljs.registerLanguage(name, grammar);
const aliases: Record<string, string> = {
  py: "python", js: "javascript", ts: "typescript", tsx: "typescript", jsx: "javascript", sh: "bash", shell: "bash",
  ps1: "powershell", pwsh: "powershell", html: "xml", c: "cpp", cu: "cpp", cuda: "cpp", yml: "yaml", md: "markdown",
};

/** HTML of the highlighted code (hljs escapes the text itself), or null when the language is not known. */
export function highlight(code: string, language: string): string | null {
  const name = aliases[language.toLowerCase()] ?? language.toLowerCase();
  if (!hljs.getLanguage(name)) return null;
  return hljs.highlight(code, { language: name, ignoreIllegals: true }).value;
}

export const LANGUAGE_LABEL: Record<string, string> = {
  python: "Python", py: "Python", js: "JavaScript", javascript: "JavaScript", ts: "TypeScript", typescript: "TypeScript",
  json: "JSON", bash: "Bash", sh: "Shell", powershell: "PowerShell", ps1: "PowerShell", cpp: "C++", cuda: "CUDA", cu: "CUDA",
  css: "CSS", html: "HTML", xml: "XML", yaml: "YAML", yml: "YAML", md: "Markdown", markdown: "Markdown", diff: "Diff", sql: "SQL",
};
