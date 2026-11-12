"""The memory curator's side question to Devin (``devin/side.ask``) against ``tests/fake_devin_side.py``: the answer comes
back from a free model in Devin's ``ask`` mode, in a session with no MCP server that no hook or listener hears of, and
the session is deleted whatever happens to the question (an error, a stopped turn, a timeout, a cancellation, the
process ending)."""

from __future__ import annotations

import asyncio
import json
import sys
import time
from pathlib import Path

import pytest

from desktop.core.devin import side
from desktop.core.devin.errors import DevinRpcError
from desktop.core.devin.locate import DevinBinary
from desktop.core.devin.models import DevinModel
from desktop.core.memory.launch import MemoryLaunch
from desktop.core.session_state import SessionState
from desktop.core.settings import AppSettings
from desktop.core.transcript import TurnStatus
from tests.devin_support import make_engine, run, until

SIDE_FAKE = Path(__file__).with_name("fake_devin_side.py")
PROMPT = 'Reply with only the JSON {"notes": []}'


def side_engine(tmp_path, **env):
    harness = make_engine(tmp_path, **env)
    binary = DevinBinary(path=SIDE_FAKE, version="3000.11.238", source="test", command=(sys.executable, str(SIDE_FAKE)))
    harness.engine._locate = lambda explicit: binary
    return harness


async def connected(tmp_path, *, models=True, **env):
    """An engine whose ``devin acp`` runs; ``models``: the model list has been read, as the application does at start."""
    harness = side_engine(tmp_path, **env)
    await harness.engine.connection()
    if models:
        await harness.engine.list_models()
    return harness


class Since:
    """The requests the fake got after this point (reading the model list makes and deletes a session of its own)."""

    def __init__(self, harness):
        self.harness = harness
        self.start = len(harness.received())

    def requests(self, method):
        return [m.get("params") or {} for m in self.harness.received()[self.start :] if m.get("method") == method]

    def options(self):
        return [(p["configId"], p["value"]) for p in self.requests("session/set_config_option")]


def sessions(harness):
    """The sessions the fake keeps (none at all before the first one is made)."""
    return set(harness.state()["sessions"]) if (harness.home / "state.json").is_file() else set()


def deleted(harness):
    return [params["sessionId"] for params in harness.requests("session/delete")]


def asked(since):
    (prompt,) = since.requests("session/prompt")
    return prompt["sessionId"]


async def listed(harness, cwd):
    client = await harness.engine.connection()
    result = await client.request("session/list", {"cwd": cwd}, timeout=10)
    return [entry["sessionId"] for entry in result["sessions"]]


# ---------------------------------------------------------------------------------------------- the question


def test_the_answer_comes_back_from_a_free_model_in_ask_mode_and_the_session_is_deleted(tmp_path):
    async def scenario():
        harness = await connected(tmp_path)
        engine = harness.engine
        project = tmp_path / "the-project"
        project.mkdir()
        since = Since(harness)
        assert await side.ask(engine, str(project), PROMPT) == "echo: " + PROMPT
        session = asked(since)
        assert since.requests("session/new") == [{"cwd": engine._scratch(), "mcpServers": []}]
        assert since.options() == [("model", "swe-2-flash-medium"), ("thought_level", "medium")], "the free cheap one, its lowest level"
        assert since.requests("session/set_mode") == [{"sessionId": session, "modeId": "ask"}]
        assert since.requests("session/prompt") == [{"sessionId": session, "prompt": [{"type": "text", "text": PROMPT}]}]
        assert since.requests("session/delete") == [{"sessionId": session}] and since.requests("session/cancel") == []
        assert str(project) not in json.dumps(harness.received())
        assert session not in sessions(harness) and session not in await listed(harness, engine._scratch())
        assert await engine.list_sessions(engine._scratch()) == []
        assert engine._sessions == {} and engine.controllers == [] and engine.running

    run(scenario())


def test_the_shared_memory_and_the_hooks_are_not_given_to_the_question(tmp_path):
    async def scenario():
        harness = await connected(tmp_path)
        engine = harness.engine
        engine.memory = MemoryLaunch(tmp_path / "memory", python=sys.executable)
        hooked, heard = [], []
        engine.hooks.on_prompt(lambda event: hooked.append(event))
        engine.add_listener(lambda controller, kind: heard.append(kind))
        since = Since(harness)
        assert await side.ask(engine, str(tmp_path), PROMPT) == "echo: " + PROMPT
        assert since.requests("session/new")[0]["mcpServers"] == []
        assert hooked == [] and heard == []
        controller = await engine.open_new(tmp_path)  # (a conversation does get both: the question would show if it did)
        assert since.requests("session/new")[-1]["mcpServers"] != []
        await controller.close()

    run(scenario())


