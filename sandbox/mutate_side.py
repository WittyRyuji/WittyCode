"""Mutation checks for the memory curator's side questions (desktop/core/codex/side.py and devin/side.py).

    python sandbox/mutate_side.py              run every mutant
    python sandbox/mutate_side.py --check      only check that every pattern is found exactly once
    python sandbox/mutate_side.py --only 3,17  run these mutants only

Each mutant replaces one fragment, runs tests/test_codex_side.py and tests/test_devin_side.py, and puts the file back byte
for byte (also when the run is interrupted); at the end every file is compared with its original bytes.
"""
from __future__ import annotations

import hashlib
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PY = str(ROOT / ".venv" / "Scripts" / "python.exe")
CODEX = ROOT / "desktop" / "core" / "codex" / "side.py"
DEVIN = ROOT / "desktop" / "core" / "devin" / "side.py"
TESTS = ["tests/test_codex_side.py", "tests/test_devin_side.py"]
SECONDS = 300

M = [
    # ------------------------------------------------------------------ Codex: what is asked for
    ("codex: the thread is not ephemeral", CODEX, '            "ephemeral": True,\n', '            "ephemeral": False,\n'),
    ("codex: approval policy on-request", CODEX, 'APPROVAL = "never"', 'APPROVAL = "on-request"'),
    ("codex: a workspace-write sandbox", CODEX, 'SANDBOX = "read-only"', 'SANDBOX = "workspace-write"'),
    ("codex: the turn runs with full access", CODEX, 'LEVEL = "readonly"', 'LEVEL = "full"'),
    ("codex: a config (MCP servers) goes with the thread", CODEX, '            "developerInstructions": INSTRUCTIONS,\n        }', '            "developerInstructions": INSTRUCTIONS,\n            "config": {"mcp_servers": {}},\n        }'),
    ("codex: no developer instructions", CODEX, '            "developerInstructions": INSTRUCTIONS,\n', ''),
    ("codex: the application's hooks are consulted", CODEX, '            "input": turn_input(prompt),', '            "input": turn_input(prompt + engine.hooks.prompt(__import__("desktop.core.hooks", fromlist=["PromptEvent"]).PromptEvent("codex", "", prompt)).text),'),
    ("codex: the question runs in the current folder", CODEX, '    folder = Path(tempfile.gettempdir()) / "WittyCode" / "codex-scratch"', '    folder = Path.cwd()'),
    # ------------------------------------------------------------------ Codex: the model and its effort
    ("codex: no cheap model is looked for", CODEX, '    cheap = [model for model in models if cheapness(model.id) < len(CHEAP)]', '    cheap = []'),
    ("codex: model words by substring (gemini is a mini)", CODEX, '    found = words(model_id)', '    found = model_id.lower()'),
    ("codex: a model without a cheap word counts as the cheapest", CODEX, 'if word in found), len(CHEAP))', 'if word in found), 0)'),
    ("codex: luna before mini", CODEX, 'CHEAP = ("nano", "mini", "luna", "lite", "flash")', 'CHEAP = ("luna", "mini", "nano", "lite", "flash")'),
    ("codex: the first model, not the default", CODEX, '    return next((model for model in models if model.is_default), None) or (models[0] if models else None)', '    return models[0] if models else None'),
    ("codex: the first effort listed, not the weakest", CODEX, '        return min(known, key=EFFORTS.index)', '        return known[0]'),
    ("codex: unknown efforts give none", CODEX, '    return efforts[0] if efforts else ""', '    return ""'),
    ("codex: no effort is sent", CODEX, '            if effort:\n                turn["effort"] = effort', '            if False:\n                turn["effort"] = effort'),
    # ------------------------------------------------------------------ Codex: when not to ask
    ("codex: a Codex that would keep the thread is asked", CODEX, '        if not _ephemeral(started):', '        if False:'),
    ("codex: any ephemeral value is taken for yes", CODEX, 'thread.get("ephemeral") is True', 'thread.get("ephemeral") is not None'),
    ("codex: a kept thread is not deleted", CODEX, '            if self.kept:', '            if False:'),
    ("codex: the process is started", CODEX, '    client = engine._client\n    if not engine.settings.codex_enabled', '    client = await engine.connection()\n    if not engine.settings.codex_enabled'),
    ("codex: switched off is asked", CODEX, '    if not engine.settings.codex_enabled or client is None', '    if client is None'),
    ("codex: signed out is asked", CODEX, '        await engine.ensure_signed_in(client)\n', ''),
    ("codex: a blank prompt is asked", CODEX, 'or not prompt.strip():', 'or not prompt:'),
    # ------------------------------------------------------------------ Codex: the answer
    ("codex: the commentary is part of the answer", CODEX, '        answer = [text for phase, text in said if text and phase != "commentary"] or [text for _, text in said if text]', '        answer = [text for _, text in said if text]'),
    ("codex: the completed text is ignored", CODEX, '                self._texts[item_id] = _text(item.get("text")) or self._texts.get(item_id, "")', '                self._texts[item_id] = self._texts.get(item_id, "")'),
    ("codex: deltas replace each other", CODEX, '                self._texts[item_id] = self._texts.get(item_id, "") + _text(params.get("delta"))', '                self._texts[item_id] = _text(params.get("delta"))'),
    ("codex: the phase is not read", CODEX, '            if _text(item.get("phase")):\n                self._phases[item_id] = item["phase"]\n', ''),
    ("codex: an interrupted turn answers", CODEX, '        if self.status != "completed":', '        if self.status == "failed":'),
    ("codex: a turn without a status failed", CODEX, '            self.status = _text(turn.get("status")) or "completed"', '            self.status = _text(turn.get("status")) or "failed"'),
    ("codex: the last turn/started wins", CODEX, '            self.turn_id = self.turn_id or _turn_id(params) or None', '            self.turn_id = _turn_id(params) or None'),
    ("codex: a closed thread is waited for", CODEX, '        elif method == "thread/closed":', '        elif False:'),
    ("codex: the end of the process is not noticed", CODEX, '            if not self._client.running:\n                raise CodexProcessError', '            if False:\n                raise CodexProcessError'),
    ("codex: a command approval is accepted", CODEX, '    "item/commandExecution/requestApproval": {"decision": "decline"},', '    "item/commandExecution/requestApproval": {"decision": "accept"},'),
    ("codex: an elicitation is accepted", CODEX, '    "mcpServer/elicitation/request": {"action": "decline", "content": None, "_meta": None},', '    "mcpServer/elicitation/request": {"action": "accept", "content": {}, "_meta": None},'),
    ("codex: the side thread is not in the engine's table", CODEX, '        engine._attach(self)\n', ''),
    # ------------------------------------------------------------------ Codex: letting go
    ("codex: no unsubscribe", CODEX, '                await client.request("thread/unsubscribe", {"threadId": self.session_id}, timeout=UNSUBSCRIBE_SECONDS)', '                pass'),
    ("codex: a running turn is not stopped", CODEX, '            if self.turn_id is not None and not self.ended.is_set():', '            if False:'),
    ("codex: the thread being made is not waited for", CODEX, '                self.session_id = _thread_id(await settle(self.starting, SETTLE_SECONDS)) or None', '                pass'),
    ("codex: the turn being started is not waited for", CODEX, '                self.turn_id = _turn_id(await settle(self.turning, SETTLE_SECONDS)) or None', '                pass'),
    ("codex: thread/start is not shielded", CODEX, '        started = await asyncio.shield(self.starting)', '        started = await self.starting'),
    ("codex: turn/start is not shielded", CODEX, '        answered = await asyncio.shield(self.turning)', '        answered = await self.turning'),
    ("codex: the side thread stays in the engine's table", CODEX, '            self.engine._detach(self)', '            pass'),
    ("codex: no timeout", CODEX, '        async with asyncio.timeout(timeout):', '        async with asyncio.timeout(None):'),
    # ------------------------------------------------------------------ shared helpers (in codex/side.py)
    ("helpers: finish raises when the engine stops the clean-up", CODEX, '        if current is not None and current.cancelling():', '        if True:'),
    ("helpers: finish is not shielded", CODEX, '        await asyncio.shield(task)', '        await task'),
    ("helpers: settle leaves a late request running", CODEX, '        request.cancel()\n        return None', '        return None'),
    ("helpers: settle gives a failed request's result", CODEX, '    if request.cancelled() or request.exception() is not None:\n        return None\n', ''),
    # ------------------------------------------------------------------ Devin
    ("devin: a memory server goes with the session", DEVIN, '{"cwd": engine._scratch(), "mcpServers": []}', '{"cwd": engine._scratch(), "mcpServers": [{"name": "wittycode-memory", "command": "python", "args": [], "env": []}]}'),
    ("devin: the session is made in the current folder", DEVIN, '{"cwd": engine._scratch(), "mcpServers": []}', '{"cwd": __import__("os").getcwd(), "mcpServers": []}'),
    ("devin: a mode that writes code", DEVIN, 'LEVEL = "ask"', 'LEVEL = "code"'),
    ("devin: a mode Devin refuses is let pass", DEVIN, '            await client.request("session/set_mode", {"sessionId": self.session_id, "modeId": mode}, timeout=CONFIG_SECONDS)', '            with contextlib.suppress(DevinRpcError):\n                await client.request("session/set_mode", {"sessionId": self.session_id, "modeId": mode}, timeout=CONFIG_SECONDS)'),
    ("devin: free models are not looked for", DEVIN, '    free = [model for model in offered if (info := engine.model_info(model)) is not None and info.note == FREE]', '    free = []'),
    ("devin: no SWE-2 family", DEVIN, '    pool = free or [model for model in offered if model.startswith(FAMILY)]', '    pool = free'),
    ("devin: the first of the pool, not the cheapest kind", DEVIN, '    return min(pool, key=cheapness) if pool else None', '    return pool[0] if pool else None'),
    ("devin: the thought level stays", DEVIN, '        if lowest and lowest != level:', '        if False:'),
    ("devin: a model refusal ends the question", DEVIN, '            except DevinRpcError as error:\n                log.info("Devin did not take', '            except ZeroDivisionError as error:\n                log.info("Devin did not take'),
    ("devin: a thought-level refusal ends the question", DEVIN, '            with contextlib.suppress(DevinRpcError):\n                options = await self._set("thought_level", lowest, options)', '            if True:\n                options = await self._set("thought_level", lowest, options)'),
    ("devin: a permission is granted", DEVIN, '        return outcome_for("cancel", [', '        return outcome_for("allow_once", ['),
    ("devin: a stopped turn answers", DEVIN, '        if stop not in ("", "end_turn"):', '        if stop == "refusal":'),
    ("devin: all the words are the answer", DEVIN, '        return next((piece.strip() for piece in reversed(self._said) if piece.strip()), "")', '        return "".join(self._said).strip()'),
    ("devin: no new piece after a tool call", DEVIN, '        elif kind in ("tool_call", "tool_call_update") and self._said[-1]:', '        elif False:'),
    ("devin: a known sign-out is asked again", DEVIN, '    if engine.cached_account().get("auth") == "signed-out":\n        return None\n', ''),
    ("devin: switched off is asked", DEVIN, '    if not engine.settings.devin_enabled or client is None', '    if client is None'),
    ("devin: the process is started", DEVIN, '    client = engine._client\n    if not engine.settings.devin_enabled', '    client = await engine.connection()\n    if not engine.settings.devin_enabled'),
    ("devin: the sign-in is not checked", DEVIN, '        await engine.ensure_signed_in()\n', ''),
    ("devin: the side session is not in the engine's table", DEVIN, '        engine._attach(self)\n', ''),
    ("devin: no delete", DEVIN, '                await client.request("session/delete", {"sessionId": self.session_id}, timeout=DELETE_SECONDS)', '                pass'),
    ("devin: a running prompt is not stopped", DEVIN, '                    await client.notify("session/cancel", {"sessionId": self.session_id})', '                    pass'),
    ("devin: a stopped prompt is not waited for", DEVIN, '                await settle(self.asking, CANCEL_SECONDS)', '                pass'),
    ("devin: the session being made is not waited for", DEVIN, '                self.session_id = _session_id(await settle(self.creating, SETTLE_SECONDS)) or None', '                pass'),
    ("devin: session/new is not shielded", DEVIN, '        created = await asyncio.shield(self.creating)', '        created = await self.creating'),
    ("devin: session/prompt is not shielded", DEVIN, '        result = await asyncio.shield(self.asking)', '        result = await self.asking'),
    ("devin: the side session stays in the engine's table", DEVIN, '            self.engine._detach(self)', '            pass'),
]


def digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def main() -> int:
    args = sys.argv[1:]
    check_only = "--check" in args
    only = None
    if "--only" in args:
        only = {int(part) for part in args[args.index("--only") + 1].split(",") if part.strip()}
    originals = {path: path.read_bytes() for path in {CODEX, DEVIN}}
    before = {path: digest(path) for path in originals}
    survivors: list[str] = []
    ran = 0
    try:
        for number, (name, path, old, new) in enumerate(M, 1):
            text = originals[path].decode("utf-8")
            if text.count(old) != 1:
                print(f"[{number}] {name}: PATTERN FOUND {text.count(old)} TIMES", flush=True)
                survivors.append(name)
                continue
            if check_only or (only is not None and number not in only):
                continue
            ran += 1
            path.write_bytes(text.replace(old, new).encode("utf-8"))
            started = time.time()
            try:
                done = subprocess.run(
                    [PY, "-m", "pytest", "-q", "-x", "-p", "no:cacheprovider", *TESTS],
                    cwd=ROOT, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=SECONDS,
                )
                caught = done.returncode != 0
                tail = (done.stdout.strip().splitlines() or ["?"])[-1]
            except subprocess.TimeoutExpired:
                caught, tail = True, f"no end within {SECONDS} s (hangs: caught)"
            finally:
                path.write_bytes(originals[path])
            print(f"[{number}] {'caught  ' if caught else 'SURVIVED'} {name}  ({time.time() - started:.0f}s) {tail}", flush=True)
            if not caught:
                survivors.append(name)
    finally:
        for path, data in originals.items():
            if path.read_bytes() != data:
                path.write_bytes(data)
    restored = all(digest(path) == before[path] for path in originals)
    if check_only:
        print(f"\n{len(M) - len(survivors)} of {len(M)} patterns found once")
    else:
        print(f"\n{ran - len(survivors)} of {ran} caught")
    print("files restored byte for byte:", restored)
    for name in survivors:
        print("  survivor:", name)
    return 1 if survivors or not restored else 0


if __name__ == "__main__":
    sys.exit(main())
