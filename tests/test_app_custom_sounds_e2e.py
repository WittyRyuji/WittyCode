"""Selected sounds must reach the real player for previews and already-running conversation notifications."""

from __future__ import annotations

import math
from pathlib import Path
import struct
import wave

import pytest
from PySide6.QtCore import Qt
from PySide6.QtMultimedia import QSoundEffect

from tests.e2e_support import open_rig
from tests.fake_responses import call, fail, say
from tests.test_app_e2e import new_conversation
from tests.ui_support import run, wait_for

pytestmark = pytest.mark.ui


def custom_wav(path):
    frames = b"".join(struct.pack("<hh", sample, sample) for index in range(8820)
        for sample in [round(5000 * math.sin(index * 2 * math.pi * 660 / 44100))])
    with wave.open(str(path), "wb") as audio:
        audio.setparams((2, 2, 44100, 0, "NONE", "not compressed"))
        audio.writeframes(frames)


@pytest.mark.parametrize("kind,label", [("question", "问题"), ("complete", "完成"), ("error", "错误")])
def test_selecting_during_a_turn_changes_both_preview_and_automatic_notification(qapp, loop, tmp_path, monkeypatch, kind, label):
    async def scenario():
        rig = await open_rig(tmp_path)
        try:
            played = []
            play = QSoundEffect.play

            def record(effect):
                played.append(Path(effect.source().toLocalFile()))
                play(effect)  # The shared Qt fixture mutes real playback, not the notification code.

            monkeypatch.setattr(QSoundEffect, "play", record)
            model = await new_conversation(rig)
            answer = {
                "question": call("ask_user", {"questions": [{"question": "Which layout?", "options": ["Compact", "Wide"]}]}),
                "complete": say("The entire demo task is complete."),
                "error": fail("bad_request", "Expected demo failure"),
            }[kind]
            model.next(answer)
            await rig.send("Wait, then finish the demo task.")
            await rig.wait_for(".tab.current .dot-running")
            custom = tmp_path / f"custom-{kind}.wav"
            custom_wav(custom)

            async def choose(_start):
                return str(custom)

            monkeypatch.setattr(rig.window.platform, "pick_sound", choose)
            await rig.press(",", Qt.KeyboardModifier.ControlModifier)
            await rig.click_text(".snav", "会话")
            await rig.click(f'[aria-label="选择{label}提示音"]')
            await wait_for(lambda: getattr(rig.core.settings, "sound_" + kind) == str(custom))
            assert await rig.js(f"document.querySelector('#sound-{kind}').value") == str(custom)
            await rig.click(f'[aria-label="试听{label}提示音"]')
            await wait_for(lambda: len(played) == 1)
            assert played == [custom]
            player = rig.window.platform._sound
            await wait_for(lambda: player.status() is QSoundEffect.Status.Ready and not player.isPlaying())
            await rig.press("Escape")
            model.go()
            await wait_for(lambda: len(played) == 2)
            assert played == [custom, custom], "the live notification must use the same chosen file as the preview"
            assert Path(player.source().toLocalFile()) == custom
            await wait_for(lambda: player.status() is QSoundEffect.Status.Ready)
            assert player.isMuted()
        finally:
            await rig.finish()

    run(loop, scenario())
