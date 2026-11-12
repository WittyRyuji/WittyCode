"""The application core behind the page, driven without a window: the own loop signed in to the loopback ChatGPT Plan fake
(and the fake Devin for engine changes), a fake platform, an event log."""

from __future__ import annotations

import asyncio
import base64
import time

import pytest

from desktop.core.app_core import AppError
from desktop.core.session_state import SessionLimitError, SessionState
from tests.app_support import MODEL, OTHER_MODEL, ready_core, save_conversation
from tests.devin_support import make_engine as make_devin
from tests.fake_responses import call, fail, say
from tests.fakes import until
from tests.support import tiny_png


def run(coro):
    return asyncio.run(coro)


def sessions_of(events):
    """What the page knows of each session: the patches merged in the order they were sent."""
    merged: dict[str, dict] = {}
    for payload in events.of("session"):
        merged.setdefault(payload["id"], {}).update(payload["patch"])
    return merged


def last_turn(events):
    return [payload for payload in events.of("turns") if payload["turns"]][-1]["turns"][-1]


async def idle(core, cid):
    """Until the session has finished whatever it was doing (a turn that was just ended included)."""
    await until(lambda: core._by_cid[cid].controller.state is SessionState.READY and core._by_cid[cid].controller.transcript.active_turn is None, 10)


async def new_session(core, events, project, **more):
    result = await core.call("session.new", {"projectId": str(project), **more})
    cid = result["id"]
    await until(lambda: any(p["id"] == cid and p["patch"].get("mark") == "ready" for p in events.of("session")))
    return cid


async def talk(core, plan, cid, text, answer="ok"):
    """One finished turn: ``text`` is sent, the model answers ``answer``."""
    plan.script(say(answer))
    turns = len(core._by_cid[cid].controller.transcript.turns)
    await core.call("session.send", {"id": cid, "text": text})
    await until(lambda: len(core._by_cid[cid].controller.transcript.turns) == turns + 1)
    await idle(core, cid)


def user_parts(request):
    messages = [item for item in request["input"] if item.get("type") == "message" and item.get("role") == "user"]
    return messages[-1]["content"]


def png_upload(name, data=None, kind="image/png"):
    return {"name": name, "type": kind, "data": base64.b64encode(data if data is not None else tiny_png()).decode()}


def pdf_bytes(words: str = "") -> bytes:
    """A PDF for the fake platform: the words after TEXT: are what 'parsing' finds; none: a scan."""
    return b"%PDF-1.4 " + (b"TEXT:" + words.encode() if words else b"")


async def devin_core(tmp_path, *, wait_models=True, **options):
    env = {key: options.pop(key) for key in list(options) if key.startswith("FAKE_DEVIN_")}
    harness = make_devin(tmp_path, **env)
    core, plan, events, platform, _ = await ready_core(tmp_path, devin_engine=harness.engine, **options)
    harness.engine.settings = core.settings
    if wait_models:
        await until(lambda: any(m["source"] == "devin" for p in events.of("models") for m in p["models"]), 10)
    return core, plan, events, platform, harness


# --------------------------------------------------------------------------- the start, a session, a turn


def test_init_returns_the_projects_defaults_and_does_not_wait_for_the_slow_parts(tmp_path):
    async def scenario():
        core, _, events, _, _ = await ready_core(tmp_path, projects=True)
        init = await core.call("app.init")
        assert init["version"] == 1 and init["tabs"] == []
        (project,) = init["projects"]
        assert project["path"] == str((tmp_path / "proj").resolve()) and project["name"] == "proj"
        assert init["defaults"] == {"model": MODEL, "reasoning": "medium", "autonomy": "code", "mode": "auto"}
        assert init["devin"]["cli"] == "missing" and init["settings"]["maxOpen"] == 4
        assert "factory" not in init and "codex" not in init, "no other program's account"
        models = events.last("models")["models"]
        sol = next(m for m in models if m["id"] == MODEL)
        assert sol["label"] == "GPT-6.1-Sol" and sol["source"] == "chatgpt"
        assert sol["efforts"] == ["low", "medium", "high"] and sol["defaultEffort"] == "medium"
        await core.shutdown()

    run(scenario())


def test_a_new_session_appears_as_a_tab_then_becomes_ready(tmp_path):
    async def scenario():
        core, _, events, _, _ = await ready_core(tmp_path)
        project = (tmp_path / "proj").resolve()
        result = await core.call("session.new", {"projectId": str(project)})
        cid = result["id"]
        tabs = events.of("tabs")
        assert tabs[0]["tabs"] == [cid] and tabs[0]["select"] == cid
        await until(lambda: sessions_of(events).get(cid, {}).get("mark") == "ready")
        patch = sessions_of(events)[cid]
        assert patch["projectId"] == str(project) and patch["engine"] == "chatgpt" and patch["nativeId"] == core._by_cid[cid].controller.session_id
        assert patch["settings"] == {"model": MODEL, "reasoning": "medium", "autonomy": "code", "mode": "auto"}
        assert patch["workingLabel"] == "就绪" and patch["handoff"] is None
        await core.shutdown()

    run(scenario())


def test_a_turn_streams_to_the_page_in_changed_pieces(tmp_path):
    async def scenario():
        core, plan, events, _, _ = await ready_core(tmp_path)
        cid = await new_session(core, events, tmp_path / "proj")
        plan.script([("hold", "t"), *say("Hi there")])
        events.clear()
        await core.call("session.send", {"id": cid, "text": "hello"})
        await until(lambda: plan.requests and any(p["turns"] for p in events.of("turns")))
        first = [p for p in events.of("turns") if p["turns"]][0]
        assert first["all"] is False and first["turns"][0]["status"] == "running"
        assert first["turns"][0]["blocks"][0] == {"kind": "user", "id": first["turns"][0]["blocks"][0]["id"], "text": "hello"}
        await until(lambda: sessions_of(events)[cid].get("mark") == "running")
        plan.release("t")
        await until(lambda: sessions_of(events)[cid].get("mark") == "ready")
        last = last_turn(events)
        assert last["status"] == "done" and last["blocks"][-1]["markdown"] == "Hi there"
        count = len(events.of("turns"))
        await asyncio.sleep(0.15)
        assert len(events.of("turns")) == count, "nothing changed, nothing is sent again"
        await core.shutdown()

    run(scenario())


