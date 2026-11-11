"""The application core behind the page, driven without a window: fake Droid sessions, a fake platform, an event log."""

from __future__ import annotations

import asyncio
import base64
import time
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
from droid_sdk import DroidProcessError, ToolConfirmationOutcome

from desktop.core.app_core import AppError
from desktop.core.session_manager import SessionLimitError, SessionState
from tests.app_support import make_core, ready_core
from tests.fakes import until
from tests.support import complete, delta, permission_request, saved_session, success, tiny_png


def run(coro):
    return asyncio.run(coro)


def make_core_with(tmp_path, providers):
    return make_core(tmp_path, providers=providers)


def sessions_of(events):
    """What the page knows of each session: the patches merged in the order they were sent."""
    merged: dict[str, dict] = {}
    for payload in events.of("session"):
        merged.setdefault(payload["id"], {}).update(payload["patch"])
    return merged


def last_turn(events):
    return events.of("turns")[-1]["turns"][-1]


async def idle(core, cid):
    """Until the session has finished whatever it was doing (a turn that was just ended included)."""
    await until(lambda: core._by_cid[cid].controller.state is SessionState.READY)


async def new_session(core, events, project):
    result = await core.call("session.new", {"projectId": str(project)})
    cid = result["id"]
    await until(lambda: any(p["id"] == cid and p["patch"].get("mark") == "ready" for p in events.of("session")))
    return cid


def test_init_returns_the_projects_defaults_and_does_not_wait_for_the_slow_parts(tmp_path):
    async def scenario():
        core, factory, events, platform, _ = await ready_core(tmp_path, projects=True)
        init = await core.call("app.init")
        assert init["version"] == 1 and init["tabs"] == []
        (project,) = init["projects"]
        assert project["path"] == str((tmp_path / "proj").resolve()) and project["name"] == "proj"
        assert init["defaults"] == {"model": "claude-haiku-5-5", "reasoning": "low", "autonomy": "off", "mode": "auto"}
        assert init["codex"]["cli"] == "missing" and init["settings"]["maxOpen"] == 4
        models = events.last("models")["models"]
        haiku = next(m for m in models if m["id"] == "claude-haiku-5-5")
        assert haiku["label"] == "Haiku 5.5" and haiku["source"] == "factory" and haiku["group"] == "Anthropic"
        assert haiku["efforts"] == ["low", "high"] and haiku["defaultEffort"] == "low"
        await core.shutdown()

    run(scenario())


def test_a_new_session_appears_as_a_tab_then_becomes_ready(tmp_path):
    async def scenario():
        core, factory, events, _, _ = await ready_core(tmp_path)
        project = (tmp_path / "proj").resolve()
        result = await core.call("session.new", {"projectId": str(project)})
        cid = result["id"]
        tabs = events.of("tabs")
        assert tabs[0]["tabs"] == [cid] and tabs[0]["select"] == cid
        await until(lambda: sessions_of(events).get(cid, {}).get("mark") == "ready")
        patch = sessions_of(events)[cid]
        assert patch["projectId"] == str(project) and patch["engine"] == "droid" and patch["nativeId"] == "new-1"
        assert patch["settings"] == {"model": "claude-haiku-5-5", "reasoning": "low", "autonomy": "off", "mode": "auto"}
        assert patch["workingLabel"] == "就绪" and patch["handoff"] is None
        assert factory.calls[-1][1]["model"] == "claude-haiku-5-5"
        await core.shutdown()

    run(scenario())


def test_a_turn_streams_to_the_page_in_changed_pieces(tmp_path):
    async def scenario():
        core, factory, events, _, _ = await ready_core(tmp_path)
        cid = await new_session(core, events, tmp_path / "proj")
        session = factory.sessions[-1]
        events.clear()
        await core.call("session.send", {"id": cid, "text": "hello"})
        await until(lambda: session.active is not None)
        await until(lambda: any(p["turns"] for p in events.of("turns")))
        first = events.of("turns")[0]
        assert first["all"] is False and first["turns"][0]["status"] == "running"
        assert first["turns"][0]["blocks"][0] == {"kind": "user", "id": first["turns"][0]["blocks"][0]["id"], "text": "hello"}
        assert sessions_of(events)[cid]["mark"] == "running"
        session.active.push(delta("Hi there"), complete(), success("Hi there"))
        session.active.end()
        await until(lambda: sessions_of(events)[cid].get("mark") == "ready")
        last = events.of("turns")[-1]["turns"][-1]
        assert last["status"] == "done" and last["blocks"][-1]["markdown"] == "Hi there"
        count = len(events.of("turns"))
        await asyncio.sleep(0.15)
        assert len(events.of("turns")) == count, "nothing changed, nothing is sent again"
        await core.shutdown()

    run(scenario())


def test_the_pending_prompt_marks_the_session_as_waiting_and_can_be_answered(tmp_path):
    async def scenario():
        core, factory, events, _, _ = await ready_core(tmp_path)
        cid = await new_session(core, events, tmp_path / "proj")
        session = factory.sessions[-1]
        await core.call("session.send", {"id": cid, "text": "go"})
        await until(lambda: session.active is not None)
        asking = asyncio.ensure_future(session.interactions.on_permission(permission_request()))
        await until(lambda: sessions_of(events).get(cid, {}).get("mark") == "attention")
        turn = events.of("turns")[-1]["turns"][-1]
        (prompt,) = turn["prompts"]
        assert prompt["kind"] == "permission" and prompt["status"] == "pending"
        answered = await core.call("session.answerPermission", {"id": cid, "promptId": prompt["id"], "value": "proceed_once"})
        assert answered == {"ok": True}
        response = await asyncio.wait_for(asking, 2)
        assert response.selected_option is ToolConfirmationOutcome.PROCEED_ONCE
        again = await core.call("session.answerPermission", {"id": cid, "promptId": prompt["id"], "value": "proceed_once"})
        assert again == {"ok": False}, "a late click is refused, not an error"
        session.active.push(success("x"))
        session.active.end()
        await core.shutdown()

    run(scenario())


def test_stop_interrupts_and_close_removes_the_tab(tmp_path):
    async def scenario():
        core, factory, events, _, _ = await ready_core(tmp_path)
        cid = await new_session(core, events, tmp_path / "proj")
        session = factory.sessions[-1]
        await core.call("session.send", {"id": cid, "text": "go"})
        await until(lambda: session.active is not None)
        await core.call("session.stop", {"id": cid})
        await until(lambda: session.interrupts == 1)
        await until(lambda: events.of("turns") and last_turn(events)["status"] == "interrupted")
        await until(lambda: sessions_of(events)[cid].get("mark") == "ready")
        await core.call("session.close", {"id": cid})
        assert events.last("tabs")["tabs"] == []
        await until(lambda: session.closed == 1)
        with pytest.raises(AppError):
            await core.call("session.send", {"id": cid, "text": "x"})
        await core.shutdown()

    run(scenario())


def test_the_sidebar_lists_saved_sessions_and_shows_an_open_one_once(tmp_path):
    async def scenario():
        core, factory, events, _, _ = await ready_core(tmp_path, projects=False)
        proj = tmp_path / "proj"
        proj.mkdir()
        factory.saved = [saved_session("s-old", "Older", minutes_ago=120), saved_session("s-new", "Newer", minutes_ago=5)]
        await core.call("project.add", {"path": str(proj)})
        await until(lambda: events.of("sidebar") and len(events.last("sidebar")["sessions"]) == 2)
        side = events.last("sidebar")
        (project,) = side["projects"]
        assert project["sessionIds"] == ["s-new", "s-old"], "newest first"
        assert side["sessions"]["s-new"]["title"] == "Newer" and side["sessions"]["s-new"]["modified"] == "5 分钟前"
        assert side["sessions"]["s-new"]["settings"]["model"] == "" and side["sessions"]["s-new"]["engine"] == "droid"
        opened = await core.call("session.openSaved", {"id": "s-new", "projectId": str(proj), "title": "Newer"})
        cid = opened["id"]
        await until(lambda: sessions_of(events).get(cid, {}).get("mark") == "ready")
        core._emit_sidebar()
        assert events.last("sidebar")["projects"][0]["sessionIds"] == [cid, "s-old"], "the open one replaces its saved row"
        again = await core.call("session.openSaved", {"id": "s-new", "projectId": str(proj)})
        assert again["id"] == cid and len([c for c in factory.calls if c[0] == "resume"]) == 1
        await core.shutdown()

    run(scenario())


async def three_saved(core, factory, events, tmp_path):
    """A project with three saved sessions (180, 60 and 5 minutes old); returns the folder and what the sidebar showed."""
    proj = tmp_path / "proj"
    proj.mkdir()
    factory.saved = [
        saved_session("s-old", "Older", minutes_ago=180),
        saved_session("s-mid", "Middle", minutes_ago=60),
        saved_session("s-new", "Newer", minutes_ago=5),
    ]
    await core.call("project.add", {"path": str(proj)})
    await until(lambda: events.of("sidebar") and len(events.last("sidebar")["sessions"]) == 3)
    return proj, events.last("sidebar")


def listed(events):
    return events.last("sidebar")["projects"][0]["sessionIds"]


def test_a_droid_session_nobody_wrote_to_is_not_listed(tmp_path):
    async def scenario():
        core, factory, events, _, _ = await ready_core(tmp_path, projects=False)
        proj = tmp_path / "proj"
        proj.mkdir()
        factory.saved = [
            saved_session("s-1", "Real", minutes_ago=5),
            saved_session("s-empty", "New Session", minutes_ago=1, messages=0),
            saved_session("s-unknown", "Count not known", minutes_ago=2, messages=None),
        ]
        await core.call("project.add", {"path": str(proj)})
        await until(lambda: events.of("sidebar") and events.last("sidebar")["sessions"])
        assert list(events.last("sidebar")["sessions"]) == ["s-1", "s-unknown"] and listed(events) == ["s-unknown", "s-1"], "only a count of zero hides a session"
        await core.shutdown()

    run(scenario())


def test_a_session_is_archived_listed_apart_and_brought_back(tmp_path):
    async def scenario():
        core, factory, events, _, _ = await ready_core(tmp_path, projects=False)
        proj, side = await three_saved(core, factory, events, tmp_path)
        assert listed(events) == ["s-new", "s-mid", "s-old"] and side["archived"] == []
        before = int(time.time() * 1000)
        assert await core.call("session.archive", {"id": "s-mid"}) == {"ok": True}
        side = events.last("sidebar")
        assert listed(events) == ["s-new", "s-old"] and "s-mid" not in side["sessions"]
        assert [row["id"] for row in side["archived"]] == ["s-mid"] and side["archived"][0]["title"] == "Middle"
        assert core.settings.archived_sessions["s-mid"] >= before, "when it was archived, in ms"
        assert [row["id"] for row in (await core.call("app.init"))["archived"]] == ["s-mid"], "a reloaded page is told too"
        await core.call("session.archive", {"id": "s-old"})
        assert [row["id"] for row in events.last("sidebar")["archived"]] == ["s-old", "s-mid"], "the one archived last first"
        await core._reload_project(str(proj))
        core._emit_sidebar()
        assert listed(events) == ["s-new"], "a fresh listing from the engine does not bring archived ones back"
        assert await core.call("session.unarchive", {"id": "s-mid"}) == {"ok": True}
        assert listed(events) == ["s-new", "s-mid"] and [row["id"] for row in events.last("sidebar")["archived"]] == ["s-old"]
        assert await core.call("session.unarchive", {"id": "s-mid"}) == {"ok": False}, "it is not archived any more"
        await core.shutdown()

    run(scenario())


