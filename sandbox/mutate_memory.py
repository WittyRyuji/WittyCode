"""Mutation checks for the shared memory and the Codex elicitation: each change to the source must make some test fail
(a mutant that does not even import is not a catch).

    python sandbox/mutate_memory.py            run every mutant
    python sandbox/mutate_memory.py store      only those whose name contains "store"
    python sandbox/mutate_memory.py --check    only check that every pattern is found exactly once
"""
from __future__ import annotations

import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PY = str(ROOT / ".venv" / "Scripts" / "python.exe")
C = ROOT / "desktop" / "core"
STORE = C / "memory" / "store.py"
SAFE = C / "memory" / "safety.py"
SERVER = C / "memory" / "server.py"
LAUNCH = C / "memory" / "launch.py"
BRIDGE = C / "memory" / "bridge.py"
ELICIT = C / "codex" / "elicit.py"
CCTL = C / "codex" / "controller.py"
CENG = C / "codex" / "engine.py"
DCTL = C / "devin" / "controller.py"
DENG = C / "devin" / "engine.py"
SM = C / "session_manager.py"
APP = C / "app_core.py"
RECALL = C / "memory" / "recall.py"
HOOKS = C / "hooks.py"
ENVELOPE = C / "context_envelope.py"
ATTACH = C / "attachments.py"
CITEMS = C / "codex" / "items.py"
SETTINGS = C / "settings.py"
TOOL = C / "toolinfo.py"
TESTS = ["tests/test_memory_store.py", "tests/test_memory_server.py", "tests/test_memory_recall.py", "tests/test_codex_elicitation.py", "tests/test_app_memory.py"]
E2E = ["tests/test_app_e2e.py", "-k", "shared_memory or memory_switched"]

