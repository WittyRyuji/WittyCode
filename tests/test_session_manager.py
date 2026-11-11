"""Session lifecycle, concurrency and the permission / question races (fake SDK sessions, no droid)."""

from __future__ import annotations

import asyncio
import os
from pathlib import Path

import pytest
from droid_sdk import (
    Autonomy,
    DroidConnectionError,
    DroidError,
    DroidProcessError,
    Mode,
    ReasoningEffort,
    RunTimeoutError,
    SessionNotFoundError,
    TokenUsageUpdate,
    ToolConfirmationOutcome,
    WorkingState,
    WorkingStateChanged,
)

from desktop.core import session_manager as sm
from desktop.core.session_manager import LiveSettings, SessionLimitError, SessionManager, SessionState
from desktop.core.settings import AppSettings
from desktop.core.transcript import ErrorBlock, NoticeBlock, PromptStatus, TurnStatus
from tests.fakes import FakeFactory, until
from tests.support import complete, delta, failure, permission_request, question_request, success


def run(coro):
    return asyncio.run(coro)


def make(max_open: int = 4, **options):
    factory = FakeFactory()
    factory.default_options.update(options)
    return factory, SessionManager(AppSettings(max_open_sessions=max_open), factory)


async def start_turn(controller, factory, text="go"):
    """Send a prompt and wait until the fake session has the stream open."""
    session = factory.sessions[-1] if controller._session is None else controller._session
    assert controller.send_prompt(text)
    await until(lambda: session.active is not None)
    return session, session.active


def test_open_new_reports_the_model_droid_actually_uses(tmp_path):
    async def scenario():
        factory, manager = make(model="substituted-model")
        manager.settings.default_model = "requested-model"
        controller = await manager.open_new(tmp_path)
        assert controller.state is SessionState.READY
        assert factory.calls[0][1]["model"] == "requested-model"
        assert controller.model == "substituted-model"
        assert controller.live_settings() == LiveSettings("substituted-model", "low", "off", "auto")
        assert controller.session_id == "new-1" and controller.project == tmp_path.resolve()

    run(scenario())


def test_a_turn_streams_into_the_transcript_and_returns_to_ready(tmp_path):
    async def scenario():
        factory, manager = make()
        controller = await manager.open_new(tmp_path)
        states: list[tuple[str, str]] = []
        manager.add_listener(lambda c, kind: states.append((kind, c.state.value)))
        session, stream = await start_turn(controller, factory, "hello")
        assert controller.state is SessionState.RUNNING and controller.busy
        assert controller.send_prompt("again") is False
        stream.push(WorkingStateChanged(state=WorkingState.THINKING))
        await until(lambda: controller.working_label == "正在思考…")
        stream.push(delta("Hi there"), complete(), success("Hi there"))
        stream.end()
        await until(lambda: controller.state is SessionState.READY)
        turn = controller.transcript.turns[-1]
        assert turn.status is TurnStatus.DONE and turn.blocks[-1].text == "Hi there"
        assert ("state", "running") in states and states[-1][1] == "ready"
        assert controller.working_label == "就绪"
        assert controller.send_prompt("   ") is False

    run(scenario())


def test_run_failure_adds_exactly_one_error_block(tmp_path):
    async def scenario():
        factory, manager = make()
        controller = await manager.open_new(tmp_path)
        _, stream = await start_turn(controller, factory)
        stream.push(failure("quota exceeded"))
        stream.end()
        await until(lambda: controller.state is SessionState.READY)
        turn = controller.transcript.turns[-1]
        assert turn.status is TurnStatus.FAILED
        assert [b.message for b in turn.blocks if isinstance(b, ErrorBlock)] == ["quota exceeded"]

    run(scenario())


def test_stream_ending_without_a_result_aborts_the_turn(tmp_path):
    async def scenario():
        factory, manager = make()
        controller = await manager.open_new(tmp_path)
        _, stream = await start_turn(controller, factory)
        stream.push(delta("partial"))
        stream.end()
        await until(lambda: controller.state is SessionState.READY)
        turn = controller.transcript.turns[-1]
        assert turn.status is TurnStatus.INTERRUPTED
        assert any(isinstance(b, NoticeBlock) and "意外结束" in b.text for b in turn.blocks)

    run(scenario())


def test_usage_updates_are_exposed_per_session(tmp_path):
    async def scenario():
        factory, manager = make()
        controller = await manager.open_new(tmp_path)
        assert controller.usage is None
        _, stream = await start_turn(controller, factory)
        stream.push(TokenUsageUpdate(input_tokens=1, output_tokens=2, cache_creation_tokens=0, cache_read_tokens=3, thinking_tokens=0, factory_credits=5.0))
        await until(lambda: controller.usage is not None)
        assert controller.usage.factory_credits == 5.0 and controller.usage.cache_read_tokens == 3

    run(scenario())


def test_a_turn_knows_what_the_model_wrote_from_the_run_result_and_how_long_it_took(tmp_path):
    from droid_sdk import Usage

    async def scenario():
        factory, manager = make()
        controller = await manager.open_new(tmp_path)
        _, stream = await start_turn(controller, factory)
        stream.push(delta("a long answer"), complete())
        used = Usage(input_tokens=4, output_tokens=400, cache_creation_tokens=0, cache_read_tokens=0, thinking_tokens=100)
        stream.push(success("a long answer", seconds=20.0, usage=used))
        stream.end()
        await until(lambda: controller.state is SessionState.READY)
        turn = controller.transcript.turns[-1]
        assert (turn.output_tokens, turn.duration, turn.model_seconds) == (400, 20.0, 20.0)
        assert turn.first_output_at is not None

    run(scenario())


