"""The memory curator's side question to Codex (``codex/side.ask``) against ``tests/fake_codex_side.py``: the answer comes
back from a cheap, read-only, ephemeral thread that gets no memory server and no hook, and nothing is left behind,
whatever happens to the question (an error, a failed turn, a timeout, a cancellation, the process ending)."""

from __future__ import annotations

import asyncio
import json
import sys
import time
from pathlib import Path

import pytest

from desktop.core.codex import side
from desktop.core.codex.errors import CodexRpcError
from desktop.core.codex.locate import CodexBinary
from desktop.core.codex.models import CodexModel
from desktop.core.memory.launch import MemoryLaunch
from desktop.core.session_manager import SessionState
from desktop.core.settings import AppSettings
from desktop.core.transcript import TurnStatus
from tests.codex_support import make_engine, run, until

SIDE_FAKE = Path(__file__).with_name("fake_codex_side.py")
PROMPT = 'Reply with only the JSON {"notes": []}'
TEXT = [{"type": "text", "text": PROMPT, "text_elements": []}]


def side_engine(tmp_path, **env):
    harness = make_engine(tmp_path, **env)
    binary = CodexBinary(path=SIDE_FAKE, version="0.166.0", source="test", command=(sys.executable, str(SIDE_FAKE)))
    harness.engine._locate = lambda explicit: binary
    return harness


async def connected(tmp_path, **env):
    harness = side_engine(tmp_path, **env)
    await harness.engine.connection()
    return harness


def made(harness):
    """The threads the fake made: ``[{"id", "ephemeral"}]``."""
    path = harness.home / "made.jsonl"
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()] if path.is_file() else []


def let_go(harness):
    return [params.get("threadId") for params in harness.requests("thread/unsubscribe")]


def saved(harness):
    return set(json.loads((harness.home / "state.json").read_text(encoding="utf-8"))["threads"])


async def listed(harness):
    client = await harness.engine.connection()
    result = await client.request("thread/list", {"limit": 100}, timeout=10)
    return [thread["id"] for thread in result["data"]]


def model(model_id, *, efforts=(), default=False):
    return CodexModel(id=model_id, display_name=model_id, efforts=tuple(efforts), is_default=default)


def entry(model_id, efforts, *, default=False):
    return {"id": model_id, "model": model_id, "displayName": model_id, "description": "", "hidden": False,
            "supportedReasoningEfforts": [{"reasoningEffort": e, "description": e} for e in efforts],
            "defaultReasoningEffort": efforts[-1] if efforts else "", "inputModalities": ["text"], "isDefault": default}


# ---------------------------------------------------------------------------------------------- the question


def test_the_answer_comes_back_from_an_ephemeral_read_only_thread_on_the_cheapest_model(tmp_path):
    async def scenario():
        harness = await connected(tmp_path)
        engine = harness.engine
        project = tmp_path / "the-project"
        project.mkdir()
        answer = await side.ask(engine, str(project), PROMPT)
        assert answer == "echo: " + PROMPT
        ((thread),) = made(harness)
        assert thread["ephemeral"] is True
        folder = str(side.scratch())
        assert side.scratch().parent.name == "WittyCode" and not (side.scratch() / "AGENTS.md").exists(), "a scratch folder of its own"
        assert harness.requests("thread/start") == [{
            "cwd": folder, "approvalPolicy": "never", "sandbox": "read-only", "serviceName": "wittycode", "ephemeral": True,
            "developerInstructions": side.INSTRUCTIONS, "model": "gpt-6-luna",
        }], "the cheapest model; no config (no MCP server of the memory), read-only, never asks, not in the project"
        assert harness.requests("turn/start") == [{
            "threadId": thread["id"], "input": TEXT, "cwd": folder, "approvalPolicy": "never",
            "sandboxPolicy": {"type": "readOnly", "networkAccess": False}, "model": "gpt-6-luna", "effort": "low",
        }]
        assert str(project) not in json.dumps(harness.received())
        assert let_go(harness) == [thread["id"]]
        assert harness.requests("turn/interrupt") == [] and harness.requests("thread/delete") == []
        assert thread["id"] not in engine._threads
        assert thread["id"] not in saved(harness)
        assert thread["id"] not in await listed(harness)
        assert await engine.list_sessions(folder) == []
        assert engine.controllers == [] and engine.running

    run(scenario())


