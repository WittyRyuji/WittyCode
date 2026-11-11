"""Codex sessions against the fake app-server: turns, approvals, stop, crash and reconnect, handoff, settings."""

from __future__ import annotations

import asyncio
import base64

import pytest

from desktop.core.attachments import image_attachment, pdf_attachment, text_attachment
from desktop.core.codex import controller as codex_controller
from desktop.core.codex.controller import CodexController, CodexUsage
from desktop.core.handoff import build_handoff
from desktop.core.session_manager import LiveSettings, SessionState
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
from tests.codex_support import finish_turn, make_engine, run, until
from tests.support import tiny_png


async def opened(harness, project, **options) -> CodexController:
    controller = await harness.engine.open_new(project, **options)
    assert controller.state is SessionState.READY, controller.last_error
    return controller


async def pending_prompt(controller: CodexController):
    await until(lambda: controller.transcript.pending_prompts())
    (prompt,) = controller.transcript.pending_prompts()
    return prompt


def test_opening_without_an_account_is_refused_before_a_thread_exists(tmp_path):
    async def scenario():
        harness = make_engine(tmp_path, account="signed-out")
        controller = await harness.engine.open_new(tmp_path)
        assert controller.state is SessionState.CLOSED and "登录" in controller.last_error
        assert harness.requests("thread/start") == [] and harness.engine.controllers == []
        assert isinstance(controller.transcript.turns[-1].blocks[-1], ErrorBlock)
        await harness.engine.close_all()

    run(scenario())


def test_a_full_turn_becomes_text_thinking_and_tools(tmp_path):
    async def scenario():
        harness = make_engine(tmp_path)
        events: list[tuple[str, str]] = []
        harness.engine.add_listener(lambda c, kind: events.append((kind, c.state.value)))
        controller = await opened(harness, tmp_path, model="gpt-6.1-sol", reasoning="high", autonomy="workspace")
        assert controller.session_id and controller.model == "gpt-6.1-sol" and controller.engine == "codex"
        assert ("state", "opening") in events and ("state", "ready") in events
        turn = await finish_turn(controller, "[full] fix it")
        assert turn.status is TurnStatus.DONE and controller.state is SessionState.READY
        kinds = [type(block) for block in turn.blocks]
        assert kinds == [UserBlock, ActivityBlock, TextBlockView, ActivityBlock, TextBlockView]
        user, thinking_group, commentary, tools_group, answer = turn.blocks
        assert user.text == "[full] fix it" and user.handoff is None
        (thinking,) = thinking_group.entries
        assert isinstance(thinking, ThinkingEntry) and thinking.text == "**Planning** the fix\n\nThen test" and thinking.complete
        assert commentary.text == "I'll run the tests." and not commentary.streaming
        assert answer.text == "Done: fixed." and not answer.streaming
        execute, edit, create, todo = tools_group.entries
        assert (execute.name, execute.input["command"], execute.status) == ("Execute", "pytest -q", ToolStatus.DONE)
        assert "2 passed" in execute.progress and execute.result.endswith("[Process exited with code 1]")
        assert execute.description == "pytest -q"
        assert (edit.name, edit.status) == ("Edit", ToolStatus.DONE) and edit.input["file_path"].endswith("app.py")
        (diff,) = edit.diffs
        assert (diff.added, diff.removed) == (1, 1)
        assert [(line.kind, line.text, line.old, line.new) for line in diff.lines] == [
            ("ctx", "def main():", 1, 1),
            ("del", "    return 1", 2, None),
            ("add", "    return 2", None, 2),
            ("ctx", "", 3, 3),
        ]
        assert create.name == "Create" and create.diffs[0].is_new_file and create.diffs[0].added == 2
        assert todo.name == "TodoWrite" and todo.status is ToolStatus.DONE
        assert todo.input["todos"] == [
            {"content": "Run the tests", "status": "completed"},
            {"content": "Fix app.py", "status": "in_progress"},
            {"content": "Run again", "status": "pending"},
        ]
        assert todo.description == "1/3 完成 · Fix app.py"
        assert controller.usage == CodexUsage(input_tokens=3000, output_tokens=3000, cache_creation_tokens=None, cache_read_tokens=9000, thinking_tokens=1000, total_tokens=15000)
        assert (controller.context.used, controller.context.window, controller.context.model) == (14_000, 272_000, "gpt-6.1-sol")
        (start,) = harness.requests("thread/start")
        assert start == {"cwd": str(tmp_path.resolve()), "approvalPolicy": "on-request", "sandbox": "workspace-write", "serviceName": "wittycode", "model": "gpt-6.1-sol"}
        (sent,) = harness.requests("turn/start")
        assert sent["input"] == [{"type": "text", "text": "[full] fix it", "text_elements": []}]
        assert (sent["approvalPolicy"], sent["model"], sent["effort"], sent["cwd"]) == ("on-request", "gpt-6.1-sol", "high", str(tmp_path.resolve()))
        assert sent["sandboxPolicy"] == {"type": "workspaceWrite", "writableRoots": [str(tmp_path.resolve())], "networkAccess": False, "excludeTmpdirEnvVar": False, "excludeSlashTmp": False}
        assert controller.live_settings() == LiveSettings("gpt-6.1-sol", "high", "workspace", "auto")
        assert controller.working_label == "就绪"
        await harness.engine.close_all()

    run(scenario())