def test_a_permission_devin_asks_for_is_refused_and_the_answer_is_what_follows_the_last_tool(tmp_path):
    async def scenario():
        harness = await connected(tmp_path)
        assert await side.ask(harness.engine, str(tmp_path), "[approve] go") == "rejected: reject_once"
        assert [answer.get("result") for answer in harness.answers()] == [{"outcome": {"outcome": "selected", "optionId": "reject_once"}}]
        assert harness.state()["sessions"] == {}

    run(scenario())


def test_the_side_question_answers_only_permission_requests(tmp_path):
    async def scenario():
        question = side._Question(None, None)
        refused = await question._on_server_request("session/request_permission", {"options": [{"optionId": "allow_once", "kind": "allow_once"}]}, 1)
        assert refused == {"outcome": {"outcome": "cancelled"}}, "no option to say no with: cancelled"
        with pytest.raises(DevinRpcError) as raised:
            await question._on_server_request("fs/read_text_file", {}, 2)
        assert raised.value.code == -32601

    run(scenario())


def test_the_answer_is_what_was_said_after_the_last_tool_call():
    question = side._Question(None, None)

    def update(kind, text=None, **more):
        content = {"type": "text", "text": text} if text is not None else None
        question._on_notification("session/update", {"sessionId": "s", "update": {"sessionUpdate": kind, "content": content, **more}})

    update("agent_thought_chunk", "thinking")
    assert question.answer() == ""
    update("agent_message_chunk", "I will ")
    update("agent_message_chunk", "look. ")
    update("tool_call", toolCallId="t1")
    update("tool_call_update", toolCallId="t1")
    update("agent_thought_chunk", "more thinking")
    update("agent_message_chunk", '{"notes": ')
    update("usage_update")
    update("agent_message_chunk", "[]}")
    assert question.answer() == '{"notes": []}'
    update("tool_call_update", toolCallId="t1")
    update("agent_message_chunk", None)
    assert question.answer() == '{"notes": []}', "a late tool update after the words does not wipe them"
    question._on_notification("_cognition.ai/turn_stats", {"update": {"sessionUpdate": "agent_message_chunk", "content": {"text": "x"}}})
    question._on_notification("session/update", {"update": "?"})
    assert question.answer() == '{"notes": []}'


# ---------------------------------------------------------------------------------------------- the model


class _Engine:
    def __init__(self, *free):
        self.free = free

    def model_info(self, model_id):
        return DevinModel(id=model_id, note="Free" if model_id in self.free else "") if model_id.startswith(("swe", "glm", "gpt")) else None


def test_a_free_model_is_chosen_else_the_swe_2_family_else_the_default():
    offered = ["adaptive", "claude-opus-5-5-medium", "swe-2-high", "glm-5-2", "swe-2-flash-medium", "gpt-6-luna-medium"]
    assert side.side_model(offered, _Engine("swe-2-high", "swe-2-flash-medium")) == "swe-2-flash-medium", "free, and the cheaper kind"
    assert side.side_model(offered, _Engine("swe-2-high")) == "swe-2-high"
    assert side.side_model(offered, _Engine("glm-5-2")) == "glm-5-2", "free comes before the family"
    assert side.side_model(offered, _Engine("gpt-6-luna-medium", "glm-5-2")) == "gpt-6-luna-medium", "the cheapest kind of the free ones"
    assert side.side_model(offered, _Engine()) == "swe-2-flash-medium", "nothing known to be free: the SWE-2 family"
    assert side.side_model(["swe-2-high", "swe-1-7-medium"], _Engine()) == "swe-2-high"
    assert side.side_model(["adaptive", "claude-opus-5-5-medium", "swe-1-6"], _Engine()) is None, "the session's default stays"
    assert side.side_model([], _Engine()) is None


def test_with_the_model_list_a_free_model_of_another_family_is_chosen(tmp_path):
    async def scenario(models, expected):
        harness = await connected(tmp_path / str(models), models=models, FAKE_SIDE_FREE="glm-5-2")
        since = Since(harness)
        assert await side.ask(harness.engine, str(tmp_path), PROMPT) == "echo: " + PROMPT
        assert since.options() == expected

    run(scenario(True, [("model", "glm-5-2"), ("thought_level", "none")]))
    run(scenario(False, [("model", "swe-2-flash-medium"), ("thought_level", "medium")]))


