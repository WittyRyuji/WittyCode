"""G3 G4 G5 of the harness review of 2026-11-08 (``docs/harness-review-2026-11-08.md``): a ChatGPT Plan conversation kept
inside its model's window, against a service that is strict about it.

The service here refuses what the real one would refuse, as far as this application can know it: a request bigger than
the window less the answer's share (by the application's own estimate, the instructions and the tools included), and a
history in which a tool result has no call before it or a call has no result. So these tests do not only ask whether a
summary was asked for: every request that went out must be one that could be sent.

- G3: a step whose tool results would fill the window by themselves (six reads of about 60,000 Chinese characters, a
  272K window). The results share the room the request has; a step too big to keep word for word is summarised whole,
  the user's request going with the summary word for word; when nothing can make the next request fit, the turn ends
  with the reason, nothing known to be too big is sent and nothing is sent twice; the log keeps everything.
- G4: what the user wrote reaches the summary model whole: a constraint in the middle of a long message, and a message
  bigger than one summary request (given in pieces).
- G5: a summary the user pasted as their first message is theirs, not the application's: the place where the summary
  stops stays between steps, in the file, at the next turn and after reopening; a file an earlier version wrote with the
  place inside a step is mended when it is opened.
"""

from __future__ import annotations

import asyncio
import copy
import json
from typing import Any

import pytest

from desktop.core.chatgpt.errors import ChatGPTError
from desktop.core.chatgpt.responses import call_output, history_item, read_events, user_message
from desktop.core.harness import compact
from desktop.core.harness.loop import AgentLoop, Outcome
from desktop.core.harness.prompt import build_instructions
from desktop.core.harness.store import SavedSession, SavedTurn
from desktop.core.transcript import TurnStatus
from tests.chatgpt_support import finish_turn, make_engine
from tests.fake_responses import calls, say, usage
from tests.test_chatgpt_controller import opened, run
from tests.test_harness_loop import FakeTools, Recorder, Script

SOL = "gpt-6.1-sol"
WINDOW = 272_000
CJK = "\u6587"
REQUEST = "读这六个文件，告诉我内容。不要改任何文件。"


# ----------------------------------------------------------------------------------------------- a strict service


def budget_of(window: int) -> int:
    """What a whole request may hold in a window: all of it but the answer's share, a twentieth."""
    return window - window // 20


def request_size(request: dict[str, Any]) -> int:
    """The application's own estimate of a whole request: what it reads, its instructions, its tools as they are sent."""
    return (
        compact.estimate_items(request["input"])
        + compact.estimate_text(request["instructions"])
        + compact.estimate_text(json.dumps(request.get("tools", []), ensure_ascii=False))
    )


def is_summary_request(request: dict[str, Any]) -> bool:
    return request["instructions"] == compact.INSTRUCTIONS or (request.get("tool_choice") == "none" and request["input"][-1]["content"][0]["text"].startswith(compact.SUMMARY_TASK))


def body_of(request: dict[str, Any]) -> str:
    return request["input"][0]["content"][0]["text"]


def text_of(item: dict[str, Any]) -> str:
    return "".join(str(part.get("text") or "") for part in item.get("content") or ())


def pairing_problem(items: list[dict[str, Any]]) -> str:
    """What a strict service says of a history whose calls and results are not paired ('' when they are)."""
    waiting: set[str] = set()
    for item in items:
        if item.get("type") == "function_call":
            waiting.add(item["call_id"])
        elif item.get("type") == "function_call_output":
            if item.get("call_id") not in waiting:
                return f"No tool call found for function call output with call_id {item.get('call_id')}."
            waiting.discard(item["call_id"])
    return f"No tool output found for function call {sorted(waiting)[0]}." if waiting else ""


