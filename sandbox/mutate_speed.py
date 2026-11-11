"""Mutation checks for the TPS figure (timing in the transcript, core/speed.py, the controllers' output probe, the session
meta and its cache): each change to the source must make some test fail.

    python sandbox/mutate_speed.py            run every mutant
    python sandbox/mutate_speed.py probe      only those whose name contains "probe"
    python sandbox/mutate_speed.py --check    only check that every pattern is found exactly once
"""
from __future__ import annotations

import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PY = str(ROOT / ".venv" / "Scripts" / "python.exe")
C = ROOT / "desktop" / "core"
TR = C / "transcript.py"
SPEED = C / "speed.py"
USAGE = C / "usage.py"
APP = C / "app_core.py"
SM = C / "session_manager.py"
CODEX = C / "codex" / "controller.py"
DEVIN = C / "devin" / "controller.py"
CHATGPT = C / "chatgpt" / "controller.py"
TESTS = [
    "tests/test_speed.py",
    "tests/test_chatgpt_controller.py",
    "tests/test_app_chatgpt.py",
    "tests/test_codex_steering.py",
    "tests/test_devin_turns.py",
    "tests/test_session_manager.py",
    "tests/test_codex_transcript.py",
]
E2E = ["tests/test_app_e2e.py", "-k", "speed"]