def test_a_run_result_without_usage_leaves_the_turn_the_difference_of_what_the_session_counted(tmp_path):
    async def scenario():
        factory, manager = make()
        controller = await manager.open_new(tmp_path)
        _, stream = await start_turn(controller, factory)
        stream.push(TokenUsageUpdate(input_tokens=1, output_tokens=400, cache_creation_tokens=0, cache_read_tokens=0, thinking_tokens=0, factory_credits=1.0))
        stream.push(delta("a long answer"), complete(), success("a long answer", seconds=20.0))
        stream.end()
        await until(lambda: controller.state is SessionState.READY)
        assert controller.transcript.turns[-1].output_tokens == 400
        _, stream = await start_turn(controller, factory)
        stream.push(TokenUsageUpdate(input_tokens=2, output_tokens=650, cache_creation_tokens=0, cache_read_tokens=0, thinking_tokens=0, factory_credits=2.0))
        stream.push(delta("another"), complete(), success("another", seconds=10.0))
        stream.end()
        await until(lambda: controller.state is SessionState.READY)
        assert controller.transcript.turns[-1].output_tokens == 250, "the session's total went from 400 to 650"

    run(scenario())


def test_permission_prompt_waits_until_answered_and_a_second_answer_is_refused(tmp_path):
    async def scenario():
        factory, manager = make()
        controller = await manager.open_new(tmp_path)
        session, _ = await start_turn(controller, factory)
        handler = asyncio.ensure_future(session.interactions.on_permission(permission_request()))
        await until(lambda: controller.transcript.pending_prompts())
        await asyncio.sleep(0.05)
        assert not handler.done(), "the handler must keep waiting; there is no timeout"
        (prompt,) = controller.transcript.pending_prompts()
        assert controller.answer_permission(prompt.id, "proceed_once") is True
        response = await asyncio.wait_for(handler, 1)
        assert response.selected_option is ToolConfirmationOutcome.PROCEED_ONCE
        assert prompt.status is PromptStatus.ANSWERED
        assert controller.answer_permission(prompt.id, "proceed_once") is False
        assert controller.answer_permission("nope", "proceed_once") is False

    run(scenario())


def test_an_option_that_was_not_offered_is_refused_and_the_prompt_stays_open(tmp_path):
    async def scenario():
        factory, manager = make()
        controller = await manager.open_new(tmp_path)
        session, _ = await start_turn(controller, factory)
        handler = asyncio.ensure_future(session.interactions.on_permission(permission_request()))
        await until(lambda: controller.transcript.pending_prompts())
        (prompt,) = controller.transcript.pending_prompts()
        assert controller.answer_permission(prompt.id, "made_up") is False
        assert controller.answer_permission(prompt.id, "proceed_edit") is False
        assert prompt.status is PromptStatus.PENDING and not handler.done()
        assert controller.answer_permission(prompt.id, "cancel") is True
        assert (await asyncio.wait_for(handler, 1)).selected_option is ToolConfirmationOutcome.CANCEL

    run(scenario())


def test_late_answer_after_interrupt_is_refused_and_the_handler_is_released(tmp_path):
    async def scenario():
        factory, manager = make()
        controller = await manager.open_new(tmp_path)
        session, _ = await start_turn(controller, factory)
        handler = asyncio.ensure_future(session.interactions.on_permission(permission_request()))
        await until(lambda: controller.transcript.pending_prompts())
        (prompt,) = controller.transcript.pending_prompts()
        await controller.interrupt()
        await until(lambda: controller.state is SessionState.READY)
        response = await asyncio.wait_for(handler, 1)
        assert response.selected_option is ToolConfirmationOutcome.CANCEL
        assert prompt.status is PromptStatus.EXPIRED
        assert controller.answer_permission(prompt.id, "proceed_once") is False
        assert controller.transcript.turns[-1].status is TurnStatus.INTERRUPTED

    run(scenario())


def test_handler_cancelled_by_the_sdk_expires_its_prompt(tmp_path):
    async def scenario():
        factory, manager = make()
        controller = await manager.open_new(tmp_path)
        session, _ = await start_turn(controller, factory)
        handler = asyncio.ensure_future(session.interactions.on_permission(permission_request()))
        await until(lambda: controller.transcript.pending_prompts())
        (prompt,) = controller.transcript.pending_prompts()
        handler.cancel()
        with pytest.raises(asyncio.CancelledError):
            await handler
        assert prompt.status is PromptStatus.EXPIRED
        assert controller.answer_permission(prompt.id, "proceed_once") is False
        assert not controller._pending

    run(scenario())


@pytest.mark.parametrize("kind", ["permission", "question"])
def test_expiring_a_pending_prompt_returns_a_cancel_answer_instead_of_raising(tmp_path, kind):
    async def scenario():
        factory, manager = make()
        controller = await manager.open_new(tmp_path)
        session, _ = await start_turn(controller, factory)
        if kind == "permission":
            handler = asyncio.ensure_future(session.interactions.on_permission(permission_request()))
        else:
            handler = asyncio.ensure_future(session.interactions.on_question(question_request()))
        await until(lambda: controller.transcript.pending_prompts())
        (prompt,) = controller.transcript.pending_prompts()

        controller._expire_prompt(prompt.id)

        response = await asyncio.wait_for(handler, 1)
        assert not handler.cancelled()
        if kind == "permission":
            assert response is sm.CancelPermission
        else:
            assert response is sm.CancelQuestion and response.cancelled and not response.answers
        assert prompt.status is PromptStatus.EXPIRED and not controller._pending

    run(scenario())


@pytest.mark.parametrize("how", ["close", "death"])
def test_session_close_and_process_death_answer_open_prompts_with_a_cancel(tmp_path, how):
    async def scenario():
        factory, manager = make()
        controller = await manager.open_new(tmp_path)
        session, stream = await start_turn(controller, factory)
        permission = asyncio.ensure_future(session.interactions.on_permission(permission_request()))
        await until(lambda: controller.transcript.pending_prompts())
        question = asyncio.ensure_future(session.interactions.on_question(question_request()))
        await until(lambda: len(controller.transcript.pending_prompts()) == 2)
        prompts = controller.transcript.pending_prompts()

        if how == "close":
            await controller.close()
        else:
            stream.push(DroidProcessError("Droid process exited unexpectedly", exit_code=1))
            await until(lambda: controller.state is SessionState.DEAD)

        assert await asyncio.wait_for(permission, 1) is sm.CancelPermission
        assert await asyncio.wait_for(question, 1) is sm.CancelQuestion
        assert not controller._pending
        assert [p.status for p in prompts] == [PromptStatus.EXPIRED, PromptStatus.EXPIRED]

    run(scenario())