def test_the_shared_memory_and_the_hooks_are_not_given_to_the_question(tmp_path):
    async def scenario():
        harness = await connected(tmp_path)
        engine = harness.engine
        engine.memory = MemoryLaunch(tmp_path / "memory", python=sys.executable)
        hooked = []
        engine.hooks.on_prompt(lambda event: hooked.append(event))
        heard = []
        engine.add_listener(lambda controller, kind: heard.append(kind))
        assert await side.ask(engine, str(tmp_path), PROMPT) == "echo: " + PROMPT
        (start,) = harness.requests("thread/start")
        assert "config" not in start and "mcp_servers" not in json.dumps(start)
        assert hooked == [] and heard == []
        controller = await engine.open_new(tmp_path)  # (a conversation does get both: the question would show if it did)
        assert "mcp_servers" in json.dumps(harness.requests("thread/start")[-1])
        await controller.close()

    run(scenario())


def test_a_turn_that_works_in_steps_answers_with_its_final_words_only(tmp_path):
    async def scenario():
        harness = await connected(tmp_path)
        assert await side.ask(harness.engine, str(tmp_path), "[full] " + PROMPT) == "Done: fixed."
        assert len(let_go(harness)) == 1

    run(scenario())


def test_what_codex_asks_for_is_refused_and_the_turn_answers_without_it(tmp_path):
    async def scenario():
        harness = await connected(tmp_path)
        engine = harness.engine
        assert await side.ask(engine, str(tmp_path), "[approve-command] go") == "not run (decline)"
        assert await side.ask(engine, str(tmp_path), "[question] go") == "answers: {}"
        reply = await side.ask(engine, str(tmp_path), "[elicit-approval] go")
        assert json.loads(reply.split(": ", 1)[1]) == {"action": "decline", "content": None, "_meta": None}
        results = [answer.get("result") for answer in harness.answers()]
        assert results == [{"decision": "decline"}, {"answers": {}}, {"action": "decline", "content": None, "_meta": None}]
        assert len(set(let_go(harness))) == 3

    run(scenario())


def test_the_side_question_refuses_every_request_it_knows_and_rejects_the_others(tmp_path):
    async def scenario():
        question = side._Question(None, None)
        for method, refusal in side.REFUSALS.items():
            answer = await question._on_server_request(method, {}, 1)
            assert answer == refusal and answer is not refusal
        with pytest.raises(CodexRpcError) as raised:
            await question._on_server_request("item/somethingNew", {}, 2)
        assert raised.value.code == -32601
        assert side.REFUSALS["item/commandExecution/requestApproval"] == {"decision": "decline"}
        assert side.REFUSALS["item/fileChange/requestApproval"] == {"decision": "decline"}
        assert side.REFUSALS["item/permissions/requestApproval"] == {"permissions": {}, "scope": "turn"}
        assert side.REFUSALS["item/tool/call"] == {"contentItems": [], "success": False}

    run(scenario())


# ---------------------------------------------------------------------------------------------- the model


def test_the_cheapest_kind_of_model_is_chosen_else_the_default_else_the_first():
    sol, luna, old_luna = model("gpt-6.1-sol", default=True), model("gpt-6-luna"), model("gpt-5.6-luna")
    assert side.side_model([sol, model("gpt-6-astra"), luna, old_luna]) is luna, "the first of the cheapest kind"
    assert side.side_model([luna, model("gpt-6-mini")]).id == "gpt-6-mini", "mini is cheaper than luna"
    assert side.side_model([model("gpt-6-flash"), model("gpt-6-lite")]).id == "gpt-6-lite"
    assert side.side_model([model("gpt-6-flash"), sol]).id == "gpt-6-flash"
    assert side.side_model([model("o9-nano"), model("o9-mini")]).id == "o9-nano"
    assert side.side_model([model("gemini-9-pro"), model("terminator"), sol]) is sol, "a word counts only as a whole part of the id"
    assert side.side_model([model("a"), model("b")]).id == "a"
    assert side.side_model([]) is None
    assert side.cheapness("GPT-6-LUNA") == 2 and side.cheapness("gpt-6-sol") == len(side.CHEAP)


