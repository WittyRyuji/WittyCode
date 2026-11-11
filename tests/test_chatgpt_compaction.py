"""A ChatGPT Plan conversation that outgrows the window of its model (a smaller model chosen, or a long task): the front of
it becomes a summary the model reads, the log stays whole; and a turn stopped late does not leak into the next one."""

from __future__ import annotations

import asyncio
from typing import Any

from desktop.core.chatgpt import controller as controller_module
from desktop.core.chatgpt.responses import user_message
from desktop.core.context import format_tokens
from desktop.core.harness import compact
from desktop.core.harness.loop import INTERRUPTED
from desktop.core.harness.prompt import build_instructions
from desktop.core.harness.store import SavedTurn
from desktop.core.harness.toolset import HarnessTools
from desktop.core.session_manager import SessionState
from desktop.core.transcript import TurnStatus
from tests.chatgpt_support import ACCESS_TOKEN, finish_turn, make_engine, until
from tests.fake_responses import FakeResponses, call, http_error, say, usage
from tests.test_chatgpt_controller import opened, run

SOL, LUNA = "gpt-6.1-sol", "gpt-6-luna"
WORDS = "word " * 300


def assistant(text: str) -> dict[str, Any]:
    return {"type": "message", "role": "assistant", "content": [{"type": "output_text", "text": text, "annotations": []}]}


def fill(controller: Any, turns: int = 6) -> None:
    """A long conversation, put straight into the log (about 430 tokens a message)."""
    for number in range(turns):
        start = len(controller.saved.items)
        controller.saved.items += [user_message(f"question {number} {WORDS}"), assistant(f"answer {number} {WORDS}")]
        controller.saved.turns.append(SavedTurn(start, 100.0 + number, 101.0 + number, "done", "", controller.model))


async def roomy(controller: Any, room: int = 3000) -> int:
    """A window ``room`` tokens bigger than what every request of ``controller`` holds besides the conversation (its
    instructions and tools, which count since the whole request has to fit): too small for the long conversation, big
    enough for its summary, however long the instructions and the tools become."""
    tools = await controller._ensure_tools()
    instructions = build_instructions(controller.project, model=controller.model, level=controller.autonomy, servers=tools.servers())
    return compact.fixed_tokens(instructions, tools.declarations(), controller.manager.shape) + room


def notices(controller: Any) -> list[str]:
    return [block.text for turn in controller.transcript.turns for block in turn.blocks if block.kind == "notice"]


def inputs(request: dict[str, Any]) -> list[dict[str, Any]]:
    return request["input"]


def is_summary_request(request: dict[str, Any]) -> bool:
    return request["instructions"] == compact.INSTRUCTIONS


# ----------------------------------------------------------------------------------------------- the next request does not fit


def test_a_conversation_too_big_for_the_window_is_summarised_before_the_next_request(tmp_path):
    async def go() -> None:
        with FakeResponses(token=ACCESS_TOKEN) as fake:
            fake.script(say("SUMMARY OF THE EARLIER PART", used=usage(900, 40)), say("Here is the answer."))
            engine, controller = await opened(tmp_path, fake)
            fill(controller)
            window = engine._windows[SOL] = await roomy(controller)
            logged = len(controller.saved.items)
            seen = []
            controller.add_listener(lambda ctl, kind: seen.append(ctl.context) if kind == "context" else None)
            controller.send_prompt("next question")
            await finish_turn(controller)
            assert any(state.compacted and state.used is None for state in seen), "after the compaction the size is unknown until the next answer"
            assert all(state.used != 940 for state in seen), "the summary request says nothing about how full the conversation is"

            summary, answer = fake.requests
            assert is_summary_request(summary) and "tools" not in summary and summary["reasoning"]["effort"] == "low"
            assert not is_summary_request(answer) and "tools" in answer
            assert compact.is_summary(inputs(answer)[0]) and "SUMMARY OF THE EARLIER PART" in compact.summary_text(inputs(answer)[0])
            assert "next question" in str(inputs(answer)[-1]) and len(inputs(answer)) < logged

            assert len(controller.saved.items) == logged + 2, "the log keeps everything: the question and its answer were added"
            record = controller.saved.compaction
            assert 0 < record["upto"] <= logged and record["mechanical"] is False and record["model"] == SOL, "the tail kept is the new question"
            assert compact.view_of(controller.saved.items, record)[1:] == controller.saved.items[record["upto"] :]
            assert engine.sessions.load(controller.session_id).compaction == record, "it is on disk"
            assert any("已压缩" in text and format_tokens(window) in text for text in notices(controller))
            assert controller.usage.input_tokens == 1020, "the summary request is counted like any other (900 + 120)"
            assert controller.saved.context_used == 150
            assert controller.context.compacted is False and controller.context.used == 150, "the next measure replaced the unknown"
            await engine.close_all()

    run(go())