def test_question_answers_single_multi_and_skip(tmp_path):
    async def scenario():
        factory, manager = make()
        controller = await manager.open_new(tmp_path)
        session, _ = await start_turn(controller, factory)
        handler = asyncio.ensure_future(session.interactions.on_question(question_request()))
        await until(lambda: controller.transcript.pending_prompts())
        (prompt,) = controller.transcript.pending_prompts()
        assert controller.answer_question(prompt.id, ["only one"]) is False, "wrong answer count"
        assert prompt.status is PromptStatus.PENDING
        assert controller.answer_question(prompt.id, ["a", ["x", "z"]]) is True
        response = await asyncio.wait_for(handler, 1)
        assert not response.cancelled and [item.answer for item in response.answers] == ["a", "x, z"]
        qa = [b for b in controller.transcript.turns[-1].blocks if b.kind == "qa"][0]
        assert qa.pairs == [("Pick one?", "a"), ("Pick several?", "x, z")]
        assert controller.answer_question(prompt.id, ["a", ["x"]]) is False

        skipped = asyncio.ensure_future(session.interactions.on_question(question_request()))
        await until(lambda: controller.transcript.pending_prompts())
        (second,) = controller.transcript.pending_prompts()
        assert controller.answer_question(second.id, None) is True
        assert (await asyncio.wait_for(skipped, 1)).cancelled

    run(scenario())


def test_pending_prompts_are_released_when_the_session_closes(tmp_path):
    async def scenario():
        factory, manager = make()
        controller = await manager.open_new(tmp_path)
        session, _ = await start_turn(controller, factory)
        handler = asyncio.ensure_future(session.interactions.on_question(question_request()))
        await until(lambda: controller.transcript.pending_prompts())
        (prompt,) = controller.transcript.pending_prompts()
        await controller.close()
        assert handler.cancelled() or handler.done()
        assert prompt.status is PromptStatus.EXPIRED
        assert controller.answer_question(prompt.id, ["a", ["x"]]) is False

    run(scenario())


def test_process_death_midturn_then_reconnect_without_duplicating_history(tmp_path):
    async def scenario():
        factory, manager = make()
        controller = await manager.open_new(tmp_path)
        first = factory.sessions[0]
        _, stream = await start_turn(controller, factory, "hi")
        stream.push(delta("par"), DroidProcessError("Droid process exited unexpectedly", exit_code=1))
        await until(lambda: controller.state is SessionState.DEAD)
        turn = controller.transcript.turns[-1]
        assert turn.status is TurnStatus.FAILED
        error = [b for b in turn.blocks if isinstance(b, ErrorBlock)][-1]
        assert error.recoverable and "exit code 1" in error.message and "重新连接" in error.message
        await until(lambda: first.closed == 1)

        await controller.reconnect()
        assert controller.state is SessionState.READY
        assert [name for name, _ in factory.calls if name in ("resume", "load_history")] == ["resume"]
        assert factory.sessions[1].id == "new-1"
        assert len(controller.transcript.turns) == 1
        assert error.resolved
        assert any(isinstance(b, NoticeBlock) and "已重新连接" in b.text for b in controller.transcript.turns[-1].blocks)
        session, stream = await start_turn(controller, factory, "back again")
        stream.push(success("ok"))
        stream.end()
        await until(lambda: controller.state is SessionState.READY)
        assert len(controller.transcript.turns) == 2

    run(scenario())


def test_connection_errors_also_mean_the_session_is_dead(tmp_path):
    async def scenario():
        factory, manager = make()
        controller = await manager.open_new(tmp_path)
        _, stream = await start_turn(controller, factory)
        stream.push(DroidConnectionError("Transport not connected"))
        await until(lambda: controller.state is SessionState.DEAD)
        assert "连接已断开" in controller.last_error

    run(scenario())


def test_interrupt_on_a_dead_connection_marks_the_session_dead(tmp_path):
    async def scenario():
        factory, manager = make()
        controller = await manager.open_new(tmp_path)
        session, _ = await start_turn(controller, factory)

        async def broken():
            raise DroidProcessError("gone", exit_code=-1)

        session.interrupt = broken
        await controller.interrupt()
        assert controller.state is SessionState.DEAD

    run(scenario())


def test_close_while_opening_waits_for_the_open_and_does_not_leak_the_process(tmp_path):
    async def scenario():
        factory, manager = make(open_delay=0.2)
        opening = asyncio.ensure_future(manager.open_new(tmp_path))
        await until(lambda: factory.sessions and "open-start" in factory.sessions[0].log)
        controller = manager.controllers[0]
        await controller.close()
        session = factory.sessions[0]
        assert session.closed == 1 and "open-end" in session.log, "close() returns only after the process is released"
        await opening
        assert controller.state is SessionState.CLOSED and controller._session is None
        assert manager.controllers == []

    run(scenario())


def test_close_cancels_the_running_turn_before_closing_the_session(tmp_path):
    async def scenario():
        factory, manager = make()
        controller = await manager.open_new(tmp_path)
        session, _ = await start_turn(controller, factory)
        await controller.close()
        assert session.log.index("stream-cancelled") < session.log.index("close")
        assert controller.state is SessionState.CLOSED
        assert controller.transcript.turns[-1].status is TurnStatus.INTERRUPTED

    run(scenario())


def test_concurrent_closes_close_the_session_once(tmp_path):
    async def scenario():
        factory, manager = make()
        controller = await manager.open_new(tmp_path)
        await asyncio.gather(controller.close(), controller.close(), manager.close(controller))
        assert factory.sessions[0].closed == 1

    run(scenario())


def test_close_survives_a_session_that_raises_on_close(tmp_path):
    async def scenario():
        factory, manager = make()
        controller = await manager.open_new(tmp_path)
        factory.sessions[0].close_error = DroidProcessError("sticky crash")
        await controller.close()
        assert controller.state is SessionState.CLOSED

    run(scenario())