def test_an_open_conversation_is_closed_when_it_is_archived_and_a_running_one_is_not_archived(tmp_path):
    async def scenario():
        core, factory, events, _, _ = await ready_core(tmp_path, projects=False)
        proj, _ = await three_saved(core, factory, events, tmp_path)
        opened = await core.call("session.openSaved", {"id": "s-new", "projectId": str(proj), "title": "Newer"})
        cid = opened["id"]
        await until(lambda: sessions_of(events).get(cid, {}).get("mark") == "ready")
        await core.call("session.send", {"id": cid, "text": "go"})
        session = factory.sessions[-1]
        await until(lambda: session.active is not None)
        with pytest.raises(AppError, match="还在运行"):
            await core.call("session.archive", {"id": cid})
        assert cid in core._by_cid and "s-new" not in core.settings.archived_sessions
        await core.call("session.stop", {"id": cid})
        await idle(core, cid)
        assert await core.call("session.archive", {"id": cid}) == {"ok": True}
        assert cid not in core._by_cid and events.last("tabs")["tabs"] == []
        assert "s-new" in core.settings.archived_sessions
        await core.shutdown()

    run(scenario())


def test_a_conversation_just_started_can_be_archived_before_the_list_has_heard_of_it(tmp_path):
    async def scenario():
        core, factory, events, _, _ = await ready_core(tmp_path)
        cid = await new_session(core, events, tmp_path / "proj")
        session = factory.sessions[-1]
        await core.call("session.send", {"id": cid, "text": "go"})
        await until(lambda: session.active is not None)
        session.active.push(delta("好", "m1"), complete("m1"), success("好"))
        session.active.end()
        await idle(core, cid)
        native = core._by_cid[cid].controller.session_id
        assert not any(item.id == native for rows in core._saved.values() for item in rows), "the engine's list does not have it yet"
        assert await core.call("session.archive", {"id": cid}) == {"ok": True}
        assert native in core.settings.archived_sessions and cid not in core._by_cid
        await core.shutdown()

    run(scenario())


def test_a_conversation_the_engine_has_not_named_yet_is_not_archived_or_renamed(tmp_path):
    async def scenario():
        core, factory, events, _, _ = await ready_core(tmp_path)
        cid = await new_session(core, events, tmp_path / "proj")
        session = factory.sessions[-1]
        await core.call("session.send", {"id": cid, "text": "go"})
        await until(lambda: session.active is not None)
        session.active.push(delta("好", "m1"), complete("m1"), success("好"))
        session.active.end()
        await idle(core, cid)
        core._by_cid[cid].controller.session_id = None
        for method, params in (("session.archive", {"id": cid}), ("session.rename", {"id": cid, "title": "x"})):
            with pytest.raises(AppError, match="还没有内容"):
                await core.call(method, params)
        assert core.settings.archived_sessions == {} and core.settings.session_titles == {} and cid in core._by_cid
        await core.shutdown()

    run(scenario())


def test_a_conversation_with_nothing_in_it_is_closed_not_archived_or_renamed(tmp_path):
    async def scenario():
        core, factory, events, _, _ = await ready_core(tmp_path)
        cid = await new_session(core, events, tmp_path / "proj")
        with pytest.raises(AppError, match="还没有内容"):
            await core.call("session.archive", {"id": cid})
        with pytest.raises(AppError, match="还没有内容"):
            await core.call("session.rename", {"id": cid, "title": "x"})
        assert core.settings.archived_sessions == {} and core.settings.session_titles == {}
        await core.shutdown()

    run(scenario())


def test_archiving_and_renaming_refuse_what_they_do_not_know(tmp_path):
    async def scenario():
        core, factory, events, _, _ = await ready_core(tmp_path, projects=False)
        await three_saved(core, factory, events, tmp_path)
        for method, params in (
            ("session.archive", {"id": "nope"}), ("session.archive", {"id": ""}), ("session.archive", {"id": 5}), ("session.archive", {}),
            ("session.archive", {"id": ["s-new"]}), ("session.rename", {"id": {"a": 1}, "title": "x"}),
            ("session.unarchive", {"id": "nope"}), ("session.rename", {"id": "nope", "title": "x"}), ("session.rename", {"id": "s-new", "title": 5}),
            ("session.rename", {"id": "s-new"}),
        ):
            with pytest.raises(AppError):
                await core.call(method, params)
        assert core.settings.archived_sessions == {} and core.settings.session_titles == {}
        await core.shutdown()

    run(scenario())


def test_a_session_is_renamed_here_without_touching_the_engines_title(tmp_path):
    async def scenario():
        core, factory, events, _, _ = await ready_core(tmp_path, projects=False)
        proj, _ = await three_saved(core, factory, events, tmp_path)
        assert await core.call("session.rename", {"id": "s-new", "title": "  新的\n  名字  "}) == {"ok": True, "title": "新的 名字"}
        assert events.last("sidebar")["sessions"]["s-new"]["title"] == "新的 名字" and core.settings.session_titles == {"s-new": "新的 名字"}
        assert events.last("sidebar")["sessions"]["s-mid"]["title"] == "Middle", "only that one"
        opened = await core.call("session.openSaved", {"id": "s-new", "projectId": str(proj), "title": "Newer"})
        cid = opened["id"]
        await until(lambda: sessions_of(events).get(cid, {}).get("mark") == "ready")
        assert sessions_of(events)[cid]["title"] == "新的 名字", "the open conversation carries the name too"
        long = await core.call("session.rename", {"id": cid, "title": "长" * 500})
        assert len(long["title"]) == 120
        await until(lambda: len(sessions_of(events)[cid]["title"]) == 120)
        assert await core.call("session.rename", {"id": "s-new", "title": "   "}) == {"ok": True, "title": ""}
        assert core.settings.session_titles == {} and events.last("sidebar")["sessions"]["s-new"]["title"] == "Newer"
        await core.shutdown()

    run(scenario())


def test_the_project_of_the_conversation_started_last_is_remembered(tmp_path):
    async def scenario():
        core, factory, events, _, _ = await ready_core(tmp_path)
        assert core.settings.last_project == "" and (await core.call("app.init"))["settings"]["lastProject"] == ""
        await new_session(core, events, tmp_path / "proj")
        assert core.settings.last_project == str((tmp_path / "proj").resolve())
        assert (await core.call("app.init"))["settings"]["lastProject"] == core.settings.last_project
        await core.shutdown()

    run(scenario())


def test_opening_a_saved_session_keeps_its_place_and_its_time_in_the_sidebar(tmp_path):
    async def scenario():
        core, factory, events, _, _ = await ready_core(tmp_path, projects=False)
        proj, before = await three_saved(core, factory, events, tmp_path)
        assert listed(events) == ["s-new", "s-mid", "s-old"]
        old = before["sessions"]["s-old"]
        cid = (await core.call("session.openSaved", {"id": "s-old", "projectId": str(proj), "title": "Older"}))["id"]
        await until(lambda: sessions_of(events).get(cid, {}).get("mark") == "ready")
        core._emit_sidebar()
        assert listed(events) == ["s-new", "s-mid", cid], "clicking an old session does not float it to the top"
        meta = sessions_of(events)[cid]
        assert meta["modifiedAt"] == old["modifiedAt"] and meta["modified"] == old["modified"] != "刚刚"

        # a second one opened later, and the first looked at again: nothing moves
        mid = (await core.call("session.openSaved", {"id": "s-mid", "projectId": str(proj)}))["id"]
        await until(lambda: sessions_of(events).get(mid, {}).get("mark") == "ready")
        await core.call("session.openSaved", {"id": "s-old", "projectId": str(proj)})
        core._emit_sidebar()
        assert listed(events) == ["s-new", mid, cid]
        await core.shutdown()

    run(scenario())


def test_a_session_whose_row_is_not_in_the_list_still_opens_and_counts_from_now(tmp_path):
    async def scenario():
        core, factory, events, _, _ = await ready_core(tmp_path, projects=False)
        proj, _ = await three_saved(core, factory, events, tmp_path)
        core._saved.clear()  # nothing known of it: what the page shows always comes from this list, so only a row that vanished gets here
        cid = (await core.call("session.openSaved", {"id": "s-old", "projectId": str(proj)}))["id"]
        await until(lambda: sessions_of(events).get(cid, {}).get("mark") == "ready")
        assert core._by_cid[cid].controller.active_at > time.time() - 60
        await core.shutdown()

    run(scenario())


def test_a_message_moves_a_session_to_the_top_and_so_does_the_end_of_its_answer(tmp_path):
    async def scenario():
        core, factory, events, _, _ = await ready_core(tmp_path, projects=False)
        proj, before = await three_saved(core, factory, events, tmp_path)
        cid = (await core.call("session.openSaved", {"id": "s-old", "projectId": str(proj)}))["id"]
        await until(lambda: sessions_of(events).get(cid, {}).get("mark") == "ready")
        session = factory.sessions[-1]
        old_ms = before["sessions"]["s-old"]["modifiedAt"]
        await core.call("session.send", {"id": cid, "text": "hello"})
        await until(lambda: session.active is not None)
        core._emit_sidebar()
        assert listed(events) == [cid, "s-new", "s-mid"], "the one just talked to is the newest"
        await until(lambda: sessions_of(events)[cid]["modifiedAt"] > old_ms)

        controller = core._by_cid[cid].controller
        controller.active_at = 1.0
        session.active.push(delta("Hi", "m1"), complete("m1"), success("Hi"))
        session.active.end()
        await idle(core, cid)
        assert controller.active_at > 1.0
        await until(lambda: sessions_of(events)[cid]["modifiedAt"] > old_ms)
        core._emit_sidebar()
        assert listed(events)[0] == cid
        await core.shutdown()

    run(scenario())


def test_a_closed_session_keeps_the_time_of_its_last_message_when_the_engine_lists_an_older_one(tmp_path):
    async def scenario():
        core, factory, events, _, _ = await ready_core(tmp_path, projects=False)
        proj, before = await three_saved(core, factory, events, tmp_path)
        old_ms = before["sessions"]["s-old"]["modifiedAt"]
        cid = (await core.call("session.openSaved", {"id": "s-old", "projectId": str(proj)}))["id"]
        await until(lambda: sessions_of(events).get(cid, {}).get("mark") == "ready")
        session = factory.sessions[-1]
        await core.call("session.send", {"id": cid, "text": "hello"})
        await until(lambda: session.active is not None)
        session.active.push(delta("Hi", "m1"), complete("m1"), success("Hi"))
        session.active.end()
        await idle(core, cid)
        # the fake engine still lists the old time (Codex does that after a turn: it stamps the thread with the turn's start)
        await core.call("session.close", {"id": cid})
        await until(lambda: events.last("sidebar")["sessions"].get("s-old", {}).get("modifiedAt", 0) > old_ms)
        assert listed(events) == ["s-old", "s-new", "s-mid"], "closing does not drop the row back to where the engine puts it"
        row = events.last("sidebar")["sessions"]["s-old"]
        assert row["modifiedAt"] > int((time.time() - 60) * 1000) and row["modified"] == "刚刚"

        # a time the engine lists that is later wins (the session was used somewhere else meanwhile)
        factory.saved = [saved_session("s-old", "Older", minutes_ago=-10)] + [s for s in factory.saved if s.id != "s-old"]
        await core.call("project.refresh", {"id": str(proj)})
        await until(lambda: events.last("sidebar")["sessions"]["s-old"]["modifiedAt"] > row["modifiedAt"] + 500_000)
        await core.shutdown()

    run(scenario())