def test_a_conversation_that_fits_is_sent_whole(tmp_path):
    async def go() -> None:
        with FakeResponses(token=ACCESS_TOKEN) as fake:
            fake.script(say("fine"))
            engine, controller = await opened(tmp_path, fake)
            fill(controller)
            engine._windows[SOL] = 400_000  # (known, and far bigger than the conversation)
            controller.send_prompt("next question")
            await finish_turn(controller)
            assert len(fake.requests) == 1 and len(inputs(fake.requests[0])) == 13, "twelve earlier messages and the new one"
            assert controller.saved.compaction == {}
            await engine.close_all()

    run(go())


def test_choosing_a_model_with_a_smaller_window_makes_the_next_message_compact(tmp_path):
    async def go() -> None:
        with FakeResponses(token=ACCESS_TOKEN) as fake:
            fake.script(say("first answer"), say("SUMMARY"), say("second answer"))
            engine, controller = await opened(tmp_path, fake)
            fill(controller)
            engine._windows[SOL] = 400_000
            controller.send_prompt("one")
            await finish_turn(controller)
            assert len(fake.requests) == 1, "the big window takes all of it"

            window = engine._windows[LUNA] = await roomy(controller)
            await controller.update_settings(model=LUNA)
            assert controller.context.window == window
            controller.send_prompt("two")
            await finish_turn(controller)
            assert [request["model"] for request in fake.requests] == [SOL, LUNA, LUNA]
            assert is_summary_request(fake.requests[1]) and compact.is_summary(inputs(fake.requests[2])[0])
            assert controller.transcript.turns[-1].status is TurnStatus.DONE
            assert any("「GPT-6-Luna」" in text or "Luna" in text for text in notices(controller))
            await engine.close_all()

    run(go())


def test_a_summary_asks_for_the_lowest_reasoning_whatever_the_user_chose(tmp_path):
    async def go() -> None:
        with FakeResponses(token=ACCESS_TOKEN) as fake:
            fake.script(say("SUMMARY"), say("answer"))
            engine, controller = await opened(tmp_path, fake)
            await controller.update_settings(reasoning="high")
            fill(controller)
            engine._windows[SOL] = await roomy(controller)
            controller.send_prompt("go")
            await finish_turn(controller)
            assert fake.requests[0]["reasoning"]["effort"] == "low" and fake.requests[1]["reasoning"]["effort"] == "high"
            await engine.close_all()

    run(go())


def test_a_conversation_with_nothing_that_could_be_cut_is_sent_as_it_is(tmp_path, monkeypatch):
    async def turn(root: Any, window: int) -> tuple[Any, Any]:
        root.mkdir(parents=True)
        with FakeResponses(token=ACCESS_TOKEN) as fake:
            fake.script(say("answer"))
            engine, controller = await opened(root, fake)
            engine._windows[SOL] = window
            controller.send_prompt("a question")
            await finish_turn(controller)
            await engine.close_all()
            return fake, controller

    async def go() -> None:
        seen = spy_on_estimates(monkeypatch)
        await turn(tmp_path / "probe", 400_000)
        # (past the point where it would be compacted, but the whole request still fits the window with the answer's
        # share left: a request that would not fit is not sent, see test_harness_g3_g5_compaction)
        fake, controller = await turn(tmp_path / "run", int(seen[0][0] / 0.9))
        assert len(fake.requests) == 1 and controller.saved.compaction == {}
        assert controller.transcript.turns[0].status is TurnStatus.DONE

    run(go())