def test_text_streams_while_the_turn_runs_and_the_output_follows_the_command(tmp_path):
    async def scenario():
        harness = make_engine(tmp_path)
        controller = await opened(harness, tmp_path)
        assert controller.send_prompt("[slow] count")
        assert controller.busy and not controller.send_prompt("again")
        turn = controller.transcript.turns[-1]
        await until(lambda: "tick 3" in next((t.progress for t in turn.tools.values()), ""))
        tool = turn.tools["cmd-slow"]
        assert tool.status is ToolStatus.RUNNING and controller.working_label == "正在执行工具…"
        await controller.interrupt()
        assert controller.state is SessionState.READY and turn.status is TurnStatus.INTERRUPTED
        assert tool.status is ToolStatus.CANCELLED
        (interrupt,) = harness.requests("turn/interrupt")
        assert interrupt["threadId"] == controller.session_id and interrupt["turnId"]
        assert (await finish_turn(controller, "after")).blocks[-1].text == "echo: after"
        await harness.engine.close_all()

    run(scenario())


def test_a_stop_that_codex_ignores_ends_the_turn_here(tmp_path, monkeypatch):
    monkeypatch.setattr(codex_controller, "INTERRUPT_GRACE_SECONDS", 0.4)

    async def scenario():
        harness = make_engine(tmp_path)
        controller = await opened(harness, tmp_path)
        assert controller.send_prompt("[stuck] count")
        turn = controller.transcript.turns[-1]
        await until(lambda: "cmd-slow" in turn.tools)
        await controller.interrupt()
        assert controller.state is SessionState.READY and turn.status is TurnStatus.INTERRUPTED
        assert any(isinstance(b, NoticeBlock) and "停止超时" in b.text for b in turn.blocks)
        await harness.engine.close_all()

    run(scenario())


@pytest.mark.parametrize(("value", "decision"), [("proceed_once", "accept"), ("proceed_always", "acceptForSession"), ("cancel", "decline")])
def test_command_approval(tmp_path, value, decision):
    async def scenario():
        harness = make_engine(tmp_path)
        controller = await opened(harness, tmp_path)
        assert controller.send_prompt("[approve-command] clean up")
        prompt = await pending_prompt(controller)
        assert [option.value for option in prompt.options] == ["proceed_once", "proceed_always", "cancel"]
        assert [option.original for option in prompt.options] == ["accept", "acceptForSession", "decline"]
        (action,) = prompt.actions
        assert (action.kind, action.command, action.reason) == ("execute", "Remove-Item build -Recurse", "deletes the build folder")
        turn = controller.transcript.turns[-1]
        assert turn.tools["cmd-ask"].status is ToolStatus.AWAITING_PERMISSION
        assert controller.working_label == "等待你的确认"
        assert controller.answer_permission(prompt.id, value)
        assert not controller.answer_permission(prompt.id, value), "a second click is refused"
        await until(lambda: not controller.busy)
        answers = [m for m in harness.answers() if "result" in m and "decision" in (m["result"] or {})]
        assert [m["result"]["decision"] for m in answers] == [decision]
        tool = turn.tools["cmd-ask"]
        if value == "cancel":
            assert tool.status is ToolStatus.CANCELLED and turn.blocks[-1].text == "not run (decline)"
        else:
            assert tool.status is ToolStatus.DONE and tool.result.startswith("removed") and tool.decision == value
        assert prompt.status is PromptStatus.ANSWERED and controller._pending == {} and controller._requests == {}
        await harness.engine.close_all()

    run(scenario())


