"""``core.harness.loop``: the agent loop with a scripted model (the fake service's turns, read by the real stream parser)
and fake tools. What matters here is the history it leaves, which is what the next request sends."""

from __future__ import annotations

import asyncio
import copy
import json
from typing import Any

import pytest

from desktop.core.chatgpt.errors import ChatGPTError
from desktop.core.chatgpt.responses import read_events
from desktop.core.harness.loop import (
    ALLOW,
    ASK,
    DECLINED,
    DENY,
    INTERRUPTED,
    AgentLoop,
    Call,
    Observer,
    Outcome,
    Steer,
    Verdict,
    clip_output,
)
from tests.fake_responses import call, calls, cut, fail, say, usage


class Script:
    """A ``Stream``: answers each request with the next scripted turn and keeps the requests."""

    def __init__(self, *turns: list[Any]) -> None:
        self.turns = list(turns)
        self.requests: list[dict[str, Any]] = []
        self.gates: dict[str, asyncio.Event] = {}

    def __call__(self, request: dict[str, Any]):
        self.requests.append(copy.deepcopy(request))
        turn = self.turns.pop(0)

        async def lines():
            for event in turn:
                if isinstance(event, tuple):
                    if event[0] == "hold":
                        await self.gates.setdefault(event[1], asyncio.Event()).wait()
                    continue
                yield "data: " + json.dumps(event)
                yield ""

        return read_events(lines())


class FakeTools:
    def __init__(self, verdicts: dict[str, Verdict] | None = None) -> None:
        self.verdicts = verdicts or {}
        self.ran: list[Call] = []
        self.remembered: list[str] = []
        self.handlers: dict[str, Any] = {"echo": lambda call: Outcome(f"echo:{call.args.get('text', '')}"), "boom": self._boom}
        self.gate: asyncio.Event | None = None

    @staticmethod
    def _boom(call: Call) -> Outcome:
        raise RuntimeError("bug")

    def declarations(self) -> list[dict[str, Any]]:
        return [{"name": name, "description": name, "parameters": {"type": "object", "properties": {}}} for name in self.handlers]

    def known(self, name: str) -> bool:
        return name in self.handlers

    def check(self, call: Call) -> Verdict:
        if call.name in self.remembered:
            return Verdict(ALLOW)
        return self.verdicts.get(call.name, Verdict(ALLOW))

    def remember(self, call: Call) -> None:
        self.remembered.append(call.name)

    async def run(self, call: Call) -> Outcome:
        self.ran.append(call)
        if self.gate is not None and call.args.get("wait"):
            await self.gate.wait()
        return self.handlers[call.name](call)


class Recorder(Observer):
    def __init__(self, answer: str = "proceed_once") -> None:
        self.log: list[tuple] = []
        self.answer = answer
        self.steers: list[Steer] = []
        self.grown: list[dict[str, Any]] = []
        self.asked: list[str] = []

    def working(self, state): self.log.append(("working", state))
    def text_done(self, item_id, text): self.log.append(("text", text))
    def thinking_done(self, item_id, text): self.log.append(("thinking", text))
    def call_ready(self, call): self.log.append(("ready", call.name, bool(call.problem)))
    def call_done(self, call, outcome): self.log.append(("done", call.name, outcome.output, outcome.is_error, outcome.cancelled))
    def usage(self, usage): self.log.append(("usage", usage.input_tokens))
    def notice(self, text): self.log.append(("notice", text))
    def history_grew(self, items): self.grown.extend(items)
    def has_steer(self): return bool(self.steers)

    def take_steer(self):
        taken, self.steers = self.steers, []
        return taken

    def steer_given(self, steer): self.log.append(("steer_given", steer.text))

    async def ask(self, call, verdict):
        self.asked.append(call.name)
        return self.answer


def run(script: Script, tools: FakeTools | None = None, observer: Recorder | None = None, history: list | None = None, **kwargs: Any):
    tools = tools or FakeTools()
    observer = observer or Recorder()
    history = history if history is not None else [{"type": "message", "role": "user", "content": [{"type": "input_text", "text": "go"}]}]
    loop = AgentLoop(script, tools, observer, model="m", reasoning="low", instructions="be brief", **kwargs)
    end = asyncio.run(loop.run(history))
    return end, history, tools, observer