def test_the_weakest_effort_is_chosen_whatever_the_order():
    assert side.lowest_effort(("high", "low", "medium")) == "low"
    assert side.lowest_effort(("xhigh", "max", "none", "minimal")) == "none"
    assert side.lowest_effort(("ultra", "minimal")) == "minimal"
    assert side.lowest_effort(("turbo", "eco")) == "turbo", "unknown names: the server lists them weakest first"
    assert side.lowest_effort(("turbo", "high")) == "high"
    assert side.lowest_effort(()) == ""


def test_without_a_cheap_model_the_default_runs_at_its_weakest_effort(tmp_path):
    async def scenario():
        models = [entry("gpt-6-astra", ["high", "medium"]), entry("gpt-6.1-sol", ["max", "low", "medium"], default=True)]
        harness = await connected(tmp_path, FAKE_SIDE_MODELS=json.dumps(models))
        assert await side.ask(harness.engine, str(tmp_path), PROMPT) == "echo: " + PROMPT
        (start,), (turn,) = harness.requests("thread/start"), harness.requests("turn/start")
        assert (start["model"], turn["model"], turn["effort"]) == ("gpt-6.1-sol", "gpt-6.1-sol", "low")

    run(scenario())


def test_a_model_without_efforts_and_an_empty_model_list(tmp_path):
    async def scenario():
        harness = await connected(tmp_path, FAKE_SIDE_MODELS=json.dumps([entry("gpt-6-luna", [])]))
        assert await side.ask(harness.engine, str(tmp_path), PROMPT) == "echo: " + PROMPT
        (turn,) = harness.requests("turn/start")
        assert turn["model"] == "gpt-6-luna" and "effort" not in turn

    run(scenario())

    async def empty():
        harness = await connected(tmp_path / "empty", FAKE_SIDE_MODELS="[]")
        assert await side.ask(harness.engine, str(tmp_path), PROMPT) == "echo: " + PROMPT
        (start,), (turn,) = harness.requests("thread/start"), harness.requests("turn/start")
        assert "model" not in start and "model" not in turn and "effort" not in turn, "Codex's own default"

    run(empty())


# ---------------------------------------------------------------------------------------------- the answer's words


def feed(question, *notifications):
    for method, params in notifications:
        question._on_notification(method, params)


def message(method, item_id, text="", phase=None):
    item = {"type": "agentMessage", "id": item_id, "text": text}
    if phase is not None:
        item["phase"] = phase
    return method, {"threadId": "t", "turnId": "u", "item": item}


def test_the_answer_is_the_agent_messages_without_the_commentary():
    question = side._Question(None, None)
    feed(
        question,
        message("item/started", "m1", phase="commentary"),
        ("item/agentMessage/delta", {"itemId": "m1", "delta": "Let me look."}),
        message("item/completed", "m1", "Let me look.", "commentary"),
        ("item/started", {"item": {"type": "reasoning", "id": "r1"}}),
        ("item/reasoning/summaryTextDelta", {"itemId": "r1", "delta": "thinking"}),
        ("item/started", {"item": {"type": "commandExecution", "id": "c1", "command": "dir"}}),
        message("item/started", "m2", phase="partial_answer"),
        ("item/agentMessage/delta", {"itemId": "m2", "delta": '{"notes": '}),
        message("item/completed", "m2", '{"notes": ', "partial_answer"),
        message("item/started", "m3"),
        ("item/agentMessage/delta", {"itemId": "m3", "delta": "[]}"}),
        message("item/completed", "m3", "[]}", "final_answer"),
    )
    assert question.answer() == '{"notes":\n\n[]}'


def test_the_words_of_the_deltas_count_until_the_message_is_complete():
    question = side._Question(None, None)
    feed(question, ("item/agentMessage/delta", {"itemId": "m1", "delta": "half "}), ("item/agentMessage/delta", {"itemId": "m1", "delta": "done"}))
    assert question.answer() == "half done"
    feed(question, message("item/completed", "m1", ""))
    assert question.answer() == "half done", "a completed message without text keeps what was streamed"
    feed(question, message("item/completed", "m1", "whole"))
    assert question.answer() == "whole", "the completed text is the message"
    feed(question, ("item/agentMessage/delta", {"itemId": "", "delta": "nobody's"}), ("item/completed", {"item": {"type": "agentMessage", "id": ""}}))
    assert question.answer() == "whole"


