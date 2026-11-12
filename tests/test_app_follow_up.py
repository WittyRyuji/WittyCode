"""Messages typed while a turn runs: queued behind it or given to it (core level), and the token total.

The turns run on the own loop against the loopback ChatGPT Plan fake: each turn holds (``("hold", "t1")``) until the test
lets it end, so messages can be typed while it runs."""

from __future__ import annotations

import asyncio
import base64

import pytest

from desktop.core.app_core import AppError
from desktop.core.cache import Cache
from desktop.core.session_state import SessionState, SteerOutcome
from desktop.core.settings import FOLLOW_UPS, AppSettings, settings_from_dict
from tests.app_support import ready_core
from tests.devin_support import make_engine as make_devin
from tests.fake_responses import fail, say, usage
from tests.fakes import until
from tests.support import tiny_png


def run(coro):
    return asyncio.run(coro)


def sessions_of(events):
    merged: dict[str, dict] = {}
    for payload in events.of("session"):
        merged.setdefault(payload["id"], {}).update(payload["patch"])
    return merged


async def new_session(core, events, project, **more):
    cid = (await core.call("session.new", {"projectId": str(project), **more}))["id"]
    await until(lambda: any(p["id"] == cid and p["patch"].get("mark") == "ready" for p in events.of("session")))
    return cid


def queued_texts(events, cid):
    return [m["text"] for m in sessions_of(events).get(cid, {}).get("queued") or []]


def queue_ids(events, cid):
    return [m["id"] for m in sessions_of(events)[cid]["queued"]]


def user_parts(request):
    messages = [item for item in request["input"] if item.get("type") == "message" and item.get("role") == "user"]
    return messages[-1]["content"]


def user_text(request):
    return user_parts(request)[0]["text"]


class Turns:
    """The turns of a test conversation: each one holds until ``end`` lets it finish with an answer."""

    def __init__(self, plan, count: int = 4) -> None:
        self.plan = plan
        self.ended = 0
        for n in range(1, count + 1):
            plan.script([("hold", f"t{n}"), *say(f"answer {n}", item_id=f"msg_{n}")])

    def end(self) -> None:
        self.ended += 1
        self.plan.release(f"t{self.ended}")

    @property
    def prompts(self) -> list[str]:
        return [user_text(request) for request in self.plan.requests]


async def running_turn(tmp_path, **options):
    core, plan, events, _, _ = await ready_core(tmp_path, **options)
    turns = Turns(plan)
    cid = await new_session(core, events, tmp_path / "proj")
    await core.call("session.send", {"id": cid, "text": "first"})
    await until(lambda: len(plan.requests) == 1 and core._by_cid[cid].controller.state is SessionState.RUNNING)
    return core, turns, events, cid


async def stop_all(core, turns) -> None:
    for n in range(turns.ended + 1, 6):
        turns.plan.release(f"t{n}")
    await core.shutdown()


def blocks_of_last_turn(events):
    """The blocks of the turn the page was last sent a change of (an event may carry no turn at all)."""
    for payload in reversed(events.of("turns")):
        if payload["turns"]:
            return payload["turns"][-1]["blocks"]
    return []


def refused(message="busy"):
    async def steer(text, attachments=()):
        return SteerOutcome(False, message=message)

    return steer


# ----------------------------------------------------------------------------- queued behind the turn


def test_messages_typed_while_a_turn_runs_wait_in_line_and_each_goes_out_as_its_own_turn(tmp_path):
    async def scenario():
        core, turns, events, cid = await running_turn(tmp_path)
        assert await core.call("session.send", {"id": cid, "text": "second", "how": "queue"}) == {"ok": True, "queued": True}
        assert await core.call("session.send", {"id": cid, "text": "third"}) == {"ok": True, "queued": True}, "no word on how: queued"
        await until(lambda: queued_texts(events, cid) == ["second", "third"])
        assert turns.prompts == ["first"], "nothing goes out while the turn runs"

        turns.end()
        await until(lambda: turns.prompts == ["first", "second"])
        await until(lambda: queued_texts(events, cid) == ["third"])
        turns.end()
        await until(lambda: turns.prompts == ["first", "second", "third"])
        await until(lambda: queued_texts(events, cid) == [])
        turns.end()
        await until(lambda: core._by_cid[cid].controller.state is SessionState.READY)
        transcript_turns = core._by_cid[cid].controller.transcript.turns
        assert [t.blocks[0].text for t in transcript_turns] == ["first", "second", "third"], "three turns, in the order they were typed"
        await stop_all(core, turns)

    run(scenario())


