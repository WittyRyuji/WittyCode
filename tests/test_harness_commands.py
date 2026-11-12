"""What WittyCode's own loop does for the commands typed with a slash: ``/compact`` (the whole conversation becomes a summary now),
the rules of ``/goal`` (the driver is the application's, for every engine: ``test_app_commands``) and Plan mode (``/plan``: look,
do not touch)."""

from __future__ import annotations

import json
from typing import Any

import pytest

from desktop.core.chatgpt.errors import ChatGPTError
from desktop.core.chatgpt.responses import user_message
from desktop.core.harness import compact, goal as goals
from desktop.core.harness.loop import ALLOW, ASK, DENY, Call
from desktop.core.harness.mcp import McpResult, McpTool
from desktop.core.harness.prompt import PLAN_RULES, build_instructions
from desktop.core.harness.store import SavedSession, SavedTurn, SessionStore
from desktop.core.harness.tools import ToolContext, ToolRegistry
from desktop.core.harness.toolset import PLAN_REFUSAL, HarnessTools
from desktop.core.session_state import SessionState
from desktop.core.transcript import TurnStatus
from tests.chatgpt_support import ACCESS_TOKEN, finish_turn, until
from tests.fake_responses import FakeResponses, call, cut, http_error, say, usage
from tests.test_chatgpt_compaction import SOL, assistant, fill, notices
from tests.test_chatgpt_controller import opened, run, texts, tools_of
from tests.test_harness_compact import Script, long_view, u
from tests.test_harness_compact import run as run_coroutine

HARMLESS = {"readOnlyHint": False, "destructiveHint": False, "openWorldHint": False}


# ----------------------------------------------------------------------------------------------- the goal's rules (no model)


def test_only_the_last_line_can_end_a_goal():
    assert goals.mark_of("Done.\nGOAL: DONE") == ("done", "")
    assert goals.mark_of("Done.\n\n  goal: done  \n\n") == ("done", ""), "case, spaces and blank lines do not matter"
    assert goals.mark_of("**GOAL: DONE**") == ("done", "")
    assert goals.mark_of("`GOAL: DONE`") == ("done", "")
    assert goals.mark_of("> GOAL: DONE.") == ("done", "")
    assert goals.mark_of("I need a key.\nGOAL: BLOCKED I need an API key") == ("blocked", "I need an API key")
    assert goals.mark_of("GOAL: BLOCKED: no network.") == ("blocked", "no network")
    assert goals.mark_of("When it is met I write GOAL: DONE.\nStill working.") == ("", ""), "a mention is not the mark"
    assert goals.mark_of("GOAL: DONE\nbut one more thing") == ("", ""), "the mark ends the message"
    assert goals.mark_of("GOAL: DONEISH") == ("", "")
    assert goals.mark_of("") == ("", "") and goals.mark_of("   \n ") == ("", "")


def test_the_messages_of_a_goal_put_the_goal_to_the_model_and_ask_for_the_marks_the_goal_reads():
    first = goals.opening("  make the tests pass  ")
    assert first.startswith("make the tests pass\n\n") and goals.GOAL_DONE in first and goals.GOAL_BLOCKED in first
    later = goals.Goal("make the tests pass", rounds=2).next_message()
    assert "第 3/20 轮" in later and "make the tests pass" in later, "the goal is in front of the model again, to be checked against the result"
    assert goals.GOAL_DONE in later and goals.GOAL_BLOCKED in later and "对照目标检查" in later
    for line in (goals.GOAL_DONE, goals.GOAL_BLOCKED):
        assert goals.mark_of(line)[0] in ("done", "blocked")


