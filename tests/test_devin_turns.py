"""Devin sessions against the fake `devin acp`: turns, approvals, steering, stop, crash and reconnect, history, settings."""

from __future__ import annotations

import asyncio

from desktop.core.attachments import image_attachment, pdf_attachment, text_attachment
from desktop.core.elicit import DENY, OPEN
from desktop.core.devin import controller as devin_controller
from desktop.core.devin.controller import DevinController
from desktop.core.handoff import build_handoff
from desktop.core.session_state import LiveSettings, SessionState
from desktop.core.transcript import (
    ActivityBlock,
    ErrorBlock,
    NoticeBlock,
    PromptStatus,
    TextBlockView,
    ThinkingEntry,
    ToolStatus,
    Transcript,
    TurnStatus,
    UserBlock,
)
from desktop.core.usage import Tokens
from tests.devin_support import finish_turn, make_engine, run, until
from tests.support import tiny_png


async def opened(harness, project, **options) -> DevinController:
    controller = await harness.engine.open_new(project, **options)
    assert controller.state is SessionState.READY, controller.last_error
    return controller


async def pending_prompt(controller: DevinController):
    await until(lambda: controller.transcript.pending_prompts())
    (prompt,) = controller.transcript.pending_prompts()
    return prompt


def tools_of(turn):
    return [entry for block in turn.blocks if isinstance(block, ActivityBlock) for entry in block.entries if hasattr(entry, "tool_use_id")]


def text_of(turn) -> str:
    return "".join(block.text for block in turn.blocks if isinstance(block, TextBlockView))


# ---------------------------------------------------------------------------------------------- a turn


def test_a_full_turn_becomes_text_thinking_and_tools(tmp_path):
    async def scenario():
        harness = make_engine(tmp_path)
        events: list[tuple[str, str]] = []
        harness.engine.add_listener(lambda c, kind: events.append((kind, c.state.value)))
        controller = await opened(harness, tmp_path, model="swe-2-high", reasoning="high", autonomy="code")
        assert controller.session_id and controller.model == "swe-2-high" and controller.engine == "devin"
        assert ("state", "opening") in events and ("state", "ready") in events
        turn = await finish_turn(controller, "[full] fix it")
        assert turn.status is TurnStatus.DONE and controller.state is SessionState.READY
        assert [type(block) for block in turn.blocks] == [UserBlock, ActivityBlock, TextBlockView, ActivityBlock, TextBlockView]
        user, thinking_group, lead, tools_group, answer = turn.blocks
        assert user.text == "[full] fix it" and user.handoff is None
        (thinking,) = thinking_group.entries
        assert isinstance(thinking, ThinkingEntry) and thinking.text == "Thinking about it." and thinking.complete and thinking.duration == 0.012
        assert lead.text == "I will look around. " and not lead.streaming
        assert answer.text == "All done." and not answer.streaming
        execute, create, edit, grep = tools_group.entries
        assert (execute.name, execute.input["command"], execute.status) == ("Execute", "echo hi", ToolStatus.DONE)
        assert execute.input["timeout"] == 20000 and execute.progress == "line 1\nline 2"
        assert execute.result == "line 1\nline 2\n\n[Process exited with code 1]"  # (Droid's marker, so the page shows the code)
        assert (create.name, create.status) == ("Create", ToolStatus.DONE) and create.input["file_path"].endswith("new.txt")
        assert create.diffs[0].is_new_file and create.diffs[0].added == 1
        assert (edit.name, edit.status) == ("Edit", ToolStatus.DONE)
        (diff,) = edit.diffs
        assert (diff.added, diff.removed) == (1, 1)
        assert (grep.name, grep.input, grep.result) == ("Grep", {"pattern": "beta", "path": str(tmp_path)}, "new.txt")
        assert controller.title == "[full] fix it"
        assert controller.working_label == "就绪" and not controller.busy
        assert ("state", "running") in events and events[-1][1] == "ready"

    run(scenario())


def test_the_cost_of_a_prompt_is_its_turn_stats_keyed_by_the_user_message(tmp_path):
    async def scenario():
        harness = make_engine(tmp_path)
        controller = await opened(harness, tmp_path)
        assert controller.usage is None and controller.usage_series() == {}
        first = await finish_turn(controller, "[full] one")
        assert first.output_tokens == 50, "what the prompt wrote: its turn stats, which come before the answer to the prompt"
        ((key, tokens),) = controller.usage_series().items()
        assert key.startswith("turn:msg-")
        # input is what was not read from the cache: (1000 - 200) + (1500 - 900); output 20 + 30; the cache reads 200 + 900
        assert tokens == Tokens(1400, 50, None, 1100)
        second = await finish_turn(controller, "second")
        assert second.output_tokens == 12
        series = controller.usage_series()
        assert len(series) == 2 and series[key] == tokens  # (the first figure never changes)
        assert controller.usage == Tokens(1400 + 900, 50 + 12, None, 1100 + 300)
        # the context is the last model call and the window the one the model has
        assert (controller.context.used, controller.context.window, controller.context.model) == (1212, 262000, "swe-2-high")

    run(scenario())


