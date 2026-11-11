"""Messages typed while a turn runs: queued behind it or given to it (core level, fake engines), and the token total."""

from __future__ import annotations

import asyncio
import base64

import pytest
from droid_sdk import DroidError, DroidProcessError

from desktop.core.app_core import AppError
from desktop.core.cache import Cache
from desktop.core.session_manager import HistorySnapshot, SessionState
from desktop.core.settings import FOLLOW_UPS, AppSettings, settings_from_dict
from tests.app_support import make_core, ready_core
from tests.fakes import FakeFactory, until
from tests.support import complete, delta, failure, history_messages, saved_session, success, tiny_png, usage, user_message


def run(coro):
    return asyncio.run(coro)


def sessions_of(events):
    merged: dict[str, dict] = {}
    for payload in events.of("session"):
        merged.setdefault(payload["id"], {}).update(payload["patch"])
    return merged


async def new_session(core, events, project):
    cid = (await core.call("session.new", {"projectId": str(project)}))["id"]
    await until(lambda: any(p["id"] == cid and p["patch"].get("mark") == "ready" for p in events.of("session")))
    return cid


def queued_texts(events, cid):
    return [m["text"] for m in sessions_of(events).get(cid, {}).get("queued") or []]


async def running_turn(tmp_path, **options):
    core, factory, events, _, codex = await ready_core(tmp_path, **options)
    cid = await new_session(core, events, tmp_path / "proj")
    session = factory.sessions[-1]
    await core.call("session.send", {"id": cid, "text": "first"})
    await until(lambda: session.active is not None)
    return core, factory, events, cid, session


async def end_turn(session, text="ok", message_id="m"):
    stream = session.active
    stream.push(delta(text, message_id), complete(message_id), success(text))
    stream.end()


def blocks_of_last_turn(events):
    """The blocks of the turn the page was last sent a change of (an event may carry no turn at all)."""
    for payload in reversed(events.of("turns")):
        if payload["turns"]:
            return payload["turns"][-1]["blocks"]
    return []


def test_messages_typed_while_a_turn_runs_wait_in_line_and_each_goes_out_as_its_own_turn(tmp_path):
    async def scenario():
        core, _, events, cid, session = await running_turn(tmp_path)
        assert await core.call("session.send", {"id": cid, "text": "second", "how": "queue"}) == {"ok": True, "queued": True}
        assert await core.call("session.send", {"id": cid, "text": "third"}) == {"ok": True, "queued": True}, "no word on how: queued"
        await until(lambda: queued_texts(events, cid) == ["second", "third"])
        assert [s.prompt for s in session.streams] == ["first"], "nothing goes out while the turn runs"

        await end_turn(session)
        await until(lambda: [s.prompt for s in session.streams] == ["first", "second"])
        await until(lambda: queued_texts(events, cid) == ["third"])
        await end_turn(session, "two", "m2")
        await until(lambda: [s.prompt for s in session.streams] == ["first", "second", "third"])
        await until(lambda: queued_texts(events, cid) == [])
        await end_turn(session, "three", "m3")
        await until(lambda: core._by_cid[cid].controller.state is SessionState.READY)
        turns = core._by_cid[cid].controller.transcript.turns
        assert [t.blocks[0].text for t in turns] == ["first", "second", "third"], "three turns, in the order they were typed"
        await core.shutdown()

    run(scenario())


def test_a_message_that_arrives_ahead_of_waiting_ones_keeps_its_place(tmp_path):
    async def scenario():
        core, _, events, cid, session = await running_turn(tmp_path)
        await core.call("session.send", {"id": cid, "text": "second"})
        await end_turn(session)
        # the turn is over, "second" is on its way out: a message typed now is behind it, not in front
        await core.call("session.send", {"id": cid, "text": "third"})
        await until(lambda: [s.prompt for s in session.streams][:2] == ["first", "second"])
        assert "third" not in [s.prompt for s in session.streams] or [s.prompt for s in session.streams].index("third") == 2
        await until(lambda: queued_texts(events, cid) == ["third"])
        await core.shutdown()

    run(scenario())


def test_a_steer_is_given_to_the_running_turn_and_shown_where_it_was_said(tmp_path):
    async def scenario():
        core, _, events, cid, session = await running_turn(tmp_path)
        result = await core.call("session.send", {"id": cid, "text": "also the docs", "how": "steer"})
        assert result == {"ok": True, "steered": True}
        (message,) = session.user_messages
        assert message["text"] == "also the docs"
        await until(lambda: any(b.get("steer") for b in blocks_of_last_turn(events)))
        steer = next(b for b in blocks_of_last_turn(events) if b.get("steer"))
        assert steer["text"] == "also the docs" and steer["pending"] is True and steer["kind"] == "user"
        assert queued_texts(events, cid) == [], "it is not waiting in line: the turn has it"

        session.active.push(user_message(message["message_id"], "also the docs"))
        await until(lambda: not next(b for b in blocks_of_last_turn(events) if b.get("steer")).get("pending"))
        await end_turn(session)
        await until(lambda: core._by_cid[cid].controller.state is SessionState.READY)
        assert len(session.streams) == 1, "a steer does not start a turn"
        await core.shutdown()

    run(scenario())


def test_a_steer_that_droid_refuses_is_queued_instead_and_the_user_is_told(tmp_path):
    async def scenario():
        core, _, events, cid, session = await running_turn(tmp_path)
        session.add_message_error = DroidError("busy")
        events.clear()
        result = await core.call("session.send", {"id": cid, "text": "please", "how": "steer"})
        assert result == {"ok": True, "queued": True}
        assert any("没能引导本轮" in n["text"] and "排队" in n["text"] for n in events.of("notice"))
        await until(lambda: queued_texts(events, cid) == ["please"])
        assert not any(b.get("steer") for b in blocks_of_last_turn(events)) if events.of("turns") else True
        await core.shutdown()

    run(scenario())


