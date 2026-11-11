"""A stand-in for the Devin CLI (tests only: no network). It answers what ``desktop.core.devin`` asks of the real one,
with the message shapes of Devin 3000.11 as seen on the real program.

``python tests/fake_devin.py --version`` / ``auth status|login|logout`` / ``models list --format json`` / ``acp``.

``acp``: newline-delimited JSON on stdin/stdout **with** ``"jsonrpc": "2.0"``, exit code 0 when stdin closes. State lives
in ``$FAKE_DEVIN_HOME/state.json`` (the account and the sessions with everything they said), so a second process (after a
crash, a reconnect, or for history) sees the same sessions. Every message received is appended to
``$FAKE_DEVIN_HOME/received.jsonl`` for the tests to inspect.

What a prompt does depends on markers in its text:

- ``[full]``: a thought, a command with cumulative output snapshots and exit code 1, a new file, an edit, a search, two model
  calls, the final answer;
- ``[approve]``: a command that needs approval (``session/request_permission``); what happens next follows the choice
  (``switch_bypass`` also changes the mode);
- ``[slow]``: a command that prints until the turn is cancelled, or until a second prompt is merged into it (then the answer
  is ``got: <the second prompt>``);
- ``[stuck]``: like ``[slow]``, but a cancel is ignored;
- ``[lag]`` (with any of them): a prompt merged into the turn is answered 0.4 s after the one it joined;
- ``[crash]``: some text, then the process exits with code 3;
- ``[fail]``: the prompt is answered with a JSON-RPC error;
- anything else: one answer, ``echo: <the first text block>`` (plus a count of the other blocks), in two chunks.

Environment: ``FAKE_DEVIN_ACCOUNT`` = signed-in | signed-out (until the state file has one); ``FAKE_DEVIN_LOGIN_DELAY``
(seconds, default 0.2, or ``never``); ``FAKE_DEVIN_BROKEN=1`` (``acp`` exits 2 at once); ``FAKE_DEVIN_NO_MODELS_JSON=1``
(``models list`` fails).
"""

from __future__ import annotations

import asyncio
import itertools
import json
import os
import sys
import threading
import time
from pathlib import Path
from typing import Any

HOME = Path(os.environ.get("FAKE_DEVIN_HOME") or Path.cwd() / ".fake-devin")
STATE = HOME / "state.json"
RECEIVED = HOME / "received.jsonl"
WORDS = ["cliff", "parrot", "magnetic", "stay", "lizard", "mechanic", "bold", "owner", "alpine", "jersey", "wood", "pullover"]

# (value, name, images, thought levels, default level, window, free)
MODELS: list[tuple[str, str, bool, list[str], str, int, bool]] = [
    ("adaptive", "Adaptive", True, [], "", 0, False),
    ("swe-2-high", "SWE-2", True, ["medium", "high", "max"], "high", 262000, True),
    ("claude-opus-5-5-medium", "Claude Opus 5.5", True, ["low", "medium", "high", "xhigh", "max"], "medium", 1000000, False),
    ("glm-5-2", "GLM-5.2 High", False, ["none", "high", "max"], "high", 200000, False),
]
FUSION = "fusion-claude-opus-5-5-medium-sidekick-swe-2-medium"
MODES = [
    ("accept-edits", "Code", "Write and edit code"),
    ("smart", "Smart", "Auto-approve actions the model judges safe"),
    ("ask", "Ask", "Answer questions without code changes"),
    ("plan", "Plan", "Plan changes before implementing"),
    ("bypass", "Bypass Permissions", "Auto-approve all tool calls"),
]
PERMISSION_OPTIONS = [
    {"optionId": "allow_once", "name": "Allow", "kind": "allow_once"},
    {"optionId": "allow_session", "name": "Yes, allow `rm` commands (this session)", "kind": "allow_always"},
    {"optionId": "allow_always", "name": "Yes, always allow `rm` commands in `proj`", "kind": "allow_always"},
    {"optionId": "switch_bypass", "name": "Yes, switch to bypass mode", "kind": "allow_always"},
    {"optionId": "reject_once", "name": "Reject", "kind": "reject_once"},
]
CLI_VERSION = "3000.11.238"


