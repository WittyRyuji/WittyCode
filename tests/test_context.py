"""The context meter: Droid's figures, how they are worded and how a session controller keeps them current (fake sessions)."""

from __future__ import annotations

import asyncio
import dataclasses
from types import SimpleNamespace

import pytest
from droid_sdk import ContextUsage, DroidError, DroidProcessError, Session
from droid_sdk.client import DroidClient
from droid_sdk.schemas.client import GetContextBreakdownResult

from desktop.core.context import ContextState, format_tokens, last_call_figure, plausible, read_context
from desktop.core.session_manager import SessionManager, SessionState
from desktop.core.settings import AppSettings
from tests.fakes import FakeFactory, FakeSession, until
from tests.support import success

USAGE = {"type": "session_token_usage_changed", "lastCallTokenUsage": {"inputTokens": 3464, "outputTokens": 27, "cacheReadTokens": 14492}}
COMPACTED = {"type": "session_compacted", "summaryId": "s", "removedCount": 3, "visibleBoundaryMessageId": None}


def run(coro):
    return asyncio.run(coro)


def make(**options):
    factory = FakeFactory()
    factory.default_options.update(options)
    return factory, SessionManager(AppSettings(max_open_sessions=4), factory)


def bare_session(**options) -> FakeSession:
    return FakeSession(SimpleNamespace(), session_id="bare", **options)


async def settled(controller, expected: ContextState | None = None, **fields) -> None:
    """Wait until the controller's context reading has caught up (to ``expected``, or to these field values)."""

    def reached() -> bool:
        context = controller.context
        if expected is not None:
            return context == expected
        return all(getattr(context, name) == value for name, value in fields.items())

    await until(reached)


# ---------------------------------------------------------------------------------------------- words


@pytest.mark.parametrize(
    ("count", "text"),
    [
        (0, "0"),
        (850, "850"),
        (999, "999"),
        (1000, "1K"),
        (2500, "2.5K"),
        (9949, "9.9K"),
        (9999, "10K"),
        (17983, "18K"),
        (25431, "25K"),
        (196608, "197K"),
        (250000, "250K"),
        (872000, "872K"),
        (922000, "922K"),
        (999499, "999K"),
        (999500, "1M"),
        (1_000_000, "1M"),
        (1_240_000, "1.2M"),
    ],
)
def test_token_counts_are_written_compactly(count, text):
    assert format_tokens(count) == text


# ---------------------------------------------------------------------------------------------- what the SDK gives


def test_the_sdk_still_offers_what_the_context_reading_relies_on():
    """``read_context`` reaches the breakdown through a private door of the pinned SDK: an upgrade must not close it unnoticed."""
    assert callable(getattr(Session, "_require_client", None))
    assert callable(getattr(DroidClient, "get_context_breakdown", None))
    assert {"model_id", "context_budget", "last_call_compaction_tokens"} <= set(GetContextBreakdownResult.model_fields)
    assert "limit" in {field.name for field in dataclasses.fields(ContextUsage)}
    assert callable(Session.context) and callable(Session.on_notification)


def test_reading_a_session_gives_window_budget_and_the_last_call():
    async def scenario():
        session = bare_session(last_call_tokens=17983)
        assert await read_context(session) == ContextState(used=17983, window=872000, budget=250000, model="claude-haiku-5-5")
        session.last_call_tokens = None
        assert (await read_context(session)).used is None, "nothing recorded yet is unknown, not zero"
        session.last_call_tokens = 0
        assert (await read_context(session)).used is None

    run(scenario())


def test_without_the_breakdown_only_the_window_is_known():
    async def scenario():
        session = bare_session(last_call_tokens=17983)
        session.breakdown_error = DroidError("no such request")
        assert await read_context(session) == ContextState(window=872000)
        session._require_client = None  # the private door is gone altogether
        assert await read_context(session) == ContextState(window=872000)

    run(scenario())


def test_a_session_that_cannot_answer_raises_instead_of_guessing():
    async def scenario():
        session = bare_session()
        session.context_error = DroidProcessError("gone", exit_code=1)
        with pytest.raises(DroidProcessError):
            await read_context(session)

    run(scenario())


# ---------------------------------------------------------------------------------------------- the controller


def test_a_new_session_knows_its_window_but_not_what_it_holds(tmp_path):
    async def scenario():
        factory, manager = make()
        controller = await manager.open_new(tmp_path)
        await settled(controller, ContextState(window=872000, budget=250000, model="claude-haiku-5-5"))

    run(scenario())