def test_a_closed_codex_thread_keeps_the_time_of_its_last_message_too(tmp_path):
    async def scenario():
        core, factory, events, _, codex = await ready_core(tmp_path, codex=True)
        proj = (tmp_path / "proj").resolve()
        cid = (await core.call("session.new", {"projectId": str(proj), "modelId": "codex:gpt-5.3-codex"}))["id"]
        await until(lambda: sessions_of(events).get(cid, {}).get("mark") == "ready")
        controller = core._by_cid[cid].controller
        assert controller.engine == "codex"
        session = codex.factory.sessions[-1]
        await core.call("session.send", {"id": cid, "text": "hello"})
        await until(lambda: session.active is not None)
        session.active.push(delta("Hi", "m1"), complete("m1"), success("Hi"))
        session.active.end()
        await idle(core, cid)
        thread = controller.session_id
        # Codex lists the thread with the start of the turn, here three hours before the message
        codex.threads = [SimpleNamespace(id=thread, title="Hello", modified_at=datetime.now(timezone.utc) - timedelta(hours=3), message_count=None)]
        await core.call("session.close", {"id": cid})
        row_id = f"cx:{thread}"
        await until(lambda: row_id in events.last("sidebar")["sessions"])
        row = events.last("sidebar")["sessions"][row_id]
        assert row["engine"] == "codex" and row["modifiedAt"] > int((time.time() - 60) * 1000) and row["modified"] == "刚刚"
        await core.shutdown()

    run(scenario())


def test_a_turn_that_keeps_producing_output_counts_as_conversation_while_it_runs(tmp_path):
    async def scenario():
        core, factory, events, _, _ = await ready_core(tmp_path, projects=False)
        proj, _ = await three_saved(core, factory, events, tmp_path)
        long_one = (await core.call("session.openSaved", {"id": "s-old", "projectId": str(proj)}))["id"]
        await until(lambda: sessions_of(events).get(long_one, {}).get("mark") == "ready")
        session = factory.sessions[-1]
        await core.call("session.send", {"id": long_one, "text": "a long job"})
        await until(lambda: session.active is not None)
        controller = core._by_cid[long_one].controller
        # the job has been running for ten minutes, and another session said something two minutes ago
        controller.active_at = time.time() - 600
        other = (await core.call("session.openSaved", {"id": "s-mid", "projectId": str(proj)}))["id"]
        await until(lambda: sessions_of(events).get(other, {}).get("mark") == "ready")
        core._by_cid[other].controller.active_at = time.time() - 120
        core._emit_sidebar()
        assert listed(events).index(other) < listed(events).index(long_one), "silent for ten minutes: it is the older one"
        # the sidebar refreshes itself a moment after a session changes (and again after re-reading the saved list):
        # wait until it has been quiet, so that only the output below can re-sort the list
        seen = -1
        while seen != len(events.of("sidebar")):
            seen = len(events.of("sidebar"))
            await asyncio.sleep(1.0)

        session.active.push(delta("still going", "m1"))
        await until(lambda: controller.active_at > time.time() - 5)
        await until(lambda: listed(events).index(long_one) < listed(events).index(other))
        await until(lambda: sessions_of(events)[long_one]["modified"] == "刚刚")

        # output arriving many times a second does not move it (and the list) each time
        stamp = controller.active_at
        session.active.push(delta(" more", "m1"), delta(" and more", "m1"))
        await asyncio.sleep(0.2)
        assert controller.active_at == stamp
        session.active.push(complete("m1"), success("still going more and more"))
        session.active.end()
        await idle(core, long_one)
        await core.shutdown()

    run(scenario())


def test_a_new_empty_session_is_the_newest_and_changing_engine_does_not_change_its_place(tmp_path):
    async def scenario():
        core, factory, events, _, codex = await ready_core(tmp_path, projects=False, codex=True)
        proj, _ = await three_saved(core, factory, events, tmp_path)
        cid = await new_session(core, events, proj)
        core._emit_sidebar()
        assert listed(events) == [cid, "s-new", "s-mid", "s-old"], "nothing is older than a session that was just made"

        # a conversation last spoken to two hours ago, then moved to the other engine
        controller = core._by_cid[cid].controller
        spoken = time.time() - 7200
        controller.active_at = spoken
        core._emit_sidebar()
        assert listed(events) == ["s-new", "s-mid", cid, "s-old"]
        assert (await core.call("session.choose", {"id": cid, "field": "model", "value": "codex:gpt-5.3-codex"}))["switched"]
        after = core._by_cid[cid].controller
        assert after is not controller and after.active_at == spoken
        core._emit_sidebar()
        assert listed(events) == ["s-new", "s-mid", cid, "s-old"], "the switch is not a message"
        assert sessions_of(events)[cid]["modifiedAt"] == int(spoken * 1000)
        await core.shutdown()

    run(scenario())


def test_adding_and_removing_projects(tmp_path):
    async def scenario():
        core, factory, events, platform, _ = await ready_core(tmp_path, projects=False)
        proj = tmp_path / "p1"
        proj.mkdir()
        platform.folder = str(proj)
        added = await core.call("project.add", {})
        assert added["added"] is True and events.last("sidebar")["projects"][0]["name"] == "p1"
        with pytest.raises(AppError, match="已经在项目列表"):
            await core.call("project.add", {"path": str(proj)})
        with pytest.raises(AppError, match="不是有效的文件夹"):
            await core.call("project.add", {"path": str(tmp_path / "nope")})
        platform.folder = None
        assert (await core.call("project.add", {})) == {"added": False}
        cid = await new_session(core, events, proj)
        await core.call("project.remove", {"id": str(proj.resolve())})
        assert events.last("sidebar")["projects"] == [] and events.last("tabs")["tabs"] == [], "its open sessions go with it"
        assert cid not in [c.cid for c in core.order]
        await core.shutdown()

    run(scenario())


def test_idle_sessions_make_room_for_new_ones_and_busy_ones_do_not(tmp_path):
    async def scenario():
        core, factory, events, _, _ = await ready_core(tmp_path, max_open=2)
        proj = tmp_path / "proj"
        first = await new_session(core, events, proj)
        second = await new_session(core, events, proj)
        events.clear()
        third = await new_session(core, events, proj)
        assert events.last("tabs")["tabs"] == [second, third]
        assert any("已自动关闭最久未使用的会话" in n["text"] for n in events.of("notice"))
        session = factory.sessions[-1]
        await core.call("session.send", {"id": third, "text": "work"})
        await until(lambda: session.active is not None)
        other = factory.sessions[1]
        await core.call("session.send", {"id": second, "text": "work"})
        await until(lambda: other.active is not None)
        with pytest.raises(SessionLimitError):
            await core.call("session.new", {"projectId": str(proj)})
        session.active.push(success("x"))
        session.active.end()
        other.active.push(success("x"))
        other.active.end()
        await core.shutdown()

    run(scenario())


def test_attachments_are_admitted_by_python_and_sent_with_the_message(tmp_path):
    async def scenario():
        core, factory, events, platform, _ = await ready_core(tmp_path)
        cid = await new_session(core, events, tmp_path / "proj")
        session = factory.sessions[-1]
        png = base64.b64encode(tiny_png()).decode()
        added = await core.call("attachments.add", {"id": cid, "files": [{"name": "shot.png", "type": "image/png", "data": png}, {"name": "notes.txt", "type": "text/plain", "data": base64.b64encode(b"hello").decode()}, {"name": "empty.bin", "type": "", "data": ""}]})
        views = {v["name"]: v for v in added["added"]}
        assert set(views) == {"shot.png", "notes.txt"} and views["shot.png"]["url"].startswith("data:image/png") and views["shot.png"]["kind"] == "image"
        assert "empty.bin" in added["rejected"]
        picture_id = views["shot.png"]["id"]
        assert (await core.call("attachments.full", {"id": cid, "attachmentId": picture_id}))["url"] == "data:image/png;base64,FULL"
        await core.call("attachments.remove", {"id": cid, "attachmentId": views["notes.txt"]["id"]})
        await core.call("session.send", {"id": cid, "text": "look", "attachmentIds": [picture_id]})
        await until(lambda: session.active is not None)
        assert len(session.streams[-1].images) == 1 and not session.streams[-1].files
        await until(lambda: events.of("turns") and events.of("turns")[-1]["turns"])
        user = last_turn(events)["blocks"][0]
        assert user["attachments"][0]["name"] == "shot.png"
        session.active.push(success("ok"))
        session.active.end()
        await idle(core, cid)
        with pytest.raises(AppError):
            await core.call("attachments.add", {"id": "nope", "files": []})
        await core.shutdown()

    run(scenario())


def test_a_failed_turn_gives_the_message_and_its_attachments_back(tmp_path):
    async def scenario():
        core, factory, events, _, _ = await ready_core(tmp_path)
        cid = await new_session(core, events, tmp_path / "proj")
        session = factory.sessions[-1]
        png = base64.b64encode(tiny_png()).decode()
        (view,) = (await core.call("attachments.add", {"id": cid, "files": [{"name": "a.png", "type": "image/png", "data": png}]}))["added"]
        await core.call("session.send", {"id": cid, "text": "try this", "attachmentIds": [view["id"]]})
        await until(lambda: session.active is not None)
        session.active.push(DroidProcessError("boom", exit_code=1))
        await until(lambda: bool(events.of("restore")))
        restore = events.last("restore")
        assert restore["id"] == cid and restore["text"] == "try this" and [a["name"] for a in restore["attachments"]] == ["a.png"]
        assert any("附件已放回输入框" in n["text"] for n in events.of("notice"))
        await core.shutdown()

    run(scenario())


def test_reasoning_and_the_defaults_follow_the_choice(tmp_path):
    async def scenario():
        core, factory, events, _, _ = await ready_core(tmp_path)
        cid = await new_session(core, events, tmp_path / "proj")
        session = factory.sessions[-1]
        events.clear()
        assert (await core.call("session.choose", {"id": cid, "field": "reasoning", "value": "high"})) == {"ok": True}
        await until(lambda: sessions_of(events).get(cid, {}).get("settings", {}).get("reasoning") == "high")
        assert core.settings.reasoning_effort == "high" and events.last("defaults")["defaults"]["reasoning"] == "high"
        assert (await core.call("session.choose", {"id": cid, "field": "autonomy", "value": "medium"}))["ok"]
        assert core.settings.autonomy == "medium"
        # A model that does not offer the current level gets its own default, and the page is told why.
        events.clear()
        assert (await core.call("session.choose", {"id": cid, "field": "model", "value": "gpt-5.4"}))["ok"]
        assert session.settings_calls[-1]["model"] == "gpt-5.4" and session.settings_calls[-1]["reasoning_effort"].value == "medium"
        assert any("不支持「high」推理，已改为「medium」" in n["text"] for n in events.of("notice"))
        await until(lambda: sessions_of(events).get(cid, {}).get("settings", {}).get("model") == "gpt-5.4")
        # Without a session the choice only changes the defaults for new ones.
        assert (await core.call("session.choose", {"field": "mode", "value": "spec"})) == {"ok": True}
        assert core.settings.mode == "spec"
        with pytest.raises(AppError):
            await core.call("session.choose", {"id": cid, "field": "colour", "value": "red"})
        await core.shutdown()

    run(scenario())


def test_application_settings_are_validated_and_announced(tmp_path):
    async def scenario():
        core, factory, events, _, _ = await ready_core(tmp_path)
        view = await core.call("settings.set", {"key": "theme", "value": "dark"})
        assert view["theme"] == "dark" and events.last("settings")["settings"]["theme"] == "dark"
        assert (await core.call("settings.set", {"key": "maxOpen", "value": 6}))["maxOpen"] == 6
        for key, value in (("theme", "pink"), ("maxOpen", 99), ("maxOpen", True), ("fontScale", "xl"), ("sidebarWidth", 10), ("nothing", 1)):
            with pytest.raises(AppError):
                await core.call("settings.set", {"key": key, "value": value})
        await core.shutdown()

    run(scenario())