def test_the_pending_prompt_marks_the_session_as_waiting_and_can_be_answered(tmp_path):
    async def scenario():
        core, plan, events, _, _ = await ready_core(tmp_path)
        plan.script(call("shell", {"command": "New-Item marker.txt"}, call_id="c1"), say("made"))
        cid = await new_session(core, events, tmp_path / "proj", autonomy="ask")
        await core.call("session.send", {"id": cid, "text": "go"})
        await until(lambda: sessions_of(events).get(cid, {}).get("mark") == "attention", 10)
        (prompt,) = last_turn(events)["prompts"]
        assert prompt["kind"] == "permission" and prompt["status"] == "pending"
        assert not (tmp_path / "proj" / "marker.txt").exists(), "nothing ran before the answer"
        answered = await core.call("session.answerPermission", {"id": cid, "promptId": prompt["id"], "value": "proceed_once"})
        assert answered == {"ok": True}
        again = await core.call("session.answerPermission", {"id": cid, "promptId": prompt["id"], "value": "proceed_once"})
        assert again == {"ok": False}, "a late click is refused, not an error"
        await idle(core, cid)
        assert (tmp_path / "proj" / "marker.txt").exists()
        await core.shutdown()

    run(scenario())


def test_stop_interrupts_and_close_removes_the_tab(tmp_path):
    async def scenario():
        core, plan, events, _, _ = await ready_core(tmp_path)
        cid = await new_session(core, events, tmp_path / "proj")
        plan.script([("hold", "t"), *say("late")])
        await core.call("session.send", {"id": cid, "text": "go"})
        await until(lambda: plan.requests)
        await core.call("session.stop", {"id": cid})
        await until(lambda: events.of("turns") and last_turn(events)["status"] == "interrupted", 10)
        await until(lambda: sessions_of(events)[cid].get("mark") == "ready")
        plan.release("t")
        await core.call("session.close", {"id": cid})
        assert events.last("tabs")["tabs"] == []
        with pytest.raises(AppError):
            await core.call("session.send", {"id": cid, "text": "x"})
        await core.shutdown()

    run(scenario())


# --------------------------------------------------------------------------- the sidebar


async def saved(core, events, tmp_path, *conversations):
    """A project whose own-loop conversations were saved (id, title, minutes ago); returns the folder and the sidebar."""
    proj = tmp_path / "proj"
    proj.mkdir(exist_ok=True)
    for session_id, title, minutes in conversations:
        save_conversation(core.chatgpt, proj, session_id, title, minutes_ago=minutes)
    await core.call("project.add", {"path": str(proj)})
    await until(lambda: events.of("sidebar") and len(events.last("sidebar")["sessions"]) == len(conversations))
    return proj, events.last("sidebar")


async def three_saved(core, events, tmp_path):
    return await saved(core, events, tmp_path, ("old", "Older", 180), ("mid", "Middle", 60), ("new", "Newer", 5))


def listed(events):
    return events.last("sidebar")["projects"][0]["sessionIds"]


def test_the_sidebar_lists_saved_sessions_and_shows_an_open_one_once(tmp_path):
    async def scenario():
        core, _, events, _, _ = await ready_core(tmp_path, projects=False)
        proj, side = await saved(core, events, tmp_path, ("old", "Older", 120), ("new", "Newer", 5))
        (project,) = side["projects"]
        assert project["sessionIds"] == ["cg:new", "cg:old"], "newest first"
        row = side["sessions"]["cg:new"]
        assert row["title"] == "Newer" and row["modified"] == "5 分钟前" and row["engine"] == "chatgpt" and row["settings"]["model"] == MODEL
        cid = (await core.call("session.openSaved", {"id": "cg:new", "projectId": str(proj), "title": "Newer"}))["id"]
        await until(lambda: sessions_of(events).get(cid, {}).get("mark") == "ready")
        core._emit_sidebar()
        assert listed(events) == [cid, "cg:old"], "the open one replaces its saved row"
        again = await core.call("session.openSaved", {"id": "cg:new", "projectId": str(proj)})
        assert again["id"] == cid and len(core.order) == 1
        await core.shutdown()

    run(scenario())


def test_a_session_is_archived_listed_apart_and_brought_back(tmp_path):
    async def scenario():
        core, _, events, _, _ = await ready_core(tmp_path, projects=False)
        proj, side = await three_saved(core, events, tmp_path)
        assert listed(events) == ["cg:new", "cg:mid", "cg:old"] and side["archived"] == []
        before = int(time.time() * 1000)
        assert await core.call("session.archive", {"id": "cg:mid"}) == {"ok": True}
        side = events.last("sidebar")
        assert listed(events) == ["cg:new", "cg:old"] and "cg:mid" not in side["sessions"]
        assert [row["id"] for row in side["archived"]] == ["cg:mid"] and side["archived"][0]["title"] == "Middle"
        assert core.settings.archived_sessions["cg:mid"] >= before, "when it was archived, in ms"
        assert [row["id"] for row in (await core.call("app.init"))["archived"]] == ["cg:mid"], "a reloaded page is told too"
        await core.call("session.archive", {"id": "cg:old"})
        assert [row["id"] for row in events.last("sidebar")["archived"]] == ["cg:old", "cg:mid"], "the one archived last first"
        await core._reload_project(str(proj.resolve()))
        core._emit_sidebar()
        assert listed(events) == ["cg:new"], "a fresh listing from the engine does not bring archived ones back"
        assert await core.call("session.unarchive", {"id": "cg:mid"}) == {"ok": True}
        assert listed(events) == ["cg:new", "cg:mid"] and [row["id"] for row in events.last("sidebar")["archived"]] == ["cg:old"]
        assert await core.call("session.unarchive", {"id": "cg:mid"}) == {"ok": False}, "it is not archived any more"
        await core.shutdown()

    run(scenario())