def test_file_change_approval_shows_the_diff(tmp_path):
    async def scenario():
        harness = make_engine(tmp_path)
        controller = await opened(harness, tmp_path)
        assert controller.send_prompt("[approve-file] change it")
        prompt = await pending_prompt(controller)
        (action,) = prompt.actions
        assert action.kind == "edit" and action.title == "编辑文件 app.py" and action.diffs[0].added == 1
        assert controller.answer_permission(prompt.id, "proceed_once")
        await until(lambda: not controller.busy)
        turn = controller.transcript.turns[-1]
        assert turn.tools["fc-ask"].status is ToolStatus.DONE and turn.blocks[-1].text == "file completed (accept)"
        await harness.engine.close_all()

    run(scenario())


def test_an_open_approval_expires_when_the_turn_is_stopped(tmp_path):
    async def scenario():
        harness = make_engine(tmp_path)
        controller = await opened(harness, tmp_path)
        assert controller.send_prompt("[approve-command] clean up")
        prompt = await pending_prompt(controller)
        await controller.interrupt()
        assert controller.state is SessionState.READY
        assert prompt.status is PromptStatus.EXPIRED and controller._pending == {}
        assert not controller.answer_permission(prompt.id, "proceed_once")
        assert controller.transcript.turns[-1].status is TurnStatus.INTERRUPTED
        await harness.engine.close_all()

    run(scenario())


def test_a_stop_right_after_sending_waits_for_the_turn_id(tmp_path, monkeypatch):
    monkeypatch.setattr(codex_controller, "INTERRUPT_GRACE_SECONDS", 3.0)

    async def scenario():
        harness = make_engine(tmp_path)
        controller = await opened(harness, tmp_path)
        assert controller.send_prompt("[slow] count")
        await controller.interrupt()
        turn = controller.transcript.turns[-1]
        assert controller.state is SessionState.READY and turn.status is TurnStatus.INTERRUPTED
        assert not any(isinstance(b, NoticeBlock) and "停止超时" in b.text for b in turn.blocks)
        assert len(harness.requests("turn/interrupt")) == 1
        await harness.engine.close_all()

    run(scenario())


def test_an_approval_left_open_by_a_finished_turn_is_answered_cancel(tmp_path):
    async def scenario():
        harness = make_engine(tmp_path)
        controller = await opened(harness, tmp_path)
        turn = await finish_turn(controller, "[orphan-approval] go")
        assert turn.status is TurnStatus.DONE and not turn.pending_prompts()
        await until(lambda: any((m.get("result") or {}).get("decision") == "cancel" for m in harness.answers()))
        assert controller._pending == {} and controller._requests == {}
        await harness.engine.close_all()

    run(scenario())


def test_a_question_from_codex_is_answered(tmp_path):
    async def scenario():
        harness = make_engine(tmp_path)
        controller = await opened(harness, tmp_path)
        assert controller.send_prompt("[question] pick")
        prompt = await pending_prompt(controller)
        (question,) = prompt.questions
        assert (question.topic, question.question, question.options) == ("Color", "Which color?", ("red", "blue"))
        assert controller.answer_question(prompt.id, ["blue"])
        await until(lambda: not controller.busy)
        turn = controller.transcript.turns[-1]
        assert turn.blocks[-1].text == 'answers: {"color": {"answers": ["blue"]}}'
        assert turn.tools["q-1"].status is ToolStatus.DONE
        await harness.engine.close_all()

    run(scenario())


def test_a_failed_turn_shows_one_error_and_retries_show_in_the_label(tmp_path):
    async def scenario():
        harness = make_engine(tmp_path)
        controller = await opened(harness, tmp_path)
        turn = await finish_turn(controller, "[fail] go")
        assert turn.status is TurnStatus.FAILED and controller.state is SessionState.READY
        errors = [block for block in turn.blocks if isinstance(block, ErrorBlock)]
        assert len(errors) == 1 and "用量已到上限" in errors[0].message and "usage limit" in errors[0].message
        labels: list[str] = []
        controller.add_listener(lambda c, kind: labels.append(c.working_label))
        turn = await finish_turn(controller, "[retry] go")
        assert turn.status is TurnStatus.DONE and not [b for b in turn.blocks if isinstance(b, ErrorBlock)]
        assert "连接中断，正在重试（2/5）…" in labels
        await harness.engine.close_all()

    run(scenario())


