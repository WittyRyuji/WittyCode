"""Optional Factory CLI integration, using its documented stream-jsonrpc wire protocol, never the Factory SDK.

One process owns one native session. The CLI owns authentication and its own files; WittyCode keeps only an index of
sessions opened here. No Factory settings are imported into the application's API-provider configuration.
"""

from __future__ import annotations

import asyncio
import base64
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime, timezone
import json
from pathlib import Path
import shutil
import time
from typing import Any
import uuid

from desktop.core.context import ContextState
from desktop.core.context_envelope import strip, wrap
from desktop.core.devin.rpc import AcpClient
from desktop.core.engine_errors import EngineRpcError
from desktop.core.engine_items import split_handoff
from desktop.core.hooks import Hooks, PromptEvent
from desktop.core.prompts import ActionView, OptionView, QuestionView, OPTION_LABELS, UNSUPPORTED_OPTIONS
from desktop.core.session_state import LiveSettings, SessionState, SESSION_STATE_LABELS, SteerOutcome
from desktop.core.settings import normalize_folder
from desktop.core.transcript import Transcript, TurnStatus
from desktop.core.usage import Tokens

FACTORY_PERMISSIONS = ("off", "low", "medium", "high")
EXEC_ARGS = ("exec", "--input-format", "stream-jsonrpc", "--output-format", "stream-jsonrpc")


