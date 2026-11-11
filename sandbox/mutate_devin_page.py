"""Mutation checks for the page side of the Devin engine."""
from __future__ import annotations

import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
FE = ROOT / "frontend"
PY = str(ROOT / ".venv" / "Scripts" / "python.exe")
SRC = FE / "src"


def run(command, cwd, timeout=900):
    return subprocess.run(command, cwd=cwd, capture_output=True, text=True, timeout=timeout, shell=isinstance(command, str), encoding="utf-8", errors="replace")


def unit():
    result = run("npm run unit", FE)
    return result.returncode != 0, ""


def check():
    built = run("npm run build:app", FE)
    if built.returncode != 0:
        return False, "DID NOT BUILD"
    result = run("node scripts/check.mjs", FE)
    lines = [l for l in result.stdout.splitlines() if "FAIL" in l]
    return result.returncode != 0, lines[0] if lines else ""


def e2e():
    built = run("npm run build:app", FE)
    if built.returncode != 0:
        return False, "DID NOT BUILD"
    result = run([PY, "-m", "pytest", "-q", "-x", "-p", "no:cacheprovider", "tests/test_app_e2e.py", "-k", "devin"], ROOT)
    lines = [l for l in result.stdout.splitlines() if l.startswith("FAILED")]
    return result.returncode != 0, lines[0] if lines else ""


M = [
    ("a Devin source is a Droid one", SRC / "lib" / "models.ts", 'source === "devin" ? "devin" : "droid"', '"droid"', unit),
    ("Devin starts at the head of its list", SRC / "lib" / "vocab.ts", 'const START_LEVELS: Partial<Record<Engine, string>> = { devin: "code" };', "const START_LEVELS: Partial<Record<Engine, string>> = {};", unit),
    ("Devin's modes in another order", SRC / "lib" / "vocab.ts", '{ value: "ask", label: "问答", hint: "只回答问题，不改代码" },\n    { value: "plan", label: "计划", hint: "先出方案，批准后再动手" },', '{ value: "plan", label: "计划", hint: "先出方案，批准后再动手" },\n    { value: "ask", label: "问答", hint: "只回答问题，不改代码" },', unit),
    ("the Devin account is not taken from accounts events", SRC / "liveReducer.ts", "        ...(e.devin ? { devin: e.devin } : {}),\n", "", unit),
    ("Devin's path is not read from the settings", SRC / "liveReducer.ts", 'devinPath: v.devinPath ?? "",', 'devinPath: "",', unit),
    ("the menu has no Devin section", SRC / "components" / "modelMenu.ts", "  if (i.devin.enabled) {", "  if (!i.devin.enabled) {", check),
    ("the accounts page has no Devin card", SRC / "components" / "ProvidersPage.tsx", "      <DevinCard />\n", "      {false && <DevinCard />}\n", check),
    ("the sidebar does not name Devin", SRC / "components" / "Sidebar.tsx", 'session.engine === "codex" || session.engine === "devin" ? session.engine : "factory"', 'session.engine === "codex" ? "codex" : "factory"', e2e),
    ("a Devin model does not bring Devin's levels in the mock", SRC / "store.ts", "settings.autonomy = startingLevel(engine);", "settings.autonomy = startingLevel(engine) && permissionsFor(engine)[0]!.value;", check),
    ("the Devin sign-in does not reach Python", SRC / "live.ts", 'const devin = await attempt(liveCall("devin.login"));', 'const devin = await attempt(liveCall("devin.account"));', e2e),
    ("the Devin sign-out does not reach Python", SRC / "live.ts", 'const devin = await attempt(liveCall("devin.logout"));', 'const devin = await attempt(liveCall("devin.account"));', e2e),
    ("the Devin program path goes to Codex's setting", SRC / "live.ts", 'setDevinPath: (path) => void setting("devinPath", path),', 'setDevinPath: (path) => void setting("codexPath", path),', e2e),
    ("a Devin login waits no longer than a call", SRC / "transport.ts", '"codex.login", "devin.login", "project.add"', '"codex.login", "project.add"', unit),
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
