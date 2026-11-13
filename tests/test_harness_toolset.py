"""``HarnessTools``: the built-in tools, the MCP servers' tools and the permission policy as the agent loop sees them."""

from __future__ import annotations

import asyncio
from typing import Any

from desktop.core.harness.loop import ALLOW, ASK, Call
from desktop.core.harness.mcp import McpResult, McpTool
from desktop.core.harness.tools import ToolContext, ToolRegistry
from desktop.core.harness.toolset import NAME_LIMIT, HarnessTools, mcp_name, ui_call

HARMLESS = {"readOnlyHint": False, "destructiveHint": False, "openWorldHint": False}


class StubClient:
    """Just what ``HarnessTools`` reads of an ``McpClient``."""

    def __init__(self, name: str, tools: list[McpTool], *, running: bool = True, instructions: str = "") -> None:
        self.name = name
        self.tools = tools
        self.running = running
        self.instructions = instructions
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.result = McpResult("done", False)

    async def call(self, tool: str, arguments: dict[str, Any]) -> McpResult:
        self.calls.append((tool, dict(arguments)))
        return self.result


def tool(server: str, name: str, **annotations: Any) -> McpTool:
    return McpTool(server, name, f"{name} of {server}", {"type": "object", "properties": {"q": {"type": "string"}}}, dict(annotations))


def make(tmp_path, level: str = "code", clients: list[StubClient] | None = None) -> HarnessTools:
    return HarnessTools(ToolRegistry.default(), ToolContext(project=tmp_path), level, clients or [])


def call(name: str, **args: Any) -> Call:
    return Call("item-1", "call-1", name, "{}", args)


def test_a_server_and_tool_name_become_one_valid_function_name():
    assert mcp_name("wittycode-memory", "memory_write") == "mcp__wittycode-memory__memory_write"
    assert mcp_name("my server", "do.it!") == "mcp__my_server__do_it_"
    long = mcp_name("s" * 40, "t" * 40)
    assert len(long) == NAME_LIMIT and long.startswith("mcp__" + "s" * 40 + "__")


def test_the_declarations_are_the_built_in_tools_and_every_running_servers_tools(tmp_path):
    memory = StubClient("wittycode-memory", [tool("wittycode-memory", "memory_search", **HARMLESS)])
    stopped = StubClient("old", [tool("old", "ghost")], running=False)
    tools = make(tmp_path, clients=[memory, stopped])
    names = [d["name"] for d in tools.declarations()]
    assert names[: len(ToolRegistry.default().specs())] == [spec.name for spec in ToolRegistry.default().specs()]
    assert names[-2:] == ["mcp__wittycode-memory__memory_search", "mcp__old__ghost"], "known schemas remain even while a server is offline"
    declared = tools.declarations()[-2]
    assert declared["description"] == "memory_search of wittycode-memory" and declared["parameters"]["properties"] == {"q": {"type": "string"}}


def test_a_server_that_comes_up_later_keeps_the_same_known_schema(tmp_path):
    late = StubClient("late", [tool("late", "ping")], running=False)
    tools = make(tmp_path, clients=[late])
    before = tools.declarations()
    assert tools.known("mcp__late__ping")
    late.running = True
    tools.refresh()
    assert tools.known("mcp__late__ping") and tools.declarations() == before


def test_known_names_are_the_built_in_and_the_mcp_ones_only(tmp_path):
    tools = make(tmp_path, clients=[StubClient("s", [tool("s", "t")])])
    assert tools.known("read_file") and tools.known("shell") and tools.known("mcp__s__t")
    assert not tools.known("mcp__s__other") and not tools.known("nonsense")


def test_a_tool_declared_harmless_runs_without_a_question_at_every_level(tmp_path):
    for level in ("ask", "code", "full"):
        tools = make(tmp_path, level, [StubClient("s", [tool("s", "quiet", **HARMLESS), tool("s", "look", readOnlyHint=True)])])
        assert tools.check(call("mcp__s__quiet")).verdict == ALLOW, level
        assert tools.check(call("mcp__s__look")).verdict == ALLOW, level