def test_a_crash_makes_the_session_dead_and_reconnect_resumes_the_thread(tmp_path):
    async def scenario():
        harness = make_engine(tmp_path)
        controller = await opened(harness, tmp_path, autonomy="full")
        other = await opened(harness, tmp_path)
        assert controller.send_prompt("[crash] go")
        await until(lambda: controller.state is SessionState.DEAD)
        turn = controller.transcript.turns[-1]
        assert turn.status is TurnStatus.FAILED
        error = turn.blocks[-1]
        assert isinstance(error, ErrorBlock) and error.recoverable and "Codex 进程异常退出（exit code 3）" in error.message
        assert other.state is SessionState.DEAD, "the sessions share the process"
        assert not controller.send_prompt("now")
        await controller.reconnect()
        assert controller.state is SessionState.READY and error.resolved
        assert any(isinstance(b, NoticeBlock) and "已重新连接" in b.text for b in controller.transcript.turns[-1].blocks)
        resumed = harness.requests("thread/resume")
        assert resumed[-1]["threadId"] == controller.session_id and resumed[-1]["approvalPolicy"] == "never"
        assert resumed[-1]["sandbox"] == "danger-full-access"
        assert len(harness.clients) == 2 and not harness.clients[0].running
        assert (await finish_turn(controller, "again")).blocks[-1].text == "echo: again"
        await other.reconnect()
        assert other.state is SessionState.READY
        await harness.engine.close_all()

    run(scenario())


def conversation() -> Transcript:
    transcript = Transcript()
    turn = transcript.start_turn("What is 2+2?")
    transcript.append_text(turn, "m1", 0, "4.")
    transcript.finish_turn(turn, TurnStatus.DONE)
    return transcript


def test_the_handoff_goes_to_codex_but_not_on_the_page_and_ends_after_a_good_turn(tmp_path):
    async def scenario():
        harness = make_engine(tmp_path)
        transcript = conversation()
        handoff = build_handoff(transcript.turns)
        controller = await opened(harness, tmp_path, transcript=transcript, handoff=handoff)
        assert controller.transcript is transcript and controller.handoff is handoff
        turn = await finish_turn(controller, "And times 3?")
        (sent,) = harness.requests("turn/start")
        assert sent["input"][0]["text"] == handoff.prefix + "And times 3?"
        assert turn.user_block.text == "And times 3?" and turn.user_block.handoff == (2, False)
        assert controller.handoff is None
        await finish_turn(controller, "next")
        assert harness.requests("turn/start")[-1]["input"][0]["text"] == "next"
        await harness.engine.close_all()

    run(scenario())


def test_the_handoff_is_kept_for_the_retry_when_the_turn_fails(tmp_path):
    async def scenario():
        harness = make_engine(tmp_path)
        transcript = conversation()
        handoff = build_handoff(transcript.turns)
        controller = await opened(harness, tmp_path, transcript=transcript, handoff=handoff)
        failed = await finish_turn(controller, "[fail] And times 3?")
        assert failed.status is TurnStatus.FAILED and controller.handoff is handoff
        assert failed.user_block.handoff == (2, False)
        await finish_turn(controller, "And times 3?")
        assert harness.requests("turn/start")[-1]["input"][0]["text"].startswith(handoff.prefix)
        assert controller.handoff is None
        await harness.engine.close_all()

    run(scenario())


def conversation_with_picture() -> tuple[Transcript, list]:
    transcript = Transcript()
    png = tiny_png()
    turn = transcript.start_turn("look at this", [image_attachment("shot.png", png, "image/png"), text_attachment("notes.md", "buy milk")])
    transcript.append_text(turn, "m1", 0, "A red box.")
    transcript.finish_turn(turn, TurnStatus.DONE)
    return transcript, list(turn.user_block.attachments)