def test_session_limit_evicts_the_least_recently_used_idle_session(tmp_path):
    async def scenario():
        factory, manager = make(max_open=2)
        a = await manager.open_new(tmp_path / "a")
        b = await manager.open_new(tmp_path / "b")
        a.used_at, b.used_at = 1.0, 2.0
        c = await manager.open_new(tmp_path / "c")
        assert a.state is SessionState.CLOSED and b.is_open and c.is_open
        assert manager.controllers == [b, c]
        await start_turn(b, factory)
        await start_turn(c, factory)
        with pytest.raises(SessionLimitError):
            await manager.open_new(tmp_path / "d")
        assert manager.open_controllers() == [b, c]

    run(scenario())


def test_a_session_that_replaces_another_in_its_tab_is_not_a_further_open_session(tmp_path):
    async def scenario():
        factory, manager = make(max_open=1)
        a = await manager.open_new(tmp_path / "a")
        b = await manager.open_new(tmp_path / "b", replacing=True)
        assert a.is_open and b.is_open and manager.controllers == [a, b], "no slot taken, nobody evicted"
        a.used_at, b.used_at = 1.0, 2.0
        c = await manager.open_new(tmp_path / "c")
        assert manager.open_controllers() == [c], "a session that is new still keeps to the limit"

    run(scenario())


def alive(factory):
    """Fake droid processes that were started and not closed yet."""
    return [s for s in factory.sessions if "open-start" in s.log and not s.closed]


def test_evicting_an_idle_session_is_announced_and_names_it(tmp_path):
    async def scenario():
        factory, manager = make(max_open=1)
        events: list[tuple[str, str, SessionState]] = []
        manager.add_listener(lambda controller, kind: events.append((kind, controller.eviction_notice, controller.state)))
        first = await manager.open_new(tmp_path / "a")
        assert controller_never_announced(events)
        await start_turn(first, factory, "refactor the parser")
        factory.sessions[0].active.push(success("done"))
        factory.sessions[0].active.end()
        await until(lambda: first.state is SessionState.READY)

        second = await manager.open_new(tmp_path / "b")

        assert first.evicted and not second.evicted
        expected = "已自动关闭最久未使用的会话「refactor the parser」以腾出位置，可在侧边栏重新打开。"
        assert first.eviction_notice == expected
        announced = [event for event in events if event[0] == "evicted"]
        assert announced == [("evicted", expected, SessionState.READY)], "announced while it was still open, once"
        assert first.state is SessionState.CLOSED and second.state is SessionState.READY

    run(scenario())


def controller_never_announced(events):
    return not [event for event in events if event[0] == "evicted"]


def test_an_evicted_session_that_never_ran_a_turn_is_not_promised_to_be_reopenable(tmp_path):
    async def scenario():
        factory, manager = make(max_open=1)
        first = await manager.open_new(tmp_path / "a")
        await manager.open_new(tmp_path / "b")
        assert first.eviction_notice == "已自动关闭最久未使用的会话「新会话」以腾出位置。"

    run(scenario())


def test_a_session_that_is_busy_is_never_evicted_and_nothing_is_announced(tmp_path):
    async def scenario():
        factory, manager = make(max_open=1)
        events: list[str] = []
        manager.add_listener(lambda controller, kind: events.append(kind))
        busy = await manager.open_new(tmp_path / "a")
        await start_turn(busy, factory)
        with pytest.raises(SessionLimitError, match="最多同时打开 1 个会话"):
            await manager.open_new(tmp_path / "b")
        assert "evicted" not in events and not busy.evicted
        assert busy.state is SessionState.RUNNING and manager.controllers == [busy]
        assert factory.sessions[0].closed == 0 and len(factory.sessions) == 1

    run(scenario())


def test_two_concurrent_opens_at_the_limit_never_exceed_it(tmp_path):
    async def scenario():
        # A real droid takes seconds to start; while it does, that session is OPENING and counts as taken.
        factory, manager = make(max_open=1, open_delay=0.05)
        old = await manager.open_new(tmp_path / "old")
        factory.sessions[0].close_delay = 0.05  # wide window for the second open to look at the stale list
        peak = 0

        def watch(controller, kind):
            nonlocal peak
            peak = max(peak, len(manager.open_controllers()), len(alive(factory)))

        manager.add_listener(watch)
        results = await asyncio.gather(
            manager.open_new(tmp_path / "x"), manager.open_new(tmp_path / "y"), return_exceptions=True
        )
        winners = [r for r in results if not isinstance(r, BaseException)]
        losers = [r for r in results if isinstance(r, BaseException)]
        assert len(winners) == 1 and len(losers) == 1 and isinstance(losers[0], SessionLimitError)
        assert peak <= 1
        assert manager.open_controllers() == winners and winners[0].state is SessionState.READY
        assert old.state is SessionState.CLOSED and factory.sessions[0].closed == 1
        assert len(alive(factory)) == 1 and len(factory.sessions) == 2, "the refused open never started a droid process"

    run(scenario())


def test_concurrent_opens_each_evict_their_own_idle_session(tmp_path):
    async def scenario():
        factory, manager = make(max_open=2, open_delay=0.03)
        a = await manager.open_new(tmp_path / "a")
        b = await manager.open_new(tmp_path / "b")
        a.used_at, b.used_at = 1.0, 2.0
        for session in factory.sessions:
            session.close_delay = 0.03
        announced: list[str] = []
        manager.add_listener(lambda controller, kind: kind == "evicted" and announced.append(controller.title))
        peak = 0

        def watch(controller, kind):
            nonlocal peak
            peak = max(peak, len(manager.open_controllers()), len(alive(factory)))

        manager.add_listener(watch)
        c, d = await asyncio.gather(manager.open_new(tmp_path / "c"), manager.open_new(tmp_path / "d"))
        assert a.state is SessionState.CLOSED and b.state is SessionState.CLOSED
        assert manager.open_controllers() == [c, d] and peak <= 2
        assert len(alive(factory)) == 2
        assert [s.closed for s in factory.sessions[:2]] == [1, 1]
        assert len(announced) == 2

    run(scenario())


