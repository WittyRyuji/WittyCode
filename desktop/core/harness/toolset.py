"""The tools of a conversation as the loop sees them: the built-in ones, the MCP servers' tools, and the permission policy.

``HarnessTools`` joins what ``tools`` / ``permissions`` (the built-ins) and ``mcp`` (connected servers) provide into the
``loop.Tools`` interface, and says how a call looks on the page (``display``, ``action``). An MCP tool is offered to the
model as ``mcp__<server>__<tool>`` (a function name may hold letters, digits, ``_`` and ``-``, 64 at most); that is also the
form the page already recognises for the shared memory's tools (``toolinfo.memory_tool``).

Only the bundled server's locally trusted annotations may grant automatic permission. Third-party MCP annotations
are untrusted: their tools ask unless the user explicitly approved that tool or selected ``full``.

In Plan mode (``plan``) the permission level is set aside: reads and read-only commands run, anything that could change
something is refused. Only locally trusted, read-only MCP tools run; memory writes do not bypass Plan mode.

``ask_user`` (``asking``) is the model's way to put a question to the user. It is offered when the conversation can show
the question (``asker``), asks no permission itself, and is shown on the page as the AskUser row the other engines have.
"""

from __future__ import annotations

import asyncio
import hashlib
import re
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import replace
from typing import Any

from desktop.core.fusion import debate_bridge
from desktop.core.fusion import tools as fusion_tools
from desktop.core.harness import asking, outputs, plan_approval, recall_tool
from desktop.core.harness.loop import ALLOW, ASK, DENY, Call, Outcome, Verdict
from desktop.core.harness.mcp import McpClient, McpTool
from desktop.core.harness.permissions import _rememberable_command, action_view, decide, remember_key
from desktop.core.harness.shell import RUNNER_SHELL_DESCRIPTION, ShellStatus, ShellWatch
from desktop.core.harness.tools import ToolContext, ToolError, ToolRegistry, ToolSpec
from desktop.core.prompts import ActionView, QuestionView

MCP_PREFIX = "mcp__"
_UNSAFE = re.compile(r"[^A-Za-z0-9_-]")
NAME_LIMIT = 64
PLAN_REFUSAL = (
    "Plan mode is on: nothing may be changed yet, so this was not done. Only reading and searching run. Finish the plan as "
    "your answer; the user switches to Auto to let you carry it out."
)

READ_ONLY_REFUSAL = "This Fusion brief is read-only: no edits or non-read-only commands are allowed."
IDENTITY_REFUSAL = "This tool is not available to the expert's Fusion identity."

Asker = Callable[[Call, tuple[QuestionView, ...]], Awaitable["list[str | list[str]] | None"]]
"""Shows the questions of a call and waits: the answers (one per question), or ``None`` when the user skipped them."""


def mcp_name(server: str, tool: str) -> str:
    raw = MCP_PREFIX + server + "__" + tool
    name = _UNSAFE.sub("_", raw)
    if name != raw or len(name) > NAME_LIMIT or "__" in server or "__" in tool:
        suffix = "__" + hashlib.sha256((server + "\0" + tool).encode("utf-8")).hexdigest()[:12]
        name = name[:NAME_LIMIT - len(suffix)] + suffix
    return name


def ui_call(registry: ToolRegistry, name: str, args: Mapping[str, Any]) -> tuple[str, dict[str, Any]]:
    """How a call of ``name`` with ``args`` is shown: the page's tool name and the input its summary line reads."""
    if name == asking.NAME:
        return "AskUser", {"questionnaire": asking.questionnaire(args)}
    spec = registry.get(name)
    if spec is not None:
        return spec.ui_name, registry.ui_input(name, args)
    return name, dict(args)