def test_the_pictures_handed_over_go_with_the_first_message_and_not_with_the_next(tmp_path):
    async def scenario():
        harness = make_engine(tmp_path)
        transcript, (picture, notes) = conversation_with_picture()
        handoff = build_handoff(transcript.turns, carry=[picture, notes])
        assert [a.name for a in handoff.attachments] == ["shot.png"]
        controller = await opened(harness, tmp_path, transcript=transcript, handoff=handoff)
        await harness.engine.list_models()
        before = len(transcript.turns[0].user_block.attachments)

        turn = await finish_turn(controller, "and its size?")
        (sent,) = harness.requests("turn/start")
        assert sent["input"][0]["text"] == handoff.prefix + "and its size?"
        assert sent["input"][1] == {"type": "image", "url": "data:image/png;base64," + base64.b64encode(tiny_png()).decode("ascii")}
        assert len(sent["input"]) == 2
        assert turn.user_block.text == "and its size?" and not turn.user_block.attachments, "the page shows only what was typed"
        assert len(transcript.turns[0].user_block.attachments) == before
        assert controller.handoff is None

        await finish_turn(controller, "next")
        assert [item["type"] for item in harness.requests("turn/start")[-1]["input"]] == ["text"], "the picture goes once"
        await harness.engine.close_all()

    run(scenario())


def test_the_pictures_handed_over_stay_for_the_retry_when_the_turn_fails_and_count_with_the_new_ones(tmp_path):
    async def scenario():
        harness = make_engine(tmp_path)
        transcript, (picture, notes) = conversation_with_picture()
        handoff = build_handoff(transcript.turns, carry=[picture])
        controller = await opened(harness, tmp_path, transcript=transcript, handoff=handoff)
        await harness.engine.list_models()
        extra = image_attachment("extra.png", tiny_png(5, 5), "image/png")
        failed = await finish_turn(controller, "[fail] size?", [extra])
        assert failed.status is TurnStatus.FAILED and controller.handoff is handoff
        assert [a.name for a in failed.user_block.attachments] == ["extra.png"], "only what was attached now is shown"

        await finish_turn(controller, "size?", [extra])
        images = [item for item in harness.requests("turn/start")[-1]["input"] if item["type"] == "image"]
        assert len(images) == 2 and controller.handoff is None, "the retry carries both, then it is done"
        await harness.engine.close_all()

    run(scenario())


@pytest.mark.parametrize(
    ("level", "approval", "sandbox", "policy"),
    [
        ("readonly", "on-request", "read-only", "readOnly"),
        ("workspace", "on-request", "workspace-write", "workspaceWrite"),
        ("full", "never", "danger-full-access", "dangerFullAccess"),
        ("read-only", "on-request", "read-only", "readOnly"),
    ],
)
def test_permission_levels_map_to_approval_policy_and_sandbox(tmp_path, level, approval, sandbox, policy):
    async def scenario():
        harness = make_engine(tmp_path)
        controller = await opened(harness, tmp_path, autonomy=level)
        await finish_turn(controller, "hi")
        (start,) = harness.requests("thread/start")
        (turn,) = harness.requests("turn/start")
        assert (start["approvalPolicy"], start["sandbox"]) == (approval, sandbox)
        assert (turn["approvalPolicy"], turn["sandboxPolicy"]["type"]) == (approval, policy)
        await harness.engine.close_all()

    run(scenario())


def test_settings_changes_go_out_with_the_next_turn(tmp_path):
    async def scenario():
        harness = make_engine(tmp_path, settings=None)
        controller = await opened(harness, tmp_path)
        assert controller.live_settings() == LiveSettings("gpt-6.1-sol", "", "readonly", "auto")
        await harness.engine.list_models()
        assert controller.live_settings().reasoning == "low", "the model's default effort"
        kinds: list[str] = []
        controller.add_listener(lambda c, kind: kinds.append(kind))
        assert await controller.update_settings(model="gpt-6-luna", reasoning="high", autonomy="full", mode="spec")
        assert "settings" in kinds
        assert controller.live_settings() == LiveSettings("gpt-6-luna", "high", "full", "auto")
        assert not await controller.update_settings(autonomy="medium")
        await finish_turn(controller, "hi")
        (turn,) = harness.requests("turn/start")
        assert (turn["model"], turn["effort"], turn["approvalPolicy"], turn["sandboxPolicy"]) == (
            "gpt-6-luna",
            "high",
            "never",
            {"type": "dangerFullAccess"},
        )
        await harness.engine.close_all()

    run(scenario())


