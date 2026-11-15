"""Automatic sounds use the same published session state as the conversation's status dot."""

import asyncio
from pathlib import Path

import pytest

from desktop.core import app_core
from desktop.core.prompts import QuestionView
from tests.app_support import ready_core
from tests.fake_responses import call, fail, say
from tests.fakes import until
from tests.test_app_core import idle, new_session, sessions_of


@pytest.mark.parametrize("ending", ["complete", "error", "question", "permission"])
def test_notifications_follow_the_published_status_dot(tmp_path, ending):
    async def scenario():
        core, plan, events, platform, _ = await ready_core(tmp_path)
        try:
            cid = await new_session(core, events, tmp_path / "proj")
            controller = core._by_cid[cid].controller
            snapshots = []
            play = platform.play_sound

            def record(path):
                snapshots.append((Path(path).stem, sessions_of(events)[cid]["mark"]))
                play(path)

            platform.play_sound = record
            answer = {
                "complete": say("The entire task is complete."),
                "error": fail("bad_request", "Expected demo failure"),
                "question": call("ask_user", {"questions": [{"question": "Which layout?", "options": ["Compact", "Wide"]}]}),
                "permission": call("create_file", {"file_path": "demo.txt", "content": "Demo"}),
            }[ending]
            if ending == "permission":
                controller.autonomy = "ask"
            plan.script([("hold", "before-notification"), *answer])
            await core.call("session.send", {"id": cid, "text": "Do the demo task."})
            await until(lambda: sessions_of(events)[cid]["mark"] == "running")
            assert snapshots == []
            plan.release("before-notification")
            await until(lambda: snapshots)
            expected = ("question", "attention") if ending in ("question", "permission") else (ending, "ready")
            assert snapshots == [expected], "a sound must not precede the corresponding status-dot update"
        finally:
            await core.shutdown()
    asyncio.run(scenario())


def test_transient_substep_prompts_do_not_ring_while_the_published_dot_keeps_running(tmp_path):
    async def scenario():
        core, plan, events, platform, _ = await ready_core(tmp_path)
        try:
            cid = await new_session(core, events, tmp_path / "proj")
            conv = core._by_cid[cid]
            controller = conv.controller
            plan.script([("hold", "final-answer"), *say("All steps are finished.")])
            await core.call("session.send", {"id": cid, "text": "Complete all demo steps."})
            await until(lambda: sessions_of(events)[cid]["mark"] == "running")
            for index in range(8):
                prompt = controller.transcript.add_question_prompt(
                    f"substep-{index}", (QuestionView(1, "Layout", "Which layout?", ("Compact", "Wide"), False),),
                )
                assert prompt is not None
                controller._changed()
                controller.transcript.answer_question(prompt.id, ["Compact"])
                controller._changed()
                core._flush(conv)
                assert sessions_of(events)[cid]["mark"] == "running"
                assert platform.played_sounds == [], "unpublished intermediate events are not user-visible waits"
            plan.release("final-answer")
            await idle(core, cid)
            await until(lambda: platform.played_sounds)
            assert [Path(path).stem for path in platform.played_sounds] == ["complete"]
            assert sessions_of(events)[cid]["mark"] == "ready"
        finally:
            await core.shutdown()
    asyncio.run(scenario())


def test_a_published_question_and_final_completion_are_each_notified_once_despite_reload(tmp_path):
    async def scenario():
        core, plan, events, platform, _ = await ready_core(tmp_path)
        try:
            cid = await new_session(core, events, tmp_path / "proj")
            conv = core._by_cid[cid]
            controller = conv.controller
            plan.script(
                call("ask_user", {"questions": [{"question": "Which layout?", "options": ["Compact", "Wide"]}]}),
                [("hold", "final-answer"), *say("The whole task is complete.")],
            )
            await core.call("session.send", {"id": cid, "text": "Ask, then finish the demo task."})
            await until(lambda: platform.played_sounds)
            prompt = controller.transcript.pending_prompts()[0]
            assert sessions_of(events)[cid]["mark"] == "attention"
            for _ in range(3):
                await core.call("app.init")
                controller._changed()
                core._flush(conv)
            assert [Path(path).stem for path in platform.played_sounds] == ["question"]
            assert controller.answer_question(prompt.id, ["Compact"])
            await until(lambda: len(plan.requests) == 2)
            core._flush(conv)
            assert sessions_of(events)[cid]["mark"] == "running"
            assert [Path(path).stem for path in platform.played_sounds] == ["question"]
            plan.release("final-answer")
            await until(lambda: len(platform.played_sounds) == 2)
            for _ in range(3):
                await core.call("app.init")
                controller._changed()
                core._flush(conv)
            assert [Path(path).stem for path in platform.played_sounds] == ["question", "complete"]
            assert sessions_of(events)[cid]["mark"] == "ready"
        finally:
            await core.shutdown()
    asyncio.run(scenario())


@pytest.mark.parametrize("followup", ["continue", "mute"])
def test_unpublished_completion_cannot_ring_late_after_continuation_or_muting(tmp_path, monkeypatch, followup):
    async def scenario():
        core, plan, events, platform, _ = await ready_core(tmp_path)
        try:
            cid = await new_session(core, events, tmp_path / "proj")
            conv = core._by_cid[cid]
            monkeypatch.setattr(app_core, "FLUSH_SECONDS", 60)
            if core._flush_handle is not None:
                core._flush_handle.cancel()
                core._flush_handle = None
            plan.script(say("First part finished."), [("hold", "final-answer"), *say("Everything is complete.")])
            await core.call("session.send", {"id": cid, "text": "First part"})
            core._flush(conv)
            assert sessions_of(events)[cid]["mark"] == "running"
            await until(lambda: conv.sound_result == "complete")
            assert platform.played_sounds == []
            if followup == "continue":
                await core.call("session.send", {"id": cid, "text": "Continue the task"})
                await until(lambda: len(plan.requests) == 2)
                core._flush(conv)
                assert sessions_of(events)[cid]["mark"] == "running"
                assert platform.played_sounds == []
                plan.release("final-answer")
                await until(lambda: conv.sound_result == "complete")
                core._flush(conv)
                assert [Path(path).stem for path in platform.played_sounds] == ["complete"]
            else:
                await core.call("settings.set", {"key": "soundEnabled", "value": False})
                core._flush(conv)
                await core.call("settings.set", {"key": "soundEnabled", "value": True})
                core._flush(conv)
                assert platform.played_sounds == [], "unmuting never replays a result consumed while muted"
            assert sessions_of(events)[cid]["mark"] == "ready"
        finally:
            await core.shutdown()
    asyncio.run(scenario())