def test_how_is_ignored_when_no_turn_is_running(tmp_path):
    async def scenario():
        core, factory, events, _, _ = await ready_core(tmp_path)
        cid = await new_session(core, events, tmp_path / "proj")
        session = factory.sessions[-1]
        assert await core.call("session.send", {"id": cid, "text": "hello", "how": "steer"}) == {"ok": True}
        await until(lambda: session.active is not None)
        assert [s.prompt for s in session.streams] == ["hello"] and session.user_messages == []
        await end_turn(session)
        await core.shutdown()

    run(scenario())


def test_how_must_be_one_of_the_two(tmp_path):
    async def scenario():
        core, _, events, cid, session = await running_turn(tmp_path)
        for bad in ("now", 3, ["steer"], ""):
            with pytest.raises(AppError, match="发送方式不合法"):
                await core.call("session.send", {"id": cid, "text": "x", "how": bad})
        assert queued_texts(events, cid) == [] and session.user_messages == []
        await core.shutdown()

    run(scenario())


def test_a_steer_for_a_turn_that_has_just_ended_goes_out_as_an_ordinary_message(tmp_path):
    from desktop.core.session_manager import SteerOutcome

    async def scenario():
        core, _, events, cid, session = await running_turn(tmp_path)
        controller = core._by_cid[cid].controller

        async def too_late(text, attachments=()):
            await end_turn(session)  # the turn ended while the steer was on its way
            await until(lambda: controller.state is SessionState.READY)
            return SteerOutcome(False, over=True)

        controller.steer = too_late
        result = await core.call("session.send", {"id": cid, "text": "later then", "how": "steer"})
        assert result == {"ok": True}, "not queued, not an error: the user pressed send"
        await until(lambda: [s.prompt for s in session.streams] == ["first", "later then"])
        assert queued_texts(events, cid) == []
        await core.shutdown()

    run(scenario())


def test_stopping_a_turn_still_sends_what_was_queued_behind_it(tmp_path):
    async def scenario():
        core, _, events, cid, session = await running_turn(tmp_path)
        await core.call("session.send", {"id": cid, "text": "instead, do this"})
        await core.call("session.stop", {"id": cid})
        await until(lambda: [s.prompt for s in session.streams] == ["first", "instead, do this"])
        await core.shutdown()

    run(scenario())


def test_a_failed_turn_does_not_send_what_was_queued_it_comes_back_to_the_composer(tmp_path):
    async def scenario():
        core, _, events, cid, session = await running_turn(tmp_path)
        await core.call("session.send", {"id": cid, "text": "second"})
        await core.call("session.send", {"id": cid, "text": "third"})
        events.clear()
        session.active.push(failure("the model said no"))
        session.active.end()
        await until(lambda: events.of("restore"), timeout=5)
        assert events.last("restore")["text"] == "second\n\nthird", "in the order they were typed"
        assert any("上一轮没有成功" in n["text"] for n in events.of("notice"))
        assert [s.prompt for s in session.streams] == ["first"], "nothing was sent behind its back"
        await until(lambda: queued_texts(events, cid) == [])
        await core.shutdown()

    run(scenario())


def test_a_lost_connection_gives_the_queue_back(tmp_path):
    async def scenario():
        core, _, events, cid, session = await running_turn(tmp_path)
        await core.call("session.send", {"id": cid, "text": "second"})
        events.clear()
        session.active.push(DroidProcessError("crashed", exit_code=3))
        await until(lambda: events.of("restore"), timeout=5)
        assert events.last("restore")["text"] == "second"
        assert any("连接断开了" in n["text"] for n in events.of("notice"))
        assert core._by_cid[cid].controller.state is SessionState.DEAD and core._by_cid[cid].queue == []
        await core.shutdown()

    run(scenario())


def test_one_queued_message_can_be_taken_back_and_the_rest_stay(tmp_path):
    async def scenario():
        core, _, events, cid, session = await running_turn(tmp_path)
        await core.call("session.send", {"id": cid, "text": "second"})
        await core.call("session.send", {"id": cid, "text": "third"})
        await until(lambda: queued_texts(events, cid) == ["second", "third"])
        (first, second) = [m["id"] for m in sessions_of(events)[cid]["queued"]]
        assert await core.call("session.unqueue", {"id": cid, "queueId": "nope"}) == {"ok": False}
        with pytest.raises(AppError):
            await core.call("session.unqueue", {"id": cid, "queueId": 7})
        assert await core.call("session.unqueue", {"id": cid, "queueId": first}) == {"ok": True}
        assert events.last("restore")["text"] == "second"
        await until(lambda: queued_texts(events, cid) == ["third"])
        assert await core.call("session.unqueue", {"id": cid, "queueId": first}) == {"ok": False}, "already back"
        await end_turn(session)
        await until(lambda: [s.prompt for s in session.streams] == ["first", "third"])
        await core.shutdown()

    run(scenario())


def queue_ids(events, cid):
    return [m["id"] for m in sessions_of(events)[cid]["queued"]]


