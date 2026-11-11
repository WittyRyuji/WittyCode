"""Mutation checks for the page side of the follow-up / token-total work."""
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


def e2e(names):
    def go():
        built = run("npm run build:app", FE)
        if built.returncode != 0:
            return False, "DID NOT BUILD"
        result = run([PY, "-m", "pytest", "-q", "-x", "-p", "no:cacheprovider", "tests/test_app_e2e.py", "-k", names], ROOT)
        lines = [l for l in result.stdout.splitlines() if l.startswith("FAILED")]
        return result.returncode != 0, lines[0] if lines else ""

    return go


def check():
    built = run("npm run build:app", FE)
    if built.returncode != 0:
        return False, "DID NOT BUILD"
    result = run("node scripts/check.mjs", FE)
    lines = [l for l in result.stdout.splitlines() if "FAIL" in l]
    return result.returncode != 0, lines[0] if lines else ""


NEW_E2E = "enter_queues or taken_back_and_the_setting"
M = [
    ("restore puts the returned words in front", SRC / "liveReducer.ts", "!e.text.trim() || before.draft.includes(e.text) ? before.draft : `${e.text}\\n\\n${before.draft}`", "before.draft", unit),
    ("an empty queue list stays as a list", SRC / "liveReducer.ts", "queued && queued.length > 0 ? queued : undefined", "queued ?? undefined", unit),
    ("the MiMo presets lose the bearer key", SRC / "lib" / "vocab.ts", "  bearer: true,\n  models: MIMO_MODELS,", "  models: MIMO_MODELS,", unit),
    ("followUp is not read from the settings", SRC / "liveReducer.ts", 'followUp: v.followUp ?? "queue",', 'followUp: "queue",', unit),
    ("Ctrl+Enter does what Enter does", SRC / "components" / "Composer.tsx", "send(session.id, e.ctrlKey || e.metaKey ? otherHow(followUp) : followUp);", "send(session.id, followUp);", e2e(NEW_E2E)),
    ("the page ignores the key that was pressed", SRC / "live.ts", "{ how: how ?? get().followUp }", "{ how: how && get().followUp }", e2e(NEW_E2E)),
    ("withdrawing takes every waiting message", SRC / "components" / "Transcript.tsx", "onClick={() => unqueue(session.id, item.id)}", "onClick={() => unqueue(session.id)}", e2e(NEW_E2E)),
    ("the status bar has no total row", SRC / "components" / "StatusBar.tsx", '    ["合计", usageTotal(u).toLocaleString()],\n', "", check),
    ("the settings page has no follow-up switch", SRC / "components" / "Settings.tsx", "onChange={s.setFollowUp}", "onChange={() => undefined}", e2e("taken_back_and_the_setting")),
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