def test_a_window_that_is_not_known_is_not_compared_with_anything(tmp_path):
    async def go() -> None:
        with FakeResponses(token=ACCESS_TOKEN) as fake:
            fake.script(say("answer"))
            engine, controller = await opened(tmp_path, fake)
            fill(controller)
            engine._windows.pop(SOL, None)
            controller.send_prompt("go")
            await finish_turn(controller)
            assert len(fake.requests) == 1 and controller.saved.compaction == {}
            await engine.close_all()

    run(go())


# ----------------------------------------------------------------------------------------------- the server says it is too big


def test_a_request_the_server_refuses_as_too_big_is_summarised_and_sent_again(tmp_path):
    async def go() -> None:
        refusal = http_error(400, {"error": {"code": "context_length_exceeded", "message": "Your input exceeds the context window of this model."}})
        with FakeResponses(token=ACCESS_TOKEN) as fake:
            fake.script(refusal, say("SUMMARY AFTER THE REFUSAL"), say("answer at last"))
            engine, controller = await opened(tmp_path, fake)
            fill(controller)
            engine._windows.pop(SOL, None)
            controller.send_prompt("go")
            await finish_turn(controller)
            first, summary, second = fake.requests
            assert len(inputs(first)) == 13 and is_summary_request(summary) and not is_summary_request(second)
            assert compact.is_summary(inputs(second)[0]) and "AFTER THE REFUSAL" in compact.summary_text(inputs(second)[0])
            assert controller.transcript.turns[-1].status is TurnStatus.DONE and texts_of(controller)[-1] == "answer at last"
            assert any("服务器说对话太长" in text for text in notices(controller))
            await engine.close_all()

    run(go())


def test_a_known_window_that_the_server_still_refuses_gets_a_smaller_target(tmp_path):
    async def go() -> None:
        refusal = http_error(400, {"error": {"code": "context_length_exceeded", "message": "too long"}})
        with FakeResponses(token=ACCESS_TOKEN) as fake:
            fake.script(refusal, say("SUMMARY"), say("answer"))
            engine, controller = await opened(tmp_path, fake)
            fill(controller)
            engine._windows[SOL] = 400_000  # (known; the server says it is too small a figure)
            controller.send_prompt("go")
            await finish_turn(controller)
            assert len(fake.requests) == 3
            record = controller.saved.compaction
            assert record["after"] < record["before"] * 0.5, "the target followed the estimate, not the (too optimistic) window"
            await engine.close_all()

    run(go())


def test_an_error_that_is_not_about_size_does_not_start_a_compaction(tmp_path):
    async def go() -> None:
        with FakeResponses(token=ACCESS_TOKEN) as fake:
            fake.script(http_error(400, {"error": {"code": "invalid_request_error", "message": "oops"}}))  # (a 5xx would be asked again)
            engine, controller = await opened(tmp_path, fake)
            fill(controller)
            controller.send_prompt("go")
            await finish_turn(controller)
            assert len(fake.requests) == 1 and controller.saved.compaction == {}
            assert controller.transcript.turns[-1].status is TurnStatus.FAILED
            await engine.close_all()

    run(go())


# ----------------------------------------------------------------------------------------------- when the summary cannot be written


def test_a_summary_that_cannot_be_written_is_a_list_of_what_the_user_asked_and_the_conversation_goes_on(tmp_path):
    async def go() -> None:
        with FakeResponses(token=ACCESS_TOKEN) as fake:
            fake.script(http_error(500, {"error": {"code": "server_error", "message": "overloaded"}}), say("answer anyway"))
            engine, controller = await opened(tmp_path, fake)
            fill(controller)
            engine._windows[SOL] = await roomy(controller)  # (the digest, with the instructions and the tools, has to fit the window too)
            controller.send_prompt("go")
            await finish_turn(controller)
            record = controller.saved.compaction
            assert record["mechanical"] is True and "question 0" in record["summary"] and "could not be summarised" in record["summary"]
            assert controller.transcript.turns[-1].status is TurnStatus.DONE
            assert any("摘要没写成" in text and "overloaded" in text for text in notices(controller))
            await engine.close_all()

    run(go())