def test_commentary_alone_is_the_answer_when_there_is_nothing_else():
    question = side._Question(None, None)
    feed(question, message("item/completed", "m1", " only this ", "commentary"), message("item/completed", "m2", "   ", "final_answer"))
    assert question.answer() == "only this"
    assert side._Question(None, None).answer() == ""


def test_the_turn_ends_with_its_status_or_when_the_thread_is_closed():
    question = side._Question(None, None)
    feed(question, ("turn/started", {"turn": {"id": "turn-1"}}), ("turn/started", {"turn": {"id": "turn-2"}}))
    assert question.turn_id == "turn-1" and not question.ended.is_set()
    feed(question, ("turn/completed", {"turn": {"id": "turn-1", "status": "failed", "error": {"message": "limit"}}}))
    assert (question.status, question.error, question.ended.is_set()) == ("failed", "limit", True)
    feed(question, ("thread/closed", {}))
    assert question.status == "failed"
    other = side._Question(None, None)
    feed(other, ("turn/completed", {"turn": {"id": "x"}}))
    assert (other.status, other.error) == ("completed", "")
    closed = side._Question(None, None)
    feed(closed, ("thread/closed", {}))
    assert (closed.status, closed.ended.is_set()) == ("closed", True)


# ---------------------------------------------------------------------------------------------- when it cannot be asked


def test_nothing_is_asked_when_codex_is_not_running_switched_off_signed_out_or_the_prompt_is_empty(tmp_path):
    async def not_running():
        harness = side_engine(tmp_path / "a")
        assert await side.ask(harness.engine, str(tmp_path), PROMPT) is None
        assert harness.clients == [] and not harness.engine.running, "a side question never starts Codex"

    async def switched_off():
        harness = await connected(tmp_path / "b")
        harness.engine.settings = AppSettings(codex_enabled=False)
        assert await side.ask(harness.engine, str(tmp_path), PROMPT) is None
        assert harness.requests("thread/start") == []

    async def signed_out():
        harness = await connected(tmp_path / "c", account="signed-out")
        assert await side.ask(harness.engine, str(tmp_path), PROMPT) is None
        assert harness.requests("thread/start") == [] and harness.requests("account/login/start") == []
        assert harness.opened == [], "nobody is signed in"

    async def empty_prompt():
        harness = await connected(tmp_path / "d")
        assert await side.ask(harness.engine, str(tmp_path), "  \n ") is None
        assert harness.requests("account/read") == [] and harness.requests("thread/start") == []

    for scenario in (not_running, switched_off, signed_out, empty_prompt):
        run(scenario())


def test_an_error_answer_gives_none_and_what_was_made_is_let_go(tmp_path):
    async def failed_start():
        harness = await connected(tmp_path / "a", FAKE_SIDE_FAIL="thread/start")
        assert await side.ask(harness.engine, str(tmp_path), PROMPT) is None
        assert made(harness) == [] and harness.requests("turn/start") == [] and let_go(harness) == []

    async def failed_turn_start():
        harness = await connected(tmp_path / "b", FAKE_SIDE_FAIL="turn/start")
        assert await side.ask(harness.engine, str(tmp_path), PROMPT) is None
        ((thread),) = made(harness)
        assert let_go(harness) == [thread["id"]] and thread["id"] not in harness.engine._threads

    async def failed_models():
        harness = await connected(tmp_path / "c", FAKE_SIDE_FAIL="model/list")
        assert await side.ask(harness.engine, str(tmp_path), PROMPT) is None
        assert harness.requests("thread/start") == []

    for scenario in (failed_start, failed_turn_start, failed_models):
        run(scenario())


def test_a_failed_turn_gives_none_and_the_thread_is_let_go(tmp_path):
    async def scenario():
        harness = await connected(tmp_path)
        assert await side.ask(harness.engine, str(tmp_path), "[fail] " + PROMPT) is None
        ((thread),) = made(harness)
        assert let_go(harness) == [thread["id"]] and harness.requests("turn/interrupt") == []
        assert harness.engine._threads == {}

    run(scenario())


def test_a_turn_that_was_stopped_gives_none_even_with_words_said(tmp_path):
    async def scenario():
        harness = await connected(tmp_path)
        assert await side.ask(harness.engine, str(tmp_path), "[said-interrupted] " + PROMPT) is None
        assert len(let_go(harness)) == 1 and harness.requests("turn/interrupt") == []

    run(scenario())