def test_a_round_that_ends_the_goal_or_carries_it_on():
    goal = goals.Goal("fix it")
    carried = goals.step(goal, "Fixed two things, more to go.", True)
    assert carried.prompt == goal.next_message() and "第 2/20 轮" in carried.prompt and carried.notice == "" and goal.rounds == 1
    done = goals.step(goal, "All green.\nGOAL: DONE", False)
    assert done.prompt == "" and "完成" in done.notice and "2 轮" in done.notice
    blocked = goals.step(goals.Goal("x"), "Cannot go on.\nGOAL: BLOCKED the repo is read-only", True)
    assert blocked.prompt == "" and "需要你" in blocked.notice and "the repo is read-only" in blocked.notice
    assert "见上一条回复" in goals.step(goals.Goal("x"), "GOAL: BLOCKED", True).notice


def test_two_rounds_in_a_row_without_a_tool_call_end_the_goal_one_does_not():
    goal = goals.Goal("x")
    assert goals.step(goal, "I think...", False).prompt, "one talking round is allowed"
    assert goals.step(goal, "Working", True).prompt and goal.idle == 0, "a round that touched a tool starts the count again"
    assert goals.step(goal, "hmm", False).prompt
    ended = goals.step(goal, "hmm again", False)
    assert ended.prompt == "" and f"{goals.IDLE} 轮" in ended.notice


def test_the_goal_ends_after_its_round_limit(monkeypatch):
    monkeypatch.setattr(goals, "ROUNDS", 3)
    goal = goals.Goal("x")
    assert goals.step(goal, "a", True).prompt and goals.step(goal, "b", True).prompt
    ended = goals.step(goal, "c", True)
    assert ended.prompt == "" and "3 轮" in ended.notice and goal.rounds == 3
    assert "第 2/3 轮" in goals.Goal("x", rounds=1).next_message()


def test_the_words_that_cancel_a_goal():
    assert {"off", "stop", "clear", "cancel", "关闭", "停止", "取消", "清除"} == set(goals.STOP_WORDS)


# ----------------------------------------------------------------------------------------------- the instructions


def test_the_plan_is_in_the_instructions_only_when_it_is_on(tmp_path):
    plain = build_instructions(tmp_path, model="m", level="code")
    assert "Plan mode" not in plain and "Goal mode" not in plain
    assert "Reading, searching, creating and editing files inside the project run directly" in plain
    planned = build_instructions(tmp_path, model="m", level="full", plan=True)
    assert PLAN_RULES in planned
    assert "Only reading and searching run (Plan mode)." in planned, "the permission level's line gives way to the mode"
    assert "Everything runs without asking" not in planned


# ----------------------------------------------------------------------------------------------- Plan mode and the tools


def tool(server: str, name: str, **annotations: Any) -> McpTool:
    return McpTool(server, name, name, {"type": "object", "properties": {}}, dict(annotations))


class Stub:
    def __init__(self, tools: list[McpTool]) -> None:
        self.name, self.tools, self.running, self.instructions = "s", tools, True, ""

    async def call(self, tool: str, arguments: dict[str, Any]) -> McpResult:
        return McpResult("ok", False)


def planned(tmp_path, level: str = "code", clients: list[Any] | None = None, **context: Any) -> HarnessTools:
    tools = HarnessTools(ToolRegistry.default(), ToolContext(project=tmp_path, **context), level, clients or [])
    tools.plan = True
    return tools


def use(name: str, **args: Any) -> Call:
    return Call("item-1", "call-1", name, "{}", args)


def test_in_plan_mode_reading_and_read_only_commands_run_and_everything_else_is_refused(tmp_path):
    (tmp_path / "a.txt").write_text("x", encoding="utf-8")
    for level in ("ask", "code", "full"):
        tools = planned(tmp_path, level)
        assert tools.check(use("read_file", file_path=str(tmp_path / "a.txt"))).verdict == ALLOW, level
        assert tools.check(use("list_dir", directory_path=str(tmp_path))).verdict == ALLOW, level
        assert tools.check(use("grep", pattern="x", path=str(tmp_path))).verdict == ALLOW, level
        assert tools.check(use("todo_write", todos=[])).verdict == ALLOW, level
        assert tools.check(use("shell", command="git status")).verdict == ALLOW, level
        for refused in (
            use("create_file", file_path=str(tmp_path / "b.txt"), content="y"),
            use("edit_file", file_path=str(tmp_path / "a.txt"), old_str="x", new_str="z"),
            use("shell", command="dotnet build"),
            use("shell", command="Remove-Item -Recurse ."),
        ):
            verdict = tools.check(refused)
            assert verdict.verdict == DENY and verdict.reason == PLAN_REFUSAL, (level, refused.name)
    off = planned(tmp_path, "code")
    off.plan = False
    assert off.check(use("create_file", file_path=str(tmp_path / "b.txt"), content="y")).verdict == ALLOW, "Auto again: the level decides"