def test_a_stop_while_the_summary_is_written_changes_nothing(tmp_path):
    async def go() -> None:
        with FakeResponses(token=ACCESS_TOKEN) as fake:
            fake.script([("hold", "summary"), *say("SUMMARY")], say("S2"), say("answer"))
            engine, controller = await opened(tmp_path, fake)
            fill(controller)
            engine._windows[SOL] = await roomy(controller)
            logged = len(controller.saved.items)
            controller.send_prompt("first try")
            await until(lambda: len(fake.requests) == 1, message="the summary request")
            assert controller.transcript.working_state.value == "compacting_conversation", "the page says what is going on"
            await controller.interrupt()
            assert controller.state is SessionState.READY and controller.transcript.turns[-1].status is TurnStatus.INTERRUPTED
            assert controller.saved.compaction == {}, "nothing was committed"
            assert len(controller.saved.items) == logged + 1, "only the user's message was added to the log"
            fake.release("summary")
            controller.send_prompt("second try")
            await finish_turn(controller)
            assert controller.saved.compaction["upto"] > 0 and controller.transcript.turns[-1].status is TurnStatus.DONE
            await engine.close_all()

    run(go())


# ----------------------------------------------------------------------------------------------- going on from a compaction


def test_a_reopened_conversation_reads_the_summary_and_shows_all_it_ever_said(tmp_path):
    async def go() -> None:
        with FakeResponses(token=ACCESS_TOKEN) as fake:
            fake.script(say("SUMMARY"), say("answer one"), say("answer two"))
            engine, controller = await opened(tmp_path, fake)
            fill(controller)
            engine._windows[SOL] = await roomy(controller)
            controller.send_prompt("one")
            await finish_turn(controller)
            session_id, record = controller.session_id, dict(controller.saved.compaction)
            await controller.close()

            again = make_engine(tmp_path, fake)
            reopened = await again.open_saved(tmp_path / "project", session_id)
            assert len(reopened.transcript.turns) == 7, "the page shows every turn, summarised or not"
            reopened.send_prompt("two")
            await finish_turn(reopened)
            request = fake.requests[-1]
            assert compact.is_summary(inputs(request)[0]) and len(inputs(request)) == 1 + len(reopened.saved.items) - 2 - record["upto"] + 1
            assert reopened.saved.compaction == record, "400 000 tokens of window: nothing more to do"
            await again.close_all()

    run(go())


def test_a_second_compaction_carries_the_first_summary_and_the_places_stay_in_order(tmp_path):
    async def go() -> None:
        with FakeResponses(token=ACCESS_TOKEN) as fake:
            fake.script(say("FIRST SUMMARY"), say("answer one"), say("SECOND SUMMARY"), say("answer two"))
            engine, controller = await opened(tmp_path, fake)
            fill(controller)
            engine._windows[SOL] = await roomy(controller)
            controller.send_prompt("one")
            await finish_turn(controller)
            first = dict(controller.saved.compaction)
            fill(controller, turns=8)  # (well past the point: the figure the server gave for the first turn is a few hundred tokens)
            controller.send_prompt("two")
            await finish_turn(controller)
            second = controller.saved.compaction
            assert second["upto"] > first["upto"] and second["summary"] == "SECOND SUMMARY"
            body = fake.requests[2]["input"][0]["content"][0]["text"]
            assert is_summary_request(fake.requests[2]) and "<previous_summary>\nFIRST SUMMARY\n" in body
            assert compact.view_of(controller.saved.items, second)[1:] == controller.saved.items[second["upto"] :]
            # and the place is where the model's own request began, not just where the file says it did
            sent = inputs(fake.requests[3])
            assert compact.is_summary(sent[0]) and "SECOND SUMMARY" in compact.summary_text(sent[0])
            assert sent[1:] == controller.saved.items[second["upto"] : second["upto"] + len(sent) - 1]
            await engine.close_all()

    run(go())


def test_a_compaction_that_cannot_make_the_conversation_small_is_not_repeated_at_the_next_step(tmp_path):
    async def go() -> None:
        (tmp_path / "project").mkdir()
        (tmp_path / "project" / "big.txt").write_text("a line that is long enough to count\n" * 300, encoding="utf-8")
        (tmp_path / "project" / "small.txt").write_text("tiny", encoding="utf-8")
        with FakeResponses(token=ACCESS_TOKEN) as fake:
            fake.script(
                # (the server says the conversation is big already; the big file still fits what a compaction keeps word for word)
                call("read_file", {"file_path": "big.txt"}, call_id="c1", used=usage(11_000, 20)),
                say("SUMMARY ONE"),
                # (what the server would report: the big file is still in what it was given)
                call("read_file", {"file_path": "small.txt"}, call_id="c2", used=usage(14_400, 20)),
                say("Done."),
            )
            engine, controller = await opened(tmp_path, fake)
            engine._windows[SOL] = 16_000
            controller.send_prompt("read both")
            await finish_turn(controller)
            assert controller.transcript.turns[-1].status is TurnStatus.DONE
            assert len([request for request in fake.requests if is_summary_request(request)]) == 1, "the big file is still in the view, summarising again would change nothing"
            assert len(fake.requests) == 4
            assert controller.saved.compaction == {}, "a summary that made nothing smaller is not kept"
            await engine.close_all()

    run(go())


