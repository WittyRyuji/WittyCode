"""Saved Codex threads: the sidebar list, reopening with history, and the pure history and diff helpers."""

from __future__ import annotations

from datetime import timezone

from desktop.core.codex.engine import cwd_variants, saved_thread
from desktop.core.codex.history import build_turns
from desktop.core.codex.items import file_diffs, parse_unified_diff, split_handoff, turn_input, user_message_parts
from desktop.core.handoff import build_handoff
from desktop.core.attachments import text_attachment
from desktop.core.session_manager import SessionState
from desktop.core.transcript import ActivityBlock, ErrorBlock, TextBlockView, ToolStatus, Transcript, TurnStatus
from tests.codex_support import finish_turn, make_engine, run
from tests.fakes import until


def test_saved_threads_are_listed_and_reopen_with_their_history(tmp_path):
    project = tmp_path / "proj"
    project.mkdir()
    home = tmp_path / "codex-home"

    async def first_run() -> str:
        harness = make_engine(tmp_path, home=home)
        controller = await harness.engine.open_new(project)
        await finish_turn(controller, "[full] fix it")
        await finish_turn(controller, "second")
        thread_id = controller.session_id
        await harness.engine.close_all()
        return thread_id

    async def second_run(thread_id: str) -> None:
        harness = make_engine(tmp_path, home=home)
        engine = harness.engine
        (saved,) = await engine.list_sessions(project)
        assert saved.id == thread_id and saved.title == "[full] fix it" and saved.message_count is None
        assert saved.modified_at.tzinfo is timezone.utc and saved.created_at <= saved.modified_at
        (listed,) = harness.requests("thread/list")
        assert listed["cwd"][0] == str(project.resolve()) and listed["sortKey"] == "updated_at" and listed["limit"] == 50
        controller = await engine.open_saved(project, thread_id, title=saved.title)
        assert controller.state is SessionState.READY and controller.session_id == thread_id
        assert await engine.open_saved(project, thread_id) is controller
        first, second = controller.transcript.turns
        assert first.from_history and first.status is TurnStatus.DONE and first.user_block.text == "[full] fix it"
        assert [type(b) for b in first.blocks][:3] == [type(first.user_block), ActivityBlock, TextBlockView]
        tools = {tool.name: tool for tool in first.tools.values()}
        assert tools["Execute"].status is ToolStatus.DONE and tools["Execute"].result.endswith("[Process exited with code 1]")
        assert tools["Edit"].diffs[0].removed == 1 and tools["Create"].diffs[0].is_new_file
        assert first.blocks[-1].text == "Done: fixed."
        assert second.user_block.text == "second" and second.blocks[-1].text == "echo: second"
        assert first.duration is not None
        assert controller.title == "[full] fix it"
        assert harness.requests("thread/turns/list")[0]["itemsView"] == "full"
        assert harness.requests("thread/resume")[-1] == {"threadId": thread_id, "excludeTurns": True}
        turn = await finish_turn(controller, "third")
        assert turn.blocks[-1].text == "echo: third" and not turn.from_history
        await engine.close_all()

    thread_id = run(first_run())
    run(second_run(thread_id))


def test_a_reopened_thread_keeps_the_time_of_its_last_message(tmp_path):
    home = tmp_path / "codex-home"

    async def first_run() -> str:
        harness = make_engine(tmp_path, home=home)
        controller = await harness.engine.open_new(tmp_path)
        await finish_turn(controller, "hello")
        thread_id = controller.session_id
        await harness.engine.close_all()
        return thread_id

    async def second_run(thread_id: str) -> None:
        harness = make_engine(tmp_path, home=home)
        saved_at = 1_700_000_000.0
        controller = await harness.engine.open_saved(tmp_path, thread_id, modified=saved_at)
        assert controller.state is SessionState.READY
        assert controller.active_at == saved_at, "resuming a thread is not a message"
        assert controller.send_prompt("again")
        assert controller.active_at > saved_at, "sending a message moves it"
        controller.active_at = 1.0
        await until(lambda: controller.state is not SessionState.RUNNING)
        assert controller.active_at > saved_at, "so does the end of its answer"
        await harness.engine.close_all()

    run(second_run(run(first_run())))