def kinds(history: list[dict[str, Any]]) -> list[str]:
    return [item["type"] if item["type"] != "message" else f"message:{item['role']}" for item in history]


def assert_paired(history: list[dict[str, Any]]) -> None:
    calls_ = [item["call_id"] for item in history if item["type"] == "function_call"]
    outputs = [item["call_id"] for item in history if item["type"] == "function_call_output"]
    assert sorted(calls_) == sorted(outputs), "every function_call needs exactly one output"


def test_a_plain_answer_ends_the_turn_after_one_step():
    script = Script(say("Hi!", used=usage(50, 5)))
    end, history, _, observer = run(script)
    assert (end.reason, end.steps) == ("done", 1)
    assert kinds(history) == ["message:user", "message:assistant"]
    assert ("text", "Hi!") in observer.log and ("usage", 50) in observer.log
    assert observer.grown == history[1:]  # everything appended was announced, in order
    assert script.requests[0]["model"] == "m" and script.requests[0]["reasoning"]["effort"] == "low"
    assert script.requests[0]["tools"][0]["name"] == "echo"


def test_a_tool_call_is_run_and_its_result_goes_back_to_the_model():
    script = Script(call("echo", {"text": "hi"}, call_id="c1", text="Let me check."), say("It said hi."))
    end, history, tools, observer = run(script)
    assert (end.reason, end.steps) == ("done", 2)
    assert kinds(history) == ["message:user", "message:assistant", "function_call", "function_call_output", "message:assistant"]
    assert history[3] == {"type": "function_call_output", "call_id": "c1", "output": "echo:hi"}
    second = script.requests[1]["input"]
    assert second[-1]["type"] == "function_call_output" and second[-2]["type"] == "function_call"
    assert "id" not in second[-2]  # (ids are not sent back: nothing is stored server-side)
    assert [entry for entry in observer.log if entry[0] in ("ready", "done")] == [("ready", "echo", False), ("done", "echo", "echo:hi", False, False)]
    assert_paired(history)


def test_two_calls_in_one_answer_both_run_and_both_get_outputs_in_order():
    script = Script(calls([("echo", '{"text": "a"}', "c1", "f1"), ("echo", '{"text": "b"}', "c2", "f2")]), say("ok"))
    _, history, tools, _ = run(script)
    assert [c.call_id for c in tools.ran] == ["c1", "c2"]
    assert kinds(history) == ["message:user", "function_call", "function_call", "function_call_output", "function_call_output", "message:assistant"]
    assert [h["output"] for h in history if h["type"] == "function_call_output"] == ["echo:a", "echo:b"]


def test_an_unknown_tool_is_reported_to_the_model_with_the_names_it_may_use():
    script = Script(call("nope", {}), say("sorry"))
    _, history, tools, _ = run(script)
    assert not tools.ran
    output = next(h["output"] for h in history if h["type"] == "function_call_output")
    assert "no tool named 'nope'" in output and "echo" in output


@pytest.mark.parametrize("raw, fragment", [("{not json", "not valid JSON"), ("[1, 2]", "must be a JSON object")])
def test_unusable_arguments_are_reported_to_the_model_and_nothing_runs(raw, fragment):
    script = Script(call("echo", raw), say("retry"))
    _, history, tools, observer = run(script)
    assert not tools.ran
    assert fragment in next(h["output"] for h in history if h["type"] == "function_call_output")
    assert ("ready", "echo", True) in observer.log


def test_empty_arguments_mean_an_empty_object():
    script = Script(call("echo", ""), say("ok"))
    _, history, tools, _ = run(script)
    assert tools.ran[0].args == {}


def test_a_call_that_needs_approval_runs_when_the_user_allows_it():
    tools = FakeTools({"echo": Verdict(ASK, "writes")})
    _, _, tools, observer = run(Script(call("echo", {"text": "x"}), say("ok")), tools)
    assert observer.asked == ["echo"] and len(tools.ran) == 1


def test_a_declined_call_does_not_run_and_the_model_is_told_not_to_retry():
    tools = FakeTools({"echo": Verdict(ASK)})
    _, history, tools, observer = run(Script(call("echo", {}), say("ok")), tools, Recorder("cancel"))
    assert not tools.ran
    assert next(h["output"] for h in history if h["type"] == "function_call_output") == DECLINED
    assert ("done", "echo", DECLINED, True, True) in observer.log


