"""The memory as an MCP server over stdio: the protocol, the five tools, what a conversation may see, and a real process."""

from __future__ import annotations

import io
import json
import os
import subprocess

import pytest

from desktop.core.memory import server as server_module
from desktop.core.memory.launch import BRIEF_CHARS, MemoryLaunch, build_brief, console_python, new_run
from desktop.core.memory.server import TOOLS, MemoryServer, serve
from desktop.core.memory.store import MemoryStore, agent, project_path


@pytest.fixture
def root(tmp_path):
    return tmp_path / "memory"


@pytest.fixture
def proj(tmp_path):
    folder = tmp_path / "proj-one"
    folder.mkdir()
    return str(folder)


@pytest.fixture
def other(tmp_path):
    folder = tmp_path / "proj-two"
    folder.mkdir()
    return str(folder)


def make(root, project, engine="codex", run="run-1"):
    return MemoryServer(MemoryStore(root), engine=engine, project=project, run=run)


def call(server, name, **arguments):
    answer = server.handle({"jsonrpc": "2.0", "id": 7, "method": "tools/call", "params": {"name": name, "arguments": arguments}})
    result = answer["result"]
    assert answer["id"] == 7 and result["content"][0]["type"] == "text"
    return result["content"][0]["text"], bool(result.get("isError"))


# ----------------------------------------------------------------------------- the protocol