def test_a_codex_that_would_keep_the_thread_is_not_asked_and_the_thread_is_deleted(tmp_path):
    async def scenario():
        harness = await connected(tmp_path, FAKE_SIDE_OLD="1")
        assert await side.ask(harness.engine, str(tmp_path), PROMPT) is None
        ((thread),) = made(harness)
        assert thread["ephemeral"] is False
        assert harness.requests("turn/start") == [], "no turn: nothing of the question would stay in the user's lists"
        assert harness.requests("thread/delete") == [{"threadId": thread["id"]}]
        assert let_go(harness) == [thread["id"]] and thread["id"] not in saved(harness)

    run(scenario())


# ---------------------------------------------------------------------------------------------- time and cancellation


def test_a_hung_question_ends_after_its_timeout_and_its_turn_is_stopped_and_let_go(tmp_path):
    async def scenario(marker):
        harness = await connected(tmp_path / marker.strip("[]"))
        began = time.monotonic()
        assert await side.ask(harness.engine, str(tmp_path), f"{marker} {PROMPT}", timeout=0.6) is None
        assert time.monotonic() - began < 5.0
        ((thread),) = made(harness)
        (turn,) = harness.requests("turn/start")
        (stop,) = harness.requests("turn/interrupt")
        assert stop["threadId"] == thread["id"] and stop["turnId"]
        assert let_go(harness) == [thread["id"]] and harness.engine._threads == {}

    run(scenario("[slow]"))
    run(scenario("[stuck]"))


def test_a_timeout_while_the_thread_is_being_made_still_lets_go_of_it(tmp_path):
    async def scenario():
        harness = await connected(tmp_path, FAKE_SIDE_SLOW="thread/start:1.0")
        began = time.monotonic()
        assert await side.ask(harness.engine, str(tmp_path), PROMPT, timeout=0.3) is None
        assert time.monotonic() - began < 4.0
        ((thread),) = made(harness)
        assert harness.requests("turn/start") == [] and let_go(harness) == [thread["id"]]

    run(scenario())


def test_a_timeout_while_the_turn_is_being_started_still_stops_it(tmp_path):
    async def scenario():
        harness = await connected(tmp_path, FAKE_SIDE_SLOW="turn/start:1.0")
        assert await side.ask(harness.engine, str(tmp_path), "[slow] " + PROMPT, timeout=0.5) is None
        ((thread),) = made(harness)
        (stop,) = harness.requests("turn/interrupt")
        assert stop["threadId"] == thread["id"] and stop["turnId"], "the turn id came with the late answer"
        assert let_go(harness) == [thread["id"]]

    run(scenario())


def test_a_cancelled_question_raises_and_is_cleaned_up_first(tmp_path):
    async def scenario():
        harness = await connected(tmp_path)
        task = asyncio.ensure_future(side.ask(harness.engine, str(tmp_path), "[slow] " + PROMPT))
        await until(lambda: harness.requests("turn/start"))
        await asyncio.sleep(0.2)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        ((thread),) = made(harness)
        assert [stop["threadId"] for stop in harness.requests("turn/interrupt")] == [thread["id"]]
        assert let_go(harness) == [thread["id"]] and harness.engine._threads == {}

    run(scenario())


def test_cancelled_again_while_it_cleans_up_the_clean_up_still_finishes(tmp_path):
    async def scenario():
        harness = await connected(tmp_path, FAKE_SIDE_SLOW="thread/unsubscribe:0.8")
        task = asyncio.ensure_future(side.ask(harness.engine, str(tmp_path), "[slow] " + PROMPT))
        await until(lambda: harness.requests("turn/start"))
        await asyncio.sleep(0.2)
        task.cancel()
        await until(lambda: harness.requests("thread/unsubscribe"))
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert harness.engine._threads, "still letting go"
        await until(lambda: not harness.engine._threads)
        ((thread),) = made(harness)
        assert let_go(harness) == [thread["id"]]

    run(scenario())


def test_the_process_ending_mid_question_gives_none_at_once(tmp_path):
    async def scenario():
        harness = await connected(tmp_path)
        began = time.monotonic()
        assert await side.ask(harness.engine, str(tmp_path), "[crash] " + PROMPT, timeout=30) is None
        assert time.monotonic() - began < 5.0, "not after the timeout"
        assert harness.engine._threads == {} and not harness.engine.running

    run(scenario())


