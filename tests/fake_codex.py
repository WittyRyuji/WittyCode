"""A stand-in for ``codex app-server`` that speaks the message shapes of codex-cli 0.165.0 (tests only: no network).

Run as ``python tests/fake_codex.py app-server``. Newline-delimited JSON on stdin/stdout, no ``"jsonrpc"`` member,
``emittedAtMs`` next to notification params, ``-32600 Not initialized`` before the handshake, exit code 0 when stdin
closes: the same as the real server (see ``desktop.core.codex``).

State lives in ``$FAKE_CODEX_HOME/state.json`` (the account, the threads and their turns), so a second process (after
a crash, a reconnect, or for history) sees the same threads. Every message received is appended to
``$FAKE_CODEX_HOME/received.jsonl`` for the tests to inspect.

What a turn does depends on markers in the user's text:

- ``[full]``: a reasoning summary in two parts, commentary, a command with output deltas and exit code 1, an edit
  (unified diff), a new file, a plan update, token usage, the final answer;
- ``[approve-command]`` / ``[approve-file]``: an item that needs approval; what happens next follows the decision;
- ``[question]``: an ``item/tool/requestUserInput`` request;
- ``[elicit-form]`` / ``[elicit-confirm]`` / ``[elicit-approval]`` / ``[elicit-url]`` / ``[elicit-odd]``: an
  ``mcpServer/elicitation/request`` (a form with five kinds of field, a bare yes / no, Codex asking whether an MCP tool
  may run, a link to open, a mode nobody knows); the answer is echoed;
- ``[orphan-approval]``: an approval request, then the turn ends at once without waiting or resolving it;
- ``[slow]``: a command that prints a line every 50 ms until the turn is interrupted (``turn/steer`` is accepted meanwhile;
  with ``[nosteer]`` as well it is refused, as for a review);
- ``[stuck]``: like ``[slow]``, but an interrupt is answered and then ignored (the turn never ends);
- ``[crash]``: some text, then the process exits with code 3;
- ``[fail]``: ``error`` (``usageLimitExceeded``, ``willRetry: false``) and a failed turn;
- ``[retry]``: two ``error`` notifications with ``willRetry: true``, then the echo answer;
- ``[late-usage]``: the echo answer, with the token usage after ``turn/completed``;
- anything else: one answer, ``echo: <the whole first text input>``, in two deltas.

Environment: ``FAKE_CODEX_ACCOUNT`` = signed-out | chatgpt | apikey (until the state file has one);
``FAKE_CODEX_LOGIN_DELAY`` (seconds, default 0.2, or ``never``); ``FAKE_CODEX_DOWNGRADE=1`` (workspace-write comes
back read-only, as on a Windows machine without Codex's sandbox); ``FAKE_CODEX_BROKEN=1`` (exit 2 at once).
"""

from __future__ import annotations

import asyncio
import itertools
import json
import os
import sys
import threading
import time
import uuid
from pathlib import Path
from typing import Any

HOME = Path(os.environ.get("FAKE_CODEX_HOME") or Path.cwd() / ".fake-codex")
STATE = HOME / "state.json"
RECEIVED = HOME / "received.jsonl"
CONTEXT_WINDOW = 272_000
EFFORTS = ["low", "medium", "high", "xhigh"]
MODELS = [
    {"id": "gpt-6.1-sol", "model": "gpt-6.1-sol", "displayName": "GPT-6.1-Sol", "description": "Workhorse.", "hidden": False,
     "supportedReasoningEfforts": [{"reasoningEffort": e, "description": e} for e in EFFORTS],
     "defaultReasoningEffort": "low", "inputModalities": ["text", "image"], "supportsPersonality": False, "isDefault": True},
    {"id": "gpt-6-luna", "model": "gpt-6-luna", "displayName": "GPT-6-Luna", "description": "Fast.", "hidden": False,
     "supportedReasoningEfforts": [{"reasoningEffort": e, "description": e} for e in EFFORTS[:3]],
     "defaultReasoningEffort": "medium", "inputModalities": ["text"], "supportsPersonality": False, "isDefault": False},
    {"id": "gpt-hidden", "model": "gpt-hidden", "displayName": "Hidden", "description": "", "hidden": True,
     "supportedReasoningEfforts": [], "defaultReasoningEffort": "low", "inputModalities": ["text"], "isDefault": False},
]
APPROVALS = ("untrusted", "on-request", "never")
SANDBOXES = ("read-only", "workspace-write", "danger-full-access")