def test_the_compaction_threshold_is_set_validated_and_announced(tmp_path):
    async def scenario():
        core, factory, events, _, _ = await ready_core(tmp_path)
        init = (await core.call("app.init"))["settings"]
        assert (init["compactAuto"], init["compactAt"]) == (True, 85)
        view = await core.call("settings.set", {"key": "compactAt", "value": 70})
        assert view["compactAt"] == 70 and core.settings.chatgpt_compact_at == 70 and events.last("settings")["settings"]["compactAt"] == 70
        view = await core.call("settings.set", {"key": "compactAuto", "value": False})
        assert view["compactAuto"] is False and core.settings.chatgpt_compact is False
        for value in (50, 95):
            assert (await core.call("settings.set", {"key": "compactAt", "value": value}))["compactAt"] == value
        for key, value in (("compactAt", 49), ("compactAt", 96), ("compactAt", True), ("compactAt", "80"), ("compactAt", 80.5), ("compactAuto", 1), ("compactAuto", "yes")):
            with pytest.raises(AppError):
                await core.call("settings.set", {"key": key, "value": value})
        assert (core.settings.chatgpt_compact, core.settings.chatgpt_compact_at) == (False, 95), "a refused value changes nothing"
        assert (await core.call("app.init"))["settings"]["compactAt"] == 95, "a reloaded page is told too"
        await core.shutdown()

    run(scenario())


def test_models_are_hidden_and_shown_for_the_model_menu(tmp_path):
    async def scenario():
        core, factory, events, _, _ = await ready_core(tmp_path)
        assert (await core.call("app.init"))["settings"]["hiddenModels"] == []
        view = await core.call("models.setVisible", {"ids": ["gpt-5.4", "gpt-5.4", "codex:x"], "visible": False})
        assert view["hiddenModels"] == ["gpt-5.4", "codex:x"], "once each, in the order hidden"
        assert events.last("settings")["settings"]["hiddenModels"] == ["gpt-5.4", "codex:x"]
        assert core.settings.hidden_models == ["gpt-5.4", "codex:x"]
        view = await core.call("models.setVisible", {"ids": ["gpt-5.4", "unknown"], "visible": True})
        assert view["hiddenModels"] == ["codex:x"], "showing a model that was not hidden is nothing"
        assert (await core.call("app.init"))["settings"]["hiddenModels"] == ["codex:x"], "a reloaded page is told too"
        for params in ({"ids": "gpt-5.4", "visible": False}, {"ids": [""], "visible": False}, {"ids": [3], "visible": True}, {"ids": ["a"], "visible": 1}, {"visible": True}):
            with pytest.raises(AppError):
                await core.call("models.setVisible", params)
        assert core.settings.hidden_models == ["codex:x"]
        await core.shutdown()

    run(scenario())


def test_a_hidden_default_model_is_not_what_new_sessions_start_with(tmp_path):
    async def scenario():
        core, factory, events, _, _ = await ready_core(tmp_path)
        assert (await core.call("session.choose", {"field": "model", "value": "gpt-5.4"})) == {"ok": True}
        assert core.defaults()["model"] == "gpt-5.4"
        await core.call("models.setVisible", {"ids": ["gpt-5.4"], "visible": False})
        assert events.last("defaults")["defaults"]["model"] == "claude-haiku-5-5", "the first model that is shown"
        assert core.settings.default_model == "gpt-5.4", "the pick itself is kept"
        await core.call("models.setVisible", {"ids": ["claude-haiku-5-5"], "visible": False})
        assert events.last("defaults")["defaults"]["model"] == "", "nothing of Factory's is shown: Droid picks"
        await core.call("models.setVisible", {"ids": ["gpt-5.4"], "visible": True})
        assert events.last("defaults")["defaults"]["model"] == "gpt-5.4", "shown again, the pick is the default again"
        await core.shutdown()

    run(scenario())


def test_the_window_calls_reach_the_platform(tmp_path):
    async def scenario():
        core, factory, events, platform, _ = await ready_core(tmp_path)
        await core.call("window.minimize")
        assert (await core.call("window.toggleMaximize")) is True
        await core.call("window.startMove")
        await core.call("window.startResize", {"edge": "se"})
        await core.call("window.close")
        assert platform.calls == ["minimize", "maximize", "move", "resize:se", "close"]
        await core.call("shell.openExternal", {"url": "https://example.com/x"})
        await core.call("shell.openExternal", {"url": "file:///C:/Windows/notepad.exe"})
        assert platform.opened == ["https://example.com/x"], "only web links leave the app"
        with pytest.raises(AppError):
            await core.call("no.such.call")
        await core.shutdown()

    run(scenario())


# --------------------------------------------------------------------------- changing engine inside a conversation


def test_changing_engine_continues_the_same_conversation_with_the_text_handed_over(tmp_path):
    async def scenario():
        core, factory, events, _, codex = await ready_core(tmp_path, codex=True)
        models = events.last("models")["models"]
        assert [m["id"] for m in models if m["source"] == "codex"] == ["codex:gpt-5.3-codex"]
        cid = await new_session(core, events, tmp_path / "proj")
        droid = factory.sessions[-1]
        await core.call("session.send", {"id": cid, "text": "What does context.py do?"})
        await until(lambda: droid.active is not None)
        droid.active.push(delta("It reads the context size.", "m1"), complete("m1"), success("It reads the context size."))
        droid.active.end()
        await idle(core, cid)
        events.clear()

        result = await core.call("session.choose", {"id": cid, "field": "model", "value": "codex:gpt-5.3-codex"})
        assert result == {"ok": True, "switched": True}
        assert [c.cid for c in core.order] == [cid], "the same tab"
        assert codex.opened[0]["model"] == "gpt-5.3-codex" and codex.opened[0]["handoff"].carried == 2
        turns = events.of("turns")[-1]
        assert turns["all"] is False and turns["turns"][-1]["blocks"][0]["kind"] == "switch"
        switch = turns["turns"][-1]["blocks"][0]
        assert switch["from"] == {"model": "Haiku 5.5", "engine": "Droid"} and switch["to"]["engine"] == "Codex" and switch["carried"] == 2
        meta = sessions_of(events)[cid]
        assert meta["engine"] == "codex" and meta["settings"]["model"] == "codex:gpt-5.3-codex"
        assert meta["usage"] is None and meta["handoff"]["carried"] == 2
        await until(lambda: droid.closed == 1)
        assert any("已换到 GPT-5.3 Codex（Codex）" in n["text"] and "2 条对话的文字" in n["text"] for n in events.of("notice"))

        # the next message carries the earlier text to the model, but the page shows only what was typed
        events.clear()
        await core.call("session.send", {"id": cid, "text": "and then?"})
        codex_session = codex.factory.sessions[-1]
        await until(lambda: codex_session.active is not None)
        prompt = codex_session.streams[-1].prompt
        assert "What does context.py do?" in prompt and "It reads the context size." in prompt and prompt.endswith("and then?")
        await until(lambda: events.of("turns") and events.of("turns")[-1]["turns"])
        user = last_turn(events)["blocks"][0]
        assert user["text"] == "and then?" and user["handoff"] == {"carried": 2, "truncated": False}
        codex_session.active.push(delta("Next.", "m2"), complete("m2"), success("Next."))
        codex_session.active.end()
        await idle(core, cid)
        await until(lambda: sessions_of(events)[cid].get("handoff", 1) is None)

        # and back: the whole conversation, including the Codex turn, goes along
        factory.next_session = {"model": "gpt-5.4"}
        events.clear()
        assert (await core.call("session.choose", {"id": cid, "field": "model", "value": "gpt-5.4"}))["switched"]
        second = events.of("turns")[-1]["turns"][-1]["blocks"][0]
        assert second["kind"] == "switch" and second["from"]["engine"] == "Codex" and second["carried"] == 4
        await core.shutdown()

    run(scenario())


async def send_with_files(core, factory, events, cid, text, files):
    """One finished turn on the conversation's Droid session, with these (name, type, bytes) files attached."""
    added = await core.call(
        "attachments.add",
        {"id": cid, "files": [{"name": name, "type": kind, "data": base64.b64encode(data).decode()} for name, kind, data in files]},
    )
    droid = factory.sessions[-1]
    before = len(droid.streams)
    await core.call("session.send", {"id": cid, "text": text, "attachmentIds": [v["id"] for v in added["added"]]})
    await until(lambda: len(droid.streams) > before and droid.active is not None)
    droid.active.push(delta("noted", "m1"), complete("m1"), success("noted"))
    droid.active.end()
    await idle(core, cid)


def pdf_bytes(words: str = "") -> bytes:
    """A PDF for the fake platform: the words after TEXT: are what 'parsing' finds; none: a scan."""
    return b"%PDF-1.4 " + (b"TEXT:" + words.encode() if words else b"")


async def ask_to_switch(core, cid, *, model="codex:gpt-5.3-codex", **more):
    return await core.call("session.choose", {"id": cid, "field": "model", "value": model, **more})


def test_changing_engine_lists_every_attachment_and_changes_nothing_until_told_which_go(tmp_path):
    async def scenario():
        core, factory, events, _, codex = await ready_core(tmp_path, codex=True)
        cid = await new_session(core, events, tmp_path / "proj")
        picture = tiny_png()
        await send_with_files(
            core, factory, events, cid, "read these",
            [
                ("notes.txt", "text/plain", b"remember the milk"),
                ("shot.png", "image/png", picture),
                ("scan.pdf", "application/pdf", pdf_bytes()),
                ("paper.pdf", "application/pdf", pdf_bytes("the budget is five")),
            ],
        )
        before = len(codex.opened)

        asked = await ask_to_switch(core, cid)
        assert asked["ok"] is False
        listed = {item["name"]: item for item in asked["choose"]["attachments"]}
        assert list(listed) == ["notes.txt", "shot.png", "scan.pdf", "paper.pdf"], "every one is listed, in the order they were sent"
        assert [(i["kind"], i["as"], i["bytes"]) for i in listed.values()] == [
            ("text", "text", len(b"remember the milk")),
            ("image", "image", len(picture)),
            ("pdf", "", len(pdf_bytes())),  # a scan cannot go to Codex: nothing to carry it as
            ("pdf", "text", len("the budget is five".encode())),
        ]
        assert [("reason" in i) for i in listed.values()] == [False, False, True, False]
        assert "只能读 PDF 里的文字" in listed["scan.pdf"]["reason"], "what cannot go says why; it is not left out"
        assert len({i["id"] for i in listed.values()}) == 4
        assert asked["choose"]["limits"] == {"files": 10, "bytes": 20 * 1024 * 1024}
        assert len(codex.opened) == before and core._by_cid[cid].controller.engine == "droid", "asking changes nothing"

        for bad in ("yes", 1, "true", [1], {"a": "b"}, True):
            with pytest.raises(AppError, match="不合法"):
                await ask_to_switch(core, cid, attachments=bad)
        with pytest.raises(AppError, match="找不到选中的附件"):
            await ask_to_switch(core, cid, attachments=["a999"])
        with pytest.raises(AppError, match="scan.pdf.*带不过去.*只能读 PDF 里的文字"):
            await ask_to_switch(core, cid, attachments=[listed["scan.pdf"]["id"]])
        assert len(codex.opened) == before, "a wrong answer changes nothing either"
        await core.shutdown()

    run(scenario())