def test_a_waiting_message_can_be_reworded_and_keeps_its_place_and_its_attachments(tmp_path):
    async def scenario():
        core, _, events, cid, session = await running_turn(tmp_path)
        png = base64.b64encode(tiny_png()).decode()
        await core.call("session.send", {"id": cid, "text": "second"})
        await core.call("attachments.add", {"id": cid, "files": [{"name": "shot.png", "type": "image/png", "data": png}]})
        await core.call("session.send", {"id": cid, "text": "third"})
        await until(lambda: queued_texts(events, cid) == ["second", "third"])
        first, second = queue_ids(events, cid)
        assert await core.call("session.editQueued", {"id": cid, "queueId": second, "text": "  third, reworded  "}) == {"ok": True}
        await until(lambda: queued_texts(events, cid) == ["second", "third, reworded"])
        assert await core.call("session.editQueued", {"id": cid, "queueId": first, "text": "second, reworded"}) == {"ok": True}
        await until(lambda: queued_texts(events, cid) == ["second, reworded", "third, reworded"])
        assert queue_ids(events, cid) == [first, second], "the same messages in the same places"
        assert [a["name"] for a in sessions_of(events)[cid]["queued"][1]["attachments"]] == ["shot.png"]
        await end_turn(session)
        await until(lambda: [s.prompt for s in session.streams] == ["first", "second, reworded"])
        await end_turn(session, "two", "m2")
        await until(lambda: [s.prompt for s in session.streams] == ["first", "second, reworded", "third, reworded"])
        assert len(session.streams[2].images) == 1
        await core.shutdown()

    run(scenario())


def test_editing_a_waiting_message_is_checked_and_a_message_that_has_gone_is_not_an_error(tmp_path):
    async def scenario():
        core, _, events, cid, session = await running_turn(tmp_path)
        await core.call("session.send", {"id": cid, "text": "second"})
        await until(lambda: queued_texts(events, cid) == ["second"])
        (queued,) = queue_ids(events, cid)
        with pytest.raises(AppError):
            await core.call("session.editQueued", {"id": cid, "queueId": queued, "text": "   "})
        with pytest.raises(AppError):
            await core.call("session.editQueued", {"id": cid, "queueId": queued, "text": 7})
        with pytest.raises(AppError):
            await core.call("session.editQueued", {"id": cid, "queueId": 7, "text": "x"})
        assert queued_texts(events, cid) == ["second"], "nothing changed"
        assert await core.call("session.editQueued", {"id": cid, "queueId": "nope", "text": "x"}) == {"ok": False}
        await core.call("session.unqueue", {"id": cid, "queueId": queued})
        assert await core.call("session.editQueued", {"id": cid, "queueId": queued, "text": "late"}) == {"ok": False}, "taken back meanwhile"
        await core.shutdown()

    run(scenario())


def test_a_waiting_message_can_be_sent_now_to_the_turn_and_the_others_stay_in_line(tmp_path):
    async def scenario():
        core, _, events, cid, session = await running_turn(tmp_path)
        await core.call("session.send", {"id": cid, "text": "second"})
        await core.call("session.send", {"id": cid, "text": "third"})
        await until(lambda: queued_texts(events, cid) == ["second", "third"])
        first, second = queue_ids(events, cid)
        assert await core.call("session.sendQueued", {"id": cid, "queueId": second}) == {"ok": True, "steered": True}
        assert [m["text"] for m in session.user_messages] == ["third"], "given to the running turn, ahead of the one before it"
        await until(lambda: queued_texts(events, cid) == ["second"])
        await until(lambda: any(b.get("steer") and b["text"] == "third" for b in blocks_of_last_turn(events)))
        assert await core.call("session.sendQueued", {"id": cid, "queueId": second}) == {"ok": False}, "already gone"
        await end_turn(session)
        await until(lambda: [s.prompt for s in session.streams] == ["first", "second"])
        assert len(session.streams) == 2, "'third' was not sent again as a turn of its own"
        await core.shutdown()

    run(scenario())


def test_a_message_sent_now_as_the_turn_ends_is_the_next_message_and_not_lost(tmp_path):
    async def scenario():
        from desktop.core.session_manager import SteerOutcome

        core, _, events, cid, session = await running_turn(tmp_path)
        await core.call("session.send", {"id": cid, "text": "second"})
        await core.call("session.send", {"id": cid, "text": "third"})
        await until(lambda: queued_texts(events, cid) == ["second", "third"])
        first, second = queue_ids(events, cid)

        async def too_late(text, attachments=()):
            return SteerOutcome(False, over=True)

        core._by_cid[cid].controller.steer = too_late  # (the turn ended while the message was on its way)
        assert await core.call("session.sendQueued", {"id": cid, "queueId": second}) == {"ok": True}
        assert [m.text for m in core._by_cid[cid].queue] == ["third", "second"], "ahead of the other one: it was asked for now"
        await end_turn(session)
        await until(lambda: [s.prompt for s in session.streams][:2] == ["first", "third"])
        await core.shutdown()

    run(scenario())


def test_sending_now_a_message_the_turn_cannot_take_leaves_it_where_it_was_and_says_so(tmp_path):
    async def scenario():
        core, _, events, cid, session = await running_turn(tmp_path)
        await core.call("session.send", {"id": cid, "text": "second"})
        await core.call("session.send", {"id": cid, "text": "third"})
        await until(lambda: queued_texts(events, cid) == ["second", "third"])
        first, second = queue_ids(events, cid)
        session.add_message_error = DroidError("busy")
        events.clear()
        assert await core.call("session.sendQueued", {"id": cid, "queueId": first}) == {"ok": False}
        assert any("没能引导本轮" in n["text"] and "还在排队" in n["text"] for n in events.of("notice"))
        assert [m.id for m in core._by_cid[cid].queue] == [first, second], "the same place in the line"
        with pytest.raises(AppError):
            await core.call("session.sendQueued", {"id": cid, "queueId": 7})
        await core.shutdown()

    run(scenario())