def _measured(events: list[Any], size: int) -> list[Any]:
    """The events with the usage a service would report: the request's size in, the answer's out."""
    events = copy.deepcopy(events)
    out = 0
    for event in events:
        if isinstance(event, dict) and event.get("type") == "response.output_item.done":
            item = history_item(event["item"])
            out += compact.estimate_item(item) if item is not None else 0
    events[-1]["response"]["usage"] = usage(size, out)
    return events


def _streamed(events: list[Any]):
    async def lines():
        for event in events:
            yield "data: " + json.dumps(event)
            yield ""

    return read_events(lines())


class Strict:
    """The model's service, strict: see the module docstring. A summary request gets ``summary``; the others the next of
    ``answers``. ``limit``: a smaller limit the service keeps to itself (the window it gives is too optimistic)."""

    def __init__(self, window: int, *answers: list[Any], limit: int | None = None, summary: str = "SUMMARY of what was done.") -> None:
        self.window = window
        self.limit = limit
        self.answers = list(answers)
        self.summary = summary
        self.requests: list[dict[str, Any]] = []
        self.refused: list[tuple[str, dict[str, Any]]] = []

    @property
    def mains(self) -> list[dict[str, Any]]:
        return [request for request in self.requests if not is_summary_request(request)]

    @property
    def summaries(self) -> list[dict[str, Any]]:
        return [request for request in self.requests if is_summary_request(request)]

    def __call__(self, request: dict[str, Any]):
        request = copy.deepcopy(request)
        self.requests.append(request)
        problem = pairing_problem(request["input"])
        if problem:
            self.refused.append(("malformed", request))
            raise ChatGPTError(problem, code="invalid_request_error", status=400)
        size = request_size(request)
        if size > budget_of(self.window) or (self.limit is not None and size > self.limit):
            self.refused.append(("too long", request))
            raise ChatGPTError("Your input exceeds the context window of this model.", code="context_length_exceeded", status=400)
        events = say(self.summary) if is_summary_request(request) else self.answers.pop(0)
        return _streamed(_measured(events, size))


def assistant(text: str) -> dict[str, Any]:
    return {"type": "message", "role": "assistant", "content": [{"type": "output_text", "text": text}]}


def function_call(call_id: str, path: str = "old.txt") -> dict[str, Any]:
    return {"type": "function_call", "call_id": call_id, "name": "read_file", "arguments": json.dumps({"file_path": path})}


def fill(controller: Any, *pairs: tuple[str, str]) -> None:
    for question, answer in pairs:
        start = len(controller.saved.items)
        controller.saved.items += [user_message(question), assistant(answer)]
        controller.saved.turns.append(SavedTurn(start, 100.0 + start, 101.0 + start, "done", "", SOL))


def notices(controller: Any) -> list[str]:
    return [block.text for turn in controller.transcript.turns for block in turn.blocks if block.kind == "notice"]


async def fixed_of(controller: Any) -> int:
    """What every request of ``controller`` holds besides the conversation: its instructions and its tools."""
    tools = await controller._ensure_tools()
    instructions = build_instructions(controller.project, model=controller.model, level=controller.autonomy, servers=tools.servers())
    return compact.fixed_tokens(instructions, tools.declarations(), controller.manager.shape)


def error_of(controller: Any) -> str:
    return next(block.message for block in controller.transcript.turns[-1].blocks if block.kind == "error")


def six_files(project: Any) -> list[Any]:
    for number in range(6):
        (project / f"part-{number}.txt").write_text((CJK * 600 + "\n") * 100, encoding="utf-8")
    return calls([("read_file", json.dumps({"file_path": f"part-{number}.txt"}), f"c{number}", f"i{number}") for number in range(6)])


# ----------------------------------------------------------------------------------------------- G3: six big results