def test_plan_mode_does_not_honour_what_the_user_allowed_for_the_session(tmp_path):
    tools = planned(tmp_path, "ask")
    write = use("create_file", file_path=str(tmp_path / "b.txt"), content="y")
    install = use("shell", command="npm install")
    tools.remember(write)
    tools.remember(install)
    assert tools.check(write).verdict == DENY and tools.check(install).verdict == DENY
    tools.plan = False
    assert tools.check(write).verdict == ALLOW and tools.check(install).verdict == ALLOW


def test_plan_mode_still_asks_before_reading_outside_the_project_and_still_denies_the_protected(tmp_path):
    project, outside, secret = tmp_path / "project", tmp_path / "outside", tmp_path / "secret"
    for folder in (project, outside, secret):
        folder.mkdir()
        (folder / "f.txt").write_text("x", encoding="utf-8")
    tools = planned(project, "full", protected=(secret,))
    assert tools.check(use("read_file", file_path=str(outside / "f.txt"))).verdict == ASK
    denied = tools.check(use("read_file", file_path=str(secret / "f.txt")))
    assert denied.verdict == DENY and denied.reason != PLAN_REFUSAL
    assert tools.check(use("shell", command="git status", cwd=str(outside))).verdict == DENY, "a command outside the project would ask: refused"


def test_in_plan_mode_an_mcp_tool_runs_only_when_its_server_calls_it_harmless(tmp_path):
    for level in ("ask", "full"):
        tools = planned(tmp_path, level, [Stub([tool("s", "quiet", **HARMLESS), tool("s", "risky", destructiveHint=True)])])
        assert tools.check(use("mcp__s__quiet")).verdict == ALLOW, level
        verdict = tools.check(use("mcp__s__risky"))
        assert verdict.verdict == DENY and verdict.reason == PLAN_REFUSAL, level


def test_asking_the_user_is_allowed_in_plan_mode(tmp_path):
    tools = HarnessTools(ToolRegistry.default(), ToolContext(project=tmp_path), "ask", [], asker=lambda *args: None)  # type: ignore[arg-type]
    tools.plan = True
    assert tools.check(use("ask_user")).verdict == ALLOW


# ----------------------------------------------------------------------------------------------- a compaction the user asked for


def big_view(turns: int = 6) -> list[dict[str, Any]]:
    return long_view(turns)


def test_the_whole_conversation_becomes_the_summary_when_asked_for():
    view = big_view()
    script = Script(say("THE SUMMARY"))
    result = run_coroutine(compact.compact(view, stream=script, model="m", reasoning=None, window=400_000, whole=True))
    assert result is not None and result.cut == len(view) and result.fits
    assert result.view == [compact.summary_item("THE SUMMARY")] and result.kept == "", "nothing is kept: no turn is running"
    body = script.requests[0]["input"][0]["content"][0]["text"]
    assert "question 0" in body and f"question {len(view) // 2 - 1}" in body, "the summary reads all of it"
    ordinary = run_coroutine(compact.compact(view, stream=Script(say("S")), model="m", reasoning=None, window=400_000))
    assert ordinary is not None and ordinary.cut < len(view) and len(ordinary.view) > 1, "without the flag the newest part stays word for word"