def test_allow_for_the_session_is_remembered_and_not_asked_again():
    tools = FakeTools({"echo": Verdict(ASK)})
    script = Script(call("echo", {}, call_id="c1"), call("echo", {}, call_id="c2"), say("ok"))
    _, _, tools, observer = run(script, tools, Recorder("proceed_always"))
    assert observer.asked == ["echo"] and tools.remembered == ["echo"] and len(tools.ran) == 2


def test_a_refused_call_is_not_run_and_not_asked_about():
    tools = FakeTools({"echo": Verdict(DENY, "protected path")})
    _, history, tools, observer = run(Script(call("echo", {}), say("ok")), tools)
    assert not tools.ran and not observer.asked
    assert "protected path" in next(h["output"] for h in history if h["type"] == "function_call_output")


def test_a_tool_that_crashes_is_an_error_result_not_the_end_of_the_conversation():
    end, history, _, observer = run(Script(call("boom", {}), say("ok")))
    assert end.reason == "done"
    assert "RuntimeError" in next(h["output"] for h in history if h["type"] == "function_call_output")
    assert any(entry[0] == "done" and entry[3] for entry in observer.log)


def test_stopping_while_a_tool_runs_leaves_every_call_with_an_output():
    script = Script(calls([("echo", '{"wait": true}', "c1", "f1"), ("echo", "{}", "c2", "f2")]))
    tools = FakeTools()
    history = [{"type": "message", "role": "user", "content": []}]

    async def go() -> None:
        tools.gate = asyncio.Event()
        loop = AgentLoop(script, tools, Recorder(), model="m", reasoning=None, instructions="")
        task = asyncio.ensure_future(loop.run(history))
        await asyncio.sleep(0.1)
        assert len(tools.ran) == 1
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(go())
    assert_paired(history)
    outputs = [h["output"] for h in history if h["type"] == "function_call_output"]
    assert outputs == [INTERRUPTED, INTERRUPTED]


def test_a_call_finished_before_the_stop_keeps_its_real_output():
    script = Script(calls([("echo", '{"text": "a"}', "c1", "f1"), ("echo", '{"wait": true}', "c2", "f2")]))
    tools = FakeTools()
    history = [{"type": "message", "role": "user", "content": []}]

    async def go() -> None:
        tools.gate = asyncio.Event()
        loop = AgentLoop(script, tools, Recorder(), model="m", reasoning=None, instructions="")
        task = asyncio.ensure_future(loop.run(history))
        await asyncio.sleep(0.1)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(go())
    assert [h["output"] for h in history if h["type"] == "function_call_output"] == ["echo:a", INTERRUPTED]


def test_a_stream_that_fails_leaves_the_history_as_it_was():
    script = Script(fail("subscription_sharing_usage_limit_exceeded"))
    history = [{"type": "message", "role": "user", "content": []}]
    tools, observer = FakeTools(), Recorder()
    loop = AgentLoop(script, tools, observer, model="m", reasoning=None, instructions="")
    with pytest.raises(ChatGPTError) as caught:
        asyncio.run(loop.run(history))
    assert caught.value.code == "subscription_sharing_usage_limit_exceeded"
    assert kinds(history) == ["message:user"]


def test_something_said_during_the_turn_is_given_to_the_model_at_its_next_step():
    observer = Recorder()
    script = Script(call("echo", {}), say("ok"))
    original = FakeTools.run

    async def run_and_steer(self, call_):
        observer.steers.append(Steer("also check b", ref="block"))
        return await original(self, call_)

    FakeTools.run = run_and_steer
    try:
        _, history, _, _ = run(script, observer=observer)
    finally:
        FakeTools.run = original
    assert kinds(history) == ["message:user", "function_call", "function_call_output", "message:user", "message:assistant"]
    assert history[3]["content"][0]["text"] == "also check b"
    assert ("steer_given", "also check b") in observer.log
    assert script.requests[1]["input"][-1]["content"][0]["text"] == "also check b"