@pytest.mark.parametrize("percent", [85, 50])
def test_six_big_tool_results_share_the_room_the_request_has_and_nothing_too_big_is_sent(tmp_path, percent):
    async def go() -> None:
        engine, controller = await opened(tmp_path, None)
        engine._windows[SOL] = WINDOW
        engine.settings.chatgpt_compact_at = percent  # (the user's point of compacting, not a number of the code)
        service = Strict(WINDOW, six_files(controller.project), say("要点如下：" + "每个文件都是同一个字重复。" * 300), say("整理好了。"))
        engine.stream = service
        controller.send_prompt(REQUEST)
        await finish_turn(controller)
        assert controller.transcript.turns[-1].status is TurnStatus.DONE
        assert service.refused == [], "every request could be sent"
        assert service.summaries == [], "the six results were made to fit below the point of compacting: nothing to summarise"
        first, second = service.mains
        at = int(WINDOW * percent / 100)
        assert at * 0.95 < request_size(second) < at, "as much of the six results as fits below the user's point, no more"
        outputs = [item for item in controller.saved.items if item["type"] == "function_call_output"]
        sizes = [compact.estimate_text(item["output"]) for item in outputs]
        assert len(outputs) == 6 and all("had to share what is left of the context window" in item["output"] for item in outputs)
        assert max(sizes) - min(sizes) <= max(sizes) * 0.03, "shared alike: no result crowded out the others"

        # the next message: the conversation is past the point now, and the summary stops where the new message begins
        controller.send_prompt("继续，整理成表格。")
        await finish_turn(controller)
        assert controller.transcript.turns[-1].status is TurnStatus.DONE and service.refused == []
        items, record = controller.saved.items, controller.saved.compaction
        assert len(items) == 1 + 6 + 6 + 1 + 1 + 1 and len([i for i in items if i["type"] == "function_call_output"]) == 6, "the log keeps everything"
        assert record["upto"] == 14 and "继续，整理成表格。" in text_of(items[14])
        assert record["after"] < record["before"]
        assert REQUEST in body_of(service.summaries[0]), "the user's request is in what the summary model read"
        last = service.mains[-1]
        assert compact.is_summary(last["input"][0]) and request_size(last) < at
        await engine.close_all()

    run(go())


def test_a_step_too_big_to_keep_word_for_word_is_summarised_whole_and_the_users_request_goes_with_it():
    view = [user_message(REQUEST)]
    view += [function_call(f"c{number}", f"part-{number}.txt") for number in range(6)]
    view += [call_output(f"c{number}", CJK * 60_000) for number in range(6)]
    fixed = 15_000
    assert compact.estimate_items(view[1:]) + fixed > budget_of(WINDOW), "keeping the step word for word cannot fit"
    script = Script(say("The six files were read; each repeats one character."))
    result = asyncio.run(compact.compact(view, stream=script, model="m", reasoning=None, window=WINDOW, fixed=fixed))
    assert result is not None and result.fits
    assert result.cut == len(view) and len(result.view) == 1, "the whole step went into the summary"
    assert result.kept == REQUEST and result.view[0] == compact.summary_item(result.summary, REQUEST)
    assert REQUEST in compact.summary_text(result.view[0]), "the user's request goes on word for word"
    assert result.after < result.before and result.after + fixed <= budget_of(WINDOW)
    body = body_of(script.requests[0])
    assert body.count("TOOL RESULT:") == 6 and f"USER: {REQUEST}" in body


def test_a_compaction_reports_success_only_when_the_view_shrank_and_the_whole_request_fits():
    view: list[dict[str, Any]] = []
    for number in range(6):
        view += [user_message(f"question {number} " + "word " * 300), assistant(f"answer {number} " + "word " * 300)]

    def compacted(window: int, fixed: int, items: list[dict[str, Any]] = view) -> compact.Compacted:
        result = asyncio.run(compact.compact(items, stream=Script(say("S")), model="m", reasoning=None, window=window, fixed=fixed))
        assert result is not None
        return result

    fitting = compacted(4000, 2000)
    assert fitting.fits and fitting.after + 2000 <= budget_of(4000)
    assert not compacted(4000, 3000).fits, "smaller, but with the instructions and the tools it would still not fit"
    step = [user_message("go"), function_call("c1"), call_output("c1", "word " * 400)]
    assert not compacted(10_000, 0, step).fits, "a summary of the one message before the step makes nothing smaller"