def test_the_attachments_picked_go_along_one_by_one_and_the_rest_stay_behind(tmp_path):
    async def scenario():
        core, factory, events, _, codex = await ready_core(tmp_path, codex=True)
        cid = await new_session(core, events, tmp_path / "proj")
        await send_with_files(
            core, factory, events, cid, "read these",
            [
                ("notes.txt", "text/plain", b"remember the milk"),
                ("secret.txt", "text/plain", b"do not hand this over"),
                ("shot.png", "image/png", tiny_png()),
                ("other.png", "image/png", tiny_png(5, 5, (1, 2, 3))),
            ],
        )
        asked = await ask_to_switch(core, cid)
        ids = {item["name"]: item["id"] for item in asked["choose"]["attachments"]}

        done = await ask_to_switch(core, cid, attachments=[ids["notes.txt"], ids["shot.png"], ids["shot.png"]])
        assert done == {"ok": True, "switched": True}
        handoff = codex.opened[-1]["handoff"]
        assert handoff.files == 1 and handoff.pictures == 1 and handoff.pdfs == 0 and handoff.carried == 2
        assert "remember the milk" in handoff.prefix and "do not hand this over" not in handoff.prefix
        assert [a.name for a in handoff.attachments] == ["shot.png"], "a picture picked twice goes once; the one not picked stays"
        (note,) = [n["text"] for n in events.of("notice") if "已换到" in n["text"]]
        assert "1 个文本附件的内容、1 张图片一起带过去" in note

        # the first message sends the picture with it; the page shows only what was typed
        events.clear()
        await core.call("session.send", {"id": cid, "text": "and now?"})
        codex_session = codex.factory.sessions[-1]
        await until(lambda: codex_session.active is not None)
        stream = codex_session.streams[-1]
        assert [i.source.media_type for i in stream.images] == ["image/png"] and "remember the milk" in stream.prompt
        await until(lambda: events.of("turns") and events.of("turns")[-1]["turns"])
        assert "attachments" not in last_turn(events)["blocks"][0], "the page shows only what was typed"
        codex_session.active.push(delta("ok", "m2"), complete("m2"), success("ok"))
        codex_session.active.end()
        await idle(core, cid)
        await until(lambda: sessions_of(events)[cid].get("handoff", 1) is None)

        await core.call("session.send", {"id": cid, "text": "again"})
        await until(lambda: codex_session.active is not None and len(codex_session.streams) == 2)
        assert codex_session.streams[-1].images == (), "what was handed over goes once"
        codex_session.active.push(success("ok"))
        codex_session.active.end()
        await core.shutdown()

    run(scenario())


def test_picking_nothing_carries_no_attachment_of_any_kind(tmp_path):
    async def scenario():
        core, factory, events, _, codex = await ready_core(tmp_path, codex=True)
        cid = await new_session(core, events, tmp_path / "proj")
        await send_with_files(
            core, factory, events, cid, "read",
            [("notes.txt", "text/plain", b"remember the milk"), ("shot.png", "image/png", tiny_png())],
        )
        assert (await ask_to_switch(core, cid, attachments=[])) == {"ok": True, "switched": True}
        handoff = codex.opened[-1]["handoff"]
        assert handoff.files == 0 and handoff.pictures == 0 and handoff.attachments == () and handoff.carried == 2
        assert "remember the milk" not in handoff.prefix
        (note,) = [n["text"] for n in events.of("notice") if "已换到" in n["text"]]
        assert "一起带过去" not in note
        await core.shutdown()

    run(scenario())


def test_a_conversation_of_pictures_only_is_asked_about_and_the_picture_can_be_all_that_goes(tmp_path):
    async def scenario():
        core, factory, events, _, codex = await ready_core(tmp_path, codex=True)
        cid = await new_session(core, events, tmp_path / "proj")
        await send_with_files(core, factory, events, cid, "look", [("shot.png", "image/png", tiny_png())])
        asked = await ask_to_switch(core, cid)
        assert asked["ok"] is False, "a picture is the user's to place, so the question is asked"
        (item,) = asked["choose"]["attachments"]
        assert item["kind"] == "image" and item["as"] == "image" and "reason" not in item

        await ask_to_switch(core, cid, attachments=[item["id"]])
        await core.call("session.send", {"id": cid, "text": "what is in it?"})
        codex_session = codex.factory.sessions[-1]
        await until(lambda: codex_session.active is not None)
        assert len(codex_session.streams[-1].images) == 1, "the picture goes with the first message"
        codex_session.active.push(success("a red box"))
        codex_session.active.end()
        await core.shutdown()

    run(scenario())


def test_a_scanned_pdf_goes_back_to_droid_as_the_file_and_stays_behind_for_codex(tmp_path):
    async def scenario():
        core, factory, events, _, codex = await ready_core(tmp_path, codex=True)
        cid = await new_session(core, events, tmp_path / "proj")
        await send_with_files(core, factory, events, cid, "scan", [("scan.pdf", "application/pdf", pdf_bytes())])
        await ask_to_switch(core, cid, attachments=[])
        codex_session = codex.factory.sessions[-1]
        await core.call("session.send", {"id": cid, "text": "hello"})
        await until(lambda: codex_session.active is not None)
        codex_session.active.push(delta("hi", "m1"), complete("m1"), success("hi"))
        codex_session.active.end()
        await idle(core, cid)

        # the scan is still in the conversation: Droid can read it, so it is offered with no reason
        factory.next_session = {"model": "gpt-5.4"}
        asked = await ask_to_switch(core, cid, model="gpt-5.4")
        (item,) = asked["choose"]["attachments"]
        assert item["name"] == "scan.pdf" and item["as"] == "pdf" and "reason" not in item
        await ask_to_switch(core, cid, model="gpt-5.4", attachments=[item["id"]])
        final = core._by_cid[cid].controller.handoff
        assert final.pdfs == 1 and [a.name for a in final.attachments] == ["scan.pdf"] and final.attachments[0].data.startswith(b"%PDF")
        await core.shutdown()

    run(scenario())


def test_what_the_new_model_cannot_take_is_listed_with_the_reason_and_cannot_be_picked(tmp_path):
    async def scenario():
        core, factory, events, _, codex = await ready_core(tmp_path, codex=True)
        codex.supports_images = False
        cid = await new_session(core, events, tmp_path / "proj")
        await send_with_files(
            core, factory, events, cid, "read",
            [("notes.txt", "text/plain", b"remember the milk"), ("shot.png", "image/png", tiny_png())],
        )
        asked = await ask_to_switch(core, cid)
        listed = {item["name"]: item for item in asked["choose"]["attachments"]}
        assert "reason" not in listed["notes.txt"]
        assert listed["shot.png"]["reason"] == "「GPT-5.3 Codex」不支持图片输入。"
        before = len(codex.opened)
        with pytest.raises(AppError, match="shot.png.*带不过去.*不支持图片输入"):
            await ask_to_switch(core, cid, attachments=[listed["notes.txt"]["id"], listed["shot.png"]["id"]])
        assert len(codex.opened) == before, "nothing is carried quietly in place of what was asked"
        await ask_to_switch(core, cid, attachments=[listed["notes.txt"]["id"]])
        assert codex.opened[-1]["handoff"].files == 1
        await core.shutdown()

    run(scenario())


def test_picking_more_than_a_message_usually_holds_is_the_users_call_told_by_how_much(tmp_path):
    async def scenario():
        core, factory, events, _, codex = await ready_core(tmp_path, codex=True)
        cid = await new_session(core, events, tmp_path / "proj")
        for turn in range(2):  # 12 pictures, in two messages: one message usually holds 10
            await send_with_files(
                core, factory, events, cid, f"batch {turn}",
                [(f"p{turn}-{i}.png", "image/png", tiny_png(4 + i, 3, (turn, i, 9))) for i in range(6)],
            )
        asked = await ask_to_switch(core, cid)
        ids = [item["id"] for item in asked["choose"]["attachments"]]
        assert len(ids) == 12, "all twelve are listed"
        assert asked["choose"]["limits"] == {"files": 10, "bytes": 20 * 1024 * 1024}
        assert asked["choose"]["hard"] == {"files": 50, "bytes": 200 * 1024 * 1024}, "what the page may not let through at all"
        before = len(codex.opened)

        # without the user's word it is an error that says by how much (the page asks them); nothing changes
        with pytest.raises(AppError, match="比一条消息通常带的量多 2 个文件.*这条消息有 12 个附件.*请在清单里确认"):
            await ask_to_switch(core, cid, attachments=ids)
        for bad in ("yes", 1, [True]):
            with pytest.raises(AppError, match="不合法"):
                await ask_to_switch(core, cid, attachments=ids, overLimit=bad)
        assert len(codex.opened) == before

        done = await ask_to_switch(core, cid, attachments=ids, overLimit=True)
        assert done == {"ok": True, "switched": True}
        assert len(codex.opened[-1]["handoff"].attachments) == 12, "all twelve go: nothing is dropped for being many"

        # the twelve go with the next message; a picture in the box makes thirteen: asked once more when it is sent
        added = await core.call("attachments.add", {"id": cid, "files": [png_upload("early.png", tiny_png(9, 9))]})
        assert len(added["added"]) == 1 and "多 3 个文件" in added["over"] and "发送时会让你确认" in added["over"]
        codex_session = codex.factory.sessions[-1]
        asked_to_send = await core.call("session.send", {"id": cid, "text": "go"})
        assert asked_to_send["ok"] is False and asked_to_send["over"]["files"] == 3 and asked_to_send["over"]["count"] == 13
        assert "多 3 个文件" in asked_to_send["over"]["text"] and codex_session.streams == [], "nothing was sent"
        await core.call("session.send", {"id": cid, "text": "go", "confirmOver": True})
        await until(lambda: codex_session.active is not None)
        assert len(codex_session.streams[-1].images) == 13
        codex_session.active.push(success("ok"))
        codex_session.active.end()
        await core.shutdown()

    run(scenario())


def test_the_hand_over_to_a_droid_model_follows_the_window_the_new_session_reports(tmp_path):
    """Droid's models have no window in the model list; the new session says it a moment after it is open."""
    from desktop.core.handoff import MAX_BYTES

    async def scenario():
        core, factory, events, _, codex = await ready_core(tmp_path, codex=True)
        cid = await new_session(core, events, tmp_path / "proj")
        for i in range(5):
            droid = factory.sessions[-1]
            await core.call("session.send", {"id": cid, "text": f"question {i}"})
            await until(lambda: droid.active is not None)
            droid.active.push(delta("y" * 30_000, f"m{i}"), complete(f"m{i}"), success("y"))
            droid.active.end()
            await idle(core, cid)
        assert (await core.call("session.choose", {"id": cid, "field": "model", "value": "codex:gpt-5.3-codex"}))["switched"]
        codex_handoff = codex.opened[-1]["handoff"]
        assert codex_handoff.truncated and codex_handoff.size <= 120_000, "Codex's window (400K) gives 120 KB"

        factory.next_session = {"model": "gpt-5.4"}
        assert (await core.call("session.choose", {"id": cid, "field": "model", "value": "gpt-5.4"}))["switched"]
        final = core._by_cid[cid].controller.handoff
        assert final.carried == final.total and not final.truncated, "the whole conversation fits the window the Droid session reported"
        assert final.size > MAX_BYTES, "more than the budget of a model whose window is not known"
        switch = events.of("turns")[-1]["turns"][-1]["blocks"][0]
        assert switch["kind"] == "switch" and switch["carried"] == final.carried and switch["truncated"] is False
        await core.shutdown()

    run(scenario())


def test_a_switch_that_fails_leaves_the_conversation_as_it_was(tmp_path):
    async def scenario():
        core, factory, events, _, codex = await ready_core(tmp_path, codex=True)
        cid = await new_session(core, events, tmp_path / "proj")
        droid = factory.sessions[-1]
        await core.call("session.send", {"id": cid, "text": "hello"})
        await until(lambda: droid.active is not None)
        droid.active.push(delta("Hi.", "m1"), complete("m1"), success("Hi."))
        droid.active.end()
        await idle(core, cid)
        codex.fail_open = DroidProcessError("codex is not signed in", exit_code=1)
        events.clear()
        with pytest.raises(AppError):
            await core.call("session.choose", {"id": cid, "field": "model", "value": "codex:gpt-5.3-codex"})
        assert [c.cid for c in core.order] == [cid] and core.order[0].controller.engine == "droid"
        transcript = core._by_cid[cid].controller.transcript
        assert not any(getattr(block, "kind", "") == "switch" for turn in transcript.turns for block in turn.blocks)
        assert not any(getattr(block, "kind", "") == "error" for turn in transcript.turns for block in turn.blocks), "the failed attempt leaves no trace"
        assert droid.closed == 0, "the working session is not closed by a failed attempt"
        await core.call("session.send", {"id": cid, "text": "still here?"})
        await until(lambda: droid.active is not None)
        droid.active.push(success("yes"))
        droid.active.end()
        await idle(core, cid)
        await core.shutdown()

    run(scenario())