def test_a_message_sent_now_while_the_session_is_still_connecting_waits_and_the_user_is_told(tmp_path):
    async def scenario():
        core, factory, events, _, _ = await ready_core(tmp_path)
        factory.default_options["open_delay"] = 1.0
        opening = asyncio.ensure_future(core.call("session.new", {"projectId": str(tmp_path / "proj")}))
        await until(lambda: bool(core.order))
        cid = core.order[0].cid
        assert core._by_cid[cid].controller.state is SessionState.OPENING
        await core.call("session.send", {"id": cid, "text": "early"})
        await until(lambda: queued_texts(events, cid) == ["early"])
        (queued,) = queue_ids(events, cid)
        events.clear()
        assert await core.call("session.sendQueued", {"id": cid, "queueId": queued}) == {"ok": False}
        assert any("还没连上" in n["text"] for n in events.of("notice"))
        assert [m.text for m in core._by_cid[cid].queue] == ["early"], "still waiting"
        await opening
        await until(lambda: [s.prompt for s in factory.sessions[-1].streams] == ["early"], timeout=10)
        await core.shutdown()

    run(scenario())


def test_without_a_queue_id_unqueue_takes_all_of_them_back(tmp_path):
    async def scenario():
        core, _, events, cid, session = await running_turn(tmp_path)
        await core.call("session.send", {"id": cid, "text": "second"})
        await core.call("session.send", {"id": cid, "text": "third"})
        assert await core.call("session.unqueue", {"id": cid}) == {"ok": True}
        assert events.last("restore")["text"] == "second\n\nthird"
        assert await core.call("session.unqueue", {"id": cid}) == {"ok": False}
        await until(lambda: queued_texts(events, cid) == [])
        await end_turn(session)
        await until(lambda: core._by_cid[cid].controller.state is SessionState.READY)
        assert len(session.streams) == 1
        await core.shutdown()

    run(scenario())


def test_a_queued_message_keeps_its_attachments_and_gets_them_back_when_withdrawn(tmp_path):
    async def scenario():
        core, _, events, cid, session = await running_turn(tmp_path)
        png = base64.b64encode(tiny_png()).decode()
        await core.call("attachments.add", {"id": cid, "files": [{"name": "shot.png", "type": "image/png", "data": png}]})
        await core.call("session.send", {"id": cid, "text": "see this"})
        await until(lambda: queued_texts(events, cid) == ["see this"])
        (queued,) = sessions_of(events)[cid]["queued"]
        assert [a["name"] for a in queued["attachments"]] == ["shot.png"]
        assert core._by_cid[cid].drafts == [], "the composer is empty: the picture is in the message"
        await core.call("session.unqueue", {"id": cid, "queueId": queued["id"]})
        assert [a["name"] for a in events.last("restore")["attachments"]] == ["shot.png"]
        assert len(core._by_cid[cid].drafts) == 1
        await core.shutdown()

    run(scenario())


def test_a_queued_message_with_a_picture_goes_out_with_it(tmp_path):
    async def scenario():
        core, _, events, cid, session = await running_turn(tmp_path)
        png = base64.b64encode(tiny_png()).decode()
        await core.call("attachments.add", {"id": cid, "files": [{"name": "shot.png", "type": "image/png", "data": png}]})
        await core.call("session.send", {"id": cid, "text": "see this"})
        await end_turn(session)
        await until(lambda: len(session.streams) == 2)
        assert len(session.streams[1].images) == 1
        await core.shutdown()

    run(scenario())


def test_a_steer_droid_discards_comes_back_to_the_composer(tmp_path):
    async def scenario():
        core, _, events, cid, session = await running_turn(tmp_path)
        await core.call("session.send", {"id": cid, "text": "wait, no", "how": "steer"})
        events.clear()
        session.notify({"type": "queued_messages_discarded", "text": "wait, no", "requestId": "r1"})
        await until(lambda: events.of("restore"), timeout=5)
        assert events.last("restore")["text"] == "wait, no"
        assert any("引导没有来得及生效" in n["text"] for n in events.of("notice"))
        await end_turn(session)
        await core.shutdown()

    run(scenario())


def test_closing_a_conversation_with_messages_waiting_says_they_were_not_sent(tmp_path):
    async def scenario():
        core, _, events, cid, session = await running_turn(tmp_path)
        await core.call("session.send", {"id": cid, "text": "second"})
        await core.call("session.send", {"id": cid, "text": "third"})
        events.clear()
        await core.call("session.close", {"id": cid})
        assert any("2 条排队的消息没有发出" in n["text"] for n in events.of("notice"))
        await core.shutdown()

    run(scenario())


def test_what_is_waiting_when_a_session_closes_is_told_not_lost(tmp_path):
    async def scenario():
        core, _, events, cid, session = await running_turn(tmp_path)
        await core.call("session.send", {"id": cid, "text": "second"})
        conv = core._by_cid[cid]
        conv.queue.append(type(conv.queue[0])("q9", "also", ()))
        conv.controller.state = SessionState.CLOSED
        events.clear()
        core._drop(conv, conv.controller)
        assert any("second" in n["text"] and "also" in n["text"] for n in events.of("notice"))
        await core.shutdown()

    run(scenario())


# ----------------------------------------------------------------------------- the setting


def test_the_default_for_enter_is_queue_and_can_be_changed(tmp_path):
    async def scenario():
        core, _, events, _, _ = await ready_core(tmp_path)
        assert core.settings_view()["followUp"] == "queue"
        view = await core.call("settings.set", {"key": "followUp", "value": "steer"})
        assert view["followUp"] == "steer" and events.last("settings")["settings"]["followUp"] == "steer"
        assert core.settings.follow_up == "steer"
        for bad in ("both", 1, None):
            with pytest.raises(AppError):
                await core.call("settings.set", {"key": "followUp", "value": bad})
        assert core.settings.follow_up == "steer"
        await core.shutdown()

    run(scenario())