def test_what_the_user_said_before_goes_into_the_new_summary():
    view = [compact.summary_item("OLD SUMMARY", "the user's earlier words"), *big_view(3)]
    script = Script(say("NEW SUMMARY"))
    result = run_coroutine(
        compact.compact(
            view, stream=script, model="m", reasoning=None, window=400_000, synthetic=1, previous="OLD SUMMARY", kept="the user's earlier words", whole=True, turn=len(view)
        )
    )
    assert result is not None and result.cut == len(view)
    body = script.requests[0]["input"][0]["content"][0]["text"]
    assert "OLD SUMMARY" in body and "the user's earlier words" in body
    assert result.view == [compact.summary_item("NEW SUMMARY")]


def test_a_conversation_that_ends_with_a_message_to_answer_is_cut_as_usual_not_whole():
    view = [*big_view(2), u("an unanswered question")]
    result = run_coroutine(compact.compact(view, stream=Script(say("S")), model="m", reasoning=None, window=4000, whole=True))
    assert result is not None and result.cut < len(view), "the question stays word for word"
    assert "an unanswered question" in str(result.view[1:] or result.kept)


def test_nothing_is_left_to_summarise_in_a_conversation_that_is_a_summary_already():
    script = Script()
    assert run_coroutine(compact.compact([compact.summary_item("S")], stream=script, model="m", reasoning=None, window=4000, synthetic=1, whole=True)) is None
    assert script.requests == []


def test_a_summary_that_cannot_be_written_raises_when_the_user_asked_for_it():
    with pytest.raises(compact.CompactionFailed):
        run_coroutine(compact.compact(big_view(), stream=Script(say("   ")), model="m", reasoning=None, window=4000, whole=True, fallback=False))
    with pytest.raises(ChatGPTError):
        run_coroutine(compact.compact(big_view(), stream=_failing, model="m", reasoning=None, window=4000, whole=True, fallback=False))
    kept = run_coroutine(compact.compact(big_view(), stream=Script(say("   ")), model="m", reasoning=None, window=4000, whole=True))
    assert kept is not None and kept.mechanical, "without the flag the digest still stands in"


async def _failing(request: dict[str, Any]):
    raise ChatGPTError("the server said no")
    yield  # pragma: no cover


def test_the_command_summarises_the_conversation_and_adds_nothing_to_it(tmp_path):
    async def go() -> None:
        with FakeResponses(token=ACCESS_TOKEN) as fake:
            fake.script(say("SUMMARY OF EVERYTHING", used=usage(900, 40)), say("the next answer"))
            engine, controller = await opened(tmp_path, fake)
            fill(controller)
            engine._windows[SOL] = 400_000  # (it all fits: the user asked, nothing was too big)
            logged, recorded = len(controller.saved.items), len(controller.saved.turns)
            assert controller.start_compaction() is None
            assert controller.state is SessionState.RUNNING
            running = controller.transcript.turns[-1]
            assert running.user_block is not None and running.user_block.text == "/compact"
            assert controller.transcript.working_state.value == "compacting_conversation"
            await finish_turn(controller)

            assert len(fake.requests) == 1 and fake.requests[0]["instructions"] == compact.INSTRUCTIONS
            record = controller.saved.compaction
            assert record["upto"] == logged and record["mechanical"] is False and "SUMMARY OF EVERYTHING" in record["summary"]
            assert len(controller.saved.items) == logged and len(controller.saved.turns) == recorded, "the log gets nothing"
            assert compact.view_of(controller.saved.items, record) == [compact.summary_item("SUMMARY OF EVERYTHING")]
            assert running.status is TurnStatus.DONE and controller.state is SessionState.READY
            assert any("已按你的要求压缩" in text for text in notices(controller))
            assert controller.context.compacted is True and controller.usage.input_tokens == 900
            assert engine.sessions.load(controller.session_id).compaction == record, "it is on disk"

            controller.send_prompt("what next?")
            await finish_turn(controller)
            sent = fake.requests[1]["input"]
            assert len(sent) == 2 and compact.is_summary(sent[0]) and "what next?" in str(sent[1])
            await engine.close_all()

    run(go())