def test_a_running_turn_cannot_change_engine_and_an_empty_session_switches_without_a_divider(tmp_path):
    async def scenario():
        core, factory, events, _, codex = await ready_core(tmp_path, codex=True)
        cid = await new_session(core, events, tmp_path / "proj")
        droid = factory.sessions[-1]
        result = await core.call("session.choose", {"id": cid, "field": "model", "value": "codex:gpt-5.3-codex"})
        assert result["switched"] and codex.opened[0]["handoff"] is None
        assert not any(b["kind"] == "switch" for turn in [t for p in events.of("turns") for t in p["turns"]] for b in turn["blocks"])
        await core.call("session.send", {"id": cid, "text": "work"})
        codex_session = codex.factory.sessions[-1]
        await until(lambda: codex_session.active is not None)
        with pytest.raises(AppError, match="本轮还在运行"):
            await core.call("session.choose", {"id": cid, "field": "model", "value": "gpt-5.4"})
        codex_session.active.push(success("x"))
        codex_session.active.end()
        await core.shutdown()

    run(scenario())


def test_codex_calls_report_the_account_and_refresh_the_model_list(tmp_path):
    async def scenario():
        core, factory, events, _, codex = await ready_core(tmp_path, codex=True)
        codex.signed_in = False
        account = await core.call("codex.account")
        assert account["auth"] == "signed-out" and account["enabled"] is True
        events.clear()
        account = await core.call("codex.login")
        assert account["auth"] == "chatgpt" and events.last("accounts")["codex"]["email"] == "a@b.c"
        await until(lambda: bool(events.of("models")))
        account = await core.call("codex.setEnabled", {"on": False})
        assert account["enabled"] is False and core.settings.codex_enabled is False
        await core.shutdown()

    run(scenario())


def test_without_a_codex_engine_the_codex_calls_explain_what_is_missing(tmp_path):
    async def scenario():
        core, factory, events, _, _ = await ready_core(tmp_path)
        assert (await core.call("codex.account"))["cli"] == "missing"
        with pytest.raises(AppError, match="Codex"):
            await core.call("codex.login")
        with pytest.raises(AppError, match="API Provider"):
            await core.call("providers.save", {"draft": {}})
        assert await core.call("providers.envStatus", {"names": ["A"]}) == {"A": "unset"}
        await core.shutdown()

    run(scenario())


# --------------------------------------------------------------------------- API providers through the core


def provider_draft(**over):
    base = {
        "id": "p1", "name": "OpenRouter", "kind": "generic-chat-completion-api", "baseUrl": "https://openrouter.ai/api/v1",
        "keyMode": "env", "keyHint": "${OPENROUTER_API_KEY}",
        "models": [{"id": "openai/gpt-oss-120b", "displayName": "GPT-OSS 120B", "reasoning": "medium", "images": False}],
        "headers": [], "extraArgs": "",
    }
    base.update(over)
    return base


def test_a_saved_provider_shows_up_in_the_model_menu_under_its_own_source_and_runs_on_droid(tmp_path):
    import json

    from droid_sdk import ModelInfo, ModelProvider

    from desktop.core.providers import ProviderStore

    async def scenario():
        factory_settings = tmp_path / "factory.json"
        store = ProviderStore(factory_settings=factory_settings, sidecar=tmp_path / "providers.json", environ={"OPENROUTER_API_KEY": "sentinel"}, registry_env=lambda: {})
        core, factory, events, _, _ = make_core_with(tmp_path, store)
        await core.call("app.init")
        await until(lambda: bool(events.of("models")))
        assert not [m for m in events.last("models")["models"] if m["source"].startswith("api:")]

        saved = await core.call("providers.save", {"draft": provider_draft()})
        assert saved["id"] == "p1" and "sentinel" not in json.dumps(saved)
        (written,) = json.loads(factory_settings.read_text(encoding="utf-8"))["customModels"]
        assert written["apiKey"] == "${OPENROUTER_API_KEY}" and written["model"] == "openai/gpt-oss-120b"
        (custom_id, _) = store.custom_model_ids("p1")[0]
        # droid now lists it: the next model list refresh puts it under the provider
        factory.models.append(
            ModelInfo(id=custom_id, display_name="GPT-OSS 120B", short_display_name="GPT-OSS", model_provider=ModelProvider.GENERIC_CHAT_COMPLETION_API,
                      supported_reasoning_efforts=(), default_reasoning_effort="medium", is_custom=True)
        )
        events.clear()
        await until(lambda: bool(events.of("models")))
        models = events.last("models")["models"]
        mine = [m for m in models if m["source"] == "api:p1"]
        assert [m["id"] for m in mine] == [f"api:p1:{custom_id}"] and mine[0]["label"] == "GPT-OSS 120B"
        assert core.catalog.target(mine[0]["id"]).model == custom_id and core.catalog.engine_of(mine[0]["id"]) == "droid"

        # a session started with it sends droid the custom id
        result = await core.call("session.new", {"projectId": str((tmp_path / "proj").resolve()), "modelId": mine[0]["id"]})
        assert factory.calls[-1][1]["model"] == custom_id and result["id"]

        await core.call("providers.remove", {"id": "p1"})
        assert json.loads(factory_settings.read_text(encoding="utf-8"))["customModels"] == []
        assert (await core.call("providers.envStatus", {"names": ["OPENROUTER_API_KEY", "NOPE"]})) == {"OPENROUTER_API_KEY": "set", "NOPE": "unset"}
        await core.shutdown()

    run(scenario())


def test_an_unreadable_factory_settings_file_is_reported_and_the_app_still_starts(tmp_path):
    from desktop.core.providers import ProviderStore

    async def scenario():
        broken = tmp_path / "factory.json"
        broken.write_text("{ not json", encoding="utf-8")
        store = ProviderStore(factory_settings=broken, sidecar=tmp_path / "providers.json", environ={}, registry_env=lambda: {})
        core, factory, events, _, _ = make_core_with(tmp_path, store)
        init = await core.call("app.init")
        assert init["providers"] == [] and init["projects"], "the rest of the application is there"
        assert any("API Provider" in n["text"] for n in events.of("notice"))
        with pytest.raises(Exception):
            await core.call("providers.save", {"draft": provider_draft()})
        assert broken.read_text(encoding="utf-8") == "{ not json", "nothing is written over a file that cannot be read"
        await core.shutdown()

    run(scenario())


# --------------------------------------------------------------------------- the real Codex engine (fake app-server) through the core


def make_core_with_real_codex(tmp_path, *, account="chatgpt"):
    """AppCore on a fake Droid and the real CodexEngine talking to ``tests/fake_codex.py``."""
    from desktop.core.app_core import AppCore
    from desktop.core.session_manager import SessionManager
    from tests.app_support import Events, FakePlatform
    from tests.codex_support import make_engine
    from tests.fakes import FakeFactory
    from desktop.core.settings import AppSettings
    from droid_sdk import ModelInfo, ModelProvider, ReasoningEffort

    project = tmp_path / "proj"
    project.mkdir(exist_ok=True)
    settings = AppSettings(default_model="claude-haiku-5-5", reasoning_effort="low", think_chinese=False)  # (as make_core)
    settings.add_project(project)
    factory = FakeFactory()
    factory.models = [
        ModelInfo(id="claude-haiku-5-5", display_name="Haiku 5.5", short_display_name="Haiku", model_provider=ModelProvider.ANTHROPIC,
                  supported_reasoning_efforts=(ReasoningEffort.LOW, ReasoningEffort.HIGH), default_reasoning_effort=ReasoningEffort.LOW)
    ]
    harness = make_engine(tmp_path, account=account, settings=settings)
    events = Events()
    core = AppCore(SessionManager(settings, factory), FakePlatform(), codex=harness.engine, persist=False, emit=events)
    return core, factory, events, harness


def test_the_real_codex_engine_lists_its_models_and_reports_its_account_through_the_core(tmp_path):
    async def scenario():
        core, factory, events, harness = make_core_with_real_codex(tmp_path)
        init = await core.call("app.init")
        assert init["codex"]["auth"] == "checking" and init["codex"]["cli"] == "found"
        await until(lambda: bool(events.of("models")) and events.of("accounts"))
        codex_models = [m for m in events.last("models")["models"] if m["source"] == "codex"]
        assert codex_models and all(m["id"].startswith("codex:") for m in codex_models)
        assert all(m["efforts"] for m in codex_models), "Codex tells which levels each model offers"
        account = events.of("accounts")[-1]["codex"]
        assert account["auth"] == "chatgpt" and account["enabled"] is True and account["limits"]
        await core.shutdown()

    run(scenario())


def test_a_droid_conversation_continues_on_the_real_codex_engine_with_the_earlier_text(tmp_path):
    async def scenario():
        core, factory, events, harness = make_core_with_real_codex(tmp_path)
        await core.call("app.init")
        await until(lambda: any(m["source"] == "codex" for p in events.of("models") for m in p["models"]))
        cid = await new_session(core, events, tmp_path / "proj")
        droid = factory.sessions[-1]
        await core.call("session.send", {"id": cid, "text": "What does context.py do?"})
        await until(lambda: droid.active is not None)
        droid.active.push(delta("It reads the context size.", "m1"), complete("m1"), success("It reads the context size."))
        droid.active.end()
        await idle(core, cid)
        model = next(m for m in events.last("models")["models"] if m["source"] == "codex")
        events.clear()

        result = await core.call("session.choose", {"id": cid, "field": "model", "value": model["id"]})
        assert result["switched"] is True and [c.cid for c in core.order] == [cid]
        assert core.order[0].engine == "codex" and core.order[0].controller.engine == "codex"
        patch = sessions_of(events)[cid]
        assert patch["engine"] == "codex" and patch["settings"]["model"] == model["id"] and patch["handoff"]["carried"] == 2
        assert patch["settings"]["autonomy"] == "readonly", "the permission scale is Codex's: the old Droid level gave way"
        await until(lambda: droid.closed == 1)

        events.clear()
        await core.call("session.send", {"id": cid, "text": "and then?"})
        await idle(core, cid)
        await until(lambda: any(t["status"] == "done" for p in events.of("turns") for t in p["turns"]))
        started = harness.requests("turn/start")
        assert len(started) == 1
        sent = started[0]["input"][0]["text"]
        assert "What does context.py do?" in sent and "It reads the context size." in sent and sent.endswith("and then?")
        user = next(b for p in events.of("turns") for t in p["turns"] for b in t["blocks"] if b["kind"] == "user")
        assert user["text"] == "and then?" and user["handoff"] == {"carried": 2, "truncated": False}
        assert sessions_of(events)[cid]["handoff"] is None, "handed over once"
        await core.shutdown()

    run(scenario())


def test_switching_to_codex_while_signed_out_fails_with_a_clear_message_and_changes_nothing(tmp_path):
    async def scenario():
        core, factory, events, harness = make_core_with_real_codex(tmp_path, account="signed-out")
        await core.call("app.init")
        await until(lambda: bool(events.of("models")))
        assert not [m for m in events.last("models")["models"] if m["source"] == "codex"], "no Codex models until signed in"
        cid = await new_session(core, events, tmp_path / "proj")
        with pytest.raises(AppError, match="登录"):
            await core.call("session.choose", {"id": cid, "field": "model", "value": "codex:gpt-5.3-codex"})
        assert core.order[0].controller.engine == "droid" and factory.sessions[-1].closed == 0
        events.clear()
        account = await core.call("codex.login")
        assert account["auth"] == "chatgpt"
        await until(lambda: bool(events.of("models")))
        assert any(m["source"] == "codex" for m in events.last("models")["models"]), "signing in adds the models"
        await core.shutdown()

    run(scenario())