def test_an_answer_and_a_title_come_back_in_pieces(tmp_path):
    async def scenario():
        harness = make_engine(tmp_path)
        controller = await opened(harness, tmp_path)
        assert controller.title == "新会话"
        turn = await finish_turn(controller, "hello there")
        assert text_of(turn) == "echo: hello there"
        assert controller.title == "hello there" and controller.transcript.title == "hello there"
        assert controller.active_at >= controller.opened_at

    run(scenario())


def test_the_sessions_share_one_process_and_do_not_hear_each_other(tmp_path):
    async def scenario():
        harness = make_engine(tmp_path)
        first = await opened(harness, tmp_path)
        second = await opened(harness, tmp_path)
        assert len(harness.clients) == 1 and first.session_id != second.session_id
        assert first.send_prompt("[slow] first") and second.send_prompt("second")
        await until(lambda: second.state is SessionState.READY)
        assert text_of(second.transcript.turns[-1]) == "echo: second" and first.state is SessionState.RUNNING
        assert "second" not in text_of(first.transcript.turns[-1])
        await first.interrupt()

    run(scenario())


def test_a_failed_prompt_is_a_failed_turn_and_the_session_goes_on(tmp_path):
    async def scenario():
        harness = make_engine(tmp_path)
        controller = await opened(harness, tmp_path)
        turn = await finish_turn(controller, "[fail] now")
        assert turn.status is TurnStatus.FAILED and controller.state is SessionState.READY
        errors = [b for b in turn.blocks if isinstance(b, ErrorBlock)]
        assert [e.message for e in errors] == ["Devin 报错：Insufficient credits"]
        turn = await finish_turn(controller, "again")
        assert turn.status is TurnStatus.DONE and text_of(turn) == "echo: again"

    run(scenario())


# ---------------------------------------------------------------------------------------------- settings


def test_the_settings_asked_for_are_applied_to_a_new_session(tmp_path):
    async def scenario():
        harness = make_engine(tmp_path)
        controller = await opened(harness, tmp_path, model="claude-opus-5-5-medium", reasoning="max", autonomy="full")
        assert controller.live_settings() == LiveSettings("claude-opus-5-5-medium", "max", "full", "auto")
        sent = [(p["configId"], p["value"]) for p in harness.requests("session/set_config_option")]
        assert sent == [("model", "claude-opus-5-5-medium"), ("thought_level", "max")]
        assert [p["modeId"] for p in harness.requests("session/set_mode")] == ["bypass"]
        assert harness.state()["sessions"][controller.session_id]["mode"] == "bypass"
        plain = await opened(harness, tmp_path)  # (the defaults of the account: nothing to apply)
        assert plain.live_settings() == LiveSettings("swe-2-high", "high", "code", "auto")
        assert len(harness.requests("session/set_config_option")) == 2

    run(scenario())


def test_a_model_the_account_refuses_is_said_and_the_default_is_used(tmp_path):
    async def scenario():
        harness = make_engine(tmp_path)
        controller = await opened(harness, tmp_path, model="no-such-model")
        assert controller.model == "swe-2-high" and controller.requested_model == "swe-2-high"
        notes = [b.text for turn in controller.transcript.turns for b in turn.blocks if isinstance(b, NoticeBlock)]
        assert len(notes) == 1 and "no-such-model" in notes[0]

    run(scenario())


def test_settings_change_while_the_session_is_open(tmp_path):
    async def scenario():
        harness = make_engine(tmp_path)
        controller = await opened(harness, tmp_path, model="claude-opus-5-5-medium", reasoning="high")
        changes: list[str] = []
        controller.add_listener(lambda c, kind: changes.append(kind))
        # choosing a model resets the level to that model's own, then the level asked for is set
        assert await controller.update_settings(model="glm-5-2", reasoning="max")
        assert (controller.model, controller.reasoning) == ("glm-5-2", "max") and "settings" in changes
        assert controller.transcript.model == "glm-5-2"
        assert await controller.update_settings(model="adaptive")
        assert (controller.model, controller.reasoning) == ("adaptive", "")  # (no thought level for this one)
        assert await controller.update_settings(autonomy="plan")
        assert controller.autonomy == "plan" and harness.state()["sessions"][controller.session_id]["mode"] == "plan"
        assert not await controller.update_settings(autonomy="godmode")
        assert controller.autonomy == "plan"
        assert not await controller.update_settings(model="no-such-model")
        assert controller.model == "adaptive"
        notes = [b.text for b in controller.transcript.turns[-1].blocks if isinstance(b, NoticeBlock)]
        assert any("godmode" in note for note in notes) and any("设置更新失败" in note for note in notes)

    run(scenario())