def test_any_other_mcp_tool_asks_except_at_full_access(tmp_path):
    servers = lambda: [StubClient("s", [tool("s", "plain"), tool("s", "risky", destructiveHint=True, openWorldHint=False), tool("s", "web", destructiveHint=False, openWorldHint=True)])]
    for level in ("ask", "code"):
        tools = make(tmp_path, level, servers())
        for name in ("plain", "risky", "web"):
            verdict = tools.check(call(f"mcp__s__{name}"))
            assert verdict.verdict == ASK, (level, name)
            assert "s" in verdict.reason
    full = make(tmp_path, "full", servers())
    assert all(full.check(call(f"mcp__s__{name}")).verdict == ALLOW for name in ("plain", "risky", "web"))


def test_allowing_an_mcp_tool_for_the_conversation_is_remembered_for_that_tool_only(tmp_path):
    tools = make(tmp_path, "code", [StubClient("s", [tool("s", "one"), tool("s", "two")])])
    first = call("mcp__s__one")
    assert tools.check(first).verdict == ASK
    tools.remember(first)
    assert tools.check(first).verdict == ALLOW
    assert tools.check(call("mcp__s__two")).verdict == ASK


def test_the_built_in_tools_follow_the_permission_level(tmp_path):
    target = tmp_path / "a.txt"
    target.write_text("x", encoding="utf-8")
    read = call("read_file", file_path=str(target))
    write = call("create_file", file_path=str(tmp_path / "b.txt"), content="y")
    ask = make(tmp_path, "ask")
    assert ask.check(read).verdict == ALLOW and ask.check(write).verdict == ASK
    code = make(tmp_path, "code")
    assert code.check(write).verdict == ALLOW, "writes inside the project need no question in the code level"
    outside = call("create_file", file_path=str(tmp_path.parent / "elsewhere.txt"), content="y")
    assert code.check(outside).verdict == ASK


def test_allowing_a_built_in_call_for_the_conversation_is_remembered(tmp_path):
    tools = make(tmp_path, "ask")
    write = call("create_file", file_path=str(tmp_path / "b.txt"), content="y")
    assert tools.check(write).verdict == ASK
    tools.remember(write)
    assert tools.check(write).verdict == ALLOW


def test_an_mcp_call_goes_to_its_server_with_the_servers_own_tool_name(tmp_path):
    client = StubClient("s", [tool("s", "do it")])
    tools = make(tmp_path, clients=[client])
    client.result = McpResult("the answer", False)
    outcome = asyncio.run(tools.run(call(mcp_name("s", "do it"), q="hi")))
    assert client.calls == [("do it", {"q": "hi"})]
    assert (outcome.output, outcome.is_error) == ("the answer", False)
    client.result = McpResult("it broke", True)
    assert asyncio.run(tools.run(call(mcp_name("s", "do it")))).is_error


def test_a_built_in_call_runs_the_tool_and_carries_its_result(tmp_path):
    (tmp_path / "a.txt").write_text("hello\n", encoding="utf-8")
    tools = make(tmp_path)
    outcome = asyncio.run(tools.run(call("read_file", file_path=str(tmp_path / "a.txt"))))
    assert not outcome.is_error and "hello" in outcome.output


def test_the_page_sees_the_droid_style_name_for_built_in_tools_and_the_plain_name_for_the_rest(tmp_path):
    tools = make(tmp_path, clients=[StubClient("s", [tool("s", "t")])])
    name, shown = tools.display("read_file", {"file_path": "a.txt"})
    assert name == "Read" and shown["file_path"] == "a.txt"
    assert tools.display("mcp__s__t", {"q": "x"}) == ("mcp__s__t", {"q": "x"})
    assert ui_call(ToolRegistry.default(), "unknown", {"a": 1}) == ("unknown", {"a": 1})


def test_an_mcp_action_is_described_for_the_approval_card(tmp_path):
    tools = make(tmp_path, clients=[StubClient("memory", [tool("memory", "memory_write")])])
    action = tools.action(call("mcp__memory__memory_write"))
    assert action.kind == "mcp" and action.tool_use_id == "call-1" and action.tool_name == "mcp__memory__memory_write"
    assert "memory_write" in action.title and "memory" in action.title


def test_the_running_servers_are_listed_with_their_instructions_for_the_system_prompt(tmp_path):
    tools = make(tmp_path, clients=[StubClient("a", [], instructions="use me"), StubClient("b", [], running=False, instructions="gone")])
    assert tools.servers() == [("a", "use me"), ("b", "gone")], "connection state must not rewrite instruction history"