def test_the_setting_survives_a_restart_and_a_bad_value_in_the_file_means_queue():
    assert FOLLOW_UPS == ("queue", "steer")
    assert AppSettings().follow_up == "queue"
    assert settings_from_dict({"follow_up": "steer"}).follow_up == "steer"
    assert settings_from_dict({"follow_up": "sideways"}).follow_up == "queue"
    assert settings_from_dict({}).follow_up == "queue"


# ----------------------------------------------------------------------------- the whole conversation's tokens


def total_of(events, cid):
    return sessions_of(events).get(cid, {}).get("usage")


def test_the_total_adds_up_what_droid_counts_turn_after_turn(tmp_path):
    async def scenario():
        core, factory, events, _, _ = await ready_core(tmp_path)
        cid = await new_session(core, events, tmp_path / "proj")
        controller = core._by_cid[cid].controller
        controller.transcript.usage = usage(input_tokens=1000, output_tokens=100, cache_read=5000)
        core._by_cid[cid].dirty = True
        core._schedule_flush()
        await until(lambda: (total_of(events, cid) or {}).get("input") == 1000)
        controller.transcript.usage = usage(input_tokens=1500, output_tokens=180, cache_read=9000)
        core._by_cid[cid].dirty = True
        core._schedule_flush()
        await until(lambda: (total_of(events, cid) or {}).get("input") == 1500)
        assert total_of(events, cid) == {"input": 1500, "output": 180, "cacheWrite": 0, "cacheRead": 9000}, "droid's own running total, not a sum of sums"
        await core.shutdown()

    run(scenario())


def test_the_total_goes_on_after_the_session_is_closed_and_opened_again(tmp_path):
    async def scenario():
        proj = tmp_path / "proj"
        proj.mkdir()
        cache = Cache(tmp_path / "cache")
        core, factory, events, _, _ = await ready_core(tmp_path, projects=False, cache=cache)
        factory.saved = [saved_session("s-1", "Chat", minutes_ago=7)]
        factory.history["s-1"] = HistorySnapshot(messages=history_messages(), title="Chat")
        await core.call("project.add", {"path": str(proj)})
        await until(lambda: events.of("sidebar") and "s-1" in events.last("sidebar")["sessions"])
        cid = (await core.call("session.openSaved", {"id": "s-1", "projectId": str(proj), "title": "Chat"}))["id"]
        await until(lambda: sessions_of(events).get(cid, {}).get("mark") == "ready")
        core._by_cid[cid].controller.transcript.usage = usage(input_tokens=1000, output_tokens=100, cache_read=5000)
        core._by_cid[cid].dirty = True
        core._schedule_flush()
        await until(lambda: (total_of(events, cid) or {}).get("input") == 1000)
        await core.call("session.close", {"id": cid})
        await until(lambda: cache.load_session("s-1") is not None)
        await core.shutdown()

        # a new run of the application: droid knows the session has used 1000 + 400, the cache says 1000
        again, factory2, events2, _, _ = await ready_core(tmp_path, projects=False, cache=cache)
        factory2.saved = [saved_session("s-1", "Chat", minutes_ago=7)]
        factory2.history["s-1"] = HistorySnapshot(
            messages=history_messages(), title="Chat", usage={"inputTokens": 1400, "outputTokens": 140, "cacheCreationTokens": 0, "cacheReadTokens": 6000}
        )
        await again.call("project.add", {"path": str(proj)})
        await until(lambda: events2.of("sidebar") and "s-1" in events2.last("sidebar")["sessions"])
        cid2 = (await again.call("session.openSaved", {"id": "s-1", "projectId": str(proj), "title": "Chat"}))["id"]
        await until(lambda: sessions_of(events2).get(cid2, {}).get("mark") == "ready")
        assert total_of(events2, cid2) == {"input": 1400, "output": 140, "cacheWrite": 0, "cacheRead": 6000}, (
            "the session went on after the cache was written (it was used elsewhere): droid's figure is the truth, nothing is counted twice"
        )
        await again.shutdown()

    run(scenario())


def test_a_reopened_session_shows_the_cached_total_before_droid_has_said_anything(tmp_path):
    async def scenario():
        proj = tmp_path / "proj"
        proj.mkdir()
        cache = Cache(tmp_path / "cache")
        core, factory, events, _, _ = await ready_core(tmp_path, projects=False, cache=cache)
        factory.saved = [saved_session("s-1", "Chat", minutes_ago=7)]
        factory.history["s-1"] = HistorySnapshot(messages=history_messages(), title="Chat")
        await core.call("project.add", {"path": str(proj)})
        await until(lambda: events.of("sidebar") and "s-1" in events.last("sidebar")["sessions"])
        cid = (await core.call("session.openSaved", {"id": "s-1", "projectId": str(proj), "title": "Chat"}))["id"]
        await until(lambda: sessions_of(events).get(cid, {}).get("mark") == "ready")
        core._by_cid[cid].controller.transcript.usage = usage(input_tokens=1000, output_tokens=100, cache_read=5000)
        core._by_cid[cid].dirty = True
        core._schedule_flush()
        await until(lambda: (total_of(events, cid) or {}).get("input") == 1000)
        await core.call("session.close", {"id": cid})
        await until(lambda: cache.load_session("s-1") is not None)
        await core.shutdown()

        again, factory2, events2, _, _ = await ready_core(tmp_path, projects=False, cache=cache)
        factory2.saved = [saved_session("s-1", "Chat", minutes_ago=7)]
        factory2.history["s-1"] = HistorySnapshot(messages=history_messages(), title="Chat")
        factory2.default_options["open_delay"] = 0.5
        await again.call("project.add", {"path": str(proj)})
        await until(lambda: events2.of("sidebar") and "s-1" in events2.last("sidebar")["sessions"])
        opening = asyncio.ensure_future(again.call("session.openSaved", {"id": "s-1", "projectId": str(proj), "title": "Chat"}))
        await until(lambda: any((p["patch"].get("usage") or {}).get("input") == 1000 for p in events2.of("session")), timeout=5)
        await opening
        await again.shutdown()

    run(scenario())