def test_without_a_free_or_swe_2_model_the_default_runs_at_its_lowest_level(tmp_path):
    async def scenario():
        harness = await connected(tmp_path, FAKE_SIDE_FREE="", FAKE_SIDE_DROP="swe-2-high,swe-2-flash-medium")
        since = Since(harness)
        assert await side.ask(harness.engine, str(tmp_path), PROMPT) == "echo: " + PROMPT
        assert since.options() == [("thought_level", "low")], "claude-opus-5-5-medium stays; low is its lowest"

    run(scenario())


def test_a_model_devin_will_not_take_leaves_the_default_to_answer(tmp_path):
    async def scenario():
        harness = await connected(tmp_path, FAKE_SIDE_FAIL="session/set_config_option")
        since = Since(harness)
        assert await side.ask(harness.engine, str(tmp_path), PROMPT) == "echo: " + PROMPT
        assert since.options() == [("model", "swe-2-flash-medium"), ("thought_level", "low")], "the default's own lowest level, also refused"
        assert len(since.requests("session/set_mode")) == 1 and [p["sessionId"] for p in since.requests("session/delete")] == [asked(since)]

    run(scenario())


# ---------------------------------------------------------------------------------------------- when it cannot be asked


def test_nothing_is_asked_when_devin_is_not_running_switched_off_signed_out_or_the_prompt_is_empty(tmp_path):
    async def not_running():
        harness = side_engine(tmp_path / "a")
        assert await side.ask(harness.engine, str(tmp_path), PROMPT) is None
        assert harness.clients == [] and not harness.engine.running, "a side question never starts Devin"

    async def switched_off():
        harness = await connected(tmp_path / "b", models=False)
        harness.engine.settings = AppSettings(devin_enabled=False)
        assert await side.ask(harness.engine, str(tmp_path), PROMPT) is None
        assert harness.requests("session/new") == []

    async def signed_out_known():
        harness = await connected(tmp_path / "c", models=False, account="signed-out")
        engine = harness.engine
        await engine.account()
        ran, cli = [], engine._cli

        async def counted(*arguments, **options):
            ran.append(arguments)
            return await cli(*arguments, **options)

        engine._cli = counted
        assert await side.ask(engine, str(tmp_path), PROMPT) is None
        assert ran == [] and harness.requests("session/new") == [], "known to be signed out: not even asked again"

    async def signed_out_unknown():
        harness = await connected(tmp_path / "d", models=False, account="signed-out")
        assert harness.engine.cached_account()["auth"] == "checking"
        assert await side.ask(harness.engine, str(tmp_path), PROMPT) is None
        assert harness.requests("session/new") == [] and harness.engine.cached_account()["auth"] == "signed-out"
        assert not (harness.home / "state.json").is_file(), "nobody is signed in (a sign-in would be written there)"

    async def empty_prompt():
        harness = await connected(tmp_path / "e", models=False)
        assert await side.ask(harness.engine, str(tmp_path), " \n ") is None
        assert harness.requests("session/new") == []

    for scenario in (not_running, switched_off, signed_out_known, signed_out_unknown, empty_prompt):
        run(scenario())


def test_an_error_answer_gives_none_and_what_was_made_is_deleted(tmp_path):
    async def failed_new():
        harness = await connected(tmp_path / "a", models=False, FAKE_SIDE_FAIL="session/new")
        assert await side.ask(harness.engine, str(tmp_path), PROMPT) is None
        assert harness.requests("session/prompt") == [] and deleted(harness) == [] and sessions(harness) == set()

    async def failed_mode():
        harness = await connected(tmp_path / "b", models=False, FAKE_SIDE_FAIL="session/set_mode")
        assert await side.ask(harness.engine, str(tmp_path), PROMPT) is None
        assert harness.requests("session/prompt") == [], "not in the read-only mode: not asked"
        assert len(deleted(harness)) == 1 and sessions(harness) == set()

    async def failed_prompt():
        harness = await connected(tmp_path / "c", models=False)
        since = Since(harness)
        assert await side.ask(harness.engine, str(tmp_path), "[fail] " + PROMPT) is None
        assert deleted(harness) == [asked(since)] and sessions(harness) == set() and harness.engine._sessions == {}

    for scenario in (failed_new, failed_mode, failed_prompt):
        run(scenario())


def test_a_turn_that_was_stopped_gives_none(tmp_path):
    async def scenario():
        harness = await connected(tmp_path, models=False)
        since = Since(harness)
        task = asyncio.ensure_future(side.ask(harness.engine, str(tmp_path), "[slow] " + PROMPT))
        await until(lambda: since.requests("session/prompt"))
        client = await harness.engine.connection()
        await client.notify("session/cancel", {"sessionId": asked(since)})
        assert await task is None
        assert len(since.requests("session/cancel")) == 1, "the turn was over: the clean-up does not stop it again"
        assert deleted(harness) == [asked(since)]

    run(scenario())