def test_a_workspace_level_without_a_windows_sandbox_is_explained(tmp_path):
    async def scenario():
        harness = make_engine(tmp_path, FAKE_CODEX_DOWNGRADE=1)
        controller = await opened(harness, tmp_path, autonomy="workspace")
        assert harness.engine.workspace_write_downgraded
        turn = await finish_turn(controller, "hi")
        notices = [b.text for b in turn.blocks if isinstance(b, NoticeBlock)]
        assert len(notices) == 1 and "Windows 沙箱" in notices[0]
        turn = await finish_turn(controller, "hi again")
        assert not [b for b in turn.blocks if isinstance(b, NoticeBlock)]
        await harness.engine.close_all()

    run(scenario())


def test_attachments_go_as_input_items(tmp_path):
    async def scenario():
        harness = make_engine(tmp_path)
        controller = await opened(harness, tmp_path)
        await harness.engine.list_models()
        png = tiny_png()
        picture = image_attachment("shot.png", png, "image/png")
        notes = text_attachment("notes.md", "# 笔记\nbuy milk")
        assert controller.attachment_problem([picture, notes]) is None
        turn = await finish_turn(controller, "", [picture, notes])
        assert turn.user_block.text == "请查看附件。" and len(turn.user_block.attachments) == 2
        (sent,) = harness.requests("turn/start")
        assert sent["input"][0] == {"type": "text", "text": "请查看附件。", "text_elements": []}
        assert sent["input"][1] == {"type": "image", "url": "data:image/png;base64," + base64.b64encode(png).decode("ascii")}
        assert sent["input"][2]["text"] == '<attachment name="notes.md" type="text/plain">\n# 笔记\nbuy milk\n</attachment>'
        await controller.update_settings(model="gpt-6-luna")
        assert "不支持图片" in controller.attachment_problem([picture])
        assert "PDF" in controller.attachment_problem([pdf_attachment("scan.pdf", b"%PDF-1.4", parsed_text="")])
        assert controller.attachment_problem([pdf_attachment("doc.pdf", b"%PDF-1.4", parsed_text="Invoice 4417")]) is None
        await harness.engine.close_all()

    run(scenario())


def test_late_usage_still_counts_and_the_title_follows_the_thread_name(tmp_path):
    async def scenario():
        harness = make_engine(tmp_path)
        controller = await opened(harness, tmp_path)
        await finish_turn(controller, "[late-usage] hi")
        await until(lambda: controller.usage is not None)
        assert controller.usage.cache_read_tokens == 9000 and controller.context.used == 14_000
        assert controller.title == "[late-usage] hi"
        client = await harness.engine.connection()
        await client.request("thread/name/set", {"threadId": controller.session_id, "name": "Greeting"})
        await until(lambda: controller.title == "Greeting")
        await harness.engine.close_all()

    run(scenario())


def test_closing_a_session_unsubscribes_and_close_all_ends_the_process(tmp_path):
    async def scenario():
        harness = make_engine(tmp_path)
        controller = await opened(harness, tmp_path)
        second = await opened(harness, tmp_path)
        assert harness.engine.find(controller.session_id) is controller
        await controller.close()
        assert controller.state is SessionState.CLOSED and controller not in harness.engine.controllers
        assert harness.engine.find(controller.session_id) is None
        assert harness.requests("thread/unsubscribe") == [{"threadId": controller.session_id}]
        client = harness.clients[0]
        assert client.running
        await harness.engine.close_all()
        assert second.state is SessionState.CLOSED and not client.running and client.exit_code == 0
        assert harness.engine.controllers == []

    run(scenario())


def test_closing_during_a_turn_cancels_it(tmp_path):
    async def scenario():
        harness = make_engine(tmp_path)
        controller = await opened(harness, tmp_path)
        assert controller.send_prompt("[slow] go")
        turn = controller.transcript.turns[-1]
        await until(lambda: "cmd-slow" in turn.tools)
        await controller.close()
        assert controller.state is SessionState.CLOSED and turn.status is TurnStatus.INTERRUPTED
        assert harness.requests("turn/interrupt")
        await harness.engine.close_all()

    run(scenario())


def test_an_open_that_raises_is_not_left_registered(tmp_path):
    async def scenario():
        harness = make_engine(tmp_path)

        async def broken(self):
            raise RuntimeError("boom")

        original = CodexController.open_new
        CodexController.open_new = broken  # type: ignore[method-assign]
        try:
            with pytest.raises(RuntimeError):
                await harness.engine.open_new(tmp_path)
        finally:
            CodexController.open_new = original  # type: ignore[method-assign]
        assert harness.engine.controllers == []
        await harness.engine.close_all()

    run(scenario())