def test_the_command_says_why_it_cannot_start(tmp_path):
    async def go() -> None:
        with FakeResponses(token=ACCESS_TOKEN) as fake:
            fake.script([("hold", "answer"), *say("late")])
            engine, controller = await opened(tmp_path, fake)
            assert "还没有对话" in (controller.start_compaction() or "")
            controller.send_prompt("a question")
            await until(lambda: len(fake.requests) == 1, message="the request")
            assert "还在运行" in (controller.start_compaction() or "")
            fake.release("answer")
            await finish_turn(controller)
            assert len(fake.requests) == 1
            await engine.close_all()

    run(go())


def test_a_conversation_too_short_to_get_smaller_is_left_alone(tmp_path):
    async def go() -> None:
        with FakeResponses(token=ACCESS_TOKEN) as fake:
            fake.script(say("A SUMMARY THAT IS LONGER THAN THE TALK"))
            engine, controller = await opened(tmp_path, fake)
            controller.saved.items += [user_message("hi"), assistant("hello")]
            controller.saved.turns.append(SavedTurn(0, 1.0, 2.0, "done", "", SOL))
            assert controller.start_compaction() is None
            await finish_turn(controller)
            assert controller.saved.compaction == {} and len(controller.saved.items) == 2
            assert any("没有压缩" in text and "太短" in text for text in notices(controller))
            assert controller.transcript.turns[-1].status is TurnStatus.DONE
            await engine.close_all()

    run(go())


def test_asking_again_when_the_conversation_is_a_summary_already_changes_nothing(tmp_path):
    async def go() -> None:
        with FakeResponses(token=ACCESS_TOKEN) as fake:
            fake.script(say("SUMMARY"))
            engine, controller = await opened(tmp_path, fake)
            fill(controller)
            assert controller.start_compaction() is None
            await finish_turn(controller)
            record = dict(controller.saved.compaction)
            assert controller.start_compaction() is None
            await finish_turn(controller)
            assert len(fake.requests) == 1, "nothing to summarise: the model is not asked"
            assert controller.saved.compaction == record
            assert any("已经是一段摘要" in text for text in notices(controller))
            await engine.close_all()

    run(go())


def test_a_summary_that_fails_leaves_the_conversation_as_it_was_and_says_so(tmp_path):
    async def go() -> None:
        with FakeResponses(token=ACCESS_TOKEN) as fake:
            fake.script(http_error(400, {"error": {"code": "invalid_request_error", "message": "refused"}}), say("fine now"))
            engine, controller = await opened(tmp_path, fake)
            fill(controller)
            logged = len(controller.saved.items)
            assert controller.start_compaction() is None
            await finish_turn(controller)
            assert controller.saved.compaction == {}, "no digest in place of the summary the user asked for"
            assert len(controller.saved.items) == logged
            failed = controller.transcript.turns[-1]
            assert failed.status is TurnStatus.FAILED
            assert any("没有压缩，对话没有改动" in getattr(block, "message", "") for block in failed.blocks)
            controller.send_prompt("go on")
            await finish_turn(controller)
            assert controller.transcript.turns[-1].status is TurnStatus.DONE
            await engine.close_all()

    run(go())


def test_a_stop_while_the_summary_is_written_changes_nothing(tmp_path):
    async def go() -> None:
        with FakeResponses(token=ACCESS_TOKEN) as fake:
            fake.script([("hold", "summary"), *say("SUMMARY")])
            engine, controller = await opened(tmp_path, fake)
            fill(controller)
            logged = len(controller.saved.items)
            assert controller.start_compaction() is None
            await until(lambda: len(fake.requests) == 1, message="the summary request")
            await controller.interrupt()
            assert controller.state is SessionState.READY and controller.transcript.turns[-1].status is TurnStatus.INTERRUPTED
            assert controller.saved.compaction == {} and len(controller.saved.items) == logged
            fake.release("summary")
            await engine.close_all()

    run(go())


# ----------------------------------------------------------------------------------------------- Plan mode in a conversation