def test_a_change_made_by_devin_itself_is_followed(tmp_path):
    async def scenario():
        harness = make_engine(tmp_path)
        controller = await opened(harness, tmp_path, autonomy="code")
        controller._on_update({"sessionUpdate": "current_mode_update", "currentModeId": "smart"})
        assert controller.autonomy == "smart"
        controller._on_update({"sessionUpdate": "current_mode_update", "currentModeId": "mystery"})
        assert controller.autonomy == "smart"

    run(scenario())


# ---------------------------------------------------------------------------------------------- approvals


def test_a_command_waits_for_an_answer_and_runs_when_allowed(tmp_path):
    async def scenario():
        harness = make_engine(tmp_path)
        controller = await opened(harness, tmp_path, autonomy="code")
        assert controller.send_prompt("[approve] clean")
        prompt = await pending_prompt(controller)
        assert [o.value for o in prompt.options] == ["allow_once", "allow_session", "allow_always", "switch_bypass", "cancel"]
        assert prompt.options[0].label == "允许（仅此一次）" and prompt.options[0].tone == "primary"
        assert prompt.options[1].label == "允许，本会话内同类操作不再询问：allow `rm` commands (this session)"
        assert prompt.options[4].label == "拒绝" and prompt.options[4].tone == "danger" and prompt.options[4].original == "Reject"
        (action,) = prompt.actions
        assert (action.kind, action.title, action.command, action.reason) == ("execute", "运行命令", "rm -rf build", "Write(proj)")
        item = controller.transcript.turns[-1].tools[prompt.tool_use_ids[0]]
        assert item.status is ToolStatus.AWAITING_PERMISSION and controller.working_label == "等待你的确认"
        assert controller.answer_permission(prompt.id, "allow_once")
        assert not controller.answer_permission(prompt.id, "allow_once")  # (a late second click)
        await until(lambda: controller.state is SessionState.READY)
        turn = controller.transcript.turns[-1]
        assert text_of(turn).endswith("approved: allow_once") and item.status is ToolStatus.DONE
        assert item.decision == "allow_once" and prompt.status is PromptStatus.ANSWERED
        (answer,) = [m for m in harness.answers() if "result" in m]
        assert answer["result"] == {"outcome": {"outcome": "selected", "optionId": "allow_once"}}

    run(scenario())


def test_refusing_answers_with_the_reject_option_devin_offered(tmp_path):
    async def scenario():
        harness = make_engine(tmp_path)
        controller = await opened(harness, tmp_path, autonomy="code")
        controller.send_prompt("[approve] clean")
        prompt = await pending_prompt(controller)
        assert controller.answer_permission(prompt.id, "cancel")
        await until(lambda: controller.state is SessionState.READY)
        turn = controller.transcript.turns[-1]
        (item,) = [t for t in tools_of(turn) if t.name == "Execute"]
        assert text_of(turn).endswith("rejected: reject_once")
        assert item.decision == "cancel" and item.status in (ToolStatus.CANCELLED, ToolStatus.ERROR)
        assert [m["result"] for m in harness.answers() if "result" in m] == [{"outcome": {"outcome": "selected", "optionId": "reject_once"}}]

    run(scenario())


def test_switching_to_bypass_from_a_prompt_changes_the_permission_level(tmp_path):
    async def scenario():
        harness = make_engine(tmp_path)
        controller = await opened(harness, tmp_path, autonomy="code")
        controller.send_prompt("[approve] clean")
        prompt = await pending_prompt(controller)
        assert controller.answer_permission(prompt.id, "switch_bypass")
        await until(lambda: controller.state is SessionState.READY)
        assert controller.autonomy == "full" and text_of(controller.transcript.turns[-1]).endswith("approved: switch_bypass")
        assert controller.live_settings().autonomy == "full"

    run(scenario())


def test_stopping_with_a_question_open_answers_it_cancelled(tmp_path):
    async def scenario():
        harness = make_engine(tmp_path)
        controller = await opened(harness, tmp_path, autonomy="code")
        controller.send_prompt("[approve] clean")
        prompt = await pending_prompt(controller)
        await controller.interrupt()
        assert controller.state is SessionState.READY and prompt.status is PromptStatus.EXPIRED
        assert [m["result"] for m in harness.answers() if "result" in m] == [{"outcome": {"outcome": "cancelled"}}]

    run(scenario())