def test_an_open_conversation_is_closed_when_it_is_archived_and_a_running_one_is_not_archived(tmp_path):
    async def scenario():
        core, plan, events, _, _ = await ready_core(tmp_path, projects=False)
        proj, _ = await three_saved(core, events, tmp_path)
        cid = (await core.call("session.openSaved", {"id": "cg:new", "projectId": str(proj), "title": "Newer"}))["id"]
        await until(lambda: sessions_of(events).get(cid, {}).get("mark") == "ready")
        plan.script([("hold", "t"), *say("late")])
        await core.call("session.send", {"id": cid, "text": "go"})
        await until(lambda: plan.requests)
        with pytest.raises(AppError, match="还在运行"):
            await core.call("session.archive", {"id": cid})
        assert cid in core._by_cid and "cg:new" not in core.settings.archived_sessions
        await core.call("session.stop", {"id": cid})
        plan.release("t")
        await idle(core, cid)
        assert await core.call("session.archive", {"id": cid}) == {"ok": True}
        assert cid not in core._by_cid and events.last("tabs")["tabs"] == []
        assert "cg:new" in core.settings.archived_sessions
        await core.shutdown()

    run(scenario())


def test_a_conversation_just_started_can_be_archived_before_the_list_has_heard_of_it(tmp_path):
    async def scenario():
        core, plan, events, _, _ = await ready_core(tmp_path)
        cid = await new_session(core, events, tmp_path / "proj")
        await talk(core, plan, cid, "go", "好")
        native = core._by_cid[cid].controller.session_id
        assert not any(item.native == native for rows in core._saved.values() for item in rows), "the engine's list does not have it yet"
        assert await core.call("session.archive", {"id": cid}) == {"ok": True}
        assert f"cg:{native}" in core.settings.archived_sessions and cid not in core._by_cid
        await core.shutdown()

    run(scenario())


def test_a_conversation_the_engine_has_not_named_yet_is_not_archived_or_renamed(tmp_path):
    async def scenario():
        core, plan, events, _, _ = await ready_core(tmp_path)
        cid = await new_session(core, events, tmp_path / "proj")
        await talk(core, plan, cid, "go", "好")
        core._by_cid[cid].controller.session_id = None
        for method, params in (("session.archive", {"id": cid}), ("session.rename", {"id": cid, "title": "x"})):
            with pytest.raises(AppError, match="还没有内容"):
                await core.call(method, params)
        assert core.settings.archived_sessions == {} and core.settings.session_titles == {} and cid in core._by_cid
        await core.shutdown()

    run(scenario())


def test_a_conversation_with_nothing_in_it_is_closed_not_archived_or_renamed(tmp_path):
    async def scenario():
        core, _, events, _, _ = await ready_core(tmp_path)
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
        core, _, events, _, _ = await ready_core(tmp_path, projects=False)
        await three_saved(core, events, tmp_path)
        for method, params in (
            ("session.archive", {"id": "nope"}), ("session.archive", {"id": ""}), ("session.archive", {"id": 5}), ("session.archive", {}),
            ("session.archive", {"id": ["cg:new"]}), ("session.rename", {"id": {"a": 1}, "title": "x"}),
            ("session.unarchive", {"id": "nope"}), ("session.rename", {"id": "nope", "title": "x"}), ("session.rename", {"id": "cg:new", "title": 5}),
            ("session.rename", {"id": "cg:new"}),
        ):
            with pytest.raises(AppError):
                await core.call(method, params)
        assert core.settings.archived_sessions == {} and core.settings.session_titles == {}
        await core.shutdown()

    run(scenario())


def test_a_session_is_renamed_here_without_touching_the_engines_title(tmp_path):
    async def scenario():
        core, _, events, _, _ = await ready_core(tmp_path, projects=False)
        proj, _ = await three_saved(core, events, tmp_path)
        assert await core.call("session.rename", {"id": "cg:new", "title": "  新的\n  名字  "}) == {"ok": True, "title": "新的 名字"}
        assert events.last("sidebar")["sessions"]["cg:new"]["title"] == "新的 名字" and core.settings.session_titles == {"cg:new": "新的 名字"}
        assert events.last("sidebar")["sessions"]["cg:mid"]["title"] == "Middle", "only that one"
        cid = (await core.call("session.openSaved", {"id": "cg:new", "projectId": str(proj), "title": "Newer"}))["id"]
        await until(lambda: sessions_of(events).get(cid, {}).get("mark") == "ready")
        assert sessions_of(events)[cid]["title"] == "新的 名字", "the open conversation carries the name too"
        long = await core.call("session.rename", {"id": cid, "title": "长" * 500})
        assert len(long["title"]) == 120
        await until(lambda: len(sessions_of(events)[cid]["title"]) == 120)
        assert await core.call("session.rename", {"id": "cg:new", "title": "   "}) == {"ok": True, "title": ""}
        assert core.settings.session_titles == {} and core.chatgpt.sessions.load("new").title == "Newer", "the engine's own title was never touched"
        await core.shutdown()

    run(scenario())


def test_the_project_of_the_conversation_started_last_is_remembered(tmp_path):
    async def scenario():
        core, _, events, _, _ = await ready_core(tmp_path)
        assert core.settings.last_project == "" and (await core.call("app.init"))["settings"]["lastProject"] == ""
        await new_session(core, events, tmp_path / "proj")
        assert core.settings.last_project == str((tmp_path / "proj").resolve())
        assert (await core.call("app.init"))["settings"]["lastProject"] == core.settings.last_project
        await core.shutdown()

    run(scenario())


def test_opening_a_saved_session_keeps_its_place_and_its_time_in_the_sidebar(tmp_path):
    async def scenario():
        core, _, events, _, _ = await ready_core(tmp_path, projects=False)
        proj, before = await three_saved(core, events, tmp_path)
        old = before["sessions"]["cg:old"]
        cid = (await core.call("session.openSaved", {"id": "cg:old", "projectId": str(proj), "title": "Older"}))["id"]
        await until(lambda: sessions_of(events).get(cid, {}).get("mark") == "ready")
        core._emit_sidebar()
        assert listed(events) == ["cg:new", "cg:mid", cid], "clicking an old session does not float it to the top"
        meta = sessions_of(events)[cid]
        assert meta["modifiedAt"] == old["modifiedAt"] and meta["modified"] == old["modified"] != "刚刚"

        mid = (await core.call("session.openSaved", {"id": "cg:mid", "projectId": str(proj)}))["id"]
        await until(lambda: sessions_of(events).get(mid, {}).get("mark") == "ready")
        await core.call("session.openSaved", {"id": "cg:old", "projectId": str(proj)})
        core._emit_sidebar()
        assert listed(events) == ["cg:new", mid, cid]
        await core.shutdown()

    run(scenario())