class FactoryClient(AcpClient):
    """Ordered JSONL replies/notifications, with Factory's versioned JSON-RPC envelope."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._ids = iter(lambda: str(uuid.uuid4()), None)

    async def _write(self, message):
        await super()._write({"factoryApiVersion": "1.0.0", "factoryProtocolVersion": "1.221.0", "type": "request" if "method" in message else "response", **message})


def _mapping(value):
    return value if isinstance(value, Mapping) else {}


def _text(value):
    return value if isinstance(value, str) else ""


class FactoryEngine:
    def __init__(self, settings, root: Path, *, client_factory=None, locate=None):
        self.settings, self.root = settings, Path(root)
        self._client_factory = client_factory
        self._locate = locate or shutil.which
        self.controllers = []
        self._listeners, self._account_listeners = [], []
        self.memory = None
        self.connections = None
        self.hooks = Hooks()
        self._models = []
        self._index = self._read_index()

    def _read_index(self):
        try:
            raw = json.loads((self.root / "index.json").read_text(encoding="utf-8"))
            return raw if isinstance(raw, dict) else {}
        except (OSError, ValueError):
            return {}

    def _remember(self, controller):
        if not controller.session_id or not controller.transcript.turns:
            return
        self._index[controller.session_id] = {"title": controller.title, "project": str(controller.project), "modified": controller.active_at, "created": controller.created_at, "model": controller.model, "usage": controller._usage.to_json() if controller._usage else None, "models": {k: v.to_json() for k, v in controller._usage_models.items()}}
        self.root.mkdir(parents=True, exist_ok=True)
        target = self.root / "index.json"
        temporary = target.with_suffix(".tmp")
        temporary.write_text(json.dumps(self._index, ensure_ascii=False), encoding="utf-8")
        temporary.replace(target)

    def binary(self):
        path = self.settings.factory_path.strip()
        return str(Path(path)) if path and Path(path).is_file() else self._locate("droid") if not path else None

    async def account(self):
        path = await asyncio.to_thread(self.binary)
        return {"cli": "found" if path else "missing", "cliPath": path or "", "enabled": self.settings.factory_enabled}

    def add_listener(self, callback):
        self._listeners.append(callback)

    def add_account_listener(self, callback):
        self._account_listeners.append(callback)

    def _notify(self, controller, kind="transcript"):
        for callback in list(self._listeners):
            callback(controller, kind)

    def client(self, project, **handlers):
        if self._client_factory is not None:
            return self._client_factory(project, **handlers)
        binary = self.binary()
        if not binary:
            raise RuntimeError("没有找到 Factory CLI：在外部程序中指定 droid，使用前先在终端登录 Factory。")
        return FactoryClient([binary, *EXEC_ARGS], cwd=project, name="Factory CLI", **handlers)

    async def list_models(self, **_):
        self.root.mkdir(parents=True, exist_ok=True)
        client = self.client(str(self.root))
        try:
            await client.start()
            raw = await client.request("droid.list_models", {"includeDisabled": False}, timeout=30)
            self._models = [dict(m) for m in _mapping(raw).get("models", []) if isinstance(m, Mapping) and m.get("id") and not m.get("disabled")]
            return list(self._models)
        finally:
            await client.close()

    def model_info(self, model):
        return next((item for item in self._models if item["id"] == model), {})

    def model_window(self, model):
        return self.model_info(model).get("contextWindow") or 0

    async def list_sessions(self, project, limit=50):
        from types import SimpleNamespace

        rows = []
        for native, raw in self._index.items():
            if _mapping(raw).get("project") == normalize_folder(project):
                rows.append(SimpleNamespace(id=native, title=raw.get("title", ""), model=raw.get("model", ""), message_count=None, modified_at=datetime.fromtimestamp(raw.get("modified", 0), timezone.utc), created_at=datetime.fromtimestamp(raw.get("created", 0), timezone.utc)))
        return sorted(rows, key=lambda row: row.modified_at, reverse=True)[:limit]

    async def open_new(self, project, **options):
        controller = FactoryController(self, Path(normalize_folder(project)), **options)
        self.controllers.append(controller)
        await controller.open()
        return controller

    async def open_saved(self, project, native, *, title=None, modified=None):
        raw = _mapping(self._index.get(native))
        controller = FactoryController(self, Path(normalize_folder(project)), session_id=native, saved_title=title or raw.get("title", ""), created_at=raw.get("created", time.time()))
        controller._usage = Tokens.of(raw.get("usage"))
        controller._usage_models = {k: v for k, item in _mapping(raw.get("models")).items() if (v := Tokens.of(item)) is not None}
        self.controllers.append(controller)
        if modified is not None:
            controller.active_at = modified
        await controller.open(resume=True)
        return controller

    async def close_all(self):
        await asyncio.gather(*(c.close() for c in list(self.controllers)))

    async def path_changed(self):
        self._models = []
        return not any(c.is_open for c in self.controllers)


@dataclass(eq=False)
class FactoryController:
    manager: FactoryEngine
    project: Path
    model: str = ""
    reasoning: str = ""
    autonomy: str = "off"
    mode: str = "auto"
    transcript: Transcript = field(default_factory=Transcript)
    handoff: Any = None
    session_id: str = ""
    saved_title: str = ""
    state: SessionState = SessionState.OPENING
    active_at: float = field(default_factory=time.time)
    used_at: float = field(default_factory=time.time)
    created_at: float = field(default_factory=time.time)
    context: ContextState = field(default_factory=ContextState)
    last_error: str = ""
    eviction_notice: str = ""
    run: str = field(default_factory=lambda: uuid.uuid4().hex[:12])
    _usage: Tokens | None = None
    _usage_models: dict = field(default_factory=dict)
    _pending: dict = field(default_factory=dict)
    _tasks: set = field(default_factory=set)
    _client: Any = None
    _history_loaded: bool = False
    _close_requested: bool = False
    _turn_model: str = ""
    _turn_usage: Tokens | None = None
    engine = "factory"

    @property
    def title(self):
        return self.transcript.title or self.saved_title or "新会话"

    @property
    def busy(self):
        return self.state is SessionState.RUNNING

    @property
    def is_open(self):
        return self.state is not SessionState.CLOSED and not self._close_requested

    @property
    def working_label(self):
        return "等待你的确认" if self.transcript.pending_prompts() else SESSION_STATE_LABELS[self.state]

    def _spawn(self, coroutine):
        task = asyncio.ensure_future(coroutine)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        return task

    def _changed(self, kind="transcript"):
        self.manager._notify(self, kind)

    def live_settings(self):
        return LiveSettings(self.model, self.reasoning, self.autonomy, self.mode)

    def usage_series(self):
        return {"self": self._usage} if self._usage is not None else {}

    def usage_model_series(self):
        return {f"model:{model}": (model, tokens) for model, tokens in self._usage_models.items()}

    def servers(self):
        commands = []
        if self.manager.memory is not None:
            command = self.manager.memory.server_for(self.engine, self.project, self.run)
            if command is not None:
                commands.append(command)
        if self.manager.connections is not None:
            commands.extend(self.manager.connections.mcp_commands(self.project))
        return [{"name": c.name, "command": c.command, "args": list(c.args), "env": c.env} for c in commands]

    async def open(self, resume=False):
        self.state = SessionState.OPENING
        self._changed("state")
        try:
            client = self.manager.client(str(self.project), on_notification=self._notification, on_request=self._request, on_exit=self._lost)
            self._client = client
            await client.start()
            if resume:
                result = await client.request("droid.load_session", {"sessionId": self.session_id, "mcpServers": self.servers()}, timeout=60)
            else:
                params = {"machineId": "wittycode", "cwd": str(self.project), "mcpServers": self.servers(), "autonomyLevel": self.autonomy, "interactionMode": "spec" if self.mode == "spec" else "auto"}
                if self.model:
                    params["modelId"] = self.model
                if self.reasoning:
                    params["reasoningEffort"] = self.reasoning
                result = await client.request("droid.initialize_session", params, timeout=60)
                self.session_id = _text(result.get("sessionId"))
                if not self.session_id:
                    raise RuntimeError("Factory 没有返回会话 ID。")
            result = _mapping(result)
            self._learn(_mapping(result.get("settings")))
            self.transcript.title = _text(_mapping(result.get("session")).get("title")) or self.saved_title
            if resume:
                self._history(_mapping(result.get("session")).get("messages", []))
            self._usage = Tokens.of(_mapping(result.get("session")).get("tokenUsage")) or self._usage
            self._history_loaded = True
            self.state = SessionState.READY
            self.manager._remember(self)
        except asyncio.CancelledError:
            raise
        except Exception as error:
            self.last_error = f"Factory 连接失败：{error}"
            self.state = SessionState.DEAD if resume else SessionState.CLOSED
            self.transcript.add_error(self.last_error, recoverable=resume)
            if self._client is not None:
                await self._client.close()
        self._changed("state")

    def _learn(self, settings):
        actual = _text(settings.get("modelId"))
        if self.busy and actual and actual != self.model:
            self._turn_model = ""  # A mixed turn has no trustworthy per-call split; leave it unassigned.
        self.model = actual or self.model
        self.reasoning = _text(settings.get("reasoningEffort")) or self.reasoning
        self.autonomy = _text(settings.get("autonomyLevel")) or self.autonomy
        self.mode = _text(settings.get("interactionMode")) or self.mode
        self.transcript.model = self.model

    def _history(self, messages):
        turn = None
        for raw in messages:
            raw = _mapping(raw)
            message = _mapping(raw.get("message")) or raw
            content = message.get("content", [])
            if message.get("role") == "user":
                text = "\n".join(_text(block.get("text")) for block in content if isinstance(block, Mapping) and block.get("type") == "text")
                if text:
                    typed, handoff = split_handoff(strip(text))
                    turn = self.transcript.new_history_turn(typed.lstrip("\r\n") if handoff is not None else typed)
                    if turn.user_block is not None:
                        turn.user_block.handoff = handoff
                    self.transcript.turns.append(turn)
            if turn is not None:
                if message.get("role") == "user":
                    content = [block for block in content if isinstance(block, Mapping) and block.get("type") == "tool_result"]
                self._content(turn, _text(raw.get("id")) or uuid.uuid4().hex, content)
        for turn in self.transcript.turns:
            if turn.from_history:
                self.transcript.finish_turn(turn, TurnStatus.DONE)

    def _content(self, turn, message_id, content):
        for index, block in enumerate(content if isinstance(content, list) else []):
            if not isinstance(block, Mapping):
                continue
            kind = block.get("type")
            if kind == "text":
                self.transcript.end_text(turn, message_id, index, _text(block.get("text")))
            elif kind == "tool_use":
                item = self.transcript.ensure_tool(turn, str(block.get("id", "")), _text(block.get("name")))
                self.transcript.set_tool_input(turn, item, _mapping(block.get("input")), final=True)
            elif kind == "tool_result":
                item = self.transcript.ensure_tool(turn, _text(block.get("tool_use_id")), "")
                self.transcript.record_result(turn, item, block.get("content", ""), bool(block.get("is_error")))

    def _notification(self, method, params):
        if self._close_requested or method != "droid.session_notification":
            return
        params = _mapping(params)
        if params.get("sessionId") and params["sessionId"] != self.session_id:
            return
        event = _mapping(params.get("notification"))
        kind = event.get("type")
        if kind == "session_title_updated":
            self.transcript.title = " ".join(_text(event.get("title")).split())[:200]
            self.manager._remember(self)
        elif kind == "settings_updated":
            self._learn(_mapping(event.get("settings")))
        elif kind == "session_token_usage_changed":
            self._usage = Tokens.of(event.get("inclusiveTokenUsage") or event.get("tokenUsage")) or self._usage
            latest = _mapping(event.get("lastCallTokenUsage"))
            if latest:
                used = sum(int(latest.get(k) or 0) for k in ("inputTokens", "cacheReadTokens", "outputTokens"))
                self.context = ContextState(used=used, window=self.context.window, model=self.model)
        turn = self.transcript.active_turn
        if turn is not None:
            message = _text(event.get("messageId")) or "answer"
            index = event.get("blockIndex", 0)
            if kind == "assistant_text_delta":
                self.transcript.append_text(turn, message, index, _text(event.get("textDelta")))
            elif kind == "thinking_text_delta":
                self.transcript.append_thinking(turn, message, index, _text(event.get("textDelta")))
            elif kind == "assistant_text_complete":
                self.transcript.end_text(turn, message, index)
            elif kind == "tool_call":
                self._content(turn, message, [event.get("toolUse")])
            elif kind == "create_message":
                raw = _mapping(event.get("message"))
                message_body = _mapping(raw.get("message")) or raw
                if message_body.get("role") == "assistant":
                    self._content(turn, _text(raw.get("id")) or message, message_body.get("content", []))
            elif kind == "tool_result":
                item = self.transcript.ensure_tool(turn, _text(event.get("toolUseId")), "")
                self.transcript.record_result(turn, item, event.get("content", ""), bool(event.get("isError")))
            elif kind == "droid_working_state_changed":
                self.transcript.set_working_state(event.get("newState"))
            elif kind == "error":
                self.transcript.add_error(_text(event.get("message")) or "Factory 请求失败。")
            elif kind == "agent_turn_completed":
                self._usage = Tokens.of(event.get("cumulativeTokenUsage")) or self._usage
                counted = Tokens.of(event.get("tokenUsage"))
                if counted is not None and self._turn_model:
                    self._usage_models[self._turn_model] = self._usage_models.get(self._turn_model, Tokens()).plus(counted)
                reason = event.get("reason")
                status = TurnStatus.DONE if reason in ("completed", "spec_handoff") else TurnStatus.INTERRUPTED if reason in ("interrupted", "cancelled", "permission_rejected") else TurnStatus.FAILED
                if status is TurnStatus.FAILED:
                    self.transcript.add_error(f"Factory 未能完成本轮（{str(reason)[:100]}）。")
                self.transcript.finish_turn(turn, status, usage=counted)
                if status is TurnStatus.DONE:
                    self.handoff = None
                self.state = SessionState.READY
                self.active_at = time.time()
                self._expire()
                self.manager._remember(self)
        self._changed("state" if kind == "agent_turn_completed" else "transcript")

    def attachment_problem(self, attachments):
        info = self.manager.model_info(self.model)
        if info.get("noImageSupport") and any(a.kind == "image" for a in attachments):
            return "这个 Factory 模型不支持图片。"
        return None

    def _message(self, text, attachments):
        hook = self.manager.hooks.prompt(PromptEvent(self.engine, self.project, text, first=not any(t.status is TurnStatus.DONE and t.user_block for t in self.transcript.turns[:-1])))
        preface = self.manager.memory.brief_for(self.engine, self.project) if self.manager.memory is not None and hook and not self.transcript.turns[:-1] else ""
        if self.handoff is not None:
            attachments = tuple(self.handoff.attachments) + tuple(attachments)
            text = self.handoff.prefix + "\n\n" + text
        params = {"messageId": uuid.uuid4().hex, "text": text + wrap(preface, hook.text)}
        images, files = [], []
        for item in attachments:
            if item.kind == "image":
                images.append({"type": "base64", "mediaType": item.media_type, "data": base64.b64encode(item.data).decode("ascii")})
            elif item.kind == "text":
                params["text"] += f"\n\nFile: {item.name}\n{item.text}"
            elif item.data:
                files.append({"type": "base64", "mediaType": "application/pdf", "data": base64.b64encode(item.data).decode("ascii")})
            elif item.parsed_text:
                params["text"] += f"\n\nFile: {item.name}\n{item.parsed_text}"
        if images:
            params["images"] = images
        if files:
            params["files"] = files
        return params

    def send_prompt(self, text, attachments=()):
        if self.state is not SessionState.READY:
            return False
        self.state = SessionState.RUNNING
        self.used_at = self.active_at = time.time()
        self._turn_model, self._turn_usage = self.model, self._usage
        handoff = (self.handoff.carried, self.handoff.truncated) if self.handoff is not None else None
        self.transcript.start_turn(text, attachments, handoff=handoff)
        self._changed("state")
        self._spawn(self._send(text, attachments))
        return True

    async def _send(self, text, attachments):
        try:
            await self._client.request("droid.add_user_message", self._message(text, attachments), timeout=30)
        except asyncio.CancelledError:
            raise
        except Exception as error:
            if not self._close_requested and self.state is SessionState.RUNNING:
                self.state = SessionState.DEAD
                self.transcript.fail_turn(f"Factory 发送失败，结果未确认，请重新连接：{error}", recoverable=True)
                self._expire()
                await self._client.close()
                self._changed("state")

    async def steer(self, text, attachments=()):
        # The application's queue remains available. Do not claim steering was adopted without a correlated CLI echo.
        return SteerOutcome(False, over=not self.busy, message="Factory CLI 的当前适配暂不确认引导回显，请使用排队。")

    async def update_settings(self, **changes):
        params = {}
        for name, wire in (("model", "modelId"), ("reasoning", "reasoningEffort"), ("autonomy", "autonomyLevel"), ("mode", "interactionMode")):
            if name in changes:
                params[wire] = changes[name]
        if self.busy and "modelId" in params:
            self.transcript.add_notice("Factory 运行中暂不切换模型；请等本轮结束。")
            self._changed()
            return False
        try:
            await self._client.request("droid.update_session_settings", params, timeout=30)
            self._learn(params)
            self._changed()
            return True
        except Exception as error:
            self.transcript.add_notice(f"Factory 设置未生效：{error}")
            self._changed()
            return False

    async def _request(self, method, params, request_id):
        params = _mapping(params)
        if self._close_requested or not self.busy:
            return {"selectedOption": "cancel"} if method == "droid.request_permission" else {"cancelled": True, "answers": []}
        if method == "droid.request_permission":
            ids, actions = [], []
            for raw in params.get("toolUses", []):
                use = _mapping(_mapping(raw).get("toolUse"))
                native, name = _text(use.get("id")), _text(use.get("name"))
                item = self.transcript.ensure_tool(self.transcript.active_turn, native, name)
                self.transcript.set_tool_input(self.transcript.active_turn, item, _mapping(use.get("input")), final=True)
                ids.append(native)
                data = _mapping(use.get("input"))
                actions.append(ActionView("factory", native, name, f"Factory 请求执行 {name}", path=data.get("file_path"), command=data.get("command")))
            options = []
            for raw in params.get("options", []):
                value = _text(_mapping(raw).get("value")) or _text(raw)
                if value and value not in UNSUPPORTED_OPTIONS:
                    options.append(OptionView(value, OPTION_LABELS.get(value, value), value, "danger" if value == "cancel" else "normal"))
            if not options:
                return {"selectedOption": "cancel"}
            prompt = self.transcript.add_permission_prompt(ids, actions, options)
        elif method == "droid.ask_user":
            questions = [QuestionView(i + 1, _text(q.get("topic")), _text(q.get("question")), tuple(q.get("options", [])), bool(q.get("multiSelect"))) for i, q in enumerate(params.get("questions", [])) if isinstance(q, Mapping)]
            prompt = self.transcript.add_question_prompt(_text(params.get("toolCallId")), questions, who="Factory")
        else:
            raise EngineRpcError(-32601, "Unsupported Factory request")
        if prompt is None:
            return {"selectedOption": "cancel"} if method == "droid.request_permission" else {"cancelled": True, "answers": []}
        future = asyncio.get_running_loop().create_future()
        self._pending[prompt.id] = future
        self._changed()
        try:
            answer = await future
            if method == "droid.request_permission":
                return {"selectedOption": answer or "cancel"}
            return {"cancelled": answer is None, "answers": [] if answer is None else [{"index": i + 1, "question": q.question, "answer": a} for i, (q, a) in enumerate(zip(prompt.questions, answer))]}
        finally:
            self._pending.pop(prompt.id, None)
            self.transcript.expire_prompt(prompt.id)

    def answer_permission(self, prompt_id, value):
        future = self._pending.get(prompt_id)
        if future is None or future.done() or not self.transcript.answer_permission(prompt_id, value):
            return False
        future.set_result(value)
        self._changed()
        return True

    def answer_question(self, prompt_id, answers):
        future = self._pending.get(prompt_id)
        if isinstance(answers, list):
            answers = ["、".join(a) if isinstance(a, list) else a for a in answers]
        if future is None or future.done() or not self.transcript.answer_question(prompt_id, answers):
            return False
        future.set_result(answers)
        self._changed()
        return True

    def _expire(self):
        self.transcript.expire_prompts()
        for future in self._pending.values():
            if not future.done():
                future.set_result(None)

    async def interrupt(self):
        if not self.busy:
            return
        self.transcript.mark_stopping()
        self._changed()
        try:
            await self._client.request("droid.interrupt_session", {}, timeout=10)
            deadline = time.monotonic() + 10
            while self.busy and time.monotonic() < deadline:
                await asyncio.sleep(0.02)
            if self.busy:
                raise TimeoutError("Factory did not confirm the turn ended")
        except Exception:
            self.last_error = "Factory 未能确认停止，连接已关闭，请重新连接后继续。"
            self.state = SessionState.DEAD
            await self._client.close()
            self.transcript.fail_turn(self.last_error, recoverable=True)
        self._expire()
        self._changed("state")

    def _lost(self, error):
        if not self._close_requested:
            self.last_error = "Factory CLI 连接已断开，可以重新连接。"
            self.transcript.fail_turn(self.last_error, recoverable=True)
            self.state = SessionState.DEAD
            self._expire()
            self._changed("state")

    async def reconnect(self):
        if self.state is SessionState.DEAD:
            if self._client is not None:
                await self._client.close()
            # Keep the displayed transcript; loading the native session is not a new message.
            kept = self.transcript
            self.transcript = Transcript()
            await self.open(resume=True)
            if self.state is not SessionState.READY:
                self.transcript = kept

    async def close(self):
        self._close_requested = True
        self._expire()
        for task in list(self._tasks):
            task.cancel()
        if self._tasks:
            await asyncio.gather(*list(self._tasks), return_exceptions=True)
        self.manager._remember(self)
        if self._client is not None:
            await self._client.close()
        self.state = SessionState.CLOSED
        self._changed("state")
