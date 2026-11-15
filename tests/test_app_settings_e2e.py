"""Real Qt/React settings: notification file selection, model order and aligned program forms."""

from __future__ import annotations

import json
import shutil

import pytest
from PySide6.QtCore import Qt

from desktop.core import sounds
from tests.e2e_support import open_rig
from tests.ui_support import run, wait_for

pytestmark = pytest.mark.ui


def test_notification_controls_are_last_show_paths_and_select_reset_preview_and_mute(qapp, loop, tmp_path):
    async def scenario():
        rig = await open_rig(tmp_path)
        try:
            played = []
            rig.window.platform.play_sound = played.append
            custom = tmp_path / "custom-chime.wav"
            shutil.copyfile(sounds.sound_path("complete"), custom)

            async def choose(_start):
                return str(custom)

            rig.window.platform.pick_sound = choose
            await rig.press(",", Qt.KeyboardModifier.ControlModifier)
            await rig.click_text(".snav", "会话")
            await rig.wait_for(".sound-setting", 3)
            assert await rig.js("document.querySelector('.settings-scroll').lastElementChild.classList.contains('sound-settings')")
            for kind in sounds.KINDS:
                assert await rig.js(f"document.querySelector('#sound-{kind}').value") == sounds.sound_path(kind)
            assert await rig.count('.sound-settings [role="switch"]') == 1
            await rig.click('[aria-label="选择问题提示音"]')
            await wait_for(lambda: rig.core.settings.sound_question == str(custom))
            await rig.click('[aria-label="试听问题提示音"]')
            await wait_for(lambda: played == [str(custom)])
            await rig.click('[aria-label="提示音总开关"]')
            await wait_for(lambda: not rig.core.settings.sound_enabled)
            assert await rig.js("[...document.querySelectorAll('.sound-setting button')].filter(b => b.textContent === '试听').every(b => b.disabled)")
            assert await rig.js("document.querySelector('#sound-question').value") == str(custom), "muting never hides the chosen paths"
            await rig.click('[aria-label="恢复默认问题提示音"]')
            await wait_for(lambda: rig.core.settings.sound_question == "")
            assert await rig.js("document.querySelector('#sound-question').value") == sounds.sound_path("question")
            rig.window.view.reload()
            await rig.wait_for(".boot", 0)
            await rig.press(",", Qt.KeyboardModifier.ControlModifier)
            await rig.click_text(".snav", "会话")
            assert await rig.js("document.querySelector('[aria-label=\"提示音总开关\"]').getAttribute('aria-checked')") == "false"
            assert played == [str(custom)], "reloading does not replay sounds"
        finally:
            await rig.finish()

    run(loop, scenario())


@pytest.mark.parametrize("theme,size", [("light", (1320, 860)), ("dark", (900, 600))])
def test_program_forms_align_in_auto_and_custom_modes_and_api_provider_is_last(qapp, loop, tmp_path, theme, size):
    async def scenario():
        rig = await open_rig(tmp_path, size=size)
        try:
            await rig.core.call("settings.set", {"key": "theme", "value": theme})
            await rig.press(",", Qt.KeyboardModifier.ControlModifier)
            await rig.click_text(".snav", "账号与模型")
            await rig.wait_for(".factory-card")
            assert await rig.texts(".providers > .pcard h4") == ["ChatGPT Plan", "Devin 账号", "Factory CLI（可选）", "API Provider"]
            await rig.click_text(".snav", "外部程序")
            await rig.wait_for(".program-path")
            geometry = """(() => {
              const f = document.querySelector('.factory-program'), d = document.querySelector('.program-path');
              const selectors = [f, d, f.querySelector('h4'), f.querySelector('input'), d.querySelector('.field-label'), d.querySelector('.pathbox')];
              return JSON.stringify(selectors.map(e => { const r = e.getBoundingClientRect(); return {left:r.left, width:r.width, top:r.top, bottom:r.bottom}; }));
            })()"""
            rects = json.loads(await rig.js(geometry))
            assert abs(rects[0]["left"] - rects[1]["left"]) < 1
            assert abs(rects[0]["width"] - rects[1]["width"]) < 1
            assert abs(rects[1]["top"] - rects[0]["bottom"] - 14) < 1
            assert all(abs(r["left"] - rects[2]["left"]) < 1 for r in rects[3:])
            assert await rig.js("getComputedStyle(document.querySelector('.factory-program header')).paddingLeft") == "0px"
            await rig.click(".program-path .segc button", 1)
            await rig.wait_for('.program-path input[aria-label="Devin 命令行 路径"]')
            await rig.click('.program-path input[aria-label="Devin 命令行 路径"]')
            await rig.type("C:\\Apps\\Devin\\devin.exe")
            await rig.press("Enter")
            await wait_for(lambda: rig.core.settings.devin_path == "C:\\Apps\\Devin\\devin.exe")
            assert await rig.js("(() => { const a=document.querySelector('.program-path .pathbox').getBoundingClientRect(), b=document.querySelector('.program-path input').getBoundingClientRect(); return Math.abs(a.left-b.left)<1 && Math.abs(a.width-b.width)<1 && document.querySelector('.settings-scroll').scrollWidth<=document.querySelector('.settings-scroll').clientWidth; })()")
        finally:
            await rig.finish()

    run(loop, scenario())