def test_in_plan_mode_the_model_may_look_but_not_touch_and_auto_lets_it_through_again(tmp_path):
    async def go() -> None:
        target = tmp_path / "project" / "new.txt"
        with FakeResponses(token=ACCESS_TOKEN) as fake:
            fake.script(
                call("create_file", {"file_path": str(target), "content": "hello"}, call_id="c1"),
                say("Here is my plan: create new.txt."),
                call("create_file", {"file_path": str(target), "content": "hello"}, call_id="c2"),
                say("Created."),
            )
            engine, controller = await opened(tmp_path, fake)
            assert await controller.update_settings(mode="spec") is True
            assert controller.mode == "spec" and controller.live_settings().mode == "spec"
            controller.send_prompt("add a file")
            await finish_turn(controller)
            assert not target.exists(), "nothing was written in Plan mode"
            first, second = fake.requests[0], fake.requests[1]
            assert "Plan mode is on" in first["instructions"]
            refusal = [item for item in second["input"] if item.get("type") == "function_call_output"]
            assert refusal and "Plan mode is on" in refusal[0]["output"]
            assert controller.saved.mode == "spec"

            assert await controller.update_settings(mode="auto") is True
            controller.send_prompt("go ahead")
            await finish_turn(controller)
            assert target.read_text(encoding="utf-8") == "hello"
            assert "Plan mode" not in fake.requests[2]["instructions"]
            assert controller.saved.mode == ""
            await engine.close_all()

    run(go())


def test_a_mode_chosen_while_a_turn_runs_is_the_next_turns_and_the_page_is_told(tmp_path):
    async def go() -> None:
        with FakeResponses(token=ACCESS_TOKEN) as fake:
            fake.script([("hold", "answer"), *say("one")], say("two"))
            engine, controller = await opened(tmp_path, fake)
            controller.send_prompt("first")
            await until(lambda: len(fake.requests) == 1, message="the request")
            await controller.update_settings(mode="spec")
            assert any("模式改为「Plan」" in text and "这一轮仍是「Auto」" in text for text in notices(controller))
            fake.release("answer")
            await finish_turn(controller)
            assert "Plan mode" not in fake.requests[0]["instructions"]
            controller.send_prompt("second")
            await finish_turn(controller)
            assert "Plan mode is on" in fake.requests[1]["instructions"]
            await engine.close_all()

    run(go())


def test_a_mode_that_does_not_exist_is_refused_and_the_old_one_stays(tmp_path):
    async def go() -> None:
        with FakeResponses(token=ACCESS_TOKEN) as fake:
            engine, controller = await opened(tmp_path, fake)
            assert await controller.update_settings(mode="yolo") is False
            assert controller.mode == "auto" and any("没有「yolo」这个模式" in text for text in notices(controller))
            await engine.close_all()

    run(go())


def test_a_reopened_conversation_is_still_in_plan_mode(tmp_path):
    async def go() -> None:
        with FakeResponses(token=ACCESS_TOKEN) as fake:
            fake.script(say("a plan"))
            engine, controller = await opened(tmp_path, fake)
            await controller.update_settings(mode="spec")
            controller.send_prompt("plan it")
            await finish_turn(controller)
            session_id = controller.session_id
            await controller.close()
            reopened = await engine.open_saved(controller.project, session_id)
            assert reopened.mode == "spec" and reopened.live_settings().mode == "spec"
            assert texts(reopened) == ["a plan"] and tools_of(reopened) == []
            await engine.close_all()

    run(go())


def test_the_mode_is_kept_in_the_file_and_a_file_without_it_reads_as_auto(tmp_path):
    store = SessionStore(tmp_path)
    store.save(SavedSession(id="a" * 32, project="p", mode="spec", items=[user_message("x")]))
    store.save(SavedSession(id="b" * 32, project="p", items=[user_message("x")]))
    assert store.load("a" * 32).mode == "spec" and store.load("b" * 32).mode == ""
    path = store._path("a" * 32)
    older = json.loads(path.read_text(encoding="utf-8"))
    del older["mode"]
    path.write_text(json.dumps(older), encoding="utf-8")
    assert store.load("a" * 32).mode == "", "a conversation saved before Plan mode existed"