def test_an_engine_change_keeps_the_total_and_goes_on_counting_with_the_new_engine(tmp_path):
    async def scenario():
        core, factory, events, _, codex = await ready_core(tmp_path, codex=True)
        cid = await new_session(core, events, tmp_path / "proj")
        droid = factory.sessions[-1]
        await core.call("session.send", {"id": cid, "text": "hello"})
        await until(lambda: droid.active is not None)
        droid.active.push(delta("Hi", "m1"), complete("m1"), success("Hi"))
        droid.active.end()
        await until(lambda: core._by_cid[cid].controller.state is SessionState.READY)
        core._by_cid[cid].controller.transcript.usage = usage(input_tokens=1000, output_tokens=100, cache_read=5000)
        core._by_cid[cid].dirty = True
        core._schedule_flush()
        await until(lambda: (total_of(events, cid) or {}).get("input") == 1000)

        assert (await core.call("session.choose", {"id": cid, "field": "model", "value": "codex:gpt-5.3-codex"}))["switched"]
        assert total_of(events, cid) == {"input": 1000, "output": 100, "cacheWrite": 0, "cacheRead": 5000}, "the old session's tokens stay"
        conv = core._by_cid[cid]
        # the new session counts from its own zero: Droid-like numbers from the fake Codex-like controller
        # (more than the old session counted: only a series that starts over adds all of it)
        conv.controller.transcript.usage = usage(input_tokens=2500, output_tokens=250, cache_read=7000)
        conv.dirty = True
        core._schedule_flush()
        await until(lambda: (total_of(events, cid) or {}).get("input") == 3500)
        assert total_of(events, cid) == {"input": 3500, "output": 350, "cacheWrite": 0, "cacheRead": 12000}
        conv.controller.transcript.usage = usage(input_tokens=2600, output_tokens=270, cache_read=8000)
        conv.dirty = True
        core._schedule_flush()
        await until(lambda: (total_of(events, cid) or {}).get("input") == 3600)
        assert total_of(events, cid) == {"input": 3600, "output": 370, "cacheWrite": 0, "cacheRead": 13000}
        await core.shutdown()

    run(scenario())


def real_codex_core(tmp_path, home, cache=None):
    """AppCore on a fake Droid and the real CodexEngine talking to ``tests/fake_codex.py`` (whose state lives in ``home``)."""
    from droid_sdk import ModelInfo, ModelProvider, ReasoningEffort

    from desktop.core.app_core import AppCore
    from desktop.core.session_manager import SessionManager
    from tests.app_support import Events, FakePlatform
    from tests.codex_support import make_engine

    project = tmp_path / "proj"
    project.mkdir(exist_ok=True)
    settings = AppSettings(default_model="claude-haiku-5-5", reasoning_effort="low", think_chinese=False)  # (as make_core)
    settings.add_project(project)
    factory = FakeFactory()
    factory.models = [
        ModelInfo(id="claude-haiku-5-5", display_name="Haiku 5.5", short_display_name="Haiku", model_provider=ModelProvider.ANTHROPIC,
                  supported_reasoning_efforts=(ReasoningEffort.LOW, ReasoningEffort.HIGH), default_reasoning_effort=ReasoningEffort.LOW)
    ]
    harness = make_engine(tmp_path, settings=settings, home=home)
    events = Events()
    core = AppCore(SessionManager(settings, factory), FakePlatform(), codex=harness.engine, persist=False, emit=events, cache=cache)
    return core, events, harness


def test_a_codex_turn_takes_a_steer_and_a_queued_message_through_the_core(tmp_path):
    from tests.codex_support import run as run_codex

    async def scenario():
        core, events, harness = real_codex_core(tmp_path, tmp_path / "codex-home")
        await core.call("app.init")
        await until(lambda: any(m["source"] == "codex" for p in events.of("models") for m in p["models"]), timeout=10)
        cid = (await core.call("session.new", {"projectId": str((tmp_path / "proj").resolve()), "modelId": "codex:gpt-6.1-sol"}))["id"]
        await until(lambda: sessions_of(events).get(cid, {}).get("mark") == "ready", timeout=10)
        controller = core._by_cid[cid].controller
        await core.call("session.send", {"id": cid, "text": "[slow] count"})
        await until(lambda: controller._turn_id, timeout=10)
        assert await core.call("session.send", {"id": cid, "text": "and then", "how": "queue"}) == {"ok": True, "queued": True}
        assert await core.call("session.send", {"id": cid, "text": "go left", "how": "steer"}) == {"ok": True, "steered": True}
        (sent,) = harness.requests("turn/steer")
        assert sent["input"][0]["text"] == "go left"
        await until(lambda: any(b.get("steer") and not b.get("pending") for p in events.of("turns") for t in p["turns"] for b in t["blocks"]))
        assert queued_texts(events, cid) in (["and then"], []) and len(harness.requests("turn/start")) == 1
        await core.call("session.stop", {"id": cid})
        await until(lambda: len(harness.requests("turn/start")) == 2, timeout=10)
        assert harness.requests("turn/start")[1]["input"][0]["text"] == "and then", "the queued message went out after the stop"
        await until(lambda: controller.state is SessionState.READY, timeout=10)
        await core.shutdown()

    run_codex(scenario())