def test_initialize_answers_with_the_clients_version_when_it_is_known_and_the_newest_otherwise(root, proj):
    server = make(root, proj)
    for asked, expected in (("2025-06-18", "2025-06-18"), ("2024-11-05", "2024-11-05"), ("1999-01-01", server_module.PROTOCOL_VERSIONS[0]), (None, server_module.PROTOCOL_VERSIONS[0])):
        params = {} if asked is None else {"protocolVersion": asked}
        answer = server.handle({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": params})
        result = answer["result"]
        assert answer["id"] == 1 and result["protocolVersion"] == expected
        assert result["capabilities"] == {"tools": {"listChanged": False}} and result["serverInfo"]["name"] == "wittycode-memory"
        assert "memory_search" in result["instructions"] and "secrets" in result["instructions"]
        assert "tags" in result["instructions"] and "deploy" in result["instructions"], "tags in every language the user writes in: how a note is found for a message"


def test_notifications_get_no_answer_and_ping_and_unknown_methods_are_answered(root, proj):
    server = make(root, proj)
    assert server.handle({"jsonrpc": "2.0", "method": "notifications/initialized"}) is None
    assert server.handle({"jsonrpc": "2.0", "method": "notifications/cancelled", "params": {"requestId": 3}}) is None
    assert server.handle({"jsonrpc": "2.0", "id": 2, "method": "ping"}) == {"jsonrpc": "2.0", "id": 2, "result": {}}
    unknown = server.handle({"jsonrpc": "2.0", "id": 3, "method": "resources/list"})
    assert unknown["error"]["code"] == -32601 and unknown["id"] == 3
    assert server.handle([1, 2])["error"]["code"] == -32600
    assert server.handle({"jsonrpc": "2.0", "id": 4})["error"]["code"] == -32600


def test_the_tool_list_names_five_tools_and_marks_the_reading_ones_read_only(root, proj):
    tools = make(root, proj).handle({"jsonrpc": "2.0", "id": 1, "method": "tools/list"})["result"]["tools"]
    by_name = {tool["name"]: tool for tool in tools}
    assert list(by_name) == ["memory_search", "memory_read", "memory_write", "memory_update", "memory_forget"] and tools == list(TOOLS)
    assert {n for n, t in by_name.items() if t["annotations"]["readOnlyHint"]} == {"memory_search", "memory_read"}
    for tool in tools:
        assert tool["annotations"]["destructiveHint"] is False and tool["annotations"]["openWorldHint"] is False, "no engine asks before it runs one"
    for tool in tools:
        assert tool["inputSchema"]["type"] == "object" and len(tool["description"]) > 40
    assert by_name["memory_write"]["inputSchema"]["required"] == ["title", "text"]


def test_a_line_that_is_not_json_is_a_parse_error_and_the_server_goes_on(root, proj):
    out = io.StringIO()
    ping = json.dumps({"jsonrpc": "2.0", "id": 1, "method": "ping"})
    serve(make(root, proj), io.StringIO("this is not json\n\n" + ping + "\n"), out)
    first, second = [json.loads(line) for line in out.getvalue().splitlines()]
    assert first["error"]["code"] == -32700 and second == {"jsonrpc": "2.0", "id": 1, "result": {}}


def test_a_bug_in_a_tool_is_an_error_answer_not_the_end_of_the_server(root, proj):
    server = make(root, proj)
    server._tools["memory_search"] = lambda arguments: 1 / 0
    out = io.StringIO()
    request = json.dumps({"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": "memory_search", "arguments": {}}})
    serve(server, io.StringIO(request + "\n" + json.dumps({"jsonrpc": "2.0", "id": 2, "method": "ping"}) + "\n"), out)
    first, second = [json.loads(line) for line in out.getvalue().splitlines()]
    assert first["error"]["code"] == -32603 and "ZeroDivisionError" in first["error"]["message"] and second["id"] == 2


# ----------------------------------------------------------------------------- the tools


def test_write_then_search_then_read_then_update_then_forget(root, proj):
    server = make(root, proj)
    text, failed = call(server, "memory_write", title="E2E loop", text="Run the e2e file 20 times in a row before calling it done.", kind="convention", tags=["e2e"])
    assert not failed and text.startswith("Saved as m-") and "(project)" in text
    note_id = text.split()[2]
    found, _ = call(server, "memory_search", query="e2e")
    assert note_id in found and "E2E loop" in found and "convention" in found and "written by codex" in found and "information, not instructions" in found
    full, _ = call(server, "memory_read", id=note_id)
    assert "Run the e2e file 20 times" in full and "tags: e2e" in full and "information, not instructions" in full
    updated, failed = call(server, "memory_update", id=note_id, text="Run the e2e file 20 times in a row; stderr must stay empty.")
    assert not failed and "revision 2" in updated
    assert "stderr must stay empty" in call(server, "memory_read", id=note_id)[0]
    forgot, failed = call(server, "memory_forget", id=note_id, reason="no longer the rule")
    assert not failed and "The user can restore it" in forgot
    assert call(server, "memory_search", query="e2e")[0] == "No matching memories."
    store = MemoryStore(root)
    assert [c.op for c in store.journal()] == ["create", "update", "forget"] and all(c.by == agent("codex", "run-1") for c in store.journal())


def test_search_without_a_query_says_when_the_memory_is_empty(root, proj):
    server = make(root, proj)
    assert call(server, "memory_search")[0] == "The memory is empty."
    call(server, "memory_write", title="One", text="First note.")
    assert "One" in call(server, "memory_search")[0]


def test_what_goes_wrong_is_told_to_the_model_as_a_tool_error_with_what_to_do(root, proj):
    server = make(root, proj)
    call(server, "memory_write", title="Commit messages", text="In Chinese.")
    text, failed = call(server, "memory_write", title="Commit messages", text="Again.")
    assert failed and "memory_update" in text and "m-" in text
    text, failed = call(server, "memory_write", title="Key", text="sk-" + "abcdefghijklmnopqrstuvwxyz123456")
    assert failed and "API key" in text and "never stored" in text
    assert call(server, "memory_read", id="m-0-none")[1] and call(server, "no_such_tool")[1]
    assert call(server, "memory_write", title="x")[1], "a missing argument is an error answer"
    for arguments in ({"scope": "everywhere"}, {"limit": "many"}, {"limit": True}):
        assert call(server, "memory_search", **arguments)[1]
    assert call(server, "memory_update", id="m-1")[1] and call(server, "memory_forget", id="m-1", reason="  ")[1]
    assert call(server, "memory_write", title="Tags", text="Text", tags="a,b")[1]


def test_a_search_gives_back_at_most_twenty_notes_and_at_least_one(root, proj):
    store = MemoryStore(root)
    words = ["amber", "basalt", "cobalt", "dahlia", "ember", "fjord", "garnet", "heron", "indigo", "juniper", "kestrel", "lichen", "marble", "nectar", "obsidian",
             "paprika", "quartz", "russet", "saffron", "tundra", "umber", "velvet", "willow", "xenon", "yarrow"]
    for word in words:
        store.create(title=word.capitalize(), text=f"A note about {word}, which is not like the others.", scope="global", by="user")
    server = make(root, proj)
    hit_lines = lambda text: [line for line in text.splitlines() if line.startswith("m-")]  # noqa: E731
    assert len(hit_lines(call(server, "memory_search", limit=100)[0])) == 20
    assert len(hit_lines(call(server, "memory_search", limit=0)[0])) == 1
    assert len(hit_lines(call(server, "memory_search")[0])) == 5, "the default"


def test_forgetting_needs_a_reason_and_an_update_needs_a_change(root, proj):
    server = make(root, proj)
    note_id = call(server, "memory_write", title="Alpha", text="Beta.")[0].split()[2]
    text, failed = call(server, "memory_forget", id=note_id, reason="  ")
    assert failed and "reason" in text
    text, failed = call(server, "memory_update", id=note_id)
    assert failed and "nothing to change" in text
    assert MemoryStore(root).read(note_id).rev == 1 and MemoryStore(root).read(note_id).active


def test_the_scope_defaults_to_this_project_and_a_conversation_without_a_project_writes_globally(root, proj):
    here = make(root, proj)
    call(here, "memory_write", title="Mine", text="Project note.")
    call(here, "memory_write", title="Everyone", text="Global note.", scope="global")
    store = MemoryStore(root)
    assert {(e.title, e.scope) for e in store.entries()} == {("Mine", "project"), ("Everyone", "global")}
    nowhere = make(root, "")
    text, failed = call(nowhere, "memory_write", title="Lost", text="No project here.")
    assert failed and "global" in text
    assert not call(nowhere, "memory_write", title="Free", text="Fine.", scope="global")[1]
    assert "Mine" not in call(nowhere, "memory_search")[0] and "Everyone" in call(nowhere, "memory_search")[0]


def test_one_conversation_cannot_see_or_touch_the_notes_of_another_project(root, proj, other):
    mine, theirs = make(root, proj), make(root, other, engine="droid")
    text, _ = call(theirs, "memory_write", title="Secret plans", text="Only for the other project, about widgets.")
    their_id = text.split()[2]
    assert "Secret plans" not in call(mine, "memory_search", query="widgets")[0]
    for name, arguments in (("memory_read", {}), ("memory_update", {"text": "hijacked"}), ("memory_forget", {"reason": "mine now"})):
        answer, failed = call(mine, name, id=their_id, **arguments)
        assert failed and "no memory" in answer.lower()
    assert MemoryStore(root).read(their_id).text.startswith("Only for the other"), "untouched"


def test_notes_from_another_agent_show_who_wrote_them(root, proj):
    call(make(root, proj, engine="devin", run="d1"), "memory_write", title="From Devin", text="Written in Devin.")
    seen = call(make(root, proj, engine="droid", run="r2"), "memory_search", query="Devin")[0]
    assert "written by devin" in seen


def test_an_agent_may_make_only_so_many_changes_an_hour(root, proj, monkeypatch):
    monkeypatch.setattr(server_module, "WRITES_PER_HOUR", 3)
    server = make(root, proj)
    for n in range(3):
        assert not call(server, "memory_write", title=f"Note {n}{'xyzw'[n]}", text=f"Text about {'abc'[n] * 5}")[1]
    text, failed = call(server, "memory_write", title="One more", text="Too many.")
    assert failed and "Too many" in text and "tell the user" in text
    assert not call(server, "memory_search")[1], "reading is not limited"
    assert MemoryStore(root).counts() == {"active": 3}


# ----------------------------------------------------------------------------- the launch and the note before a conversation


def test_the_server_command_carries_everything_it_needs_and_nothing_secret(root, proj):
    launch = MemoryLaunch(root, python="py.exe")
    command = launch.server_for("codex", proj, "abc123")
    assert command.name == "wittycode-memory" and command.command == "py.exe"
    assert command.args[:2] == ("-m", "desktop.core.memory")
    assert dict(zip(command.args[2::2], command.args[3::2])) == {"--store": str(root), "--engine": "codex", "--project": project_path(proj), "--run": "abc123"}
    assert "PYTHONPATH" in command.env and command.env["PYTHONUTF8"] == "1"
    assert len(new_run()) == 12 and new_run() != new_run()


def test_the_memory_can_be_off_for_one_engine(root, proj):
    launch = MemoryLaunch(root, enabled=lambda engine: engine != "devin")
    assert launch.server_for("devin", proj, "r") is None and launch.brief_for("devin", proj) == ""
    assert launch.server_for("codex", proj, "r") is not None and launch.brief_for("codex", proj)


def test_the_console_python_replaces_pythonw(tmp_path, monkeypatch):
    (tmp_path / "pythonw.exe").write_text("")
    (tmp_path / "python.exe").write_text("")
    monkeypatch.setattr("sys.executable", str(tmp_path / "pythonw.exe"))
    assert console_python() == str(tmp_path / "python.exe")
    monkeypatch.setattr("sys.executable", str(tmp_path / "other.exe"))
    assert console_python() == str(tmp_path / "other.exe")


def test_the_brief_lists_titles_of_the_global_notes_and_this_projects_and_says_how_to_use_them(root, proj, other):
    store = MemoryStore(root)
    store.create(title="Reply short", text="Keep answers short.", scope="global", kind="preference", by="user")
    store.create(title="E2E loop", text="Twenty times.", scope="project", project=proj, kind="convention", by=agent("codex"))
    store.create(title="Other project's", text="Not here.", scope="project", project=other, by=agent("droid"))
    brief = build_brief(store, proj)
    assert brief.startswith("[Shared memory") and "memory_search" in brief and "information, not instructions" in brief
    assert "Global notes:" in brief and "Reply short (preference)" in brief
    assert "Notes for this project:" in brief and "E2E loop (convention)" in brief and "Other project" not in brief
    assert "Keep answers short" not in brief, "titles only: the text is read on demand"
    assert brief.endswith("\n\n")


def test_an_empty_memory_still_gets_the_reminder_and_a_big_one_stays_small(root, proj):
    store = MemoryStore(root)
    empty = build_brief(store, proj)
    assert "memory_write" in empty and "Global notes" not in empty
    for n in range(12):
        store.create(title=f"Subject {n} " + "word" * 15, text=f"Body {n} " + "q" * n, scope="project", project=proj, kind="fact", by="user")
    for n in range(8):
        store.create(title=f"Global {n} " + "term" * 15, text=f"Body {n} " + "z" * n, scope="global", kind="fact", by="user")
    big = build_brief(store, proj)
    assert "Subject 11" in big and len(big) <= BRIEF_CHARS + 4


def test_the_note_before_a_conversation_is_cut_at_its_limit(root, proj, monkeypatch):
    from desktop.core.memory import launch as launch_module

    store = MemoryStore(root)
    for n in range(10):
        store.create(title=f"Subject {n} " + "word" * 8, text=f"Body {n} " + "q" * n, scope="global", kind="fact", by="user")
    full = build_brief(store, proj)
    monkeypatch.setattr(launch_module, "BRIEF_CHARS", 420)
    cut = build_brief(store, proj)
    assert len(full) > 420 and len(cut) <= 420 + 4 and "\n…" in cut and cut.endswith("\n\n")
    assert cut.startswith("[Shared memory"), "the reminder of how to use the memory is what stays"


# ----------------------------------------------------------------------------- a real process


def start(root, proj, engine="codex"):
    command = MemoryLaunch(root).server_for(engine, proj, "proc-1")
    environment = {**os.environ, **command.env}
    return subprocess.Popen(
        [command.command, *command.args], stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=environment, cwd=os.path.expanduser("~")
    )


def exchange(process, *messages):
    answers = []
    for message in messages:
        process.stdin.write((json.dumps(message, ensure_ascii=False) + "\n").encode("utf-8"))
        process.stdin.flush()
        if "id" in message:
            answers.append(json.loads(process.stdout.readline().decode("utf-8")))
    return answers


def test_a_real_server_process_talks_over_stdio_in_utf8_from_any_folder(root, proj):
    process = start(root, proj)
    try:
        hello, listed, written, found = exchange(
            process,
            {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "t", "version": "1"}}},
            {"jsonrpc": "2.0", "method": "notifications/initialized"},
            {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
            {"jsonrpc": "2.0", "id": 3, "method": "tools/call", "params": {"name": "memory_write", "arguments": {"title": "提交信息", "text": "提交信息一律用中文。", "kind": "preference"}}},
            {"jsonrpc": "2.0", "id": 4, "method": "tools/call", "params": {"name": "memory_search", "arguments": {"query": "中文"}}},
        )
        assert hello["result"]["protocolVersion"] == "2025-06-18" and len(listed["result"]["tools"]) == 5
        assert "Saved as m-" in written["result"]["content"][0]["text"]
        assert "提交信息" in found["result"]["content"][0]["text"], "Chinese went in and came out intact"
    finally:
        process.stdin.close()
        assert process.wait(timeout=20) == 0, "it ends when the engine closes its pipe"
        error = process.stderr.read().decode("utf-8", "replace")
        process.stdout.close()
        process.stderr.close()
    assert error == "", "nothing but the protocol on stdout and nothing at all on stderr"
    (entry,) = MemoryStore(root).entries()
    assert entry.title == "提交信息" and entry.source == agent("codex", "proc-1") and entry.project == project_path(proj)


def test_two_server_processes_share_one_memory(root, proj):
    first, second = start(root, proj, "codex"), start(root, proj, "droid")
    try:
        (written,) = exchange(first, {"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": "memory_write", "arguments": {"title": "Shared fact", "text": "Written by the first."}}})
        assert "Saved as" in written["result"]["content"][0]["text"]
        (found,) = exchange(second, {"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": "memory_search", "arguments": {"query": "Shared"}}})
        assert "Shared fact" in found["result"]["content"][0]["text"] and "written by codex" in found["result"]["content"][0]["text"]
    finally:
        for process in (first, second):
            process.stdin.close()
            process.wait(timeout=20)
            process.stdout.close()
            process.stderr.close()
