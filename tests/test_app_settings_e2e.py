"""Real Qt/React settings: notification file selection, model order and aligned program forms."""

from __future__ import annotations

import json
import os
import shutil
from pathlib import Path

import pytest
from PySide6.QtCore import Qt

from desktop.core import sounds
from tests.e2e_support import open_rig
from tests.fake_responses import http_error
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


def test_reconnect_settings_save_and_control_the_next_conversation_turn(qapp, loop, tmp_path):
    async def scenario():
        rig = await open_rig(tmp_path)
        try:
            await rig.press(",", Qt.KeyboardModifier.ControlModifier)
            await rig.click_text(".snav", "会话")
            await rig.wait_text(".reconnect-settings h3", "重连设置")
            interval, limit = ('input[aria-label="重连间隔"]', 'input[aria-label="重连上限"]')
            assert await rig.js(f"document.querySelector({json.dumps(interval)}).value") == "30"
            assert await rig.js(f"document.querySelector({json.dumps(limit)}).value") == "5"
            await rig.js("document.querySelector('.reconnect-settings').scrollIntoView({block: 'center'})")
            if evidence := os.environ.get("WITTYCODE_TEST_EVIDENCE"):
                await rig.shot(".reconnect-settings", Path(evidence) / "reconnect-settings.png")

            for selector, attr in ((interval, "reconnect_interval"), (limit, "reconnect_limit")):
                await rig.click(selector)
                await rig.press("A", Qt.KeyboardModifier.ControlModifier)
                await rig.type("1")
                await rig.press("Enter")
                await wait_for(lambda: getattr(rig.core.settings, attr) == 1)

            await rig.click('.settings-body [aria-label="关闭"]')
            await rig.core.call("session.new", {"projectId": str(rig.project)})
            await rig.wait_text(".statusbar .st-left", "就绪")
            error = http_error(502, {"error": {"message": "Bad gateway"}}, {"Retry-After": "60"})
            rig.plan.script(error, error)
            await rig.send("检查库存接口")
            await rig.wait_text(".block.notice", "1 秒后自动重试（第 1/1 次）")
            await rig.wait_for(".block.error", 1)
            assert len(rig.plan.requests) == 2
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
              const f = document.querySelector('.factory-program'), d = document.querySelector('.devin-program');
              const selectors = [f, d, f.querySelector('.field-label'), d.querySelector('.field-label'), f.querySelector('.pathbox'), d.querySelector('.pathbox')];
              return JSON.stringify(selectors.map(e => { const r = e.getBoundingClientRect(); return {left:r.left, width:r.width, top:r.top, bottom:r.bottom}; }));
            })()"""
            rects = json.loads(await rig.js(geometry))
            cards, labels, pathboxes = rects[:2], rects[2:4], rects[4:]
            assert abs(cards[0]["left"] - cards[1]["left"]) < 1
            assert abs(cards[0]["width"] - cards[1]["width"]) < 1
            assert abs(cards[1]["top"] - cards[0]["bottom"] - 14) < 1
            assert abs(labels[0]["left"] - labels[1]["left"]) < 1
            assert abs(pathboxes[0]["left"] - pathboxes[1]["left"]) < 1
            assert abs(pathboxes[0]["width"] - pathboxes[1]["width"]) < 1
            assert all(abs(label["left"] - pathbox["left"]) < 1 for label, pathbox in zip(labels, pathboxes))
            await rig.click(".devin-program .segc button", 1)
            await rig.wait_for('.devin-program input[aria-label="Devin 命令行 路径"]')
            await rig.click('.devin-program input[aria-label="Devin 命令行 路径"]')
            await rig.type("C:\\Apps\\Devin\\devin.exe")
            await rig.press("Enter")
            await wait_for(lambda: rig.core.settings.devin_path == "C:\\Apps\\Devin\\devin.exe")
            assert await rig.js("(() => { const a=document.querySelector('.devin-program .pathbox').getBoundingClientRect(), b=document.querySelector('.devin-program input').getBoundingClientRect(); return Math.abs(a.left-b.left)<1 && Math.abs(a.width-b.width)<1 && document.querySelector('.settings-scroll').scrollWidth<=document.querySelector('.settings-scroll').clientWidth; })()")
        finally:
            await rig.finish()

    run(loop, scenario())