def test_a_codex_threads_total_is_shown_from_the_cache_when_it_is_reopened(tmp_path):
    from tests.codex_support import run as run_codex

    async def scenario():
        home = tmp_path / "codex-home"
        cache = Cache(tmp_path / "cache")
        core, events, _ = real_codex_core(tmp_path, home, cache)
        await core.call("app.init")
        await until(lambda: any(m["source"] == "codex" for p in events.of("models") for m in p["models"]), timeout=10)
        cid = (await core.call("session.new", {"projectId": str((tmp_path / "proj").resolve()), "modelId": "codex:gpt-6.1-sol"}))["id"]
        await until(lambda: sessions_of(events).get(cid, {}).get("mark") == "ready", timeout=10)
        controller = core._by_cid[cid].controller
        await core.call("session.send", {"id": cid, "text": "[full] fix it"})
        await until(lambda: (total_of(events, cid) or {}).get("output") == 3000, timeout=15)
        expected = {"input": 3000, "output": 3000, "cacheWrite": None, "cacheRead": 9000}
        assert total_of(events, cid) == expected
        thread = controller.session_id
        await until(lambda: controller.state is SessionState.READY, timeout=10)
        await core.call("session.close", {"id": cid})
        await until(lambda: cache.load_session("cx:" + thread) is not None, timeout=10)
        await core.shutdown()

        again, events2, _ = real_codex_core(tmp_path, home, cache)
        await again.call("app.init")
        await until(lambda: events2.of("sidebar") and any(r.startswith("cx:") for r in events2.last("sidebar")["sessions"]), timeout=15)
        cid2 = (await again.call("session.openSaved", {"id": "cx:" + thread, "projectId": str((tmp_path / "proj").resolve()), "title": "t"}))["id"]
        await until(lambda: total_of(events2, cid2) is not None, timeout=10)
        assert total_of(events2, cid2) == expected, "known before Codex has said a word about the reopened thread"
        await until(lambda: sessions_of(events2).get(cid2, {}).get("mark") == "ready", timeout=15)
        await asyncio.sleep(0.3)
        assert total_of(events2, cid2) == expected
        await again.shutdown()

    run_codex(scenario())


def test_the_old_session_is_not_counted_again_in_the_moment_before_the_new_one_takes_its_place(tmp_path):
    """``_switch_engine`` starts the new session's series (``restart``) and opens it; until its first event the old
    controller is still the conversation's, and its figures must not be taken as a new series."""

    async def scenario():
        core, factory, events, _, codex = await ready_core(tmp_path, codex=True)
        cid = await new_session(core, events, tmp_path / "proj")
        droid = factory.sessions[-1]
        figures = {"inputTokens": 1000, "outputTokens": 100, "cacheCreationTokens": 0, "cacheReadTokens": 5000}
        droid.notify({"type": "session_token_usage_changed", "sessionId": droid.id, "tokenUsage": figures, "inclusiveTokenUsage": figures})
        await until(lambda: (total_of(events, cid) or {}).get("input") == 1000)
        conv = core._by_cid[cid]
        before = conv.ledger.total
        conv.ledger.restart()
        conv.switching = True
        conv.dirty = True
        core._schedule_flush()
        await asyncio.sleep(0.15)
        assert conv.ledger.total == before and conv.ledger.seen == {}, "nothing is fed while a change is under way"
        conv.switching = False
        await core.shutdown()

    run(scenario())


def test_a_switch_that_fails_does_not_disturb_the_total(tmp_path):
    async def scenario():
        core, factory, events, _, codex = await ready_core(tmp_path, codex=True)
        cid = await new_session(core, events, tmp_path / "proj")
        conv = core._by_cid[cid]
        conv.controller.transcript.usage = usage(input_tokens=1000, output_tokens=100, cache_read=5000)
        conv.dirty = True
        core._schedule_flush()
        await until(lambda: (total_of(events, cid) or {}).get("input") == 1000)
        codex.fail_open = DroidProcessError("codex is not signed in", exit_code=1)
        with pytest.raises(AppError):
            await core.call("session.choose", {"id": cid, "field": "model", "value": "codex:gpt-5.3-codex"})
        conv.controller.transcript.usage = usage(input_tokens=1200, output_tokens=120, cache_read=5500)
        conv.dirty = True
        core._schedule_flush()
        await until(lambda: (total_of(events, cid) or {}).get("input") == 1200)
        assert total_of(events, cid) == {"input": 1200, "output": 120, "cacheWrite": 0, "cacheRead": 5500}, "counted once, not twice"
        await core.shutdown()

    run(scenario())