def test_a_thread_the_server_dropped_is_resumed_before_the_retry(tmp_path):
    async def scenario():
        harness = make_engine(tmp_path)
        controller = await opened(harness, tmp_path)
        client = await harness.engine.connection()
        await client.request("thread/unsubscribe", {"threadId": controller.session_id})
        failed = await finish_turn(controller, "first")
        assert failed.status is TurnStatus.FAILED and "thread not loaded" in failed.blocks[-1].message
        retried = await finish_turn(controller, "second")
        assert retried.status is TurnStatus.DONE and retried.blocks[-1].text == "echo: second"
        assert harness.requests("thread/resume")[-1]["threadId"] == controller.session_id
        await harness.engine.close_all()

    run(scenario())


def test_two_sessions_on_one_process_keep_their_events_apart(tmp_path):
    async def scenario():
        harness = make_engine(tmp_path)
        first = await opened(harness, tmp_path)
        second = await opened(harness, tmp_path)
        assert first.send_prompt("one") and second.send_prompt("two")
        await until(lambda: not first.busy and not second.busy)
        assert first.transcript.turns[-1].blocks[-1].text == "echo: one"
        assert second.transcript.turns[-1].blocks[-1].text == "echo: two"
        assert len(harness.clients) == 1
        await harness.engine.close_all()

    run(scenario())


def test_send_prompt_refuses_empty_text_and_a_closed_session(tmp_path):
    async def scenario():
        harness = make_engine(tmp_path)
        controller = await opened(harness, tmp_path)
        assert not controller.send_prompt("   ")
        await controller.close()
        assert not controller.send_prompt("hi")
        assert asyncio.all_tasks() >= {asyncio.current_task()}
        await harness.engine.close_all()

    run(scenario())


# ----------------------------------------------------------------------------------------------- /compact


async def ready_again(controller) -> None:
    await until(lambda: controller.state is SessionState.READY)


def test_compaction_is_codexs_own_and_shows_as_a_turn_with_the_context_marked_compacted(tmp_path):
    async def scenario():
        harness = make_engine(tmp_path)
        controller = await opened(harness, tmp_path)
        await finish_turn(controller, "hello")
        assert controller.start_compaction() is None
        assert controller.state is SessionState.RUNNING and controller.transcript.working_state.value == "compacting_conversation"
        assert "运行" in (controller.start_compaction() or ""), "not while a turn runs"
        await ready_again(controller)
        turn = controller.transcript.turns[-1]
        assert turn.user_block is not None and turn.user_block.text == "/compact" and turn.status is TurnStatus.DONE
        assert any(isinstance(b, NoticeBlock) and "Codex 压缩了上下文" in b.text for b in turn.blocks)
        assert controller.context.compacted is True and controller.context.used is None
        assert [r["threadId"] for r in harness.requests("thread/compact/start")] == [controller.session_id]
        again = await finish_turn(controller, "and again")
        assert again.status is TurnStatus.DONE, "the thread goes on"
        await harness.engine.close_all()

    run(scenario())


def test_a_compaction_codex_refuses_leaves_the_thread_as_it_was_and_says_so(tmp_path):
    async def scenario():
        harness = make_engine(tmp_path)
        controller = await opened(harness, tmp_path)
        await finish_turn(controller, "hello [compact-fail]")
        assert controller.start_compaction() is None
        await ready_again(controller)
        turn = controller.transcript.turns[-1]
        assert turn.status is TurnStatus.FAILED
        assert any(isinstance(b, ErrorBlock) and "没有压缩，对话没有改动" in b.message for b in turn.blocks)
        assert controller.state is SessionState.READY and controller.context.compacted is False
        await harness.engine.close_all()

    run(scenario())


def test_a_stop_ends_a_compaction_that_takes_long(tmp_path):
    async def scenario():
        harness = make_engine(tmp_path)
        controller = await opened(harness, tmp_path)
        await finish_turn(controller, "hello [compact-slow]")
        assert controller.start_compaction() is None
        turn = controller.transcript.turns[-1]
        await until(lambda: controller.transcript.working_state.value == "compacting_conversation")
        await asyncio.sleep(0.3)
        await controller.interrupt()
        await ready_again(controller)
        assert turn.status is TurnStatus.INTERRUPTED
        await harness.engine.close_all()

    run(scenario())