def test_a_reopened_handoff_thread_shows_what_was_typed(tmp_path):
    home = tmp_path / "codex-home"
    earlier = Transcript()
    turn = earlier.start_turn("Plan a trip")
    earlier.append_text(turn, "m1", 0, "Where to?")
    earlier.finish_turn(turn, TurnStatus.DONE)
    handoff = build_handoff(earlier.turns)

    async def first_run() -> str:
        harness = make_engine(tmp_path, home=home)
        controller = await harness.engine.open_new(tmp_path, transcript=earlier, handoff=handoff)
        await finish_turn(controller, "Lisbon")
        thread_id = controller.session_id
        await harness.engine.close_all()
        return thread_id

    async def second_run(thread_id: str) -> None:
        harness = make_engine(tmp_path, home=home)
        (saved,) = await harness.engine.list_sessions(tmp_path)
        assert saved.title == "Lisbon", "the handoff text is not the title"
        controller = await harness.engine.open_saved(tmp_path, thread_id)
        (turn,) = controller.transcript.turns
        assert turn.user_block.text == "Lisbon" and turn.user_block.handoff == (2, False)
        assert controller.title == "Lisbon"
        await harness.engine.close_all()

    run(second_run(run(first_run())))


def test_a_thread_without_turns_opens_without_history_and_an_unknown_one_is_dead(tmp_path):
    home = tmp_path / "codex-home"

    async def scenario():
        harness = make_engine(tmp_path, home=home)
        controller = await harness.engine.open_new(tmp_path)
        thread_id = controller.session_id
        await harness.engine.close_all()
        again = make_engine(tmp_path, home=home)
        reopened = await again.engine.open_saved(tmp_path, thread_id)
        assert reopened.state is SessionState.READY and reopened.transcript.turns == []
        assert again.requests("thread/read") == [{"threadId": thread_id, "includeTurns": True}]
        unknown = await again.engine.open_saved(tmp_path, "nope")
        assert unknown.state is SessionState.DEAD
        error = unknown.transcript.turns[-1].blocks[-1]
        assert isinstance(error, ErrorBlock) and error.recoverable and "no rollout found" in error.message
        await again.engine.close_all()

    run(scenario())


def test_history_turns_that_failed_were_stopped_or_were_cut_off():
    transcript = Transcript()
    saved = [
        {"id": "t1", "status": "failed", "startedAt": 100, "completedAt": 140, "durationMs": 40_000,
         "error": {"message": "unexpected status 401", "codexErrorInfo": {"httpConnectionFailed": {"httpStatusCode": 401}}},
         "items": [{"type": "userMessage", "id": "u1", "content": [{"type": "text", "text": "Say hi.", "text_elements": []}]}]},
        {"id": "t2", "status": "interrupted", "startedAt": 150, "completedAt": 151, "durationMs": 1000,
         "items": [{"type": "userMessage", "id": "u2", "content": [{"type": "text", "text": "Again.", "text_elements": []}]},
                   {"type": "commandExecution", "id": "c1", "command": "sleep 9", "cwd": "C:\\p", "status": "inProgress",
                    "aggregatedOutput": None, "exitCode": None},
                   {"type": "reasoning", "id": "r1", "summary": [], "content": ["raw thought"]},
                   {"type": "agentMessage", "id": "a0", "text": "", "phase": None}]},
    ]
    failed, stopped = build_turns(transcript, saved)
    assert failed.status is TurnStatus.FAILED and failed.duration == 40.0
    errors = [b for b in failed.blocks if isinstance(b, ErrorBlock)]
    assert len(errors) == 1 and "登录已失效" in errors[0].message
    assert stopped.status is TurnStatus.INTERRUPTED and stopped.tools["c1"].status is ToolStatus.CANCELLED
    assert stopped.thinking[("r1", 0)].text == "raw thought"
    assert not [b for b in stopped.blocks if isinstance(b, TextBlockView)], "an empty answer leaves no block"
    transcript.add_history_turns([failed, stopped])
    assert transcript.turns == [failed, stopped] and transcript.active_turn is None