def test_a_message_that_arrives_ahead_of_waiting_ones_keeps_its_place(tmp_path):
    async def scenario():
        core, turns, events, cid = await running_turn(tmp_path)
        await core.call("session.send", {"id": cid, "text": "second"})
        turns.end()
        # the turn is over, "second" is on its way out: a message typed now is behind it, not in front
        await core.call("session.send", {"id": cid, "text": "third"})
        await until(lambda: turns.prompts[:2] == ["first", "second"])
        await until(lambda: queued_texts(events, cid) == ["third"])
        await stop_all(core, turns)

    run(scenario())


def test_stopping_a_turn_still_sends_what_was_queued_behind_it(tmp_path):
    async def scenario():
        core, turns, events, cid = await running_turn(tmp_path)
        await core.call("session.send", {"id": cid, "text": "instead, do this"})
        await core.call("session.stop", {"id": cid})
        await until(lambda: turns.prompts == ["first", "instead, do this"], timeout=10)
        await stop_all(core, turns)

    run(scenario())


def test_a_failed_turn_does_not_send_what_was_queued_it_comes_back_to_the_composer(tmp_path):
    async def scenario():
        core, plan, events, _, _ = await ready_core(tmp_path)
        plan.script([("hold", "t1"), *fail("invalid_request_error", "the model said no")])
        cid = await new_session(core, events, tmp_path / "proj")
        await core.call("session.send", {"id": cid, "text": "first"})
        await until(lambda: len(plan.requests) == 1)
        await core.call("session.send", {"id": cid, "text": "second"})
        await core.call("session.send", {"id": cid, "text": "third"})
        events.clear()
        plan.release("t1")
        await until(lambda: events.of("restore"), timeout=5)
        assert events.last("restore")["text"] == "second\n\nthird", "in the order they were typed"
        assert any("上一轮没有成功" in n["text"] for n in events.of("notice"))
        assert len(plan.requests) == 1, "nothing was sent behind its back"
        await until(lambda: queued_texts(events, cid) == [])
        await core.shutdown()

    run(scenario())


def test_one_queued_message_can_be_taken_back_and_the_rest_stay(tmp_path):
    async def scenario():
        core, turns, events, cid = await running_turn(tmp_path)
        await core.call("session.send", {"id": cid, "text": "second"})
        await core.call("session.send", {"id": cid, "text": "third"})
        await until(lambda: queued_texts(events, cid) == ["second", "third"])
        (first, second) = queue_ids(events, cid)
        assert await core.call("session.unqueue", {"id": cid, "queueId": "nope"}) == {"ok": False}
        with pytest.raises(AppError):
            await core.call("session.unqueue", {"id": cid, "queueId": 7})
        assert await core.call("session.unqueue", {"id": cid, "queueId": first}) == {"ok": True}
        assert events.last("restore")["text"] == "second"
        await until(lambda: queued_texts(events, cid) == ["third"])
        assert await core.call("session.unqueue", {"id": cid, "queueId": first}) == {"ok": False}, "already back"
        turns.end()
        await until(lambda: turns.prompts == ["first", "third"])
        await stop_all(core, turns)

    run(scenario())


def test_a_waiting_message_can_be_reworded_and_keeps_its_place_and_its_attachments(tmp_path):
    async def scenario():
        core, turns, events, cid = await running_turn(tmp_path)
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
        turns.end()
        await until(lambda: turns.prompts == ["first", "second, reworded"])
        turns.end()
        await until(lambda: turns.prompts == ["first", "second, reworded", "third, reworded"])
        assert [part["type"] for part in user_parts(turns.plan.requests[2])].count("input_image") == 1
        await stop_all(core, turns)

    run(scenario())