def test_a_session_whose_row_is_not_in_the_list_still_opens_and_counts_from_now(tmp_path):
    async def scenario():
        core, _, events, _, _ = await ready_core(tmp_path, projects=False)
        proj, _ = await three_saved(core, events, tmp_path)
        core._saved.clear()  # nothing known of it: what the page shows always comes from this list, so only a row that vanished gets here
        cid = (await core.call("session.openSaved", {"id": "cg:old", "projectId": str(proj)}))["id"]
        await until(lambda: sessions_of(events).get(cid, {}).get("mark") == "ready")
        assert core._by_cid[cid].controller.active_at > time.time() - 60
        await core.shutdown()

    run(scenario())


def test_messages_and_answers_update_the_time_without_moving_the_session(tmp_path):
    async def scenario():
        core, plan, events, _, _ = await ready_core(tmp_path, projects=False)
        proj, before = await three_saved(core, events, tmp_path)
        cid = (await core.call("session.openSaved", {"id": "cg:old", "projectId": str(proj)}))["id"]
        await until(lambda: sessions_of(events).get(cid, {}).get("mark") == "ready")
        old_ms = before["sessions"]["cg:old"]["modifiedAt"]
        plan.script([("hold", "t"), *say("Hi")])
        await core.call("session.send", {"id": cid, "text": "hello"})
        await until(lambda: plan.requests)
        core._emit_sidebar()
        assert listed(events) == ["cg:new", "cg:mid", cid], "activity does not change creation order"
        await until(lambda: sessions_of(events)[cid]["modifiedAt"] > old_ms)

        controller = core._by_cid[cid].controller
        controller.active_at = 1.0
        plan.release("t")
        await idle(core, cid)
        assert controller.active_at > 1.0
        core._emit_sidebar()
        assert listed(events) == ["cg:new", "cg:mid", cid]
        await core.shutdown()

    run(scenario())


def test_a_closed_session_keeps_the_time_of_its_last_message(tmp_path):
    async def scenario():
        core, plan, events, _, _ = await ready_core(tmp_path, projects=False)
        proj, before = await three_saved(core, events, tmp_path)
        old_ms = before["sessions"]["cg:old"]["modifiedAt"]
        cid = (await core.call("session.openSaved", {"id": "cg:old", "projectId": str(proj)}))["id"]
        await until(lambda: sessions_of(events).get(cid, {}).get("mark") == "ready")
        await talk(core, plan, cid, "hello", "Hi")
        await core.call("session.close", {"id": cid})
        await until(lambda: events.last("sidebar")["sessions"].get("cg:old", {}).get("modifiedAt", 0) > old_ms)
        assert listed(events) == ["cg:new", "cg:mid", "cg:old"], "closing keeps creation order despite newer activity"
        row = events.last("sidebar")["sessions"]["cg:old"]
        assert row["modifiedAt"] > int((time.time() - 60) * 1000) and row["modified"] == "刚刚"
        await core.shutdown()

    run(scenario())


def test_a_new_empty_session_is_the_newest_and_changing_engine_does_not_change_its_place(tmp_path):
    async def scenario():
        core, _, events, _, _ = await devin_core(tmp_path, projects=False)
        proj, _ = await three_saved(core, events, tmp_path)
        cid = await new_session(core, events, proj)
        core._emit_sidebar()
        assert listed(events) == [cid, "cg:new", "cg:mid", "cg:old"], "nothing is older than a session that was just made"

        # a conversation last spoken to two hours ago, then moved to the other engine
        controller = core._by_cid[cid].controller
        spoken = time.time() - 7200
        controller.active_at = spoken
        core._emit_sidebar()
        assert listed(events) == [cid, "cg:new", "cg:mid", "cg:old"]
        assert (await core.call("session.choose", {"id": cid, "field": "model", "value": "devin:swe-2-high"}))["switched"]
        after = core._by_cid[cid].controller
        assert after is not controller and after.active_at == spoken
        core._emit_sidebar()
        assert listed(events) == [cid, "cg:new", "cg:mid", "cg:old"], "the switch keeps the original creation time"
        assert sessions_of(events)[cid]["modifiedAt"] == int(spoken * 1000)
        await core.shutdown()

    run(scenario())


def test_adding_and_removing_projects(tmp_path):
    async def scenario():
        core, _, events, platform, _ = await ready_core(tmp_path, projects=False)
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
        core, plan, events, _, _ = await ready_core(tmp_path, max_open=2)
        proj = tmp_path / "proj"
        await new_session(core, events, proj)
        second = await new_session(core, events, proj)
        events.clear()
        third = await new_session(core, events, proj)
        assert events.last("tabs")["tabs"] == [second, third]
        assert any("已自动关闭最久未使用的会话" in n["text"] for n in events.of("notice"))
        plan.script([("hold", "a"), *say("x")], [("hold", "b"), *say("y")])
        await core.call("session.send", {"id": third, "text": "work"})
        await until(lambda: len(plan.requests) == 1)
        await core.call("session.send", {"id": second, "text": "work"})
        await until(lambda: len(plan.requests) == 2)
        with pytest.raises(SessionLimitError):
            await core.call("session.new", {"projectId": str(proj)})
        plan.release("a")
        plan.release("b")
        await core.shutdown()

    run(scenario())


# --------------------------------------------------------------------------- attachments


def test_attachments_are_admitted_by_python_and_sent_with_the_message(tmp_path):
    async def scenario():
        core, plan, events, _, _ = await ready_core(tmp_path)
        cid = await new_session(core, events, tmp_path / "proj")
        png = base64.b64encode(tiny_png()).decode()
        added = await core.call("attachments.add", {"id": cid, "files": [
            {"name": "shot.png", "type": "image/png", "data": png},
            {"name": "notes.txt", "type": "text/plain", "data": base64.b64encode(b"hello").decode()},
            {"name": "empty.bin", "type": "", "data": ""},
        ]})
        views = {v["name"]: v for v in added["added"]}
        assert set(views) == {"shot.png", "notes.txt"} and views["shot.png"]["url"].startswith("data:image/png") and views["shot.png"]["kind"] == "image"
        assert "empty.bin" in added["rejected"]
        picture_id = views["shot.png"]["id"]
        assert (await core.call("attachments.full", {"id": cid, "attachmentId": picture_id}))["url"] == "data:image/png;base64,FULL"
        await core.call("attachments.remove", {"id": cid, "attachmentId": views["notes.txt"]["id"]})
        plan.script(say("ok"))
        await core.call("session.send", {"id": cid, "text": "look", "attachmentIds": [picture_id]})
        await until(lambda: plan.requests)
        kinds = [part["type"] for part in user_parts(plan.requests[-1])]
        assert kinds.count("input_image") == 1 and kinds.count("input_text") == 1, "the picture, and no file"
        await until(lambda: events.of("turns") and any(p["turns"] for p in events.of("turns")))
        assert last_turn(events)["blocks"][0]["attachments"][0]["name"] == "shot.png"
        await idle(core, cid)
        with pytest.raises(AppError):
            await core.call("attachments.add", {"id": "nope", "files": []})
        await core.shutdown()

    run(scenario())