class RpcError(Exception):
    def __init__(self, code: int, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


def iso(moment: float | None = None) -> str:
    stamp = time.time() if moment is None else moment
    whole = time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(stamp))
    return f"{whole}.{int((stamp % 1) * 1_000_000_000):09d}+00:00"


def iso_seconds(moment: float) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S+00:00", time.gmtime(moment))


def iso_z(moment: float) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S.000Z", time.gmtime(moment))


def model_row(value: str) -> tuple[str, str, bool, list[str], str, int, bool]:
    return next(row for row in MODELS if row[0] == value)


class Fake:
    def __init__(self, loop: asyncio.AbstractEventLoop) -> None:
        self.loop = loop
        self.state = self.load()
        self.initialized = False
        self.running: dict[str, dict[str, Any]] = {}
        self.waiting: dict[Any, asyncio.Future[Any]] = {}
        self.server_ids = itertools.count(1)
        self.lock = threading.Lock()
        self.tasks: set[asyncio.Task[Any]] = set()

    # ------------------------------------------------------------------ state and wire

    def load(self) -> dict[str, Any]:
        if STATE.is_file():
            return json.loads(STATE.read_text(encoding="utf-8"))
        return {"account": os.environ.get("FAKE_DEVIN_ACCOUNT", "signed-in"), "sessions": {}, "counter": 0}

    def save(self) -> None:
        HOME.mkdir(parents=True, exist_ok=True)
        temporary = STATE.with_suffix(".tmp")
        temporary.write_text(json.dumps(self.state, ensure_ascii=False), encoding="utf-8")
        os.replace(temporary, STATE)

    def send(self, message: dict[str, Any]) -> None:
        data = (json.dumps({"jsonrpc": "2.0", **message}, ensure_ascii=False) + "\n").encode("utf-8")
        with self.lock:
            sys.stdout.buffer.write(data)
            sys.stdout.buffer.flush()

    def notify(self, method: str, params: dict[str, Any]) -> None:
        self.send({"method": method, "params": params})

    def update(self, sid: str, update: dict[str, Any], *, record: bool = True) -> None:
        self.notify("session/update", {"sessionId": sid, "update": update})
        if record and update.get("sessionUpdate") not in ("usage_update", "config_option_update", "current_mode_update", "available_commands_update"):
            self.state["sessions"][sid]["log"].append({"update": update})

    async def ask(self, method: str, params: dict[str, Any]) -> Any:
        request_id = f"req-{next(self.server_ids)}"
        future: asyncio.Future[Any] = self.loop.create_future()
        self.waiting[request_id] = future
        self.send({"id": request_id, "method": method, "params": params})
        try:
            return await future
        finally:
            self.waiting.pop(request_id, None)

    # ------------------------------------------------------------------ main loop

    async def run(self) -> None:
        queue: asyncio.Queue[bytes | None] = asyncio.Queue()

        def read() -> None:
            for raw in sys.stdin.buffer:
                self.loop.call_soon_threadsafe(queue.put_nowait, raw)
            self.loop.call_soon_threadsafe(queue.put_nowait, None)

        threading.Thread(target=read, daemon=True).start()
        while True:
            raw = await queue.get()
            if raw is None:
                return
            try:
                message = json.loads(raw)
            except ValueError:
                continue
            HOME.mkdir(parents=True, exist_ok=True)
            with RECEIVED.open("a", encoding="utf-8") as log:
                log.write(json.dumps(message, ensure_ascii=False) + "\n")
            if "method" in message and message.get("id") is not None:
                task = asyncio.ensure_future(self.on_request(message))
                self.tasks.add(task)
                task.add_done_callback(self.tasks.discard)
            elif "method" in message:
                self.on_notification(message)
            else:
                future = self.waiting.get(message.get("id"))
                if future is not None and not future.done():
                    future.set_result(message.get("result") if "result" in message else {"error": message.get("error")})

    def on_notification(self, message: dict[str, Any]) -> None:
        if message["method"] == "session/cancel":
            turn = self.running.get((message.get("params") or {}).get("sessionId"))
            if turn is not None and not turn["stuck"]:
                turn["cancel"].set()

    async def on_request(self, message: dict[str, Any]) -> None:
        method, request_id, params = message["method"], message["id"], message.get("params") or {}
        try:
            if method == "initialize":
                if self.initialized:
                    raise RpcError(-32600, "Already initialized")
                self.initialized = True
                result: Any = {
                    "protocolVersion": 1,
                    "agentCapabilities": {
                        "loadSession": True,
                        "promptCapabilities": {"image": True, "audio": False, "embeddedContext": True},
                        "sessionCapabilities": {"list": {}, "delete": {}, "close": {}},
                        "_meta": {"cognition.ai/midTurnPrompts": True},
                    },
                    "authMethods": [{"id": "devin-browser", "name": "Log in with browser"}],
                }
            elif not self.initialized:
                raise RpcError(-32600, "Not initialized")
            elif method == "session/prompt":
                result = await self.m_prompt(request_id, params)
                if result is None:
                    return  # answered later, with the turn
            else:
                handler = getattr(self, "m_" + method.replace("/", "_"), None)
                if handler is None:
                    raise RpcError(-32601, f"Method not found: {method}")
                result = handler(params)
                if asyncio.iscoroutine(result):
                    result = await result
        except RpcError as error:
            self.send({"id": request_id, "error": {"code": error.code, "message": error.message}})
            return
        self.send({"id": request_id, "result": result})

    # ------------------------------------------------------------------ sessions

    def options(self, session: dict[str, Any]) -> list[dict[str, Any]]:
        row = model_row(session["model"])
        out: list[dict[str, Any]] = [
            {
                "id": "mode", "name": "Session Mode", "category": "mode", "type": "select", "currentValue": session["mode"],
                "options": [{"value": value, "name": name, "description": text} for value, name, text in MODES],
            },
            {
                "id": "model", "name": "Model", "category": "model", "type": "select", "currentValue": session["model"],
                "options": [
                    {"value": r[0], "name": r[1], "_meta": {"cognition.ai/supportsImages": r[2]}} for r in MODELS
                ] + [{"value": FUSION, "name": "Fusion", "_meta": {"cognition.ai/supportsImages": True}}],
            },
        ]
        if row[3]:
            out.append({
                "id": "thought_level", "name": "Thinking", "category": "thought_level", "type": "select",
                "currentValue": session["thought_level"] or row[4], "options": [{"value": v, "name": v.title()} for v in row[3]],
            })
        out.append({
            "id": "speed", "name": "Speed", "category": "model_config", "type": "select", "currentValue": session["speed"],
            "options": [{"value": "standard", "name": "Standard"}, {"value": "fast", "name": "Fast"}],
        })
        return out

    def session(self, params: dict[str, Any]) -> dict[str, Any]:
        found = self.state["sessions"].get(params.get("sessionId"))
        if found is None:
            raise RpcError(-32602, f"Session not found: {params.get('sessionId')}")
        return found

    def m_session_new(self, params: dict[str, Any]) -> Any:
        self.state["counter"] += 1
        number = self.state["counter"]
        sid = f"{WORDS[number % len(WORDS)]}-{WORDS[(number * 5 + 1) % len(WORDS)]}-{number}"
        now = time.time()
        row = MODELS[1]
        self.state["sessions"][sid] = {
            "cwd": params.get("cwd", ""), "title": None, "createdAt": now, "updatedAt": now, "used": False, "log": [],
            "model": row[0], "mode": "accept-edits", "thought_level": row[4], "speed": "standard", "messages": 0,
        }
        self.save()
        session = self.state["sessions"][sid]
        return {
            "sessionId": sid,
            "modes": {"currentModeId": session["mode"], "availableModes": [{"id": v, "name": n} for v, n, _ in MODES]},
            "configOptions": self.options(session),
            "_meta": {},
        }

    def m_session_load(self, params: dict[str, Any]) -> Any:
        session = self.session(params)
        sid = params["sessionId"]
        stats = []
        for entry in session["log"]:
            if "user" in entry:
                for index, part in enumerate(entry["user"]["parts"]):
                    self.update(sid, {
                        "sessionUpdate": "user_message_chunk", "content": part,
                        "_meta": {"cognition.ai/clientMessageId": entry["user"]["id"], "cognition.ai/timestamp": iso(entry["user"]["at"]), "cognition.ai/messageSubIndex": index},
                    }, record=False)
            elif "stats" in entry:
                stats.append(entry["stats"])  # (the real program sends every turn's statistics at the very end of the replay)
            else:
                self.update(sid, entry["update"], record=False)
        for params_ in stats:
            self.notify("_cognition.ai/turn_stats", params_)
        if session.get("last_call"):
            meta = session["last_call"]
            for extra in ({}, {"cognition.ai/subagent_context": {"parentAgentId": "root"}}):
                self.update(sid, {"sessionUpdate": "usage_update", "used": meta["used"], "size": meta["size"], "_meta": {**meta["meta"], **extra}}, record=False)
        return {"modes": {"currentModeId": session["mode"], "availableModes": []}, "configOptions": self.options(session)}

    def m_session_list(self, params: dict[str, Any]) -> Any:
        out = []
        for sid, session in self.state["sessions"].items():
            if session["cwd"] != params.get("cwd"):
                continue
            out.append({
                "sessionId": sid, "cwd": session["cwd"], "title": session["title"], "updatedAt": iso_seconds(session["updatedAt"]),
                "_meta": {"cognition.ai/createdAt": iso_z(session["createdAt"]), "cognition.ai/hasUserMessages": session["used"],
                          "cognition.ai/isLocked": False, "cognition.ai/lockHolderPid": None},
            })
        return {"sessions": out}

    def m_session_delete(self, params: dict[str, Any]) -> Any:
        self.session(params)
        del self.state["sessions"][params["sessionId"]]
        self.save()
        return {}

    def m_session_close(self, params: dict[str, Any]) -> Any:
        self.session(params)
        return {}

    def m_session_set_config_option(self, params: dict[str, Any]) -> Any:
        session = self.session(params)
        config_id, value = params.get("configId"), params.get("value")
        if config_id == "model":
            if value not in {row[0] for row in MODELS}:
                raise RpcError(-32602, f"Invalid value for model: {value}")
            session["model"] = value
            session["thought_level"] = model_row(value)[4]
        elif config_id == "thought_level":
            if value not in model_row(session["model"])[3]:
                raise RpcError(-32602, f"Invalid value for thought_level: {value}")
            session["thought_level"] = value
        elif config_id == "speed" and value in ("standard", "fast"):
            session["speed"] = value
        elif config_id == "mode" and value in {m[0] for m in MODES}:
            session["mode"] = value
        else:
            raise RpcError(-32602, f"Invalid config option: {config_id}")
        self.save()
        options = self.options(session)
        self.update(params["sessionId"], {"sessionUpdate": "config_option_update", "configOptions": options}, record=False)
        return {"configOptions": options}

    def m_session_set_mode(self, params: dict[str, Any]) -> Any:
        session = self.session(params)
        if params.get("modeId") not in {m[0] for m in MODES}:
            raise RpcError(-32602, f"Invalid mode: {params.get('modeId')}")
        session["mode"] = params["modeId"]
        self.save()
        self.update(params["sessionId"], {"sessionUpdate": "current_mode_update", "currentModeId": session["mode"]}, record=False)
        return {}

    # ------------------------------------------------------------------ prompts

    @staticmethod
    def texts(params: dict[str, Any]) -> list[str]:
        return [block.get("text", "") for block in params.get("prompt") or () if block.get("type") == "text"]

    async def m_prompt(self, request_id: Any, params: dict[str, Any]) -> Any:
        session = self.session(params)
        sid = params["sessionId"]
        texts = self.texts(params)
        text = texts[0] if texts else ""
        self.state["counter"] += 1
        message_id = f"msg-{self.state['counter']}"
        if "[fail]" in text:
            raise RpcError(-32000, "Insufficient credits")
        turn = self.running.get(sid)
        user = {"user": {"id": message_id, "parts": list(params.get("prompt") or ()), "at": time.time()}}
        session["used"] = True
        session["messages"] += 1
        session["updatedAt"] = time.time()
        if turn is not None:  # a prompt sent to a running turn is merged into it
            # (the real program lists it right after the turn's own prompt, whenever it was sent)
            session["log"].insert(turn["user_at"] + len(turn["prompts"]), user)
            turn["prompts"].append((request_id, message_id, text))
            turn["split"].append(len(turn["calls"]))
            turn["merged"].set()
            return None
        user_at = len(session["log"])
        session["log"].append(user)
        first = session["title"] is None
        if first:
            session["title"] = text[:60]
            self.update(sid, {"sessionUpdate": "session_info_update", "title": session["title"]})
        turn = {
            "user_at": user_at,
            "prompts": [(request_id, message_id, text)], "calls": [], "split": [0], "cancel": asyncio.Event(), "merged": asyncio.Event(),
            "stuck": "[stuck]" in text, "blocks": len(texts) - 1 + sum(1 for b in params.get("prompt") or () if b.get("type") == "image"),
        }
        self.running[sid] = turn
        task = asyncio.ensure_future(self.run_turn(sid, session, turn, text))
        self.tasks.add(task)
        task.add_done_callback(self.tasks.discard)
        return None

    def call(self, sid: str, session: dict[str, Any], turn: dict[str, Any], read: int, written: int, cached: int) -> None:
        """One model call: ``usage_update`` twice, the second as the root agent's (as the real program does)."""
        window = model_row(session["model"])[5] or 200000
        meta = {"cognition.ai/inputTokens": read, "cognition.ai/outputTokens": written, "cognition.ai/cachedReadTokens": cached}
        turn["calls"].append((read, written, cached))
        session["last_call"] = {"used": read + written, "size": window, "meta": meta}
        for extra in ({}, {"cognition.ai/subagent_context": {"parentAgentId": "root"}}):
            self.update(sid, {"sessionUpdate": "usage_update", "used": read + written, "size": window, "_meta": {**meta, **extra}}, record=False)

    def stats(self, message_id: str, calls: list[tuple[int, int, int]]) -> dict[str, Any]:
        def dimension(uid: str, value: float) -> dict[str, Any]:
            return {"uid": uid, "groupTitle": "Token Usage", "label": uid, "kind": {"type": "cumulativeMetric", "value": float(value)}}

        params: dict[str, Any] = {"sessionId": "", "turnClientMessageId": message_id}
        if calls:
            params["responseDimensions"] = [
                dimension("input_tokens", sum(r - c for r, _, c in calls)),
                dimension("output_tokens", sum(w for _, w, _ in calls)),
                dimension("cached_input_tokens", sum(c for _, _, c in calls)),
            ]
        return params

    def tool(self, sid: str, call_id: str, title: str, kind: str, raw: dict[str, Any], native: str, content: Any = None) -> None:
        update: dict[str, Any] = {"sessionUpdate": "tool_call", "toolCallId": call_id, "title": title, "kind": kind, "rawInput": raw,
                                  "_meta": {"cognition.ai/inferenceToolName": native}}
        if content is not None:
            update["content"] = content
        self.update(sid, update)

    def progress(self, sid: str, call_id: str, native: str, status: str | None = None, content: Any = None, meta: dict[str, Any] | None = None) -> None:
        update: dict[str, Any] = {"sessionUpdate": "tool_call_update", "toolCallId": call_id, "_meta": {"cognition.ai/inferenceToolName": native, **(meta or {})}}
        if status:
            update["status"] = status
        if content is not None:
            update["content"] = content
        self.update(sid, update)

    @staticmethod
    def words(text: str) -> list[dict[str, Any]]:
        return [{"type": "content", "content": {"type": "text", "text": text}}]

    async def run_turn(self, sid: str, session: dict[str, Any], turn: dict[str, Any], text: str) -> None:
        stop = "end_turn"
        try:
            if "[crash]" in text:
                self.update(sid, {"sessionUpdate": "agent_message_chunk", "content": {"type": "text", "text": "about to crash"}})
                await asyncio.sleep(0.05)
                os._exit(3)
            elif "[full]" in text:
                await self.scenario_full(sid, session, turn)
            elif "[approve]" in text:
                await self.scenario_approve(sid, session, turn)
            elif "[slow]" in text or "[stuck]" in text:
                stop = await self.scenario_slow(sid, session, turn)
            else:
                await asyncio.sleep(0.02)
                echo = f"echo: {text}" + (f" (+{turn['blocks']} more)" if turn["blocks"] else "")
                half = len(echo) // 2
                for piece in (echo[:half], echo[half:]):
                    self.update(sid, {"sessionUpdate": "agent_message_chunk", "content": {"type": "text", "text": piece}})
                self.call(sid, session, turn, 1200, 12, 300)
        finally:
            await self.finish(sid, session, turn, stop)

    async def finish(self, sid: str, session: dict[str, Any], turn: dict[str, Any], stop: str) -> None:
        calls, split = turn["calls"], turn["split"] + [len(turn["calls"])]
        for index, (request_id, message_id, _) in enumerate(turn["prompts"]):
            mine = calls[split[index] : split[index + 1]]
            stats = self.stats(message_id, [] if stop == "cancelled" else mine)
            stats["sessionId"] = sid
            session["log"].append({"stats": stats})
            self.notify("_cognition.ai/turn_stats", stats)
        self.notify("_cognition.ai/agent_stopped", {"cause": "cancelled" if stop == "cancelled" else "complete", "sessionId": sid})
        last = calls[-1] if calls else (0, 0, 0)
        usage = {"totalTokens": last[0] + last[1], "inputTokens": last[0], "outputTokens": last[1], "cachedReadTokens": last[2]}
        last_message = turn["prompts"][-1][1]
        self.running.pop(sid, None)
        session["updatedAt"] = time.time()
        self.save()
        lag = "[lag]" in turn["prompts"][0][2]
        for number, (request_id, _, _) in enumerate(turn["prompts"]):
            if lag and number:
                await asyncio.sleep(0.4)  # (a prompt merged into the turn is answered a moment after the one it joined)
            self.send({"id": request_id, "result": {"stopReason": stop, "usage": usage, "_meta": {"cognition.ai/userMessageId": last_message}}})

    async def scenario_full(self, sid: str, session: dict[str, Any], turn: dict[str, Any]) -> None:
        self.update(sid, {"sessionUpdate": "agent_thought_chunk", "content": {"type": "text", "text": "Thinking about it."}})
        self.notify("_cognition.ai/thinking_complete", {"durationMs": 12, "blockIndex": 0, "sessionId": sid})
        self.update(sid, {"sessionUpdate": "agent_message_chunk", "content": {"type": "text", "text": "I will look around. "}})
        shell = {"type": "content", "content": {"type": "resource", "resource": {"mimeType": "text/x-shellscript", "text": "echo hi", "uri": "tool://preview"}}}
        self.tool(sid, "exec:0#a1", "Ran echo", "execute", {"command": "echo hi", "timeout": 20000}, "exec", [shell])
        self.progress(sid, "exec:0#a1", "exec", "in_progress")
        await asyncio.sleep(0.02)
        self.progress(sid, "exec:0#a1", "exec", "in_progress", self.words("line 1"), {"cognition.ai/terminalPreview": True})
        await asyncio.sleep(0.02)
        self.progress(sid, "exec:0#a1", "exec", "in_progress", self.words("line 1\nline 2"), {"cognition.ai/terminalPreview": True})
        self.progress(sid, "exec:0#a1", "exec", "in_progress", self.words("line 1\nline 2"), {"terminal_exit": {"terminal_id": "t1", "exit_code": 1, "signal": None}})
        self.progress(sid, "exec:0#a1", "exec", "completed")
        self.call(sid, session, turn, 1000, 20, 200)
        path = str(Path(session["cwd"]) / "new.txt")
        self.tool(sid, "write:0#b2", "Wrote .\\new.txt", "edit", {"file_path": path, "content": "alpha\n"}, "write",
                  [{"type": "diff", "path": path, "newText": "alpha\n"}])
        self.progress(sid, "write:0#b2", "write", "completed")
        self.tool(sid, "edit:0#c3", "Edit file", "edit", {"file_path": path, "old_string": "alpha", "new_string": "beta"}, "edit",
                  [{"type": "diff", "path": path, "oldText": "alpha\n", "newText": "beta\n"}])
        self.progress(sid, "edit:0#c3", "edit", "completed")
        self.tool(sid, "grep:0#d4", "Search for 'beta'", "search", {"query": "beta", "path": session["cwd"]}, "grep")
        self.progress(sid, "grep:0#d4", "grep", "in_progress", [{"type": "content", "content": {"type": "resource_link", "name": "new.txt", "uri": "file:///new.txt"}}])
        self.progress(sid, "grep:0#d4", "grep", "completed")
        self.call(sid, session, turn, 1500, 30, 900)
        self.update(sid, {"sessionUpdate": "agent_message_chunk", "content": {"type": "text", "text": "All done."}})

    async def scenario_approve(self, sid: str, session: dict[str, Any], turn: dict[str, Any]) -> None:
        self.update(sid, {"sessionUpdate": "agent_message_chunk", "content": {"type": "text", "text": "Cleaning up."}})
        self.tool(sid, "exec:0#p1", "Ran rm", "execute", {"command": "rm -rf build", "timeout": 20000}, "exec")
        if session["mode"] == "bypass":
            answer = {"outcome": {"outcome": "selected", "optionId": "bypass"}}
        else:
            answer = await self.ask("session/request_permission", {
                "sessionId": sid,
                "toolCall": {"toolCallId": "exec:0#p1", "_meta": {"cognition.ai/editableCommand": "rm -rf build", "cognition.ai/permissionRule": "Write(proj)"}},
                "options": PERMISSION_OPTIONS,
            })
        outcome = (answer or {}).get("outcome") or {}
        chosen = outcome.get("optionId") if outcome.get("outcome") == "selected" else None
        if chosen in ("allow_once", "allow_session", "allow_always", "switch_bypass", "bypass"):
            if chosen == "switch_bypass":
                session["mode"] = "bypass"
                self.update(sid, {"sessionUpdate": "current_mode_update", "currentModeId": "bypass"}, record=False)
            self.progress(sid, "exec:0#p1", "exec", "in_progress", self.words("removed"), {"terminal_exit": {"exit_code": 0}})
            self.progress(sid, "exec:0#p1", "exec", "completed")
            reply = f"approved: {chosen}"
        else:
            self.progress(sid, "exec:0#p1", "exec", "failed", self.words("Tool execution was rejected: User rejected this tool call"))
            reply = f"rejected: {chosen or outcome.get('outcome')}"
        self.call(sid, session, turn, 1100, 15, 100)
        self.update(sid, {"sessionUpdate": "agent_message_chunk", "content": {"type": "text", "text": reply}})

    async def scenario_slow(self, sid: str, session: dict[str, Any], turn: dict[str, Any]) -> str:
        self.update(sid, {"sessionUpdate": "agent_message_chunk", "content": {"type": "text", "text": "Waiting."}})
        self.tool(sid, "exec:0#s1", "Ran ping", "execute", {"command": "ping -n 12 127.0.0.1"}, "exec")
        self.progress(sid, "exec:0#s1", "exec", "in_progress")
        self.call(sid, session, turn, 1000, 10, 100)
        output = ""
        for number in range(400):
            if turn["cancel"].is_set():
                self.progress(sid, "exec:0#s1", "exec", "failed")
                return "cancelled"
            if turn["merged"].is_set():
                self.progress(sid, "exec:0#s1", "exec", "completed", self.words(output))
                self.call(sid, session, turn, 1300, 25, 1000)
                self.update(sid, {"sessionUpdate": "agent_message_chunk", "content": {"type": "text", "text": f"got: {turn['prompts'][-1][2]}"}})
                return "end_turn"
            if number % 4 == 0:
                output += f"Reply {number}\n"
                self.progress(sid, "exec:0#s1", "exec", "in_progress", self.words(output), {"cognition.ai/terminalPreview": True})
            await asyncio.sleep(0.05)
        return "end_turn"


