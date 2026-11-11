"""The shared memory in the application: given to every engine's conversations, the note before a new one, the page's calls."""

from __future__ import annotations

import asyncio

import pytest

from desktop.core import app_core
from desktop.core.memory.launch import MemoryLaunch
from desktop.core.memory.store import MemoryStore, agent
from desktop.core.toolinfo import Category, describe_input, memory_tool, tool_category
from tests import chatgpt_support, codex_support, devin_support
from tests.app_support import make_core, ready_core
from tests.fakes import until
from tests.support import complete, delta, failure, saved_session, success


def run(coro):
    return asyncio.run(coro)


def launch(tmp_path, **more):
    return MemoryLaunch(tmp_path / "memory", python="py.exe", **more)


def args_of(command_args):
    return dict(zip(command_args[2::2], command_args[3::2]))


# ----------------------------------------------------------------------------- Droid


def test_a_new_droid_session_is_given_the_memory_server_and_the_note_after_its_first_message(tmp_path):
    async def scenario():
        memory = launch(tmp_path)
        MemoryStore(memory.root).create(title="Reply short", text="Keep answers short.", scope="global", kind="preference", by="user")
        core, factory, events, _, _ = await ready_core(tmp_path, memory=memory)
        cid = (await core.call("session.new", {"projectId": str((tmp_path / "proj").resolve())}))["id"]
        controller = core._by_cid[cid].controller
        (call,) = [info for name, info in factory.calls if name == "create"]
        (server,) = call["config"].mcp_servers
        assert server.name == "wittycode-memory" and server.command == "py.exe"
        assert args_of(server.args) == {"--store": str(memory.root), "--engine": "droid", "--project": str((tmp_path / "proj").resolve()), "--run": controller.run}
        assert controller.preface.startswith("[Shared memory") and "Reply short (preference)" in controller.preface

        await core.call("session.send", {"id": cid, "text": "first question"})
        session = factory.sessions[-1]
        await until(lambda: session.active is not None)
        prompt = session.streams[-1].prompt
        assert prompt.startswith("first question\n\n<wittycode-context>\n[Shared memory") and prompt.endswith("</wittycode-context>"), "after the message: the engine names the conversation after what comes first"
        assert controller.transcript.turns[-1].user_block.text == "first question", "the page shows only what was typed"
        session.active.push(delta("ok", "m1"), complete("m1"), success("ok"))
        session.active.end()
        await until(lambda: controller.state.value == "ready")
        assert controller.preface == ""
        await core.call("session.send", {"id": cid, "text": "second question"})
        await until(lambda: len(session.streams) == 2)
        assert session.streams[-1].prompt == "second question", "the note goes with the first message only"
        await core.shutdown()

    run(scenario())


def test_a_first_message_that_failed_keeps_the_note_for_the_retry(tmp_path):
    async def scenario():
        core, factory, events, _, _ = await ready_core(tmp_path, memory=launch(tmp_path))
        cid = (await core.call("session.new", {"projectId": str((tmp_path / "proj").resolve())}))["id"]
        controller = core._by_cid[cid].controller
        await core.call("session.send", {"id": cid, "text": "hello"})
        session = factory.sessions[-1]
        await until(lambda: session.active is not None)
        session.active.push(failure("boom"))
        session.active.end()
        await until(lambda: controller.state.value == "ready")
        assert controller.preface.startswith("[Shared memory"), "the model has not had it yet"
        await core.shutdown()

    run(scenario())


def test_a_resumed_droid_session_is_given_the_server_again_but_not_the_note(tmp_path):
    async def scenario():
        core, factory, events, _, _ = await ready_core(tmp_path, memory=launch(tmp_path))
        factory.saved = [saved_session("s-1", "Old", minutes_ago=5)]
        await core.call("session.openSaved", {"id": "s-1", "projectId": str((tmp_path / "proj").resolve())})
        (servers,) = factory.resume_servers
        assert [s.name for s in servers] == ["wittycode-memory"] and args_of(servers[0].args)["--engine"] == "droid"
        controller = core.order[0].controller
        assert controller.preface == ""
        await core.shutdown()

    run(scenario())