# ---------------------------------------------------------------------------------------------- time and cancellation


def test_a_hung_question_ends_after_its_timeout_and_its_session_is_stopped_and_deleted(tmp_path, monkeypatch):
    monkeypatch.setattr(side, "CANCEL_SECONDS", 0.3)

    async def scenario(marker):
        harness = await connected(tmp_path / marker.strip("[]"), models=False)
        since = Since(harness)
        began = time.monotonic()
        assert await side.ask(harness.engine, str(tmp_path), f"{marker} {PROMPT}", timeout=0.6) is None
        assert time.monotonic() - began < 4.0
        session = asked(since)
        assert since.requests("session/cancel") == [{"sessionId": session}]
        assert deleted(harness) == [session] and sessions(harness) == set() and harness.engine._sessions == {}
        assert harness.engine._client._pending == {}, "a prompt that would not end is dropped, not left waiting"

    run(scenario("[slow]"))
    run(scenario("[stuck]"))


def test_a_timeout_while_the_session_is_being_made_still_deletes_it(tmp_path):
    async def scenario():
        harness = await connected(tmp_path, models=False, FAKE_SIDE_SLOW="session/new:1.0")
        began = time.monotonic()
        assert await side.ask(harness.engine, str(tmp_path), PROMPT, timeout=0.3) is None
        assert time.monotonic() - began < 4.0
        assert harness.requests("session/prompt") == [] and len(deleted(harness)) == 1 and sessions(harness) == set()

    run(scenario())


def test_a_cancelled_question_raises_and_is_cleaned_up_first(tmp_path):
    async def scenario():
        harness = await connected(tmp_path, models=False)
        since = Since(harness)
        task = asyncio.ensure_future(side.ask(harness.engine, str(tmp_path), "[slow] " + PROMPT))
        await until(lambda: since.requests("session/prompt"))
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        session = asked(since)
        assert since.requests("session/cancel") == [{"sessionId": session}] and deleted(harness) == [session]
        assert sessions(harness) == set() and harness.engine._sessions == {}

    run(scenario())


def test_the_process_ending_mid_question_gives_none_at_once(tmp_path):
    async def scenario():
        harness = await connected(tmp_path, models=False)
        began = time.monotonic()
        assert await side.ask(harness.engine, str(tmp_path), "[crash] " + PROMPT, timeout=30) is None
        assert time.monotonic() - began < 5.0, "not after the timeout"
        assert harness.engine._sessions == {}

    run(scenario())


# ---------------------------------------------------------------------------------------------- with other work


def test_two_questions_in_a_row_each_get_their_own_session_and_delete_it(tmp_path):
    async def scenario():
        harness = await connected(tmp_path)
        since = Since(harness)
        assert await side.ask(harness.engine, str(tmp_path), "one") == "echo: one"
        assert await side.ask(harness.engine, str(tmp_path), "two") == "echo: two"
        made = [params["sessionId"] for params in since.requests("session/prompt")]
        assert len(set(made)) == 2 and [p["sessionId"] for p in since.requests("session/delete")] == made
        assert sessions(harness) == set() and harness.engine._sessions == {}

    run(scenario())


def test_a_question_while_a_conversation_runs_leaves_the_conversation_alone(tmp_path):
    async def scenario():
        harness = await connected(tmp_path)
        engine = harness.engine
        controller = await engine.open_new(tmp_path)
        assert controller.send_prompt("[slow] count")
        await until(lambda: harness.requests("session/prompt"))
        since = Since(harness)
        assert await side.ask(engine, str(tmp_path), PROMPT) == "echo: " + PROMPT, "nothing of the conversation's turn in it"
        assert controller.state is SessionState.RUNNING
        turn = controller.transcript.turns[-1]
        assert turn.status is TurnStatus.RUNNING and "echo:" not in json.dumps([getattr(b, "text", "") for b in turn.blocks])
        assert [p["sessionId"] for p in since.requests("session/delete")] == [asked(since)] != [controller.session_id]
        assert list(engine._sessions) == [controller.session_id] and sessions(harness) == {controller.session_id}
        await controller.interrupt()
        assert controller.state is SessionState.READY and controller.transcript.turns[-1].status is TurnStatus.INTERRUPTED
        await controller.close()

    run(scenario())