def test_what_the_running_turn_has_cost_so_far_is_known_before_the_prompt_s_figure(tmp_path):
    async def scenario():
        harness = make_engine(tmp_path)
        controller = await opened(harness, tmp_path)
        controller.send_prompt("[slow] wait")
        await until(lambda: controller.usage_pending() is not None)
        assert controller.usage_pending() == Tokens(900, 10, None, 100), "the call so far: input without the cache, output, cache read"
        assert controller.usage_series() == {}, "not booked: Devin gives the prompt's own figure when the turn ends"
        await controller.interrupt()
        await until(lambda: controller.state is SessionState.READY)
        assert controller.usage_pending() is None
        await harness.engine.close_all()

    run(scenario())


def test_devin_may_ask_questions_and_the_answers_go_back_to_it(tmp_path):
    async def scenario():
        harness = make_engine(tmp_path)
        controller = await opened(harness, tmp_path, autonomy="code")
        (init,) = harness.requests("initialize")
        assert init["clientCapabilities"]["elicitation"] == {"form": {}, "url": {}}, "Devin asks only a client that says it can show questions"
        controller.send_prompt("[ask] pick one")
        prompt = await pending_prompt(controller)
        first, second = prompt.questions
        from desktop.core.viewmodel import prompt_json

        assert prompt_json(prompt)["who"] == "Devin", "the engine that asked, not the app displaying the card"
        assert first.question.startswith("Which way?") and first.options == ("fast", "safe") and not first.multi_select
        assert second.options == ("tests", "docs") and second.multi_select
        assert controller.answer_question(prompt.id, ["safe", ["tests", "docs"]])
        assert not controller.answer_question(prompt.id, ["fast", []]), "a late second answer"
        await until(lambda: controller.state is SessionState.READY)
        turn = controller.transcript.turns[-1]
        assert text_of(turn).endswith('answered: {"action": "accept", "content": {"approach": "safe", "extras": ["tests", "docs"]}}')
        controller.send_prompt("[ask] again")
        prompt = await pending_prompt(controller)
        assert controller.answer_question(prompt.id, None)
        await until(lambda: controller.state is SessionState.READY)
        assert text_of(controller.transcript.turns[-1]).endswith('answered: {"action": "cancel"}'), "skipping is cancel"
        await harness.engine.close_all()

    run(scenario())


def test_a_devin_form_does_not_repeat_its_description_and_keeps_the_actual_asker(tmp_path):
    async def scenario():
        from desktop.core.viewmodel import prompt_json

        harness = make_engine(tmp_path)
        controller = await opened(harness, tmp_path, autonomy="code")
        try:
            controller.send_prompt("[slow] wait")
            question = "如何处理这份演示配置？"
            request = {
                "sessionId": controller.session_id, "mode": "form", "message": question,
                "requestedSchema": {"type": "object", "properties": {
                    "config": {"type": "string", "title": "配置文件", "description": question, "enum": ["保留", "移除"]},
                }, "required": ["config"]},
            }
            waiting = asyncio.ensure_future(harness.engine._route_request(harness.clients[0], "elicitation/create", request, "form1"))
            prompt = await pending_prompt(controller)
            shown = prompt_json(prompt)
            assert shown["who"] == "Devin"
            assert shown["questions"] == [{"topic": "配置文件", "question": question, "options": ["保留", "移除"], "multiSelect": False}]
            assert controller.answer_question(prompt.id, ["保留"])
            assert await waiting == {"action": "accept", "content": {"config": "保留"}}
            await controller.interrupt()
        finally:
            await harness.engine.close_all()

    run(scenario())