def test_the_memory_can_be_switched_off_for_everything_or_for_one_engine(tmp_path):
    async def scenario():
        settings_holder = {}
        memory = launch(tmp_path, enabled=lambda engine: settings_holder["settings"].memory_enabled and engine in settings_holder["settings"].memory_engines)
        core, factory, events, _, _ = await ready_core(tmp_path, memory=memory)
        settings_holder["settings"] = core.settings
        await core.call("settings.set", {"key": "memoryEngines", "value": ["codex", "devin"]})
        await core.call("session.new", {"projectId": str((tmp_path / "proj").resolve())})
        assert [info["config"].mcp_servers for name, info in factory.calls if name == "create"] == [()], "off for Droid"
        assert core.order[0].controller.preface == ""
        await core.call("settings.set", {"key": "memoryEngines", "value": ["droid"]})
        await core.call("settings.set", {"key": "memoryEnabled", "value": False})
        await core.call("session.new", {"projectId": str((tmp_path / "proj").resolve())})
        assert [len(info["config"].mcp_servers) for name, info in factory.calls if name == "create"] == [0, 0], "off for everyone"
        await core.shutdown()

    run(scenario())


# ----------------------------------------------------------------------------- Codex and Devin (the real engines on the fake programs)


def test_a_codex_thread_is_given_the_memory_server_when_started_and_when_resumed(tmp_path):
    async def scenario():
        memory = launch(tmp_path)
        harness = codex_support.make_engine(tmp_path)
        harness.engine.memory = memory
        project = tmp_path / "proj"
        project.mkdir()
        controller = await harness.engine.open_new(project)
        (start,) = harness.requests("thread/start")
        server = start["config"]["mcp_servers"]["wittycode-memory"]
        assert server["command"] == "py.exe" and args_of(server["args"])["--engine"] == "codex" and args_of(server["args"])["--run"] == controller.run
        assert server["env"]["PYTHONUTF8"] == "1" and controller.preface.startswith("[Shared memory")
        assert controller.send_prompt("hello codex")
        await codex_support.until(lambda: not controller.busy)
        first = harness.requests("turn/start")[-1]["input"][0]["text"]
        assert first.startswith("hello codex\n\n<wittycode-context>\n[Shared memory") and first.endswith("</wittycode-context>")
        assert controller.preface == "" and controller.send_prompt("again")
        await codex_support.until(lambda: not controller.busy)
        assert harness.requests("turn/start")[-1]["input"][0]["text"] == "again"
        thread = controller.session_id
        await harness.engine.close_all()

        harness.engine.memory = memory
        again = await harness.engine.open_saved(project, thread)
        assert "wittycode-memory" in harness.requests("thread/resume")[-1]["config"]["mcp_servers"] and again.preface == ""
        await harness.engine.close_all()

    codex_support.run(scenario())


def test_a_codex_thread_gets_nothing_when_the_memory_is_off(tmp_path):
    async def scenario():
        harness = codex_support.make_engine(tmp_path)
        harness.engine.memory = launch(tmp_path, enabled=lambda engine: False)
        controller = await harness.engine.open_new(tmp_path)
        assert "config" not in harness.requests("thread/start")[0] and controller.preface == ""
        await harness.engine.close_all()

    codex_support.run(scenario())


def test_a_devin_session_is_given_the_memory_server_when_created_and_when_loaded(tmp_path):
    async def scenario():
        memory = launch(tmp_path)
        harness = devin_support.make_engine(tmp_path)
        harness.engine.memory = memory
        project = tmp_path / "proj"
        project.mkdir()
        controller = await harness.engine.open_new(project, model="swe-2-high", reasoning="high", autonomy="code")
        real = harness.requests("session/new")[-1]
        (server,) = real["mcpServers"]
        env = {item["name"]: item["value"] for item in server["env"]}
        assert server["name"] == "wittycode-memory" and server["command"] == "py.exe" and args_of(server["args"])["--engine"] == "devin"
        assert args_of(server["args"])["--run"] == controller.run and env["PYTHONUTF8"] == "1"
        assert controller.preface.startswith("[Shared memory")
        session = controller.session_id
        await controller.close()
        again = await harness.engine.open_saved(project, session)
        (loaded,) = harness.requests("session/load")[-1]["mcpServers"]
        assert loaded["name"] == "wittycode-memory" and again.preface == ""
        await harness.engine.close_all()

    devin_support.run(scenario())


