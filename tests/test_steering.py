"""Saying something to a turn that is running (a steer): Droid's side, against the fake SDK session."""

from __future__ import annotations

import asyncio

import pytest
from droid_sdk import DroidError, DroidProcessError

from desktop.core import session_manager as sm
from desktop.core.attachments import image_attachment, text_attachment
from desktop.core.session_manager import SessionManager, SessionState, SteerOutcome
from desktop.core.settings import AppSettings
from desktop.core.transcript import TurnStatus, UserBlock
from desktop.core.usage import Tokens
from tests.fakes import FakeFactory, until
from tests.support import complete, delta, success, tiny_png, user_message


@pytest.fixture(autouse=True)
def quick_settling(monkeypatch):
    """A turn that ends with a steer nobody echoed waits this long before giving it back; the tests that look at the
    wait set their own, the rest must not leave a sleeping task behind when their loop closes."""
    monkeypatch.setattr(sm, "STEER_SETTLE_SECONDS", 0.02)


def run(coro):
    return asyncio.run(coro)


def make():
    factory = FakeFactory()
    return factory, SessionManager(AppSettings(max_open_sessions=4), factory)


async def running(tmp_path):
    factory, manager = make()
    controller = await manager.open_new(tmp_path)
    session = factory.sessions[-1]
    assert controller.send_prompt("start")
    await until(lambda: session.active is not None)
    return factory, controller, session


def steers(controller):
    return [b for b in controller.transcript.turns[-1].blocks if isinstance(b, UserBlock) and b.steer]


def test_a_steer_goes_to_droid_with_an_id_and_waits_until_droid_has_taken_it_in(tmp_path):
    async def scenario():
        _, controller, session = await running(tmp_path)
        events: list[str] = []
        controller.add_listener(lambda c, kind: events.append(kind))
        outcome = await controller.steer("  also check the docs  ")
        assert outcome == SteerOutcome(True)
        (message,) = session.user_messages
        assert message["text"] == "also check the docs" and message["message_id"], "droid is told the id, to recognise the echo by"
        assert "images" not in message and "files" not in message
        (block,) = steers(controller)
        assert (block.text, block.pending, block.message_id) == ("also check the docs", True, message["message_id"])
        assert controller.transcript.turns[-1].blocks[0].steer is False, "the message that began the turn is not a steer"
        assert "changed" in events

        session.active.push(user_message(message["message_id"], "also check the docs"))
        await until(lambda: not block.pending)
        assert len(steers(controller)) == 1, "the echo settles the block that is there, it does not add another"
        session.active.push(delta("done"), complete(), success("done"))
        session.active.end()
        await until(lambda: controller.state is SessionState.READY)
        assert controller.take_unadopted() == []

    run(scenario())


def test_a_steer_with_nothing_running_says_the_turn_is_over(tmp_path):
    async def scenario():
        factory, manager = make()
        controller = await manager.open_new(tmp_path)
        assert await controller.steer("hello") == SteerOutcome(False, over=True)
        assert factory.sessions[-1].user_messages == []

    run(scenario())


def test_a_steer_needs_words_or_an_attachment(tmp_path):
    async def scenario():
        _, controller, session = await running(tmp_path)
        assert (await controller.steer("   ")).ok is False and session.user_messages == []
        picture = image_attachment("p.png", tiny_png(), "image/png")
        outcome = await controller.steer("", [picture])
        assert outcome.ok and session.user_messages[0]["text"], "attachments alone get the default prompt, like a message does"

    run(scenario())


def test_a_steer_carries_its_attachments_in_the_wire_form_the_client_takes(tmp_path):
    async def scenario():
        _, controller, session = await running(tmp_path)
        picture = image_attachment("p.png", tiny_png(), "image/png")
        notes = text_attachment("notes.txt", "remember this")
        assert (await controller.steer("look", [picture, notes])).ok
        (message,) = session.user_messages
        (image,) = message["images"]
        assert image["type"] == "base64" and image["mediaType"] == "image/png" and image["data"]
        (document,) = message["files"]
        assert document["type"] == "text" and document["name"] == "notes.txt" and "remember this" in document["data"]
        (block,) = steers(controller)
        assert [a.name for a in block.attachments] == ["p.png", "notes.txt"]

    run(scenario())


def test_a_steer_droid_refuses_leaves_no_trace_and_says_why(tmp_path):
    async def scenario():
        _, controller, session = await running(tmp_path)
        session.add_message_error = DroidError("no such session")
        outcome = await controller.steer("hello")
        assert outcome.ok is False and outcome.over is False and "没有收下" in outcome.message
        assert steers(controller) == [] and controller.take_unadopted() == []

    run(scenario())


def test_a_steer_to_a_lost_connection_is_not_blamed_on_droid(tmp_path):
    async def scenario():
        _, controller, session = await running(tmp_path)
        session.add_message_error = DroidProcessError("gone", exit_code=1)
        outcome = await controller.steer("hello")
        assert outcome.ok is False and "没有收下" not in outcome.message
        assert steers(controller) == []

    run(scenario())


def test_a_steer_is_refused_while_the_turn_is_being_stopped(tmp_path):
    async def scenario():
        _, controller, session = await running(tmp_path)
        controller.transcript.mark_stopping()
        outcome = await controller.steer("hello")
        assert outcome.ok is False and "停止" in outcome.message and session.user_messages == []

    run(scenario())