def test_a_page_devin_asks_to_open_is_opened_only_with_consent_and_an_unknown_request_is_declined(tmp_path):
    async def scenario():
        harness = make_engine(tmp_path)
        controller = await opened(harness, tmp_path, autonomy="code")
        engine, client = harness.engine, harness.clients[0]
        controller.send_prompt("[slow] wait")
        await until(lambda: controller.state is SessionState.RUNNING and controller.transcript.active_turn is not None)
        request = {"sessionId": controller.session_id, "mode": "url", "message": "Sign in", "url": "https://example.com/connect", "elicitationId": "e1"}
        agreed = asyncio.ensure_future(engine._route_request(client, "elicitation/create", request, "x1"))
        prompt = await pending_prompt(controller)
        assert "https://example.com/connect" in prompt.questions[0].question, "the full address is shown before consent"
        assert controller.answer_question(prompt.id, [OPEN])
        assert await agreed == {"action": "accept"} and harness.opened == ["https://example.com/connect"]
        refused = asyncio.ensure_future(engine._route_request(client, "elicitation/create", {**request, "elicitationId": "e2"}, "x2"))
        prompt = await pending_prompt(controller)
        assert controller.answer_question(prompt.id, [DENY])
        assert await refused == {"action": "decline"} and len(harness.opened) == 1
        odd = await engine._route_request(client, "elicitation/create", {"sessionId": controller.session_id, "mode": "telepathy", "message": "?"}, "x3")
        assert odd == {"action": "decline"}
        assert any("不会显示的提问" in block.text for block in controller.transcript.turns[-1].blocks if isinstance(block, NoticeBlock))
        await controller.interrupt()
        await until(lambda: controller.state is SessionState.READY)
        late = await engine._route_request(client, "elicitation/create", {**request, "elicitationId": "e3"}, "x4")
        assert late == {"action": "decline"}, "no turn to show it in"
        await harness.engine.close_all()

    run(scenario())


def test_an_approval_for_a_session_nobody_shows_is_cancelled(tmp_path):
    async def scenario():
        harness = make_engine(tmp_path)
        controller = await opened(harness, tmp_path)
        engine = harness.engine
        client = harness.clients[0]
        assert await engine._route_request(client, "session/request_permission", {"sessionId": "unknown"}, 1) == {"outcome": {"outcome": "cancelled"}}
        try:
            await engine._route_request(client, "fs/read_text_file", {"sessionId": controller.session_id}, 2)
        except Exception as error:
            assert getattr(error, "code", None) == -32601
        else:
            raise AssertionError("an unknown request must be refused")

    run(scenario())


# ---------------------------------------------------------------------------------------------- steering and stopping


def test_a_message_said_to_a_running_turn_is_merged_into_it(tmp_path):
    async def scenario():
        harness = make_engine(tmp_path)
        controller = await opened(harness, tmp_path)
        assert controller.send_prompt("[slow] wait")
        await until(lambda: tools_of(controller.transcript.turns[-1]))
        outcome = await controller.steer("go ahead")
        assert outcome.ok
        turn = controller.transcript.turns[-1]
        steers = [b for b in turn.blocks if isinstance(b, UserBlock) and b.steer]
        assert [(b.text, b.pending) for b in steers] == [("go ahead", False)]  # (nothing to wait for: Devin took it in)
        await until(lambda: controller.state is SessionState.READY)
        assert len(controller.transcript.turns) == 1 and turn.status is TurnStatus.DONE
        assert text_of(turn).endswith("got: go ahead")
        assert len(harness.requests("session/prompt")) == 2
        # each prompt cost what its own calls cost, and together they are what the model calls said
        series = controller.usage_series()
        assert sorted(tokens.input_tokens for tokens in series.values()) == [300, 900]
        assert controller.take_unadopted() == []

    run(scenario())


def test_the_turn_is_over_when_every_prompt_sent_to_it_has_been_answered(tmp_path):
    async def scenario():
        harness = make_engine(tmp_path)
        controller = await opened(harness, tmp_path)
        assert controller.send_prompt("[slow][lag] wait")
        await until(lambda: tools_of(controller.transcript.turns[-1]))
        assert (await controller.steer("go ahead")).ok
        await until(lambda: len(controller.usage_series()) == 2)  # (both prompts' figures are in: the answers follow)
        await asyncio.sleep(0.15)
        assert controller.state is SessionState.RUNNING, "the first answer is not the end of the turn: the second is still to come"
        await until(lambda: controller.state is SessionState.READY)
        assert controller.transcript.turns[-1].status is TurnStatus.DONE and text_of(controller.transcript.turns[-1]).endswith("got: go ahead")

    run(scenario())


def test_saying_something_to_a_session_that_is_not_running_is_not_a_steer(tmp_path):
    async def scenario():
        harness = make_engine(tmp_path)
        controller = await opened(harness, tmp_path)
        outcome = await controller.steer("hello")
        assert not outcome.ok and outcome.over
        controller.send_prompt("[slow] wait")
        await until(lambda: tools_of(controller.transcript.turns[-1]))
        assert (await controller.steer("   ")).message == "没有内容"
        controller.transcript.mark_stopping()
        assert (await controller.steer("late")).message == "本轮正在停止"
        controller.transcript.clear_stopping()
        await controller.interrupt()

    run(scenario())