def test_a_step_that_fits_the_window_is_kept_word_for_word_though_it_is_more_than_the_tail_share():
    view = [user_message("first " + "word " * 2000), assistant("ok"), user_message("read it"), function_call("c1"), call_output("c1", CJK * 8_000)]
    result = asyncio.run(compact.compact(view, stream=Script(say("S")), model="m", reasoning=None, window=20_000))
    assert result is not None and result.fits
    assert result.cut == 3 and result.view[1:] == view[3:], "only what is before the step is summarised"
    assert result.kept == "read it", "the request of the turn that is running goes on word for word"


def test_the_words_kept_with_a_summary_go_on_while_their_turn_does_and_are_summarised_after_it():
    words = "the user's request: 不要改 billing"
    earlier = compact.summary_item("S1", words)
    step = [function_call("c1"), call_output("c1", CJK * 30_000)]
    script = Script(say("S2"))
    same = asyncio.run(compact.compact(
        [earlier, *step], stream=script, model="m", reasoning=None, window=40_000, synthetic=1, previous="S1", kept=words, turn=0,
    ))
    assert same is not None and same.fits and same.cut == 3 and same.kept == words, "the turn began in what S1 covers"
    assert compact.KEPT_HEAD not in body_of(script.requests[0])
    script = Script(say("S3"))
    later = asyncio.run(compact.compact(
        [earlier, user_message("now this"), *step], stream=script, model="m", reasoning=None, window=40_000, synthetic=1,
        previous="S1", kept=words, turn=1,
    ))
    assert later is not None and later.kept == "now this", "a new turn: what is kept is its own"
    assert f"S1\n\n{compact.KEPT_HEAD}\n\n{words}" in body_of(script.requests[0]), "and the earlier words are the summary model's to keep"


def test_a_step_refused_as_too_big_is_summarised_whole_and_asked_once_more(tmp_path):
    async def go() -> None:
        engine, controller = await opened(tmp_path, None)
        engine._windows[SOL] = WINDOW
        engine.settings.chatgpt_compact = False  # (no summary before the server says no: the results share the window)
        service = Strict(WINDOW, six_files(controller.project), say("这是六个文件的内容摘要。"), limit=200_000)
        engine.stream = service
        controller.send_prompt(REQUEST)
        await finish_turn(controller)
        assert controller.transcript.turns[-1].status is TurnStatus.DONE
        assert [reason for reason, _ in service.refused] == ["too long"], "refused once; what was refused is not sent again"
        first, refused, retry = service.mains
        assert request_size(refused) <= budget_of(WINDOW), "the results were shared out to the window"
        assert request_size(retry) <= 200_000 and len(service.summaries) == 1
        items, record = controller.saved.items, controller.saved.compaction
        assert len(items) == 14 and record["upto"] == 13, "the summary covers the whole step; the log keeps it all"
        assert REQUEST in record["kept"] and retry["input"] == [compact.summary_item(record["summary"], record["kept"])]
        assert REQUEST in text_of(retry["input"][0]), "the user's request goes on word for word"
        body = body_of(service.summaries[0])
        assert body.count("TOOL RESULT:") == 6 and REQUEST in body
        assert any("刚完成的这一步也在内" in text for text in notices(controller))
        await engine.close_all()

    run(go())


