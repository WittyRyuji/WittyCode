"""The tools of a conversation as the loop sees them: the built-in ones, the MCP servers' tools, and the permission policy.

``HarnessTools`` joins what ``tools`` / ``permissions`` (the built-ins) and ``mcp`` (connected servers) provide into the
``loop.Tools`` interface, and says how a call looks on the page (``display``, ``action``). An MCP tool is offered to the
model as ``mcp__<server>__<tool>`` (a function name may hold letters, digits, ``_`` and ``-``, 64 at most); that is also the
form the page already recognises for the shared memory's tools (``toolinfo.memory_tool``).

A tool declared harmless by its server (read-only, or neither destructive nor open-world: the memory's) runs without a
question at every level. Any other MCP tool asks, except at the ``full`` level.

In Plan mode (``plan``) the permission level is set aside: reads and read-only commands run, anything that could change
something is refused (not asked: the answer would be no), and so is an MCP tool its server did not declare harmless.

``ask_user`` (``asking``) is the model's way to put a question to the user. It is offered when the conversation can show
the question (``asker``), asks no permission itself, and is shown on the page as the AskUser row the other engines have.
"""

from __future__ import annotations

import re
from collections.abc import Awaitable, Callable, Mapping, Sequence
from typing import Any

from desktop.core.harness import asking
from desktop.core.harness.loop import ALLOW, ASK, DENY, Call, Outcome, Verdict
from desktop.core.harness.mcp import McpClient, McpTool
from desktop.core.harness.permissions import action_view, decide, remember_key
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
    return (MCP_PREFIX + _UNSAFE.sub("_", server) + "__" + _UNSAFE.sub("_", tool))[:NAME_LIMIT]


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
        self.asker = asker
        self.plan = False
        """Plan mode (set by the controller for the turn): only reading, searching and read-only commands run."""
        self.remembered: set[str] = set()
        self._mcp: dict[str, tuple[McpClient, McpTool]] = {}
        self.refresh()

    def refresh(self) -> None:
        """Read the connected servers' tools again (a server may have come up since)."""
        self._mcp = {mcp_name(tool.server, tool.name): (client, tool) for client in self.clients if client.running for tool in client.tools}

    # ------------------------------------------------------------------ loop.Tools

    def declarations(self) -> list[dict[str, Any]]:
        declared = [{"name": spec.name, "description": spec.description, "parameters": spec.parameters} for spec in self.registry.specs()]
        if self.asker is not None:
            declared.append(dict(asking.DECLARATION))
        for name, (_, tool) in self._mcp.items():
            declared.append({"name": name, "description": tool.description, "parameters": tool.input_schema})
        return declared

    def known(self, name: str) -> bool:
        return self.registry.get(name) is not None or name in self._mcp or (name == asking.NAME and self.asker is not None)

    def check(self, call: Call) -> Verdict:
        if call.name == asking.NAME:
            return Verdict(ALLOW)  # (asking is not an action: the question is the user's say)
        spec = self.registry.get(call.name)
        if spec is not None:
            return self._planned(spec, call) if self.plan else self._decided(spec, call)
        _, tool = self._mcp[call.name]
        if self.plan:
            return Verdict(ALLOW) if tool.harmless else Verdict(DENY, PLAN_REFUSAL)
        if self.level == "full" or tool.harmless or f"mcp:{call.name}" in self.remembered:
            return Verdict(ALLOW)
        return Verdict(ASK, f"tool of the server {tool.server}")

    def _decided(self, spec: ToolSpec, call: Call, level: str | None = None, remembered: set[str] | None = None) -> Verdict:
        decision = decide(level or self.level, spec, call.args, self.context, remembered=self.remembered if remembered is None else remembered)
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

    def remember(self, call: Call) -> None:
        spec = self.registry.get(call.name)
        self.remembered.add(remember_key(spec, call.args, self.context) if spec is not None else f"mcp:{call.name}")

    async def run(self, call: Call) -> Outcome:
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
            return Outcome(result.output, result.is_error, tuple(result.diffs))
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
        return [(client.name, client.instructions) for client in self.clients if client.running]
