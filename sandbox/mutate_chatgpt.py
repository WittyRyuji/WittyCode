"""Mutation checks for the ChatGPT Plan engine's own code (request building, the stream, the agent loop, the permission
policy over MCP tools, the session store, the model list, the controller, the engine, the settings migration): each change
to the source must make some test fail (a mutant that does not even import is not a catch).

    python sandbox/mutate_chatgpt.py            run every mutant
    python sandbox/mutate_chatgpt.py loop       only those whose name contains "loop"
    python sandbox/mutate_chatgpt.py --check    only check that every pattern is found exactly once
"""
from __future__ import annotations

import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PY = str(ROOT / ".venv" / "Scripts" / "python.exe")
C = ROOT / "desktop" / "core"
RESP = C / "chatgpt" / "responses.py"
MODELS = C / "chatgpt" / "models.py"
CTL = C / "chatgpt" / "controller.py"
ENG = C / "chatgpt" / "engine.py"
LOOP = C / "harness" / "loop.py"
TOOLSET = C / "harness" / "toolset.py"
STORE = C / "harness" / "store.py"
SETTINGS = C / "settings.py"
APP = C / "app_core.py"
TESTS = [
    "tests/test_chatgpt_responses.py",
    "tests/test_chatgpt_controller.py",
    "tests/test_chatgpt_models.py",
    "tests/test_chatgpt_engine.py",
    "tests/test_harness_loop.py",
    "tests/test_harness_store.py",
    "tests/test_harness_toolset.py",
    "tests/test_app_chatgpt.py",
    "tests/test_app_memory.py",
    "tests/test_settings.py",
]
E2E = ["tests/test_app_e2e.py", "-k", "chatgpt"]