def test_what_an_earlier_engine_used_is_still_in_the_total_after_a_restart(tmp_path):
    """The Codex thread of a conversation that began in Droid knows only its own tokens: the rest is the ledger's."""

    async def scenario():
        from datetime import datetime, timedelta, timezone
        from types import SimpleNamespace

        cache = Cache(tmp_path / "cache")
        core, factory, events, _, codex = await ready_core(tmp_path, codex=True, cache=cache)
        cid = await new_session(core, events, tmp_path / "proj")
        droid = factory.sessions[-1]
        await core.call("session.send", {"id": cid, "text": "hello"})
        await until(lambda: droid.active is not None)
        droid.active.push(delta("Hi", "m1"), complete("m1"), success("Hi"))
        droid.active.end()
        await until(lambda: core._by_cid[cid].controller.state is SessionState.READY)
        core._by_cid[cid].controller.transcript.usage = usage(input_tokens=1000, output_tokens=100, cache_read=5000)
        core._by_cid[cid].dirty = True
        core._schedule_flush()
        await until(lambda: (total_of(events, cid) or {}).get("input") == 1000)
        assert (await core.call("session.choose", {"id": cid, "field": "model", "value": "codex:gpt-5.3-codex"}))["switched"]
        conv = core._by_cid[cid]
        await core.call("session.send", {"id": cid, "text": "and now?"})
        await until(lambda: codex.factory.sessions[-1].active is not None)
        codex.factory.sessions[-1].active.push(delta("ok", "m2"), complete("m2"), success("ok"))
        codex.factory.sessions[-1].active.end()
        await until(lambda: conv.controller.state is SessionState.READY)
        conv.controller.transcript.usage = usage(input_tokens=300, output_tokens=30, cache_read=700)
        conv.dirty = True
        core._schedule_flush()
        await until(lambda: (total_of(events, cid) or {}).get("input") == 1300)
        thread = conv.controller.session_id
        await core.call("session.close", {"id": cid})
        await until(lambda: cache.load_session("cx:" + thread) is not None)
        await core.shutdown()

        # a new run: the Codex thread counts 300/30/700 for itself, and that is all it can say
        again, factory2, events2, _, codex2 = make_core(tmp_path, codex=True, cache=cache)
        codex2.threads = [SimpleNamespace(id=thread, title="Hello", modified_at=datetime.now(timezone.utc) - timedelta(hours=1), message_count=None)]
        codex2.factory.history[thread] = HistorySnapshot(
            messages=history_messages(), title="Hello", usage={"inputTokens": 300, "outputTokens": 30, "cacheCreationTokens": 0, "cacheReadTokens": 700}
        )
        await again.call("app.init")
        await until(lambda: events2.of("sidebar") and ("cx:" + thread) in events2.last("sidebar")["sessions"])
        cid2 = (await again.call("session.openSaved", {"id": "cx:" + thread, "projectId": str((tmp_path / "proj").resolve()), "title": "Hello"}))["id"]
        await until(lambda: sessions_of(events2).get(cid2, {}).get("mark") == "ready")
        await asyncio.sleep(0.2)
        assert total_of(events2, cid2) == {"input": 1300, "output": 130, "cacheWrite": 0, "cacheRead": 5700}, "Droid's 1000 + Codex's 300, not Codex's alone"
        await again.shutdown()

    run(scenario())


def test_the_ledger_of_a_cache_older_than_the_session_is_still_used(tmp_path):
    """The conversation shown from the cache must be as new as the session; the token ledger need not be."""

    async def scenario():
        proj = tmp_path / "proj"
        proj.mkdir()
        cache = Cache(tmp_path / "cache")
        core, factory, events, _, _ = await ready_core(tmp_path, projects=False, cache=cache)
        factory.saved = [saved_session("s-1", "Chat", minutes_ago=7)]
        factory.history["s-1"] = HistorySnapshot(messages=history_messages(), title="Chat")
        await core.call("project.add", {"path": str(proj)})
        await until(lambda: events.of("sidebar") and "s-1" in events.last("sidebar")["sessions"])
        cid = (await core.call("session.openSaved", {"id": "s-1", "projectId": str(proj), "title": "Chat"}))["id"]
        await until(lambda: sessions_of(events).get(cid, {}).get("mark") == "ready")
        core._by_cid[cid].controller.transcript.usage = usage(input_tokens=1000, output_tokens=100, cache_read=5000)
        core._by_cid[cid].dirty = True
        core._schedule_flush()
        await until(lambda: (total_of(events, cid) or {}).get("input") == 1000)
        await core.call("session.close", {"id": cid})
        await until(lambda: cache.load_session("s-1") is not None)
        await core.shutdown()

        # the session was used elsewhere afterwards: its saved time is newer than the cached conversation
        again, factory2, events2, _, _ = await ready_core(tmp_path, projects=False, cache=cache)
        factory2.saved = [saved_session("s-1", "Chat", minutes_ago=-10)]
        factory2.history["s-1"] = HistorySnapshot(messages=history_messages(), title="Chat")
        factory2.default_options["open_delay"] = 0.4
        await again.call("project.add", {"path": str(proj)})
        await until(lambda: events2.of("sidebar") and "s-1" in events2.last("sidebar")["sessions"])
        opening = asyncio.ensure_future(again.call("session.openSaved", {"id": "s-1", "projectId": str(proj), "title": "Chat"}))
        await until(lambda: any((p["patch"].get("usage") or {}).get("input") == 1000 for p in events2.of("session")), timeout=5)
        assert not any(p["all"] for p in events2.of("turns")), "the cached conversation is too old to show (a preview is sent as 'all'), its token total is not"
        await opening
        await again.shutdown()

    run(scenario())


def test_a_cache_from_before_ledgers_is_read_as_one_sessions_total(tmp_path):
    from desktop.core.app_core import AppCore
    from desktop.core.usage import Tokens

    ledger = AppCore._ledger_of({"meta": {"usage": {"input": 5, "output": 6, "cacheWrite": 0, "cacheRead": 7}}})
    assert ledger.total == Tokens(5, 6, 0, 7) and ledger.seen == {"self": Tokens(5, 6, 0, 7)}
    assert AppCore._ledger_of({}).total is None
    assert AppCore._ledger_of({"meta": {"usage": None}}).seen == {}