def test_a_session_the_user_is_already_closing_gives_its_slot_without_evicting_another(tmp_path):
    async def scenario():
        factory, manager = make(max_open=2)
        a = await manager.open_new(tmp_path / "a")
        b = await manager.open_new(tmp_path / "b")
        a.used_at, b.used_at = 5.0, 1.0
        factory.sessions[0].close_delay = 0.1
        events: list[str] = []
        manager.add_listener(lambda controller, kind: events.append(kind))
        user_close = asyncio.ensure_future(a.close())
        await until(lambda: a._close_requested)
        c = await manager.open_new(tmp_path / "c")
        await user_close
        assert a.state is SessionState.CLOSED and not a.evicted
        assert b.is_open and not b.evicted and c.is_open, "the older idle session b was left alone"
        assert "evicted" not in events and factory.sessions[1].closed == 0
        assert len(alive(factory)) == 2

    run(scenario())


def test_concurrent_opens_of_the_same_saved_session_share_one_controller(tmp_path):
    async def scenario():
        # At the limit the registration has to wait for an eviction: that is when a second open of the
        # same saved session used to slip past the "already open?" check and start a second process.
        factory, manager = make(max_open=1, open_delay=0.05)
        old = await manager.open_new(tmp_path / "old")
        factory.sessions[0].close_delay = 0.05
        first, second = await asyncio.gather(
            manager.open_saved(tmp_path, "saved-1"), manager.open_saved(tmp_path, "saved-1")
        )
        assert first is second and manager.controllers == [first] and old.state is SessionState.CLOSED
        assert [name for name, _ in factory.calls].count("resume") == 1
        assert len(alive(factory)) == 1

    run(scenario())


def test_open_saved_loads_history_first_then_resumes_inside_the_project_cwd(tmp_path):
    async def scenario():
        project = tmp_path / "proj"
        project.mkdir()
        factory, manager = make()
        before = Path.cwd()
        controller = await manager.open_saved(project, "saved-1", title="Old chat")
        assert [name for name, _ in factory.calls] == ["load_history", "resume"]
        session = factory.sessions[0]
        assert session.cwd_at_open == project.resolve()
        assert Path.cwd() == before, "the process cwd is restored after the resume"
        assert controller.state is SessionState.READY and controller.title == "Old chat"

    run(scenario())


def test_activity_time_is_the_last_message_not_the_last_open(tmp_path):
    async def scenario():
        factory, manager = make()
        saved_at = 1_700_000_000.0
        resumed = await manager.open_saved(tmp_path, "saved-1", modified=saved_at)
        assert resumed.state is SessionState.READY
        assert resumed.active_at == saved_at, "resuming a session is not a message"
        fresh = await manager.open_new(tmp_path / "fresh")
        assert fresh.active_at > saved_at and fresh.active_at == pytest.approx(fresh.used_at, abs=5)

        session, stream = await start_turn(resumed, factory)
        sent_at = resumed.active_at
        assert sent_at > saved_at and sent_at == resumed.used_at, "sending is a message"
        resumed.active_at = 1.0
        stream.push(delta("done", "m1"), complete("m1"), success("done"))
        stream.end()
        await until(lambda: resumed.state is SessionState.READY)
        assert resumed.active_at > sent_at - 1, "the end of the answer counts too"
        assert fresh.active_at < resumed.active_at + 5

    run(scenario())


def test_a_dropped_connection_that_is_reconnected_keeps_the_activity_time(tmp_path):
    async def scenario():
        factory, manager = make()
        controller = await manager.open_saved(tmp_path, "saved-1", modified=1_700_000_000.0)
        _, stream = await start_turn(controller, factory, "hi")
        stream.push(DroidProcessError("Droid process exited unexpectedly", exit_code=1))
        await until(lambda: controller.state is SessionState.DEAD)
        died_at = controller.active_at
        assert died_at > 1_700_000_000.0, "the message that was sent counts"
        await controller.reconnect()
        assert controller.state is SessionState.READY and controller.active_at == died_at, "connecting is not a message"

    run(scenario())


def test_concurrent_resumes_never_overlap_their_cwd_changes(tmp_path):
    async def scenario():
        projects = []
        for name in ("p1", "p2", "p3"):
            (tmp_path / name).mkdir()
            projects.append(tmp_path / name)
        factory, manager = make(open_delay=0.03)
        before = Path.cwd()
        controllers = await asyncio.gather(
            *(manager.open_saved(project, f"s{index}") for index, project in enumerate(projects))
        )
        assert factory.tracker["max"] == 1
        assert {s.id: s.cwd_at_open for s in factory.sessions} == {c.session_id: c.project for c in controllers}
        assert Path.cwd() == before
        assert all(c.state is SessionState.READY for c in controllers)

    run(scenario())


def test_new_sessions_do_not_need_the_cwd_lock(tmp_path):
    async def scenario():
        factory, manager = make(open_delay=0.05)
        controllers = await asyncio.gather(*(manager.open_new(tmp_path / n) for n in ("a", "b", "c")))
        assert factory.tracker["max"] == 3 and all(c.state is SessionState.READY for c in controllers)

    run(scenario())


def test_opening_the_same_saved_session_twice_returns_the_same_controller(tmp_path):
    async def scenario():
        factory, manager = make()
        factory.history_delay = 0.1
        first = asyncio.ensure_future(manager.open_saved(tmp_path, "s1"))
        await asyncio.sleep(0.02)
        second = await manager.open_saved(tmp_path, "s1")
        assert (await first) is second
        assert [name for name, _ in factory.calls].count("resume") == 1

    run(scenario())


def test_history_load_failure_is_a_notice_and_the_session_still_opens(tmp_path):
    async def scenario():
        factory, manager = make()
        factory.history_error = RuntimeError("no history")
        controller = await manager.open_saved(tmp_path, "s1")
        assert controller.state is SessionState.READY
        notices = [b.text for t in controller.transcript.turns for b in t.blocks if isinstance(b, NoticeBlock)]
        assert any("历史记录加载失败" in text for text in notices)

    run(scenario())