def test_words_that_arrive_during_the_last_step_get_a_step_of_their_own():
    observer = Recorder()
    script = Script(say("one"), say("two"))

    def during_first_request(request):
        if not script.requests:
            observer.steers.append(Steer("wait, also this"))
        return script(request)

    loop = AgentLoop(during_first_request, FakeTools(), observer, model="m", reasoning=None, instructions="")
    history = [{"type": "message", "role": "user", "content": []}]
    end = asyncio.run(loop.run(history))
    assert end.steps == 2
    assert kinds(history)[-1] == "message:assistant" and "two" in json.dumps(history[-1])


def test_the_step_limit_stops_a_model_that_never_stops_calling_tools():
    script = Script(*[call("echo", {}, call_id=f"c{i}") for i in range(5)])
    end, history, _, _ = run(script, max_steps=3)
    assert (end.reason, end.steps) == ("step_limit", 3)
    assert len(script.requests) == 3, "three requests, not one more"
    assert_paired(history)


def test_an_answer_cut_short_is_reported_as_incomplete():
    end, _, _, _ = run(Script(cut("part")))
    assert (end.reason, end.detail) == ("incomplete", "max_output_tokens")


def test_a_huge_tool_result_is_cut_in_the_middle_before_the_model_sees_it():
    tools = FakeTools()
    tools.handlers["big"] = lambda call: Outcome("x" * 200_000)
    _, history, _, _ = run(Script(call("big", {}), say("ok")), tools)
    output = next(h["output"] for h in history if h["type"] == "function_call_output")
    assert len(output) < 61_000 and "characters omitted" in output


def test_clip_output_keeps_head_and_tail():
    text = "A" * 100 + "B" * 100
    clipped = clip_output(text, 60)
    assert clipped.startswith("A" * 40) and clipped.endswith("B" * 20) and "140 characters omitted" in clipped
    assert clip_output("short", 60) == "short"


# ------------------------------------------------------------------------------------------------ requests that fail and may pass


class Flaky:
    """A ``Stream`` whose first requests fail with the given errors (before any answer) and which then answers as ``then`` does."""

    def __init__(self, errors: list[BaseException], then: Script) -> None:
        self.errors = list(errors)
        self.then = then
        self.attempts = 0

    def __call__(self, request: dict[str, Any]):
        self.attempts += 1
        if not self.errors:
            return self.then(request)
        error = self.errors.pop(0)

        async def failing():
            raise error
            yield  # pragma: no cover

        return failing()


class Pauses:
    def __init__(self) -> None:
        self.seconds: list[float] = []

    async def __call__(self, seconds: float) -> None:
        self.seconds.append(seconds)


def run_flaky(stream, pauses: Pauses | None = None, observer: Recorder | None = None, **kwargs: Any):
    observer = observer or Recorder()
    history = [{"type": "message", "role": "user", "content": [{"type": "input_text", "text": "go"}]}]
    loop = AgentLoop(stream, FakeTools(), observer, model="m", reasoning=None, instructions="", pause=pauses or Pauses(), jitter=lambda: 1.0, **kwargs)
    return asyncio.run(loop.run(history)), history, observer


def test_a_request_the_server_refused_as_busy_is_sent_again_after_a_pause_and_the_user_is_told():
    stream = Flaky([ChatGPTError("busy", status=503), ChatGPTError("slow down", code="rate_limit_exceeded", status=429)], Script(say("Hi!")))
    pauses = Pauses()
    end, history, observer = run_flaky(stream, pauses)
    assert end.reason == "done" and stream.attempts == 3 and kinds(history) == ["message:user", "message:assistant"]
    assert pauses.seconds == [1.5 * 1.25, 4.0 * 1.25], "each pause longer than the last, with the jitter on top"
    notices = [entry[1] for entry in observer.log if entry[0] == "notice"]
    assert notices == ["服务暂时出错（HTTP 503），2 秒后自动重试（第 1/4 次）。", "请求太频繁（HTTP 429），5 秒后自动重试（第 2/4 次）。"]


def test_a_connection_that_broke_and_an_answer_cut_off_before_it_began_are_sent_again():
    stream = Flaky([ChatGPTError("reset", code="network", transient=True), ChatGPTError("The stream ended before the response was complete.", code="stream")], Script(say("ok")))
    pauses = Pauses()
    end, _history, observer = run_flaky(stream, pauses)
    assert end.reason == "done" and stream.attempts == 3
    notices = [entry[1] for entry in observer.log if entry[0] == "notice"]
    assert notices[0].startswith("连接中断了，") and notices[1].startswith("回答被截断了，")