def test_a_steer_the_process_refuses_goes_back_to_the_user(tmp_path):
    async def scenario():
        harness = make_engine(tmp_path)
        controller = await opened(harness, tmp_path)
        controller.send_prompt("[slow] wait")
        await until(lambda: tools_of(controller.transcript.turns[-1]))
        kinds: list[str] = []
        controller.add_listener(lambda c, kind: kinds.append(kind))
        outcome = await controller.steer("[fail] nope")
        assert outcome.ok
        await until(lambda: controller.take_unadopted.__self__._unadopted)  # the refusal arrives
        assert "steer" in kinds
        assert controller.take_unadopted() == [("[fail] nope", ())]
        turn = controller.transcript.turns[-1]
        assert not [b for b in turn.blocks if isinstance(b, UserBlock) and b.steer]
        assert controller.state is SessionState.RUNNING
        await controller.interrupt()
        assert any("引导没有送达" in b.text for b in turn.blocks if isinstance(b, NoticeBlock))

    run(scenario())


def test_stopping_a_turn_ends_it_as_stopped_and_still_counts_what_it_used(tmp_path):
    async def scenario():
        harness = make_engine(tmp_path)
        controller = await opened(harness, tmp_path)
        controller.send_prompt("[slow] wait")
        await until(lambda: tools_of(controller.transcript.turns[-1]) and controller.usage_series() == {} and controller._calls)
        task = asyncio.ensure_future(controller.interrupt())
        await until(lambda: controller.transcript.turns[-1].stopping or controller.state is SessionState.READY)
        await task
        turn = controller.transcript.turns[-1]
        assert turn.status is TurnStatus.INTERRUPTED and controller.state is SessionState.READY and not turn.stopping
        assert harness.requests("session/cancel") == [{"sessionId": controller.session_id}]
        # a stopped turn reports no figures of its own: the one model call it made is what is counted
        ((key, tokens),) = controller.usage_series().items()
        assert tokens == Tokens(900, 10, None, 100)
        (tool,) = tools_of(turn)
        assert tool.status is ToolStatus.CANCELLED or tool.status is ToolStatus.ERROR
        turn = await finish_turn(controller, "after")
        assert text_of(turn) == "echo: after"

    run(scenario())


def test_a_stop_that_is_not_honoured_ends_the_turn_here(tmp_path, monkeypatch):
    async def scenario():
        monkeypatch.setattr(devin_controller, "INTERRUPT_GRACE_SECONDS", 0.3)
        harness = make_engine(tmp_path)
        controller = await opened(harness, tmp_path)
        controller.send_prompt("[stuck] wait")
        await until(lambda: tools_of(controller.transcript.turns[-1]))
        await controller.interrupt()
        turn = controller.transcript.turns[-1]
        assert controller.state is SessionState.READY and turn.status is TurnStatus.INTERRUPTED
        assert any("停止超时" in b.text for b in turn.blocks if isinstance(b, NoticeBlock))

    run(scenario())


def test_closing_a_running_session_cancels_it(tmp_path):
    async def scenario():
        harness = make_engine(tmp_path)
        controller = await opened(harness, tmp_path)
        controller.send_prompt("[slow] wait")
        await until(lambda: tools_of(controller.transcript.turns[-1]))
        await controller.close()
        assert controller.state is SessionState.CLOSED and harness.engine.controllers == []
        assert harness.requests("session/cancel") and harness.requests("session/close") == [{"sessionId": controller.session_id}]
        assert controller.transcript.turns[-1].status is TurnStatus.INTERRUPTED

    run(scenario())


# ---------------------------------------------------------------------------------------------- crash and reconnect


def test_a_dead_process_leaves_a_session_that_reconnects(tmp_path):
    async def scenario():
        harness = make_engine(tmp_path)
        controller = await opened(harness, tmp_path)
        await finish_turn(controller, "before")
        controller.send_prompt("[crash] now")
        await until(lambda: controller.state is SessionState.DEAD)
        turn = controller.transcript.turns[-1]
        assert turn.status is TurnStatus.FAILED
        errors = [b for b in turn.blocks if isinstance(b, ErrorBlock)]
        assert errors and errors[-1].recoverable and "重新连接" in errors[-1].message and "exit code 3" in errors[-1].message
        assert not controller.send_prompt("nothing")
        turns_before = len(controller.transcript.turns)
        await controller.reconnect()
        assert controller.state is SessionState.READY and len(harness.clients) == 2
        assert len(controller.transcript.turns) == turns_before  # (the replay is not shown a second time)
        assert controller.transcript.turns[0].user_block.text == "before"
        assert all(b.resolved for t in controller.transcript.turns for b in t.blocks if isinstance(b, ErrorBlock) and b.recoverable)
        turn = await finish_turn(controller, "after")
        assert text_of(turn) == "echo: after"

    run(scenario())