def test_a_devin_session_gets_the_note_with_its_first_message_only(tmp_path):
    async def scenario():
        harness = devin_support.make_engine(tmp_path)
        harness.engine.memory = launch(tmp_path)
        project = tmp_path / "proj"
        project.mkdir()
        controller = await harness.engine.open_new(project, model="swe-2-high", reasoning="high", autonomy="code")
        await devin_support.finish_turn(controller, "hello devin")
        first = harness.requests("session/prompt")[-1]["prompt"][0]["text"]
        assert first.startswith("hello devin\n\n<wittycode-context>\n[Shared memory") and first.endswith("</wittycode-context>")
        assert controller.preface == "", "it is in the session now"
        await devin_support.finish_turn(controller, "once more")
        assert harness.requests("session/prompt")[-1]["prompt"][0]["text"] == "once more"
        await harness.engine.close_all()

    devin_support.run(scenario())


def test_the_application_hands_the_memory_to_all_four_engines(tmp_path):
    memory = launch(tmp_path)
    codex = codex_support.make_engine(tmp_path)
    devin = devin_support.make_engine(tmp_path)
    chatgpt = chatgpt_support.make_engine(tmp_path, None)
    core, *_ = make_core(tmp_path, codex_engine=codex.engine, devin_engine=devin.engine, chatgpt_engine=chatgpt, memory=memory)
    assert core.manager.memory is memory and codex.engine.memory is memory and devin.engine.memory is memory and chatgpt.memory is memory
    hooks = core.manager.hooks
    assert codex.engine.hooks is hooks and devin.engine.hooks is hooks and chatgpt.hooks is hooks, "one set of hooks for the four"
    (tmp_path / "bare").mkdir()
    bare, *_ = make_core(tmp_path / "bare")
    assert bare.memory is None and bare.manager.memory is None


# ----------------------------------------------------------------------------- the notes that bear on a message (the hook)

ASK = "what is the deploy command for this project"
NOTE = "The deploy command is make ship-it-7421."


def deploy_note(memory, project):
    return MemoryStore(memory.root).create(title="Deploy command", text=NOTE, scope="project", project=str(project), kind="convention", by="user")


def notices_of(turn):
    return [block.text for block in turn.blocks if block.kind == "notice"]


def test_a_droid_message_goes_with_the_notes_that_bear_on_it_once_and_the_page_says_so(tmp_path):
    async def scenario():
        memory = launch(tmp_path)
        proj = (tmp_path / "proj").resolve()
        made = deploy_note(memory, proj)
        core, factory, events, _, _ = await ready_core(tmp_path, memory=memory)
        cid = (await core.call("session.new", {"projectId": str(proj)}))["id"]
        controller = core._by_cid[cid].controller
        await core.call("session.send", {"id": cid, "text": ASK})
        session = factory.sessions[-1]
        await until(lambda: session.active is not None)
        prompt = session.streams[-1].prompt
        assert prompt.startswith(ASK + "\n\n<wittycode-context>\n[Shared memory") and "Notes from the user's shared memory" in prompt
        assert f"- {made.id} Deploy command (convention): {NOTE}" in prompt
        turn = controller.transcript.turns[-1]
        assert turn.user_block.text == ASK, "the page shows what was typed"
        assert notices_of(turn) == ["带上了 1 条相关记忆：「Deploy command」"]
        session.active.push(delta("ok", "m1"), complete("m1"), success("ok"))
        session.active.end()
        await until(lambda: controller.state.value == "ready")
        assert controller.recalled == {made.id}

        await core.call("session.send", {"id": cid, "text": "and the deploy command once more, please"})
        await until(lambda: len(session.streams) == 2)
        assert session.streams[-1].prompt == "and the deploy command once more, please", "the same note is not given twice"
        await core.shutdown()

    run(scenario())


def test_a_later_message_brings_no_notes_even_when_another_note_fits_it_better(tmp_path):
    """The start of a conversation gets the notes; after that the model searches the memory itself, on its own call."""

    async def scenario():
        memory = launch(tmp_path)
        proj = (tmp_path / "proj").resolve()
        deploy_note(memory, proj)
        MemoryStore(memory.root).create(title="Lint command", text="The lint command is make lint-strict-9917.", scope="project", project=str(proj), by="user")
        core, factory, events, _, _ = await ready_core(tmp_path, memory=memory)
        cid = (await core.call("session.new", {"projectId": str(proj)}))["id"]
        controller = core._by_cid[cid].controller
        await core.call("session.send", {"id": cid, "text": ASK})
        session = factory.sessions[-1]
        await until(lambda: session.active is not None)
        assert "make ship-it-7421" in session.streams[-1].prompt and "lint-strict-9917" not in session.streams[-1].prompt
        session.active.push(delta("ok", "m1"), complete("m1"), success("ok"))
        session.active.end()
        await until(lambda: controller.state.value == "ready")

        second = "what is the lint command for this project"
        await core.call("session.send", {"id": cid, "text": second})
        await until(lambda: len(session.streams) == 2)
        assert session.streams[-1].prompt == second, "a note that fits is not brought along: the model has memory_search"
        assert notices_of(controller.transcript.turns[-1]) == []
        await core.shutdown()

    run(scenario())