def test_a_failed_turn_gives_the_message_and_its_attachments_back(tmp_path):
    async def scenario():
        core, plan, events, _, _ = await ready_core(tmp_path)
        cid = await new_session(core, events, tmp_path / "proj")
        (view,) = (await core.call("attachments.add", {"id": cid, "files": [png_upload("a.png")]}))["added"]
        plan.script(fail("invalid_request_error", "boom"))
        await core.call("session.send", {"id": cid, "text": "try this", "attachmentIds": [view["id"]]})
        await until(lambda: bool(events.of("restore")), 10)
        restore = events.last("restore")
        assert restore["id"] == cid and restore["text"] == "try this" and [a["name"] for a in restore["attachments"]] == ["a.png"]
        assert any("附件已放回输入框" in n["text"] for n in events.of("notice"))
        await core.shutdown()

    run(scenario())


def test_a_failed_send_puts_every_attachment_back_even_when_the_box_was_refilled_meanwhile(tmp_path):
    async def scenario():
        core, plan, events, _, _ = await ready_core(tmp_path)
        cid = await new_session(core, events, tmp_path / "proj")
        first = await core.call("attachments.add", {"id": cid, "files": [png_upload("original.png", tiny_png(7, 7))]})
        plan.script([("hold", "t"), *fail("invalid_request_error", "overloaded")], say("ok"))
        await core.call("session.send", {"id": cid, "text": "look", "attachmentIds": [first["added"][0]["id"]]})
        await until(lambda: plan.requests)
        # while it runs, the box takes ten more files (what a message usually holds)
        many = await core.call("attachments.add", {"id": cid, "files": [png_upload(f"n{i}.png", tiny_png(5 + i, 5, (i, 1, 1))) for i in range(10)]})
        assert len(many["added"]) == 10
        events.clear()
        plan.release("t")
        await until(lambda: events.of("restore"), 10)
        restored = events.of("restore")[-1]
        names = [a["name"] for a in restored["attachments"]]
        assert restored["text"] == "look" and len(names) == 11 and names[0] == "original.png", "the original is not left behind"
        notice = [n["text"] for n in events.of("notice")][-1]
        assert "放回输入框" in notice and "比一条消息通常带的量多 1 个文件" in notice and "重试时会让你确认" in notice, "and the user is told by how much"

        asked = await core.call("session.send", {"id": cid, "text": "look"})
        assert asked["ok"] is False and asked["over"]["files"] == 1 and len(plan.requests) == 1
        await core.call("session.send", {"id": cid, "text": "look", "confirmOver": True})
        await until(lambda: len(plan.requests) == 2)
        assert [part["type"] for part in user_parts(plan.requests[-1])].count("input_image") == 11
        await core.shutdown()

    run(scenario())


def test_a_failed_send_with_room_in_the_box_restores_quietly(tmp_path):
    async def scenario():
        core, plan, events, _, _ = await ready_core(tmp_path)
        cid = await new_session(core, events, tmp_path / "proj")
        added = await core.call("attachments.add", {"id": cid, "files": [png_upload("a.png")]})
        plan.script(fail("invalid_request_error", "overloaded"))
        events.clear()
        await core.call("session.send", {"id": cid, "text": "look", "attachmentIds": [added["added"][0]["id"]]})
        await until(lambda: events.of("restore"), 10)
        assert [a["name"] for a in events.of("restore")[-1]["attachments"]] == ["a.png"]
        assert [n["text"] for n in events.of("notice")][-1] == "这条消息没有发送成功：附件已放回输入框，可以直接重试。"
        await core.shutdown()

    run(scenario())


# --------------------------------------------------------------------------- settings and models


def test_reasoning_and_the_defaults_follow_the_choice(tmp_path):
    async def scenario():
        core, _, events, _, _ = await ready_core(tmp_path)
        cid = await new_session(core, events, tmp_path / "proj")
        events.clear()
        assert (await core.call("session.choose", {"id": cid, "field": "reasoning", "value": "high"})) == {"ok": True}
        await until(lambda: sessions_of(events).get(cid, {}).get("settings", {}).get("reasoning") == "high")
        assert core.settings.reasoning_effort == "high" and events.last("defaults")["defaults"]["reasoning"] == "high"
        assert (await core.call("session.choose", {"id": cid, "field": "autonomy", "value": "full"}))["ok"]
        assert core.settings.chatgpt_autonomy == "full"
        # A model that does not offer the current level gets its own default, and the page is told why.
        events.clear()
        assert (await core.call("session.choose", {"id": cid, "field": "model", "value": OTHER_MODEL}))["ok"]
        assert any("不支持「high」推理，已改为「low」" in n["text"] for n in events.of("notice"))
        await until(lambda: sessions_of(events).get(cid, {}).get("settings", {}).get("model") == OTHER_MODEL)
        assert core._by_cid[cid].engine == "chatgpt", "a model of the same engine: the same session"
        # Without a session the choice only changes the defaults for new ones.
        assert (await core.call("session.choose", {"field": "mode", "value": "spec"})) == {"ok": True}
        assert core.settings.mode == "spec"
        with pytest.raises(AppError):
            await core.call("session.choose", {"id": cid, "field": "colour", "value": "red"})
        await core.shutdown()

    run(scenario())


