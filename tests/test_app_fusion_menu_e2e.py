"""Fusion is selected at the top of the real model menu, not below the composer."""
from __future__ import annotations

import pytest
from PySide6.QtCore import Qt

from tests.e2e_support import open_rig
from tests.fake_responses import say
from tests.fakes import until
from tests.ui_support import run

pytestmark = pytest.mark.ui


def test_fusion_is_first_in_model_menu_and_normal_model_exits_the_selection(qapp, loop, tmp_path):
    async def scenario():
        rig = await open_rig(tmp_path)
        try:
            await until(lambda: any(model["id"] == "chatgpt:gpt-6-luna" for model in rig.core.catalog.models))
            for key, value in {"fusionMain": "chatgpt:gpt-6.1-sol", "fusionWorkers": ["chatgpt:gpt-6-luna"],
                               "fusionLight": "chatgpt:gpt-6-luna", "fusionReasoning": {"chatgpt:gpt-6.1-sol": "high"}}.items():
                await rig.core.call("settings.set", {"key": key, "value": value})
            assert not await rig.exists(".fusion-choice, .fusion-badge")
            await rig.click(".chip-model")
            await rig.wait_text(".menu-item .menu-label", "Fusion")
            assert (await rig.texts(".menu-item .menu-label"))[0] == "Fusion"
            assert await rig.js('document.querySelector(".menu").firstElementChild.querySelector(".menu-label").textContent') == "Fusion"
            assert rig.window.grab().save(str(tmp_path / "fusion-model-menu.png"))
            await rig.press(Qt.Key.Key_Down)
            await rig.press("Enter")
            await rig.wait_text(".chip-model .chip-v", "Fusion")
            await rig.wait_text(".chip-reasoning .chip-v", "High")
            assert not await rig.js('document.querySelector(".chip-model").disabled')
            assert not await rig.exists(".chip-model .src-tag")
            assert "GPT-6.1-Sol" in await rig.js('document.querySelector(".chip-model").title')
            assert rig.window.grab().save(str(tmp_path / "fusion-selected.png"))
            assert not rig.plan.requests
            await rig.click(".chip-model")
            await rig.wait_text(".menu-item .menu-label", "Fusion")
            assert await rig.js('!!document.querySelector(".menu-item .menu-check svg")')
            assert await rig.count(".menu-item .menu-check svg") == 1
            await rig.click_text(".menu-item", "GPT-6-Luna")
            await rig.wait_text(".chip-model .chip-v", "GPT-6-Luna")
            assert not await rig.js('document.querySelector(".chip-reasoning").disabled')
            rig.plan.script(say("普通模型会话已完成。"))
            await rig.send("Inspect this module")
            await rig.wait_text(".transcript", "普通模型会话已完成")
            assert len(rig.core.order) == 1 and rig.core.order[0].controller._fusion is None
            await rig.click(".chip-model")
            await rig.wait_text(".menu-item .menu-label", "Fusion")
            assert await rig.js('document.querySelector(".menu-item").disabled')
            assert "仅新会话" in await rig.text(".menu-item", 0)
            assert not await rig.exists(".fusion-choice, .fusion-badge")
        finally:
            await rig.finish()
    run(loop, scenario())