def test_a_long_turn_is_compacted_in_the_middle_and_its_calls_stay_with_their_outputs(tmp_path):
    async def go() -> None:
        (tmp_path / "project").mkdir()
        (tmp_path / "project" / "big.txt").write_text("line of text\n" * 400, encoding="utf-8")
        with FakeResponses(token=ACCESS_TOKEN) as fake:
            fake.script(
                call("read_file", {"file_path": "big.txt"}, call_id="c1", text="Reading.", used=usage(2500, 30)),
                call("read_file", {"file_path": "big.txt"}, call_id="c2", text="Reading again.", used=usage(5600, 30)),
                say("SUMMARY OF THE WORK SO FAR"),
                say("Done."),
            )
            engine, controller = await opened(tmp_path, fake)
            engine._windows[SOL] = 8000
            controller.send_prompt("read it twice")
            await finish_turn(controller)
            assert controller.transcript.turns[-1].status is TurnStatus.DONE
            final = inputs(fake.requests[-1])
            calls_ = [item["call_id"] for item in final if item["type"] == "function_call"]
            outputs = [item["call_id"] for item in final if item["type"] == "function_call_output"]
            assert sorted(calls_) == sorted(outputs), "no call was left without its output, and no output without its call"
            assert any(is_summary_request(request) for request in fake.requests)
            await engine.close_all()

    run(go())


# ----------------------------------------------------------------------------------------------- the user's own threshold


async def long_conversation_turn(root: Any, *, window: int | None, auto: bool = True, at: int | None = None,
                                 refuse: bool = False) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """The long conversation and one more message, with the settings changed after the engine was made (they are read at every
    request, not once). Returns the requests that were sent and the compaction record."""
    root.mkdir(parents=True)
    refusal = http_error(400, {"error": {"code": "context_length_exceeded", "message": "too long"}})
    with FakeResponses(token=ACCESS_TOKEN) as fake:
        fake.script(*([refusal] if refuse else []), say("SUMMARY"), say("answer"))
        engine, controller = await opened(root, fake)
        fill(controller)
        if window is None:
            engine._windows.pop(SOL, None)
        else:
            engine._windows[SOL] = window
        engine.settings.chatgpt_compact = auto
        if at is not None:
            engine.settings.chatgpt_compact_at = at
        controller.send_prompt("go")
        await finish_turn(controller)
        assert controller.transcript.turns[-1].status is TurnStatus.DONE
        record = dict(controller.saved.compaction)
        await engine.close_all()
        return list(fake.requests), record


def spy_on_estimates(monkeypatch) -> list[tuple[int, bool]]:
    seen: list[tuple[int, bool]] = []
    original = controller_module._Watch.prepare

    async def spy(self, history, estimate, overflow):
        seen.append((estimate, overflow))
        return await original(self, history, estimate, overflow)

    monkeypatch.setattr(controller_module._Watch, "prepare", spy)
    return seen


def test_the_percent_the_user_chose_decides_when_the_conversation_is_summarised(tmp_path, monkeypatch):
    async def go() -> None:
        seen = spy_on_estimates(monkeypatch)
        await long_conversation_turn(tmp_path / "probe", window=400_000)
        size = seen[0][0]
        assert size > 2000

        # (at: the percent the user chose; fullness: how full the window is with this conversation; summarised: the expected answer)
        for number, (at, fullness, summarised) in enumerate([
            (85, 0.90, True), (95, 0.90, False), (95, 0.97, True), (50, 0.60, True), (65, 0.60, False), (50, 0.40, False),
        ]):
            requests, record = await long_conversation_turn(tmp_path / f"run{number}", window=int(size / fullness), at=at)
            assert any(is_summary_request(request) for request in requests) is summarised, (at, fullness)
            assert bool(record) is summarised, (at, fullness)

    run(go())


