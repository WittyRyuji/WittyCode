"""Significant session activity and three configurable, non-replayed notification sounds."""

from __future__ import annotations

import asyncio
from pathlib import Path
import shutil
import wave

import pytest

from desktop.core import sounds
from desktop.core.app_core import AppError
from desktop.core.settings import settings_from_dict, settings_to_dict
from tests.app_support import ready_core
from tests.fake_responses import call, fail, say
from tests.fakes import until
from tests.test_app_core import idle, new_session, three_saved


def test_three_concurrent_streams_stay_put_but_each_final_result_promotes_within_its_project(tmp_path):
    async def scenario():
        core, plan, events, platform, _ = await ready_core(tmp_path, projects=False)
        try:
            project, _ = await three_saved(core, events, tmp_path)
            ids = []
            for native in ("old", "mid", "new"):
                cid = (await core.call("session.openSaved", {"id": "cg:" + native, "projectId": str(project)}))["id"]
                await idle(core, cid)
                ids.append(cid)
                plan.script([("hold", native), *say("Final " + native)])
                before = len(plan.requests)
                await core.call("session.send", {"id": cid, "text": "Continue " + native})
                await until(lambda: len(plan.requests) > before)
                assert core.project_rows()[0]["sessionIds"][0] == cid
            expected = list(reversed(ids))
            other = tmp_path / "other"
            other.mkdir()
            await core.call("project.add", {"path": str(other)})
            other_cid = await new_session(core, events, other)
            for _ in range(3):
                for cid in ids:
                    controller = core._by_cid[cid].controller
                    controller.active_at = 1  # force the displayed-age refresh path too
                    turn = controller.transcript.turns[-1]
                    controller.transcript.append_thinking(turn, "thought", 0, "Thinking")
                    controller.transcript.append_text(turn, "progress", 0, "Working")
                    controller._changed()
                    core._emit_sidebar()
                    assert core.project_rows()[0]["sessionIds"] == expected
                    assert core.project_rows()[1]["sessionIds"] == [other_cid]
            for native, cid in zip(("old", "mid", "new"), ids):
                plan.release(native)
                await idle(core, cid)
                expected.remove(cid)
                expected.insert(0, cid)
                assert core.project_rows()[0]["sessionIds"] == expected
            await until(lambda: len(platform.played_sounds) == 3)
            assert [Path(p).stem for p in platform.played_sounds] == ["complete"] * 3
            order = list(core.settings.session_order)
            await core.call("app.init")
            assert core.settings.session_order == order
            assert len(platform.played_sounds) == 3, "page reload does not replay completed notifications"
        finally:
            await core.shutdown()

    asyncio.run(scenario())


def test_new_question_and_permission_each_sound_once_and_final_failure_is_not_completion(tmp_path):
    async def scenario():
        core, plan, events, platform, _ = await ready_core(tmp_path)
        try:
            cid = await new_session(core, events, tmp_path / "proj")
            controller = core._by_cid[cid].controller
            plan.script(call("ask_user", {"questions": [{"question": "Which storage?", "options": ["Files", "SQLite"]}]}), say("Done"))
            await core.call("session.send", {"id": cid, "text": "Ask first"})
            await until(lambda: controller.transcript.pending_prompts())
            prompt = controller.transcript.pending_prompts()[0]
            for _ in range(5):
                controller._changed()
                await core.call("app.init")
            assert [Path(p).stem for p in platform.played_sounds] == ["question"]
            controller.answer_question(prompt.id, ["Files"])
            await idle(core, cid)
            await until(lambda: len(platform.played_sounds) == 2)
            assert [Path(p).stem for p in platform.played_sounds] == ["question", "complete"]
            plan.script(call("create_file", {"file_path": "demo.txt", "content": "demo"}), say("Saved"))
            controller.autonomy = "ask"
            await core.call("session.send", {"id": cid, "text": "Create file"})
            await until(lambda: controller.transcript.pending_prompts())
            prompt = controller.transcript.pending_prompts()[0]
            controller._changed()
            assert [Path(p).stem for p in platform.played_sounds][-1] == "question"
            controller.answer_permission(prompt.id, "cancel")
            await idle(core, cid)
            await until(lambda: len(platform.played_sounds) == 4)
            plan.script(fail("bad_request", "An expected test failure"))
            await core.call("session.send", {"id": cid, "text": "Fail this turn"})
            await idle(core, cid)
            controller._changed()
            assert [Path(p).stem for p in platform.played_sounds] == ["question", "complete", "question", "complete", "error"]
        finally:
            await core.shutdown()

    asyncio.run(scenario())