def test_a_message_that_failed_gives_the_notes_again_with_the_retry(tmp_path):
    async def scenario():
        memory = launch(tmp_path)
        proj = (tmp_path / "proj").resolve()
        deploy_note(memory, proj)
        core, factory, events, _, _ = await ready_core(tmp_path, memory=memory)
        cid = (await core.call("session.new", {"projectId": str(proj)}))["id"]
        controller = core._by_cid[cid].controller
        await core.call("session.send", {"id": cid, "text": ASK})
        session = factory.sessions[-1]
        await until(lambda: session.active is not None)
        session.active.push(failure("boom"))
        session.active.end()
        await until(lambda: controller.state.value == "ready")
        assert controller.recalled == set(), "the model has not had them"
        await core.call("session.send", {"id": cid, "text": ASK})
        await until(lambda: len(session.streams) == 2)
        assert "make ship-it-7421" in session.streams[-1].prompt
        await core.shutdown()

    run(scenario())


def test_nothing_is_added_when_nothing_bears_on_the_message_or_the_recall_is_off(tmp_path):
    async def scenario():
        for recall_on, text in ((True, "good morning"), (False, ASK)):
            memory = launch(tmp_path / f"r{recall_on}", recall=lambda on=recall_on: on)
            proj = (tmp_path / "proj").resolve()
            deploy_note(memory, proj)
            core, factory, events, _, _ = await ready_core(tmp_path / f"r{recall_on}", memory=memory)
            cid = (await core.call("session.new", {"projectId": str(proj)}))["id"]
            await core.call("session.send", {"id": cid, "text": text})
            session = factory.sessions[-1]
            await until(lambda: session.active is not None)
            prompt = session.streams[-1].prompt
            assert "Notes from the user's shared memory" not in prompt and "ship-it-7421" not in prompt
            assert notices_of(core._by_cid[cid].controller.transcript.turns[-1]) == []
            await core.shutdown()

    run(scenario())


def test_a_codex_message_goes_with_the_notes_that_bear_on_it(tmp_path):
    async def scenario():
        memory = launch(tmp_path)
        harness = codex_support.make_engine(tmp_path)
        harness.engine.memory = memory
        harness.engine.hooks.on_prompt(memory.prompt_hook)
        project = tmp_path / "proj"
        project.mkdir()
        made = deploy_note(memory, project.resolve())
        MemoryStore(memory.root).create(title="Lint command", text="The lint command is make lint-strict-9917.", scope="project", project=str(project.resolve()), by="user")
        controller = await harness.engine.open_new(project)
        assert controller.send_prompt("[fail] " + ASK)
        await codex_support.until(lambda: not controller.busy)
        assert controller.recalled == set(), "a message that failed has not given them"
        assert controller.send_prompt(ASK)
        await codex_support.until(lambda: not controller.busy)
        text = harness.requests("turn/start")[-1]["input"][0]["text"]
        assert text.startswith(ASK + "\n\n<wittycode-context>\n[Shared memory") and f"- {made.id} Deploy command" in text and NOTE in text
        assert notices_of(controller.transcript.turns[-1]) == ["带上了 1 条相关记忆：「Deploy command」"]
        assert controller.recalled == {made.id}, "what was given is remembered"
        assert controller.send_prompt("the deploy command, once more")
        await codex_support.until(lambda: not controller.busy)
        assert harness.requests("turn/start")[-1]["input"][0]["text"] == "the deploy command, once more"
        assert controller.send_prompt("what is the lint command for this project")
        await codex_support.until(lambda: not controller.busy)
        assert harness.requests("turn/start")[-1]["input"][0]["text"] == "what is the lint command for this project", "only the first message brings notes"
        await harness.engine.close_all()

    codex_support.run(scenario())