def test_the_session_comes_back_with_the_settings_it_had(tmp_path):
    async def scenario():
        harness = make_engine(tmp_path)
        controller = await opened(harness, tmp_path, model="claude-opus-5-5-medium", reasoning="high", autonomy="smart")
        controller.send_prompt("[crash] now")
        await until(lambda: controller.state is SessionState.DEAD)
        await controller.reconnect()
        assert controller.live_settings() == LiveSettings("claude-opus-5-5-medium", "high", "smart", "auto")

    run(scenario())


# ---------------------------------------------------------------------------------------------- saved sessions


def test_a_saved_session_comes_back_with_its_history(tmp_path):
    async def scenario():
        first = make_engine(tmp_path)
        original = await opened(first, tmp_path, model="claude-opus-5-5-medium", autonomy="smart")
        await finish_turn(original, "[full] one")
        await finish_turn(original, "two")
        live_turns = len(original.transcript.turns)
        live_series = original.usage_series()
        session_id = original.session_id
        title = original.title
        await first.engine.close_all()
        second = make_engine(tmp_path, home=first.home)
        listed = await second.engine.list_sessions(tmp_path)
        assert [s.id for s in listed] == [session_id] and listed[0].title == "[full] one"
        reopened = await second.engine.open_saved(tmp_path, session_id, title=listed[0].title)
        assert reopened.state is SessionState.READY, reopened.last_error
        turns = reopened.transcript.turns
        assert len(turns) == live_turns == 2
        full, plain = turns
        assert [type(b) for b in full.blocks] == [UserBlock, ActivityBlock, TextBlockView, ActivityBlock, TextBlockView]
        assert full.user_block.text == "[full] one" and full.status is TurnStatus.DONE and full.from_history
        names = [(t.name, t.status) for t in tools_of(full)]
        assert names == [("Execute", ToolStatus.DONE), ("Create", ToolStatus.DONE), ("Edit", ToolStatus.DONE), ("Grep", ToolStatus.DONE)]
        execute, create, edit, _ = tools_of(full)
        assert execute.input["command"] == "echo hi" and execute.result.endswith("[Process exited with code 1]")
        assert create.diffs[0].is_new_file and (edit.diffs[0].added, edit.diffs[0].removed) == (1, 1)
        assert text_of(full) == "I will look around. All done."
        assert text_of(plain) == "echo: two" and plain.user_block.text == "two"
        assert full.duration is not None and full.started_at is not None
        # the settings are the session's own
        assert reopened.live_settings() == LiveSettings("claude-opus-5-5-medium", "medium", "smart", "auto")
        assert reopened.title == title
        # what each prompt cost is under the same names as when it ran, with the same figures: counted once
        assert reopened.usage_series() == live_series
        assert (reopened.context.used, reopened.context.window) == (1212, 1000000)
        turn = await finish_turn(reopened, "three")
        assert text_of(turn) == "echo: three" and len(reopened.transcript.turns) == 3
        assert len(reopened.usage_series()) == 3

    run(scenario())


def test_a_steered_turn_comes_back_as_one_turn_with_the_steer_in_it(tmp_path):
    async def scenario():
        first = make_engine(tmp_path)
        original = await opened(first, tmp_path)
        original.send_prompt("[slow] wait")
        await until(lambda: tools_of(original.transcript.turns[-1]))
        await original.steer("go ahead")
        await until(lambda: original.state is SessionState.READY)
        series, session_id = original.usage_series(), original.session_id
        await first.engine.close_all()
        second = make_engine(tmp_path, home=first.home)
        reopened = await second.engine.open_saved(tmp_path, session_id)
        (turn,) = reopened.transcript.turns
        steers = [b for b in turn.blocks if isinstance(b, UserBlock) and b.steer]
        assert [(b.text, b.pending) for b in steers] == [("go ahead", False)]
        assert turn.user_block.text == "[slow] wait" and text_of(turn).endswith("got: go ahead")
        assert reopened.usage_series() == series

    run(scenario())


def test_a_stopped_turn_comes_back_without_figures_of_its_own(tmp_path):
    async def scenario():
        first = make_engine(tmp_path)
        original = await opened(first, tmp_path)
        original.send_prompt("[slow] wait")
        await until(lambda: tools_of(original.transcript.turns[-1]))
        await original.interrupt()
        await finish_turn(original, "next")
        session_id = original.session_id
        counted = original.usage_series()
        await first.engine.close_all()
        second = make_engine(tmp_path, home=first.home)
        reopened = await second.engine.open_saved(tmp_path, session_id)
        assert len(reopened.transcript.turns) == 2
        # the stopped prompt reported nothing, so only the second prompt's figure is in the replay (a series that is
        # missing is not a series that went down: the conversation's total keeps what it counted)
        assert len(counted) == 2 and len(reopened.usage_series()) == 1
        assert set(reopened.usage_series().items()) <= set(counted.items())

    run(scenario())