def test_resume_failure_leaves_a_dead_session_with_a_recoverable_error(tmp_path):
    async def scenario():
        factory, manager = make()
        factory.next_session = {"fail_open": SessionNotFoundError("gone")}
        controller = await manager.open_saved(tmp_path, "s1")
        assert controller.state is SessionState.DEAD and not controller.is_open
        assert factory.sessions[0].closed == 1
        assert [b for t in controller.transcript.turns for b in t.blocks if isinstance(b, ErrorBlock) and b.recoverable]

    run(scenario())


def test_new_session_failure_closes_the_session_and_forgets_the_controller(tmp_path):
    async def scenario():
        factory, manager = make()
        factory.next_session = {"fail_open": DroidConnectionError("cannot start")}
        controller = await manager.open_new(tmp_path)
        assert controller.state is SessionState.CLOSED and controller.last_error
        assert factory.sessions[0].closed == 1 and manager.controllers == []

    run(scenario())


def test_a_listener_that_raises_does_not_break_the_session_or_other_listeners(tmp_path, caplog):
    async def scenario():
        factory, manager = make()
        controller = await manager.open_new(tmp_path)
        seen: list[str] = []

        def broken(_controller, _kind):
            raise RuntimeError("Internal C++ object already deleted")

        controller.add_listener(broken)
        controller.add_listener(lambda _c, kind: seen.append(kind))
        manager.add_listener(broken)
        manager.add_listener(lambda _c, kind: seen.append("manager-" + kind))
        session, stream = await start_turn(controller, factory)
        stream.push(delta("ok"), complete(), success("ok"))
        stream.end()
        await until(lambda: controller.state is SessionState.READY)
        assert "changed" in seen and "manager-changed" in seen
        assert controller.transcript.turns[-1].status is TurnStatus.DONE

    caplog.set_level("ERROR")
    run(scenario())
    assert "session listener failed" in caplog.text and "manager listener failed" in caplog.text


def test_failure_messages_are_readable_sentences():
    from desktop.core.droid_locator import DroidNotFoundError

    assert sm.friendly_failure(DroidNotFoundError("找不到 droid：请安装 Factory 桌面版。")) == "找不到 droid：请安装 Factory 桌面版。"
    assert "exit code 3" in sm.friendly_failure(DroidProcessError("died", exit_code=3))
    assert sm.friendly_failure(ValueError("bad")) == "ValueError: bad"


@pytest.mark.parametrize("how", ["new", "saved"])
def test_an_open_that_raises_does_not_keep_a_slot_or_a_controller(tmp_path, how):
    async def scenario():
        factory, manager = make(max_open=1)
        events: list[tuple[str, SessionState]] = []
        manager.add_listener(lambda controller, kind: events.append((kind, controller.state)))

        def missing(*args, **kwargs):
            raise FileNotFoundError("droid.exe not found")

        setattr(factory, "create" if how == "new" else "resume", missing)
        with pytest.raises(FileNotFoundError):
            if how == "new":
                await manager.open_new(tmp_path)
            else:
                await manager.open_saved(tmp_path, "s1")
        assert manager.controllers == [] and manager.open_controllers() == []
        assert ("state", SessionState.CLOSED) in events
        delattr(factory, "create" if how == "new" else "resume")
        controller = await manager.open_new(tmp_path)
        assert controller.state is SessionState.READY

    run(scenario())


def notices_of(controller):
    return [b.text for turn in controller.transcript.turns for b in turn.blocks if isinstance(b, NoticeBlock)]


def test_stuck_interrupt_replaces_the_droid_process_instead_of_offering_a_possibly_busy_one(tmp_path, monkeypatch):
    monkeypatch.setattr(sm, "INTERRUPT_GRACE_SECONDS", 0.05)

    async def scenario():
        factory, manager = make(interrupt_ends_turn=False)
        controller = await manager.open_new(tmp_path)
        stuck, _ = await start_turn(controller, factory)
        offered: list[tuple[SessionState, object]] = []
        controller.add_listener(lambda c, kind: kind == "state" and offered.append((c.state, c._session)))

        await controller.interrupt()

        assert controller.state is SessionState.READY
        fresh = factory.sessions[1]
        assert fresh is not stuck and controller._session is fresh and fresh.id == stuck.id
        states = [state for state, _ in offered]
        assert states[-1] is SessionState.READY and states.count(SessionState.READY) == 1
        assert set(states[:-1]) == {SessionState.OPENING}, "never READY (or RUNNING again) while the old process was in use"
        assert all(session is not stuck for state, session in offered if state is SessionState.READY)
        assert stuck.closed == 1 and fresh.closed == 0 and len(alive(factory)) == 1
        assert [name for name, _ in factory.calls if name in ("resume", "load_history")] == ["resume"]
        assert factory.calls[-1] == ("resume", stuck.id)

        turn = controller.transcript.turns[-1]
        assert turn.status is TurnStatus.INTERRUPTED and not turn.stopping
        assert notices_of(controller) == ["本轮已取消。", "停止超时，已重启会话连接"]
        assert not controller.transcript.pending_prompts()

        assert controller.send_prompt("again")
        await until(lambda: fresh.active is not None)
        assert fresh.active.prompt == "again" and stuck.active is None, "the next prompt goes to the fresh process"
        fresh.active.push(success("ok"))
        fresh.active.end()
        await until(lambda: controller.state is SessionState.READY and not controller.busy)

    run(scenario())


