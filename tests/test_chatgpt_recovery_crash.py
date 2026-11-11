"""Opening a ChatGPT Plan conversation that the application left in the middle of a turn.

A crash (or the process being killed, or the power going) does not run the clean-up a stop runs, so the file on disk is
whatever the conversation's delayed save wrote last: a turn that never ended, perhaps a tool call without its result. These
tests make that state the way it really comes about (the controller's own delayed save writes the file while a call waits
or runs; the file is kept as it is at that moment; everything is shut down the clean way; the kept file is put back, which
is what the disk holds after a crash) and open it with a fresh engine. What must hold: the history the next request sends
is one the Responses API accepts (every call has exactly one output after it), the turn shows as cut off rather than
finished, nothing is run again, and the model is told that the result is unknown.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Callable
from pathlib import Path
from typing import Any

from desktop.core.chatgpt import controller as controller_module
from desktop.core.harness import toolset
from desktop.core.harness.loop import DECLINED
from desktop.core.harness.store import LOST
from desktop.core.transcript import ToolStatus, TurnStatus
from tests.chatgpt_support import ACCESS_TOKEN, finish_turn, make_engine, until
from tests.fake_responses import FakeResponses, call, cut, say

FAST_SAVE = 0.05


def run(coroutine: Any) -> Any:
    return asyncio.run(coroutine)


async def opened(tmp_path: Path, fake: FakeResponses, *, autonomy: str = "code"):
    engine = make_engine(tmp_path, fake)
    folder = tmp_path / "project"
    folder.mkdir(exist_ok=True)
    controller = await engine.open_new(folder, model="gpt-6.1-sol", reasoning="medium", autonomy=autonomy)
    return engine, controller


def session_file(tmp_path: Path, session_id: str) -> Path:
    return tmp_path / "chatgpt" / "sessions" / f"{session_id}.json"


def on_disk(tmp_path: Path, session_id: str) -> dict[str, Any]:
    return json.loads(session_file(tmp_path, session_id).read_text(encoding="utf-8"))


def unpaired(items: list[dict[str, Any]]) -> list[str]:
    """The call ids that break "every function_call has exactly one function_call_output after it"."""
    waiting: list[str] = []
    wrong: list[str] = []
    for item in items:
        if item.get("type") == "function_call":
            waiting.append(item["call_id"])
        elif item.get("type") == "function_call_output":
            if item["call_id"] in waiting:
                waiting.remove(item["call_id"])
            else:
                wrong.append(item["call_id"])
    return waiting + wrong


def outputs(items: list[dict[str, Any]]) -> dict[str, str]:
    return {item["call_id"]: item["output"] for item in items if item.get("type") == "function_call_output"}


def has_call(data: dict[str, Any]) -> bool:
    return any(item.get("type") == "function_call" for item in data["items"])


def notices(turn: Any) -> list[str]:
    return [block.text for block in turn.blocks if block.kind == "notice"]


async def crash_after_save(tmp_path: Path, engine: Any, controller: Any, ready: Callable[[dict[str, Any]], bool]) -> str:
    """Wait for the conversation's own delayed save to write a file for which ``ready`` holds, keep that file (what a crash
    at this moment leaves on disk), close everything the clean way, and put the kept file back. Returns the session id."""
    path = session_file(tmp_path, controller.session_id)

    def written() -> bool:
        try:
            return ready(json.loads(path.read_text(encoding="utf-8")))
        except (OSError, ValueError):
            return False

    await until(written, message="the delayed save")
    kept = path.read_bytes()
    await engine.close_all()
    assert path.read_bytes() != kept, "the clean close wrote the end of the turn, which a crash does not"
    path.write_bytes(kept)
    return controller.session_id


def check_unknown_result(text: str) -> None:
    for words in ("unknown", "closed or crashed", "may or may not have run", "check the current state"):
        assert words in text, f"the model is told: {words!r}"


# ----------------------------------------------------------------------------------------------- crash states


def test_a_call_that_waited_for_approval_when_the_app_died_gets_an_unknown_result_and_its_turn_shows_as_cut_off(tmp_path, monkeypatch):
    monkeypatch.setattr(controller_module, "SAVE_DELAY_SECONDS", FAST_SAVE)

    async def go() -> None:
        with FakeResponses(token=ACCESS_TOKEN) as fake:
            fake.script(call("shell", {"command": "New-Item marker.txt"}, call_id="c1", text="I will create it."))
            engine, controller = await opened(tmp_path, fake, autonomy="ask")
            controller.send_prompt("create marker.txt")
            await until(lambda: controller.transcript.pending_prompts(), message="the question about the command")
            session_id = await crash_after_save(tmp_path, engine, controller, has_call)

            again = make_engine(tmp_path, fake)
            reopened = await again.open_saved(tmp_path / "project", session_id)
            assert unpaired(reopened.saved.items) == []
            lost = outputs(reopened.saved.items)["c1"]
            check_unknown_result(lost)
            (turn,) = reopened.transcript.turns
            assert turn.status is TurnStatus.INTERRUPTED, "cut off, not finished"
            (notice,) = notices(turn)
            assert "关闭" in notice and "1 个" in notice
            assert [b.text for b in turn.blocks if b.kind == "text"] == ["I will create it."]
            (tool,) = turn.tools.values()
            assert tool.status is ToolStatus.CANCELLED and tool.result == lost
            data = on_disk(tmp_path, session_id)
            assert unpaired(data["items"]) == [] and data["turns"][0]["status"] == "interrupted", "the repair is saved"

            fake.script(say("Nothing was created yet; shall I try again?"))
            reopened.send_prompt("did it work?")
            await finish_turn(reopened)
            sent = fake.requests[-1]["input"]
            assert unpaired(sent) == [] and outputs(sent)["c1"] == lost
            assert not (tmp_path / "project" / "marker.txt").exists(), "nothing was run again"
            await again.close_all()

    run(go())


def test_a_command_that_had_already_changed_something_is_not_run_again_and_the_model_is_told_to_look_first(tmp_path, monkeypatch):
    monkeypatch.setattr(controller_module, "SAVE_DELAY_SECONDS", FAST_SAVE)
    real_run = toolset.HarnessTools.run
    ran: list[str] = []

    async def a_long_command(self: Any, call_: Any) -> Any:
        if call_.name != "shell":
            return await real_run(self, call_)
        ran.append(call_.call_id)
        (self.context.project / "started.txt").write_text("the job began", encoding="utf-8")
        await asyncio.Event().wait()  # (still running when the application dies)

    monkeypatch.setattr(toolset.HarnessTools, "run", a_long_command)

    async def go() -> None:
        with FakeResponses(token=ACCESS_TOKEN) as fake:
            fake.script(call("shell", {"command": "New-Item started.txt; Start-Sleep 600"}, call_id="c1"))
            engine, controller = await opened(tmp_path, fake, autonomy="full")
            controller.send_prompt("start the job")
            await until(lambda: ran, message="the command to start")
            session_id = await crash_after_save(tmp_path, engine, controller, has_call)
            started = tmp_path / "project" / "started.txt"
            assert started.exists()
            started.unlink()

            again = make_engine(tmp_path, fake)
            reopened = await again.open_saved(tmp_path / "project", session_id)
            (turn,) = reopened.transcript.turns
            assert turn.status is TurnStatus.INTERRUPTED
            assert next(iter(turn.tools.values())).status is ToolStatus.CANCELLED, "not shown as a success"
            fake.script(say("started.txt is not there, so I cannot tell; shall I run it again?"))
            reopened.send_prompt("did the job start?")
            await finish_turn(reopened)
            assert ran == ["c1"], "the command ran once: not again on opening, not in the next turn"
            assert not started.exists()
            sent = fake.requests[-1]["input"]
            assert unpaired(sent) == []
            check_unknown_result(outputs(sent)["c1"])
            await again.close_all()

    run(go())


def test_a_turn_cut_off_between_two_requests_shows_as_cut_off_and_its_valid_history_is_left_as_it_was(tmp_path, monkeypatch):
    monkeypatch.setattr(controller_module, "SAVE_DELAY_SECONDS", FAST_SAVE)

    async def go() -> None:
        (tmp_path / "project").mkdir()
        (tmp_path / "project" / "notes.txt").write_text("alpha\n", encoding="utf-8")
        with FakeResponses(token=ACCESS_TOKEN) as fake:
            fake.script(call("read_file", {"file_path": "notes.txt"}, call_id="c1"), [("hold", "stuck"), *say("never")])
            engine, controller = await opened(tmp_path, fake)
            controller.send_prompt("what is in notes.txt?")
            await until(lambda: len(fake.requests) == 2, message="the second request")
            session_id = await crash_after_save(tmp_path, engine, controller, lambda data: "alpha" in outputs(data["items"]).get("c1", ""))
            fake.release("stuck")
            kept = on_disk(tmp_path, session_id)["items"]

            again = make_engine(tmp_path, fake)
            reopened = await again.open_saved(tmp_path / "project", session_id)
            assert reopened.saved.items == kept, "a valid history is not touched"
            assert "alpha" in outputs(reopened.saved.items)["c1"]
            (turn,) = reopened.transcript.turns
            assert turn.status is TurnStatus.INTERRUPTED, "no answer came: the turn did not finish"
            (notice,) = notices(turn)
            assert "没有做完" in notice and "工具" not in notice, "no tool lost its result"
            assert next(iter(turn.tools.values())).status is ToolStatus.DONE, "the call that had finished keeps its result"
            assert on_disk(tmp_path, session_id)["turns"][0]["status"] == "interrupted"
            await again.close_all()

    run(go())


# ----------------------------------------------------------------------------------------------- what the saves write


def test_while_a_call_waits_the_file_is_a_valid_history_and_the_turn_is_marked_running(tmp_path, monkeypatch):
    monkeypatch.setattr(controller_module, "SAVE_DELAY_SECONDS", FAST_SAVE)

    async def go() -> None:
        with FakeResponses(token=ACCESS_TOKEN) as fake:
            fake.script(call("shell", {"command": "New-Item marker.txt"}, call_id="c1"), say("understood"))
            engine, controller = await opened(tmp_path, fake, autonomy="ask")
            controller.send_prompt("create marker.txt")
            await until(lambda: controller.transcript.pending_prompts(), message="the question about the command")
            path = session_file(tmp_path, controller.session_id)
            await until(lambda: path.exists() and has_call(json.loads(path.read_text(encoding="utf-8"))), message="the delayed save")
            data = on_disk(tmp_path, controller.session_id)
            assert unpaired(data["items"]) == [] and outputs(data["items"]) == {"c1": LOST}
            assert (data["turns"][0]["status"], data["turns"][0]["finished"]) == ("running", None)
            assert outputs(controller.saved.items) == {}, "the history the loop works on is not touched"

            controller.answer_permission(controller.transcript.pending_prompts()[0].id, "cancel")
            await finish_turn(controller)
            data = on_disk(tmp_path, controller.session_id)
            assert unpaired(data["items"]) == [] and outputs(data["items"]) == {"c1": DECLINED}, "the real output replaces it"
            assert data["turns"][0]["status"] == "done"
            assert unpaired(fake.requests[1]["input"]) == [] and outputs(fake.requests[1]["input"]) == {"c1": DECLINED}
            await engine.close_all()

    run(go())


def test_a_call_that_may_change_something_is_on_disk_before_it_runs_but_a_read_costs_no_save(tmp_path, monkeypatch):
    monkeypatch.setattr(controller_module, "SAVE_DELAY_SECONDS", 3600.0)  # (the delayed save does not come in this test)
    real_run = toolset.HarnessTools.run
    seen: dict[str, Any] = {}

    async def watched(self: Any, call_: Any) -> Any:
        path = session_file(tmp_path, seen["session"])
        seen[call_.name] = json.loads(path.read_text(encoding="utf-8")) if path.exists() else None
        if call_.name != "shell":
            return await real_run(self, call_)
        await asyncio.Event().wait()

    monkeypatch.setattr(toolset.HarnessTools, "run", watched)

    async def go() -> None:
        (tmp_path / "project").mkdir()
        (tmp_path / "project" / "notes.txt").write_text("alpha\n", encoding="utf-8")
        with FakeResponses(token=ACCESS_TOKEN) as fake:
            fake.script(call("read_file", {"file_path": "notes.txt"}, call_id="c0"), call("shell", {"command": "Remove-Item notes.txt"}, call_id="c1"))
            engine, controller = await opened(tmp_path, fake, autonomy="full")
            seen["session"] = controller.session_id
            controller.send_prompt("read it, then remove it")
            await until(lambda: "shell" in seen, message="the command to start")
            assert seen["read_file"] is None, "no save before a read"
            data = seen["shell"]
            assert data is not None, "saved before the command ran"
            assert unpaired(data["items"]) == [] and "alpha" in outputs(data["items"])["c0"] and outputs(data["items"])["c1"] == LOST
            assert data["turns"][0]["status"] == "running"
            await engine.close_all()

    run(go())


# ----------------------------------------------------------------------------------------------- files of the earlier version


def write_session(tmp_path: Path, session_id: str, *, items: list[dict[str, Any]], turns: list[dict[str, Any]], tools: dict[str, Any] | None = None) -> Path:
    """A session file in the shape the earlier version wrote (no ``note``, a running turn saved as ``done`` with no end)."""
    (tmp_path / "project").mkdir(exist_ok=True)
    data = {
        "version": 1,
        "id": session_id,
        "project": str(tmp_path / "project"),
        "title": "older",
        "model": "gpt-6.1-sol",
        "reasoning": "medium",
        "autonomy": "code",
        "created_at": 100.0,
        "modified_at": 160.0,
        "turns": turns,
        "tools": tools or {},
        "usage": {"input": 10, "output": 5, "cacheRead": 0},
        "context_used": 15,
        "recalled": [],
        "items": items,
    }
    path = session_file(tmp_path, session_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data), encoding="utf-8")
    return path


USER = {"type": "message", "role": "user", "content": [{"type": "input_text", "text": "list the folder"}]}
ANSWER = {"type": "message", "role": "assistant", "content": [{"type": "output_text", "text": "Listing it."}]}
CALL = {"type": "function_call", "call_id": "c1", "name": "list_dir", "arguments": "{}"}


def test_a_file_the_earlier_version_left_after_a_crash_is_repaired_when_it_is_opened(tmp_path):
    async def go() -> None:
        path = write_session(tmp_path, "older1", items=[USER, ANSWER, CALL], turns=[{"start": 0, "at": 100.0, "finished": None, "status": "done", "error": "", "model": "gpt-6.1-sol"}])
        with FakeResponses(token=ACCESS_TOKEN) as fake:
            engine = make_engine(tmp_path, fake)
            reopened = await engine.open_saved(tmp_path / "project", "older1")
            assert unpaired(reopened.saved.items) == []
            check_unknown_result(outputs(reopened.saved.items)["c1"])
            (turn,) = reopened.transcript.turns
            assert turn.status is TurnStatus.INTERRUPTED and notices(turn)
            assert next(iter(turn.tools.values())).status is ToolStatus.CANCELLED
            assert turn.duration == 60.0, "until the last save"
            data = json.loads(path.read_text(encoding="utf-8"))
            assert data["turns"][0]["status"] == "interrupted" and unpaired(data["items"]) == []
            fake.script(say("ok"))
            reopened.send_prompt("again")
            await finish_turn(reopened)
            assert unpaired(fake.requests[-1]["input"]) == []
            await engine.close_all()

    run(go())


def test_a_finished_file_of_the_earlier_version_opens_as_before_and_is_not_rewritten(tmp_path):
    async def go() -> None:
        items = [USER, CALL, {"type": "function_call_output", "call_id": "c1", "output": "a.txt"}, ANSWER]
        path = write_session(tmp_path, "older2", items=items, turns=[{"start": 0, "at": 100.0, "finished": 130.0, "status": "done", "error": "", "model": "gpt-6.1-sol"}], tools={"c1": {"status": "done", "diffs": []}})
        before = path.read_bytes()
        engine = make_engine(tmp_path, None)
        reopened = await engine.open_saved(tmp_path / "project", "older2")
        (turn,) = reopened.transcript.turns
        assert turn.status is TurnStatus.DONE and notices(turn) == [] and turn.duration == 30.0
        assert next(iter(turn.tools.values())).status is ToolStatus.DONE
        assert reopened.saved.items == items
        assert path.read_bytes() == before, "nothing to repair, nothing written"
        await engine.close_all()

    run(go())


# ----------------------------------------------------------------------------------------------- turns that ended early


def test_a_turn_that_ran_out_of_steps_is_saved_as_such_and_says_so_again_when_reopened(tmp_path, monkeypatch):
    real = controller_module.AgentLoop
    monkeypatch.setattr(controller_module, "AgentLoop", lambda *args, **kwargs: real(*args, max_steps=2, **kwargs))

    async def go() -> None:
        with FakeResponses(token=ACCESS_TOKEN) as fake:
            where = str(tmp_path / "project")
            fake.script(*[call("list_dir", {"directory_path": where}, call_id=f"c{i}") for i in range(3)])
            engine, controller = await opened(tmp_path, fake)
            controller.send_prompt("keep going")
            await finish_turn(controller)
            live = [b.text for b in controller.transcript.turns[0].blocks if b.kind == "notice"]
            saved = engine.sessions.load(controller.session_id)
            assert saved is not None and saved.turns[0].status == "limit"
            session_id = controller.session_id
            await engine.close_all()

            again = make_engine(tmp_path, fake)
            reopened = await again.open_saved(tmp_path / "project", session_id)
            (turn,) = reopened.transcript.turns
            assert turn.status is TurnStatus.DONE
            assert notices(turn) == live and "2 步" in live[0]
            await again.close_all()

    run(go())


def test_an_answer_that_was_cut_short_is_saved_as_incomplete_and_says_so_again_when_reopened(tmp_path):
    async def go() -> None:
        with FakeResponses(token=ACCESS_TOKEN) as fake:
            fake.script(cut("The first half of the answer"))
            engine, controller = await opened(tmp_path, fake)
            controller.send_prompt("write a lot")
            await finish_turn(controller)
            saved = engine.sessions.load(controller.session_id)
            assert saved is not None and saved.turns[0].status == "incomplete"
            session_id = controller.session_id
            await engine.close_all()

            again = make_engine(tmp_path, fake)
            reopened = await again.open_saved(tmp_path / "project", session_id)
            (turn,) = reopened.transcript.turns
            assert turn.status is TurnStatus.DONE
            assert notices(turn) == ["回答太长，被截断了；可以回复「继续」。"]
            await again.close_all()

    run(go())