# --------------------------------------------------------------------------- small guarantees the page relies on


def test_provider_and_session_limit_errors_keep_their_own_message():
    from desktop.core.app_core import error_text
    from desktop.core.providers import ProviderError

    assert error_text(ProviderError("Base URL 不能是空的。")) == "Base URL 不能是空的。"
    assert error_text(SessionLimitError("最多同时打开 4 个会话；请先停止或关闭一个。")) == "最多同时打开 4 个会话；请先停止或关闭一个。"
    assert error_text(AppError("这个会话已经关闭。")) == "这个会话已经关闭。"


def test_a_page_reinit_resends_every_open_conversation(tmp_path):
    async def scenario():
        core, factory, events, _, _ = await ready_core(tmp_path)
        cid = await new_session(core, events, tmp_path / "proj")
        session = factory.sessions[-1]
        await core.call("session.send", {"id": cid, "text": "hello"})
        await until(lambda: session.active is not None)
        session.active.push(delta("Hi there"), complete(), success("Hi there"))
        session.active.end()
        await idle(core, cid)
        events.clear()

        init = await core.call("app.init")
        assert init["tabs"] == [cid] and init["sessions"] == {}
        await until(lambda: events.of("turns") and cid in sessions_of(events))
        turns = [turn for payload in events.of("turns") for turn in payload["turns"]]
        assert any(block.get("text") == "hello" for turn in turns for block in turn["blocks"] if block["kind"] == "user")
        assert any(block.get("markdown") == "Hi there" for turn in turns for block in turn["blocks"] if block["kind"] == "text")
        patch = sessions_of(events)[cid]
        assert {"title", "mark", "engine", "settings", "usage", "context"} <= set(patch), "the whole meta again, not just a diff"
        assert patch["mark"] == "ready" and patch["settings"]["model"] == "claude-haiku-5-5"
        await core.shutdown()

    run(scenario())


def test_changing_the_droid_path_forgets_and_redetects_the_version(tmp_path):
    async def scenario():
        core, factory, events, _, _ = await ready_core(tmp_path)
        await until(lambda: bool(events.of("accounts")), timeout=15.0)  # init's own refresh is done racing us
        exe = tmp_path / "droid.exe"
        exe.write_bytes(b"")
        core._droid_version = "0.0.0-old"

        async def fake_version():
            return "9.9.9-test"

        core._droid_version_text = fake_version
        events.clear()
        view = await core.call("settings.set", {"key": "droidPath", "value": str(exe)})
        assert core._droid_version == "", "the cached version is forgotten the moment the path changes"
        assert view["droidFound"].endswith("droid.exe")
        await until(lambda: any(p.get("factory", {}).get("version") == "9.9.9-test" for p in events.of("accounts")))
        await core.shutdown()

    run(scenario())


def test_removing_the_provider_behind_the_default_model_drops_the_default(tmp_path):
    from droid_sdk import ModelInfo, ModelProvider

    from desktop.core.providers import ProviderStore

    async def scenario():
        store = ProviderStore(
            factory_settings=tmp_path / "factory.json", sidecar=tmp_path / "providers.json",
            environ={"OPENROUTER_API_KEY": "sentinel"}, registry_env=lambda: {},
        )
        core, factory, events, _, _ = make_core_with(tmp_path, store)
        await core.call("app.init")
        await until(lambda: bool(events.of("models")))
        saved = await core.call("providers.save", {"draft": provider_draft()})
        (custom_id, _) = store.custom_model_ids(saved["id"])[0]
        factory.models.append(
            ModelInfo(id=custom_id, display_name="GPT-OSS 120B", short_display_name="GPT-OSS", model_provider=ModelProvider.GENERIC_CHAT_COMPLETION_API,
                      supported_reasoning_efforts=(), default_reasoning_effort="medium", is_custom=True)
        )
        await until(lambda: any(m["source"] == "api:p1" for p in events.of("models") for m in p["models"]))
        page_model = f"api:p1:{custom_id}"
        assert (await core.call("session.choose", {"field": "model", "value": page_model})) == {"ok": True}
        assert core.settings.default_model == page_model and core.defaults()["model"] == page_model

        seen = len(events.of("models"))
        await core.call("providers.remove", {"id": "p1"})
        assert core.settings.default_model == "", "the default went with its provider"
        factory.models.pop()  # droid no longer lists the custom model either
        await until(lambda: len(events.of("models")) > seen)
        assert events.last("models")["defaults"]["model"] == "claude-haiku-5-5", "the page falls back to a factory model"
        assert core.defaults()["model"] == "claude-haiku-5-5"
        await core.shutdown()

    run(scenario())


# --------------------------------------------------------------------------- fixes after the cross-reviews (2026-10-30)


def png_upload(name, data=None, kind="image/png"):
    return {"name": name, "type": kind, "data": base64.b64encode(data if data is not None else tiny_png()).decode()}


def test_a_failed_send_puts_every_attachment_back_even_when_the_box_was_refilled_meanwhile(tmp_path):
    from tests.support import failure

    async def scenario():
        core, factory, events, _, _ = await ready_core(tmp_path)
        cid = await new_session(core, events, tmp_path / "proj")
        droid = factory.sessions[-1]
        first = await core.call("attachments.add", {"id": cid, "files": [png_upload("original.png", tiny_png(7, 7))]})
        await core.call("session.send", {"id": cid, "text": "look", "attachmentIds": [first["added"][0]["id"]]})
        await until(lambda: droid.active is not None)
        # while it runs, the box takes ten more files (what a message usually holds)
        many = await core.call("attachments.add", {"id": cid, "files": [png_upload(f"n{i}.png", tiny_png(5 + i, 5, (i, 1, 1))) for i in range(10)]})
        assert len(many["added"]) == 10
        events.clear()
        droid.active.push(failure("overloaded"))
        droid.active.end()
        await idle(core, cid)
        await until(lambda: events.of("restore"))
        restored = events.of("restore")[-1]
        names = [a["name"] for a in restored["attachments"]]
        assert restored["text"] == "look" and len(names) == 11 and names[0] == "original.png", "the original is not left behind"
        notice = [n["text"] for n in events.of("notice")][-1]
        assert "放回输入框" in notice and "比一条消息通常带的量多 1 个文件" in notice and "重试时会让你确认" in notice, "and the user is told by how much"

        # the retry is asked about once, and goes with all eleven when the user says so: none is dropped
        asked = await core.call("session.send", {"id": cid, "text": "look"})
        assert asked["ok"] is False and asked["over"]["files"] == 1 and len(droid.streams) == 1
        await core.call("session.send", {"id": cid, "text": "look", "confirmOver": True})
        await until(lambda: len(droid.streams) == 2 and droid.active is not None)
        assert len(droid.streams[-1].images) == 11
        droid.active.push(success("ok"))
        droid.active.end()
        await core.shutdown()

    run(scenario())


def test_a_failed_send_with_room_in_the_box_restores_quietly(tmp_path):
    from tests.support import failure

    async def scenario():
        core, factory, events, _, _ = await ready_core(tmp_path)
        cid = await new_session(core, events, tmp_path / "proj")
        droid = factory.sessions[-1]
        added = await core.call("attachments.add", {"id": cid, "files": [png_upload("a.png")]})
        await core.call("session.send", {"id": cid, "text": "look", "attachmentIds": [added["added"][0]["id"]]})
        await until(lambda: droid.active is not None)
        events.clear()
        droid.active.push(failure("overloaded"))
        droid.active.end()
        await idle(core, cid)
        await until(lambda: events.of("restore"))
        assert [a["name"] for a in events.of("restore")[-1]["attachments"]] == ["a.png"]
        assert [n["text"] for n in events.of("notice")][-1] == "这条消息没有发送成功：附件已放回输入框，可以直接重试。"
        await core.shutdown()

    run(scenario())


def test_two_conversations_keep_their_own_thumbnails_and_an_entry_goes_with_its_attachment(tmp_path):
    import gc
    import hashlib

    from desktop.core.attachments import Thumbnail

    async def scenario():
        core, factory, events, platform, _ = await ready_core(tmp_path)
        platform.thumbnail = lambda a: Thumbnail("data:image/png;base64," + hashlib.sha1(a.data).hexdigest()[:16], 4, 3) if a.kind == "image" else None
        cid1 = await new_session(core, events, tmp_path / "proj")
        await send_with_files(core, factory, events, cid1, "A", [("a.png", "image/png", tiny_png(4, 3, (255, 0, 0)))])
        cid2 = await new_session(core, events, tmp_path / "proj")
        await send_with_files(core, factory, events, cid2, "B", [("b.png", "image/png", tiny_png(4, 3, (0, 0, 255)))])
        await asyncio.sleep(0.2)

        def pictures(cid):
            return [(a["id"], a["url"]) for p in events.of("turns") if p["id"] == cid for t in p["turns"] for b in t["blocks"] for a in b.get("attachments", [])]

        (id1, url1), (id2, url2) = pictures(cid1)[-1], pictures(cid2)[-1]
        assert id1 == id2, "every transcript numbers its attachments from the same start: the ids alone cannot tell them apart"
        assert url1 != url2, "each conversation shows its own picture"

        # a thumbnail goes with its attachment: a draft that is removed leaves nothing behind
        before = len(core._thumbs)
        added = await core.call("attachments.add", {"id": cid1, "files": [png_upload("draft.png", tiny_png(9, 9, (1, 2, 3)))]})
        assert len(core._thumbs) == before + 1
        await core.call("attachments.remove", {"id": cid1, "attachmentId": added["added"][0]["id"]})
        gc.collect()
        assert len(core._thumbs) == before
        await core.shutdown()

    run(scenario())


def test_closing_a_conversation_lets_its_pictures_go(tmp_path):
    import gc

    async def scenario():
        core, factory, events, _, _ = await ready_core(tmp_path)
        cid = await new_session(core, events, tmp_path / "proj")
        await send_with_files(core, factory, events, cid, "A", [("a.png", "image/png", tiny_png(4, 3, (255, 0, 0)))])
        await asyncio.sleep(0.2)
        assert len(core._thumbs) >= 1
        await core.call("session.close", {"id": cid})
        await until(lambda: factory.sessions[-1].closed >= 1)
        factory.sessions.clear()  # the fake factory keeps every session it made (the real one does not)
        for _ in range(5):
            await asyncio.sleep(0.05)
            gc.collect()
        assert len(core._thumbs) == 0 and len(core._retired) == 0, "a closed conversation is not kept (its thumbnails, its transcript)"
        await core.shutdown()

    run(scenario())


def test_without_the_own_loop_an_api_models_id_is_translated_for_droid_even_before_the_model_list_has_loaded(tmp_path):
    async def scenario():
        core, factory, events, platform, _ = make_core(tmp_path)  # no own loop (chatgpt=None), no app.init: the catalog is still empty
        core.settings.default_model = "api:p123:custom:My-Model-0"
        await core.call("session.new", {"projectId": str((tmp_path / "proj").resolve())})
        assert factory.calls[-1][1]["model"] == "custom:My-Model-0", "droid replaces an id it does not know with another model"
        await core.shutdown()

    run(scenario())