M = [
    # ------------------------------------------------------------------ the timing
    ("timing: waiting is the model's time", TR, "        if turn.blocked:\n            turn.blocked_seconds +=", "        if False:\n            turn.blocked_seconds +="),
    ("timing: a question to the user is not waiting [EQUIVALENT alone: its tool is awaiting permission as well]", TR, "or any(\n            prompt.status is PromptStatus.PENDING for prompt in turn.prompts\n        )", "or False"),
    ("timing: a tool awaiting permission is not waiting [EQUIVALENT alone: its prompt is pending as well]", TR, "item.status in (ToolStatus.RUNNING, ToolStatus.AWAITING_PERMISSION)", "item.status in (ToolStatus.RUNNING,)"),
    ("timing: neither a question nor a tool awaiting permission is waiting", TR, "turn.blocked = any(item.status in (ToolStatus.RUNNING, ToolStatus.AWAITING_PERMISSION) for item in turn.tools.values()) or any(\n            prompt.status is PromptStatus.PENDING for prompt in turn.prompts\n        )", "turn.blocked = any(item.status in (ToolStatus.RUNNING,) for item in turn.tools.values())"),
    ("timing: a tool at work is not waiting", TR, "item.status in (ToolStatus.RUNNING, ToolStatus.AWAITING_PERMISSION)", "item.status in (ToolStatus.AWAITING_PERMISSION,)"),
    ("timing: the last stretch of waiting is lost", TR, "        self._tick(turn)  # (while it still runs: the last stretch of waiting is counted)\n", ""),
    ("timing: the clock of the turn is not started", TR, "        turn.ticked_at = turn.started_at\n", ""),
    ("timing: the model's time is the whole turn", TR, "turn.model_seconds = max(0.0, duration - turn.blocked_seconds)", "turn.model_seconds = duration"),
    ("first output: text does not count", TR, "            turn.blocks.append(block)\n            self._note_output(turn)", "            turn.blocks.append(block)"),
    ("first output: thinking does not count", TR, "            turn.group_of[entry.id] = group\n            self._note_output(turn)", "            turn.group_of[entry.id] = group"),
    ("first output: a tool call does not count", TR, "            self._items[item.id] = item\n            self._note_output(turn)", "            self._items[item.id] = item"),
    ("first output: the latest, not the first", TR, "        if turn.first_output_at is None and not turn.from_history:", "        if not turn.from_history:"),
    # ------------------------------------------------------------------ the output
    ("output: the turn's own figure is not preferred", TR, "        if isinstance(written, int) and written > 0:\n            return written", "        if False:\n            return written"),
    ("output: an unknown start is trusted", TR, "        if probe is None or not turn.out_trusted:", "        if probe is None:"),
    ("output: every start is trusted", TR, "turn.out_trusted = turn.out_base is not None or first", "turn.out_trusted = True"),
    ("output: no start is trusted", TR, "turn.out_trusted = turn.out_base is not None or first", "turn.out_trusted = False"),
    ("output: the past is part of the turn", TR, "        base = turn.out_base or 0", "        base = 0"),
    ("output: a total that did not grow is a figure", TR, "return now - base if now is not None and now > base else None", "return now - base if now is not None else None"),
    ("output: the start is not read", TR, "                turn.out_base = self.output_probe()", "                turn.out_base = None"),
    ("output: a failing probe ends the turn", TR, "        try:\n            now = probe()\n        except Exception:\n            return None", "        now = probe()"),
    ("output: a sub-agent's tokens are the session's", USAGE, 'for key, tokens in series.items() if not key.startswith("agent:")]', "for key, tokens in series.items()]"),
    # ------------------------------------------------------------------ which turn tells and what it says
    ("speed: an interrupted turn tells", SPEED, "    if turn.status is not TurnStatus.DONE or turn.from_history:", "    if turn.from_history:"),
    ("speed: a few tokens tell", SPEED, "tokens < MIN_TOKENS or seconds < MIN_SECONDS", "tokens < 0 or seconds < MIN_SECONDS"),
    ("speed: a moment tells", SPEED, "tokens < MIN_TOKENS or seconds < MIN_SECONDS", "tokens < MIN_TOKENS or seconds < 0"),
    ("speed: the first wait is wrong", SPEED, "first = max(0.0, turn.first_output_at - turn.started_at)", "first = turn.first_output_at - turn.started_at + 1"),
    ("speed: the speed without the wait is the plain one", SPEED, "gen_tps=tokens / generating if generating is not None and generating >= MIN_GENERATING_SECONDS else None,", "gen_tps=tokens / seconds,"),
    ("speed: any time left is enough to divide by", SPEED, "generating >= MIN_GENERATING_SECONDS", "generating > 0"),
    ("speed: the oldest turn is shown", SPEED, "    for turn in reversed(turns):", "    for turn in turns:"),
    ("speed: the figure is not rounded", SPEED, '"tps": round(self.tps, 1),', '"tps": self.tps,'),
    ("speed: the time is off", SPEED, "        tps=tokens / seconds,", "        tps=tokens / (seconds + 1),"),
    # ------------------------------------------------------------------ the engines and the page
    ("engines: Codex does not give the count", CODEX, "        self.transcript.output_probe = self._output_so_far\n", ""),
    ("engines: Devin does not give the count", DEVIN, "        self.transcript.output_probe = self._output_so_far\n", ""),
    ("engines: ChatGPT Plan does not give the count", CHATGPT, "        self.transcript.output_probe = self._output_so_far\n", ""),
    ("engines: Droid has no count to fall back on", SM, "        self.transcript.output_probe = self._output_so_far\n", ""),
    ("app: the speed is never in the session", APP, "        found = speed_of(controller.transcript.turns)", "        found = None"),
    ("app: the cached speed is forgotten", APP, 'speed = found.to_json() if found is not None else cached.get("speed")', "speed = found.to_json() if found is not None else None"),
    ("app: the speed is not cached", APP, '{name: meta[name] for name in ("settings", "usage", "speed", "context")}', '{name: meta[name] for name in ("settings", "usage", "context")}'),
    ("app: a new speed does not mark the cache [EQUIVALENT: the usage changes in the same flush]", APP, 'if "usage" in patch or "context" in patch or "speed" in patch:', 'if "usage" in patch or "context" in patch:'),
]

args = [a for a in sys.argv[1:]]
check_only = "--check" in args
only = [a for a in args if a != "--check"]
survivors = []
for number, (name, path, old, new) in enumerate(M, 1):
    if only and not any(word in name for word in only):
        continue
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
        done = subprocess.run([PY, "-m", "pytest", "-q", "-x", "-p", "no:cacheprovider", *TESTS], cwd=ROOT, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=900)
        if done.returncode == 0:  # the quick tests let it through: the whole-window one gets its say
            done = subprocess.run([PY, "-m", "pytest", "-q", "-x", "-p", "no:cacheprovider", *E2E], cwd=ROOT, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=900)
        caught = done.returncode != 0
        tail = (done.stdout.strip().splitlines() or ["?"])[-1]
    except subprocess.TimeoutExpired:
        caught, tail = True, "timeout (hangs: caught)"
    finally:
        path.write_text(text, encoding="utf-8")
    print(f"[{number}] {'caught  ' if caught else 'SURVIVED'} {name}  ({time.time() - started:.0f}s) {tail if not caught or 'error' in tail.lower() else ''}", flush=True)
    if not caught:
        survivors.append(name)

print()
print("survivors:" if survivors else "all mutants caught")
for name in survivors:
    print("  -", name)
