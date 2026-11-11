"""Mutation checks for the bare-name environment-variable field."""
from __future__ import annotations

import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
FE = ROOT / "frontend"
SRC = FE / "src"


def run(command, cwd, timeout=900):
    return subprocess.run(command, cwd=cwd, capture_output=True, text=True, timeout=timeout, shell=True, encoding="utf-8", errors="replace")


def unit():
    return run("npm run unit", FE).returncode != 0, ""


def check():
    built = run("npm run build:app", FE)
    if built.returncode != 0:
        return False, "DID NOT BUILD"
    result = run("node scripts/check.mjs", FE)
    lines = [l for l in result.stdout.splitlines() if "FAIL" in l]
    return result.returncode != 0, lines[0] if lines else ""


MODELS = SRC / "lib" / "models.ts"
M = [
    ("a pasted wrapper is wrapped twice", MODELS, 'const name = envVarText(typed);\n  return name ?', 'const name = typed.trim();\n  return name ?', unit),
    ("the stored form loses its braces", MODELS, 'return name ? `\\${${name}}` : "";', 'return name;', unit),
    ("nothing typed stores an empty reference", MODELS, 'return name ? `\\${${name}}` : "";', 'return `\\${${name}}`;', unit),
    ("the field text keeps the opening wrapper", MODELS, '.replace(/^\\$\\{/, "")', '', unit),
    ("the field text keeps the closing brace", MODELS, '.replace(/\\}$/, "")', '', unit),
    ("the editor shows the stored reference", SRC / "components" / "ProviderEditor.tsx", "value={envVarText(d.keyHint)}", "value={d.keyHint + envVarText('')}", check),
    ("the editor stores what was typed", SRC / "components" / "ProviderEditor.tsx", 'onChange={(v) => set("keyHint", envHintOf(v))}', 'onChange={(v) => set("keyHint", v + envHintOf(""))}', check),
    ("the list shows the stored reference", SRC / "components" / "ProvidersPage.tsx", "`环境变量 ${envVarText(p.keyHint)}", "`环境变量 ${p.keyHint + envVarText('')}", check),
]

only = sys.argv[1:]
survivors = []
for number, (name, path, old, new, test) in enumerate(M, 1):
    if only and not any(word in name for word in only):
        continue
    text = path.read_text(encoding="utf-8")
    if text.count(old) != 1:
        print(f"[{number}] {name}: PATTERN FOUND {text.count(old)} TIMES", flush=True)
        survivors.append(name)
        continue
    path.write_text(text.replace(old, new), encoding="utf-8")
    started = time.time()
    try:
        caught, detail = test()
    finally:
        path.write_text(text, encoding="utf-8")
    verdict = "caught " if caught and detail != "DID NOT BUILD" else ("DID NOT BUILD" if detail == "DID NOT BUILD" else "SURVIVED")
    print(f"[{number}] {verdict} {name} ({time.time() - started:.0f}s) {detail}", flush=True)
    if verdict != "caught ":
        survivors.append(name)
built = run("npm run build:app", FE)
print("clean page rebuilt:", built.returncode == 0)
print("survivors:", survivors or "none")