def test_an_api_models_context_window_comes_from_its_provider_into_the_model_list(tmp_path):
    import json

    from droid_sdk import ModelInfo, ModelProvider

    from desktop.core.providers import ProviderStore

    async def scenario():
        factory_settings = tmp_path / "factory.json"
        store = ProviderStore(factory_settings=factory_settings, sidecar=tmp_path / "providers.json", environ={"OPENROUTER_API_KEY": "x"}, registry_env=lambda: {})
        core, factory, events, _, _ = make_core_with(tmp_path, store)
        await core.call("app.init")
        await until(lambda: bool(events.of("models")))
        draft = provider_draft(models=[{"id": "m/with-window", "displayName": "Windowed", "reasoning": "", "images": False, "contextWindow": 64000},
                                       {"id": "m/without", "displayName": "Plain", "reasoning": "", "images": False}])
        await core.call("providers.save", {"draft": draft})
        for custom_id, _native in store.custom_model_ids("p1"):
            name = "Windowed" if "Windowed" in custom_id else "Plain"
            factory.models.append(ModelInfo(id=custom_id, display_name=name, short_display_name=name, model_provider=ModelProvider.GENERIC_CHAT_COMPLETION_API,
                                            supported_reasoning_efforts=(), default_reasoning_effort="medium", is_custom=True))
        events.clear()
        core._schedule_models()
        await until(lambda: bool(events.of("models")))
        windows = {m["label"]: m["window"] for m in events.last("models")["models"] if m["source"] == "api:p1"}
        assert windows == {"Windowed": 64000, "Plain": 0}
        await core.shutdown()

    run(scenario())


def test_the_attachment_ids_of_a_send_say_which_drafts_go_an_empty_list_is_none_of_them(tmp_path):
    async def scenario():
        core, factory, events, _, _ = await ready_core(tmp_path)
        cid = await new_session(core, events, tmp_path / "proj")
        droid = factory.sessions[-1]
        await core.call("attachments.add", {"id": cid, "files": [png_upload("draft.png")]})
        await core.call("session.send", {"id": cid, "text": "words only", "attachmentIds": []})
        await until(lambda: droid.active is not None)
        assert droid.active.images == (), "an empty list is none of the drafts"
        assert len(core._by_cid[cid].drafts) == 1, "and the draft is still in the box"
        droid.active.push(success("ok"))
        droid.active.end()
        await idle(core, cid)
        with pytest.raises(AppError, match="附件列表不合法"):
            await core.call("session.send", {"id": cid, "text": "x", "attachmentIds": "d1"})
        await core.call("session.send", {"id": cid, "text": "with it"})
        await until(lambda: len(droid.streams) == 2 and droid.active is not None)
        assert len(droid.active.images) == 1, "no list at all: every draft"
        droid.active.push(success("ok"))
        droid.active.end()
        await core.shutdown()

    run(scenario())


def test_changing_engine_never_closes_another_conversation_even_above_the_limit(tmp_path):
    async def scenario():
        core, factory, events, _, codex = await ready_core(tmp_path, codex=True, max_open=4)
        a = await new_session(core, events, tmp_path / "proj")
        da = factory.sessions[-1]
        b = await new_session(core, events, tmp_path / "proj")
        assert (await core.call("session.choose", {"id": b, "field": "model", "value": "codex:gpt-5.3-codex"}))["switched"]
        await core.call("settings.set", {"key": "maxOpen", "value": 1})  # two conversations are open: above the limit now
        result = await core.call("session.choose", {"id": b, "field": "model", "value": "claude-haiku-5-5"})
        await asyncio.sleep(0.3)
        assert result == {"ok": True, "switched": True}
        assert da.closed == 0 and [c.cid for c in core.order] == [a, b], "a change of engine is no new session: nothing else is closed"
        assert core._by_cid[b].controller.engine == "droid"

        # the limit still holds for a new session: it makes room
        await core.call("session.new", {"projectId": str((tmp_path / "proj").resolve()), "modelId": "claude-haiku-5-5"})
        assert len(core.order) == 1
        await core.shutdown()

    run(scenario())


def test_a_new_codex_path_refreshes_the_account_and_the_models(tmp_path):
    async def scenario():
        core, factory, events, _, codex = await ready_core(tmp_path, codex=True)
        events.clear()
        await core.call("settings.set", {"key": "codexPath", "value": r"D:\tools\codex.exe"})
        await until(lambda: codex.path_changes == 1 and events.of("accounts") and events.of("models"))
        assert "cli" in events.of("accounts")[-1]["codex"]

        await core.call("settings.set", {"key": "codexPath", "value": r"D:\tools\codex.exe"})
        await asyncio.sleep(0.1)
        assert codex.path_changes == 1, "the same path again changes nothing"

        codex.keep_server = True  # a session is using the old app-server
        events.clear()
        await core.call("settings.set", {"key": "codexPath", "value": ""})
        await until(lambda: any("原来的程序" in n["text"] for n in events.of("notice")))
        assert codex.path_changes == 2
        await core.shutdown()

    run(scenario())


def test_the_message_box_takes_its_share_when_a_pick_is_counted(tmp_path):
    async def scenario():
        core, factory, events, _, codex = await ready_core(tmp_path, codex=True)
        cid = await new_session(core, events, tmp_path / "proj")
        await send_with_files(
            core, factory, events, cid, "ten",
            [(f"p{i}.png", "image/png", tiny_png(4 + i, 3, (1, i, 9))) for i in range(10)],
        )
        early = (await core.call("attachments.add", {"id": cid, "files": [png_upload("early.png", tiny_png(9, 9))]}))["added"]
        asked = await ask_to_switch(core, cid)
        assert asked["choose"]["drafts"] == {"count": 1, "bytes": early[0]["size"]}, "the question says what the box already holds"
        ids = [item["id"] for item in asked["choose"]["attachments"]]
        before = len(codex.opened)
        with pytest.raises(AppError, match="输入框里已有 1 个附件|这条消息有 11 个附件"):
            await ask_to_switch(core, cid, attachments=ids)
        assert len(codex.opened) == before, "ten pictures and one in the box is eleven"
        await ask_to_switch(core, cid, attachments=ids[:9])
        assert len(codex.opened[-1]["handoff"].attachments) == 9, "nine and one are ten: what a message usually holds, no question"
        await core.shutdown()

    run(scenario())


def test_a_pick_past_the_safety_ceiling_is_refused_even_when_the_user_says_yes(tmp_path, monkeypatch):
    from desktop.core import attachments as attachments_module

    async def scenario():
        core, factory, events, _, codex = await ready_core(tmp_path, codex=True)
        cid = await new_session(core, events, tmp_path / "proj")
        await send_with_files(
            core, factory, events, cid, "twelve",
            [(f"p{i}.png", "image/png", tiny_png(4 + i, 3, (1, i, 9))) for i in range(6)],
        )
        await send_with_files(
            core, factory, events, cid, "more",
            [(f"q{i}.png", "image/png", tiny_png(4 + i, 3, (2, i, 9))) for i in range(6)],
        )
        asked = await ask_to_switch(core, cid)
        ids = [item["id"] for item in asked["choose"]["attachments"]]
        monkeypatch.setattr(attachments_module, "HARD_ATTACHMENTS", 11)
        before = len(codex.opened)
        with pytest.raises(AppError, match="附件太多了：一条消息最多 11 个.*现在有 12 个.*请分几条消息发"):
            await ask_to_switch(core, cid, attachments=ids, overLimit=True)
        assert len(codex.opened) == before
        await core.shutdown()

    run(scenario())


def test_adding_past_what_a_message_usually_holds_is_allowed_and_said_and_only_the_ceiling_refuses(tmp_path, monkeypatch):
    from desktop.core import attachments as attachments_module

    async def scenario():
        core, factory, events, _, _ = await ready_core(tmp_path)
        cid = await new_session(core, events, tmp_path / "proj")
        monkeypatch.setattr(attachments_module, "HARD_ATTACHMENTS", 12)
        ten = await core.call("attachments.add", {"id": cid, "files": [png_upload(f"a{i}.png", tiny_png(5 + i, 5, (i, 1, 1))) for i in range(10)]})
        assert len(ten["added"]) == 10 and ten["over"] == "" and ten["rejected"] == "", "ten: nothing to say"
        two_more = await core.call("attachments.add", {"id": cid, "files": [png_upload(f"b{i}.png", tiny_png(5 + i, 6, (i, 2, 2))) for i in range(2)]})
        assert len(two_more["added"]) == 2 and two_more["rejected"] == "", "twelve are taken"
        assert two_more["over"].startswith("现在比一条消息通常带的量多 2 个文件") and two_more["over"].endswith("发送时会让你确认。")
        refused = await core.call("attachments.add", {"id": cid, "files": [png_upload("c.png", tiny_png(9, 9, (3, 3, 3)))]})
        assert refused["added"] == [] and "最多附加 12" in refused["rejected"], "only the safety ceiling refuses"
        await core.shutdown()

    run(scenario())


def test_a_message_over_what_is_usual_asks_once_and_goes_when_confirmed(tmp_path):
    async def scenario():
        core, factory, events, _, _ = await ready_core(tmp_path)
        cid = await new_session(core, events, tmp_path / "proj")
        droid = factory.sessions[-1]
        await core.call("attachments.add", {"id": cid, "files": [png_upload(f"a{i}.png", tiny_png(5 + i, 5, (i, 1, 1))) for i in range(11)]})
        asked = await core.call("session.send", {"id": cid, "text": "look at all"})
        assert asked == {
            "ok": False,
            "over": {"count": 11, "total": asked["over"]["total"], "files": 1, "bytes": 0, "text": asked["over"]["text"]},
        }
        assert asked["over"]["text"].startswith("比一条消息通常带的量多 1 个文件（这条消息有 11 个附件、")
        assert droid.streams == [] and len(core._by_cid[cid].drafts) == 11, "asking sends nothing and takes nothing out of the box"
        with pytest.raises(AppError, match="确认标记不合法"):
            await core.call("session.send", {"id": cid, "text": "look at all", "confirmOver": "yes"})
        again = await core.call("session.send", {"id": cid, "text": "look at all", "confirmOver": False})
        assert again["ok"] is False, "no is not yes"
        sent = await core.call("session.send", {"id": cid, "text": "look at all", "confirmOver": True})
        assert sent == {"ok": True}
        await until(lambda: droid.active is not None)
        assert len(droid.active.images) == 11 and core._by_cid[cid].drafts == []
        droid.active.push(success("ok"))
        droid.active.end()
        await core.shutdown()

    run(scenario())


def test_a_message_within_what_is_usual_is_not_asked_about(tmp_path):
    async def scenario():
        core, factory, events, _, _ = await ready_core(tmp_path)
        cid = await new_session(core, events, tmp_path / "proj")
        droid = factory.sessions[-1]
        await core.call("attachments.add", {"id": cid, "files": [png_upload(f"a{i}.png", tiny_png(5 + i, 5, (i, 1, 1))) for i in range(10)]})
        assert (await core.call("session.send", {"id": cid, "text": "ten"})) == {"ok": True}
        await until(lambda: droid.active is not None)
        assert len(droid.active.images) == 10
        droid.active.push(success("ok"))
        droid.active.end()
        await core.shutdown()

    run(scenario())


def test_a_send_past_the_safety_ceiling_is_refused_in_words(tmp_path, monkeypatch):
    from desktop.core import attachments as attachments_module

    async def scenario():
        core, factory, events, _, _ = await ready_core(tmp_path)
        cid = await new_session(core, events, tmp_path / "proj")
        await core.call("attachments.add", {"id": cid, "files": [png_upload(f"a{i}.png", tiny_png(5 + i, 5, (i, 1, 1))) for i in range(12)]})
        monkeypatch.setattr(attachments_module, "HARD_ATTACHMENTS", 11)
        with pytest.raises(AppError, match="附件太多了：一条消息最多 11 个.*现在有 12 个.*请分几条消息发"):
            await core.call("session.send", {"id": cid, "text": "x", "confirmOver": True})
        await core.shutdown()

    run(scenario())