def test_editing_a_waiting_message_is_checked_and_a_message_that_has_gone_is_not_an_error(tmp_path):
    async def scenario():
        core, turns, events, cid = await running_turn(tmp_path)
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
        await stop_all(core, turns)

    run(scenario())


def test_without_a_queue_id_unqueue_takes_all_of_them_back(tmp_path):
    async def scenario():
        core, turns, events, cid = await running_turn(tmp_path)
        await core.call("session.send", {"id": cid, "text": "second"})
        await core.call("session.send", {"id": cid, "text": "third"})
        assert await core.call("session.unqueue", {"id": cid}) == {"ok": True}
        assert events.last("restore")["text"] == "second\n\nthird"
        assert await core.call("session.unqueue", {"id": cid}) == {"ok": False}
        await until(lambda: queued_texts(events, cid) == [])
        turns.end()
        await until(lambda: core._by_cid[cid].controller.state is SessionState.READY)
        assert turns.prompts == ["first"]
        await stop_all(core, turns)

    run(scenario())


def test_a_queued_message_keeps_its_attachments_and_gets_them_back_when_withdrawn(tmp_path):
    async def scenario():
        core, turns, events, cid = await running_turn(tmp_path)
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
        await stop_all(core, turns)

    run(scenario())


def test_a_queued_message_with_a_picture_goes_out_with_it(tmp_path):
    async def scenario():
        core, turns, events, cid = await running_turn(tmp_path)
        png = base64.b64encode(tiny_png()).decode()
        await core.call("attachments.add", {"id": cid, "files": [{"name": "shot.png", "type": "image/png", "data": png}]})
        await core.call("session.send", {"id": cid, "text": "see this"})
        turns.end()
        await until(lambda: len(turns.plan.requests) == 2)
        assert [part["type"] for part in user_parts(turns.plan.requests[1])].count("input_image") == 1
        await stop_all(core, turns)

    run(scenario())


def test_closing_a_conversation_with_messages_waiting_says_they_were_not_sent(tmp_path):
    async def scenario():
        core, turns, events, cid = await running_turn(tmp_path)
        await core.call("session.send", {"id": cid, "text": "second"})
        await core.call("session.send", {"id": cid, "text": "third"})
        events.clear()
        await core.call("session.close", {"id": cid})
        assert any("2 条排队的消息没有发出" in n["text"] for n in events.of("notice"))
        await stop_all(core, turns)

    run(scenario())


def test_what_is_waiting_when_a_session_closes_is_told_not_lost(tmp_path):
    async def scenario():
        core, turns, events, cid = await running_turn(tmp_path)
        await core.call("session.send", {"id": cid, "text": "second"})
        conv = core._by_cid[cid]
        conv.queue.append(type(conv.queue[0])("q9", "also", ()))
        conv.controller.state = SessionState.CLOSED
        events.clear()
        core._drop(conv, conv.controller)
        assert any("second" in n["text"] and "also" in n["text"] for n in events.of("notice"))
        await stop_all(core, turns)

    run(scenario())


# ----------------------------------------------------------------------------- given to the running turn


def test_a_steer_is_given_to_the_running_turn_and_shown_where_it_was_said(tmp_path):
    async def scenario():
        core, turns, events, cid = await running_turn(tmp_path)
        result = await core.call("session.send", {"id": cid, "text": "also the docs", "how": "steer"})
        assert result == {"ok": True, "steered": True}
        await until(lambda: any(b.get("steer") for b in blocks_of_last_turn(events)))
        steer = next(b for b in blocks_of_last_turn(events) if b.get("steer"))
        assert steer["text"] == "also the docs" and steer["pending"] is True and steer["kind"] == "user"
        assert queued_texts(events, cid) == [], "it is not waiting in line: the turn has it"

        turns.end()
        await until(lambda: len(turns.plan.requests) == 2)
        assert turns.prompts[1] == "also the docs", "the loop takes it into its next request"
        await until(lambda: not next(b for b in blocks_of_last_turn(events) if b.get("steer")).get("pending"))
        turns.end()
        await until(lambda: core._by_cid[cid].controller.state is SessionState.READY)
        assert len(core._by_cid[cid].controller.transcript.turns) == 1, "a steer does not start a turn"
        await stop_all(core, turns)

    run(scenario())