# ---------------------------------------------------------------------------------------------- with other work


def test_two_questions_in_a_row_each_get_their_own_thread_and_let_it_go(tmp_path):
    async def scenario():
        harness = await connected(tmp_path)
        first = await side.ask(harness.engine, str(tmp_path), "one")
        second = await side.ask(harness.engine, str(tmp_path), "two")
        assert (first, second) == ("echo: one", "echo: two")
        threads = [thread["id"] for thread in made(harness)]
        assert len(set(threads)) == 2 and let_go(harness) == threads
        assert harness.engine._threads == {}

    run(scenario())


def test_a_question_while_a_conversation_runs_leaves_the_conversation_alone(tmp_path):
    async def scenario():
        harness = await connected(tmp_path)
        engine = harness.engine
        controller = await engine.open_new(tmp_path)
        assert controller.send_prompt("[slow] count")
        await until(lambda: controller._turn_id)
        answer = await side.ask(engine, str(tmp_path), PROMPT)
        assert answer == "echo: " + PROMPT, "no tick of the conversation's command in it"
        assert controller.state is SessionState.RUNNING
        turn = controller.transcript.turns[-1]
        assert turn.status is TurnStatus.RUNNING and "echo:" not in json.dumps([getattr(b, "text", "") for b in turn.blocks])
        assert let_go(harness) == [made(harness)[-1]["id"]] != [controller.session_id]
        assert list(engine._threads) == [controller.session_id]
        await controller.interrupt()
        assert controller.state is SessionState.READY and controller.transcript.turns[-1].status is TurnStatus.INTERRUPTED
        await controller.close()

    run(scenario())


# ---------------------------------------------------------------------------------------------- the helpers


class _Engine:
    def __init__(self):
        self.tasks = set()

    def _spawn(self, coroutine):
        task = asyncio.ensure_future(coroutine)
        self.tasks.add(task)
        return task


def test_settle_gives_what_a_request_answers_in_time_and_drops_it_otherwise():
    async def scenario():
        loop = asyncio.get_running_loop()
        assert await side.settle(None, 1) is None
        done = loop.create_future()
        done.set_result({"turn": {"id": "x"}})
        assert await side.settle(done, 0) == {"turn": {"id": "x"}}
        failed = loop.create_future()
        failed.set_exception(CodexRpcError(-1, "no"))
        assert await side.settle(failed, 1) is None
        late = loop.create_future()
        loop.call_later(0.05, late.set_result, "late")
        assert await side.settle(late, 1) == "late"
        never = loop.create_future()
        assert await side.settle(never, 0.05) is None and never.cancelled()
        cancelled = loop.create_future()
        cancelled.cancel()
        assert await side.settle(cancelled, 1) is None

    run(scenario())


def test_finish_runs_the_clean_up_through_and_raises_only_its_own_cancellation():
    async def scenario():
        engine, finished = _Engine(), []

        async def cleanup(seconds=0.0, fail=False):
            await asyncio.sleep(seconds)
            if fail:
                raise RuntimeError("boom")
            finished.append(seconds)

        await side.finish(engine, cleanup(fail=True))  # (logged, not raised)
        caller = asyncio.ensure_future(side.finish(engine, cleanup(0.2)))
        await asyncio.sleep(0.05)
        caller.cancel()
        with pytest.raises(asyncio.CancelledError):
            await caller
        await asyncio.gather(*engine.tasks, return_exceptions=True)
        assert finished == [0.2], "cancelled meanwhile, the clean-up went on"

        async def stopped_by_the_engine():
            task = asyncio.ensure_future(side.finish(engine, cleanup(5)))
            await asyncio.sleep(0.05)
            for inner in list(engine.tasks):
                inner.cancel()
            await task  # (the engine closing stops it: no cancellation of the caller's own)
            return "returned"

        assert await stopped_by_the_engine() == "returned"

    run(scenario())


def test_the_reason_in_the_log_says_what_happened():
    assert side.reason(TimeoutError(), 90) == "no answer within 90 s"
    assert side.reason(CodexRpcError(-32603, "bad"), 90) == "CodexRpcError: bad (-32603)"
    assert side.reason(ValueError(), 90) == "ValueError: "