# ---------------------------------------------------------------------------------------------- the command line


def cli(arguments: list[str]) -> int:
    state = json.loads(STATE.read_text(encoding="utf-8")) if STATE.is_file() else {"account": os.environ.get("FAKE_DEVIN_ACCOUNT", "signed-in"), "sessions": {}, "counter": 0}

    def save() -> None:
        HOME.mkdir(parents=True, exist_ok=True)
        STATE.write_text(json.dumps(state, ensure_ascii=False), encoding="utf-8")

    if arguments[:1] == ["--version"]:
        print(f"devin {CLI_VERSION} (fake)")
        return 0
    if arguments[:2] == ["auth", "status"]:
        if state["account"] == "signed-in":
            print("Logged in (via Devin).\n\nCredentials:\n  File:              C:\\fake\\credentials.toml\n\nUser:\n  Name:              Test User\n"
                  "  Email:             me@example.com\n\nAccount:\n  Tier:              Devin Pro\n  Plan:              Pro\n")
        else:
            print("Not logged in")
        return 0
    if arguments[:2] == ["auth", "login"]:
        delay = os.environ.get("FAKE_DEVIN_LOGIN_DELAY", "0.2")
        if delay == "never":
            time.sleep(3600)
        time.sleep(float(delay))
        state["account"] = "signed-in"
        save()
        print("Logged in.")
        return 0
    if arguments[:2] == ["auth", "logout"]:
        state["account"] = "signed-out"
        save()
        print("Logged out.")
        return 0
    if arguments[:2] == ["models", "list"]:
        if os.environ.get("FAKE_DEVIN_NO_MODELS_JSON") == "1":
            print("error: unexpected argument", file=sys.stderr)
            return 2
        families = []
        for value, name, _, _, _, window, free in MODELS:
            if window:
                families.append({"family_label": name, "slug": value, "variants": [
                    {"model_uid": value, "label": name, "max_context_tokens": window, "cost_tier": "Free" if free else "High cost"}]})
        print(json.dumps({"families": families}))
        return 0
    print(f"error: unrecognized {arguments}", file=sys.stderr)
    return 2


def main() -> int:
    arguments = sys.argv[1:]
    if arguments[:1] != ["acp"]:
        return cli(arguments)
    if os.environ.get("FAKE_DEVIN_BROKEN") == "1":
        return 2
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    loop.run_until_complete(Fake(loop).run())
    return 0


if __name__ == "__main__":
    sys.exit(main())
