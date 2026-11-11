"""Devin engine: Cognition's Devin (the CLI / desktop app's agent), through ``devin acp`` (the Agent Client Protocol,
JSON-RPC over stdio, one message per line).

Modules: ``rpc`` (the process and JSON-RPC, on the Codex client), ``locate`` (finding the CLI), ``account`` (``devin auth
status`` -> the page's account JSON), ``models`` (a session's ``configOptions`` -> models), ``policy`` (permission levels =
Devin's modes, and the approval buttons), ``items`` (tool calls, prompts and statistics -> transcript), ``history`` (a
replayed saved session -> transcript), ``controller`` (one session), ``engine`` (the process, account, sessions).

Protocol facts, and where they come from
========================================

VERIFIED against the real program: Devin 3000.11.234 / .238 (the desktop app's own ``devin.exe``, signed in; the probes
``sandbox/devin_*.py`` and their traces are local only: ``sandbox/`` is git-ignored). Only the free SWE-2 models were prompted.

- Transport: one JSON object per line on stdin/stdout, **with** ``"jsonrpc": "2.0"``. ``initialize {protocolVersion: 1,
  clientCapabilities, clientInfo}`` -> ``agentCapabilities`` (``loadSession``, image prompts, ``sessionCapabilities`` list /
  delete / close, ``_meta`` ``cognition.ai/midTurnPrompts``) and ``authMethods: [devin-browser]``. Declaring no ``fs`` /
  ``terminal`` capability means the agent does those itself.
- ``session/new {cwd, mcpServers: []}`` (2–4 s) -> ``{sessionId, modes, configOptions}``. ``configOptions``: ``mode``
  (``accept-edits`` "Code", ``smart``, ``ask``, ``plan``, ``bypass``), ``model`` (a select of ids such as ``swe-2-high``,
  ``claude-opus-5-5-medium``; about 50 plus 70 ``fusion-…`` pairings, each with ``_meta`` ``cognition.ai/supportsImages``),
  ``thought_level`` (only for models that think; the values differ per model), ``speed`` (``standard`` / ``fast``).
  ``session/set_config_option {sessionId, configId, value}`` -> the new ``configOptions`` (about 60 ms; choosing a model
  resets ``thought_level`` to that model's own default and drops it for models without); ``session/set_mode {sessionId,
  modeId}``. A bad value -> ``-32602``.
- ``session/list {cwd}`` -> ``{sessions: [{sessionId, cwd, title, updatedAt (ISO), _meta: {cognition.ai/createdAt,
  hasUserMessages, isLocked, lockHolderPid}}]}``; ``session/delete``, ``session/close``.
- ``session/prompt {sessionId, prompt: [{type: text | image (data, mimeType)}]}`` streams ``session/update`` notifications
  (``agent_message_chunk``, ``agent_thought_chunk``, ``tool_call``, ``tool_call_update``, ``usage_update``,
  ``session_info_update`` (title), ``config_option_update``, ``current_mode_update``, ``available_commands_update``) plus
  ``_cognition.ai/thinking_complete {durationMs}``, ``_cognition.ai/turn_stats`` and ``_cognition.ai/agent_stopped``, and is
  answered ``{stopReason: end_turn | cancelled, usage, _meta: {cognition.ai/userMessageId}}``. The answer's ``usage`` is the
  *last model call's*, not the turn's.
- ``usage_update`` is **one model call** (``used`` = the context size after it, ``size`` = the window, ``_meta``
  ``cognition.ai/inputTokens`` (cached included) / ``outputTokens`` / ``cachedReadTokens``) and arrives twice (the second
  time with ``cognition.ai/subagent_context``). ``turn_stats`` is **one prompt** (``responseDimensions``: ``input_tokens`` =
  uncached input, ``output_tokens``, ``cached_input_tokens``), keyed by ``turnClientMessageId``; a stopped turn's has none.
- Permissions: the agent sends ``session/request_permission {sessionId, toolCall: {toolCallId, _meta: {editableCommand,
  permissionRule}}, options: [{optionId, name, kind}]}`` (``allow_once``, ``allow_session``, ``allow_always``,
  ``switch_bypass``, ``reject_once``); the answer is ``{outcome: {outcome: "selected", optionId}}`` (or ``cancelled``).
  ``bypass`` mode asks for nothing.
- A second ``session/prompt`` while one runs is merged into the running turn; both are answered together; two ``turn_stats``
  follow, with disjoint figures. ``session/cancel`` (a notification) -> the prompt is answered ``stopReason: cancelled``.
- ``session/load {sessionId, cwd, mcpServers: []}`` replays the session as notifications (``user_message_chunk`` with
  ``_meta`` ``cognition.ai/clientMessageId`` / ``timestamp``, then the agent's updates, then ``turn_stats`` per prompt) and
  answers with ``modes`` and ``configOptions``. The replay has no tool output text and no exit codes.
- ``devin auth status`` (≈3 s; ``Logged in (via Devin).`` and a ``User:`` / ``Account:`` block, or ``Not logged in``),
  ``devin auth login`` / ``logout``, ``devin models list --format json`` (families with ``max_context_tokens`` and cost tiers).

NOT verified: ``devin auth login`` (the machine was signed in; running it would have disturbed the user's sign-in), sessions
that other Devin programs hold open (``isLocked``), sub-agents' own updates, and any model that is not free.
"""

from desktop.core.devin.account import DevinStatus, account_json, parse_status
from desktop.core.devin.controller import DevinController
from desktop.core.devin.engine import DevinEngine, DevinSavedSession, default_client_factory
from desktop.core.devin.errors import (
    DevinError,
    DevinNotFoundError,
    DevinProcessError,
    DevinRpcError,
    DevinSignedOutError,
    DevinTimeoutError,
    friendly_failure,
)
from desktop.core.devin.locate import DevinBinary, locate_devin
from desktop.core.devin.models import DevinModel
from desktop.core.devin.policy import MODE_OF_LEVEL, mode_of
from desktop.core.devin.rpc import AcpClient

__all__ = [
    "MODE_OF_LEVEL",
    "AcpClient",
    "DevinBinary",
    "DevinController",
    "DevinEngine",
    "DevinError",
    "DevinModel",
    "DevinNotFoundError",
    "DevinProcessError",
    "DevinRpcError",
    "DevinSavedSession",
    "DevinSignedOutError",
    "DevinStatus",
    "DevinTimeoutError",
    "account_json",
    "default_client_factory",
    "friendly_failure",
    "locate_devin",
    "mode_of",
    "parse_status",
]