def test_a_saved_turn_that_was_steered_shows_the_steer_after_the_message_that_began_it():
    """Real Codex 0.166.0 saves ``turn/steer`` input as a second ``userMessage`` of the turn (measured)."""
    from desktop.core.transcript import UserBlock

    transcript = Transcript()
    saved = [
        {"id": "t1", "status": "completed", "startedAt": 100, "completedAt": 130, "durationMs": 30_000,
         "items": [{"type": "userMessage", "id": "u1", "content": [{"type": "text", "text": "Run ping.", "text_elements": []}]},
                   {"type": "userMessage", "id": "u2", "content": [{"type": "text", "text": "Say BRAVO.", "text_elements": []},
                                                                    {"type": "image", "url": "data:image/png;base64,iVBORw0KGgo="}]},
                   {"type": "userMessage", "id": "u3", "content": []},
                   {"type": "agentMessage", "id": "a1", "text": "BRAVO", "phase": "final_answer"}]},
    ]
    (turn,) = build_turns(transcript, saved)
    users = [b for b in turn.blocks if isinstance(b, UserBlock)]
    assert [(b.text, b.steer, b.pending) for b in users] == [("Run ping.", False, False), ("Say BRAVO.", True, False)], "an empty one is not a message"
    assert len(users[1].attachments) == 1 and users[1].message_id == "u2"
    assert turn.user_block is users[0]
    assert [type(b).__name__ for b in turn.blocks] == ["UserBlock", "UserBlock", "TextBlockView"]


def test_saved_user_messages_give_back_text_pictures_and_files():
    picture = turn_input("look", [text_attachment("notes.md", "body")])
    content = [*picture, {"type": "image", "url": "data:image/png;base64,iVBORw0KGgo="}, {"type": "localImage", "path": r"C:\tmp\shot.png"}]
    text, attachments, handoff = user_message_parts({"type": "userMessage", "id": "u", "content": content})
    assert text == "look" and handoff is None
    assert [(a.kind, a.name) for a in attachments] == [("text", "notes.md"), ("image", "图片 1"), ("image", "shot.png")]
    assert attachments[1].data.startswith(b"\x89PNG")


def test_split_handoff_undoes_build_handoff():
    transcript = Transcript()
    for question, answer in (("one?", "1"), ("two?", "2")):
        turn = transcript.start_turn(question)
        transcript.append_text(turn, question, 0, answer)
        transcript.finish_turn(turn, TurnStatus.DONE)
    full = build_handoff(transcript.turns)
    assert split_handoff(full.prefix + "typed\nmore") == ("typed\nmore", (4, False))
    partial = build_handoff(transcript.turns, max_messages=1)
    assert split_handoff(partial.prefix + "x") == ("x", (1, True))
    assert split_handoff("plain text") == ("plain text", None)


def test_unified_diffs_keep_their_line_numbers():
    diff = parse_unified_diff(
        "a.py",
        "diff --git a/a.py b/a.py\nindex 1..2 100644\n--- a/a.py\n+++ b/a.py\n@@ -1,2 +1,2 @@\n-x = 1\n+x = 2\n y\n"
        "@@ -10,1 +10,2 @@\n z\n+--- not a header\n\\ No newline at end of file\n",
    )
    assert (diff.added, diff.removed) == (2, 1)
    assert [(l.kind, l.text, l.old, l.new) for l in diff.lines] == [
        ("del", "x = 1", 1, None),
        ("add", "x = 2", None, 1),
        ("ctx", "y", 2, 2),
        ("gap", "", None, None),
        ("ctx", "z", 10, 10),
        ("add", "--- not a header", None, 11),
    ]


def test_file_changes_of_every_kind():
    changes = [
        {"path": "new.txt", "kind": {"type": "add"}, "diff": "a\nb\n"},
        {"path": "old.txt", "kind": {"type": "delete"}, "diff": "gone\n"},
        {"path": "a.txt", "kind": {"type": "update", "move_path": "b.txt"}, "diff": "@@ -1 +1 @@\n-a\n+b\n"},
        {"path": "plain.txt", "kind": "update", "diff": "@@ -3 +3 @@\n-c\n+d\n"},
    ]
    added, deleted, moved, plain = file_diffs(changes)
    assert added.is_new_file and added.added == 2 and added.path == "new.txt"
    assert deleted.removed == 1 and deleted.lines[0].kind == "del"
    assert moved.path == "b.txt" and (moved.added, moved.removed) == (1, 1)
    assert plain.lines[0].old == 3


def test_saved_thread_rows_and_cwd_variants():
    row = saved_thread({"id": "t", "name": None, "preview": "first line\nsecond", "createdAt": 1_700_000_000, "updatedAt": 1_700_000_500, "cwd": "D:\\p"})
    assert (row.title, row.cwd) == ("first line", "D:\\p")
    assert row.modified_at.timestamp() == 1_700_000_500
    assert saved_thread({"id": "t", "name": "Named", "preview": "x", "createdAt": 1, "updatedAt": 2}).title == "Named"
    assert cwd_variants(r"D:\Work\x") == [r"D:\Work\x", r"d:\Work\x"]
    assert cwd_variants("/home/me") == ["/home/me"]