class HarnessTools:
    def __init__(self, registry: ToolRegistry, context: ToolContext, level: str, clients: Sequence[McpClient] = (), *, asker: Asker | None = None) -> None:
        self.registry = registry
        self.context = context
        self.level = level
        self.clients = list(clients)
        self.connections = None
        self.asker = asker
        self.tool_started: Callable[[Call, str], None] | None = None
        self.tool_recorded: Callable[[Call, Outcome, str], Awaitable[None]] | None = None
        self.tool_finished: Callable[[Call, Outcome, str], Awaitable[None]] | None = None
        self.plan_submitter: Callable[[str], None] | None = None
        self.fusion_runner: Callable[..., Awaitable[Outcome]] | None = None
        self.unit_runner: Callable[..., Awaitable[Outcome]] | None = None
        self.fusion_brief_runner: Callable[..., Awaitable[Outcome]] | None = None
        self.identity_tools: frozenset[str] | None = None
        self.read_only_brief = False
        self.frozen_paths: tuple[str, ...] | None = None
        self.record_reader: Callable[[str], Awaitable[Outcome]] | None = None
        self.report_submitter: Callable[[Mapping[str, Any]], Outcome] | None = None
        self.route_submitter: Callable[[Mapping[str, Any]], Awaitable[Outcome]] | None = None
        self.map_reader: Callable[[Mapping[str, Any]], Awaitable[Outcome]] | None = None
        self.map_chooser: Callable[[Call], Awaitable[Outcome]] | None = None
        self.map_updater: Callable[[Mapping[str, Any]], Awaitable[Outcome]] | None = None
        self.topic_declarer: Callable[[str, str, str, str], Awaitable[Outcome]] | None = None
        self.declare_state: Callable[[], Mapping[str, Any] | None] | None = None
        """The main session's V3 turn declaration receipt (``intent``, ``failed``), or ``None`` while undeclared."""
        self.work_plan_submitter: Callable[[Mapping[str, Any]], Awaitable[Outcome]] | None = None
        self.route_guard: Callable[[str, tuple[str, ...]], str] | None = None
        self.route_edit: Callable[[], None] | None = None
        self.rating_submitter: Callable[[Mapping[str, Any]], Awaitable[Outcome]] | None = None
        self.strict_context = False
        self.runner_shell = False  # Enabled only by the Fusion delegation that owns the hard deadline.
        self.active_shell: ShellWatch | None = None
        self.shell_progress: Callable[[Call, ShellStatus], None] | None = None
        self.shell_health_check: Callable[[Call, ShellStatus], str | None] | None = None
        self.permission_parent: HarnessTools | None = None
        """Fusion experts use the main session's live level and grants, without sharing their own approvals back."""
        self.plan = False
        """Plan mode (set by the controller for the turn): only reading, searching and read-only commands run."""
        self.remembered: set[str] = set()
        self._mcp: dict[str, tuple[McpClient, McpTool]] = {}
        self.refresh()

    def refresh(self) -> None:
        """Read the connected servers' tools again (a server may have come up since)."""
        # A disconnected server is an unavailable tool, not a different prompt. Keep known schemas and their order.
        found = {}
        for client in self.clients:
            for tool in client.tools:
                name = mcp_name(tool.server, tool.name)
                if name in found:
                    raise ValueError("MCP tool names must be unique; check duplicate server/tool definitions.")
                found[name] = (client, tool)
        self._mcp = dict(sorted(found.items()))
        self._server_notes = sorted((client.name, client.instructions) for client in self.clients)

    # ------------------------------------------------------------------ loop.Tools

    def _frozen_units(self) -> bool:
        state = self.declare_state() if self.declare_state else None
        return bool(self.unit_runner and state and (state.get('workplan') or {}).get('frozen'))

    def declarations(self) -> list[dict[str, Any]]:
        declared = [{"name": spec.name, "description": spec.description, "parameters": spec.parameters} for spec in self.registry.specs()]
        if self.runner_shell:
            for item in declared:
                if item["name"] == "shell":
                    schema = item["parameters"]
                    item["parameters"] = {**schema, "properties": {k: v for k, v in schema["properties"].items() if k != "timeout"}}
                    item["description"] = RUNNER_SHELL_DESCRIPTION
                    break
        if self.asker is not None:
            declared.append(dict(asking.DECLARATION))
        if self.plan_submitter is not None:
            declared.append(dict(plan_approval.DECLARATION))
        if self._frozen_units():
            from desktop.core.fusion.protocol import UNIT_BATCH, UNIT_DELEGATE
            declared.extend((UNIT_DELEGATE, UNIT_BATCH))
        elif self.fusion_brief_runner is not None:
            declared.extend((dict(fusion_tools.DELEGATE_DECLARATION), dict(fusion_tools.BRIEF_BATCH_DECLARATION)))
        elif self.fusion_runner is not None:
            declared.extend((dict(fusion_tools.DECLARATION), dict(fusion_tools.BATCH_DECLARATION)))
        if self.record_reader is not None:
            declared.append(dict(fusion_tools.READ_RECORD_DECLARATION))
        if self.report_submitter is not None:
            declared.append(dict(fusion_tools.REPORT_DECLARATION))
        if self.route_submitter is not None:
            from desktop.core.fusion.protocol import UNIT_ROUTE
            declared.append(dict(UNIT_ROUTE if self._frozen_units() else fusion_tools.ROUTE_DECLARATION))
        if self.rating_submitter is not None:
            declared.append(dict(fusion_tools.RATE_DECLARATION))
        if self.map_reader is not None:
            declared.append(dict(fusion_tools.READ_MAP_DECLARATION))
        if self.map_chooser is not None:
            declared.append(dict(fusion_tools.CHOOSE_MAP_DECLARATION))
        if self.map_updater is not None:
            declared.append(dict(fusion_tools.UPDATE_MAP_DECLARATION))
        if self.topic_declarer is not None:
            declared.append(dict(debate_bridge.DECLARE_TOPIC_DECLARATION))
        if self.work_plan_submitter is not None:
            declared.append(dict(debate_bridge.SUBMIT_WORK_PLAN_DECLARATION))
        if self._history_declared():
            declared.append(dict(recall_tool.DECLARATION))
        for name, (_, tool) in self._mcp.items():
            declared.append({"name": name, "description": tool.description, "parameters": tool.input_schema})
        return [item for item in declared if not self.identity_refusal(item["name"])] if self.identity_tools is not None else declared

    def _history_declared(self) -> bool:
        return bool(getattr(self.context.history_recall, "declared", False))

    def identity_refusal(self, name: str) -> str:
        kinds = self.identity_tools
        if kinds is None:
            return ""
        spec = self.registry.get(name)
        mcp = self._mcp.get(name)
        if mcp is not None:
            _, tool = mcp
            if tool.server == "wittycode-memory":
                if tool.name not in ("memory_search", "memory_read"):
                    return IDENTITY_REFUSAL
                kind = "read"
            else:
                kind = "read" if tool.read_only else "edit"
        elif spec is not None:
            kind = spec.kind
        elif name == fusion_tools.READ_RECORD:
            kind = "read"
        elif name in (asking.NAME, plan_approval.NAME, fusion_tools.NAME, fusion_tools.BATCH_NAME,
                      debate_bridge.DECLARE_TOPIC, debate_bridge.SUBMIT_WORK_PLAN, recall_tool.NAME):
            return IDENTITY_REFUSAL
        else:
            return ""  # Unknown names still go through the usual unknown-tool handling.
        if kind == "edit" and self.read_only_brief:
            return READ_ONLY_REFUSAL
        return "" if kind in kinds else IDENTITY_REFUSAL

    def known(self, name: str) -> bool:
        return not self.identity_refusal(name) and (self.registry.get(name) is not None or name in self._mcp
                or (name == asking.NAME and self.asker is not None)
                or (name == plan_approval.NAME and self.plan_submitter is not None)
                or (name in (fusion_tools.NAME, fusion_tools.BATCH_NAME) and (self.fusion_runner is not None or self.fusion_brief_runner is not None))
                or (name == fusion_tools.READ_RECORD and self.record_reader is not None)
                or (name == fusion_tools.SUBMIT_REPORT and self.report_submitter is not None)
                or (name == fusion_tools.SUBMIT_ROUTE and self.route_submitter is not None)
                or (name == fusion_tools.RATE_RECORDS and self.rating_submitter is not None)
                or (name == fusion_tools.READ_MAP and self.map_reader is not None)
                or (name == fusion_tools.CHOOSE_MAP and self.map_chooser is not None)
                or (name == fusion_tools.UPDATE_MAP and self.map_updater is not None)
                or (name == debate_bridge.DECLARE_TOPIC and self.topic_declarer is not None)
                or (name == debate_bridge.SUBMIT_WORK_PLAN and self.work_plan_submitter is not None)
                or (name == recall_tool.NAME and self._history_declared()))

    def _identity_check(self, call: Call) -> Verdict | None:
        reason = self.identity_refusal(call.name)
        if reason:
            return Verdict(DENY, reason)
        if self.frozen_paths is not None:
            spec = self.registry.get(call.name)
            if spec is not None and spec.kind == 'edit':
                raw = call.args.get('file_path')
                if not isinstance(raw, str):
                    return Verdict(DENY, 'An unscoped write is outside the frozen unit.')
                try:
                    root = self.context.project
                    target = (root / raw).resolve()
                    if not any(target.is_relative_to(root / path) for path in self.frozen_paths):
                        return Verdict(DENY, 'Write outside frozen unit paths.')
                except (OSError, ValueError, RuntimeError):
                    return Verdict(DENY, 'Invalid frozen unit path.')
            mcp = self._mcp.get(call.name)
            if mcp and not (getattr(mcp[0], 'trusted_annotations', False) and mcp[1].read_only):
                return Verdict(DENY, 'This server tool cannot establish the frozen write scope.')
        spec = self.registry.get(call.name) if self.read_only_brief else None
        if spec is not None and spec.kind == "exec":
            verdict = self._planned(spec, call)
            if verdict.verdict != ALLOW:
                return Verdict(DENY, READ_ONLY_REFUSAL if verdict.reason == PLAN_REFUSAL else f"{READ_ONLY_REFUSAL} {verdict.reason}")
            return verdict
        return None

    def _intent_check(self, call: Call) -> str:
        """The V3 intent gate: a turn that may plan, route, delegate or change needs a declared intent — ``execute``
        lets them through once a complex execution validated its work plan, ``plan`` only the work-plan and
        submit_plan calls. Chat and reads never need a declaration; an untrusted server's ``read_only`` flag does
        not exempt a tool, and the map callbacks act like the writes they are."""
        if call.name == debate_bridge.DECLARE_TOPIC:
            return ""
        plan_only = call.name == plan_approval.NAME
        work_plan = call.name == debate_bridge.SUBMIT_WORK_PLAN
        if not (plan_only or work_plan):
            gated = call.name in (fusion_tools.SUBMIT_ROUTE, fusion_tools.NAME, fusion_tools.BATCH_NAME,
                                  fusion_tools.CHOOSE_MAP, fusion_tools.UPDATE_MAP)
            spec = self.registry.get(call.name)
            if not gated and spec is not None:
                gated = spec.kind == "edit" or spec.kind == "exec" and self._planned(spec, call).verdict != ALLOW
            if not gated and spec is None:
                mcp = self._mcp.get(call.name)
                gated = mcp is not None and not (mcp[1].read_only and getattr(mcp[0], "trusted_annotations", False))
            if not gated:
                return ""
        receipt = self.declare_state()
        if isinstance(receipt, Mapping) and receipt.get("failed"):
            return "The turn's declaration failed, so it may not plan, route, delegate or change anything."
        intent = receipt.get("intent") if isinstance(receipt, Mapping) else None
        kind = receipt.get("kind") if isinstance(receipt, Mapping) else None
        if work_plan:
            if intent == "plan" or intent == "execute" and kind == "complex":
                return ""
            return "submit_work_plan follows a plan or complex-execute declaration."
        if intent == "execute":
            if plan_only:
                return ""
            if kind == "complex" and not receipt.get("approved") and not receipt.get("workplan"):
                return "Validate this turn's work plan with submit_work_plan before routing, delegating or changing anything."
            return ""
        if intent == "plan" and plan_only:
            if receipt.get("workplan"):
                return ""
            return "Validate the work plan with submit_work_plan before submitting the plan for approval."
        if intent == "plan":
            return "This turn was declared for planning: submit_work_plan and submit_plan are the only gated calls it allows."
        if intent == "discussion":
            return "This turn was declared a discussion: it may not plan, route, delegate or change anything."
        return "Call declare_topic first: without a declared intent this turn may not plan, route, delegate or change anything."

    def _route_check(self, call: Call) -> str:
        if self.declare_state is not None:
            if refusal := self._intent_check(call):
                return refusal
        guard = self.route_guard
        if guard is None or self.plan or self.permission_parent is not None and self.permission_parent.plan:
            return ""
        if call.name in (fusion_tools.NAME, fusion_tools.BATCH_NAME):
            workstreams = fusion_tools.writing_workstreams(call.name, call.args, structured=self.fusion_brief_runner is not None)
            return guard("delegate", workstreams) if workstreams else ""
        spec = self.registry.get(call.name)
        if spec is not None and spec.kind in ("edit", "exec"):
            refusal = guard(spec.kind, ())
            if refusal and (spec.kind == "edit" or self._planned(spec, call).verdict != ALLOW):
                return refusal
        return ""

    def check(self, call: Call) -> Verdict:
        identity = self._identity_check(call)
        if identity is not None:
            return identity
        if refusal := self._route_check(call):
            return Verdict(DENY, refusal)
        parent = self.permission_parent
        plan = self.plan or parent is not None and parent.plan
        level = parent.level if parent is not None else self.level
        remembered = self.remembered | parent.remembered if parent is not None else self.remembered
        if call.name in (fusion_tools.NAME, fusion_tools.BATCH_NAME) and (self.fusion_runner is not None or self.fusion_brief_runner is not None):
            return Verdict(DENY, PLAN_REFUSAL) if plan else Verdict(ALLOW)
        if call.name == plan_approval.NAME and self.plan_submitter is not None:
            return Verdict(ALLOW) if plan else Verdict(DENY, "Plans can only be submitted in Plan mode.")
        if call.name == asking.NAME:
            return Verdict(ALLOW)  # (asking is not an action: the question is the user's say)
        if call.name == "read_output":
            return Verdict(ALLOW)  # Validated opaque references only, confined to this conversation.
        if call.name == recall_tool.NAME:
            # A read of this session's own archive under the turn ledger; it is never an action on the workspace.
            return (Verdict(ALLOW) if getattr(self.context.history_recall, "enabled", False)
                    else Verdict(DENY, recall_tool.UNAVAILABLE))
        if call.name == fusion_tools.READ_RECORD:
            return Verdict(ALLOW) if self.record_reader is not None else Verdict(DENY, "Fusion Drive is unavailable.")
        if call.name == fusion_tools.READ_MAP:
            return Verdict(ALLOW) if self.map_reader is not None else Verdict(DENY, "No active Fusion project map.")
        if call.name == fusion_tools.SUBMIT_REPORT:
            return Verdict(ALLOW) if self.report_submitter is not None else Verdict(DENY, "No active expert report.")
        if call.name == fusion_tools.RATE_RECORDS:  # (a judgement on retained data: nothing in the workspace changes)
            return Verdict(ALLOW) if self.rating_submitter is not None else Verdict(DENY, "Fusion Drive is unavailable.")
        if call.name in (fusion_tools.CHOOSE_MAP, fusion_tools.UPDATE_MAP):
            if plan:
                return Verdict(DENY, PLAN_REFUSAL)
            enabled = self.map_chooser if call.name == fusion_tools.CHOOSE_MAP else self.map_updater
            return Verdict(ALLOW) if enabled is not None else Verdict(DENY, "No active Fusion project map.")
        if call.name == fusion_tools.SUBMIT_ROUTE:
            if plan:
                return Verdict(DENY, PLAN_REFUSAL)
            return Verdict(ALLOW) if self.route_submitter is not None else Verdict(DENY, "No active Fusion route.")
        if call.name == debate_bridge.DECLARE_TOPIC:  # (read-only: the debate never changes the workspace)
            return Verdict(ALLOW) if self.topic_declarer is not None else Verdict(DENY, "Fusion debate is unavailable.")
        if call.name == debate_bridge.SUBMIT_WORK_PLAN:  # (read-only too: it only records the turn's validated plan)
            return Verdict(ALLOW) if self.work_plan_submitter is not None else Verdict(DENY, "Fusion work plans are unavailable.")
        spec = self.registry.get(call.name)
        if spec is not None:
            if plan:
                return self._planned(spec, call)
            return self._decided(spec, call, level, remembered)
        client, tool = self._mcp[call.name]
        trusted = getattr(client, "trusted_annotations", False)
        if plan:
            return Verdict(ALLOW) if trusted and tool.read_only else Verdict(DENY, PLAN_REFUSAL)
        if level == "full" or trusted and tool.harmless or f"mcp:{call.name}" in remembered:
            return Verdict(ALLOW)
        return Verdict(ASK, f"tool of the server {tool.server}")

    def _decided(self, spec: ToolSpec, call: Call, level: str | None = None, remembered: set[str] | None = None) -> Verdict:
        decision = decide(level or self.level, spec, call.args, self.context, remembered=self.remembered if remembered is None else remembered)
        if self.permission_parent is not None:
            # The expert keeps its own output/read tracking, but neither session's protected paths may be bypassed.
            parent = decide(level or self.level, spec, call.args, self.permission_parent.context,
                            remembered=self.remembered if remembered is None else remembered)
            if parent.verdict == DENY or decision.verdict == ALLOW:
                decision = parent
        return Verdict(decision.verdict, decision.reason)

    def _planned(self, spec: ToolSpec, call: Call) -> Verdict:
        """Plan mode: a read runs as at the ``ask`` level (one outside the project still asks), a command runs only when it is
        a read-only one that the ``code`` level lets through, everything else is refused. What the user allowed for the rest
        of the session (``remembered``) does not count here, and the protected places stay denied whatever the level."""
        if spec.kind == "read":
            return self._decided(spec, call, "ask", set())
        if spec.kind == "exec":
            verdict = self._decided(spec, call, "code", set())
            if verdict.verdict != ASK:
                return verdict
        return Verdict(DENY, PLAN_REFUSAL)

    def parallel_safe(self, call: Call) -> bool:
        """Reads or guarded delegations; do not mix these groups or parallelise arbitrary writes/hooks."""
        delegate = call.name == fusion_tools.NAME and (self.fusion_runner is not None or self.fusion_brief_runner is not None)
        if self.strict_context and not delegate:
            return False
        if self.connections is not None:
            hint = getattr(self.connections, "has_tool_hooks", None)
            if hint is None or hint(self.context.project):
                return False
        return (
            not call.problem and (delegate or call.name in ("read_file", "list_dir", "glob", "grep"))
            and self.known(call.name) and self.check(call).verdict == ALLOW
        )

    def parallel_group(self, call: Call) -> str:
        if not self.parallel_safe(call):
            return ""
        return "delegates" if call.name == fusion_tools.NAME else "reads"

    def finish_call(self, call: Call) -> str:
        """The submission that ends the turn once accepted: it must be the only call of its step."""
        if call.name == fusion_tools.SUBMIT_REPORT and self.report_submitter is not None:
            return "report"
        return ""

    def remember(self, call: Call) -> None:
        spec = self.registry.get(call.name)
        if spec is not None and spec.kind == "exec" and not _rememberable_command(str(call.args.get("command") or "")):
            return
        self.remembered.add(remember_key(spec, call.args, self.context) if spec is not None else f"mcp:{call.name}")

    async def run(self, call: Call) -> Outcome:
        identity = self._identity_check(call)
        if identity is not None and identity.verdict == DENY:
            return Outcome(f"Refused: {identity.reason}", True)
        if refusal := self._route_check(call):
            return Outcome(f"Refused: {refusal}", True)
        spec = self.registry.get(call.name)
        kind = spec.kind if spec is not None else "read"
        if self.tool_started is not None:
            self.tool_started(call, kind)
        if self.connections is not None:
            from desktop.core.connections import ConnectionError
            try:
                await self.connections.hooks("PreToolUse", self.context.project, {"tool_name": call.name, "tool_input": call.args})
            except ConnectionError as error:
                return Outcome(str(error), True, cancelled=True)
        outcome = await self._run(call)
        if self.tool_recorded is not None:
            await self.tool_recorded(call, outcome, kind)
        if self.connections is not None:
            from desktop.core.connections import ConnectionError
            try:
                await self.connections.hooks("PostToolUse", self.context.project, {"tool_name": call.name, "tool_input": call.args, "tool_response": outcome.output})
            except ConnectionError as error:
                outcome.output += "\n" + str(error)
        if self.route_edit is not None and not outcome.is_error:
            edited = self.registry.get(call.name)
            if edited is not None and edited.kind == "edit":
                self.route_edit()  # (the main agent's own edits are counted: more than a couple is a Coder's work)
        if self.tool_finished is not None:
            await self.tool_finished(call, outcome, kind)
        return outcome

    async def _run(self, call: Call) -> Outcome:
        if call.name == debate_bridge.DECLARE_TOPIC:
            verdict = self.check(call)
            if verdict.verdict != ALLOW or self.topic_declarer is None:
                return Outcome(verdict.reason, True)
            values, problem = debate_bridge.parse_topic(call.args)
            if problem or values is None:
                return Outcome(problem, True)
            return await self.topic_declarer(*values)
        if call.name == debate_bridge.SUBMIT_WORK_PLAN:
            verdict = self.check(call)
            if verdict.verdict != ALLOW or self.work_plan_submitter is None:
                return Outcome(verdict.reason, True)
            data = call.args.get("data")
            if not isinstance(data, Mapping):
                return Outcome("Provide the work plan as a `data` JSON object.", True)
            return await self.work_plan_submitter(data)
        if call.name == fusion_tools.READ_MAP:
            if self.map_reader is None:
                return Outcome("No active Fusion project map.", True)
            return await self.map_reader(call.args)
        if call.name == fusion_tools.CHOOSE_MAP:
            verdict = self.check(call)
            if verdict.verdict != ALLOW or self.map_chooser is None:
                return Outcome(verdict.reason, True)
            return await self.map_chooser(call)
        if call.name == fusion_tools.UPDATE_MAP:
            verdict = self.check(call)
            if verdict.verdict != ALLOW or self.map_updater is None:
                return Outcome(verdict.reason, True)
            return await self.map_updater(call.args)
        if call.name == fusion_tools.SUBMIT_ROUTE:
            verdict = self.check(call)
            if verdict.verdict != ALLOW or self.route_submitter is None:
                return Outcome(verdict.reason, True)
            return await self.route_submitter(call.args)
        if call.name == fusion_tools.SUBMIT_REPORT:
            return self.report_submitter(call.args) if self.report_submitter is not None else Outcome("No active expert report.", True)
        if call.name == fusion_tools.RATE_RECORDS:
            if self.rating_submitter is None:
                return Outcome("Fusion Drive is unavailable.", True)
            return await self.rating_submitter(call.args)
        if call.name == fusion_tools.READ_RECORD:
            if self.record_reader is None:
                return Outcome("Fusion Drive is unavailable.", True)
            pointer = call.args.get("pointer")
            if not isinstance(pointer, str):
                return Outcome("Provide a Fusion Drive page pointer.", True)
            return await self.record_reader(pointer)
        runner = self.fusion_brief_runner or self.fusion_runner
        structured = self.fusion_brief_runner is not None
        if self._frozen_units() and call.name in (fusion_tools.NAME, fusion_tools.BATCH_NAME):
            verdict = self.check(call)
            if verdict.verdict != ALLOW:
                return Outcome(verdict.reason, True)
            from desktop.core.fusion.protocol import valid_unit_action
            tasks = call.args.get('tasks') if call.name == fusion_tools.BATCH_NAME else [call.args]
            if (not isinstance(tasks, list) or not 1 <= len(tasks) <= fusion_tools.MAX_PARALLEL
                    or not all(valid_unit_action(t) for t in tasks)):
                return Outcome('Use only a frozen unit id with at most one of resume, budget_decision, or user_question.', True)
            if len({t['unit'] for t in tasks}) != len(tasks):
                return Outcome('A batch cannot act on the same unit twice.', True)
            pending = [asyncio.create_task(self.unit_runner(t['unit'], resume=t.get('resume', False),
                                                           decision=t.get('budget_decision'), tool_call=call,
                                                           **({'user_question': t['user_question']} if 'user_question' in t else {}))) for t in tasks]
            try:
                results = await asyncio.gather(*pending)
            finally:
                for task in pending:
                    if not task.done():
                        task.cancel()
                await asyncio.gather(*pending, return_exceptions=True)
            if len(results) == 1:
                return results[0]
            output = '\n'.join(r.output for r in results)
            reference = ''
            if (store := self.context.outputs) is not None and any(r.reference for r in results):
                # The turn logs one retained reference per tool outcome. Publish the batch's handoffs together.
                def retain_batch():
                    blocks = [r.output + ('\n' + store.read(r.reference) if r.reference else '') for r in results]
                    return store.put('\n\n'.join(blocks))

                try:
                    reference = await asyncio.to_thread(retain_batch)
                except (OSError, ToolError):
                    pass
            return Outcome(output, any(r.is_error for r in results), reference=reference)
        if call.name == fusion_tools.BATCH_NAME and self.check(call).verdict == ALLOW and runner is not None:
            values, problem = fusion_tools.parse_batch(call.args, structured=structured)
            if problem or values is None:
                return Outcome(problem, True)
            tasks = [asyncio.create_task(runner(*args, tool_call=call)) for args in values]
            try:
                results = await asyncio.gather(*tasks)
            except BaseException:
                for task in tasks:
                    task.cancel()
                await asyncio.gather(*tasks, return_exceptions=True)
                raise
            return Outcome("\n\n".join(f"Workstream {args[0].workstream if structured else args[0]}:\n{result.output}"
                                       for args, result in zip(values, results)), any(result.is_error for result in results))
        if call.name == fusion_tools.NAME and not self.plan and runner is not None:
            values, problem = fusion_tools.parse_delegation(call.args, structured=structured)
            if problem or values is None:
                return Outcome(problem, True)
            return await runner(*values, tool_call=call)
        if call.name == plan_approval.NAME and self.plan and self.plan_submitter is not None:
            text, problem = plan_approval.parse(call.args)
            if problem:
                return Outcome(problem, True)
            self.plan_submitter(text)
            return Outcome("The plan has been submitted. Wait for the user's decision; do not start execution.")
        if call.name == asking.NAME and self.asker is not None:
            views, problem = asking.parse(call.args)
            if problem:
                return Outcome(problem, True)
            answers = await self.asker(call, views)
            if answers is None:
                return Outcome(asking.SKIPPED, cancelled=True)
            return Outcome(asking.result(views, answers))
        if call.name == recall_tool.NAME:
            bound = self.context.history_recall
            if not getattr(bound, "enabled", False):
                return Outcome("Refused: " + recall_tool.UNAVAILABLE, True)
            question, reason, problem = recall_tool.parse(call.args)
            if problem:
                return Outcome(problem, True)
            # Disk IO on the session archive; a worker thread keeps the loop free like the registry's sync handlers.
            text = await asyncio.to_thread(bound, question, reason)
            outcome = Outcome(text, text.startswith("[recall] denied"))
            store = getattr(self.context, "outputs", None)
            if store is not None and len(text) > outputs.RETAIN_CHARS:
                # The result would be retained wholesale; attach a real reference to the non-evidence note instead,
                # so the archive text never enters the store and read_output cannot replay it past the ledger.
                try:
                    outcome.reference = await asyncio.to_thread(store.put, outputs.NOT_REPLAYABLE)
                except (OSError, ToolError):
                    pass
            return outcome
        spec = self.registry.get(call.name)
        if spec is not None:
            if call.name == "shell" and self.runner_shell:
                def on_status(status: ShellStatus) -> str | None:
                    if self.shell_progress is not None:
                        self.shell_progress(call, status)
                    return self.shell_health_check(call, status) if self.shell_health_check is not None else None

                watch = self.active_shell = ShellWatch(on_status)
                try:
                    # Ignore a legacy/hallucinated timeout instead of bringing back a second timer.
                    args = {k: v for k, v in call.args.items() if k != "timeout"}
                    result = await self.registry.run(call.name, args, replace(self.context, shell_watch=watch))
                finally:
                    self.active_shell = None
            else:
                result = await self.registry.run(call.name, call.args, self.context)
            return Outcome(result.output, result.is_error, tuple(result.diffs), images=result.images)
        client, tool = self._mcp[call.name]
        result_ = await client.call(tool.name, call.args)
        return Outcome(result_.text, result_.is_error)

    # ------------------------------------------------------------------ the page

    def display(self, call_name: str, args: Mapping[str, Any]) -> tuple[str, dict[str, Any]]:
        return ui_call(self.registry, call_name, args)

    def action(self, call: Call) -> ActionView:
        if call.name == asking.NAME:
            return ActionView(kind="ask_user", tool_use_id=call.call_id, tool_name="AskUser", title="回答问题")
        spec = self.registry.get(call.name)
        if spec is not None:
            return action_view(call.call_id, spec, call.args, self.context)
        _, tool = self._mcp[call.name]
        return ActionView(kind="mcp", tool_use_id=call.call_id, tool_name=call.name, title=f"调用 MCP 工具 {tool.name}（{tool.server}）")

    def servers(self) -> list[tuple[str, str]]:
        """``(name, instructions)`` of the connected servers, for the system prompt."""
        return list(self._server_notes)