class RpcError(Exception):
    def __init__(self, code: int, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


def now_ms() -> int:
    return int(time.time() * 1000)


class Fake:
    def __init__(self, loop: asyncio.AbstractEventLoop) -> None:
        self.loop = loop
        self.state = self.load()
        self.initialized = False
        self.loaded: set[str] = set()
        self.running: dict[str, tuple[str, asyncio.Task[None], str]] = {}
        self.waiting: dict[int, asyncio.Future[Any]] = {}
        self.server_ids = itertools.count(1000)
        self.login: tuple[str, asyncio.Task[None] | None] | None = None
        self.lock = threading.Lock()

    # ------------------------------------------------------------------ state and wire

    def load(self) -> dict[str, Any]:
        if STATE.is_file():
            return json.loads(STATE.read_text(encoding="utf-8"))
        return {"account": os.environ.get("FAKE_CODEX_ACCOUNT", "signed-out"), "threads": {}}

    def save(self) -> None:
        HOME.mkdir(parents=True, exist_ok=True)
        temporary = STATE.with_suffix(".tmp")
        temporary.write_text(json.dumps(self.state, ensure_ascii=False), encoding="utf-8")
        os.replace(temporary, STATE)

    def send(self, message: dict[str, Any]) -> None:
        data = (json.dumps(message, ensure_ascii=False) + "\n").encode("utf-8")
        with self.lock:
            sys.stdout.buffer.write(data)
            sys.stdout.buffer.flush()

    def notify(self, method: str, params: dict[str, Any]) -> None:
        self.send({"method": method, "params": params, "emittedAtMs": now_ms()})

    async def ask(self, method: str, params: dict[str, Any], thread_id: str) -> Any:
        """A request to the client (an approval); serverRequest/resolved follows the answer, or the cancellation."""
        request_id = next(self.server_ids)
        future: asyncio.Future[Any] = self.loop.create_future()
        self.waiting[request_id] = future
        self.send({"id": request_id, "method": method, "params": params})
        try:
            return await future
        finally:
            self.waiting.pop(request_id, None)
            self.notify("serverRequest/resolved", {"threadId": thread_id, "requestId": request_id})

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
                await self.on_request(message)
            elif "method" in message:
                continue  # initialized
            else:
                future = self.waiting.get(message.get("id"))
                if future is not None and not future.done():
                    future.set_result(message.get("result") if "result" in message else {"error": message.get("error")})

    async def on_request(self, message: dict[str, Any]) -> None:
        method, request_id, params = message["method"], message["id"], message.get("params") or {}
        if method == "turn/interrupt":
            # 0.165.0 answers only while the turn runs (an interrupt for an ended turn gets no answer at all)
            running = self.running.get(params.get("threadId"))
            if running is not None and running[0] == params.get("turnId"):
                self.send({"id": request_id, "result": {}})
                if "[stuck]" not in running[2]:
                    running[1].cancel()
            return
        try:
            if method == "initialize":
                if self.initialized:
                    raise RpcError(-32600, "Already initialized")
                self.initialized = True
                result: Any = {"userAgent": "wittycode/0.165.0 (fake)", "codexHome": str(HOME), "platformFamily": "windows", "platformOs": "windows"}
            elif not self.initialized:
                raise RpcError(-32600, "Not initialized")
            else:
                handler = getattr(self, "m_" + method.replace("/", "_"), None)
                if handler is None:
                    raise RpcError(-32600, f"Invalid request: unknown variant `{method}`")
                result = handler(params)
                if asyncio.iscoroutine(result):
                    result = await result
        except RpcError as error:
            self.send({"id": request_id, "error": {"code": error.code, "message": error.message}})
            return
        self.send({"id": request_id, "result": result})

    # ------------------------------------------------------------------ account

    def account(self) -> dict[str, Any] | None:
        kind = self.state.get("account")
        if kind == "chatgpt":
            return {"type": "chatgpt", "email": "me@example.com", "planType": "pro"}
        if kind == "apikey":
            return {"type": "apiKey"}
        return None

    def m_account_read(self, params: dict[str, Any]) -> Any:
        return {"account": self.account(), "requiresOpenaiAuth": True, "workspaceRouting": None}

    def m_account_rateLimits_read(self, params: dict[str, Any]) -> Any:
        if self.state.get("account") != "chatgpt":
            raise RpcError(-32600, "codex account authentication required to read rate limits")
        moment = int(time.time())
        snapshot = {
            "limitId": "codex", "limitName": None, "normalModelSlug": None,
            "primary": {"usedPercent": 17, "windowDurationMins": 300, "resetsAt": moment + 3 * 3600 + 12 * 60 + 30},
            "secondary": {"usedPercent": 41.4, "windowDurationMins": 10080, "resetsAt": moment + 4 * 86400 + 6 * 3600 + 30},
            "credits": None, "individualLimit": None, "spendControlReached": None, "planType": "pro", "rateLimitReachedType": None,
        }
        return {"ordinaryUsageAllowed": True, "rateLimits": snapshot, "rateLimitsByLimitId": {"codex": snapshot},
                "rateLimitResetCredits": None, "accountId": None, "rateLimitUpsell": None}

    def m_account_login_start(self, params: dict[str, Any]) -> Any:
        if params.get("type") != "chatgpt":
            raise RpcError(-32600, "only chatgpt in the fake")
        login_id = str(uuid.uuid4())
        delay = os.environ.get("FAKE_CODEX_LOGIN_DELAY", "0.2")
        task = None if delay == "never" else asyncio.ensure_future(self.complete_login(login_id, float(delay)))
        self.login = (login_id, task)
        return {"type": "chatgpt", "loginId": login_id, "authUrl": f"https://auth.openai.com/oauth/authorize?state={login_id}"}

    async def complete_login(self, login_id: str, delay: float) -> None:
        await asyncio.sleep(delay)
        self.state["account"] = "chatgpt"
        self.save()
        self.login = None
        self.notify("account/login/completed", {"loginId": login_id, "success": True, "error": None, "onboardingEntrypoint": None})
        self.notify("account/updated", {"authMode": "chatgpt", "planType": "pro"})

    def m_account_login_cancel(self, params: dict[str, Any]) -> Any:
        if self.login is None or self.login[0] != params.get("loginId"):
            return {"status": "notFound"}
        login_id, task = self.login
        if task is not None:
            task.cancel()
        self.login = None
        self.notify("account/login/completed", {"loginId": login_id, "success": False, "error": "Login was not completed", "onboardingEntrypoint": None})
        return {"status": "canceled"}

    def m_account_logout(self, params: dict[str, Any]) -> Any:
        self.state["account"] = "signed-out"
        self.save()
        self.notify("account/updated", {"authMode": None, "planType": None})
        return {}

    def m_model_list(self, params: dict[str, Any]) -> Any:
        data = [m for m in MODELS if params.get("includeHidden") or not m["hidden"]]
        return {"data": data, "nextCursor": None}

    def m_windowsSandbox_readiness(self, params: dict[str, Any]) -> Any:
        return {"status": "notConfigured"}

    # ------------------------------------------------------------------ threads

    def sandbox(self, mode: str) -> dict[str, Any]:
        if mode == "workspace-write" and os.environ.get("FAKE_CODEX_DOWNGRADE") == "1":
            mode = "read-only"
        if mode == "workspace-write":
            return {"type": "workspaceWrite", "writableRoots": [], "networkAccess": False, "excludeTmpdirEnvVar": False, "excludeSlashTmp": False}
        if mode == "danger-full-access":
            return {"type": "dangerFullAccess"}
        return {"type": "readOnly", "networkAccess": False}

    def thread_object(self, record: dict[str, Any], *, turns: bool = False) -> dict[str, Any]:
        return {
            "id": record["id"], "sessionId": record["id"], "forkedFromId": None, "parentThreadId": None,
            "preview": record.get("preview", ""), "ephemeral": False, "historyMode": "paginated", "modelProvider": "openai",
            "model": record["model"], "reasoningEffort": record.get("effort"), "createdAt": record["createdAt"],
            "updatedAt": record["updatedAt"], "recencyAt": record["updatedAt"],
            "status": {"type": "idle" if record["id"] in self.loaded else "notLoaded"}, "path": record.get("path"), "cwd": record["cwd"],
            "cliVersion": "0.165.0", "originator": "wittycode", "source": record.get("source", "vscode"), "threadSource": None,
            "agentNickname": record.get("nickname"), "agentRole": record.get("role"), "gitInfo": None, "name": record.get("name"),
            "turns": [self.turn_object(turn, full=True) for turn in record["turns"]] if turns else [],
        }

    def opened(self, record: dict[str, Any]) -> dict[str, Any]:
        return {
            "thread": self.thread_object(record), "model": record["model"], "modelProvider": "openai", "serviceTier": None,
            "disabledPluginIds": [], "cwd": record["cwd"], "instructionSources": [], "approvalPolicy": record["approval"],
            "approvalsReviewer": "user", "sandbox": self.sandbox(record["sandbox"]), "reasoningEffort": record.get("effort"),
        }

    def check_settings(self, params: dict[str, Any]) -> None:
        if params.get("approvalPolicy") not in (None, *APPROVALS):
            raise RpcError(-32600, f"Invalid request: unknown variant `{params.get('approvalPolicy')}`")
        if params.get("sandbox") not in (None, *SANDBOXES):
            raise RpcError(-32600, f"Invalid request: unknown variant `{params.get('sandbox')}`")

    def record(self, thread_id: Any) -> dict[str, Any]:
        record = self.state["threads"].get(thread_id)
        if record is None:
            raise RpcError(-32600, f"no rollout found for thread id {thread_id}")
        return record

    def m_thread_start(self, params: dict[str, Any]) -> Any:
        self.check_settings(params)
        moment = int(time.time())
        thread_id = str(uuid.uuid4())
        record = {
            "id": thread_id, "cwd": params.get("cwd") or str(Path.cwd()), "model": params.get("model") or "gpt-6.1-sol",
            "approval": params.get("approvalPolicy") or "on-request", "sandbox": params.get("sandbox") or "read-only",
            "effort": None, "name": None, "preview": "", "createdAt": moment, "updatedAt": moment, "turns": [],
        }
        self.state["threads"][thread_id] = record
        self.loaded.add(thread_id)
        self.save()
        self.loop.call_soon(self.notify, "thread/started", {"thread": self.thread_object(record)})
        return self.opened(record)

    def m_thread_resume(self, params: dict[str, Any]) -> Any:
        self.check_settings(params)
        record = self.record(params.get("threadId"))
        for key, field in (("model", "model"), ("approvalPolicy", "approval"), ("sandbox", "sandbox"), ("cwd", "cwd")):
            if params.get(key):
                record[field] = params[key]
        self.loaded.add(record["id"])
        self.save()
        return self.opened(record)

    def m_thread_list(self, params: dict[str, Any]) -> Any:
        wanted = params.get("cwd")
        folders = set(wanted if isinstance(wanted, list) else [wanted]) if wanted else None
        kinds = params.get("sourceKinds")
        threads = [t for t in self.state["threads"].values() if t["turns"] and (folders is None or t["cwd"] in folders)]
        if kinds:  # a sub-agent's thread is not listed unless asked for, like the real server
            threads = [t for t in threads if t.get("source", "vscode") in kinds]
        threads.sort(key=lambda t: t["updatedAt"], reverse=True)
        limit = params.get("limit") or 25
        return {"data": [self.thread_object(t) for t in threads[:limit]], "nextCursor": None, "backwardsCursor": None}

    def m_thread_read(self, params: dict[str, Any]) -> Any:
        record = self.record(params.get("threadId"))
        if params.get("includeTurns") and not record["turns"]:
            raise RpcError(-32601, "list_turns is not supported yet")
        return {"thread": self.thread_object(record, turns=bool(params.get("includeTurns")))}

    def m_thread_turns_list(self, params: dict[str, Any]) -> Any:
        record = self.record(params.get("threadId"))
        if not record["turns"]:
            raise RpcError(-32601, "list_turns is not supported yet")
        turns = list(record["turns"])
        if params.get("sortDirection", "desc") == "desc":
            turns.reverse()
        start = int(params.get("cursor") or 0)
        limit = int(params.get("limit") or 25)
        page = turns[start : start + limit]
        more = start + limit < len(turns)
        return {"data": [self.turn_object(t, full=True) for t in page], "nextCursor": str(start + limit) if more else None, "backwardsCursor": None}

    def m_thread_name_set(self, params: dict[str, Any]) -> Any:
        record = self.record(params.get("threadId"))
        record["name"] = params.get("name")
        self.save()
        self.loop.call_soon(self.notify, "thread/name/updated", {"threadId": record["id"], "threadName": record["name"]})
        return {}

    def m_thread_unsubscribe(self, params: dict[str, Any]) -> Any:
        self.loaded.discard(params.get("threadId"))
        return {"status": "unsubscribed"}

    # ------------------------------------------------------------------ turns

    def turn_object(self, turn: dict[str, Any], *, full: bool = False) -> dict[str, Any]:
        return {
            "id": turn["id"], "rootTurnId": turn["id"], "items": list(turn["items"]) if full else [],
            "itemsView": "full" if full else "notLoaded", "status": turn["status"], "error": turn.get("error"),
            "startedAt": turn.get("startedAt"), "completedAt": turn.get("completedAt"), "durationMs": turn.get("durationMs"),
        }

    def m_turn_start(self, params: dict[str, Any]) -> Any:
        record = self.record(params.get("threadId"))
        if record["id"] not in self.loaded:
            raise RpcError(-32600, f"thread not loaded: {record['id']}")
        if record["id"] in self.running:
            raise RpcError(-32600, "a turn is already running")
        for key, field in (("model", "model"), ("effort", "effort"), ("approvalPolicy", "approval"), ("cwd", "cwd")):
            if params.get(key):
                record[field] = params[key]
        texts = [part.get("text", "") for part in params.get("input") or [] if part.get("type") == "text"]
        turn = {"id": str(uuid.uuid4()), "items": [], "status": "inProgress", "error": None, "startedAt": int(time.time()),
                "completedAt": None, "durationMs": None}
        record["turns"].append(turn)
        if not record["preview"]:
            record["preview"] = texts[0] if texts else ""
        record["updatedAt"] = int(time.time())
        self.save()
        task = asyncio.ensure_future(self.run_turn(record, turn, params.get("input") or [], texts[0] if texts else ""))
        self.running[record["id"]] = (turn["id"], task, texts[0] if texts else "")
        return {"turn": self.turn_object(turn)}

    def m_thread_compact_start(self, params: dict[str, Any]) -> Any:
        """``thread/compact/start``: answers ``{}`` at once and then reports a turn with a ``contextCompaction`` item, as the real
        app-server does (measured on codex 0.2xx with ``sandbox/probe_codex_compact.py``). A thread whose first message holds
        ``[compact-fail]`` refuses; one that holds ``[compact-slow]`` takes its time."""
        record = self.record(params.get("threadId"))
        if record["id"] not in self.loaded:
            raise RpcError(-32600, f"thread not loaded: {record['id']}")
        if record["id"] in self.running:
            raise RpcError(-32600, "a turn is already running")
        if "[compact-fail]" in record["preview"]:
            raise RpcError(-32000, "there is nothing Codex can compact yet")
        turn = {"id": str(uuid.uuid4()), "items": [], "status": "inProgress", "error": None, "startedAt": int(time.time()),
                "completedAt": None, "durationMs": None}
        record["turns"].append(turn)
        task = asyncio.ensure_future(self.run_compaction(record, turn))
        self.running[record["id"]] = (turn["id"], task, "")
        return {}

    async def run_compaction(self, record: dict[str, Any], turn: dict[str, Any]) -> None:
        began, status = time.time(), "completed"
        try:
            self.notify("turn/started", {"threadId": record["id"], "turn": self.turn_object(turn)})
            item = {"type": "contextCompaction", "id": "compact-" + uuid.uuid4().hex[:8]}
            self.started(record, turn, item)
            await asyncio.sleep(30 if "[compact-slow]" in record["preview"] else 0.05)
            self.completed(record, turn, item)
        except asyncio.CancelledError:
            status = "interrupted"
        finally:
            self.running.pop(record["id"], None)
            turn.update(status=status, completedAt=int(time.time()), durationMs=int((time.time() - began) * 1000))
            self.save()
            self.notify("turn/completed", {"threadId": record["id"], "turn": self.turn_object(turn)})
            self.notify("thread/status/changed", {"threadId": record["id"], "status": {"type": "idle"}})

    def m_turn_steer(self, params: dict[str, Any]) -> Any:
        """0.166.0: the input joins the running turn at once (its ``userMessage`` is echoed) and the model reads it at its
        next step; without a running turn: ``-32600 no active turn to steer``."""
        record = self.record(params.get("threadId"))
        running = self.running.get(record["id"])
        if running is None:
            raise RpcError(-32600, "no active turn to steer")
        if running[0] != params.get("expectedTurnId"):
            raise RpcError(-32600, "expected turn id does not match the active turn")
        if "[nosteer]" in running[2]:
            raise RpcError(-32600, "the active turn cannot be steered (it is a review)")
        if "[end-on-steer]" in running[2]:
            raise RpcError(-32600, "no active turn to steer")  # (the turn ended just before the request arrived)
        turn = next(t for t in record["turns"] if t["id"] == running[0])
        user = {"type": "userMessage", "id": "user-" + uuid.uuid4().hex[:8], "clientId": params.get("clientUserMessageId"),
                "content": params.get("input") or []}
        self.started(record, turn, user)
        self.completed(record, turn, user)
        return {"turnId": turn["id"]}

    def started(self, record: dict[str, Any], turn: dict[str, Any], item: dict[str, Any]) -> None:
        self.notify("item/started", {"item": item, "threadId": record["id"], "turnId": turn["id"], "startedAtMs": now_ms()})

    def completed(self, record: dict[str, Any], turn: dict[str, Any], item: dict[str, Any]) -> None:
        turn["items"].append(item)
        self.notify("item/completed", {"item": item, "threadId": record["id"], "turnId": turn["id"], "completedAtMs": now_ms()})

    def delta(self, record: dict[str, Any], turn: dict[str, Any], method: str, item_id: str, text: str, **extra: Any) -> None:
        self.notify(method, {"threadId": record["id"], "turnId": turn["id"], "itemId": item_id, "delta": text, **extra})

    async def say(self, record: dict[str, Any], turn: dict[str, Any], text: str, *, phase: str = "final_answer") -> None:
        item_id = "msg-" + uuid.uuid4().hex[:8]
        self.started(record, turn, {"type": "agentMessage", "id": item_id, "text": "", "phase": phase})
        half = max(1, len(text) // 2)
        for part in (text[:half], text[half:]):
            if part:
                self.delta(record, turn, "item/agentMessage/delta", item_id, part)
                await asyncio.sleep(0.005)
        self.completed(record, turn, {"type": "agentMessage", "id": item_id, "text": text, "phase": phase})

    def usage(self, record: dict[str, Any], turn: dict[str, Any]) -> None:
        total = {"totalTokens": 15000, "inputTokens": 12000, "cachedInputTokens": 9000, "cacheWriteInputTokens": 0,
                 "outputTokens": 3000, "reasoningOutputTokens": 1000}
        last = {"totalTokens": 14000, "inputTokens": 11500, "cachedInputTokens": 9000, "cacheWriteInputTokens": 0,
                "outputTokens": 2500, "reasoningOutputTokens": 800}
        self.notify("thread/tokenUsage/updated", {"threadId": record["id"], "turnId": turn["id"],
                                                  "tokenUsage": {"total": total, "last": last, "modelContextWindow": CONTEXT_WINDOW}})

    def command(self, record: dict[str, Any], item_id: str, command: str, **extra: Any) -> dict[str, Any]:
        item = {"type": "commandExecution", "id": item_id, "pluginId": None, "scriptPath": None, "command": command,
                "cwd": record["cwd"], "processId": None, "source": "agent", "status": "inProgress",
                "commandActions": [{"type": "unknown", "command": command}], "aggregatedOutput": None, "exitCode": None,
                "durationMs": None}
        item.update(extra)
        return item

    def file_change(self, item_id: str, changes: list[dict[str, Any]], status: str = "inProgress") -> dict[str, Any]:
        return {"type": "fileChange", "id": item_id, "changes": changes, "status": status}

    async def run_turn(self, record: dict[str, Any], turn: dict[str, Any], inputs: list[dict[str, Any]], text: str) -> None:
        began = time.time()
        status, error = "completed", None
        try:
            self.notify("thread/status/changed", {"threadId": record["id"], "status": {"type": "active", "activeFlags": []}})
            self.notify("turn/started", {"threadId": record["id"], "turn": self.turn_object(turn)})
            user = {"type": "userMessage", "id": "user-" + uuid.uuid4().hex[:8], "clientId": None, "content": inputs}
            self.started(record, turn, user)
            self.completed(record, turn, user)
            if "[full]" in text:
                await self.full_turn(record, turn)
            elif "[subagent" in text:
                await self.subagent(record, turn, asking="[subagent-quiet]" not in text)
            elif "[approve-command]" in text:
                status = await self.approve_command(record, turn)
            elif "[approve-file]" in text:
                await self.approve_file(record, turn)
            elif "[question]" in text:
                await self.question(record, turn)
            elif "[elicit-" in text:
                await self.elicit(record, turn, text.split("[elicit-", 1)[1].split("]", 1)[0])
            elif "[orphan-approval]" in text:
                self.started(record, turn, self.command(record, "cmd-orphan", "echo hi"))
                self.send({"id": next(self.server_ids), "method": "item/commandExecution/requestApproval", "params": {
                    "kind": "command", "threadId": record["id"], "turnId": turn["id"], "itemId": "cmd-orphan",
                    "startedAtMs": now_ms(), "environmentId": None, "command": "echo hi"}})
                await asyncio.sleep(0.2)
            elif "[slow]" in text or "[stuck]" in text:
                item = self.command(record, "cmd-slow", "python -c tick")
                self.started(record, turn, item)
                for number in itertools.count(1):
                    self.delta(record, turn, "item/commandExecution/outputDelta", "cmd-slow", f"tick {number}\n")
                    await asyncio.sleep(0.05)
            elif "[crash]" in text:
                self.started(record, turn, {"type": "agentMessage", "id": "msg-crash", "text": "", "phase": None})
                self.delta(record, turn, "item/agentMessage/delta", "msg-crash", "about to")
                await asyncio.sleep(0.05)
                sys.stdout.flush()
                os._exit(3)
            elif "[fail]" in text:
                error = {"message": "You've hit your usage limit.", "codexErrorInfo": "usageLimitExceeded", "additionalDetails": None}
                self.notify("error", {"error": error, "willRetry": False, "threadId": record["id"], "turnId": turn["id"]})
                status = "failed"
            elif "[retry]" in text:
                for attempt in (1, 2):
                    retry = {"message": f"Reconnecting... {attempt}/5", "codexErrorInfo": {"responseStreamDisconnected": {"httpStatusCode": 502}},
                             "additionalDetails": "bad gateway"}
                    self.notify("error", {"error": retry, "willRetry": True, "threadId": record["id"], "turnId": turn["id"]})
                    await asyncio.sleep(0.05)
                await self.say(record, turn, "echo: " + text)
            elif "[late-usage]" in text:
                await self.say(record, turn, "echo: " + text)
            else:
                await self.say(record, turn, "echo: " + text)
                self.usage(record, turn)
        except asyncio.CancelledError:
            status = "interrupted"
        finally:
            self.running.pop(record["id"], None)
            turn.update(status=status, error=error, completedAt=int(time.time()), durationMs=int((time.time() - began) * 1000))
            record["updatedAt"] = int(time.time())
            self.save()
            self.notify("turn/completed", {"threadId": record["id"], "turn": self.turn_object(turn)})
            if "[late-usage]" in text:
                self.usage(record, turn)
            self.notify("thread/status/changed", {"threadId": record["id"], "status": {"type": "idle"}})

    async def full_turn(self, record: dict[str, Any], turn: dict[str, Any]) -> None:
        cwd = record["cwd"]
        self.started(record, turn, {"type": "reasoning", "id": "rs-1", "summary": [], "content": []})
        self.notify("item/reasoning/summaryPartAdded", {"threadId": record["id"], "turnId": turn["id"], "itemId": "rs-1", "summaryIndex": 0})
        for index, part in ((0, "**Planning**"), (0, " the fix"), (1, "Then test")):
            self.delta(record, turn, "item/reasoning/summaryTextDelta", "rs-1", part, summaryIndex=index)
            await asyncio.sleep(0.005)
        self.completed(record, turn, {"type": "reasoning", "id": "rs-1", "summary": ["**Planning** the fix", "Then test"], "content": []})
        await self.say(record, turn, "I'll run the tests.", phase="commentary")
        self.started(record, turn, self.command(record, "cmd-1", "pytest -q"))
        for line in ("1 failed\n", "2 passed\n"):
            self.delta(record, turn, "item/commandExecution/outputDelta", "cmd-1", line)
            await asyncio.sleep(0.005)
        self.completed(record, turn, self.command(record, "cmd-1", "pytest -q", status="failed", aggregatedOutput="1 failed\n2 passed\n", exitCode=1, durationMs=1234))
        edit = [{"path": cwd + "\\app.py", "kind": {"type": "update", "move_path": None},
                 "diff": "@@ -1,3 +1,3 @@\n def main():\n-    return 1\n+    return 2\n \n"}]
        self.started(record, turn, self.file_change("fc-1", edit))
        self.completed(record, turn, self.file_change("fc-1", edit, "completed"))
        create = [{"path": cwd + "\\notes.md", "kind": {"type": "add"}, "diff": "# Notes\nhello\n"}]
        self.started(record, turn, self.file_change("fc-2", create))
        self.completed(record, turn, self.file_change("fc-2", create, "completed"))
        plan = [{"step": "Run the tests", "status": "completed"}, {"step": "Fix app.py", "status": "inProgress"}, {"step": "Run again", "status": "pending"}]
        self.notify("turn/plan/updated", {"threadId": record["id"], "turnId": turn["id"], "explanation": "Fix the bug", "plan": plan})
        self.usage(record, turn)
        await self.say(record, turn, "Done: fixed.")

    async def approve_command(self, record: dict[str, Any], turn: dict[str, Any]) -> str:
        item = self.command(record, "cmd-ask", "Remove-Item build -Recurse")
        self.started(record, turn, item)
        answer = await self.ask("item/commandExecution/requestApproval", {
            "kind": "command", "threadId": record["id"], "turnId": turn["id"], "itemId": "cmd-ask", "startedAtMs": now_ms(),
            "approvalId": None, "environmentId": None, "reason": "deletes the build folder", "command": item["command"],
            "cwd": record["cwd"], "commandActions": item["commandActions"]}, record["id"])
        decision = (answer or {}).get("decision")
        if decision in ("accept", "acceptForSession"):
            self.delta(record, turn, "item/commandExecution/outputDelta", "cmd-ask", "removed\n")
            self.completed(record, turn, self.command(record, "cmd-ask", item["command"], status="completed", aggregatedOutput="removed\n", exitCode=0, durationMs=5))
            await self.say(record, turn, f"ran it ({decision})")
            return "completed"
        self.completed(record, turn, self.command(record, "cmd-ask", item["command"], status="declined"))
        if decision == "cancel":
            return "interrupted"
        await self.say(record, turn, f"not run ({decision})")
        return "completed"

    async def subagent(self, record: dict[str, Any], turn: dict[str, Any], *, asking: bool) -> None:
        """The thread starts one sub-agent (its own thread, notified on this connection under its own id); the agent runs a
        command that needs approval (``asking``), says it is done, and the thread waits for it and answers ``all-done``."""
        moment = int(time.time())
        child_id = "agent-" + uuid.uuid4().hex[:8]
        child = {"id": child_id, "cwd": record["cwd"], "model": "gpt-6.1-sol", "effort": "max", "approval": record["approval"],
                 "sandbox": record["sandbox"], "name": None, "preview": "Write sub.txt with the word hi", "createdAt": moment,
                 "updatedAt": moment, "turns": [], "nickname": "Dirac", "role": "coder", "source": "subAgent"}
        self.state["threads"][child_id] = child
        self.save()
        activity = {"type": "subAgentActivity", "model": "gpt-6.1-sol", "reasoningEffort": "max", "id": "call-spawn", "kind": "started",
                    "agentThreadId": child_id, "agentPath": "/root/coder_write_sub", "startedAtMs": now_ms(), "completedAtMs": None}
        self.started(record, turn, activity)
        self.completed(record, turn, {**activity, "completedAtMs": now_ms()})
        child_turn = {"id": "turn-" + uuid.uuid4().hex[:8], "items": [], "status": "inProgress", "error": None, "startedAt": int(time.time())}
        child["turns"].append(child_turn)
        self.notify("warning", {"threadId": child_id, "message": "A tool server of this thread did not start"})  # as the real one does, before the turn
        self.notify("thread/status/changed", {"threadId": child_id, "status": {"type": "active", "activeFlags": []}})
        self.notify("turn/started", {"threadId": child_id, "turn": {**self.turn_object(child_turn), "rootTurnId": turn["id"]}})
        await asyncio.sleep(0.05)
        await self.say(child, child_turn, "Creating the file.", phase="commentary")
        status = "completed"
        command = "Set-Content -Path sub.txt -Value hi"
        self.started(child, child_turn, self.command(child, "cmd-child", command))
        if asking:
            self.notify("thread/status/changed", {"threadId": child_id, "status": {"type": "active", "activeFlags": ["waitingOnApproval"]}})
            answer = await self.ask("item/commandExecution/requestApproval", {
                "kind": "command", "threadId": child_id, "turnId": child_turn["id"], "itemId": "cmd-child", "startedAtMs": now_ms(),
                "approvalId": None, "environmentId": "local", "reason": "May I create sub.txt containing hi?", "command": command,
                "cwd": child["cwd"], "commandActions": [{"type": "unknown", "command": command}]}, child_id)
            decision = (answer or {}).get("decision")
        else:
            decision = "accept"
        if decision in ("accept", "acceptForSession"):
            self.completed(child, child_turn, self.command(child, "cmd-child", command, status="completed", aggregatedOutput="", exitCode=0, durationMs=7))
            await self.say(child, child_turn, "done")
        else:
            self.completed(child, child_turn, self.command(child, "cmd-child", command, status="declined"))
            status = "interrupted"
        child_turn.update(status=status, completedAt=int(time.time()), durationMs=300)
        child_total = {"totalTokens": 5000, "inputTokens": 4000, "cachedInputTokens": 1000, "cacheWriteInputTokens": 0,
                       "outputTokens": 1000, "reasoningOutputTokens": 0}
        self.notify("thread/tokenUsage/updated", {"threadId": child_id, "turnId": child_turn["id"], "tokenUsage": {
            "total": child_total, "last": child_total, "modelContextWindow": CONTEXT_WINDOW}})
        self.notify("turn/completed", {"threadId": child_id, "turn": self.turn_object(child_turn)})
        self.notify("thread/status/changed", {"threadId": child_id, "status": {"type": "idle"}})
        wait = {"type": "collabAgentToolCall", "id": "call-wait", "tool": "wait", "status": "completed", "senderThreadId": record["id"],
                "receiverThreadIds": [child_id], "prompt": None, "model": None, "reasoningEffort": None, "agentsStates": {}}
        self.started(record, turn, {**wait, "status": "inProgress"})
        self.completed(record, turn, wait)
        await self.say(record, turn, f"all-done ({decision})")

    async def approve_file(self, record: dict[str, Any], turn: dict[str, Any]) -> None:
        changes = [{"path": record["cwd"] + "\\app.py", "kind": {"type": "update", "move_path": None},
                    "diff": "@@ -2,1 +2,1 @@\n-    return 1\n+    return 3\n"}]
        self.started(record, turn, self.file_change("fc-ask", changes))
        answer = await self.ask("item/fileChange/requestApproval", {
            "threadId": record["id"], "turnId": turn["id"], "itemId": "fc-ask", "startedAtMs": now_ms(), "reason": None, "grantRoot": None}, record["id"])
        decision = (answer or {}).get("decision")
        status = "completed" if decision in ("accept", "acceptForSession") else "declined"
        self.completed(record, turn, self.file_change("fc-ask", changes, status))
        await self.say(record, turn, f"file {status} ({decision})")

    async def elicit(self, record: dict[str, Any], turn: dict[str, Any], kind: str) -> None:
        params = {"threadId": record["id"], "turnId": turn["id"], "serverName": "notes", **ELICITATIONS[kind]}
        answer = await self.ask("mcpServer/elicitation/request", params, record["id"])
        await self.say(record, turn, "elicitation: " + json.dumps(answer, sort_keys=True))

    async def question(self, record: dict[str, Any], turn: dict[str, Any]) -> None:
        answer = await self.ask("item/tool/requestUserInput", {
            "threadId": record["id"], "turnId": turn["id"], "itemId": "q-1", "isBlocking": True, "autoResolutionMs": None,
            "questions": [{"id": "color", "header": "Color", "question": "Which color?", "isOther": False, "isSecret": False,
                           "options": [{"label": "red", "description": ""}, {"label": "blue", "description": ""}]}]}, record["id"])
        await self.say(record, turn, "answers: " + json.dumps((answer or {}).get("answers"), sort_keys=True))


ELICITATIONS: dict[str, dict[str, Any]] = {
    "form": {
        "mode": "form",
        "message": "Where should the note go?",
        "requestedSchema": {
            "type": "object",
            "properties": {
                "title": {"type": "string", "title": "Title", "description": "A short title"},
                "pages": {"type": "integer", "title": "Pages"},
                "urgent": {"type": "boolean", "title": "Urgent"},
                "place": {"type": "string", "title": "Place", "oneOf": [{"const": "inbox", "title": "Inbox"}, {"const": "archive", "title": "Archive"}]},
                "tags": {"type": "array", "title": "Tags", "items": {"anyOf": [{"const": "a", "title": "Alpha"}, {"const": "b", "title": "Beta"}]}},
            },
            "required": ["title"],
        },
    },
    "confirm": {"mode": "form", "message": "Allow notes to save this note?", "requestedSchema": {"type": "object", "properties": {}}},
    "approval": {  # (as the real Codex 0.166 asks before it runs a tool of an MCP server)
        "mode": "form",
        "_meta": {
            "codex_approval_kind": "mcp_tool_call", "persist": ["session", "always"], "tool_description": "Save a note.",
            "tool_params": {"title": "Commit messages", "kind": "preference"}, "tool_params_display": [],
        },
        "message": "Allow the notes MCP server to run tool \"save\"?",
        "requestedSchema": {"type": "object", "properties": {}},
    },
    "url": {"mode": "url", "elicitationId": "e-1", "message": "Sign in to notes first.", "url": "https://example.com/signin"},
    "odd": {"mode": "mystery", "message": "?"},
}


def main() -> None:
    if os.environ.get("FAKE_CODEX_BROKEN") == "1":
        sys.stderr.write("fake codex: broken on purpose\n")
        sys.exit(2)
    if len(sys.argv) > 1 and sys.argv[1] == "--version":
        print("codex-cli 0.165.0")
        return
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    loop.run_until_complete(Fake(loop).run())


if __name__ == "__main__":
    main()
