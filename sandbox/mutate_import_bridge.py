"""Mutation checks for the import wizard's application side: ``core/import_bridge.py`` and AppCore's ``import.*``.

    python sandbox/mutate_import_bridge.py            run every mutant
    python sandbox/mutate_import_bridge.py --check    only check that every pattern is found exactly once

The importers themselves (reading Claude Code, Codex, Devin and Factory files) have their own script,
``sandbox/mutate_importers.py``.
"""
from __future__ import annotations

import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PY = str(ROOT / ".venv" / "Scripts" / "python.exe")
BRIDGE = ROOT / "desktop" / "core" / "import_bridge.py"
APP = ROOT / "desktop" / "core" / "app_core.py"
TESTS = ["tests/test_import_bridge.py", "tests/test_app_import.py", "tests/test_app_memory_curate.py"]

M = [
    ("notes: the same text the memory holds is shown once per file", BRIDGE,
     "                if candidate.digest not in seen:  # (the memory holds this text once, however many files say it)\n"
     "                    seen.add(candidate.digest)\n"
     "                    items.append(self._memory_json(candidate, True))\n                continue\n", ""),
    ("notes: the memory's own text is offered again", BRIDGE,
     "            if candidate.digest in have:\n                if candidate.digest not in seen:", "            if False:\n                if candidate.digest not in seen:"),
    ("notes: the names are not made unique", BRIDGE,
     "        planned = {candidate.key: candidate for candidate in tidy(fresh, [(entry.scope, entry.project, entry.title) for entry in existing])}",
     "        planned = {candidate.key: candidate for candidate in fresh}"),
    ("notes: a confirmed note keeps the name the memory would refuse", BRIDGE,
     "                    title=kept.title, text=kept.text,", "                    title=candidate.title, text=kept.text,"),
    ("notes: a confirmed note says it was written by the user", BRIDGE, "                    by=agent(kept.app),", '                    by="user",'),
    ("notes: undo forgets nothing", BRIDGE, "                self.store.forget(note_id, by=\"user\")", "                pass"),
    ("preview: every app is read, ticked or not", BRIDGE,
     "    chosen = tuple(name for name in value if name in APPS)\n    return chosen or APPS", "    return APPS"),
    ("scan: no count of what an app brings", BRIDGE,
     "                entry[\"memories\"] = len(self._read(lambda s: s.memories(), source))", "                entry[\"memories\"] = 0"),
    ("scan: a locked app is not said", BRIDGE,
     "            problem = next(iter(source.report.problems), \"\")\n            if problem:", "            problem = \"\"\n            if problem:"),
    ("sessions: a conversation with no project is saved out of sight", BRIDGE, "            if not info.project:", "            if False:"),
    ("sessions: an imported conversation is not marked as imported", BRIDGE,
     '            "imported": self.sessions is not None and self.sessions.load(saved_id(info.key)) is not None,', '            "imported": False,'),
    ("sessions: the conversation's own settings are not given", BRIDGE,
     "            conversation = to_saved_session(info, messages, **settings)",
     '            conversation = to_saved_session(info, messages, model="", reasoning="", autonomy="")'),
    # ---- AppCore
    ("app: the wizard is never there", APP,
     "        if self.memory is not None:\n            self.imports = ImportBridge(", "        if False:\n            self.imports = ImportBridge("),
    ("app: the imported conversation's project is not added to the sidebar", APP,
     "            added = self.settings.add_project(folder) or added", "            added = added"),
    ("app: the memory state is not returned with the import", APP,
     "        for folder in projects:\n            self._schedule_sidebar(folder, reload=True)  # (the new conversation is in the store: read the folder again)\n        return {**result, **self._no_memory().state()}",
     "        for folder in projects:\n            self._schedule_sidebar(folder, reload=True)  # (the new conversation is in the store: read the folder again)\n        return result"),
    ("app: the sidebar is not read again after an import", APP,
     "            self._schedule_sidebar(folder, reload=True)", "            self._schedule_sidebar(folder)"),
    ("app: an engine that cannot be asked takes the allowance", APP,
     "                    and self._can_ask(engine)\n", "                    and True\n"),
]

args = sys.argv[1:]
check_only = "--check" in args
survivors = []
for number, (name, path, old, new) in enumerate(M, 1):
    text = path.read_text(encoding="utf-8")
    if text.count(old) != 1:
        print(f"[{number}] {name}: PATTERN FOUND {text.count(old)} TIMES", flush=True)
        survivors.append(name)
        continue
    if check_only:
        continue
    path.write_text(text.replace(old, new), encoding="utf-8")
    started = time.time()
    try:
        done = subprocess.run([PY, "-m", "pytest", "-q", "-x", "-p", "no:cacheprovider", *TESTS], cwd=ROOT, capture_output=True, text=True,
                              encoding="utf-8", errors="replace", timeout=600)
        caught = done.returncode != 0
        tail = (done.stdout.strip().splitlines() or ["?"])[-1]
    except subprocess.TimeoutExpired:
        caught, tail = True, "timeout (hangs: caught)"
    finally:
        path.write_text(text, encoding="utf-8")
    print(f"[{number}] {'caught  ' if caught else 'SURVIVED'} {name}  ({time.time() - started:.0f}s) {tail if not caught or 'error' in tail.lower() else ''}",
          flush=True)
    if not caught:
        survivors.append(name)

print(f"\n{len(M) - len(survivors)} of {len(M)} caught" if not check_only else f"{len(M) - len(survivors)} of {len(M)} patterns found once")
for name in survivors:
    print("  survivor:", name)
sys.exit(1 if survivors else 0)