def test_stuck_interrupt_whose_resume_fails_leaves_a_dead_session_with_a_reconnect_action(tmp_path, monkeypatch):
    monkeypatch.setattr(sm, "INTERRUPT_GRACE_SECONDS", 0.05)

    async def scenario():
        factory, manager = make(interrupt_ends_turn=False)
        controller = await manager.open_new(tmp_path)
        stuck, _ = await start_turn(controller, factory)
        states: list[SessionState] = []
        controller.add_listener(lambda c, kind: kind == "state" and states.append(c.state))
        factory.next_session = {"fail_open": DroidProcessError("cannot resume", exit_code=3)}

        await controller.interrupt()

        assert controller.state is SessionState.DEAD and SessionState.READY not in states
        assert controller.last_error.startswith("停止超时，重启会话连接失败：") and "exit code 3" in controller.last_error
        error = [b for b in controller.transcript.turns[-1].blocks if isinstance(b, ErrorBlock)][-1]
        assert error.recoverable and not error.resolved, "the 重新连接 button is offered"
        assert stuck.closed == 1 and factory.sessions[1].closed == 1 and not alive(factory)
        assert controller.send_prompt("typed too soon") is False

        await controller.reconnect()
        assert controller.state is SessionState.READY and error.resolved
        assert controller._session is factory.sessions[2] and len(alive(factory)) == 1
        assert notices_of(controller)[-1] == "已重新连接，可以继续对话。"

    run(scenario())


def test_stuck_interrupt_settles_open_prompts_and_leaves_no_handler_waiting(tmp_path, monkeypatch):
    monkeypatch.setattr(sm, "INTERRUPT_GRACE_SECONDS", 0.05)

    async def scenario():
        factory, manager = make(interrupt_ends_turn=False)
        controller = await manager.open_new(tmp_path)
        stuck, _ = await start_turn(controller, factory)
        permission = asyncio.ensure_future(stuck.interactions.on_permission(permission_request()))
        await until(lambda: controller.transcript.pending_prompts())
        question = asyncio.ensure_future(stuck.interactions.on_question(question_request()))
        await until(lambda: len(controller.transcript.pending_prompts()) == 2)
        prompts = controller.transcript.pending_prompts()

        await controller.interrupt()

        assert await asyncio.wait_for(permission, 1) is sm.CancelPermission
        assert await asyncio.wait_for(question, 1) is sm.CancelQuestion
        assert [p.status for p in prompts] == [PromptStatus.EXPIRED, PromptStatus.EXPIRED]
        assert not controller._pending and controller.state is SessionState.READY
        assert controller.answer_permission(prompts[0].id, "proceed_once") is False

    run(scenario())


@pytest.mark.parametrize("how", ["never answers", "times out"])
def test_a_stop_request_droid_never_answers_also_replaces_the_process(tmp_path, monkeypatch, how):
    monkeypatch.setattr(sm, "INTERRUPT_GRACE_SECONDS", 0.05)

    async def scenario():
        factory, manager = make()
        controller = await manager.open_new(tmp_path)
        stuck, _ = await start_turn(controller, factory)

        async def never():
            await asyncio.sleep(3600)

        async def times_out():
            raise RunTimeoutError("Request timed out", method="interrupt_session", timeout_duration=30.0)

        stuck.interrupt = never if how == "never answers" else times_out
        started = asyncio.get_running_loop().time()
        await controller.interrupt()
        assert asyncio.get_running_loop().time() - started < 2
        assert controller.state is SessionState.READY and controller._session is factory.sessions[1]
        assert stuck.closed == 1 and len(alive(factory)) == 1
        assert notices_of(controller)[-1] == "停止超时，已重启会话连接"

    run(scenario())


def test_a_stop_request_that_fails_for_another_reason_changes_nothing_and_can_be_retried(tmp_path, monkeypatch):
    monkeypatch.setattr(sm, "INTERRUPT_GRACE_SECONDS", 0.05)

    async def scenario():
        factory, manager = make()
        controller = await manager.open_new(tmp_path)
        session, stream = await start_turn(controller, factory)
        working = session.interrupt

        async def refuses():
            raise DroidError("not now")

        session.interrupt = refuses
        await controller.interrupt()
        turn = controller.transcript.active_turn
        assert controller.state is SessionState.RUNNING and turn is not None and not turn.stopping
        assert any(text.startswith("停止失败：not now") for text in notices_of(controller))
        assert len(factory.sessions) == 1 and not session.closed

        session.interrupt = working
        await controller.interrupt()
        await until(lambda: controller.state is SessionState.READY)
        assert controller.transcript.turns[-1].status is TurnStatus.INTERRUPTED and len(factory.sessions) == 1

    run(scenario())


def test_the_replacement_is_resumed_only_after_the_old_process_has_exited(tmp_path, monkeypatch):
    monkeypatch.setattr(sm, "INTERRUPT_GRACE_SECONDS", 0.05)

    async def scenario():
        factory, manager = make(interrupt_ends_turn=False)
        controller = await manager.open_new(tmp_path)
        stuck, _ = await start_turn(controller, factory)
        exited = asyncio.Event()
        original_close = stuck.close

        async def slow_close():
            await original_close()
            await asyncio.sleep(0.1)
            exited.set()

        stuck.close = slow_close
        original_resume = factory.resume
        exited_when_resumed: list[bool] = []

        def resume(session_id, *, interactions):
            exited_when_resumed.append(exited.is_set())
            return original_resume(session_id, interactions=interactions)

        factory.resume = resume
        await controller.interrupt()
        assert exited_when_resumed == [True] and controller.state is SessionState.READY

    run(scenario())


def test_an_old_process_that_does_not_exit_in_time_ends_dead_and_reconnect_waits_for_it(tmp_path, monkeypatch):
    monkeypatch.setattr(sm, "INTERRUPT_GRACE_SECONDS", 0.05)
    monkeypatch.setattr(sm, "RESTART_CLOSE_SECONDS", 0.05)

    async def scenario():
        factory, manager = make(interrupt_ends_turn=False)
        controller = await manager.open_new(tmp_path)
        stuck, _ = await start_turn(controller, factory)
        exited = asyncio.Event()
        original_close = stuck.close

        async def slow_close():
            await original_close()
            await asyncio.sleep(0.3)
            exited.set()

        stuck.close = slow_close
        original_resume = factory.resume
        exited_when_resumed: list[bool] = []

        def resume(session_id, *, interactions):
            exited_when_resumed.append(exited.is_set())
            return original_resume(session_id, interactions=interactions)

        factory.resume = resume

        await controller.interrupt()

        assert controller.state is SessionState.DEAD and not exited.is_set()
        assert exited_when_resumed == [], "no second droid is started on the session while the first may be alive"
        assert "没有及时关闭" in controller.last_error
        error = [b for b in controller.transcript.turns[-1].blocks if isinstance(b, ErrorBlock)][-1]
        assert error.recoverable

        await controller.reconnect()
        assert exited_when_resumed == [True] and controller.state is SessionState.READY
        assert len(alive(factory)) == 1

    run(scenario())