def test_application_settings_are_validated_and_announced(tmp_path):
    async def scenario():
        core, _, events, _, _ = await ready_core(tmp_path)
        view = await core.call("settings.set", {"key": "theme", "value": "dark"})
        assert view["theme"] == "dark" and events.last("settings")["settings"]["theme"] == "dark"
        assert (await core.call("settings.set", {"key": "maxOpen", "value": 6}))["maxOpen"] == 6
        for key, value in (("theme", "pink"), ("maxOpen", 99), ("maxOpen", True), ("fontScale", "xl"), ("sidebarWidth", 10), ("nothing", 1), ("droidPath", "x"), ("codexPath", "x")):
            with pytest.raises(AppError):
                await core.call("settings.set", {"key": key, "value": value})
        assert "droidPath" not in view and "codexPath" not in view, "no other program's path"
        await core.shutdown()

    run(scenario())


def test_the_compaction_threshold_is_set_validated_and_announced(tmp_path):
    async def scenario():
        core, _, events, _, _ = await ready_core(tmp_path)
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
        core, _, events, _, _ = await ready_core(tmp_path)
        assert (await core.call("app.init"))["settings"]["hiddenModels"] == []
        view = await core.call("models.setVisible", {"ids": [OTHER_MODEL, OTHER_MODEL, "devin:x"], "visible": False})
        assert view["hiddenModels"] == [OTHER_MODEL, "devin:x"], "once each, in the order hidden"
        assert events.last("settings")["settings"]["hiddenModels"] == [OTHER_MODEL, "devin:x"]
        view = await core.call("models.setVisible", {"ids": [OTHER_MODEL, "unknown"], "visible": True})
        assert view["hiddenModels"] == ["devin:x"], "showing a model that was not hidden is nothing"
        assert (await core.call("app.init"))["settings"]["hiddenModels"] == ["devin:x"], "a reloaded page is told too"
        for params in ({"ids": OTHER_MODEL, "visible": False}, {"ids": [""], "visible": False}, {"ids": [3], "visible": True}, {"ids": ["a"], "visible": 1}, {"visible": True}):
            with pytest.raises(AppError):
                await core.call("models.setVisible", params)
        assert core.settings.hidden_models == ["devin:x"]
        await core.shutdown()

    run(scenario())


def test_a_hidden_default_model_is_not_what_new_sessions_start_with(tmp_path):
    async def scenario():
        core, _, events, _, _ = await ready_core(tmp_path)
        assert (await core.call("session.choose", {"field": "model", "value": OTHER_MODEL})) == {"ok": True}
        assert core.defaults()["model"] == OTHER_MODEL
        await core.call("models.setVisible", {"ids": [OTHER_MODEL], "visible": False})
        assert events.last("defaults")["defaults"]["model"] == MODEL, "the first model that is shown"
        assert core.settings.default_model == OTHER_MODEL, "the pick itself is kept"
        await core.call("models.setVisible", {"ids": [MODEL], "visible": False})
        assert events.last("defaults")["defaults"]["model"] == "", "every model hidden: nothing to start with"
        await core.call("models.setVisible", {"ids": [OTHER_MODEL], "visible": True})
        assert events.last("defaults")["defaults"]["model"] == OTHER_MODEL, "shown again, the pick is the default again"
        await core.shutdown()

    run(scenario())


def test_the_window_calls_reach_the_platform(tmp_path):
    async def scenario():
        core, _, events, platform, _ = await ready_core(tmp_path)
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


def test_a_page_reinit_resends_every_open_conversation(tmp_path):
    async def scenario():
        core, plan, events, _, _ = await ready_core(tmp_path)
        cid = await new_session(core, events, tmp_path / "proj")
        await talk(core, plan, cid, "hello", "Hi there")
        events.clear()

        init = await core.call("app.init")
        assert init["tabs"] == [cid]
        await until(lambda: events.of("turns") and cid in sessions_of(events))
        turns = [turn for payload in events.of("turns") for turn in payload["turns"]]
        assert any(block.get("text") == "hello" for turn in turns for block in turn["blocks"] if block["kind"] == "user")
        assert any(block.get("markdown") == "Hi there" for turn in turns for block in turn["blocks"] if block["kind"] == "text")
        patch = sessions_of(events)[cid]
        assert {"title", "mark", "engine", "settings", "usage", "context"} <= set(patch), "the whole meta again, not just a diff"
        assert patch["mark"] == "ready" and patch["settings"]["model"] == MODEL
        await core.shutdown()

    run(scenario())


def test_an_unreadable_providers_file_is_reported_and_the_app_still_starts(tmp_path):
    from desktop.core.providers import ProviderStore

    async def scenario():
        broken = tmp_path / "models.json"
        broken.write_text("{ not json", encoding="utf-8")
        store = ProviderStore(models_file=broken, sidecar=tmp_path / "providers.json", environ={}, registry_env=lambda: {})
        core, _, events, _, _ = await ready_core(tmp_path, providers=store)
        init = await core.call("app.init")
        assert init["providers"] == [] and any("读取 API Provider 失败" in n["text"] for n in events.of("notice"))
        assert any(m["id"] == MODEL for m in init["models"]), "the rest of the application is there"
        await core.shutdown()

    run(scenario())


# --------------------------------------------------------------------------- changing engine inside a conversation


async def send_with_files(core, plan, cid, text, files):
    """One finished own-loop turn with these (name, type, bytes) files attached."""
    added = await core.call(
        "attachments.add",
        {"id": cid, "files": [{"name": name, "type": kind, "data": base64.b64encode(data).decode()} for name, kind, data in files]},
    )
    plan.script(say("noted"))
    turns = len(core._by_cid[cid].controller.transcript.turns)
    await core.call("session.send", {"id": cid, "text": text, "attachmentIds": [v["id"] for v in added["added"]]})
    await until(lambda: len(core._by_cid[cid].controller.transcript.turns) == turns + 1)
    await idle(core, cid)


async def ask_to_switch(core, cid, *, model="devin:swe-2-high", **more):
    return await core.call("session.choose", {"id": cid, "field": "model", "value": model, **more})


def prompt_blocks(harness):
    return harness.requests("session/prompt")[-1]["prompt"]


