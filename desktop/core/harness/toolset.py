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
from typing import Any

from desktop.core.fusion import tools as fusion_tools
from desktop.core.harness import asking, plan_approval
from desktop.core.harness.loop import ALLOW, ASK, DENY, Call, Outcome, Verdict
from desktop.core.harness.mcp import McpClient, McpTool
from desktop.core.harness.permissions import _rememberable_command, action_view, decide, remember_key
from desktop.core.harness.tools import ToolContext, ToolRegistry, ToolSpec
from desktop.core.prompts import ActionView, QuestionView

MCP_PREFIX = "mcp__"
_UNSAFE = re.compile(r"[^A-Za-z0-9_-]")
NAME_LIMIT = 64
PLAN_REFUSAL = (
    "Plan mode is on: nothing may be changed yet, so this was not done. Only reading and searching run. Finish the plan as "
    "your answer; the user switches to Auto to let you carry it out."
)

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
        self.plan_submitter: Callable[[str], None] | None = None
        self.fusion_runner: Callable[..., Awaitable[Outcome]] | None = None
        self.strict_context = False
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

    def declarations(self) -> list[dict[str, Any]]:
        declared = [{"name": spec.name, "description": spec.description, "parameters": spec.parameters} for spec in self.registry.specs()]
        if self.asker is not None:
            declared.append(dict(asking.DECLARATION))
        if self.plan_submitter is not None:
            declared.append(dict(plan_approval.DECLARATION))
        if self.fusion_runner is not None:
            declared.extend((dict(fusion_tools.DECLARATION), dict(fusion_tools.BATCH_DECLARATION)))
        for name, (_, tool) in self._mcp.items():
            declared.append({"name": name, "description": tool.description, "parameters": tool.input_schema})
        return declared

    def known(self, name: str) -> bool:
        return (self.registry.get(name) is not None or name in self._mcp or (name == asking.NAME and self.asker is not None)
                or (name == plan_approval.NAME and self.plan_submitter is not None)
                or (name in (fusion_tools.NAME, fusion_tools.BATCH_NAME) and self.fusion_runner is not None))

    def check(self, call: Call) -> Verdict:
        parent = self.permission_parent
        plan = self.plan or parent is not None and parent.plan
        level = parent.level if parent is not None else self.level
        remembered = self.remembered | parent.remembered if parent is not None else self.remembered
        if call.name in (fusion_tools.NAME, fusion_tools.BATCH_NAME) and self.fusion_runner is not None:
            return Verdict(DENY, PLAN_REFUSAL) if plan else Verdict(ALLOW)
        if call.name == plan_approval.NAME and self.plan_submitter is not None:
            return Verdict(ALLOW) if plan else Verdict(DENY, "Plans can only be submitted in Plan mode.")
        if call.name == asking.NAME:
            return Verdict(ALLOW)  # (asking is not an action: the question is the user's say)
        if call.name == "read_output":
            return Verdict(ALLOW)  # Validated opaque references only, confined to this conversation.
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
        delegate = call.name == fusion_tools.NAME and self.fusion_runner is not None
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

    def remember(self, call: Call) -> None:
        spec = self.registry.get(call.name)
        if spec is not None and spec.kind == "exec" and not _rememberable_command(str(call.args.get("command") or "")):
            return
        self.remembered.add(remember_key(spec, call.args, self.context) if spec is not None else f"mcp:{call.name}")

    async def run(self, call: Call) -> Outcome:
        if self.connections is not None:
            from desktop.core.connections import ConnectionError
            try:
                await self.connections.hooks("PreToolUse", self.context.project, {"tool_name": call.name, "tool_input": call.args})
            except ConnectionError as error:
                return Outcome(str(error), True, cancelled=True)
        outcome = await self._run(call)
        if self.connections is not None:
            from desktop.core.connections import ConnectionError
            try:
                await self.connections.hooks("PostToolUse", self.context.project, {"tool_name": call.name, "tool_input": call.args, "tool_response": outcome.output})
            except ConnectionError as error:
                outcome.output += "\n" + str(error)
        return outcome

    async def _run(self, call: Call) -> Outcome:
        if call.name == fusion_tools.BATCH_NAME and self.check(call).verdict == ALLOW and self.fusion_runner is not None:
            values, problem = fusion_tools.parse_batch(call.args)
            if problem or values is None:
                return Outcome(problem, True)
            tasks = [asyncio.create_task(self.fusion_runner(*args)) for args in values]
            try:
                results = await asyncio.gather(*tasks)
            except BaseException:
                for task in tasks:
                    task.cancel()
                await asyncio.gather(*tasks, return_exceptions=True)
                raise
            return Outcome("\n\n".join(f"Workstream {args[0]}:\n{result.output}" for args, result in zip(values, results)),
                           any(result.is_error for result in results))
        if call.name == fusion_tools.NAME and not self.plan and self.fusion_runner is not None:
            values, problem = fusion_tools.parse(call.args)
            if problem or values is None:
                return Outcome(problem, True)
            return await self.fusion_runner(*values)
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
        spec = self.registry.get(call.name)
        if spec is not None:
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