def test_a_long_turn_compacted_twice_keeps_the_users_request_word_for_word(tmp_path):
    async def go() -> None:
        engine, controller = await opened(tmp_path, None)
        engine._windows[SOL] = WINDOW
        engine.settings.chatgpt_compact = False
        first = six_files(controller.project)
        again = calls([("read_file", json.dumps({"file_path": f"part-{number}.txt"}), f"d{number}", f"j{number}") for number in range(6)])
        service = Strict(WINDOW, first, again, say("两批都读完了。"), limit=200_000)
        engine.stream = service
        controller.send_prompt(REQUEST)
        await finish_turn(controller)
        assert controller.transcript.turns[-1].status is TurnStatus.DONE
        assert [reason for reason, _ in service.refused] == ["too long", "too long"] and len(service.summaries) == 2
        items, record = controller.saved.items, controller.saved.compaction
        assert len(items) == 26 and record["upto"] == 25 and REQUEST in record["kept"]
        assert REQUEST in text_of(service.mains[-1]["input"][0]), "still word for word after the second summary"
        second = body_of(service.summaries[1])
        assert "<previous_summary>\nSUMMARY of what was done.\n" in second and second.count("TOOL RESULT:") == 6
        assert REQUEST not in second, "the words of the running turn go along with the summary, not into it"
        await engine.close_all()

    run(go())


def test_a_request_that_cannot_be_made_to_fit_ends_the_turn_with_the_reason_and_is_never_sent(tmp_path):
    async def go() -> None:
        engine, controller = await opened(tmp_path, None)
        # (long instructions: what every request holds besides the conversation counts too)
        (controller.project / "AGENTS.md").write_text("# 规则\n\n" + "项目说明" * 5_000, encoding="utf-8")
        engine._windows[SOL] = 60_000
        # (an earlier part worth summarising: without the instructions the summary and the message would fit)
        fill(controller, *[(f"第{number}个问题" + CJK * 2_500, f"第{number}个回答" + CJK * 2_500) for number in range(4)])
        before = copy.deepcopy(controller.saved.items)
        service = Strict(60_000, say("never"))
        engine.stream = service
        controller.send_prompt("请看这段日志：\n" + CJK * 40_000)
        await finish_turn(controller)
        assert controller.transcript.turns[-1].status is TurnStatus.FAILED
        message = error_of(controller)
        assert "「GPT-6.1-Sol」的窗口（60K）" in message and "没有发出" in message and "完整记录还在" in message
        assert service.mains == [] and service.refused == [], "a request known to be too big is not sent"
        assert len(service.summaries) <= 1
        assert controller.saved.items == before and controller.saved.compaction == {}, "the log is as it was"
        await engine.close_all()

    run(go())


def test_a_refusal_nothing_can_help_with_ends_the_turn_without_sending_it_again(tmp_path):
    async def go() -> None:
        engine, controller = await opened(tmp_path, None)
        engine._windows[SOL] = 60_000
        engine.settings.chatgpt_compact = False  # (sent as it is: the server decides)
        service = Strict(60_000, say("never"))
        engine.stream = service
        controller.send_prompt(CJK * 70_000)
        await finish_turn(controller)
        assert controller.transcript.turns[-1].status is TurnStatus.FAILED
        message = error_of(controller)
        assert message.startswith("服务器说对话太长") and "没有发出" in message
        assert len(service.mains) == 1 and [reason for reason, _ in service.refused] == ["too long"] and service.summaries == []
        await engine.close_all()

    run(go())


class Room(Recorder):
    def __init__(self, room: int | None) -> None:
        super().__init__()
        self.room = room
        self.told: list[tuple[int, int]] = []

    def tool_room(self, estimate: int, step: int) -> int | None:
        self.told.append((estimate, step))
        return self.room


