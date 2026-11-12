"""The real settings UI saves a larger budget and updates an already running own-loop turn."""
from __future__ import annotations

import pytest
from PySide6.QtCore import Qt

from tests.e2e_support import open_rig
from tests.fake_responses import call, say
from tests.test_app_e2e import new_conversation
from tests.ui_support import run, wait_for

pytestmark = pytest.mark.ui


def test_the_step_limit_can_be_saved_in_settings_and_raised_while_a_turn_runs(qapp, loop, tmp_path):
    async def scenario():
        rig = await open_rig(tmp_path)
        try:
            model = await new_conversation(rig)

            async def settings():
                await rig.press(Qt.Key.Key_Comma, Qt.KeyboardModifier.ControlModifier)
                await rig.wait_for(".settings")
                await rig.click(".snav", 3)
                await rig.wait_for(".step-limit-setting")

            async def type_limit(text):
                await rig.click(".step-limit-setting input")
                await rig.press(Qt.Key.Key_A, Qt.KeyboardModifier.ControlModifier)
                await rig.type(text)

            async def save_limit(value):
                await type_limit(str(value))
                await rig.click(".step-limit-setting .btn")
                await wait_for(lambda: rig.core.settings.max_steps == value)

            await settings()
            assert await rig.js("document.querySelector('.step-limit-setting input').value") == "80"
            await type_limit("0")
            assert await rig.js("document.querySelector('.step-limit-setting .btn').disabled")
            assert rig.core.settings.max_steps == 80
            await type_limit("240")
            assert rig.core.settings.max_steps == 80, "typing a prefix must not change a running turn's budget"
            await rig.click(".step-limit-setting .btn")
            await wait_for(lambda: rig.core.settings.max_steps == 240)
            await rig.press("Escape")
            await settings()
            assert await rig.js("document.querySelector('.step-limit-setting input').value") == "240"
            await save_limit(1)
            await rig.press("Escape")
            model.next(call("list_dir", {"directory_path": str(rig.project)}))
            model.next(say("continued after raising the limit"), hold=False)
            await rig.send("keep working")
            await model.asked()
            await settings()
            await type_limit("2")
            assert rig.core.settings.max_steps == 1
            await rig.click(".step-limit-setting .btn")
            await wait_for(lambda: rig.core.settings.max_steps == 2)
            await rig.press("Escape")
            model.go()
            await rig.wait_text(".text-block", "continued after raising the limit")
            await rig.wait_for(".send.stop", 0)
            assert len(model.requests) == 2
            assert "步还没结束" not in await rig.text(".transcript")
        finally:
            await rig.finish()

    run(loop, scenario())