def test_droid_discarding_the_queued_message_gives_the_words_back(tmp_path):
    """Measured (droid 0.251): a message waiting when the turn is stopped is dropped with ``queued_messages_discarded``."""

    async def scenario():
        _, controller, session = await running(tmp_path)
        heard: list[str] = []
        controller.add_listener(lambda c, kind: heard.append(kind))
        picture = image_attachment("p.png", tiny_png(), "image/png")
        assert (await controller.steer("never mind", [picture])).ok
        session.notify({"type": "queued_messages_discarded", "text": "never mind", "requestId": "r1"})
        assert steers(controller) == [], "it was never part of the conversation"
        assert "steer" in heard
        ((text, attachments),) = controller.take_unadopted()
        assert text == "never mind" and [a.name for a in attachments] == ["p.png"]
        assert controller.take_unadopted() == [], "once"

    run(scenario())


def test_a_discard_of_some_other_message_changes_nothing(tmp_path):
    async def scenario():
        _, controller, session = await running(tmp_path)
        assert (await controller.steer("keep me")).ok
        session.notify({"type": "queued_messages_discarded", "text": "something else", "requestId": "r1"})
        assert len(steers(controller)) == 1 and controller.take_unadopted() == []

    run(scenario())


def test_a_steer_nobody_echoed_when_the_turn_ends_goes_back_after_a_moment(tmp_path, monkeypatch):
    monkeypatch.setattr(sm, "STEER_SETTLE_SECONDS", 0.05)

    async def scenario():
        _, controller, session = await running(tmp_path)
        heard: list[str] = []
        controller.add_listener(lambda c, kind: heard.append(kind))
        assert (await controller.steer("too late")).ok
        session.active.push(delta("done"), complete(), success("done"))
        session.active.end()
        await until(lambda: controller.state is SessionState.READY)
        await until(lambda: "steer" in heard)
        assert steers(controller) == []
        ((text, _),) = controller.take_unadopted()
        assert text == "too late"
        assert controller.transcript.turns[-1].status is TurnStatus.DONE

    run(scenario())


def test_droid_running_the_steer_as_a_turn_of_its_own_is_stopped_and_the_words_go_back(tmp_path, monkeypatch):
    """It reached droid in the instant the turn ended: droid started a turn for it that no stream follows."""
    monkeypatch.setattr(sm, "STEER_SETTLE_SECONDS", 0.3)

    async def scenario():
        _, controller, session = await running(tmp_path)
        assert (await controller.steer("raced")).ok
        (message,) = session.user_messages
        session.active.push(delta("done"), complete(), success("done"))
        session.active.end()
        await until(lambda: controller.state is SessionState.READY)
        session.notify({"type": "create_message", "message": {"role": "user", "id": message["message_id"], "content": []}})
        await until(lambda: session.interrupts >= 1)
        await until(lambda: controller.take_unadopted() != [] or session.interrupts)  # (given back within the settle time)
        await asyncio.sleep(0.4)
        assert steers(controller) == []

    run(scenario())


def test_the_echo_during_a_running_turn_is_not_an_orphan(tmp_path):
    async def scenario():
        _, controller, session = await running(tmp_path)
        assert (await controller.steer("fine")).ok
        (message,) = session.user_messages
        session.notify({"type": "create_message", "message": {"role": "user", "id": message["message_id"], "content": []}})
        await asyncio.sleep(0.05)
        assert session.interrupts == 0 and len(steers(controller)) == 1

    run(scenario())


def test_a_lost_connection_gives_the_waiting_steers_back(tmp_path):
    async def scenario():
        _, controller, session = await running(tmp_path)
        assert (await controller.steer("one")).ok
        controller._die(DroidProcessError("crashed", exit_code=3))
        ((text, _),) = controller.take_unadopted()
        assert text == "one"
        assert controller.state is SessionState.DEAD

    run(scenario())


def test_droids_inclusive_total_and_the_saved_one_make_the_session_total(tmp_path):
    """The session's figure is the largest of the three sources: they are running totals of the same session."""

    async def scenario():
        factory, manager = make()
        factory.history["old"] = sm.HistorySnapshot(
            messages=[], title=None, usage={"inputTokens": 5, "outputTokens": 6, "cacheCreationTokens": 7, "cacheReadTokens": 8}
        )
        controller = await manager.open_saved(tmp_path, "old")
        assert controller.usage_series() == {"self": Tokens(5, 6, 7, 8)}, "known from the saved session before anything is said"
        session = factory.sessions[-1]
        session.notify(
            {
                "type": "session_token_usage_changed",
                "sessionId": "old",
                "tokenUsage": {"inputTokens": 6, "outputTokens": 6, "cacheCreationTokens": 7, "cacheReadTokens": 9},
                "inclusiveTokenUsage": {"inputTokens": 9, "outputTokens": 10, "cacheCreationTokens": 7, "cacheReadTokens": 20},
            }
        )
        assert controller.usage_series() == {"self": Tokens(9, 10, 7, 20)}, "with its sub-agents"
        session.notify({"type": "session_token_usage_changed", "sessionId": "another", "tokenUsage": {"inputTokens": 999, "outputTokens": 0, "cacheCreationTokens": 0, "cacheReadTokens": 0}})
        assert controller.usage_series() == {"self": Tokens(9, 10, 7, 20)}, "another session's figure is not this one's"

    run(scenario())


def test_a_session_nobody_has_counted_for_has_no_series(tmp_path):
    async def scenario():
        _, manager = make()
        controller = await manager.open_new(tmp_path)
        assert controller.usage_series() == {}

    run(scenario())