def test_a_session_that_cannot_be_loaded_is_dead_with_the_reason(tmp_path):
    async def scenario():
        harness = make_engine(tmp_path)
        controller = await harness.engine.open_saved(tmp_path, "no-such-session", title="gone")
        assert controller.state is SessionState.DEAD and "no-such-session" in controller.last_error
        assert controller.title == "gone"
        errors = [b for t in controller.transcript.turns for b in t.blocks if isinstance(b, ErrorBlock)]
        assert errors and errors[0].recoverable

    run(scenario())


def test_opening_a_session_that_is_open_returns_it(tmp_path):
    async def scenario():
        harness = make_engine(tmp_path)
        controller = await opened(harness, tmp_path)
        assert await harness.engine.open_saved(tmp_path, controller.session_id) is controller

    run(scenario())


# ---------------------------------------------------------------------------------------------- handoff and attachments


def test_a_handed_over_conversation_goes_with_the_first_message_only(tmp_path):
    async def scenario():
        harness = make_engine(tmp_path)
        transcript = Transcript()
        earlier = transcript.start_turn("what is 2+2")
        transcript.finish_turn(earlier, TurnStatus.DONE)
        handoff = build_handoff(transcript.turns, window_tokens=100000)
        controller = await harness.engine.open_new(tmp_path, transcript=transcript, handoff=handoff)
        assert controller.state is SessionState.READY and controller.handoff is handoff
        turn = await finish_turn(controller, "and 3+3?")
        (sent,) = [p for p in harness.requests("session/prompt")]
        first = sent["prompt"][0]["text"]
        assert first.startswith(handoff.prefix) and first.endswith("and 3+3?") and "what is 2+2" in first
        assert turn.user_block.text == "and 3+3?" and turn.user_block.handoff == (handoff.carried, handoff.truncated)
        assert controller.handoff is None  # (the session has the earlier text now)
        await finish_turn(controller, "next")
        assert harness.requests("session/prompt")[-1]["prompt"][0]["text"] == "next"

    run(scenario())


def test_attachments_go_as_blocks_and_pictures_are_checked_against_the_model(tmp_path):
    async def scenario():
        harness = make_engine(tmp_path)
        controller = await opened(harness, tmp_path, model="swe-2-high")
        await harness.engine.list_models()
        picture = image_attachment("shot.png", tiny_png(), "image/png")
        note = text_attachment("notes.txt", "remember the milk")
        assert controller.attachment_problem([picture, note]) is None
        turn = await finish_turn(controller, "look", [picture, note])
        (sent,) = harness.requests("session/prompt")
        blocks = sent["prompt"]
        assert [b["type"] for b in blocks] == ["text", "image", "text"] and blocks[0]["text"] == "look"
        assert blocks[1]["mimeType"] == "image/png" and blocks[1]["data"]
        assert blocks[2]["text"].startswith('<attachment name="notes.txt"') and "remember the milk" in blocks[2]["text"]
        assert [a.name for a in turn.user_block.attachments] == ["shot.png", "notes.txt"]
        assert text_of(turn) == "echo: look (+2 more)"
        # a model without pictures refuses them before anything is sent; a PDF without words cannot go at all
        await controller.update_settings(model="glm-5-2")
        assert "不支持图片" in controller.attachment_problem([picture])
        scan = pdf_attachment("scan.pdf", b"%PDF-1.4", parsed_text="")
        assert "没有提取到文字" in controller.attachment_problem([scan])
        assert controller.attachment_problem([note]) is None

    run(scenario())


def test_a_saved_picture_and_file_are_shown_as_attachments_when_the_session_is_reopened(tmp_path):
    async def scenario():
        first = make_engine(tmp_path)
        controller = await opened(first, tmp_path)
        picture = image_attachment("shot.png", tiny_png(), "image/png")
        note = text_attachment("notes.txt", "remember the milk")
        await finish_turn(controller, "look", [picture, note])
        session_id = controller.session_id
        await first.engine.close_all()
        second = make_engine(tmp_path, home=first.home)
        reopened = await second.engine.open_saved(tmp_path, session_id)
        user = reopened.transcript.turns[0].user_block
        assert user.text == "look"
        assert sorted((a.kind, a.name) for a in user.attachments) == [("image", "图片 1"), ("text", "notes.txt")]

    run(scenario())


def test_questions_do_not_exist_in_this_protocol(tmp_path):
    async def scenario():
        harness = make_engine(tmp_path)
        controller = await opened(harness, tmp_path)
        assert controller.answer_question("p1", ["x"]) is False

    run(scenario())
