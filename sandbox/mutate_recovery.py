"""Mutation checks for opening a conversation the application did not live to finish (``harness.store.recover`` and
``pair_calls``, what ``chatgpt.controller`` saves while a turn runs, how ``chatgpt.history`` shows such a turn).

    python sandbox/mutate_recovery.py            run every mutant
    python sandbox/mutate_recovery.py --check    only check that every pattern is found exactly once

Files are changed and put back as bytes (their line endings are kept). The tests run once unchanged first; if they fail
then (another change in progress), nothing is mutated.
"""
from __future__ import annotations

import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PY = str(ROOT / ".venv" / "Scripts" / "python.exe")
CORE = ROOT / "desktop" / "core"
STORE = CORE / "harness" / "store.py"
CONTROLLER = CORE / "chatgpt" / "controller.py"
HISTORY = CORE / "chatgpt" / "history.py"
TESTS = ["tests/test_harness_store.py", "tests/test_chatgpt_recovery_history.py", "tests/test_chatgpt_recovery_crash.py", "tests/test_chatgpt_controller.py"]

M = [
    ("pair: no settling before a message of the user", STORE, "waiting and (after_output or _is_user(item)):", "waiting and after_output:"),
    ("pair: no settling before the next step", STORE, "waiting and (after_output or _is_user(item)):", "waiting and _is_user(item):"),
    ("pair: an output without its call is kept", STORE, "            if _call_id(item) not in waiting:\n                changed = True\n                continue\n", "            if _call_id(item) not in waiting:\n                changed = True\n                out.append(item)\n                continue\n"),
    ("pair: a late output is not brought back", STORE, "            if index is not None and index not in used:\n", "            if False:\n"),
    ("pair: a valid history comes back as a copy", STORE, "    return (out, position) if changed else None\n", "    return (out, position)\n"),
    ("pair: an output before its call counts as its result", STORE, 'elif item.get("type") == "function_call_output" and _call_id(item) in called:', 'elif item.get("type") == "function_call_output":'),
    ("pair: an output used in place may be used again (only matters for repeated call ids)", STORE, "            del waiting[_call_id(item)]\n            used.add(index)\n", "            del waiting[_call_id(item)]\n"),
    ("recover: the turns that follow are not moved", STORE, "        start = position[turn.start] if 0 <= turn.start < count else turn.start\n", "        start = turn.start\n"),
    ("recover: a file of the earlier version is not recognised", STORE, '        if turn.status != "running" and turn.finished is not None:\n', '        if turn.status != "running":\n'),
    ("recover: EQUIVALENT a running turn is known only by its missing end", STORE, '        if turn.status != "running" and turn.finished is not None:\n', "        if turn.finished is not None:\n"),
    ("recover: the turn stays as it was", STORE, '        turn.status = "interrupted"\n', ""),
    ("recover: the note does not count the unknown results", STORE, "        turn.note = CUT_OFF_UNKNOWN.format(count=unknown) if unknown else CUT_OFF\n", "        turn.note = CUT_OFF\n"),
    ("recover: no note", STORE, "        turn.note = CUT_OFF_UNKNOWN.format(count=unknown) if unknown else CUT_OFF\n", '        turn.note = ""\n'),
    ("recover: every output counts as unknown", STORE, ' and item.get("output") == LOST)\n', ")\n"),
    ("recover: the turn ends where it began", STORE, "        turn.finished = max(turn.at, following.at if following is not None else session.modified_at)\n", "        turn.finished = turn.at\n"),
    ("recover: the turn always ends at the last save", STORE, "        turn.finished = max(turn.at, following.at if following is not None else session.modified_at)\n", "        turn.finished = max(turn.at, session.modified_at)\n"),
    ("recover: says nothing changed", STORE, "        changed = True\n    return changed\n", "        changed = True\n    return False\n"),
    ("store: the note is not written", STORE, '"model": t.model, "note": t.note}', '"model": t.model}'),
    ("store: the note is not read", STORE, ', str(t.get("model") or ""), str(t.get("note") or ""))', ', str(t.get("model") or ""))'),
    ("controller: a new turn is saved as done", CONTROLLER, 'SavedTurn(self._turn_start, time.time(), None, "running", "", model)', 'SavedTurn(self._turn_start, time.time(), None, "done", "", model)'),
    ("controller: the save writes calls without outputs", CONTROLLER, "        return paired(saved)\n", "        return saved\n"),
    ("controller: the save pairs the loop's own history", CONTROLLER, "        return paired(saved)\n", "        pair_in_place(saved)\n        return saved\n"),
    ("controller: opening does not recover", CONTROLLER, "        recovered = recover(saved)\n", "        recovered = False\n"),
    ("controller: the repair is not saved", CONTROLLER, "                self.manager.sessions.save(saved)\n", "                pass\n"),
    ("controller: the step limit is saved as done", CONTROLLER, '{"step_limit": "limit", "incomplete": "incomplete"}.get(end.reason, "done")', '{"incomplete": "incomplete"}.get(end.reason, "done")'),
    ("controller: an answer cut short is saved as done", CONTROLLER, '{"step_limit": "limit", "incomplete": "incomplete"}.get(end.reason, "done")', '{"step_limit": "limit"}.get(end.reason, "done")'),
    ("controller: the note is not recorded", CONTROLLER, "record.finished, record.status, record.error, record.note = time.time(), status, error, note", "record.finished, record.status, record.error = time.time(), status, error"),
    ("controller: no save before a command", CONTROLLER, "            self.ctl._save_pending()  #", "            pass  #"),
    ("controller: a save before every tool, reads too", CONTROLLER, '        if spec is None or spec.kind != "read":\n            self.ctl._save_pending()', "        if True:\n            self.ctl._save_pending()"),
    ("history: a lost result is shown as done", HISTORY, "    if output == LOST:\n        return ToolStatus.CANCELLED\n", ""),
    ("history: a running turn is shown as finished", HISTORY, '    "running": TurnStatus.INTERRUPTED,\n', ""),
    ("history: the note is not shown", HISTORY, '        turn.blocks.append(NoticeBlock(id=transcript.new_id("b"), text=meta.note))\n', "        pass\n"),
]