def test_a_steer_the_turn_refuses_is_queued_instead_and_the_user_is_told(tmp_path):
    async def scenario():
        core, turns, events, cid = await running_turn(tmp_path)
        core._by_cid[cid].controller.steer = refused()
        events.clear()
        result = await core.call("session.send", {"id": cid, "text": "please", "how": "steer"})
        assert result == {"ok": True, "queued": True}
        assert any("没能引导本轮" in n["text"] and "排队" in n["text"] for n in events.of("notice"))
        await until(lambda: queued_texts(events, cid) == ["please"])
        await stop_all(core, turns)

    run(scenario())


def test_how_is_ignored_when_no_turn_is_running(tmp_path):
    async def scenario():
        core, plan, events, _, _ = await ready_core(tmp_path)
        plan.script(say("hi"))
        cid = await new_session(core, events, tmp_path / "proj")
        assert await core.call("session.send", {"id": cid, "text": "hello", "how": "steer"}) == {"ok": True}
        await until(lambda: len(plan.requests) == 1 and core._by_cid[cid].controller.state is SessionState.READY)
        assert user_text(plan.requests[0]) == "hello" and len(core._by_cid[cid].controller.transcript.turns) == 1
        await core.shutdown()

    run(scenario())


def test_how_must_be_one_of_the_two(tmp_path):
    async def scenario():
        core, turns, events, cid = await running_turn(tmp_path)
        for bad in ("now", 3, ["steer"], ""):
            with pytest.raises(AppError, match="发送方式不合法"):
                await core.call("session.send", {"id": cid, "text": "x", "how": bad})
        assert queued_texts(events, cid) == [] and not core._by_cid[cid].controller._steers
        await stop_all(core, turns)

    run(scenario())


def test_a_steer_for_a_turn_that_has_just_ended_goes_out_as_an_ordinary_message(tmp_path):
    async def scenario():
        core, turns, events, cid = await running_turn(tmp_path)
        controller = core._by_cid[cid].controller

        async def too_late(text, attachments=()):
            turns.end()  # the turn ended while the steer was on its way
            await until(lambda: controller.state is SessionState.READY)
            return SteerOutcome(False, over=True)

        controller.steer = too_late
        result = await core.call("session.send", {"id": cid, "text": "later then", "how": "steer"})
        assert result == {"ok": True}, "not queued, not an error: the user pressed send"
        await until(lambda: turns.prompts == ["first", "later then"])
        assert queued_texts(events, cid) == []
        await stop_all(core, turns)

    run(scenario())


def test_a_waiting_message_can_be_sent_now_to_the_turn_and_the_others_stay_in_line(tmp_path):
    async def scenario():
        core, turns, events, cid = await running_turn(tmp_path)
        await core.call("session.send", {"id": cid, "text": "second"})
        await core.call("session.send", {"id": cid, "text": "third"})
        await until(lambda: queued_texts(events, cid) == ["second", "third"])
        first, second = queue_ids(events, cid)
        assert await core.call("session.sendQueued", {"id": cid, "queueId": second}) == {"ok": True, "steered": True}
        await until(lambda: queued_texts(events, cid) == ["second"])
        await until(lambda: any(b.get("steer") and b["text"] == "third" for b in blocks_of_last_turn(events)))
        assert await core.call("session.sendQueued", {"id": cid, "queueId": second}) == {"ok": False}, "already gone"
        turns.end()
        await until(lambda: turns.prompts[:2] == ["first", "third"], timeout=10)
        turns.end()
        await until(lambda: turns.prompts[:3] == ["first", "third", "second"], timeout=10)
        assert turns.prompts.count("third") == 1, "'third' was not sent again as a turn of its own"
        await stop_all(core, turns)

    run(scenario())