def test_a_resumed_session_shows_what_droid_recorded_before_any_new_call(tmp_path):
    async def scenario():
        factory, manager = make(last_call_tokens=17983)
        controller = await manager.open_saved(tmp_path, "saved-1")
        await settled(controller, used=17983, window=872000)

    run(scenario())


def test_the_figure_is_read_again_when_a_turn_ends(tmp_path):
    async def scenario():
        factory, manager = make()
        controller = await manager.open_new(tmp_path)
        await settled(controller, window=872000)
        session = factory.sessions[-1]
        _, stream = await start_turn(controller, session)
        session.last_call_tokens = 17657
        stream.push(success("ok", 1.0))
        stream.end()
        await until(lambda: controller.state is SessionState.READY)
        await settled(controller, used=17657)

    run(scenario())


def test_a_model_call_during_a_turn_updates_the_figure_without_waiting_for_the_end(tmp_path):
    async def scenario():
        factory, manager = make()
        controller = await manager.open_new(tmp_path)
        session = factory.sessions[-1]
        _, stream = await start_turn(controller, session)
        for tokens in (17657, 31002):
            session.last_call_tokens = tokens
            session.notify(USAGE)
            await settled(controller, used=tokens)
            assert controller.state is SessionState.RUNNING
        stream.push(success("ok", 1.0))
        stream.end()
        await until(lambda: controller.state is SessionState.READY)

    run(scenario())


def test_changing_the_model_changes_the_window_and_the_budget(tmp_path):
    async def scenario():
        factory, manager = make(last_call_tokens=40000)
        controller = await manager.open_saved(tmp_path, "saved-1")
        await settled(controller, window=872000, budget=250000)
        assert await controller.update_settings(model="kimi-k3")
        await settled(controller, window=196608, budget=196608, used=40000)

    run(scenario())


def test_compaction_clears_the_figure_at_once_and_a_stale_reading_does_not_bring_it_back(tmp_path):
    async def scenario():
        factory, manager = make(last_call_tokens=250000)
        controller = await manager.open_saved(tmp_path, "saved-1")
        session = factory.sessions[-1]
        await settled(controller, used=250000)
        events: list[str] = []
        controller.add_listener(lambda _controller, kind: events.append(kind))

        session.notify(COMPACTED)
        assert controller.context.used is None and controller.context.compacted, "not a moment with the old figure on screen"
        assert "context" in events
        await until(lambda: session.context_reads >= 2)
        await asyncio.sleep(0.05)
        assert controller.context.used is None and controller.context.compacted, "droid still reports the pre-compaction figure"

        session.last_call_tokens = 18000  # the first call after the compaction
        session.notify(USAGE)
        await settled(controller, used=18000, compacted=False)

    run(scenario())


def test_a_figure_droid_reports_right_after_the_compaction_is_taken_over(tmp_path):
    async def scenario():
        factory, manager = make(last_call_tokens=250000)
        controller = await manager.open_saved(tmp_path, "saved-1")
        session = factory.sessions[-1]
        await settled(controller, used=250000)
        session.last_call_tokens = 18000
        session.notify(COMPACTED)
        await settled(controller, used=18000, compacted=False)

    run(scenario())


def test_after_a_compaction_droid_may_report_nothing_until_the_next_call(tmp_path):
    async def scenario():
        factory, manager = make(last_call_tokens=250000)
        controller = await manager.open_saved(tmp_path, "saved-1")
        session = factory.sessions[-1]
        await settled(controller, used=250000)
        session.last_call_tokens = None
        session.notify(COMPACTED)
        await until(lambda: session.context_reads >= 2)
        await asyncio.sleep(0.05)
        assert controller.context.used is None and controller.context.compacted

    run(scenario())


def test_a_reading_that_fails_leaves_the_figure_unknown_rather_than_out_of_date(tmp_path):
    async def scenario():
        factory, manager = make(last_call_tokens=30000)
        controller = await manager.open_saved(tmp_path, "saved-1")
        session = factory.sessions[-1]
        await settled(controller, used=30000)
        session.context_error = DroidError("busy")
        session.notify(USAGE)
        await settled(controller, used=None)
        assert controller.context.window == 872000, "what does not change is kept"
        session.context_error = None
        session.notify(USAGE)
        await settled(controller, used=30000)

    run(scenario())