def pytest(*extra: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run([PY, "-m", "pytest", "-q", "-x", "-p", "no:cacheprovider", *extra, *TESTS], cwd=ROOT, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=900)


def main() -> int:
    check_only = "--check" in sys.argv[1:]
    survivors = []
    bad_patterns = []
    for number, (name, path, old, new) in enumerate(M, 1):
        data = path.read_bytes()
        newline = "\r\n" if b"\r\n" in data else "\n"
        count = data.decode("utf-8").count(old.replace("\n", newline))
        if count != 1:
            print(f"[{number}] {name}: PATTERN FOUND {count} TIMES", flush=True)
            bad_patterns.append(name)
    if bad_patterns or check_only:
        print("patterns:", "all found once" if not bad_patterns else f"{len(bad_patterns)} wrong")
        return 1 if bad_patterns else 0
    baseline = pytest()
    if baseline.returncode != 0:
        print("the tests fail without any mutation; nothing mutated:")
        print("\n".join(baseline.stdout.strip().splitlines()[-15:]))
        return 2
    for number, (name, path, old, new) in enumerate(M, 1):
        data = path.read_bytes()
        newline = "\r\n" if b"\r\n" in data else "\n"
        text = data.decode("utf-8")
        mutated = text.replace(old.replace("\n", newline), new.replace("\n", newline))
        started = time.time()
        try:
            path.write_bytes(mutated.encode("utf-8"))
            done = pytest()
            caught = done.returncode != 0
            lines = done.stdout.strip().splitlines()
            failed = next((line for line in lines if line.startswith("FAILED") or line.startswith("ERROR")), lines[-1] if lines else "?")
        except subprocess.TimeoutExpired:
            caught, failed = True, "timeout (hangs: caught)"
        finally:
            path.write_bytes(data)
        print(f"[{number}] {'caught  ' if caught else 'SURVIVED'} {name}  ({time.time() - started:.0f}s) {failed if caught else ''}", flush=True)
        if not caught:
            survivors.append(name)
    print()
    print("survivors:" if survivors else "all mutants caught")
    for name in survivors:
        print("  -", name)
    return 0


if __name__ == "__main__":
    sys.exit(main())