def test_sound_settings_resolve_defaults_pick_reset_validate_and_round_trip(tmp_path):
    async def scenario():
        core, _, _, platform, _ = await ready_core(tmp_path)
        try:
            view = core.settings_view()
            assert view["soundEnabled"]
            for kind in sounds.KINDS:
                key = "sound" + kind.title()
                assert view[key] == sounds.sound_path(kind) and Path(view[key]).is_file()
                await core.call("sounds.preview", {"kind": kind})
            assert len(platform.played_sounds) == 3
            custom = tmp_path / "my-chime.wav"
            shutil.copyfile(sounds.sound_path("complete"), custom)
            platform.sound = str(custom)
            picked = await core.call("sounds.pick", {"kind": "question"})
            assert picked["soundQuestion"] == str(custom) and core.settings.sound_question == str(custom)
            assert settings_from_dict(settings_to_dict(core.settings)) == core.settings
            platform.sound = None
            assert (await core.call("sounds.pick", {"kind": "question"}))["soundQuestion"] == str(custom), "cancel preserves selection"
            reset = await core.call("settings.set", {"key": "soundQuestion", "value": ""})
            assert reset["soundQuestion"] == sounds.sound_path("question") and core.settings.sound_question == ""
            await core.call("settings.set", {"key": "soundEnabled", "value": False})
            assert platform.sound_stops == 1, "the master switch also stops a sound already playing"
            for kind in sounds.KINDS:
                core._play_notification(kind)
                assert await core.call("sounds.preview", {"kind": kind}) == {"ok": False}
            assert len(platform.played_sounds) == 3
            for value in (False, [], "relative.wav", str(tmp_path / "missing.wav"), r"\\server\share\chime.wav", "https://example.com/chime.wav"):
                with pytest.raises(AppError):
                    await core.call("settings.set", {"key": "soundComplete", "value": value})
            for method in ("sounds.pick", "sounds.preview"):
                for kind in ("other", [], None):
                    with pytest.raises(AppError):
                        await core.call(method, {"kind": kind})
        finally:
            await core.shutdown()

    asyncio.run(scenario())


def test_stopping_and_reading_history_are_silent_and_removed_custom_file_never_falls_back(tmp_path):
    async def scenario():
        core, plan, events, platform, _ = await ready_core(tmp_path, projects=False)
        try:
            project, _ = await three_saved(core, events, tmp_path)
            cid = (await core.call("session.openSaved", {"id": "cg:old", "projectId": str(project)}))["id"]
            await idle(core, cid)
            assert platform.played_sounds == []
            plan.script([("hold", "stopped"), *say("Unused")])
            await core.call("session.send", {"id": cid, "text": "Stop me"})
            await until(lambda: plan.requests)
            await core._by_cid[cid].controller.interrupt()
            plan.release("stopped")
            await idle(core, cid)
            assert platform.played_sounds == []
            core.settings.sound_complete = str(tmp_path / "deleted.wav")
            core._play_notification("complete")
            assert platform.played_sounds == [] and "提示音无法播放" in events.last("notice")["text"]
        finally:
            await core.shutdown()

    asyncio.run(scenario())


def test_muting_during_preview_validation_prevents_late_playback(tmp_path, monkeypatch):
    import threading

    async def scenario():
        core, _, _, platform, _ = await ready_core(tmp_path)
        entered, release = threading.Event(), threading.Event()
        validate = sounds.validate_path

        def slow(path):
            entered.set()
            assert release.wait(5)
            return validate(path)

        monkeypatch.setattr(sounds, "validate_path", slow)
        task = asyncio.create_task(core.call("sounds.preview", {"kind": "complete"}))
        try:
            await until(entered.is_set)
            await core.call("settings.set", {"key": "soundEnabled", "value": False})
            release.set()
            assert await task == {"ok": False}
            assert platform.played_sounds == [] and platform.sound_stops == 1
        finally:
            release.set()
            await task
            await core.shutdown()

    asyncio.run(scenario())


def test_bundled_sounds_are_distinct_short_pcm_wavs_and_corrupt_files_are_rejected(tmp_path):
    contents = []
    for kind in sounds.KINDS:
        path = sounds.validate_path(sounds.sound_path(kind))
        with wave.open(path, "rb") as audio:
            assert audio.getnchannels() == 1 and audio.getsampwidth() == 2
            assert 0.25 < audio.getnframes() / audio.getframerate() < 1
            data = audio.readframes(audio.getnframes())
            assert any(data)
            contents.append(data)
    assert len(set(contents)) == 3
    broken = tmp_path / "broken.wav"
    broken.write_bytes(Path(sounds.sound_path("question")).read_bytes()[:-20])
    with pytest.raises(sounds.SoundProblem):
        sounds.validate_path(str(broken))
    assert settings_from_dict({"sound_enabled": "yes", "sound_question": []}).sound_enabled is True
    assert settings_from_dict({"sound_question": []}).sound_question == ""