M = [
    # ------------------------------------------------------------------ the request
    ("request: the answer is stored", RESP, '        "store": False,', '        "store": True,'),
    ("request: not streamed", RESP, '        "stream": True,', '        "stream": False,'),
    ("request: no encrypted reasoning", RESP, "    if shape.encrypted_reasoning:", "    if False:"),
    ("request: encrypted reasoning always", RESP, "    if shape.encrypted_reasoning:", "    if True:"),
    ("request: a summary is always asked", RESP, "    if shape.reasoning_summary:", "    if True:"),
    ("request: the reasoning effort is dropped", RESP, '    if reasoning and reasoning != "off":  # ("off" is a choice of the menu, not a level the API knows)\n        options["effort"] = reasoning', '    if False:\n        options["effort"] = reasoning'),
    ("request: no tool choice", RESP, '        request["tool_choice"] = "auto"', "        pass"),
    ("request: parallel calls ignore the shape", RESP, 'request["parallel_tool_calls"] = shape.parallel_tool_calls', 'request["parallel_tool_calls"] = True'),
    ("request: the cache key is always sent", RESP, "    if shape.cache_key and conversation:", "    if conversation:"),
    ("request: the cache key is never sent", RESP, "    if shape.cache_key and conversation:", "    if False:"),
    ("request: tools are grouped even when flat", RESP, "    if shape.flat_tools or not functions:", "    if not functions:"),
    ("request: tools are never grouped", RESP, "    if shape.flat_tools or not functions:", "    if True:"),
    ("history: a reasoning item without its content is kept", RESP, '        if not item.get("encrypted_content") and not item.get("wc_reasoning") and not item.get("wc_data"):\n            return None', '        if False:\n            return None'),
    ("history: the id of a reasoning item is sent back", RESP, '("type", "summary", "encrypted_content", "wc_reasoning", "wc_signature", "wc_data", "wc_origin")', '("type", "id", "summary", "encrypted_content", "wc_reasoning", "wc_signature", "wc_data", "wc_origin")'),
    ("history: a call loses its call id", RESP, '("type", "call_id", "name", "arguments", "namespace")', '("type", "name", "arguments", "namespace")'),
    ("history: an assistant message loses its role", RESP, '"role": item.get("role", "assistant")', '"role": "user"'),
    ("usage: what the cache served counts as input", RESP, "Tokens(max(self.input_tokens - self.cached_tokens - written, 0), self.output_tokens, self.cache_write_tokens, self.cached_tokens)", "Tokens(max(self.input_tokens - written, 0), self.output_tokens, self.cache_write_tokens, self.cached_tokens)"),
    ("usage: the context is only the input", RESP, "        return self.input_tokens + self.output_tokens", "        return self.input_tokens"),
    ("stream: a stream that stops early is fine", RESP, '    if not finished:\n        raise ChatGPTError("The stream ended', '    if False:\n        raise ChatGPTError("The stream ended'),
    ("stream: a failed response is ignored", RESP, '    if kind == "response.failed":', '    if kind == "response.failed_":'),
    ("stream: an incomplete answer is complete", RESP, '        reason = str(details.get("reason") or "incomplete") if isinstance(details, Mapping) else ("incomplete" if kind == "response.incomplete" else "")', '        reason = ""'),
    ("stream: the rejected parameter is not named", RESP, "    if isinstance(param, str) and param:", "    if False:"),
    ("stream: a reasoning summary is not read from the deltas", RESP, '            text = _summary_text(item) or "\\n\\n".join(reasoning_parts.pop(item_id, {}).values())', "            text = _summary_text(item)"),
    # ------------------------------------------------------------------ the agent loop
    ("loop: steers are not delivered", LOOP, "            self._deliver_steers(history)\n            await self._prepare(history)", "            await self._prepare(history)"),
    ("loop: a steer at the end of the turn is not taken", LOOP, "                if self.observer.has_steer():\n                    continue", "                if False:\n                    continue"),
    ("loop: one step too many", LOOP, "for step in range(1, self.max_steps + 1):", "for step in range(1, self.max_steps + 2):"),
    ("loop: a stopped call is left without an output", LOOP, "            rest = [call_output(call.call_id, INTERRUPTED) for call in calls if call.call_id not in answered]", "            rest = []"),
    ("loop: a refused call runs", LOOP, "        if verdict.verdict == DENY:", "        if False:"),
    ("loop: nothing is asked", LOOP, "        if verdict.verdict == ASK:", "        if False:"),
    ("loop: always is not remembered", LOOP, '            if answer == "proceed_always":\n                self.tools.remember(call)', '            if False:\n                self.tools.remember(call)'),
    ("loop: any answer allows", LOOP, '            if answer not in ("proceed_once", "proceed_always"):', "            if False:"),
    ("loop: an unknown tool runs", LOOP, "        if not self.tools.known(call.name):", "        if False:"),
    ("loop: a tool's bug ends the conversation", LOOP, "        except Exception as error:  # a tool's bug is the model's information", "        except KeyboardInterrupt as error:  # a tool's bug is the model's information"),
    ("loop: arguments that are not an object run", LOOP, "    if not isinstance(parsed, dict):", "    if False:"),
    ("loop: output is never clipped", LOOP, "    if len(text) <= limit:\n        return text", "    if True:\n        return text"),
    ("loop: the answer items are not kept", LOOP, "        history.extend(kept)\n        self._answer", "        self._answer"),
    # ------------------------------------------------------------------ the tools of a conversation
    ("toolset: a harmful tool is allowed unasked", TOOLSET, "or tool.harmless or", "or True or"),
    ("toolset: nothing is allowed at full access", TOOLSET, 'if self.level == "full" or tool.harmless', "if tool.harmless"),
    ("toolset: an allowed tool is asked again", TOOLSET, ' or f"mcp:{call.name}" in self.remembered:', ":"),
    ("toolset: a long name is kept whole", TOOLSET, '+ _UNSAFE.sub("_", tool))[:NAME_LIMIT]', '+ _UNSAFE.sub("_", tool))'),
    ("toolset: a stopped server's tools are offered", TOOLSET, "for client in self.clients if client.running for tool in client.tools}", "for client in self.clients for tool in client.tools}"),
    # ------------------------------------------------------------------ the session store
    ("store: an index out of step is trusted", STORE, "if isinstance(index, dict) and self._consistent(index):", "if isinstance(index, dict):"),
    ("store: any id is a path", STORE, "        if not _ID.match(session_id):\n            raise ValueError", "        if False:\n            raise ValueError"),
    ("store: oldest first", STORE, "rows.sort(key=lambda row: row.modified_at, reverse=True)", "rows.sort(key=lambda row: row.modified_at, reverse=False)"),
    ("store: every project's rows", STORE, '            if project is not None and not same_folder(raw.get("project", ""), project):', "            if False:"),
    ("store: another version loads", STORE, '    if not isinstance(raw, Mapping) or raw.get("version") != VERSION or not _ID.match', '    if not isinstance(raw, Mapping) or not _ID.match'),
    ("store: a deleted session stays in the list [EQUIVALENT: the file is gone first, so the index is rebuilt anyway]", STORE, "        if index.pop(session_id, None) is not None:\n            self._write_index(index)", "        index.pop(session_id, None)"),
    ("store: the usage is not saved", STORE, '        "usage": session.usage,', '        "usage": {},'),
    ("store: what was brought up is not saved", STORE, '        "recalled": session.recalled,', '        "recalled": [],'),
    # ------------------------------------------------------------------ the model list
    ("models: hidden models are listed", MODELS, '            if visibility not in (None, "list") or entry.get("hidden"):', "            if False:"),
    ("models: a model twice", MODELS, "        if model is not None and model.id not in seen:", "        if model is not None:"),
    ("models: images are assumed unsupported when unsaid", MODELS, 'supports_images=True if not isinstance(modalities, (list, tuple)) else "image" in modalities,', 'supports_images="image" in (modalities or ()),'),
    ("models: the other spelling of the list", MODELS, '            entries = result.get("data")', "            entries = None"),
    ("models: an empty list shows nothing", ENG, "        return models or list(FALLBACK_MODELS)", "        return models"),
    # ------------------------------------------------------------------ the controller
    ("controller: the hook is not asked", CTL, "context = self.manager.hooks.prompt(PromptEvent(self.engine, self.project, text, frozenset(self.recalled), first=bool(self.preface)))", 'context = type("C", (), {"text": "", "notice": "", "ids": frozenset()})()'),
    ("controller: every message is the first", CTL, ", first=bool(self.preface)))", ", first=True))"),
    ("controller: no message is the first", CTL, ", first=bool(self.preface)))", "))"),
    ("controller: the notes go in front of the message", CTL, "        sent += wrap_context(self.preface, context.text)", "        sent = wrap_context(self.preface, context.text) + sent"),
    ("controller: the page is not told", CTL, "        if context.notice:\n            self.transcript.add_notice(context.notice)", "        pass"),
    ("controller: a failed message counts as given", CTL, "            if turn.status is TurnStatus.DONE:\n                self.recalled |= self._recall_ids", "            if True:\n                self.recalled |= self._recall_ids"),
    ("controller: a failed turn leaves its message in the history", CTL, "        if len(self.saved.items) == self._turn_start + 1 and self.saved.turns:", "        if False:"),
    ("controller: a stop does not kill the running command [EQUIVALENT: cancelling the task kills the process tree in run_shell too]", CTL, "            self._context.abort.set()  # (a running command is killed at once)", "            pass"),
    ("controller: a stopped turn is recorded as done", CTL, '                self.transcript.abort_turn("本轮已停止。")\n                self._record_turn("interrupted")', '                self.transcript.abort_turn("本轮已停止。")\n                self._record_turn("done")'),
    ("controller: the total starts over every request", CTL, "self._usage_total = tokens if self._usage_total is None else self._usage_total.plus(tokens)", "self._usage_total = tokens"),
    ("controller: the step limit is not said", CTL, '        elif end.reason == "step_limit":', '        elif end.reason == "step_limit_":'),
    ("controller: a picture goes to a model without pictures", CTL, "            if info is not None and not info.supports_images:", "            if False:"),
    ("controller: a PDF without text goes", CTL, '            if item.kind == PDF and not (item.parsed_text or "").strip():', "            if False:"),
    ("controller: a steer is not queued", CTL, "        self._steers.append(Steer(text, (), block, item))", "        pass"),
    ("controller: an unadopted steer is lost", CTL, "            self._unadopted.append((block.text, tuple(block.attachments)))", "            pass"),
    ("controller: the end of a login is not told", CTL, "            if isinstance(error, AuthError) and error.terminal:\n                self.manager._emit_account()", "            pass"),
    # ------------------------------------------------------------------ the engine
    ("engine: a second sign-in runs", ENG, '        if self._signin is not None:\n            raise ChatGPTError("A sign-in is already in progress."', '        if False:\n            raise ChatGPTError("A sign-in is already in progress."'),
    ("engine: signing in again forgets the registration", ENG, "            client_id=current.client_id if current is not None else None,", "            client_id=None,"),
    ("engine: the models are kept for ever", ENG, "and self._clock() - self._models_at < MODELS_FRESH_SECONDS", "and True"),
    ("engine: the models of another account are used", ENG, "self._models is not None and self._models_for == active and", "self._models is not None and"),
    ("engine: an answer that is not 200 is parsed", ENG, "        if response.status != 200:", "        if False:"),
    ("engine: signed out opens a conversation", ENG, '        if view["auth"] != "signed-in":\n            raise AuthError(', '        if False:\n            raise AuthError('),
    ("engine: no plan opens a conversation", ENG, '        if not view["planEnabled"]:\n            raise PlanNotEnabled(', '        if False:\n            raise PlanNotEnabled('),
    ("engine: no originator", ENG, ', "originator": APP_NAME.lower()}', "}"),
    ("engine: the stream is not closed", ENG, "            await lines.aclose()", "            pass"),
    ("engine: signing out does not revoke", ENG, "confirmed = await self.tokens.sign_out(target)", "confirmed = True"),
    ("engine: an unknown account is used", ENG, '        if self.accounts.get(client_id) is None:\n            raise ChatGPTError("That account is not saved."', '        if False:\n            raise ChatGPTError("That account is not saved."'),
    ("engine: a new sign-in is not the account in use", ENG, "        self.accounts.save(record)\n        self.accounts.set_active(record.client_id)", "        self.accounts.save(record)"),
    ("engine: the memory is not handed on", ENG, "        if self.memory is None:\n            return []", "        if True:\n            return []"),
    ("engine: a sign-in in progress is not shown", ENG, '        if self._signin is not None:\n            view["signingIn"] = True', "        pass"),
    # ------------------------------------------------------------------ settings and the application
    ("settings: the memory migration is for every file", SETTINGS, "if (not isinstance(version, int) or version < 2) and tuple(chosen) == LEGACY_MEMORY_ENGINES:", "if tuple(chosen) == LEGACY_MEMORY_ENGINES:"),
    ("settings: the memory migration never happens", SETTINGS, "if (not isinstance(version, int) or version < 2) and tuple(chosen) == LEGACY_MEMORY_ENGINES:", "if False:"),
    ("settings: the migration overrides a choice", SETTINGS, "if (not isinstance(version, int) or version < 2) and tuple(chosen) == LEGACY_MEMORY_ENGINES:", "if not isinstance(version, int) or version < 2:"),
    ("settings: the welcome is forgotten", SETTINGS, "        chatgpt_welcomed=chatgpt_welcomed,", "        chatgpt_welcomed=defaults.chatgpt_welcomed,"),
    ("settings: the switch is forgotten", SETTINGS, "        chatgpt_enabled=chatgpt_enabled,", "        chatgpt_enabled=defaults.chatgpt_enabled,"),
    ("app: saved sessions listed with the switch off", APP, "        if self.chatgpt is not None and (self.settings.chatgpt_enabled or self._has_api_models()):", "        if self.chatgpt is not None:"),
    ("app: models offered with the switch off", APP, "            if self.chatgpt is None or not self.settings.chatgpt_enabled:", "            if self.chatgpt is None:"),
    ("app: a slow refresh sends the account it read before the wait", APP, 'factory, codex, devin = await asyncio.gather(self._factory(), self._codex_account(), self._devin_account())\n        # Read when it is sent, not while the slow ones were awaited: the user may have dismissed the one-time note or\n        # signed in meanwhile, and a late event with the old values would bring it all back.\n        self.emit("accounts", {"factory": factory, "codex": codex, "devin": devin, "chatgpt": self._chatgpt_now()})', 'early = self._chatgpt_now()\n        factory, codex, devin = await asyncio.gather(self._factory(), self._codex_account(), self._devin_account())\n        self.emit("accounts", {"factory": factory, "codex": codex, "devin": devin, "chatgpt": early})'),
    ("app: the welcome is not remembered", APP, "        self.settings.chatgpt_welcomed = True", "        pass"),
    ("app: the switch is not kept", APP, '        self.settings.chatgpt_enabled = bool(params["on"])', "        pass"),
    ("app: the permission is not kept", APP, "                settings.chatgpt_autonomy = value", "                pass"),
    ("app: the engine has no hooks of its own", APP, "        for engine in (codex, devin, chatgpt):\n", "        for engine in (codex, devin):\n"),
    ("app: the engine is not closed", APP, "            await self.chatgpt.close_all()", "            pass"),
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
        if done.returncode == 0:  # the quick tests let it through: the whole-window ones get their say
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