def test_the_default_is_to_summarise_at_85_percent_of_the_window(tmp_path, monkeypatch):
    async def go() -> None:
        seen = spy_on_estimates(monkeypatch)
        await long_conversation_turn(tmp_path / "probe", window=400_000)
        size = seen[0][0]
        for number, (fullness, summarised) in enumerate([(0.80, False), (0.90, True)]):
            requests, _ = await long_conversation_turn(tmp_path / f"run{number}", window=int(size / fullness))
            assert any(is_summary_request(request) for request in requests) is summarised, fullness

    run(go())


def test_with_automatic_compaction_off_a_conversation_over_its_window_is_sent_as_it_is(tmp_path, monkeypatch):
    async def go() -> None:
        seen = spy_on_estimates(monkeypatch)
        await long_conversation_turn(tmp_path / "probe", window=400_000)
        requests, record = await long_conversation_turn(tmp_path / "off", window=seen[0][0] // 2, auto=False)
        assert len(requests) == 1 and not is_summary_request(requests[0]) and record == {}, "the user asked for no summary before the server says no"

    run(go())


def test_with_automatic_compaction_off_the_servers_refusal_still_gets_its_summary(tmp_path, monkeypatch):
    async def go() -> None:
        seen = spy_on_estimates(monkeypatch)
        for number, window in enumerate([None, 400_000]):
            requests, record = await long_conversation_turn(tmp_path / f"run{number}", window=window, auto=False, refuse=True)
            first, summary, second = requests
            assert not is_summary_request(first) and is_summary_request(summary) and compact.is_summary(inputs(second)[0]), "a conversation must not get stuck"
            assert record["mechanical"] is False
        assert sum(1 for _, overflow in seen if overflow) == 2, "once for each refusal"

    run(go())


# ----------------------------------------------------------------------------------------------- a turn that was stopped late


def texts_of(controller: Any) -> list[str]:
    return [block.text for turn in controller.transcript.turns for block in turn.blocks if block.kind == "text"]


def test_a_turn_stopped_late_leaves_the_next_turn_and_the_log_alone(tmp_path, monkeypatch):
    async def go() -> None:
        started = asyncio.Event()

        async def slow_run(self, call_):  # a tool that takes longer to wind down than the stop is willing to wait
            started.set()
            try:
                await asyncio.sleep(60)
            except asyncio.CancelledError:
                await asyncio.sleep(0.8)
                raise

        monkeypatch.setattr(controller_module, "INTERRUPT_GRACE_SECONDS", 0.15)
        monkeypatch.setattr(HarnessTools, "run", slow_run)
        with FakeResponses(token=ACCESS_TOKEN) as fake:
            fake.script(call("read_file", {"file_path": "a.txt"}, call_id="old-call"), say("second answer"))
            engine, controller = await opened(tmp_path, fake)
            controller.send_prompt("first")
            await asyncio.wait_for(started.wait(), 10)
            old = controller._turn_task
            await controller.interrupt()
            assert controller.state is SessionState.READY and not old.done(), "the old loop is still winding down"

            controller.send_prompt("second")
            await finish_turn(controller)
            request = inputs(fake.requests[1])
            assert [(item["type"], item.get("role")) for item in request] == [
                ("message", "user"), ("function_call", None), ("function_call_output", None), ("message", "user"),
            ]
            assert request[2] == {"type": "function_call_output", "call_id": "old-call", "output": INTERRUPTED}

            await asyncio.wait({old}, timeout=10)
            assert old.done()
            items = controller.saved.items
            assert [item["type"] for item in items] == ["message", "function_call", "function_call_output", "message", "message"], "nothing from the old loop was added"
            starts = [turn.start for turn in controller.saved.turns]
            assert starts == [0, 3] and items[3]["role"] == "user" and "second" in str(items[3])
            assert controller.transcript.turns[1].tools == {} and controller.transcript.turns[1].status is TurnStatus.DONE
            on_disk = engine.sessions.load(controller.session_id)
            assert [turn.start for turn in on_disk.turns] == [0, 3] and [item["type"] for item in on_disk.items] == [item["type"] for item in items]
            await engine.close_all()

    run(go())
