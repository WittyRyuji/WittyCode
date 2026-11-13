"""The ChatGPT Plan engine and controller as the application uses them: a real agent loop, the real tools and the real
shared-memory server (a subprocess), against the loopback fake of OpenAI's Responses API."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

import pytest

from desktop.core.chatgpt.errors import AuthError, PlanNotEnabled
from desktop.core.memory.launch import MemoryLaunch
from desktop.core.memory.store import MemoryStore
from desktop.core.session_state import SessionState
from desktop.core.transcript import PromptStatus, ToolStatus, TurnStatus
from tests.chatgpt_support import ACCESS_TOKEN, finish_turn, make_engine, until
from tests.fake_responses import FakeResponses, call, calls, fail, say, usage


def run(coroutine: Any) -> Any:
    return asyncio.run(coroutine)


def project(tmp_path: Path) -> Path:
    folder = tmp_path / "project"
    folder.mkdir()
    return folder


def texts(controller: Any) -> list[str]:
    return [block.text for turn in controller.transcript.turns for block in turn.blocks if block.kind == "text"]


def tools_of(controller: Any) -> list[Any]:
    return [tool for turn in controller.transcript.turns for tool in turn.tools.values()]


def sent(fake: FakeResponses) -> list[list[dict[str, Any]]]:
    return [request["input"] for request in fake.requests]


async def opened(tmp_path: Path, fake: FakeResponses, *, autonomy: str = "code", memory: MemoryLaunch | None = None, **kwargs: Any):
    engine = make_engine(tmp_path, fake, **kwargs)
    if memory is not None:
        engine.memory = memory
    controller = await engine.open_new(project(tmp_path) if not (tmp_path / "project").exists() else tmp_path / "project", model="gpt-6.1-sol", reasoning="medium", autonomy=autonomy)
    return engine, controller


# ----------------------------------------------------------------------------------------------- opening


def test_nobody_signed_in_means_no_conversation(tmp_path):
    async def go() -> None:
        engine = make_engine(tmp_path, None, signed_in=False)
        with pytest.raises(AuthError) as caught:
            await engine.open_new(project(tmp_path))
        assert caught.value.terminal
        assert engine.cached_account()["auth"] == "signed-out"
        assert engine.controllers == []

    run(go())


def test_a_sign_in_without_the_permission_to_use_the_plan_opens_nothing(tmp_path):
    async def go() -> None:
        engine = make_engine(tmp_path, None, plan=False)
        view = engine.cached_account()
        assert (view["auth"], view["planEnabled"]) == ("signed-in", False)
        with pytest.raises(PlanNotEnabled):
            await engine.open_new(project(tmp_path))

    run(go())


def test_the_account_view_names_the_account_and_marks_the_active_one(tmp_path):
    engine = make_engine(tmp_path, None)
    view = engine.cached_account()
    assert (view["auth"], view["email"], view["planEnabled"]) == ("signed-in", "me@example.com", True)
    assert [(a["label"], a["active"], a["signedIn"]) for a in view["accounts"]][0][1:] == (True, True)
    assert "signingIn" not in view


# ----------------------------------------------------------------------------------------------- a turn


def test_a_message_gets_an_answer_and_the_conversation_is_saved(tmp_path):
    async def go() -> None:
        with FakeResponses(token=ACCESS_TOKEN) as fake:
            fake.script(say("Hello there!", used=usage(200, 40, cached=50)))
            engine, controller = await opened(tmp_path, fake)
            assert controller.state is SessionState.READY and controller.model == "gpt-6.1-sol"
            assert controller.send_prompt("Say hello")
            assert controller.state is SessionState.RUNNING
            await finish_turn(controller)
            assert texts(controller) == ["Hello there!"]
            turn = controller.transcript.turns[0]
            assert turn.status is TurnStatus.DONE and turn.user_block.text == "Say hello"
            request = fake.requests[0]
            assert request["model"] == "gpt-6.1-sol" and request["reasoning"]["effort"] == "medium"
            assert str(tmp_path / "project") in request["instructions"]
            assert {"read_file", "shell", "edit_file"} <= {t["name"] for t in request["tools"]}
            assert fake.headers[0]["authorization"] == f"Bearer {ACCESS_TOKEN}"
            assert fake.headers[0]["originator"] == "wittycode"
            # tokens: what the cache did not serve is "input"; the total is kept for the whole conversation
            assert (controller.usage.input_tokens, controller.usage.output_tokens, controller.usage.cache_read_tokens) == (150, 40, 50)
            assert controller.usage_series() == {"self": controller.usage}
            assert controller.context.used == 240
            saved = engine.sessions.load(controller.session_id)
            assert saved is not None and not saved.title and saved.messages == 1, "the app summarizes titles separately; the controller must not copy the first sentence"
            assert saved.items[-1]["type"] == "message" and saved.items[-1]["role"] == "assistant"
            assert [row.id for row in await engine.list_sessions(tmp_path / "project")] == [controller.session_id]
            await engine.close_all()

    run(go())


def test_a_tool_call_runs_in_the_project_and_its_result_reaches_the_model_and_the_page(tmp_path):
    async def go() -> None:
        (tmp_path / "project").mkdir()
        (tmp_path / "project" / "notes.txt").write_text("alpha\nbeta\n", encoding="utf-8")
        with FakeResponses(token=ACCESS_TOKEN) as fake:
            fake.script(call("read_file", {"file_path": "notes.txt"}, call_id="c1", text="Reading."), say("It has two lines."))
            engine, controller = await opened(tmp_path, fake)
            controller.send_prompt("What is in notes.txt?")
            await finish_turn(controller)
            (tool,) = tools_of(controller)
            assert (tool.name, tool.status, tool.input["file_path"]) == ("Read", ToolStatus.DONE, "notes.txt")
            assert "alpha" in tool.result
            second = sent(fake)[1]
            assert [item["type"] for item in second] == ["message", "message", "function_call", "function_call_output"]
            assert "alpha" in second[-1]["output"] and second[-1]["call_id"] == "c1"
            assert texts(controller) == ["Reading.", "It has two lines."]
            await engine.close_all()

    run(go())


def test_at_code_level_an_edit_in_the_project_runs_but_a_command_waits_for_the_user(tmp_path):
    async def go() -> None:
        with FakeResponses(token=ACCESS_TOKEN) as fake:
            fake.script(
                calls([("create_file", json.dumps({"file_path": "new.txt", "content": "hi\n"}), "c1", "f1"), ("shell", json.dumps({"command": "python -c \"print('done')\""}), "c2", "f2")]),
                say("ok"),
            )
            engine, controller = await opened(tmp_path, fake, autonomy="code")
            controller.send_prompt("make a file and run something")
            await until(lambda: controller.transcript.pending_prompts(), message="the question about the command")
            assert (tmp_path / "project" / "new.txt").read_text(encoding="utf-8") == "hi\n"
            (prompt,) = controller.transcript.pending_prompts()
            assert prompt.actions[0].kind == "execute" and prompt.actions[0].command == "python -c \"print('done')\""
            assert [o.value for o in prompt.options] == ["proceed_once", "proceed_always", "cancel"]
            assert controller.working_label == "等待你的确认"
            assert controller.answer_permission(prompt.id, "proceed_once")
            await finish_turn(controller)
            shell = next(t for t in tools_of(controller) if t.name == "Execute")
            assert shell.status is ToolStatus.DONE and "done" in shell.result and "Exit code: 0" in shell.result
            await engine.close_all()

    run(go())


def test_a_refused_command_is_not_run_and_the_model_is_told(tmp_path):
    async def go() -> None:
        with FakeResponses(token=ACCESS_TOKEN) as fake:
            fake.script(call("shell", {"command": "New-Item marker.txt"}, call_id="c1"), say("understood"))
            engine, controller = await opened(tmp_path, fake, autonomy="ask")
            controller.send_prompt("run it")
            await until(lambda: controller.transcript.pending_prompts(), message="the question")
            prompt = controller.transcript.pending_prompts()[0]
            controller.answer_permission(prompt.id, "cancel")
            await finish_turn(controller)
            assert not (tmp_path / "project" / "marker.txt").exists()
            tool = next(iter(tools_of(controller)))
            assert tool.status is ToolStatus.CANCELLED
            assert "declined" in sent(fake)[1][-1]["output"]
            assert prompt.status is not PromptStatus.PENDING
            await engine.close_all()

    run(go())


def test_allowing_once_for_the_session_is_remembered_in_the_conversation(tmp_path):
    async def go() -> None:
        with FakeResponses(token=ACCESS_TOKEN) as fake:
            fake.script(
                call("shell", {"command": "python -c \"print('one')\""}, call_id="c1"),
                call("shell", {"command": "python -c \"print('two')\""}, call_id="c2"),
                say("done"),
            )
            engine, controller = await opened(tmp_path, fake, autonomy="code")
            controller.send_prompt("twice")
            await until(lambda: controller.transcript.pending_prompts(), message="the first question")
            controller.answer_permission(controller.transcript.pending_prompts()[0].id, "proceed_always")
            await finish_turn(controller)  # (the second command did not ask again, or this would time out)
            outputs = [item["output"] for request in sent(fake) for item in request if item["type"] == "function_call_output"]
            assert any("two" in output for output in outputs)
            await engine.close_all()

    run(go())


# ----------------------------------------------------------------------------------------------- stopping, steering, failing


def test_stopping_a_turn_that_is_streaming_drops_the_connection_and_the_conversation_goes_on(tmp_path):
    async def go() -> None:
        with FakeResponses(token=ACCESS_TOKEN) as fake:
            fake.script([*say("partial")[:3], ("hold", "stuck")], say("second answer"))
            engine, controller = await opened(tmp_path, fake)
            controller.send_prompt("first")
            await until(lambda: len(fake.requests) == 1 and texts(controller), message="the stream to start")
            await controller.interrupt()
            assert controller.state is SessionState.READY
            turn = controller.transcript.turns[0]
            assert turn.status is TurnStatus.INTERRUPTED
            saved = engine.sessions.load(controller.session_id)
            assert saved is not None and saved.turns[0].status == "interrupted"
            fake.release("stuck")
            controller.send_prompt("second")
            await finish_turn(controller)
            assert texts(controller)[-1] == "second answer"
            # The interrupted turn keeps what was already shown, so the next request can continue from it.
            assert [i["role"] for i in fake.requests[1]["input"] if i["type"] == "message"] == ["user", "assistant", "user"]
            await engine.close_all()

    run(go())


def test_stopping_while_a_command_runs_kills_it_and_the_call_still_gets_an_output(tmp_path):
    async def go() -> None:
        with FakeResponses(token=ACCESS_TOKEN) as fake:
            fake.script(call("shell", {"command": "Start-Sleep -Seconds 60", "timeout": 120}, call_id="c1"), say("after"))
            engine, controller = await opened(tmp_path, fake, autonomy="full")
            controller.send_prompt("sleep")
            await until(lambda: any(t.status is ToolStatus.RUNNING for t in tools_of(controller)), message="the command to run")
            await controller.interrupt()
            assert controller.state is SessionState.READY
            calls_, outputs = [i for i in controller.saved.items if i["type"] == "function_call"], [i for i in controller.saved.items if i["type"] == "function_call_output"]
            assert len(calls_) == len(outputs) == 1
            await engine.close_all()

    run(go())


def test_a_message_typed_during_the_turn_reaches_the_model_at_its_next_request(tmp_path):
    async def go() -> None:
        (tmp_path / "project").mkdir()
        (tmp_path / "project" / "a.txt").write_text("a", encoding="utf-8")
        with FakeResponses(token=ACCESS_TOKEN) as fake:
            fake.script([("hold", "go"), *call("read_file", {"file_path": "a.txt"}, call_id="c1")], say("ok, also b"))
            engine, controller = await opened(tmp_path, fake)
            controller.send_prompt("read a")
            await until(lambda: len(fake.requests) == 1, message="the first request")
            outcome = await controller.steer("and also look at b")
            assert outcome.ok
            block = controller.transcript.turns[0].blocks[-1]
            assert block.steer and block.pending
            fake.release("go")
            await finish_turn(controller)
            assert not block.pending
            last_user = [i for i in fake.requests[1]["input"] if i["type"] == "message" and i["role"] == "user"][-1]
            assert last_user["content"][0]["text"] == "and also look at b"
            assert controller.take_unadopted() == []
            await engine.close_all()

    run(go())


def test_steering_with_no_turn_running_says_the_turn_is_over(tmp_path):
    async def go() -> None:
        with FakeResponses(token=ACCESS_TOKEN) as fake:
            engine, controller = await opened(tmp_path, fake)
            outcome = await controller.steer("late")
            assert not outcome.ok and outcome.over
            await engine.close_all()

    run(go())


def test_a_usage_limit_error_is_shown_in_words_and_leaves_no_trace_in_the_history(tmp_path):
    async def go() -> None:
        with FakeResponses(token=ACCESS_TOKEN) as fake:
            fake.script(fail("subscription_sharing_usage_limit_exceeded"), say("fine now"))
            engine, controller = await opened(tmp_path, fake)
            controller.send_prompt("first try")
            await finish_turn(controller)
            turn = controller.transcript.turns[0]
            assert turn.status is TurnStatus.FAILED
            error = next(b for b in turn.blocks if b.kind == "error")
            assert "用量" in error.message and "ChatGPT" in error.message
            assert controller.saved.items == [] and controller.saved.turns == []
            assert engine.sessions.load(controller.session_id) is None  # (nothing worth saving yet)
            controller.send_prompt("second try")
            await finish_turn(controller)
            assert len([i for i in fake.requests[1]["input"] if i["type"] == "message"]) == 1
            await engine.close_all()

    run(go())


def test_a_bad_token_is_reported_as_a_refusal_not_a_crash(tmp_path, monkeypatch):
    async def go() -> None:
        with FakeResponses(token="some-other-token") as fake:
            engine, controller = await opened(tmp_path, fake)
            async def refused_refresh(client_id, record):
                raise AuthError("refresh refused", code="invalid_grant", terminal=True)
            monkeypatch.setattr(engine.tokens, "_renew", refused_refresh)
            controller.send_prompt("hi")
            await finish_turn(controller)
            error = next(b for b in controller.transcript.turns[0].blocks if b.kind == "error")
            assert "重新登录" in error.message
            await engine.close_all()

    run(go())


# ----------------------------------------------------------------------------------------------- the shared memory


def test_the_shared_memory_is_offered_to_the_model_and_a_note_it_writes_says_who_wrote_it(tmp_path):
    async def go() -> None:
        memory = MemoryLaunch(tmp_path / "memory")
        with FakeResponses(token=ACCESS_TOKEN) as fake:
            args = {"title": "Deploy command", "text": "Deploy with make ship-it.", "tags": ["deploy"]}
            fake.script(call("mcp__wittycode-memory__memory_write", args, call_id="c1"), say("noted"))
            engine, controller = await opened(tmp_path, fake, autonomy="ask", memory=memory)
            controller.send_prompt("remember how we deploy")
            await finish_turn(controller)  # (no question: the memory's tools are declared harmless)
            names = {t["name"] for t in fake.requests[0]["tools"]}
            assert "mcp__wittycode-memory__memory_search" in names and "mcp__wittycode-memory__memory_write" in names
            assert "memory_search" in fake.requests[0]["instructions"]
            (note,) = memory.store.entries(project=None)
            assert note.title == "Deploy command" and note.source["engine"] == "chatgpt" and note.source["run"] == controller.run
            tool = next(iter(tools_of(controller)))
            assert tool.name == "mcp__wittycode-memory__memory_write" and tool.category.value == "memory" and tool.status is ToolStatus.DONE
            await engine.close_all()

    run(go())


def test_the_first_message_of_a_new_conversation_carries_the_notes_brief_once(tmp_path):
    async def go() -> None:
        memory = MemoryLaunch(tmp_path / "memory")
        memory.store.create(title="Use tabs", text="Indent with tabs.", scope="global", project=None, kind="preference", tags=["indent"], by={"engine": "droid", "run": ""})
        with FakeResponses(token=ACCESS_TOKEN) as fake:
            fake.script(say("one"), say("two"))
            engine, controller = await opened(tmp_path, fake, memory=memory)
            controller.send_prompt("first")
            await finish_turn(controller)
            controller.send_prompt("second")
            await finish_turn(controller)
            first_text = "".join(part["text"] for part in fake.requests[0]["input"][0]["content"])
            assert "Use tabs" in first_text and "<wittycode-context>" in first_text
            user_messages = [i for i in fake.requests[1]["input"] if i["type"] == "message" and i["role"] == "user"]
            assert "Use tabs" not in user_messages[1]["content"][0]["text"]
            await engine.close_all()

    run(go())


def test_a_turn_tells_how_fast_the_model_wrote_and_how_long_it_took_to_begin(tmp_path):
    async def go() -> None:
        from desktop.core.speed import speed_of

        with FakeResponses(token=ACCESS_TOKEN) as fake:
            fake.script([("sleep", 0.8), *say("An answer that is long enough to measure.", used=usage(500, 120))])
            engine, controller = await opened(tmp_path, fake)
            controller.send_prompt("write")
            await finish_turn(controller)
            turn = controller.transcript.turns[0]
            assert turn.output_tokens == 120, "what the session counted while the turn ran"
            speed = speed_of(controller.transcript.turns)
            assert speed is not None and speed.tokens == 120
            assert speed.first is not None and 0.8 <= speed.first < 5, "the wait before the first output"
            assert 0.8 <= speed.seconds < 6 and abs(speed.tps - 120 / speed.seconds) < 0.2
            await engine.close_all()

    run(go())


def test_the_time_a_command_runs_is_not_counted_as_the_models(tmp_path):
    async def go() -> None:
        with FakeResponses(token=ACCESS_TOKEN) as fake:
            fake.script(call("shell", {"command": "Start-Sleep -Seconds 2", "timeout": 30}, call_id="c1", used=usage(300, 30)), say("done", used=usage(400, 80)))
            engine, controller = await opened(tmp_path, fake, autonomy="full")
            controller.send_prompt("wait a little")
            await finish_turn(controller)
            turn = controller.transcript.turns[0]
            assert turn.output_tokens == 110, "both requests of the turn"
            assert turn.blocked_seconds >= 1.9, "the command's time"
            assert abs(turn.model_seconds + turn.blocked_seconds - turn.duration) < 0.01
            await engine.close_all()

    run(go())


ASK = "what is the deploy command for this project"
NOTE = "The deploy command is make ship-it-7421."


def user_text(request: dict[str, Any], index: int = -1) -> str:
    messages = [item for item in request["input"] if item["type"] == "message" and item["role"] == "user"]
    return messages[index]["content"][0]["text"]


def test_a_message_goes_with_the_notes_that_bear_on_it_once_and_the_page_says_so(tmp_path):
    async def go() -> None:
        memory = MemoryLaunch(tmp_path / "memory")
        folder = project(tmp_path)
        made = MemoryStore(memory.root).create(title="Deploy command", text=NOTE, scope="project", project=str(folder.resolve()), kind="convention", by="user")
        MemoryStore(memory.root).create(title="Lint command", text="The lint command is make lint-strict-9917.", scope="project", project=str(folder.resolve()), by="user")
        with FakeResponses(token=ACCESS_TOKEN) as fake:
            fake.script(fail("subscription_sharing_usage_limit_exceeded"), say("one"), say("two"), say("three"))
            engine, controller = await opened(tmp_path, fake, memory=memory)
            engine.hooks.on_prompt(memory.prompt_hook)
            controller.send_prompt(ASK)
            await finish_turn(controller)
            assert controller.recalled == set(), "a message that failed has not given them"
            controller.send_prompt(ASK)
            await finish_turn(controller)
            text = user_text(fake.requests[1])
            assert text.startswith(ASK + "\n\n<wittycode-context>\n[Shared memory") and f"- {made.id} Deploy command" in text and NOTE in text
            turn = controller.transcript.turns[-1]
            assert turn.user_block.text == ASK, "the page shows what was typed"
            assert [b.text for b in turn.blocks if b.kind == "notice"] == ["带上了 1 条相关记忆：「Deploy command」"]
            assert controller.recalled == {made.id}
            controller.send_prompt("the deploy command, once more")
            await finish_turn(controller)
            assert user_text(fake.requests[2]) == "the deploy command, once more", "the same note is not given twice"
            controller.send_prompt("what is the lint command for this project")
            await finish_turn(controller)
            assert user_text(fake.requests[3]) == "what is the lint command for this project", "only the first message brings notes"
            await engine.close_all()

    run(go())


def test_a_picture_is_refused_for_a_model_that_cannot_read_it_and_a_pdf_without_text_for_every_model(tmp_path):
    async def go() -> None:
        from desktop.core.attachments import image_attachment, pdf_attachment, text_attachment

        models = [
            {"slug": "gpt-6.1-sol", "display_name": "GPT-6.1-Sol", "visibility": "list", "input_modalities": ["text", "image"]},
            {"slug": "text-only", "display_name": "Text only", "visibility": "list", "input_modalities": ["text"]},
        ]
        with FakeResponses(token=ACCESS_TOKEN, models=models) as fake:
            engine = make_engine(tmp_path, fake)
            await engine.list_models()
            picture = image_attachment("a.png", b"\x89PNG\r\n\x1a\n", "image/png")
            scan = pdf_attachment("scan.pdf", b"%PDF-1.4", parsed_text="")
            controller = await engine.open_new(project(tmp_path), model="text-only")
            problem = controller.attachment_problem([picture])
            assert problem is not None and "Text only" in problem and "图片" in problem
            problem = controller.attachment_problem([scan])
            assert problem is not None and "scan.pdf" in problem
            assert controller.attachment_problem([text_attachment("n.txt", "hi")]) is None
            await controller.update_settings(model="gpt-6.1-sol")
            assert controller.attachment_problem([picture]) is None
            assert controller.attachment_problem([scan]) is not None
            await engine.close_all()

    run(go())


def test_a_turn_that_runs_out_of_steps_says_so(tmp_path):
    async def go() -> None:
        with FakeResponses(token=ACCESS_TOKEN) as fake:
            where = str(tmp_path / "project")
            fake.script(*[call("list_dir", {"directory_path": where}, call_id=f"c{i}") for i in range(3)])
            engine, controller = await opened(tmp_path, fake)
            engine.settings.max_steps = 2
            controller.send_prompt("keep going")
            await finish_turn(controller)
            turn = controller.transcript.turns[0]
            assert turn.status is TurnStatus.DONE and len(fake.requests) == 2
            assert any("2 步" in b.text for b in turn.blocks if b.kind == "notice")
            await engine.close_all()

    run(go())


def test_an_instruction_file_that_is_too_long_is_cut_by_section_and_the_page_says_so_once(tmp_path):
    async def go() -> None:
        with FakeResponses(token=ACCESS_TOKEN) as fake:
            fake.script(say("one"), say("two"))
            folder = project(tmp_path)
            body = "".join(f"## 背景{i}\n" + "很长的背景说明。" * 400 + "\n\n" for i in range(12))
            (folder / "AGENTS.md").write_text("# 项目\n\n## 规则\n- 提交前跑测试。\n\n" + body, encoding="utf-8")
            engine, controller = await opened(tmp_path, fake)
            controller.send_prompt("first")
            await finish_turn(controller)
            controller.send_prompt("second")
            await finish_turn(controller)
            notices = [b.text for turn in controller.transcript.turns for b in turn.blocks if b.kind == "notice" and "AGENTS.md" in b.text]
            assert len(notices) == 1 and "已按章节缩减" in notices[0], "told once, not on every turn"
            assert "提交前跑测试" in fake.requests[0]["instructions"], "the rules section is what stays"
            assert fake.requests[0]["instructions"] == fake.requests[1]["instructions"]
            await engine.close_all()

    run(go())


def test_stopping_a_turn_kills_the_command_that_was_running(tmp_path):
    async def go() -> None:
        with FakeResponses(token=ACCESS_TOKEN) as fake:
            fake.script(call("shell", {"command": "Start-Sleep -Seconds 3; New-Item late.txt", "timeout": 60}, call_id="c1"), say("after"))
            engine, controller = await opened(tmp_path, fake, autonomy="full")
            controller.send_prompt("sleep, then write a file")
            await until(lambda: any(t.status is ToolStatus.RUNNING for t in tools_of(controller)), message="the command to run")
            await controller.interrupt()
            await asyncio.sleep(5)  # (longer than the command's own sleep)
            assert not (tmp_path / "project" / "late.txt").exists(), "the command went on after the stop"
            await engine.close_all()

    run(go())


def test_what_was_said_during_a_turn_that_is_stopped_goes_back_to_the_user(tmp_path):
    async def go() -> None:
        with FakeResponses(token=ACCESS_TOKEN) as fake:
            fake.script([("hold", "go"), *say("never read")])
            engine, controller = await opened(tmp_path, fake)
            controller.send_prompt("first")
            await until(lambda: len(fake.requests) == 1, message="the request")
            assert (await controller.steer("one more thing")).ok
            await controller.interrupt()
            fake.release("go")
            assert controller.take_unadopted() == [("one more thing", ())]
            assert controller.take_unadopted() == []
            await engine.close_all()

    run(go())


def test_a_sign_in_that_is_gone_is_told_to_the_page_when_a_message_meets_it(tmp_path):
    async def go() -> None:
        with FakeResponses(token=ACCESS_TOKEN) as fake:
            engine, controller = await opened(tmp_path, fake)
            seen: list[dict[str, Any]] = []
            engine.add_account_listener(seen.append)
            engine.accounts.clear_tokens(engine.accounts.active_id(), keep_id_token=False)  # (signed out in another window)
            controller.send_prompt("hi")
            await finish_turn(controller)
            error = next(b for b in controller.transcript.turns[0].blocks if b.kind == "error")
            assert "重新登录" in error.message
            assert seen and seen[-1]["auth"] == "signed-out", "the account card changes at once"
            assert fake.requests == []
            await engine.close_all()

    run(go())


# ----------------------------------------------------------------------------------------------- reopening and closing


def test_an_imported_title_survives_the_first_turn(tmp_path):
    async def go() -> None:
        with FakeResponses(token=ACCESS_TOKEN) as fake:
            fake.script(say("接着聊。"))
            engine = make_engine(tmp_path, fake)
            folder = project(tmp_path)
            started = 1_790_000_000.0
            from desktop.core.chatgpt.responses import user_message
            from desktop.core.harness.store import SavedSession, SavedTurn
            from desktop.core.importers.convert import assistant_item

            saved = SavedSession(
                id="abcdefabcdefabcdefabcdefabcdefab", project=str(folder), title="重构 session_manager", model="chatgpt:gpt-6.1-sol",
                reasoning="", autonomy="code", created_at=started, modified_at=started,
                items=[user_message("原来的第一句话"), assistant_item("原来的回答")],
                turns=[SavedTurn(0, started, started + 1, "done", model="Claude Code")],
                origin={"app": "claude", "native": "n1", "at": started},
            )
            engine.sessions.save(saved)
            controller = await engine.open_saved(folder, saved.id)
            assert controller.title == "重构 session_manager", "the imported name is what the page shows"
            controller.send_prompt("接着聊")
            await finish_turn(controller)
            again = engine.sessions.load(saved.id)
            assert again is not None and again.title == "重构 session_manager" and again.origin["app"] == "claude"
            await engine.close_all()

    run(go())


def test_a_saved_conversation_comes_back_with_its_turns_tools_tokens_and_settings(tmp_path):
    async def go() -> None:
        (tmp_path / "project").mkdir()
        (tmp_path / "project" / "notes.txt").write_text("alpha\n", encoding="utf-8")
        with FakeResponses(token=ACCESS_TOKEN) as fake:
            fake.script(
                call("read_file", {"file_path": "notes.txt"}, call_id="c1", text="Looking."),
                call("edit_file", {"file_path": "notes.txt", "old_str": "alpha", "new_str": "omega"}, call_id="c2"),
                say("Changed it.", thinking="Considering the edit."),
            )
            engine, controller = await opened(tmp_path, fake)
            controller.send_prompt("change alpha to omega")
            await finish_turn(controller)
            session_id, total = controller.session_id, controller.usage
            await controller.close()

            again = make_engine(tmp_path, fake)
            rows = await again.list_sessions(tmp_path / "project")
            assert [(r.id, r.title, r.messages) for r in rows] == [(session_id, "", 1)]
            reopened = await again.open_saved(tmp_path / "project", session_id)
            assert reopened.state is SessionState.READY and reopened.model == "gpt-6.1-sol" and reopened.reasoning == "medium"
            (turn,) = reopened.transcript.turns
            assert turn.status is TurnStatus.DONE and turn.user_block.text == "change alpha to omega"
            names = [(t.name, t.status) for t in turn.tools.values()]
            assert names == [("Read", ToolStatus.DONE), ("Edit", ToolStatus.DONE)]
            edit = list(turn.tools.values())[1]
            assert edit.diffs and edit.diffs[0].added == 1 and edit.diffs[0].removed == 1
            assert [b.text for b in turn.blocks if b.kind == "text"] == ["Looking.", "Changed it."]
            assert any(getattr(e, "text", "") == "Considering the edit." for e in turn.activity_entries())
            assert reopened.usage == total
            assert reopened.live_settings().autonomy == "code"
            # and it goes on: the model gets the whole earlier conversation
            fake.script(say("welcome back"))
            reopened.send_prompt("and now?")
            await finish_turn(reopened)
            assert any(i["type"] == "function_call_output" for i in fake.requests[-1]["input"])
            await again.close_all()

    run(go())


def test_closing_saves_and_stops_the_memory_server(tmp_path):
    async def go() -> None:
        memory = MemoryLaunch(tmp_path / "memory")
        with FakeResponses(token=ACCESS_TOKEN) as fake:
            fake.script(say("hi"))
            engine, controller = await opened(tmp_path, fake, memory=memory)
            controller.send_prompt("hello")
            await finish_turn(controller)
            (client,) = controller._clients
            assert client.running
            await controller.close()
            assert controller.state is SessionState.CLOSED and not client.running
            assert engine.controllers == [] and controller.evicted is False
            assert engine.sessions.load(controller.session_id) is not None

    run(go())


def test_an_unknown_saved_conversation_cannot_be_opened(tmp_path):
    async def go() -> None:
        engine = make_engine(tmp_path, None)
        with pytest.raises(Exception) as caught:
            await engine.open_saved(project(tmp_path), "0123456789abcdef")
        assert "存档" in str(caught.value) or "missing" in str(caught.value)
        assert engine.controllers == []

    run(go())


# ----------------------------------------------------------------------------------------------- models


def test_the_model_list_is_the_accounts_own_and_a_failed_read_falls_back(tmp_path):
    async def go() -> None:
        with FakeResponses(token=ACCESS_TOKEN) as fake:
            engine = make_engine(tmp_path, fake)
            models = await engine.list_models()
            assert [m.id for m in models] == ["gpt-6.1-sol", "gpt-6-luna"]
            assert engine.model_window("gpt-6.1-sol") == 400000 and engine.model_window("gpt-6-luna") is None
            await engine.list_models()
            assert fake.model_requests == 1  # (kept for a while)
            await engine.list_models(refresh=True)
            assert fake.model_requests == 2
        with FakeResponses(token="other") as refused:
            engine = make_engine(tmp_path / "second", refused)
            fallback = await engine.list_models()
            assert fallback and fallback[0].id == "gpt-6.1-sol"

    run(go())


def test_a_wrong_permission_name_is_refused_and_nothing_changes(tmp_path):
    async def go() -> None:
        with FakeResponses(token=ACCESS_TOKEN) as fake:
            engine, controller = await opened(tmp_path, fake)
            assert not await controller.update_settings(autonomy="root")
            assert controller.autonomy == "code"
            assert await controller.update_settings(autonomy="full", model="gpt-6-luna", reasoning="low")
            assert (controller.autonomy, controller.model, controller.reasoning) == ("full", "gpt-6-luna", "low")
            assert controller.live_settings().autonomy == "full"
            await engine.close_all()

    run(go())


def test_a_server_that_is_busy_for_a_moment_does_not_end_the_turn(tmp_path, monkeypatch):
    from desktop.core.harness import loop as loop_module
    from tests.fake_responses import http_error

    monkeypatch.setattr(loop_module, "RETRY_DELAYS", (0.0, 0.0))

    async def go() -> None:
        with FakeResponses(token=ACCESS_TOKEN) as fake:
            fake.script(http_error(503, {"error": {"message": "overloaded", "code": "server_error"}}), say("Back again."))
            engine, controller = await opened(tmp_path, fake)
            controller.send_prompt("hello")
            await finish_turn(controller)
            turn = controller.transcript.turns[-1]
            assert turn.status is TurnStatus.DONE
            assert [b.text for b in turn.blocks if b.kind == "text"] == ["Back again."]
            assert any(b.kind == "notice" and "服务暂时出错（HTTP 503）" in b.text and "自动重试" in b.text for b in turn.blocks)
            assert len(fake.requests) == 2 and fake.requests[0]["input"] == fake.requests[1]["input"], "the same request again"
            await engine.close_all()

    run(go())
