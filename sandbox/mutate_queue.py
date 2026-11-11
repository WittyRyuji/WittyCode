"""Mutation checks for changing a waiting message and sending it now (AppCore.session.editQueued / sendQueued).

    python sandbox/mutate_queue.py            run every mutant
    python sandbox/mutate_queue.py --check    only check that every pattern is found exactly once
"""
from __future__ import annotations

import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PY = str(ROOT / ".venv" / "Scripts" / "python.exe")
APP = ROOT / "desktop" / "core" / "app_core.py"
TESTS = ["tests/test_app_follow_up.py"]
E2E = ["tests/test_app_e2e.py", "-k", "looked_at_changed"]

M = [
    ("edit: the words are not trimmed", APP, "        text = text.strip()\n        if not text and not item.attachments:\n            raise AppError(\"消息不能是空的", "        if not text and not item.attachments:\n            raise AppError(\"消息不能是空的"),
    ("edit: an empty message is accepted", APP, "        if not text and not item.attachments:\n            raise AppError(\"消息不能是空的", "        if False:\n            raise AppError(\"消息不能是空的"),
    ("edit: the message loses its place", APP, "        conv.queue[index] = dataclasses.replace(item, text=text)", "        conv.queue.pop(index)\n        conv.queue.append(dataclasses.replace(item, text=text))"),
    ("edit: the attachments are dropped", APP, "        conv.queue[index] = dataclasses.replace(item, text=text)", "        conv.queue[index] = dataclasses.replace(item, text=text, attachments=())"),
    ("edit: the page is not told", APP, "        conv.queue[index] = dataclasses.replace(item, text=text)\n        conv.dirty = True\n        self._schedule_flush()", "        conv.queue[index] = dataclasses.replace(item, text=text)"),
    ("edit: a message that has gone is an error", APP, "        if index is None:\n            return {\"ok\": False}  # (it went out", "        if index is None:\n            raise AppError(\"gone\")  # (it went out"),
    ("send now: the message is not taken out of the line", APP, "            conv.queue.remove(item)  # (out of the queue first", "            pass  # (out of the queue first"),
    ("send now: a refused steer loses the message", APP, "            conv.queue.insert(min(index, len(conv.queue)), item)\n            self.say(", "            self.say("),
    ("send now: a refused steer puts it at the end", APP, "            conv.queue.insert(min(index, len(conv.queue)), item)\n            self.say(", "            conv.queue.append(item)\n            self.say("),
    ("send now: a turn that ended on the way loses the message", APP, "                conv.queue.insert(0, item)  # (the turn ended on the way", "                pass  # (the turn ended on the way"),
    ("send now: a refused steer is not said", APP, "            self.say(f\"没能引导本轮：{outcome.message if outcome else '这个会话不支持'}。这条还在排队，本轮结束后发出。\")", "            pass"),
    ("send now: a connecting session is not told", APP, "        self.say(\"会话还没连上：连上之后这条会自动发出。\")", "        pass"),
    ("send now: a gone message is an error", APP, "        item = next((m for m in conv.queue if m.id == wanted), None)\n        if item is None:\n            return {\"ok\": False}", "        item = next((m for m in conv.queue if m.id == wanted), None)\n        if item is None:\n            raise AppError(\"gone\")"),
    ("send now: the steer is not tried", APP, "            outcome = await steer(item.text, item.attachments) if steer is not None else None\n            if outcome is not None and outcome.ok:\n                conv.dirty = True", "            outcome = None\n            if outcome is not None and outcome.ok:\n                conv.dirty = True"),
]

args = [a for a in sys.argv[1:]]
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
        done = subprocess.run([PY, "-m", "pytest", "-q", "-x", "-p", "no:cacheprovider", *TESTS], cwd=ROOT, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=900)
        if done.returncode == 0:
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