def test_a_devin_message_goes_with_the_notes_that_bear_on_it(tmp_path):
    async def scenario():
        memory = launch(tmp_path)
        harness = devin_support.make_engine(tmp_path)
        harness.engine.memory = memory
        harness.engine.hooks.on_prompt(memory.prompt_hook)
        project = tmp_path / "proj"
        project.mkdir()
        made = deploy_note(memory, project.resolve())
        MemoryStore(memory.root).create(title="Lint command", text="The lint command is make lint-strict-9917.", scope="project", project=str(project.resolve()), by="user")
        controller = await harness.engine.open_new(project, model="swe-2-high", reasoning="high", autonomy="code")
        await devin_support.finish_turn(controller, "[fail] " + ASK)
        assert controller.recalled == set(), "a message that failed has not given them"
        turn = await devin_support.finish_turn(controller, ASK)
        text = harness.requests("session/prompt")[-1]["prompt"][0]["text"]
        assert text.startswith(ASK + "\n\n<wittycode-context>\n[Shared memory") and f"- {made.id} Deploy command" in text and NOTE in text
        assert notices_of(turn) == ["带上了 1 条相关记忆：「Deploy command」"]
        assert controller.recalled == {made.id}, "what was given is remembered"
        await devin_support.finish_turn(controller, "the deploy command, once more")
        assert harness.requests("session/prompt")[-1]["prompt"][0]["text"] == "the deploy command, once more"
        await devin_support.finish_turn(controller, "what is the lint command for this project")
        assert harness.requests("session/prompt")[-1]["prompt"][0]["text"] == "what is the lint command for this project", "only the first message brings notes"
        await harness.engine.close_all()

    devin_support.run(scenario())


# ----------------------------------------------------------------------------- what the page asks


def test_marking_as_seen_never_goes_past_the_newest_change_or_below_nothing(tmp_path):
    async def scenario():
        memory = launch(tmp_path)
        store = MemoryStore(memory.root)
        store.create(title="One", text="First.", scope="global", by=agent("codex", "r1"))
        core, *_ = await ready_core(tmp_path, memory=memory)
        assert (await core.call("memory.markSeen", {"seq": -5})) == {"latest": 1, "unseen": 1, "seen": 0, "pending": 0}
        assert (await core.call("memory.markSeen", {"seq": 999})) == {"latest": 1, "unseen": 0, "seen": 1, "pending": 0}
        store.create(title="Two, quite another thing", text="Second.", scope="global", by=agent("devin", "r2"))
        assert (await core.call("memory.list", {}))["unseen"] == 1, "a change after the mark is news again"
        assert (await core.call("memory.markSeen", {})) == {"latest": 2, "unseen": 0, "seen": 2, "pending": 0}
        await core.shutdown()

    run(scenario())


def test_the_page_lists_edits_forgets_restores_and_undoes_notes(tmp_path):
    async def scenario():
        core, factory, events, _, _ = await ready_core(tmp_path, memory=launch(tmp_path))
        proj = str((tmp_path / "proj").resolve())
        made = (await core.call("memory.create", {"title": "Commit messages", "text": "In Chinese.", "scope": "project", "project": proj, "kind": "convention", "tags": ["git"]}))["entry"]
        assert made["source"] == {"kind": "user"} and made["projectName"] == "proj" and made["tags"] == ["git"] and made["rev"] == 1
        listed = await core.call("memory.list", {})
        assert [e["id"] for e in listed["entries"]] == [made["id"]] and listed["counts"] == {"active": 1}
        assert (await core.call("memory.list", {"query": "Chinese"}))["entries"][0]["id"] == made["id"]
        assert (await core.call("memory.list", {"query": "nothing here"}))["entries"] == []
        assert (await core.call("memory.list", {"scope": "global"}))["entries"] == []
        assert (await core.call("memory.list", {"kind": "convention", "project": proj}))["entries"][0]["id"] == made["id"]

        updated = (await core.call("memory.update", {"id": made["id"], "text": "Always in Chinese.", "tags": ["git", "zh"]}))["entry"]
        assert updated["text"] == "Always in Chinese." and updated["rev"] == 2 and updated["editor"] == {"kind": "user"}
        promoted = (await core.call("memory.update", {"id": made["id"], "scope": "global"}))["entry"]
        assert promoted["scope"] == "global" and promoted["projectName"] == ""
        forgotten = (await core.call("memory.forget", {"id": made["id"]}))["entry"]
        assert forgotten["status"] == "forgotten" and (await core.call("memory.list", {}))["entries"] == []
        assert [e["id"] for e in (await core.call("memory.list", {"status": "forgotten"}))["entries"]] == [made["id"]]
        assert (await core.call("memory.restore", {"id": made["id"]}))["entry"]["status"] == "active"
        latest = (await core.call("memory.list", {}))["latest"]
        back = (await core.call("memory.undo", {"seq": latest}))["entry"]  # (undoing the restore: forgotten again)
        assert back["status"] == "forgotten"
        await core.shutdown()

    run(scenario())


