"""Mutation checks for the follow-up / token-total work: each mutant must make its tests fail (then the file is restored)."""
from __future__ import annotations

import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PY = str(ROOT / ".venv" / "Scripts" / "python.exe")
CORE = ROOT / "desktop" / "core"
ALL = ["tests/test_usage.py", "tests/test_steering.py", "tests/test_codex_steering.py", "tests/test_app_follow_up.py", "tests/test_codex_history.py", "tests/test_app_cache.py", "tests/test_codex_agents.py"]

M = []


def mutant(name, file, old, new, tests=None):
    M.append((name, Path(file), old, new, tests or ALL))


U = CORE / "usage.py"
A = CORE / "app_core.py"
S = CORE / "session_manager.py"
T = CORE / "transcript.py"
C = CORE / "codex" / "controller.py"
H = CORE / "codex" / "history.py"
ST = CORE / "settings.py"
V = CORE / "viewmodel.py"

mutant("since never says the series started over", U, "if min(parts) < 0 or (write is not None and write < 0):", "if False:")
mutant("a new series is counted as zero", U, "added = now if earlier is None else (now.since(earlier) or now)", "added = Tokens() if earlier is None else (now.since(earlier) or now)")
mutant("restart forgets nothing", U, "        self.seen.clear()", "        pass")
mutant("peak keeps this cache-read", U, "            max(self.cache_read_tokens, other.cache_read_tokens),\n        )\n\n    def plus", "            self.cache_read_tokens,\n        )\n\n    def plus")
mutant("feeding goes on while switching", A, "        if conv.switching:  # (the figures", "        if False:  # (the figures")
mutant("a failed switch does not restore the ledger", A, "            conv.ledger.total, conv.ledger.seen = ledger_before", "            pass")
mutant("a switch does not restart the series", A, "        conv.ledger.restart()\n        transcript.usage = None", "        transcript.usage = None")
mutant("a cache from before ledgers is not migrated", A, 'return UsageLedger(counted, {} if counted is None else {"self": counted})', "return UsageLedger(counted, {})")
mutant("the ledger only from a fresh cache", A, "return snapshot, (snapshot if fresh else None)", "return (snapshot if fresh else None), (snapshot if fresh else None)")
mutant("a stale cache is shown", A, "return snapshot, (snapshot if fresh else None)", "return snapshot, snapshot")
mutant("the snapshot has no ledger", A, '            "ledger": conv.ledger.to_json(),\n', "")
mutant("a failed turn still sends the queue", A, "if conv.queue and failed:", "if False:")
mutant("the queue sends the newest first", A, "        head = conv.queue[0]\n        if not controller.send_prompt", "        head = conv.queue[-1]\n        if not controller.send_prompt")
mutant("how=steer is ignored", A, 'if state is SessionState.RUNNING and how == "steer" and (text or attachments):', "if False:")
mutant("a steer for a turn that is over is queued", A, "            if outcome is not None and outcome.over:", "            if False:")
mutant("a connection that never came is told as a lost one", A, "            if conv.ran:\n                self._unqueue_all(conv, \"连接断开了", "            if True:\n                self._unqueue_all(conv, \"连接断开了")
mutant("unqueue always takes all", A, "        if wanted is None:\n            self._unqueue_all(conv)", "        if True:\n            self._unqueue_all(conv)")
mutant("steers that were not taken in stay lost", A, '        if kind == "steer":\n            self._return_steers(conv)', "        if False:\n            self._return_steers(conv)")
mutant("closing says nothing of the queue", A, "        if conv.queue:  # closed by the user", "        if False:  # closed by the user")
mutant("the echo does not settle a steer", T, "        if steered is not None:\n            self.settle_steer", "        if False:\n            self.settle_steer")
mutant("any discard drops the steer", S, "b.text.strip() == text.strip()", "True")
mutant("steers nobody echoed stay on the page", S, "if self.transcript.pending_steers(last) and not self._close_requested:", "if False:")
mutant("an echo while running counts as an orphan", S, "if block is None or not block.pending or self.state is SessionState.RUNNING:", "if block is None or not block.pending:")
mutant("another session's usage is taken", S, "        if note.get(\"sessionId\") not in (None, self.session_id):\n            return\n        seen = Tokens.of", "        seen = Tokens.of")
mutant("the saved total is not read", S, "for other in (self._loaded_usage, self._inclusive_usage):", "for other in (self._inclusive_usage,):")
mutant("a steer is allowed while stopping", S, "        if turn.stopping:\n            return SteerOutcome(False, message=\"本轮正在停止\")\n        message_id", "        message_id")
mutant("a lost connection keeps the waiting steers", S, "        for block in self.transcript.pending_steers(turn):  # nobody is left", "        for block in []:  # nobody is left")
mutant("codex: a turn that ended is not told as over", C, 'if isinstance(error, CodexRpcError) and "no active turn" in error.message.lower():', "if False:")
mutant("codex: a sub-agent's tokens are not kept", C, "                self._usage = CodexUsage.from_breakdown(total)\n                self._changed()\n            return", "                pass\n            return")
mutant("codex: no steer in a saved turn", H, 'if item.get("type") == "userMessage" and item is not user:', "if False:")
mutant("the setting is not read", ST, 'follow_up=text("follow_up", FOLLOW_UPS),', 'follow_up="queue",')
mutant("any follow-up value is accepted", A, 'elif key == "followUp" and value in FOLLOW_UPS:', 'elif key == "followUp":')
mutant("a steer is not marked in the page json", V, '        if block.steer:\n            out["steer"] = True', "        if False:\n            out[\"steer\"] = True")

only = sys.argv[1:]
failures = []
for number, (name, path, old, new, tests) in enumerate(M, 1):
    if only and not any(word in name for word in only):
        continue
    text = path.read_text(encoding="utf-8")
    if text.count(old) != 1:
        print(f"[{number:2}] {name}: PATTERN FOUND {text.count(old)} TIMES", flush=True)
        failures.append(name)
        continue
    path.write_text(text.replace(old, new), encoding="utf-8")
    started = time.time()
    try:
        result = subprocess.run([PY, "-m", "pytest", "-q", "-x", "-p", "no:cacheprovider", *tests], cwd=ROOT, capture_output=True, text=True, timeout=600)
        tail = [line for line in result.stdout.splitlines() if line.startswith("FAILED") or " passed" in line or " failed" in line]
        caught = result.returncode != 0
    finally:
        path.write_text(text, encoding="utf-8")
    print(f"[{number:2}] {'caught ' if caught else 'SURVIVED'} {name}  ({time.time() - started:.0f}s) {tail[0] if tail else ''}", flush=True)
    if not caught:
        failures.append(name)
print("survivors:", failures or "none")