def test_closing_the_session_in_the_middle_of_a_restart_leaves_no_process_behind(tmp_path, monkeypatch):
    monkeypatch.setattr(sm, "INTERRUPT_GRACE_SECONDS", 0.05)

    async def scenario():
        factory, manager = make(interrupt_ends_turn=False)
        controller = await manager.open_new(tmp_path)
        stuck, _ = await start_turn(controller, factory)
        stuck.close_delay = 0.15
        stopping = asyncio.ensure_future(controller.interrupt())
        await until(lambda: controller.state is SessionState.OPENING)

        await controller.close()
        await asyncio.wait_for(stopping, 2)

        assert controller.state is SessionState.CLOSED and manager.controllers == []
        assert not alive(factory) and len(factory.sessions) == 1, "no replacement was started for a closing session"

    run(scenario())


def test_closing_while_the_replacement_is_still_starting_closes_it_too(tmp_path, monkeypatch):
    monkeypatch.setattr(sm, "INTERRUPT_GRACE_SECONDS", 0.05)

    async def scenario():
        factory, manager = make(interrupt_ends_turn=False)
        controller = await manager.open_new(tmp_path)
        await start_turn(controller, factory)
        factory.next_session = {"open_delay": 0.2}
        stopping = asyncio.ensure_future(controller.interrupt())
        await until(lambda: len(factory.sessions) == 2 and "open-start" in factory.sessions[1].log)

        await controller.close()
        await asyncio.wait_for(stopping, 2)

        assert controller.state is SessionState.CLOSED and not alive(factory)
        assert [s.closed for s in factory.sessions] == [1, 1]

    run(scenario())


def test_cancelling_whoever_asked_to_stop_does_not_abandon_the_restart(tmp_path, monkeypatch):
    monkeypatch.setattr(sm, "INTERRUPT_GRACE_SECONDS", 0.05)

    async def scenario():
        factory, manager = make(interrupt_ends_turn=False)
        controller = await manager.open_new(tmp_path)
        stuck, _ = await start_turn(controller, factory)
        stuck.close_delay = 0.1
        stopping = asyncio.ensure_future(controller.interrupt())
        await until(lambda: controller.state is SessionState.OPENING)
        stopping.cancel()
        with pytest.raises(asyncio.CancelledError):
            await stopping
        await until(lambda: controller.state is SessionState.READY)
        assert controller._session is factory.sessions[1] and len(alive(factory)) == 1

    run(scenario())


def test_stopping_state_shows_in_the_working_label(tmp_path):
    async def scenario():
        factory, manager = make(interrupt_ends_turn=False)
        controller = await manager.open_new(tmp_path)
        await start_turn(controller, factory)
        controller.transcript.mark_stopping()
        assert controller.working_label == "正在停止…"
        await controller.close()

    run(scenario())


def test_toolbar_settings_are_forwarded_and_read_back(tmp_path):
    async def scenario():
        factory, manager = make()
        controller = await manager.open_new(tmp_path)
        ok = await controller.update_settings(
            model="claude-opus-9", reasoning=ReasoningEffort.HIGH, autonomy=Autonomy.HIGH, mode=Mode.SPEC
        )
        assert ok
        assert factory.sessions[0].settings_calls == [
            {"model": "claude-opus-9", "reasoning_effort": ReasoningEffort.HIGH, "autonomy": Autonomy.HIGH, "mode": Mode.SPEC}
        ]
        assert controller.model == "claude-opus-9"
        assert controller.live_settings() == LiveSettings("claude-opus-9", "high", "high", "spec")

    run(scenario())


def test_models_are_listed_once_and_cached(tmp_path):
    async def scenario():
        factory, manager = make()
        first = await manager.list_models(tmp_path)
        second = await manager.list_models(tmp_path)
        assert [m.id for m in first] == [m.id for m in second] == ["claude-haiku-5-5"]
        assert [name for name, _ in factory.calls].count("list_models") == 1
        await manager.list_models(tmp_path, refresh=True)
        assert [name for name, _ in factory.calls].count("list_models") == 2

    run(scenario())


def test_session_list_failures_are_swallowed_and_limited(tmp_path):
    async def scenario():
        factory, manager = make()
        assert await manager.list_sessions(tmp_path) == []
        assert factory.calls[-1] == ("list_sessions", (tmp_path.resolve(), sm.SESSION_LIST_LIMIT))

        async def broken(cwd, limit=None):
            raise RuntimeError("disk")

        factory.list_sessions = broken
        assert await manager.list_sessions(tmp_path) == []

    run(scenario())


def test_close_all_closes_every_session_and_clears_the_list(tmp_path):
    async def scenario():
        factory, manager = make()
        controllers = [await manager.open_new(tmp_path / n) for n in ("a", "b", "c")]
        await start_turn(controllers[1], factory)
        await manager.close_all()
        assert [s.closed for s in factory.sessions] == [1, 1, 1]
        assert manager.controllers == [] and all(c.state is SessionState.CLOSED for c in controllers)

    run(scenario())


def test_close_all_gives_up_after_the_timeout_instead_of_hanging(tmp_path):
    async def scenario():
        factory, manager = make()
        await manager.open_new(tmp_path)
        factory.sessions[0].close_delay = 5
        started = asyncio.get_running_loop().time()
        await manager.close_all(timeout=0.1)
        assert asyncio.get_running_loop().time() - started < 1.5
        assert manager.controllers == []

    run(scenario())