def test_what_the_store_refuses_is_said_in_the_users_words(tmp_path):
    async def scenario():
        core, *_ = await ready_core(tmp_path, memory=launch(tmp_path))
        from desktop.core.app_core import AppError

        for params, words in (
            ({"title": "Key", "text": "sk-" + "abcdefghijklmnopqrstuvwxyz123456", "scope": "global"}, "密钥"),
            ({"title": "", "text": "x", "scope": "global"}, "不能为空"),
            ({"title": "x" * 90, "text": "x", "scope": "global"}, "标题太长"),
            ({"title": "x", "text": "y", "scope": "project"}, "项目"),
        ):
            with pytest.raises(AppError) as refused:
                await core.call("memory.create", params)
            assert words in str(refused.value)
        with pytest.raises(AppError) as missing:
            await core.call("memory.forget", {"id": "m-0-none"})
        assert "找不到" in str(missing.value)
        with pytest.raises(AppError):
            await core.call("memory.undo", {"seq": "one"})
        await core.shutdown()

    run(scenario())


def test_an_agents_note_says_which_conversation_wrote_it_and_can_be_followed_to_it(tmp_path):
    async def scenario():
        memory = launch(tmp_path)
        core, factory, events, _, _ = await ready_core(tmp_path, memory=memory)
        proj = str((tmp_path / "proj").resolve())
        cid = (await core.call("session.new", {"projectId": proj}))["id"]
        controller = core._by_cid[cid].controller
        await until(lambda: core.memory.runs.get(controller.run, {}).get("native") == controller.session_id, timeout=3)
        MemoryStore(memory.root).create(title="From a conversation", text="Written by the agent.", scope="project", project=proj, by=agent("droid", controller.run))
        MemoryStore(memory.root).create(title="From a stranger", text="Written by a run nobody knows.", scope="project", project=proj, by=agent("codex", "unknown-run"))
        by_title = {e["title"]: e for e in (await core.call("memory.list", {}))["entries"]}
        source = by_title["From a conversation"]["source"]
        assert source["kind"] == "agent" and source["engine"] == "droid"
        assert source["conversation"]["rowId"] == controller.session_id and source["conversation"]["projectId"] == proj
        assert by_title["From a stranger"]["source"] == {"kind": "agent", "engine": "codex"}, "an unknown run: only the engine is known"
        await core.shutdown()
        assert (memory.root / "runs.json").is_file(), "written for the next start"

        again, *_ = make_core(tmp_path, memory=memory)
        assert again.memory.runs == {} and again.memory.who(agent("droid", controller.run))["conversation"]["rowId"] == controller.session_id

    run(scenario())


def test_the_page_is_told_when_an_agent_writes_and_how_many_notes_it_has_not_seen(tmp_path, monkeypatch):
    monkeypatch.setattr(app_core, "MEMORY_POLL_SECONDS", 0.05)

    async def scenario():
        memory = launch(tmp_path)
        core, factory, events, _, _ = await ready_core(tmp_path, memory=memory)
        init = await core.call("app.init")
        assert init["memory"] == {"latest": 0, "unseen": 0, "seen": 0, "pending": 0} and init["settings"]["memoryEnabled"] is True
        assert init["settings"]["memoryEngines"] == ["droid", "codex", "devin", "chatgpt"]
        store = MemoryStore(memory.root)
        store.create(title="By the user", text="The user wrote this.", scope="global", by="user")
        await until(lambda: events.of("memory"), timeout=3)
        assert events.last("memory")["unseen"] == 0, "the user's own notes are not news"
        events.clear()
        store.create(title="By an agent", text="An agent wrote this.", scope="global", by=agent("codex", "r1"))
        await until(lambda: events.of("memory") and events.last("memory")["unseen"] == 1, timeout=3)
        changes = await core.call("memory.changes", {})
        (change,) = changes["changes"]
        assert change["op"] == "create" and change["title"] == "By an agent" and change["by"]["engine"] == "codex" and change["seq"] == 2
        state = await core.call("memory.markSeen", {})
        assert state["unseen"] == 0 and events.last("memory")["unseen"] == 0
        assert (await core.call("memory.list", {}))["unseen"] == 0
        # the agent's note can be taken back from the list of changes
        await core.call("memory.undo", {"seq": change["seq"]})
        assert (await core.call("memory.list", {}))["entries"][0]["title"] == "By the user"
        await core.shutdown()

    run(scenario())