def test_a_message_sent_now_as_the_turn_ends_is_the_next_message_and_not_lost(tmp_path):
    async def scenario():
        core, turns, events, cid = await running_turn(tmp_path)
        await core.call("session.send", {"id": cid, "text": "second"})
        await core.call("session.send", {"id": cid, "text": "third"})
        await until(lambda: queued_texts(events, cid) == ["second", "third"])
        first, second = queue_ids(events, cid)

        async def too_late(text, attachments=()):
            return SteerOutcome(False, over=True)

        core._by_cid[cid].controller.steer = too_late  # (the turn ended while the message was on its way)
        assert await core.call("session.sendQueued", {"id": cid, "queueId": second}) == {"ok": True}
        assert [m.text for m in core._by_cid[cid].queue] == ["third", "second"], "ahead of the other one: it was asked for now"
        turns.end()
        await until(lambda: turns.prompts[:2] == ["first", "third"])
        await stop_all(core, turns)

    run(scenario())


def test_sending_now_a_message_the_turn_cannot_take_leaves_it_where_it_was_and_says_so(tmp_path):
    async def scenario():
        core, turns, events, cid = await running_turn(tmp_path)
        await core.call("session.send", {"id": cid, "text": "second"})
        await core.call("session.send", {"id": cid, "text": "third"})
        await until(lambda: queued_texts(events, cid) == ["second", "third"])
        first, second = queue_ids(events, cid)
        core._by_cid[cid].controller.steer = refused()
        events.clear()
        assert await core.call("session.sendQueued", {"id": cid, "queueId": first}) == {"ok": False}
        assert any("没能引导本轮" in n["text"] and "还在排队" in n["text"] for n in events.of("notice"))
        assert [m.id for m in core._by_cid[cid].queue] == [first, second], "the same place in the line"
        with pytest.raises(AppError):
            await core.call("session.sendQueued", {"id": cid, "queueId": 7})
        await stop_all(core, turns)

    run(scenario())


def test_a_message_sent_now_while_the_session_is_still_connecting_waits_and_the_user_is_told(tmp_path):
    async def scenario():
        harness = make_devin(tmp_path, FAKE_DEVIN_OPEN_DELAY="1.0")
        core, _, events, _, _ = await ready_core(tmp_path, devin_engine=harness.engine)
        harness.engine.settings = core.settings
        opening = asyncio.ensure_future(core.call("session.new", {"projectId": str(tmp_path / "proj"), "modelId": "devin:swe-2-high"}))
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
        await until(lambda: [r["prompt"][0]["text"] for r in harness.requests("session/prompt")] == ["early"], timeout=10)
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


async def say_and_wait(core, events, cid, text):
    turns = len(core._by_cid[cid].controller.transcript.turns)
    await core.call("session.send", {"id": cid, "text": text})
    await until(lambda: len(core._by_cid[cid].controller.transcript.turns) == turns + 1 and core._by_cid[cid].controller.state is SessionState.READY, timeout=10)


def test_the_total_adds_up_what_the_engine_counts_turn_after_turn(tmp_path):
    async def scenario():
        core, plan, events, _, _ = await ready_core(tmp_path)
        plan.script(say("one", used=usage(1000, 100)), say("two", used=usage(500, 80)))
        cid = await new_session(core, events, tmp_path / "proj")
        await say_and_wait(core, events, cid, "hello")
        await until(lambda: (total_of(events, cid) or {}).get("input") == 1000)
        await say_and_wait(core, events, cid, "again")
        await until(lambda: (total_of(events, cid) or {}).get("input") == 1500)
        assert total_of(events, cid)["output"] == 180, "each request counted once"
        await core.shutdown()

    run(scenario())


def test_the_total_goes_on_after_the_session_is_closed_and_opened_again(tmp_path):
    async def scenario():
        proj = tmp_path / "proj"
        proj.mkdir()
        cache = Cache(tmp_path / "cache")
        core, plan, events, _, _ = await ready_core(tmp_path, projects=False, cache=cache)
        plan.script(say("one", used=usage(1000, 100)))
        await core.call("project.add", {"path": str(proj)})
        cid = await new_session(core, events, proj)
        await say_and_wait(core, events, cid, "hello")
        await until(lambda: (total_of(events, cid) or {}).get("input") == 1000)
        native = core._by_cid[cid].controller.session_id
        await core.call("session.close", {"id": cid})
        await until(lambda: cache.load_session(f"cg:{native}") is not None)
        await core.shutdown()

        again, plan2, events2, _, _ = await ready_core(tmp_path, projects=False, cache=cache)
        await again.call("project.add", {"path": str(proj)})
        await until(lambda: events2.of("sidebar") and f"cg:{native}" in events2.last("sidebar")["sessions"])
        cid2 = (await again.call("session.openSaved", {"id": f"cg:{native}", "projectId": str(proj), "title": "Chat"}))["id"]
        await until(lambda: sessions_of(events2).get(cid2, {}).get("mark") == "ready")
        await until(lambda: total_of(events2, cid2) is not None)
        await asyncio.sleep(0.2)
        assert (total_of(events2, cid2)["input"], total_of(events2, cid2)["output"]) == (1000, 100), "the same figure, not counted twice"
        await again.shutdown()

    run(scenario())