def test_a_session_without_a_breakdown_still_shows_its_window(tmp_path):
    async def scenario():
        factory, manager = make(last_call_tokens=30000)
        controller = await manager.open_new(tmp_path)
        session = factory.sessions[-1]
        session.breakdown_error = DroidError("unsupported")
        session.notify(USAGE)
        await settled(controller, used=None, window=872000, budget=None)

    run(scenario())


def test_many_requests_are_one_reading_and_requests_during_a_reading_cost_one_more(tmp_path):
    async def scenario():
        factory, manager = make()
        controller = await manager.open_new(tmp_path)
        session = factory.sessions[-1]
        await settled(controller, window=872000)
        session.context_delay = 0.05
        reads = session.context_reads
        for _ in range(20):
            controller.refresh_context()
        await asyncio.sleep(0.25)
        assert session.context_reads - reads == 1, "twenty requests before the reading started are answered by it"
        reads = session.context_reads
        controller.refresh_context()
        await until(lambda: session.context_reads - reads == 1)
        for _ in range(20):
            controller.refresh_context()  # a reading is under way: its answer may already be out of date
        await asyncio.sleep(0.3)
        assert session.context_reads - reads == 2

    run(scenario())

def test_notifications_nobody_asked_for_are_harmless(tmp_path):
    async def scenario():
        factory, manager = make()
        controller = await manager.open_new(tmp_path)
        session = factory.sessions[-1]
        await settled(controller, window=872000)
        for note in ({}, {"type": None}, {"type": 7}, {"type": "droid_working_state_changed"}, {"type": "session_compacted"}):
            session.notify(note)
        await asyncio.sleep(0.05)
        assert controller.state is SessionState.READY

    run(scenario())


def test_a_replaced_session_is_read_for_itself_and_the_old_one_is_not_listened_to(tmp_path):
    async def scenario():
        factory, manager = make(last_call_tokens=30000)
        controller = await manager.open_saved(tmp_path, "saved-1")
        old = factory.sessions[-1]
        await settled(controller, used=30000)
        stale = list(old.handlers)
        assert stale, "the controller listens to its session"
        controller._die(DroidProcessError("gone", exit_code=1))
        assert controller.state is SessionState.DEAD
        factory.next_session = {"last_call_tokens": 22000}
        await controller.reconnect()
        assert controller.state is SessionState.READY
        await settled(controller, used=22000, compacted=False)
        for callback, _ in stale:
            callback({"type": "session_compacted"})
        assert controller.context.used == 22000 and not controller.context.compacted, "the process that is gone is not listened to"

    run(scenario())

def test_closing_a_session_stops_a_reading_that_is_under_way(tmp_path):
    async def scenario():
        factory, manager = make()
        controller = await manager.open_new(tmp_path)
        session = factory.sessions[-1]
        session.context_delay = 5.0
        controller.refresh_context()
        await until(lambda: session.context_reads >= 1)  # a reading is now waiting on droid
        task = controller._context_task
        assert task is not None and not task.done()
        await asyncio.wait_for(controller.close(), 5)
        assert task.cancelled() or task.done()
        assert controller.state is SessionState.CLOSED

    run(scenario())


def test_sessions_without_context_requests_just_have_no_figure(tmp_path):
    """The replayer's sessions have no ``context()`` and no notifications."""

    async def scenario():
        class Plain(FakeSession):
            context = None
            on_notification = None

        factory, manager = make()
        factory._new_session = lambda interactions, session_id: Plain(interactions, session_id=session_id or "plain-1")
        controller = await manager.open_new(tmp_path)
        assert controller.state is SessionState.READY
        await asyncio.sleep(0.05)
        assert controller.context == ContextState()

    run(scenario())


def usage(input_tokens: int, output_tokens: int = 0, cache_read: int = 0, session_id: str | None = None) -> dict:
    note = {
        "type": "session_token_usage_changed",
        "lastCallTokenUsage": {"inputTokens": input_tokens, "outputTokens": output_tokens, "cacheReadTokens": cache_read},
    }
    if session_id is not None:
        note["sessionId"] = session_id
    return note


async def readings_done(controller) -> None:
    await until(lambda: controller._context_task is None or controller._context_task.done())
    await asyncio.sleep(0.02)


