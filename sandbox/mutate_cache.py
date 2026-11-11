"""Mutation checks for the cache work (rollout reading, settings while connecting, reading sessions ahead): each change
to the source must make some test fail (a mutant that does not even import is not a catch)."""
from __future__ import annotations

import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PY = str(ROOT / ".venv" / "Scripts" / "python.exe")
C = ROOT / "desktop" / "core"
ROLL = C / "codex" / "rollout.py"
CCTL = C / "codex" / "controller.py"
CENG = C / "codex" / "engine.py"
SM = C / "session_manager.py"
APP = C / "app_core.py"
TESTS = ["tests/test_codex_rollout.py", "tests/test_app_warm.py", "tests/test_app_cache.py"]
E2E = ["tests/test_app_e2e.py", "-k", "nobody_has_opened or from_the_cache"]

M = [
    ("rollout: a line cut by a block boundary is lost", ROLL, "            carry = first  # (its start is in the block before this one)", "            carry = b''"),
    ("rollout: the first line of the file is not read", ROLL, "        if position == 0 and carry.strip():", "        if False:"),
    ("rollout: the whole file is scanned", ROLL, "while position > 0 and scanned < SCAN_LIMIT_BYTES:", "while position > 0:"),
    ("rollout: the names stay in snake_case", ROLL, '    return head + "".join(part.capitalize() for part in rest)', "    return name"),
    ("rollout: a token_count without info is read", ROLL, "    if not isinstance(info, Mapping):\n        return None\n    total,", "    info = info or {}\n    total,"),
    ("rollout: the permission is not read", ROLL, "normalize_level(kind))", "None)"),
    ("rollout: the oldest figures win", ROLL, "            if usage is None and _TOKEN_COUNT in line:", "            if _TOKEN_COUNT in line:"),
    ("controller: a resumed thread does not look in its file", CCTL, "        await self._learn_saved_usage(result)\n", "        pass\n"),
    ("controller: the saved figures are not applied", CCTL, '            self._on_token_usage({"tokenUsage": facts.token_usage})', "            pass"),
    ("engine: the rollout is not read ahead", CENG, "        facts = await asyncio.to_thread(read_rollout, thread.get(\"path\"))", "        facts = None"),
    ("engine: the reasoning level of the last turn is not used", CENG, 'reasoning = str(thread.get("reasoningEffort") or (facts.effort if facts else "") or "")', 'reasoning = str(thread.get("reasoningEffort") or "")'),
    ("engine: no context figure", CENG, '            peek.context_used = last_call_tokens(usage.get("last"))', "            peek.context_used = None"),
    ("engine: the window is not learned", CENG, "                self._learn_window(model, peek.window)", "                pass"),
    ("engine: an unreadable thread is an error", CENG, '            log.info("could not read codex thread %s: %s", thread_id, error)\n            return None', '            log.info("could not read codex thread %s: %s", thread_id, error)\n            raise'),
    ("manager: no context figure from the last call", SM, "    used = None\n    if last is not None:", "    used = None\n    if False:"),
    ("manager: the permission is read from the wrong field", SM, "autonomy=_enum_value(settings.autonomy_level),", "autonomy=_enum_value(settings.reasoning_effort),"),
    ("manager: a lost process is asked again", SM, "                    if isinstance(error, CONNECTION_LOST):\n                        return", "                    if isinstance(error, CONNECTION_LOST):\n                        pass"),
    ("manager: the settings are not kept with the history", SM, "                self.saved_settings = snapshot.settings", "                pass"),
    ("manager: the context is not kept with the history", SM, "                if snapshot.context_used and self.context.used is None:", "                if False:"),
    ("app: a copy is fresh a long time after its session", APP, ">= modified - 5", ">= modified - 5000000"),
    ("app: a copy without usage is complete", APP, '    return bool(meta.get("settings")) and bool(meta.get("usage")) and', '    return bool(meta.get("settings")) and'),
    ("app: a copy without a context figure is complete", APP, " and isinstance(context, Mapping) and context.get(\"used\") is not None", " and isinstance(context, Mapping)"),
    ("app: the choice replaces what was shown", APP, "        return {**left, **conv.requested}", "        return conv.requested or left"),
    ("app: what the engine file says is not used", APP, '        saved = getattr(conv.controller, "saved_settings", None)\n        left =', '        saved = None\n        left ='),
    ("app: every session is read ahead", APP, "[:WARM_ROWS]\n        wanted", "\n        wanted"),
    ("app: the read ahead overwrites the tab's copy", APP, "if peek is not None and row.id not in self._open_rows() | self._touched and not self._shutting_down:", "if peek is not None and row.id not in self._open_rows() and not self._shutting_down:"),
    ("app: the tab's copy is not noted", APP, "        self._touched.add(row_id(conv.engine, native))\n", "        pass\n"),
    ("app: an open session is read ahead", APP, "or self._warmed.get(row.id) == stamp or row.id in self._open_rows() | self._touched:", "or self._warmed.get(row.id) == stamp:"),
    ("app: the total starts again", APP, "        ledger = self._ledger_of(old) if old else UsageLedger()", "        ledger = UsageLedger()"),
    ("app: the engine's figures are not counted", APP, '            ledger.feed({"self": peek.usage})', "            pass"),
    ("app: the later time is lost", APP, "max(row.modified.timestamp(), kept_active),", "row.modified.timestamp(),"),
    ("app: an empty conversation is cached", APP, "        turns = peek.transcript.turns\n        if not turns:\n            return None", "        turns = peek.transcript.turns"),
    ("app: a failed read is asked again at once", APP, "        self._warmed[row.id] = stamp  # (one try", "        pass  # (one try"),
    ("app: a stale copy is complete", APP, "if old is not None and _is_complete(old) and _is_fresh(old, stamp):", "if old is not None and _is_complete(old):"),
    ("app: the learned window is not used", APP, "window = peek.window or self._windows.get(model_id) or int(", "window = peek.window or int("),
    ("app: the windows are not read back", APP, "        if isinstance(windows, dict):\n            self._windows =", "        if False:\n            self._windows ="),
    ("app: a window of another model is taken", APP, '        if served and self.catalog.page_id(conv.engine, served) != model_id:\n            return', "        if False:\n            return"),
    ("app: a cached window replaces a live one", APP, "if not model_id or model_id in self._windows or isinstance(window, bool)", "if not model_id or isinstance(window, bool)"),
    ("app: a cached copy's window is not learned", APP, "            if old is not None:\n                self._learn_window_from(old)", "            if old is not None:\n                pass"),
    ("app: a level that is not known is made up", APP, "settings = {name: value for name, value in (self._settings_of(row.engine, peek.settings) if peek.settings else {}).items() if value}", "settings = dict(self._settings_of(row.engine, peek.settings) if peek.settings else {})"),
    ("app: reading ahead ignores the switch", APP, "if not (self.read_ahead and self.cache.enabled) or self.offline", "if not self.cache.enabled or self.offline"),
]

only = sys.argv[1:]
survivors = []
for number, (name, path, old, new) in enumerate(M, 1):
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
        done = subprocess.run([PY, "-m", "pytest", "-q", "-x", "-p", "no:cacheprovider", *TESTS], cwd=ROOT, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=900)
        if done.returncode == 0:  # the quick tests let it through: the whole-window ones get their say
            done = subprocess.run([PY, "-m", "pytest", "-q", "-x", "-p", "no:cacheprovider", *E2E], cwd=ROOT, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=900)
    finally:
        path.write_text(text, encoding="utf-8")
    lines = [line for line in done.stdout.splitlines() if line.startswith("FAILED") or "Error" in line[:40]]
    broke = "ImportError" in done.stdout or "SyntaxError" in done.stdout or "collected 0 items" in done.stdout
    verdict = "caught " if done.returncode != 0 and not broke else ("DID NOT RUN" if broke else "SURVIVED")
    print(f"[{number}] {verdict} {name} ({time.time() - started:.0f}s) {lines[0] if lines else ''}", flush=True)
    if verdict != "caught ":
        survivors.append(name)
print("survivors:", survivors or "none")