def test_changing_engine_continues_the_same_conversation_with_the_text_handed_over(tmp_path):
    async def scenario():
        core, plan, events, _, harness = await devin_core(tmp_path)
        cid = await new_session(core, events, tmp_path / "proj")
        await talk(core, plan, cid, "What does context.py do?", "It reads the context size.")
        events.clear()

        result = await ask_to_switch(core, cid)
        assert result == {"ok": True, "switched": True}
        assert [c.cid for c in core.order] == [cid], "the same tab"
        assert core._by_cid[cid].controller.handoff.carried == 2
        turns = [p for p in events.of("turns") if p["turns"]][-1]
        assert turns["all"] is False and turns["turns"][-1]["blocks"][0]["kind"] == "switch"
        switch = turns["turns"][-1]["blocks"][0]
        assert switch["from"] == {"model": "GPT-6.1-Sol", "engine": "ChatGPT Plan"} and switch["to"]["engine"] == "Devin" and switch["carried"] == 2
        meta = sessions_of(events)[cid]
        assert meta["engine"] == "devin" and meta["settings"]["model"] == "devin:swe-2-high" and meta["handoff"]["carried"] == 2
        assert any("已换到 SWE-2（Devin）" in n["text"] and "2 条对话的文字" in n["text"] for n in events.of("notice"))

        # the next message carries the earlier text to the model, but the page shows only what was typed
        events.clear()
        await core.call("session.send", {"id": cid, "text": "and then?"})
        await until(lambda: harness.requests("session/prompt"), 10)
        text = prompt_blocks(harness)[0]["text"]
        assert "What does context.py do?" in text and "It reads the context size." in text and text.endswith("and then?")
        await until(lambda: any(p["turns"] for p in events.of("turns")))
        user = last_turn(events)["blocks"][0]
        assert user["text"] == "and then?" and user["handoff"] == {"carried": 2, "truncated": False}
        await idle(core, cid)
        await until(lambda: sessions_of(events)[cid].get("handoff", 1) is None)

        # and back: the whole conversation, the Devin turn included, goes along
        events.clear()
        assert (await ask_to_switch(core, cid, model=MODEL))["switched"]
        second = [p for p in events.of("turns") if p["turns"]][-1]["turns"][-1]["blocks"][0]
        assert second["kind"] == "switch" and second["from"]["engine"] == "Devin" and second["carried"] == 4
        await core.shutdown()

    run(scenario())


def test_changing_engine_lists_every_attachment_and_changes_nothing_until_told_which_go(tmp_path):
    async def scenario():
        core, plan, events, _, harness = await devin_core(tmp_path)
        cid = await new_session(core, events, tmp_path / "proj")
        picture = tiny_png()
        await send_with_files(core, plan, cid, "read these", [
            ("notes.txt", "text/plain", b"remember the milk"),
            ("shot.png", "image/png", picture),
            ("paper.pdf", "application/pdf", pdf_bytes("the budget is five")),
        ])
        controller = core._by_cid[cid].controller

        asked = await ask_to_switch(core, cid)
        assert asked["ok"] is False
        listed_items = {item["name"]: item for item in asked["choose"]["attachments"]}
        assert list(listed_items) == ["notes.txt", "shot.png", "paper.pdf"], "every one is listed, in the order they were sent"
        assert [(i["kind"], i["as"], i["bytes"]) for i in listed_items.values()] == [
            ("text", "text", len(b"remember the milk")),
            ("image", "image", len(picture)),
            ("pdf", "text", len("the budget is five".encode())),
        ]
        assert not any("reason" in i for i in listed_items.values())
        assert asked["choose"]["limits"] == {"files": 10, "bytes": 20 * 1024 * 1024}
        assert core._by_cid[cid].controller is controller and controller.engine == "chatgpt", "asking changes nothing"

        for bad in ("yes", 1, "true", [1], {"a": "b"}, True):
            with pytest.raises(AppError, match="不合法"):
                await ask_to_switch(core, cid, attachments=bad)
        with pytest.raises(AppError, match="找不到选中的附件"):
            await ask_to_switch(core, cid, attachments=["a999"])
        assert core._by_cid[cid].controller is controller, "a wrong answer changes nothing either"
        await core.shutdown()

    run(scenario())


def test_the_attachments_picked_go_along_one_by_one_and_the_rest_stay_behind(tmp_path):
    async def scenario():
        core, plan, events, _, harness = await devin_core(tmp_path)
        cid = await new_session(core, events, tmp_path / "proj")
        await send_with_files(core, plan, cid, "read these", [
            ("notes.txt", "text/plain", b"remember the milk"),
            ("secret.txt", "text/plain", b"do not hand this over"),
            ("shot.png", "image/png", tiny_png()),
            ("other.png", "image/png", tiny_png(5, 5, (1, 2, 3))),
        ])
        asked = await ask_to_switch(core, cid)
        ids = {item["name"]: item["id"] for item in asked["choose"]["attachments"]}

        done = await ask_to_switch(core, cid, attachments=[ids["notes.txt"], ids["shot.png"], ids["shot.png"]])
        assert done == {"ok": True, "switched": True}
        handoff = core._by_cid[cid].controller.handoff
        assert handoff.files == 1 and handoff.pictures == 1 and handoff.pdfs == 0 and handoff.carried == 2
        assert "remember the milk" in handoff.prefix and "do not hand this over" not in handoff.prefix
        assert [a.name for a in handoff.attachments] == ["shot.png"], "a picture picked twice goes once; the one not picked stays"
        (note,) = [n["text"] for n in events.of("notice") if "已换到" in n["text"]]
        assert "1 个文本附件的内容、1 张图片一起带过去" in note

        await core.call("session.send", {"id": cid, "text": "and now?"})
        await until(lambda: harness.requests("session/prompt"), 10)
        blocks = prompt_blocks(harness)
        assert [b["type"] for b in blocks].count("image") == 1 and "remember the milk" in blocks[0]["text"]
        await idle(core, cid)
        await core.call("session.send", {"id": cid, "text": "again"})
        await until(lambda: len(harness.requests("session/prompt")) == 2, 10)
        assert [b["type"] for b in prompt_blocks(harness)] == ["text"], "what was handed over goes once"
        await idle(core, cid)
        await core.shutdown()

    run(scenario())


def test_picking_nothing_carries_no_attachment_of_any_kind(tmp_path):
    async def scenario():
        core, plan, events, _, harness = await devin_core(tmp_path)
        cid = await new_session(core, events, tmp_path / "proj")
        await send_with_files(core, plan, cid, "read", [("notes.txt", "text/plain", b"remember the milk"), ("shot.png", "image/png", tiny_png())])
        assert (await ask_to_switch(core, cid, attachments=[])) == {"ok": True, "switched": True}
        handoff = core._by_cid[cid].controller.handoff
        assert handoff.files == 0 and handoff.pictures == 0 and handoff.attachments == () and handoff.carried == 2
        assert "remember the milk" not in handoff.prefix
        (note,) = [n["text"] for n in events.of("notice") if "已换到" in n["text"]]
        assert "一起带过去" not in note
        await core.shutdown()

    run(scenario())