def test_the_results_of_one_step_share_the_room_in_turn_and_a_short_one_is_left_whole():
    tools = FakeTools()
    tools.handlers["big"] = lambda call: Outcome("x" * 35_000)  # (about 10,000 tokens)
    tools.handlers["small"] = lambda call: Outcome("tiny")
    script = Script(calls([("big", "{}", "c1", "i1"), ("small", "{}", "c2", "i2"), ("big", "{}", "c3", "i3")], used=usage(900, 60)), say("done"))
    observer = Room(6_000)
    history: list[dict[str, Any]] = [user_message("go")]
    asyncio.run(AgentLoop(script, tools, observer, model="m", reasoning=None, instructions="x").run(history))
    outputs = [item for item in history if item["type"] == "function_call_output"]
    sizes = [compact.estimate_item(item) for item in outputs]
    assert outputs[1]["output"] == "tiny", "a result that fits its share is left whole"
    assert sum(sizes) <= 6_000 and sizes[0] <= 2_000 < sizes[2], "the last one gets what the ones before it left"
    assert "had to share" in outputs[0]["output"] and "had to share" in outputs[2]["output"]
    estimate, step = observer.told[0]
    assert estimate == 960 and step == compact.estimate_items(history[1:4]), "told the measure and the answer that made the calls"
    unlimited: list[dict[str, Any]] = [user_message("go")]
    script = Script(calls([("big", "{}", "c1", "i1")]), say("done"))
    asyncio.run(AgentLoop(script, tools, Room(None), model="m", reasoning=None, instructions="x").run(unlimited))
    assert unlimited[2]["output"] == "x" * 35_000, "no room given: only the limit of one result"


# ----------------------------------------------------------------------------------------------- G4: the user's words


def constrained() -> str:
    head, tail = "background " * 430, "\n" + "more detail " * 430
    middle = "不要改 billing：这是用户的明确约束。"
    message = head + middle + "x" * (9_926 - len(head) - len(middle) - len(tail)) + tail
    assert len(message) == 9_926
    return message


def test_a_constraint_in_the_middle_of_a_long_message_reaches_the_summary_request():
    message = constrained()
    view = [user_message(message), assistant("old answer " * 2000), user_message("continue")]
    script = Script(say("Summary."))
    result = asyncio.run(compact.compact(view, stream=script, model="m", reasoning=None, window=4000))
    assert result is not None and result.cut == 2
    body = body_of(script.requests[0])
    assert "不要改 billing" in body and f"USER: {message.strip()}" in body, "the whole message: head, middle and tail"
    assert "ASSISTANT: old answer" in body and "characters left out" in body, "the assistant's own words are still cut"


def test_a_message_bigger_than_one_summary_request_is_given_whole_in_pieces():
    rules = ("HEAD RULE: 不要改 billing。", "MIDDLE RULE: 不要删测试。", "TAIL RULE: 改完跑全量测试。")
    filler = "日志行的内容\n" * 2_500
    message = rules[0] + "\n" + filler + rules[1] + "\n" + filler + rules[2]
    budget = compact.chunk_budget(4000)
    assert compact.estimate_text(message) > 2 * budget
    parts = compact.summary_parts([user_message(message)], budget)
    assert len(parts) >= 3 and all(compact.estimate_text(part) <= budget for part in parts)
    pieces = [part.split("): ", 1)[1] for part in parts]
    assert all(part.startswith(f"USER (one message, part {number} of {len(parts)}): ") for number, part in enumerate(parts, 1))
    assert "".join(pieces) == message, "nothing of what the user wrote is left out"

    view = [user_message(message), assistant("ok"), user_message("next")]
    script = Script(*[say(f"S{number}") for number in range(1, 12)])
    result = asyncio.run(compact.compact(view, stream=script, model="m", reasoning=None, window=4000))
    assert result is not None and len(script.requests) >= 3
    read = "\n".join(body_of(request) for request in script.requests)
    assert all(rule in read for rule in rules), "head, middle and tail all reached the summary model"
    assert "characters left out" not in read
    assert result.summary == f"S{len(script.requests)}"