@pytest.mark.parametrize(
    ("raw", "figure"),
    [
        ({"inputTokens": 21393, "outputTokens": 551, "cacheReadTokens": 0}, 21944),
        ({"inputTokens": 3464, "outputTokens": 27, "cacheReadTokens": 14492}, 17983),
        ({"inputTokens": 353, "outputTokens": 4}, 357),
        ({"inputTokens": 0, "outputTokens": 0, "cacheReadTokens": 0}, None),
        ({"inputTokens": "many"}, None),
        ({"inputTokens": -1, "outputTokens": 5}, None),
        ({"inputTokens": True}, None),
        (None, None),
        ([1, 2], None),
    ],
)
def test_a_notification_carries_the_figure_of_its_own_call(raw, figure):
    assert last_call_figure(raw) == figure


def test_the_fixed_part_is_the_estimate_without_the_messages_and_side_calls_fall_far_below_it():
    async def scenario():
        session = bare_session()
        categories = [
            SimpleNamespace(name="System prompt", color_key="systemPrompt", tokens=1558),
            SimpleNamespace(name="System tools", color_key="systemTools", tokens=8418),
            SimpleNamespace(name="Messages", color_key="messages", tokens=618),
        ]
        breakdown = SimpleNamespace(
            model_id="claude-haiku-5-5", context_budget=800000, last_call_compaction_tokens=22518, used_tokens=13243, categories=categories
        )

        async def get_context_breakdown():
            return breakdown

        session._require_client = lambda: SimpleNamespace(get_context_breakdown=get_context_breakdown)
        reading = await read_context(session)
        assert reading.fixed == 12625
        assert reading == ContextState(used=22518, window=872000, budget=800000, model="claude-haiku-5-5"), "fixed is not compared"

    run(scenario())
    # Live figures (2026-10-29, claude-haiku-5-5): the first turn's call 22518, the session title call 357.
    assert plausible(22518, 12625) and plausible(13086, 12625) and plausible(6400, 12625)
    assert not plausible(357, 12625) and not plausible(6000, 12625)
    assert plausible(357, None), "without an estimate every figure is taken"


def test_the_session_title_call_that_ends_after_the_first_turn_does_not_become_the_figure(tmp_path):
    async def scenario():
        factory, manager = make()
        controller = await manager.open_new(tmp_path)
        await settled(controller, window=872000)
        session = factory.sessions[-1]
        _, stream = await start_turn(controller, session)
        session.last_call_tokens = 357  # by the time the breakdown is read, the title call is droid's last call
        session.notify(usage(22514, 4))
        assert controller.context.used == 22518, "the call's own figure is taken at once"
        session.notify(usage(353, 4))
        stream.push(success("ok", 1.0))
        stream.end()
        await until(lambda: controller.state is SessionState.READY)
        await readings_done(controller)
        assert controller.context.used == 22518

    run(scenario())


def test_a_title_call_that_ends_first_is_skipped_and_the_turn_s_call_is_shown(tmp_path):
    async def scenario():
        factory, manager = make()
        controller = await manager.open_new(tmp_path)
        await settled(controller, window=872000)
        session = factory.sessions[-1]
        _, stream = await start_turn(controller, session)
        session.last_call_tokens = 365
        session.notify(usage(359, 6))
        await readings_done(controller)
        assert controller.context.used is None, "a side call is not the conversation's size"
        session.last_call_tokens = 21944
        session.notify(usage(21393, 551))
        await settled(controller, used=21944)
        stream.push(success("ok", 1.0))
        stream.end()
        await until(lambda: controller.state is SessionState.READY)

    run(scenario())


def test_a_saved_session_whose_last_record_is_a_side_call_shows_no_figure_rather_than_a_wrong_one(tmp_path):
    async def scenario():
        factory, manager = make(last_call_tokens=357)
        controller = await manager.open_saved(tmp_path, "saved-1")
        await settled(controller, window=872000, budget=250000)
        await readings_done(controller)
        assert controller.context.used is None

    run(scenario())


def test_a_figure_for_another_session_is_not_taken(tmp_path):
    async def scenario():
        factory, manager = make()
        controller = await manager.open_new(tmp_path)
        await settled(controller, window=872000)
        session = factory.sessions[-1]
        session.notify(usage(48000, 900, session_id="some-subagent-session"))
        assert controller.context.used is None
        await readings_done(controller)
        assert controller.context.used is None

    run(scenario())


async def start_turn(controller, session, text="go"):
    assert controller.send_prompt(text)
    await until(lambda: session.active is not None)
    return session, session.active
