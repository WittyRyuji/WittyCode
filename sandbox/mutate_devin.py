"""Mutation checks for the Devin engine: each change to the source must make some test fail (a mutant that does not even
import is not a catch)."""
from __future__ import annotations

import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PY = str(ROOT / ".venv" / "Scripts" / "python.exe")
D = ROOT / "desktop" / "core" / "devin"
APP = ROOT / "desktop" / "core" / "app_core.py"
CAT = ROOT / "desktop" / "core" / "catalog.py"
TESTS = ["tests/test_devin_rpc.py", "tests/test_devin_items.py", "tests/test_devin_engine.py", "tests/test_devin_turns.py", "tests/test_app_devin.py"]

M = [
    ("rpc: no protocol version", D / "rpc.py", '{"jsonrpc": "2.0", **message}', "{**message}"),
    ("rpc: answers overtake the notifications", D / "rpc.py", "        self._inbox.put_nowait((kind, message))\n\n    def _resolve", "        if kind == 'response':\n            self._resolve(message)\n            return\n        self._inbox.put_nowait((kind, message))\n\n    def _resolve"),
    ("rpc: the waiting are failed before their answers are read", D / "rpc.py", "if self._closing or self._dispatch_done or self._inbox is None:", "if True:"),
    ("items: the cached part stays in the input", D / "items.py", "Tokens(max(0, (read or 0) - cached), write or 0, None, cached)", "Tokens(read or 0, write or 0, None, cached)"),
    ("items: a write is a Create even when it replaces", D / "items.py", 'creates = native == "write" or (bool(diffs) and all(d.get("oldText") is None for d in diffs))', "creates = True"),
    ("items: nanoseconds are not trimmed", D / "items.py", 'head = f"{whole}.{fraction[:6]}"', "head = f'{whole}.{fraction}'"),
    ("items: a PDF without words is sent", D / "items.py", 'raise ValueError(f"PDF「{attachment.name}」里没有可读的文字")', "pass"),
    ("history: every message is a steer after the first", D / "history.py", "if self._turn is None or self._answered:", "if self._turn is None:"),
    ("history: every message is a new turn", D / "history.py", "if self._turn is None or self._answered:", "if True:"),
    ("history: the replay's figures are lost", D / "history.py", "            self.usage[message_id] = tokens", "            pass"),
    ("history: a tool that never ended is done", D / "history.py", "            self.transcript.finish_tool(turn, tool, result, status=status, at=self._last)", "            self.transcript.finish_tool(turn, tool, result, status=ToolStatusDone, at=self._last)"),
    ("policy: a refusal is answered as chosen", D / "policy.py", '    if value == "cancel":\n        rejected = reject_option(options)\n        return selected(rejected) if rejected else CANCELLED\n    return selected(value)', "    return selected(value)"),
    ("policy: the first refusal keeps Devin's id", D / "policy.py", 'views.append(OptionView("cancel", OPTION_LABELS["reject_once"], name, "danger"))', 'views.append(OptionView(value, OPTION_LABELS["reject_once"], name, "danger"))'),
    ("policy: no scope in the label", D / "policy.py", 'return f"{label}：{detail}"', "return label"),
    ("controller: the duplicate usage_update counts", D / "controller.py", "        if items.SUBAGENT_META in meta:\n            return  # (each model call", "        if False:\n            return  # (each model call"),
    ("controller: a stopped turn is done", D / "controller.py", 'TurnStatus.INTERRUPTED if reason == "cancelled" else TurnStatus.DONE', "TurnStatus.DONE"),
    ("controller: the first answer ends the turn", D / "controller.py", "        if self._outstanding <= 0:\n            self._complete(turn)\n\n    def _prompt_failed", "        self._complete(turn)\n\n    def _prompt_failed"),
    ("controller: a figure is kept under one name", D / "controller.py", 'return {f"turn:{message_id}": tokens for message_id, tokens in self._turn_usage.items()}', 'return {"self": self.usage} if self.usage else {}'),
    ("controller: a stopped turn's calls are not counted", D / "controller.py", "        if tokens is None and self._calls:", "        if False:"),
    ("controller: the request asked for is read after Devin's reports", D / "controller.py", "wanted_model, wanted_level, wanted_mode = self.requested_model, self.reasoning, mode_of(self.autonomy)", "wanted_model, wanted_level, wanted_mode = self.requested_model, '', 'accept-edits'"),
    ("controller: the mode is not set when opening", D / "controller.py", "        if wanted_mode != current_value(options, \"mode\"):", "        if False:"),
    ("controller: a refused steer is lost", D / "controller.py", "            self._unadopted.append((steer.text, tuple(steer.attachments)))\n", "            pass\n"),
    ("controller: a steer waits for a confirmation that never comes", D / "controller.py", "        block = self.transcript.add_steer(turn, text, attachments, pending=False)", "        block = self.transcript.add_steer(turn, text, attachments, pending=True)"),
    ("controller: stop does not cancel the question", D / "controller.py", "        self._expire_pending()  # a question waiting for the user is answered", "        pass  # a question waiting for the user is answered"),
    ("controller: reconnect shows the replay again", D / "controller.py", "loaded = await self._load(client, show=not self._history_loaded)", "loaded = await self._load(client, show=True)"),
    ("controller: the output is appended, not replaced", D / "controller.py", "self._outputs[call_id] = shown[-OUTPUT_KEEP_CHARS:]", "self._outputs[call_id] = (self._outputs.get(call_id, '') + shown)[-OUTPUT_KEEP_CHARS:]"),
    ("controller: the window is forgotten after a replay", D / "controller.py", 'window = known or (self.context.window if self.context.model in ("", model) else None)', "window = known"),
    ("engine: sessions without a message are listed", D / "engine.py", 'if meta.get("cognition.ai/hasUserMessages") is False:', "if False:"),
    ("engine: the scratch session is kept", D / "engine.py", 'await client.request("session/delete", {"sessionId": session_id}, timeout=REQUEST_SECONDS)', "pass"),
    ("engine: a signed-out account opens sessions", D / "engine.py", "        if self._status is None or not self._status.signed_in:\n            raise DevinSignedOutError()", "        pass"),
    ("engine: a retired process is not waited for", D / "engine.py", "            await asyncio.gather(*(old.close() for old in retired), return_exceptions=True)", "            pass"),
    ("models: fusion pairings are listed", D / "models.py", "or item[\"value\"].startswith(FUSION_PREFIX):", "or False:"),
    ("models: the group of a maker is lost", D / "models.py", "return next((name for prefix, name in GROUPS if lowered.startswith(prefix)), \"\")", 'return ""'),
    ("catalog: a Devin id is a Droid id", CAT, "        for engine, prefix in ENGINE_PREFIXES.items():\n            if page_id.startswith(prefix):\n                return Target(engine, page_id[len(prefix) :])", "        if page_id.startswith(CODEX_PREFIX):\n            return Target('codex', page_id[len(CODEX_PREFIX) :])"),
    ("app: Devin's own level is Droid's", APP, 'return {"codex": s.codex_autonomy, "devin": s.devin_autonomy}.get(engine, s.autonomy)', 'return {"codex": s.codex_autonomy}.get(engine, s.autonomy)'),
    ("app: a Devin row opens as a Droid session", APP, "elif row_id.startswith(DEVIN_ROW_PREFIX):", "elif False:"),
    ("app: the Devin sessions are not listed", APP, "if self.devin is not None and self.settings.devin_enabled:\n            with contextlib.suppress(Exception):\n                for item in await self.devin.list_sessions", "if False:\n            with contextlib.suppress(Exception):\n                for item in await self.devin.list_sessions"),
    ("app: the Devin models are not asked for", APP, 'if account.get("auth") == "signed-in":\n                    return list(await self.devin.list_models())', "if False:\n                    return []"),
    ("app: pictures are checked against Droid's models", APP, 'if target.engine == "devin":\n            lookup = getattr(self.devin, "model_info", None)', 'if False:\n            lookup = getattr(self.devin, "model_info", None)'),
    ("app: the account is not remembered", APP, 'if isinstance(devin, Mapping) and devin.get("auth") in ("signed-in", "signed-out"):', "if False:"),
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
    mutated = text.replace(old, new)
    if "ToolStatusDone" in new:
        mutated = mutated.replace("from desktop.core.transcript import Transcript, Turn, TurnStatus", "from desktop.core.transcript import ToolStatus, Transcript, Turn, TurnStatus\nToolStatusDone = ToolStatus.DONE")
    path.write_text(mutated, encoding="utf-8")
    started = time.time()
    try:
        done = subprocess.run([PY, "-m", "pytest", "-q", "-x", "-p", "no:cacheprovider", *TESTS], cwd=ROOT, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=900)
    finally:
        path.write_text(text, encoding="utf-8")
    lines = [line for line in done.stdout.splitlines() if line.startswith("FAILED") or "Error" in line[:40]]
    broke = "ImportError" in done.stdout or "SyntaxError" in done.stdout or "collected 0 items" in done.stdout
    verdict = "caught " if done.returncode != 0 and not broke else ("DID NOT RUN" if broke else "SURVIVED")
    print(f"[{number}] {verdict} {name} ({time.time() - started:.0f}s) {lines[0] if lines else ''}", flush=True)
    if verdict != "caught ":
        survivors.append(name)
print("survivors:", survivors or "none")