def test_what_the_new_model_cannot_take_is_listed_with_the_reason_and_cannot_be_picked(tmp_path):
    async def scenario():
        core, plan, events, _, harness = await devin_core(tmp_path)
        cid = await new_session(core, events, tmp_path / "proj")
        await send_with_files(core, plan, cid, "read", [("notes.txt", "text/plain", b"remember the milk"), ("shot.png", "image/png", tiny_png())])
        controller = core._by_cid[cid].controller
        asked = await ask_to_switch(core, cid, model="devin:glm-5-2")
        listed_items = {item["name"]: item for item in asked["choose"]["attachments"]}
        assert "reason" not in listed_items["notes.txt"]
        assert "不支持图片输入" in listed_items["shot.png"]["reason"]
        with pytest.raises(AppError, match="shot.png.*带不过去.*不支持图片输入"):
            await ask_to_switch(core, cid, model="devin:glm-5-2", attachments=[listed_items["notes.txt"]["id"], listed_items["shot.png"]["id"]])
        assert core._by_cid[cid].controller is controller, "nothing is carried quietly in place of what was asked"
        await ask_to_switch(core, cid, model="devin:glm-5-2", attachments=[listed_items["notes.txt"]["id"]])
        assert core._by_cid[cid].controller.handoff.files == 1
        await core.shutdown()

    run(scenario())


def test_picking_more_than_a_message_usually_holds_is_the_users_call_told_by_how_much(tmp_path):
    async def scenario():
        core, plan, events, _, harness = await devin_core(tmp_path)
        cid = await new_session(core, events, tmp_path / "proj")
        for turn in range(2):  # 12 pictures, in two messages: one message usually holds 10
            await send_with_files(core, plan, cid, f"batch {turn}", [(f"p{turn}-{i}.png", "image/png", tiny_png(4 + i, 3, (turn, i, 9))) for i in range(6)])
        controller = core._by_cid[cid].controller
        asked = await ask_to_switch(core, cid)
        ids = [item["id"] for item in asked["choose"]["attachments"]]
        assert len(ids) == 12, "all twelve are listed"
        assert asked["choose"]["hard"] == {"files": 50, "bytes": 200 * 1024 * 1024}, "what the page may not let through at all"

        with pytest.raises(AppError, match="比一条消息通常带的量多 2 个文件.*这条消息有 12 个附件.*请在清单里确认"):
            await ask_to_switch(core, cid, attachments=ids)
        for bad in ("yes", 1, [True]):
            with pytest.raises(AppError, match="不合法"):
                await ask_to_switch(core, cid, attachments=ids, overLimit=bad)
        assert core._by_cid[cid].controller is controller

        done = await ask_to_switch(core, cid, attachments=ids, overLimit=True)
        assert done == {"ok": True, "switched": True}
        assert len(core._by_cid[cid].controller.handoff.attachments) == 12, "all twelve go: nothing is dropped for being many"

        added = await core.call("attachments.add", {"id": cid, "files": [png_upload("early.png", tiny_png(9, 9))]})
        assert len(added["added"]) == 1 and "多 3 个文件" in added["over"] and "发送时会让你确认" in added["over"]
        asked_to_send = await core.call("session.send", {"id": cid, "text": "go"})
        assert asked_to_send["ok"] is False and asked_to_send["over"]["files"] == 3 and asked_to_send["over"]["count"] == 13
        assert harness.requests("session/prompt") == [], "nothing was sent"
        await core.call("session.send", {"id": cid, "text": "go", "confirmOver": True})
        await until(lambda: harness.requests("session/prompt"), 10)
        assert [b["type"] for b in prompt_blocks(harness)].count("image") == 13
        await idle(core, cid)
        await core.shutdown()

    run(scenario())


def test_a_switch_that_fails_leaves_the_conversation_as_it_was(tmp_path):
    async def scenario():
        core, plan, events, _, harness = await devin_core(tmp_path, wait_models=False, FAKE_DEVIN_OPEN_FAIL="1")
        cid = await new_session(core, events, tmp_path / "proj")
        await talk(core, plan, cid, "hello", "Hi")
        controller = core._by_cid[cid].controller
        turns = len(controller.transcript.turns)
        with pytest.raises(AppError):
            await ask_to_switch(core, cid)
        assert core._by_cid[cid].controller is controller and core._by_cid[cid].engine == "chatgpt"
        assert len(controller.transcript.turns) == turns, "no divider was left behind"
        await until(lambda: sessions_of(events)[cid]["engine"] == "chatgpt" and sessions_of(events)[cid]["settings"]["model"] == MODEL)  # (the page is told with the next flush)
        await talk(core, plan, cid, "still here?", "yes")
        await core.shutdown()

    run(scenario())


def test_a_running_turn_cannot_change_engine_and_an_empty_session_switches_without_a_divider(tmp_path):
    async def scenario():
        core, plan, events, _, harness = await devin_core(tmp_path)
        cid = await new_session(core, events, tmp_path / "proj")
        plan.script([("hold", "t"), *say("late")])
        await core.call("session.send", {"id": cid, "text": "work"})
        await until(lambda: plan.requests)
        with pytest.raises(AppError, match="本轮还在运行"):
            await ask_to_switch(core, cid)
        await core.call("session.stop", {"id": cid})
        plan.release("t")

        empty = await new_session(core, events, tmp_path / "proj")
        assert (await ask_to_switch(core, empty))["switched"]
        assert core._by_cid[empty].engine == "devin" and core._by_cid[empty].controller.transcript.turns == [], "nothing to divide"
        await core.shutdown()

    run(scenario())


def test_changing_engine_never_closes_another_conversation_even_above_the_limit(tmp_path):
    async def scenario():
        core, plan, events, _, harness = await devin_core(tmp_path, max_open=2)
        first = await new_session(core, events, tmp_path / "proj")
        second = await new_session(core, events, tmp_path / "proj")
        await talk(core, plan, second, "hello", "Hi")
        assert (await ask_to_switch(core, second))["switched"]
        assert [c.cid for c in core.order] == [first, second], "the tab was counted already: nothing makes room for it"
        await core.shutdown()

    run(scenario())