def test_what_will_not_pass_is_not_sent_again():
    for error in (
        ChatGPTError("quota", code="insufficient_quota", status=429),
        ChatGPTError("limit", code="subscription_sharing_usage_limit_exceeded", status=429),
        ChatGPTError("bad key", status=401),
        ChatGPTError("bad request", status=400, code="invalid_request_error"),
        ValueError("a bug"),
    ):
        stream = Flaky([error], Script(say("never")))
        with pytest.raises(type(error)):
            run_flaky(stream)
        assert stream.attempts == 1, error


def test_after_the_last_repeat_the_error_is_the_error():
    stream = Flaky([ChatGPTError("busy", status=503)] * 6, Script(say("never")))
    pauses = Pauses()
    with pytest.raises(ChatGPTError, match="busy"):
        run_flaky(stream, pauses, retry_delays=(1.0, 2.0))
    assert stream.attempts == 3 and pauses.seconds == [1.25, 2.5]


def test_the_servers_own_wait_is_obeyed_within_limits():
    for asked, expected in ((7.0, 7.0), (0.2, 1.5), (500.0, 60.0)):  # (a wait shorter than our own is not shorter than ours)
        error = ChatGPTError("slow down", status=429, retry_after=asked)
        pauses = Pauses()
        run_flaky(Flaky([error], Script(say("ok"))), pauses)
        assert pauses.seconds == [expected]


def test_an_answer_that_had_begun_is_not_repeated():
    partial = say("par")[:-1] + fail("server_error")[1:]
    stream = Script(partial, say("never"))
    history = [{"type": "message", "role": "user", "content": []}]
    loop = AgentLoop(stream, FakeTools(), Recorder(), model="m", reasoning=None, instructions="", pause=Pauses())
    with pytest.raises(ChatGPTError):
        asyncio.run(loop.run(history))
    assert len(stream.requests) == 1 and kinds(history) == ["message:user"]


def test_a_stop_during_the_pause_leaves_the_history_as_it_was():
    async def go():
        history = [{"type": "message", "role": "user", "content": []}]
        started = asyncio.Event()

        async def pause(seconds: float) -> None:
            started.set()
            await asyncio.sleep(3600)

        loop = AgentLoop(Flaky([ChatGPTError("busy", status=503)], Script(say("never"))), FakeTools(), Recorder(), model="m", reasoning=None, instructions="", pause=pause)
        task = asyncio.ensure_future(loop.run(history))
        await started.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        return history

    assert kinds(asyncio.run(go())) == ["message:user"]


def test_a_history_that_is_refused_as_too_big_is_shortened_once_and_then_it_is_the_error():
    class Shrinks(Recorder):
        def __init__(self) -> None:
            super().__init__()
            self.shortened = 0

        async def prepare(self, history, estimate, overflow):
            self.shortened += 1 if overflow else 0
            return overflow

    script = Script(fail("context_length_exceeded"), fail("context_length_exceeded"), say("never"))
    observer = Shrinks()
    loop = AgentLoop(script, FakeTools(), observer, model="m", reasoning=None, instructions="", pause=Pauses())
    with pytest.raises(ChatGPTError) as caught:
        asyncio.run(loop.run([{"type": "message", "role": "user", "content": []}]))
    assert caught.value.code == "context_length_exceeded" and observer.shortened == 1 and len(script.requests) == 2


def test_an_error_is_transient_when_asking_again_may_help():
    from desktop.core.chatgpt.errors import is_transient

    assert all(is_transient(ChatGPTError("x", status=status)) for status in (408, 429, 500, 502, 503, 504, 529))
    assert all(is_transient(ChatGPTError("x", code=code)) for code in ("network", "stream", "server_error", "overloaded_error", "throttlingException", "modelStreamErrorException"))
    assert not any(is_transient(ChatGPTError("x", **fields)) for fields in ({"status": 400}, {"status": 401}, {"status": 403}, {"status": 404}, {"code": "insufficient_quota", "status": 429}, {}))
    assert not is_transient(ChatGPTError("x", code="subscription_sharing_usage_limit_exceeded", transient=True)), "a quota is not a hiccup, whatever else is said"
    assert is_transient(ChatGPTError("x", code="api", status=None, transient=True)) and not is_transient(RuntimeError("x"))