def test_a_users_constraint_reaches_the_summary_model_through_the_service(tmp_path):
    async def go() -> None:
        engine, controller = await opened(tmp_path, None)
        engine._windows[SOL] = 20_000
        fill(controller, (constrained(), "好的，不改 billing。"), *[(f"question {n} " + "word " * 1200, f"answer {n} " + "word " * 1200) for n in range(4)])
        service = Strict(20_000, say("继续做完了。"))
        engine.stream = service
        controller.send_prompt("继续")
        await finish_turn(controller)
        assert controller.transcript.turns[-1].status is TurnStatus.DONE and service.refused == []
        assert service.summaries and "不要改 billing：这是用户的明确约束。" in body_of(service.summaries[0])
        upto = controller.saved.compaction["upto"]
        assert upto > 0 and compact.is_user(controller.saved.items[upto]) and compact.is_summary(service.mains[-1]["input"][0])
        await engine.close_all()

    run(go())


# ----------------------------------------------------------------------------------------------- G5: a pasted summary


PASTED = compact.summary_item("A user copied a previous conversation's summary into a new chat.")


def test_whether_a_view_begins_with_a_summary_is_told_and_never_read_from_what_a_message_says():
    view = [PASTED, function_call("old"), call_output("old", "x"), user_message("continue")]
    assert compact.choose_cut(view, 10_000) == 3 and compact.log_index(3, 0, 0) == 3
    assert compact.log_index(3, 7, 1) == 9, "behind a summary of ours, one place less"
    assert compact.render_parts(view[:1])[0].startswith("USER: " + compact.SUMMARY_PREFIX), "what the user pasted is what the user said"
    script = Script(say("S"))
    result = asyncio.run(compact.compact(view, stream=script, model="m", reasoning=None, window=4000))
    assert result is not None and result.cut == 3
    body = body_of(script.requests[0])
    assert "(none: this is the beginning)" in body and "USER: " + compact.SUMMARY_PREFIX in body
    assert compact.boundary_ok(view, 3) and not compact.boundary_ok(view, 2) and compact.mend_boundary(view, 2) == 3
    thinking = [user_message("go"), {"type": "reasoning", "summary": [], "encrypted_content": "e"}, function_call("c"), call_output("c", "y")]
    assert not compact.boundary_ok(thinking, 2), "a reasoning item stays with what it led to"


def big_old_read(controller: Any) -> None:
    controller.saved.items = [copy.deepcopy(PASTED), function_call("old"), call_output("old", "word " * 35_000)]
    controller.saved.turns = [SavedTurn(0, 1.0, 2.0, "done", "", SOL)]


def test_a_summary_the_user_pasted_is_theirs_and_the_place_where_the_summary_stops_stays_between_steps(tmp_path):
    async def go() -> None:
        engine, controller = await opened(tmp_path, None)
        engine._windows[SOL] = 60_000
        big_old_read(controller)
        service = Strict(60_000, say("first answer"), say("second answer"))
        engine.stream = service
        controller.send_prompt("continue")
        await finish_turn(controller)
        items, record = controller.saved.items, controller.saved.compaction
        assert record["upto"] == 3 and items[3]["type"] == "message" and "continue" in text_of(items[3]), "the summary stops at a message, not at a tool result"
        summary_request = service.summaries[0]
        if summary_request.get("tool_choice") == "none":
            assert summary_request["input"][0]["role"] == "user" and text_of(summary_request["input"][0]).startswith(compact.SUMMARY_PREFIX)
        else:
            assert "USER: " + compact.SUMMARY_PREFIX in body_of(summary_request), "the pasted text was given as the user's words"
        assert engine.sessions.load(controller.session_id).compaction["upto"] == 3, "so it is on disk"
        controller.send_prompt("next")
        await finish_turn(controller)
        assert [turn.status for turn in controller.transcript.turns[-2:]] == [TurnStatus.DONE, TurnStatus.DONE]
        assert service.refused == [], "no request had a tool result without its call"

        # a second compaction, behind the first summary: the place is counted from the summary, which is not a message
        fill(controller, ("big question " + "word " * 36_000, "big answer"))
        service.answers.append(say("third answer"))
        controller.send_prompt("once more")
        await finish_turn(controller)
        items, record = controller.saved.items, controller.saved.compaction
        assert controller.transcript.turns[-1].status is TurnStatus.DONE and service.refused == []
        assert record["upto"] == len(items) - 2 == 9 and "once more" in text_of(items[9])
        second_request = service.summaries[1]
        second = body_of(second_request)
        if second_request.get("tool_choice") == "none":
            assert compact.is_summary(second_request["input"][0]) and "SUMMARY of what was done." in second
        else:
            assert "<previous_summary>\nSUMMARY of what was done." in second and "USER: " + compact.SUMMARY_PREFIX not in second
        session_id = controller.session_id
        await engine.close_all()

        again = make_engine(tmp_path, None)
        again._windows[SOL] = 60_000
        reopened = await again.open_saved(tmp_path / "project", session_id)
        assert reopened.saved.compaction["upto"] == 9
        assert pairing_problem(compact.view_of(reopened.saved.items, reopened.saved.compaction)) == ""
        service = Strict(60_000, say("third answer"))
        again.stream = service
        reopened.send_prompt("once more")
        await finish_turn(reopened)
        assert reopened.transcript.turns[-1].status is TurnStatus.DONE and service.refused == []
        await again.close_all()

    run(go())