M = [
    # ------------------------------------------------------------------ the store
    ("store: a title of any length", STORE, "        if len(title) > MAX_TITLE:", "        if False:"),
    ("store: a text of any length", STORE, "        if len(text) > MAX_TEXT:", "        if False:"),
    ("store: secrets are stored", STORE, "        secret = find_secret(title, text)", "        secret = None"),
    ("store: the same text twice", STORE, "            if other_text.strip() == text.strip() or _same_subject(wanted, _normal_title(other_title)):", "            if _same_subject(wanted, _normal_title(other_title)):"),
    ("store: the same title twice", STORE, "            if other_text.strip() == text.strip() or _same_subject(wanted, _normal_title(other_title)):", "            if other_text.strip() == text.strip():"),
    ("store: Step 1 and Step 2 are one subject", STORE, '    if re.findall(r"\\d+", a) != re.findall(r"\\d+", b):\n        return False', "    if False:\n        return False"),
    ("store: any title is similar", STORE, "ratio() >= SIMILAR_TITLE", "ratio() >= 0.3"),
    ("store: no limit of notes", STORE, "                if self._active_count(db, scope, project) >= MAX_ACTIVE:\n                    raise MemoryRejected(\n                        \"limit\"" if False else "                if self._active_count(db, scope, project) >= MAX_ACTIVE:", "                if False:"),
    ("store: an agent sees every project", STORE, '        return project is None or entry.scope == "global" or same_project(entry.project, project)', "        return True"),
    ("store: forgetting a forgotten note changes it again", STORE, "            if not before.active:\n                return before", "            if False:\n                return before"),
    ("store: restoring an active note changes it again", STORE, "            if before.active:\n                return before", "            if False:\n                return before"),
    ("store: undoing a creation", STORE, '        if change.op == "create":\n            return self.forget(change.id, by=by)', '        if False:\n            return self.forget(change.id, by=by)'),
    ("store: undoing a forget forgets again", STORE, '        if change.op == "forget":\n            return self.restore(change.id, by=by)', '        if change.op == "forget":\n            return self.forget(change.id, by=by)'),
    ("store: undoing an update does not put the title back", STORE, "            change.id, by=by, title=before.title, text=before.text,", "            change.id, by=by, title=None, text=before.text,"),
    ("store: the editor is not noted", STORE, "                rev=before.rev + 1,\n                editor=by,", "                rev=before.rev + 1,\n                editor=before.editor,"),
    ("store: the revision does not move on forgetting", STORE, "after = replace(before, status=status, updated=time.time(), rev=before.rev + 1, editor=by)", "after = replace(before, status=status, updated=time.time(), rev=before.rev, editor=by)"),
    ("store: the journal repeats the last change", STORE, '                    if int(raw.get("seq", 0)) > since:', '                    if int(raw.get("seq", 0)) >= since:'),
    ("store: the numbers of changes repeat", STORE, '                return int(json.loads(line)["seq"]) + 1', '                return int(json.loads(line)["seq"])'),
    ("store: no lock", STORE, "                    _lock(handle)\n                    break", "                    break"),
    ("store: the search index keeps the old text", STORE, '        db.execute("delete from fts where id = ?", (entry.id,))', "        pass"),
    ("store: an index out of step is trusted", STORE, "if version is None or version[0] != INDEX_VERSION or files != rows:", "if version is None or version[0] != INDEX_VERSION:"),
    ("store: any number of tags", STORE, "    return tuple(out[:MAX_TAGS])", "    return tuple(out)"),
    ("store: any kind", STORE, "        if kind not in KINDS:", "        if False:"),
    ("store: a project note without a project", STORE, '        if scope == "project" and not project:\n            raise MemoryRejected("bad_scope", "A project memory needs a project.")\n        secret', '        if False:\n            raise MemoryRejected("bad_scope", "A project memory needs a project.")\n        secret'),
    ("store: a note outside the asker's project can be read", STORE, "        if not self._visible(entry, project):", "        if False:"),
    # ------------------------------------------------------------------ secrets
    ("safety: sk- keys pass", SAFE, '    ("an API key (sk-…)", re.compile(r"\\bsk-[A-Za-z0-9_\\-]{16,}")),\n', ""),
    ("safety: a commit hash is a secret", SAFE, "(?=[A-Za-z0-9+/]*[A-Z])", ""),
    ("safety: a bearer token passes", SAFE, '    ("a bearer token", re.compile(r"\\bBearer\\s+[A-Za-z0-9._~+/\\-]{20,}=*", re.IGNORECASE)),\n', ""),
    ("safety: a private key passes", SAFE, '    ("a private key", re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----")),\n', ""),
    ("safety: a password line passes", SAFE, '[:=]\\s*[\\"\']?[^\\s\\"\']{8,}"', '[:=]\\s*[\\"\']?[^\\s\\"\']{80,}"'),
    # ------------------------------------------------------------------ the server
    ("server: unlimited writes", SERVER, "        if len(self._writes) >= WRITES_PER_HOUR:", "        if False:"),
    ("server: the run is not noted", SERVER, "        return agent(self.engine, self.run)", '        return agent(self.engine, "")'),
    ("server: an agent sees every project", SERVER, '        return self.project or "-"', "        return None"),
    ("server: the limit of a search is not kept", SERVER, "limit=min(max(limit, 1), 20))", "limit=limit)"),
    ("server: forgetting needs no reason", SERVER, '        if not str(arguments.get("reason") or "").strip():\n            raise ValueError("say why it should be forgotten (reason)")', "        pass"),
    ("server: an empty update is accepted", SERVER, '        if not changes and tags is None:\n            raise ValueError("nothing to change: give title, text, kind or tags")', "        pass"),
    ("server: a project note without a project", SERVER, '        if scope == "project" and not self.project:', "        if False:"),
    ("server: forgetting is announced as destructive", SERVER, '"annotations": {"readOnlyHint": False, "destructiveHint": False, "idempotentHint": True, "openWorldHint": False},\n    },\n)', '"annotations": {"readOnlyHint": False, "destructiveHint": True, "idempotentHint": True, "openWorldHint": False},\n    },\n)'),
    ("server: writing is announced as read-only", SERVER, '"annotations": {"readOnlyHint": False, "destructiveHint": False, "idempotentHint": False, "openWorldHint": False},', '"annotations": {"readOnlyHint": True, "destructiveHint": False, "idempotentHint": False, "openWorldHint": False},'),
    ("server: a notification gets an answer", SERVER, "        if identifier is None and isinstance(method, str):\n            return None", "        if False:\n            return None"),
    ("server: a note is read without the warning", SERVER, '        return BRIEF_NOTE + "\\n\\n" + describe(entry, full=True)', '        return describe(entry, full=True)'),
    ("server: any protocol version is echoed", SERVER, '"protocolVersion": wanted if wanted in PROTOCOL_VERSIONS else PROTOCOL_VERSIONS[0],', '"protocolVersion": wanted,'),
    ("server: a bug ends the memory", SERVER, "            except Exception as failure:  # a bug here must not end the conversation's memory", "            except KeyboardInterrupt as failure:"),
    # ------------------------------------------------------------------ what is handed to an engine
    ("launch: the memory cannot be switched off", LAUNCH, '        if not self.enabled(engine):\n            return None', "        if False:\n            return None"),
    ("launch: the brief ignores the switch", LAUNCH, '        if not self.enabled(engine):\n            return ""', '        if False:\n            return ""'),
    ("launch: the brief has no limit", LAUNCH, "    if len(text) > BRIEF_CHARS:", "    if False:"),
    ("launch: the project is not passed", LAUNCH, '"--project", project_path(project), "--run", run', '"--project", "", "--run", run'),
    ("launch: the run is not passed", LAUNCH, '"--project", project_path(project), "--run", run', '"--project", project_path(project), "--run", ""'),
    ("launch: the package is not found from another folder", LAUNCH, 'env = {"PYTHONPATH": str(PACKAGE_ROOT), "PYTHONIOENCODING": "utf-8", "PYTHONUTF8": "1"}', 'env = {"PYTHONIOENCODING": "utf-8", "PYTHONUTF8": "1"}'),
    ("launch: pythonw is used for a server that needs stdin", LAUNCH, '    if executable.name.lower() == "pythonw.exe":', "    if False:"),
    ("launch: the project notes are not in the brief", LAUNCH, 'store.search("", project=here, scope="project", limit=BRIEF_PROJECT) if here else []', "[]"),
    # ------------------------------------------------------------------ the bridge to the page
    ("bridge: the user's own notes are news", BRIDGE, "if c.seq > seen and not c.by_user)", "if c.seq > seen)"),
    ("bridge: marking as seen goes beyond the newest", BRIDGE, "value = latest if seq is None else max(0, min(seq, latest))", "value = latest if seq is None else seq"),
    ("bridge: the user's changes are listed as the agents'", BRIDGE, "agent_changes = [c for c in self.store.journal(since=since) if not c.by_user]", "agent_changes = list(self.store.journal(since=since))"),
    ("bridge: a note of the page is written as an agent's", BRIDGE, '            tags=_tags(params.get("tags")),\n            by="user",', '            tags=_tags(params.get("tags")),\n            by=None,'),
    ("bridge: the conversation of a note is never known", BRIDGE, '        known = self.runs.get(str(by.get("run") or ""))', "        known = None"),
    # ------------------------------------------------------------------ the engines
    ("app: the memory is not given to Droid", APP, "            manager.memory = memory", "            pass"),
    ("app: the memory is not given to Codex", APP, "                codex.memory = memory", "                pass"),
    ("app: the memory is not given to Devin", APP, "                devin.memory = memory", "                pass"),
    ("app: the page is not told about the memory at start", APP, "            memory = await asyncio.to_thread(self.memory.state)\n", "            memory = None\n"),
    ("app: a write is not announced", APP, "            if self._memory_state is not None and version != self._memory_state:", "            if False:"),
    ("app: unknown engines are accepted for the memory", APP, "s.memory_engines = [name for name in MEMORY_ENGINES if name in value]", "s.memory_engines = list(value)"),
    ("app: a refused note is reported in the model's words", APP, "            raise AppError(explain(rejected)) from None", "            raise AppError(rejected.message) from None"),
    ("droid: no brief on a new session", SM, 'self.preface = self.manager.memory.brief_for("droid", self.project) if self.manager.memory is not None else ""', 'self.preface = ""'),
    ("droid: the brief is not sent", SM, "        sent += wrap_context(self.preface, context.text)", "        sent += wrap_context(\"\", context.text)"),
    ("droid: the brief goes with every message", SM, '        if self._preface_turn is not None and self._preface_turn.status is TurnStatus.DONE:', "        if False:"),
    ("droid: a new session gets no server", SM, "mcp_servers=self.manager.memory_servers(self)),", "mcp_servers=()),"),
    ("droid: a resumed session gets no server", SM, '**({"mcp_servers": servers} if servers else {}))', ")"),
    ("codex: no brief on a new thread", CCTL, 'self.preface = memory.brief_for("codex", self.project) if memory is not None else ""', 'self.preface = ""'),
    ("codex: the brief is not sent", CCTL, "        sent += wrap_context(self.preface, context.text)", "        sent += wrap_context(\"\", context.text)"),
    ("codex: the brief goes with every message", CCTL, "                self.preface = \"\"  # (likewise: it is in the thread now)", "                pass"),
    ("codex: a resumed thread gets no server", CCTL, '{"threadId": self.session_id, "excludeTurns": True, **self.manager.memory_params(self)}', '{"threadId": self.session_id, "excludeTurns": True}'),
    ("codex: the server is not in the config", CENG, '        return {"config": {"mcp_servers": {command.name: server}}}', "        return {}"),
    ("codex: the environment of the server is lost", CENG, 'server = {"command": command.command, "args": list(command.args), "env": dict(command.env)}', 'server = {"command": command.command, "args": list(command.args)}'),
    ("devin: no brief on a new session", DCTL, 'self.preface = memory.brief_for("devin", self.project) if memory is not None else ""', 'self.preface = ""'),
    ("devin: the brief goes with every message", DCTL, "                self.preface = \"\"  # (likewise: it is in the session now)", "                pass"),
    ("devin: a loaded session gets no server", DCTL, '{"sessionId": self.session_id, "cwd": str(self.project), "mcpServers": self.manager.memory_servers(self)}', '{"sessionId": self.session_id, "cwd": str(self.project), "mcpServers": []}'),
    ("devin: the environment is not a list of pairs", DENG, 'env = [{"name": name, "value": value} for name, value in command.env.items()]', "env = dict(command.env)"),
    ("tools: the memory tools are other tools", TOOL, '    if memory_tool(name):\n        return Category.MEMORY', "    if False:\n        return Category.MEMORY"),
    ("tools: another server's memory_write is ours", TOOL, "    if MEMORY_SERVER not in name:\n        return None", "    if False:\n        return None"),
    # ------------------------------------------------------------------ Codex asks the user
    ("elicit: a url that is not http is opened", ELICIT, '        if not url.lower().startswith(("http://", "https://")):\n            return None', "        pass"),
    ("elicit: an approval is not told from a form", ELICIT, '    if meta.get("codex_approval_kind") == APPROVAL:', "    if False:"),
    ("elicit: more fields than a card can show", ELICIT, "    if not isinstance(properties, Mapping) or len(properties) > MAX_FIELDS:", "    if not isinstance(properties, Mapping):"),
    ("elicit: session persistence is not offered", ELICIT, '*([SESSION] if "session" in persist else [])', "*[]"),
    ("elicit: always is offered when it is not allowed", ELICIT, '*([ALWAYS] if "always" in persist else [])', "*[ALWAYS]"),
    ("elicit: a skipped question is declined, not cancelled", ELICIT, '        return {"action": "cancel", "content": None, "_meta": None}', '        return {"action": "decline", "content": None, "_meta": None}'),
    ("elicit: anything is an allowance", ELICIT, "        if first not in (ALLOW, SESSION, ALWAYS):", "        if False:"),
    ("elicit: the allowance for the session is not told", ELICIT, '        persist = {SESSION: "session", ALWAYS: "always"}.get(first)', "        persist = None"),
    ("elicit: a required field may stay empty", ELICIT, "                if field.required:\n                    raise ValueError(field.key)", "                pass"),
    ("elicit: a number is not checked", ELICIT, '    if field.kind == "integer":\n        return int(text)', '    if field.kind == "integer":\n        return text'),
    ("elicit: a choice answers with its label", ELICIT, '        return field.values[field.labels.index(text)] if text in field.labels else text\n    return text', "        return text\n    return text"),
    # ------------------------------------------------------------------ the notes that bear on a message, the hooks, the block after the typed text
    ("recall: a note at exactly the threshold is left out", RECALL, "        if points >= threshold:", "        if points > threshold:"),
    ("recall: every word counts", RECALL, "if len(w) >= 3 and w not in _STOP_WORDS and not w.isdigit()", "if len(w) >= 2"),
    ("recall: everyday words count", RECALL, "if len(w) >= 3 and w not in _STOP_WORDS and not w.isdigit()", "if len(w) >= 3 and not w.isdigit()"),
    ("recall: numbers count as words", RECALL, "if len(w) >= 3 and w not in _STOP_WORDS and not w.isdigit()", "if len(w) >= 3 and w not in _STOP_WORDS"),
    ("recall: everyday pairs count", RECALL, "            if run[i : i + 2] not in _STOP_PAIRS:\n                pairs.add(run[i : i + 2])", "            pairs.add(run[i : i + 2])"),
    ("recall: triples with an everyday pair count", RECALL, "            if piece[:2] not in _STOP_PAIRS and piece[1:] not in _STOP_PAIRS:\n                triples.add(piece)", "            triples.add(piece)"),
    ("recall: a long word is worth no more than a short one", RECALL, "2 if len(w) >= 4 else 1", "1"),
    ("recall: a note given before is given again", RECALL, "        if note.id in given:\n            continue\n", ""),
    ("recall: the project's notes do not come first", RECALL, '0 if item[1].scope == "project" else 1', "1"),
    ("recall: the oldest come first", RECALL, "-item[1].updated))", "item[1].updated))"),
    ("recall: more than the limit are given", RECALL, "scored[: max(1, limit)]", "scored"),
    ("recall: a message with no terms finds notes", RECALL, "    if not wanted:\n        return []\n", ""),
    ("recall: the notes go on past the budget", RECALL, "        if shown and len(line) > left:\n            break\n", ""),
    ("recall: a long note is given whole", RECALL, "        if len(body) > MAX_NOTE_CHARS:", "        if False:"),
    ("recall: a note keeps its line breaks", RECALL, 'body = " ".join(note.text.split())', "body = note.text"),
    ("launch: the recall switch is ignored", LAUNCH, "if event.first and self.recall():", "if event.first:"),
    ("launch: the engine switch is ignored by the memory's hook", LAUNCH, "        if not self.enabled(event.engine):\n            return None\n        said, made", "        said, made"),
    ("launch: every message brings notes, not only the first", LAUNCH, "if event.first and self.recall():", "if self.recall():"),
    ("launch: the first message brings none", LAUNCH, "if event.first and self.recall():", "if not event.first and self.recall():"),
    ("hooks: a message is the first unless it says it is not", HOOKS, "    first: bool = False\n", "    first: bool = True\n"),
    ("launch: what was given before is given again", LAUNCH, "event.text, seen=event.seen)", "event.text)"),
    ("launch: the notice names no titles", LAUNCH, 'titles = "、".join(f"「{\' \'.join(note.title.split())}」" for note in shown)', 'titles = ""'),
    ("launch: the ids are not told", LAUNCH, "frozenset(note.id for note in shown))", "frozenset())"),
    ("store: another project's notes are offered", STORE, '            where += " or e.pkey = ?"\n            args.append(project_key(project))', '            where += " or e.scope = \'project\'"'),
    ("store: forgotten notes are offered", STORE, "where, args = \"e.status = 'active' and (e.scope = 'global'\", []", "where, args = \"(e.scope = 'global'\", []"),
    ("hooks: a hook that raises stops the message", HOOKS, "            except Exception:\n                log.exception(\"prompt hook failed\")\n                continue", "            except KeyError:\n                log.exception(\"prompt hook failed\")\n                continue"),
    ("hooks: the notices are lost", HOOKS, "            if result.notice:\n                notices.append(result.notice)", "            pass"),
    ("hooks: the ids are lost", HOOKS, "            ids |= result.ids", "            pass"),
    ("hooks: an empty answer is added", HOOKS, "if result is None or not result.text.strip():", "if result is None:"),
    ("envelope: nothing to add still adds a block", ENVELOPE, '    return f"\\n\\n{OPEN}\\n{body}\\n{CLOSE}" if body else ""', '    return f"\\n\\n{OPEN}\\n{body}\\n{CLOSE}"'),
    ("envelope: a block cut in the middle stays", ENVELOPE, '(?:" + re.escape(CLOSE) + r"|\\Z)"', '(?:" + re.escape(CLOSE) + r")"'),
    ("envelope: the first version's note stays", ENVELOPE, "    return _LEGACY.sub(\"\", _BLOCK.sub(\"\", text), count=1)", "    return _BLOCK.sub(\"\", text)"),
    ("envelope: a phrase the user types is taken out [EQUIVALENT: the guard is only a fast path; _BLOCK cannot match without OPEN in the text and _LEGACY is anchored on the full legacy prefix, so both subs do nothing when the guard holds]", ENVELOPE, "    if OPEN not in text and not text.startswith(\"[Shared memory\"):\n        return text\n", ""),
    ("history: Droid's saved message keeps the block", ATTACH, "    return strip_context(\"\".join(text for text in texts if not is_system_reminder(text)))", "    return \"\".join(text for text in texts if not is_system_reminder(text))"),
    ("history: a thread's text keeps the block", CITEMS, "    text = strip_context(text)\n    if not text.startswith(HANDOFF_START):", "    if not text.startswith(HANDOFF_START):"),
    ("droid: the notes go in front of the message", SM, "        sent += wrap_context(self.preface, context.text)", "        sent = wrap_context(self.preface, context.text) + sent"),
    ("droid: the page is not told", SM, "        if context.notice:\n            self.transcript.add_notice(context.notice)", "        pass"),
    ("droid: the hook is not asked", SM, "context = self.manager.hooks.prompt(PromptEvent(self.engine, self.project, text, frozenset(self.recalled), first=bool(self.preface)))", "context = PromptContext()"),
    ("droid: every message is the first", SM, ", first=bool(self.preface)))", ", first=True))"),
    ("droid: no message is the first", SM, ", first=bool(self.preface)))", "))"),
    ("codex: every message is the first", CCTL, ", first=bool(self.preface)))", ", first=True))"),
    ("codex: no message is the first", CCTL, ", first=bool(self.preface)))", "))"),
    ("devin: every message is the first", DCTL, ", first=bool(self.preface)))", ", first=True))"),
    ("devin: no message is the first", DCTL, ", first=bool(self.preface)))", "))"),
    ("droid: a failed message counts as given", SM, "if self._recall_turn is not None and self._recall_turn.status is TurnStatus.DONE:", "if self._recall_turn is not None:"),
    ("droid: what was given is forgotten", SM, "            self.recalled |= self._recall_ids  # (a failed turn gives them again with the retry)", "            pass"),
    ("codex: the notes go in front of the message", CCTL, "        sent += wrap_context(self.preface, context.text)", "        sent = wrap_context(self.preface, context.text) + sent"),
    ("codex: the page is not told", CCTL, "        if context.notice:\n            self.transcript.add_notice(context.notice)", "        pass"),
    ("codex: a failed message counts as given", CCTL, "            if turn.status is TurnStatus.DONE:\n                self.recalled |= self._recall_ids", "            if True:\n                self.recalled |= self._recall_ids"),
    ("codex: what was given is forgotten", CCTL, "                self.recalled |= self._recall_ids  # (a failed turn gives them again with the retry)", "                pass"),
    ("devin: the notes go in front of the message", DCTL, "        sent += wrap_context(self.preface, context.text)", "        sent = wrap_context(self.preface, context.text) + sent"),
    ("devin: the page is not told", DCTL, "        if context.notice:\n            self.transcript.add_notice(context.notice)", "        pass"),
    ("devin: a failed message counts as given", DCTL, "            if turn.status is TurnStatus.DONE:\n                self.recalled |= self._recall_ids", "            if True:\n                self.recalled |= self._recall_ids"),
    ("devin: what was given is forgotten", DCTL, "                self.recalled |= self._recall_ids  # (a failed turn gives them again with the retry)", "                pass"),
    ("app: the memory's hook is not registered", APP, "            manager.hooks.on_prompt(memory.prompt_hook)\n", ""),
    ("app: Codex has hooks of its own", APP, "                codex.hooks = manager.hooks\n", ""),
    ("app: Devin has hooks of its own", APP, "                devin.hooks = manager.hooks\n", ""),
    ("settings: a bad recall value is kept", SETTINGS, "    if not isinstance(memory_recall, bool):\n        memory_recall = defaults.memory_recall", "    pass"),
    ("settings: the recall is not read back", SETTINGS, "        memory_recall=memory_recall,\n", ""),
    ("app: the recall switch is not saved", APP, "            s.memory_recall = value", "            pass"),
    ("app: the recall switch is not told to the page", APP, '            "memoryRecall": self.settings.memory_recall,\n', ""),
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
