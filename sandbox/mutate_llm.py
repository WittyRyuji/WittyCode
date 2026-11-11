"""Mutation checks for the API providers' own route (``core/llm``), the engine that routes to it, the catalog, the privacy gate,
``todo_write``, the repeat-call reminder and the Droid-side window fix (2026-11-10). Each mutant undoes one part; the tests must
catch it.

    python sandbox/mutate_llm.py            run every mutant, in a temporary copy of the repository
    python sandbox/mutate_llm.py --check    only check that every pattern is found exactly once (read only)
    python sandbox/mutate_llm.py 3 7        run just those mutants

The machinery is that of ``mutate_harness_g678.py`` (this working tree is never written to); only the table and the tests differ.
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import mutate_harness_g678 as base  # noqa: E402

CORE = Path("desktop") / "core"
CHAT, ANTHROPIC, BACKEND, RESPONSES_API = (CORE / "llm" / name for name in ("chat.py", "anthropic.py", "backend.py", "responses_api.py"))
RESPONSES, ENGINE, ERRORS = CORE / "chatgpt" / "responses.py", CORE / "chatgpt" / "engine.py", CORE / "chatgpt" / "errors.py"
CATALOG, APP, PROVIDERS, SESSIONS, LIMITS = CORE / "catalog.py", CORE / "app_core.py", CORE / "providers.py", CORE / "session_manager.py", CORE / "droid_limits.py"
TODO, LOOP, PERMISSIONS = CORE / "harness" / "todo.py", CORE / "harness" / "loop.py", CORE / "harness" / "permissions.py"

NEW = [
    "tests/test_llm_chat.py", "tests/test_llm_anthropic.py", "tests/test_llm_backend.py", "tests/test_app_api.py", "tests/test_catalog.py",
    "tests/test_harness_todo.py", "tests/test_droid_limits.py", "tests/test_providers.py",
]
OLDER = ["tests/test_app_chatgpt.py", "tests/test_app_core.py", "tests/test_harness_loop.py", "tests/test_context.py"]

M = [
    # ---- Chat Completions: the request
    ("chat: no system message", CHAT, '    messages: list[dict[str, Any]] = [{"role": "system", "content": instructions}] if instructions else []\n', "    messages: list[dict[str, Any]] = []\n"),
    ("chat: a result is a user message", CHAT, '{"role": "tool", "tool_call_id"', '{"role": "user", "tool_call_id"'),
    ("chat: every call is its own assistant message", CHAT, '            message = assistant()\n            message.setdefault("tool_calls", [])', '            open_assistant = None\n            message = assistant()\n            message.setdefault("tool_calls", [])'),
    ("chat: thoughts of any origin go back", CHAT, 'if item.get("wc_reasoning") and item.get("wc_origin") == origin:', 'if item.get("wc_reasoning"):'),
    ("chat: thoughts never go back", CHAT, '                thoughts += str(item["wc_reasoning"])\n', "                pass\n"),
    ("chat: pictures are dropped", CHAT, "            if pictures:\n", "            if False:\n"),
    ("chat: the usage is not asked for", CHAT, '        "stream_options": {"include_usage": True},\n', ""),
    ("chat: the level is not sent", CHAT, '        body["reasoning_effort"] = effort\n', "        pass\n"),
    ("chat: openai wants the same key as everyone", CHAT, '"max_completion_tokens" if "api.openai.com" in route.base_url else "max_tokens"', '"max_tokens"'),
    ("chat: the users own arguments are ignored", CHAT, "    body.update(route.extra_args)\n", "    pass\n"),
    ("chat: tools are not left to the model", CHAT, '        body["tool_choice"] = "auto"\n', "        pass\n"),
    # ---- Chat Completions: the stream
    ("chat stream: a cut tag is not waited for", CHAT, "                keep = _tag_prefix(self._buffer, THINK_CLOSE)\n", "                keep = 0\n"),
    ("chat stream: the server's own thoughts do not stop the tag search", CHAT, "            own_thoughts = True\n", "            pass\n"),
    ("chat stream: an answer in parts is not joined", CHAT, "        if isinstance(content, list):", "        if False:"),
    ("chat stream: two ids on one index are one call", CHAT, "if call is None or (call_id and call.call_id and call_id != call.call_id):", "if call is None:"),
    ("chat stream: a call without an id has none", CHAT, 'call = _Call(call_id or f"call_{uuid.uuid4().hex[:16]}", len(slots))', "call = _Call(call_id, len(slots))"),
    ("chat stream: deepseek's cache hits are not counted", CHAT, '        cached = raw.get("prompt_cache_hit_tokens")  # (DeepSeek)\n', "        cached = None\n"),
    ("chat stream: a cut answer is a whole one", CHAT, '{"length": "max_output_tokens", "content_filter": "content_filter"}.get(reason, "")', '{}.get(reason, "")'),
    ("chat stream: a stream that stops early is fine", CHAT, "    if not ended and not reason:\n", "    if False:\n"),
    ("chat stream: an error in the stream is ignored", CHAT, '        if payload.get("error"):\n            raise _error_of(payload)\n', '        if False:\n            raise _error_of(payload)\n'),
    ("chat stream: the thoughts carry no origin", CHAT, '"wc_reasoning": thoughts, "wc_origin": origin})', '"wc_reasoning": thoughts})'),
    # ---- Anthropic: the request
    ("anthropic: a result is an assistant message", ANTHROPIC, '        elif kind == "function_call_output":\n            push("user", ', '        elif kind == "function_call_output":\n            push("assistant", '),
    ("anthropic: the conversation is not cached", ANTHROPIC, '        messages[-1]["content"][-1] = {**messages[-1]["content"][-1], "cache_control": EPHEMERAL}\n', "        pass\n"),
    ("anthropic: the system prompt is not cached", ANTHROPIC, '[{"type": "text", "text": instructions, "cache_control": EPHEMERAL}]', '[{"type": "text", "text": instructions}]'),
    ("anthropic: unsigned thinking goes back", ANTHROPIC, 'elif item.get("wc_reasoning") and item.get("wc_signature"):', 'elif item.get("wc_reasoning"):'),
    ("anthropic: thinking of any origin goes back", ANTHROPIC, '            if item.get("wc_origin") != origin:\n                continue\n', "            if False:\n                continue\n"),
    ("anthropic: redacted thinking is dropped", ANTHROPIC, '            if item.get("wc_data"):', "            if False:"),
    ("anthropic: the budget may take the whole room", ANTHROPIC, "    budget = min(THINKING_BUDGETS.get(effort, THINKING_BUDGETS[\"medium\"]), max_tokens - ANSWER_ROOM)\n", '    budget = THINKING_BUDGETS.get(effort, THINKING_BUDGETS["medium"])\n'),
    ("anthropic: the picture keeps no type", ANTHROPIC, '"media_type": split[0]', '"media_type": "image/png"'),
    ("anthropic: arguments need not be an object", ANTHROPIC, "    return value if isinstance(value, dict) else {}\n", "    return value\n"),
    ("anthropic: tools are not left to the model", ANTHROPIC, '        body["tool_choice"] = {"type": "auto"}\n', "        pass\n"),
    ("anthropic: the users own arguments are ignored", ANTHROPIC, "    body.update(route.extra_args)\n", "    pass\n"),
    # ---- Anthropic: the stream
    ("anthropic stream: the cache is not in the input", ANTHROPIC, 'usage_of(count(raw.get("input_tokens")) + cached + written,', 'usage_of(count(raw.get("input_tokens")),'),
    ("anthropic stream: a cut answer is a whole one", ANTHROPIC, '"max_tokens": "max_output_tokens", ', ""),
    ("anthropic stream: an error event is ignored", ANTHROPIC, '        if kind == "error":\n            raise _error_of(payload)\n', '        if False:\n            raise _error_of(payload)\n'),
    ("anthropic stream: a stream that stops early is fine", ANTHROPIC, "    if not stopped:\n", "    if False:\n"),
    ("anthropic stream: the signature is lost", ANTHROPIC, '                block.signature += str(delta.get("signature") or "")\n', "                pass\n"),
    ("anthropic stream: a call without arguments has none", ANTHROPIC, '                arguments = block.text or "{}"\n', "                arguments = block.text\n"),
    # ---- the backend
    ("backend: a 403 is an ordinary failure", BACKEND, "    elif status in (401, 403):\n", "    elif status == 401:\n"),
    ("backend: a 404 is an ordinary failure", BACKEND, "    elif status == 404:\n", "    elif False:\n"),
    ("backend: a 429 is an ordinary failure", BACKEND, "    elif status == 429:\n", "    elif False:\n"),
    ("backend: a server error is an ordinary failure", BACKEND, "    elif status is not None and status >= 500:\n", "    elif False:\n"),
    ("backend: a network error is an ordinary failure", BACKEND, '    if error.code == "network":\n', "    if False:\n"),
    ("backend: a bearer goes in the key header", BACKEND, '"Authorization" if spec.bearer else "x-api-key"', '"x-api-key"'),
    ("backend: the users headers do not replace ours", BACKEND, "        for existing in [k for k in sent if k.lower() == name.lower()]:\n            del sent[existing]\n", "        pass\n"),
    ("backend: a removed model is asked all the same", BACKEND, "        if spec is None:\n            raise ChatGPTError(", "        if False:\n            raise ChatGPTError("),
    ("backend: a missing key is a program error", BACKEND, '            raise ChatGPTError(str(error), code="api") from error\n', "            raise\n"),
    ("backend: the reasoning menu has no off", BACKEND, 'LEVELS = ("off", "low", "medium", "high")', 'LEVELS = ("low", "medium", "high")'),
    ("backend: the models are read every time", BACKEND, "        if self._specs is None or self._specs[0] != stamp:\n", "        if True:\n"),
    ("backend: openai means chat completions", BACKEND, '"openai": RESPONSES', '"openai": CHAT'),
    # ---- the Responses exits
    ("responses api: a model without a level gets reasoning fields", RESPONSES_API, '    if not (isinstance(reasoning, Mapping) and reasoning.get("effort")):\n', "    if False:\n"),
    ("responses api: the answer's room is not sent", RESPONSES_API, '        body["max_output_tokens"] = route.max_output\n', "        pass\n"),
    ("responses api: other apis' thoughts are sent", RESPONSES_API, '    body["input"] = for_responses(body.get("input") or ())\n', "    pass\n"),
    ("responses: another api's thoughts are kept in the request to openai", RESPONSES, '        if item.get("type") == "reasoning" and not item.get("encrypted_content"):\n            continue\n', '        if False:\n            continue\n'),
    ("responses: the wc keys are sent to openai", RESPONSES, '        sent.append({key: value for key, value in item.items() if not key.startswith("wc_")})\n', "        sent.append(dict(item))\n"),
    ("responses: another api's thoughts are not kept", RESPONSES, 'if not item.get("encrypted_content") and not item.get("wc_reasoning") and not item.get("wc_data"):', 'if not item.get("encrypted_content"):'),
    ("responses: off is a level", RESPONSES, '    if reasoning and reasoning != "off":', "    if reasoning:"),
    ("responses: cache writes are not told apart", RESPONSES, "        written = self.cache_write_tokens or 0\n", "        written = 0\n"),
    # ---- the engine
    ("engine: an api model goes to the plan", ENGINE, '        if self.api is not None and is_api_model(str(request.get("model") or "")):\n', "        if False:\n"),
    ("engine: an api model needs a chatgpt sign-in", ENGINE, '        if not is_api_model(str(model or "")):', "        if True:"),
    ("engine: the plan gets other apis' thoughts", ENGINE, '        body = {**request, "input": for_responses(request.get("input") or ())}\n', "        body = request\n"),
    ("engine: an api model has no window", ENGINE, "            return None if spec is None else spec.window\n", "            return None\n"),
    ("engine: an api model is unknown", ENGINE, "            return None if spec is None else model_of(spec)\n", "            return None\n"),
    # ---- the catalog
    ("catalog: an unlisted api model is droid's", CATALOG, '            return Target("chatgpt", page_id)\n', '            return Target("droid", page_id)\n'),
    ("catalog: droid's copies of the entries stay in the list", CATALOG, '        if getattr(info, "is_custom", False) and info.id in owned:\n            continue\n', '        if False:\n            continue\n'),
    ("catalog: an old droid conversation does not name the model", CATALOG, '            catalog._page_ids.setdefault(("droid", parsed[1]), spec.id)\n', "            pass\n"),
    ("catalog: a cached droid-only entry is the own loop's", CATALOG, '                target = Target("droid", parsed[1])\n            else:\n                target = catalog.target(item["id"])', '                target = Target("chatgpt", item["id"])\n            else:\n                target = catalog.target(item["id"])'),
    ("catalog: an unknown api model keeps a prefix", CATALOG, "        if model.startswith(API_PREFIX):\n            return model\n", ""),
    # ---- the application
    ("app: the memory curator is offered api conversations", APP, '        if conv.engine == "chatgpt" and is_api_model(str(getattr(controller, "model", ""))):\n            return', '        if False:\n            return'),
    ("app: an import may go on with an api model", APP, 'if model and self.catalog.source_of(model) == "chatgpt":', 'if model and self.catalog.engine_of(model) == "chatgpt":'),
    ("app: a switch says the plan", APP, '    return "API" if source.startswith(API_PREFIX) else ENGINE_NAMES[engine]', "    return ENGINE_NAMES[engine]"),
    ("app: api conversations hide with the plan", APP, "        if self.chatgpt is not None and (self.settings.chatgpt_enabled or self._has_api_models()):", "        if self.chatgpt is not None and self.settings.chatgpt_enabled:"),
    ("app: api models are listed without the own loop", APP, "            if self.chatgpt is None or lister is None:", "            if lister is None:"),
    ("app: a build without the loop cannot fall back to droid", APP, '        if target.engine == "chatgpt" and self.chatgpt is None and droid is not None:', "        if False:"),
    ("app: a saved row does not know its model", APP, '"settings": {"model": saved.model,', '"settings": {"model": "",'),
    # ---- the providers
    ("providers: a key set later needs a restart", PROVIDERS, "        value = _lookup(self._environ, name) or _lookup(self._registry(), name)\n", "        value = _lookup(self._environ, name)\n"),
    ("providers: the key command runs every time", PROVIDERS, "        if cached is not None and cached[0] > now:\n", "        if False:\n"),
    ("providers: bedrock is run by the own loop", PROVIDERS, 'if group is None or group.kind == BEDROCK or isinstance(entry.get("bedrock"), dict):', "if group is None:"),
    ("providers: every model takes pictures", PROVIDERS, 'images=not (entry["noImageSupport"] if isinstance(entry.get("noImageSupport"), bool) else entry["provider"] == "generic-chat-completion-api"),', "images=True,"),
    ("providers: the files never seem to change", PROVIDERS, "        return stamp(self.factory_settings), stamp(self.sidecar)\n", "        return 0, 0\n"),
    ("providers: the page loses the models without figures", PROVIDERS, '        return [figures.get(model_id, {"id": model_id}) for model_id in result.get("models") or ()]\n', "        return list(figures.values())\n"),
    ("providers: figures only on the entry", PROVIDERS, '_FIGURE_SOURCES = (None, "top_provider", "capabilities", "limits")', "_FIGURE_SOURCES = (None,)"),
    ("providers: an answer limit as big as the window is kept", PROVIDERS, "        if window and output and output >= window:\n            output = None", "        if False:\n            output = None"),
    ("providers: the per-model compaction point is ignored", PROVIDERS, "            if value:\n                return value\n        return _positive_int(doc.get(\"compactionTokenLimit\"))", "            if False:\n                return value\n        return _positive_int(doc.get(\"compactionTokenLimit\"))"),
    # ---- todo_write and the reminder
    ("todo: two steps may be in progress", TODO, '    if sum(1 for todo in todos if todo["status"] == "in_progress") > 1:\n', "    if False:\n"),
    ("todo: any status goes", TODO, "        elif status not in STATES:\n", "        elif False:\n"),
    ("todo: an empty step goes", TODO, "        if not isinstance(content, str) or not content.strip():\n", "        if not isinstance(content, str):\n"),
    ("todo: any number of steps", TODO, "    if len(items) > MAX_ITEMS:\n", "    if False:\n"),
    ("todo: a step of any length", TODO, '"content": content.strip()[:MAX_TEXT]', '"content": content.strip()'),
    ("todo: no hint to start the next step", TODO, "    elif done < len(todos):\n", "    elif False:\n"),
    ("permissions: the checklist asks", PERMISSIONS, '    if spec.name in _NO_EFFECT:\n', "    if False:\n"),
    ("loop: a declined call is reminded", LOOP, "        if self._repeats < REPEAT_AFTER or outcome.cancelled:\n", "        if self._repeats < REPEAT_AFTER:\n"),
    ("loop: the reminder comes at the second call", LOOP, "REPEAT_AFTER = 3\n", "REPEAT_AFTER = 2\n"),
    ("loop: another call does not reset the count", LOOP, "            self._last_call, self._repeats = key, 1\n", "            self._last_call = key\n"),
    ("loop: the order of the arguments matters", LOOP, "json.dumps(call.args, sort_keys=True, default=str)", "json.dumps(call.args, sort_keys=False, default=str)"),
    ("loop: the reminder is not added", LOOP, "        output = clip_output(outcome.output) + self.reminder(call, outcome)\n", "        output = clip_output(outcome.output)\n"),
    # ---- the Droid side of the window
    ("droid: a built-in session is given a point", SESSIONS, "        if self._compaction_set is None and not model.startswith(CUSTOM):\n            return", "        if False:\n            return"),
    ("droid: a refused point still shows the window", SESSIONS, "            self._user_window = None  # (droid compacts where it always did", "            pass  # (droid compacts where it always did"),
    ("droid: the page shows droid's window", SESSIONS, "        if self._user_window:\n            reading = replace(reading, window=self._user_window)\n", ""),
    ("limits: the answer's room is the whole answer", LIMITS, "    reserve = min(max_output or window // 10, window // 4)\n", "    reserve = max_output or window // 10\n"),
    ("limits: 85% is the point", LIMITS, "    return max(min(int(window * SHARE), window - reserve), 1)\n", "    return int(window * SHARE)\n"),
    ("limits: a model without a window has no point", LIMITS, "            return compaction_point(window, max_output) if window else DEFAULT_LIMIT\n", "            return compaction_point(window, max_output) if window else None\n"),
    ("limits: a built-in model has no point to go back to", LIMITS, "        return self.providers.factory_compaction(model) or DEFAULT_LIMIT\n", "        return self.providers.factory_compaction(model) or None\n"),
]

if __name__ == "__main__":
    base.M[:] = M
    base.NEW[:] = NEW
    base.OLDER[:] = OLDER
    sys.exit(base.main())