def test_a_file_whose_summary_stops_inside_a_step_is_mended_when_it_is_opened(tmp_path):
    async def go() -> None:
        engine = make_engine(tmp_path, None)
        project = tmp_path / "project"
        project.mkdir()
        items = [copy.deepcopy(PASTED), function_call("old"), call_output("old", "result"), user_message("continue"), assistant("answer")]
        # (as an earlier version saved it: one place too early, at the result of a call the summary covers)
        engine.sessions.save(SavedSession(
            id="s1", project=str(project), title="pasted", model=SOL, items=items,
            turns=[SavedTurn(0, 1.0, 2.0, "done", "", SOL), SavedTurn(3, 3.0, 4.0, "done", "", SOL)],
            compaction={"upto": 2, "summary": "The old read completed.", "at": 5.0, "before": 100, "after": 10, "model": SOL, "mechanical": False},
        ))
        reopened = await engine.open_saved(project, "s1")
        assert reopened.saved.compaction["upto"] == 3 and engine.sessions.load("s1").compaction["upto"] == 3, "mended, and saved so"
        engine._windows[SOL] = 400_000
        service = Strict(400_000, say("fine"))
        engine.stream = service
        reopened.send_prompt("next")
        await finish_turn(reopened)
        assert reopened.transcript.turns[-1].status is TurnStatus.DONE and service.refused == []
        assert service.mains[0]["input"][1:3] == [user_message("continue"), assistant("answer")]
        await engine.close_all()

    run(go())


def test_a_summary_whose_place_in_the_log_would_split_a_step_is_not_kept(tmp_path, monkeypatch):
    async def go() -> None:
        real = compact.log_index
        monkeypatch.setattr(compact, "log_index", lambda cut, upto, synthetic: real(cut, upto, synthetic) - 1)  # (the old mistake)
        engine, controller = await opened(tmp_path, None)
        big_old_read(controller)
        # (past the point of compacting, short of the window: a summary that is not kept leaves a request that can be sent)
        window = engine._windows[SOL] = int((await fixed_of(controller) + compact.estimate_items(controller.saved.items)) / 0.9)
        service = Strict(window, say("first answer"), say("second answer"))
        engine.stream = service
        for message in ("continue", "next"):
            controller.send_prompt(message)
            await finish_turn(controller)
            assert controller.transcript.turns[-1].status is TurnStatus.DONE
        assert controller.saved.compaction == {}, "a place that would send a tool result without its call is not kept"
        assert service.refused == [] and len(service.summaries) == 2
        await engine.close_all()

    run(go())