def test_the_memory_settings_are_checked(tmp_path):
    async def scenario():
        core, *_ = await ready_core(tmp_path, memory=launch(tmp_path))
        from desktop.core.app_core import AppError

        view = await core.call("settings.set", {"key": "memoryEngines", "value": ["devin", "nothing", "droid"]})
        assert view["memoryEngines"] == ["droid", "devin"], "known engines only, in the usual order"
        assert (await core.call("settings.set", {"key": "memoryEnabled", "value": False}))["memoryEnabled"] is False
        assert (await core.call("app.init"))["settings"]["memoryRecall"] is True, "on unless the user turns it off"
        assert (await core.call("settings.set", {"key": "memoryRecall", "value": False}))["memoryRecall"] is False
        assert core.settings.memory_recall is False
        for key, value in (("memoryEnabled", "yes"), ("memoryRecall", 1), ("memoryEngines", "droid"), ("memoryEngines", [1])):
            with pytest.raises(AppError):
                await core.call("settings.set", {"key": key, "value": value})
        await core.shutdown()

    run(scenario())


def test_without_a_memory_the_calls_say_so(tmp_path):
    async def scenario():
        core, *_ = await ready_core(tmp_path)
        from desktop.core.app_core import AppError

        with pytest.raises(AppError):
            await core.call("memory.list", {})
        assert (await core.call("app.init"))["memory"] is None
        await core.shutdown()

    run(scenario())


def test_a_settings_file_without_the_memory_keys_gets_the_defaults_and_a_bad_one_too():
    from desktop.core.settings import settings_from_dict, settings_to_dict

    assert settings_from_dict({}).memory_enabled is True and settings_from_dict({}).memory_engines == ["droid", "codex", "devin", "chatgpt"]
    odd = settings_from_dict({"memory_enabled": "yes", "memory_engines": ["codex", "bogus", "droid", "codex"]})
    assert odd.memory_enabled is True and odd.memory_engines == ["droid", "codex"]
    assert settings_from_dict({"memory_engines": "droid"}).memory_engines == ["droid", "codex", "devin", "chatgpt"]
    assert settings_from_dict(settings_to_dict(odd)).memory_engines == ["droid", "codex"]
    assert settings_from_dict({}).memory_recall is True and settings_from_dict({"memory_recall": "no"}).memory_recall is True
    assert settings_from_dict(settings_to_dict(settings_from_dict({"memory_recall": False}))).memory_recall is False


# ----------------------------------------------------------------------------- how a call to a memory tool reads in the conversation


@pytest.mark.parametrize(
    "name",
    ["wittycode-memory___memory_write", "wittycode-memory/memory_write", "mcp__wittycode-memory__memory_write"],
)
def test_the_memory_tools_are_recognised_whatever_an_engine_calls_them(name):
    assert memory_tool(name) == "memory_write" and tool_category(name) is Category.MEMORY
    assert describe_input(name, {"title": "Commit messages", "text": "x"}) == "记住：Commit messages"
    other = name.replace("memory_write", "memory_search")
    assert describe_input(other, {"query": "提交"}) == "回忆：提交"
    assert describe_input(other, {"limit": 3, "query": ""}) == "回忆：最近的记忆"
    assert describe_input(name.replace("memory_write", "memory_read"), {"id": "m-1-a"}) == "读取记忆 m-1-a"
    assert describe_input(name.replace("memory_write", "memory_update"), {"id": "m-1-a", "text": "x"}) == "修改记忆 m-1-a"
    assert describe_input(name.replace("memory_write", "memory_forget"), {"id": "m-1-a", "reason": "x"}) == "忘记记忆 m-1-a"


def test_other_tools_are_not_taken_for_the_memory():
    for name in ("memory_write", "other-server/memory_write", "wittycode-memory/memory_dance", "Read", "ToolSearch"):
        assert memory_tool(name) is None and tool_category(name) is not Category.MEMORY