def test_an_engine_change_keeps_the_total_and_goes_on_counting_with_the_new_engine(tmp_path):
    async def scenario():
        harness = make_devin(tmp_path)
        core, plan, events, _, _ = await ready_core(tmp_path, devin_engine=harness.engine)
        harness.engine.settings = core.settings
        plan.script(say("Hi", used=usage(1000, 100)))
        cid = await new_session(core, events, tmp_path / "proj")
        await say_and_wait(core, events, cid, "hello")
        await until(lambda: (total_of(events, cid) or {}).get("input") == 1000)

        assert (await core.call("session.choose", {"id": cid, "field": "model", "value": "devin:swe-2-high"}))["switched"]
        assert (total_of(events, cid)["input"], total_of(events, cid)["output"]) == (1000, 100), "the old session's tokens stay"
        await core.call("session.send", {"id": cid, "text": "and now?"})
        # the fake Devin counts 900 input, 12 output and a 300 cache read for a turn: added to what the own loop counted
        await until(lambda: (total_of(events, cid) or {}).get("input") == 1900, timeout=10)
        assert total_of(events, cid)["output"] == 112 and total_of(events, cid)["cacheRead"] == 300
        await core.shutdown()

    run(scenario())


def test_the_old_session_is_not_counted_again_in_the_moment_before_the_new_one_takes_its_place(tmp_path):
    """``_switch_engine`` starts the new session's series (``restart``) and opens it; until its first event the old
    controller is still the conversation's, and its figures must not be taken as a new series."""

    async def scenario():
        core, plan, events, _, _ = await ready_core(tmp_path)
        plan.script(say("Hi", used=usage(1000, 100)))
        cid = await new_session(core, events, tmp_path / "proj")
        await say_and_wait(core, events, cid, "hello")
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
        harness = make_devin(tmp_path, FAKE_DEVIN_OPEN_FAIL="1")
        core, plan, events, _, _ = await ready_core(tmp_path, devin_engine=harness.engine)
        harness.engine.settings = core.settings
        plan.script(say("one", used=usage(1000, 100)), say("two", used=usage(200, 20)))
        cid = await new_session(core, events, tmp_path / "proj")
        await say_and_wait(core, events, cid, "hello")
        await until(lambda: (total_of(events, cid) or {}).get("input") == 1000)
        with pytest.raises(AppError):
            await core.call("session.choose", {"id": cid, "field": "model", "value": "devin:swe-2-high"})
        assert core._by_cid[cid].engine == "chatgpt", "the conversation stays where it was"
        await say_and_wait(core, events, cid, "again")
        await until(lambda: (total_of(events, cid) or {}).get("input") == 1200)
        assert total_of(events, cid)["output"] == 120, "counted once, not twice"
        await core.shutdown()

    run(scenario())


def test_a_cache_from_before_ledgers_is_read_as_one_sessions_total(tmp_path):
    from desktop.core.app_core import AppCore
    from desktop.core.usage import Tokens

    ledger = AppCore._ledger_of({"meta": {"usage": {"input": 5, "output": 6, "cacheWrite": 0, "cacheRead": 7}}})
    assert ledger.total == Tokens(5, 6, 0, 7) and ledger.seen == {"self": Tokens(5, 6, 0, 7)}
    assert AppCore._ledger_of({}).total is None
    assert AppCore._ledger_of({"meta": {"usage": None}}).seen == {}
