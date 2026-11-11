"""Codex engine: OpenAI's Codex on a ChatGPT subscription, through ``codex app-server`` (JSON-RPC over stdio).

Modules: ``rpc`` (the process and JSON-RPC), ``locate`` (finding the CLI), ``account`` (the page's account JSON),
``models`` (``model/list``), ``policy`` (permission levels and approval buttons), ``items`` (thread items -> transcript),
``history`` (saved threads -> transcript), ``controller`` (one session), ``engine`` (the process, account, sessions).

Protocol facts, and where they come from
========================================

VERIFIED against the real binary: codex-cli 0.165.0 (npm ``@openai/codex``, run from ``sandbox/codex-cli`` with
``CODEX_HOME`` in ``sandbox/``, no account, no API keys in the environment; the probes ``sandbox/codex_probe*.py``, the
generated schema ``sandbox/codex-schema`` / ``sandbox/codex-ts`` and the end-to-end run ``sandbox/codex_engine_check.py`` are
local only: ``sandbox/`` is git-ignored). The desktop app's copy reports ``codex-cli 0.165.0-alpha.1.2``. The engine
itself ran against it: the account, the models, refusing to open while signed out, the thread list, reopening two saved
threads with their history (a failed and an interrupted turn) and closing; ``tests/test_codex_real.py`` repeats the
handshake, ``account/read`` and ``model/list`` whenever that install is present.

- Transport: one JSON object per line on stdin/stdout, no ``"jsonrpc"`` member either way; notifications carry
  ``emittedAtMs`` next to ``params``; closing stdin ends the process with exit code 0 at once.
- Handshake: a request before ``initialize`` -> ``-32600 Not initialized``; ``initialize {clientInfo: {name, title,
  version}, capabilities: null}`` -> ``{userAgent, codexHome, platformFamily, platformOs}``; a second one ->
  ``-32600 Already initialized``; then the ``initialized`` notification. Unknown methods -> ``-32600 Invalid request:
  unknown variant ...``.
- ``account/read {refreshToken: false}`` signed out -> ``{account: null, requiresOpenaiAuth: true, workspaceRouting:
  null}``; ``account/rateLimits/read`` (no params) signed out -> ``-32600 codex account authentication required to
  read rate limits``; ``account/logout`` (no params) -> ``{}``; ``account/login/cancel {loginId}`` with an unknown id ->
  ``{status: "notFound"}``.
- ``model/list {limit, includeHidden}`` works signed out: ``{data: [{id, model, displayName, description, hidden,
  supportedReasoningEfforts: [{reasoningEffort, description}], defaultReasoningEffort, inputModalities, isDefault,
  ...}], nextCursor}``; efforts seen: low, medium, high, xhigh, max, ultra. No context window in it.
- ``thread/start {cwd, approvalPolicy, sandbox, model?, serviceName}`` works signed out -> ``{thread: {id, sessionId,
  preview, name, cwd, model, status, source: "vscode", originator: <clientInfo.name>, historyMode: "paginated", ...},
  model, approvalPolicy, sandbox: {type: ...}, reasoningEffort, ...}`` plus a ``thread/started`` notification. An
  unknown model id is accepted as it is. ``approvalPolicy`` accepts ``untrusted | on-request | never`` (not
  ``unlessTrusted`` / ``onRequest`` as some documentation shows); ``sandbox`` is ``read-only | workspace-write |
  danger-full-access``. On this Windows machine ``workspace-write`` comes back as ``readOnly`` while
  ``windowsSandbox/readiness`` says ``notConfigured``; ``danger-full-access`` is honoured.
- ``turn/start {threadId, input: [{type: "text", text, text_elements: []}], cwd, approvalPolicy, sandboxPolicy, model,
  effort}`` -> ``{turn: {id, status: "inProgress", items: [], ...}}``, answered before ``turn/started``; then
  ``thread/status/changed``, ``item/started`` / ``item/completed`` (``userMessage``), and signed out a series of
  ``error {error: {message, codexErrorInfo, additionalDetails}, willRetry: true, threadId, turnId}`` ("Reconnecting...
  2/5"), a ``warning``, a final ``error`` with ``willRetry: false``, ``thread/status/changed`` (``systemError``) and
  ``turn/completed {threadId, turn: {status: "failed", error, durationMs}}`` after about 40 s.
- ``turn/interrupt {threadId, turnId}`` -> ``{}``, then ``turn/completed`` with ``status: "interrupted"``; an interrupt
  for a turn that has already ended gets no answer at all.
- ``thread/turns/list {threadId, itemsView: "full", sortDirection, limit, cursor}`` -> ``{data: [turn], nextCursor,
  backwardsCursor}`` (also for a thread that is not loaded); a thread without saved turns answers ``-32601
  list_turns is not supported yet`` (so does ``thread/read`` with ``includeTurns``). ``thread/read`` without turns,
  ``thread/items/list``, ``thread/loaded/list``, ``thread/name/set`` (-> ``{}``) work.
- ``thread/resume {threadId, excludeTurns: true, approvalPolicy?, sandbox?, ...}`` from a new process -> the thread and
  its settings (the overrides apply), ``turnsBackwardsCursor``; an unknown id -> ``-32600 no rollout found for thread
  id ...``. ``thread/list {cwd, sourceKinds, sortKey: "updated_at", limit}`` -> ``{data: [thread], nextCursor, ...}``.
- The npm package: ``node_modules/.bin/codex.cmd`` runs ``node .../@openai/codex/bin/codex.js``, which starts
  ``@openai/codex-win32-x64/vendor/x86_64-pc-windows-msvc/bin/codex.exe`` (357 MB) with the same arguments.

From the generated 0.165.0 SCHEMA only (no account, so never seen on the wire): the shapes of ``agentMessage``,
``reasoning``, ``commandExecution``, ``fileChange``, ``mcpToolCall``, ``webSearch`` ... items and their deltas
(``item/agentMessage/delta``, ``item/reasoning/summaryTextDelta``, ``item/commandExecution/outputDelta`` ...),
``turn/plan/updated``, ``thread/tokenUsage/updated``, the approval requests (``item/commandExecution/requestApproval``,
``item/fileChange/requestApproval``) and their ``{decision: accept | acceptForSession | decline | cancel}`` answers,
``serverRequest/resolved``, ``account/login/start`` -> ``{loginId, authUrl}`` and ``account/login/completed``, the
rate-limit windows (``usedPercent``, ``windowDurationMins``, ``resetsAt``), ``thread/name/updated``.

From the DOCUMENTATION only (learn.chatgpt.com/docs/app-server): the order of an approval (``item/started``, then the
request, then ``serverRequest/resolved``, then ``item/completed``); that ``fileChange`` diffs are unified diffs for
updates; that the ChatGPT sign-in's callback is served by the app-server itself. Unverified assumptions: that
``FileUpdateChange.diff`` holds the file content for adds and deletes (both forms are handled), that a data URL works as
an ``image`` input, and that a ``clientInfo.name`` of ``wittycode`` is accepted by the ChatGPT backend.
"""

from desktop.core.codex.account import account_json, limit_windows, resets_in_text
from desktop.core.codex.controller import CodexController, CodexUsage
from desktop.core.codex.engine import CodexEngine, CodexSavedThread, default_client_factory
from desktop.core.codex.errors import (
    CodexError,
    CodexNotFoundError,
    CodexProcessError,
    CodexRpcError,
    CodexSignedOutError,
    CodexTimeoutError,
    friendly_failure,
)
from desktop.core.codex.locate import CodexBinary, locate_codex
from desktop.core.codex.models import CodexModel
from desktop.core.codex.policy import PERMISSIONS, permission
from desktop.core.codex.rpc import JsonRpcClient, launch_command

__all__ = [
    "PERMISSIONS",
    "CodexBinary",
    "CodexController",
    "CodexEngine",
    "CodexError",
    "CodexModel",
    "CodexNotFoundError",
    "CodexProcessError",
    "CodexRpcError",
    "CodexSavedThread",
    "CodexSignedOutError",
    "CodexTimeoutError",
    "CodexUsage",
    "JsonRpcClient",
    "account_json",
    "default_client_factory",
    "friendly_failure",
    "launch_command",
    "limit_windows",
    "locate_codex",
    "permission",
    "resets_in_text",
]
