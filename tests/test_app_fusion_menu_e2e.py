"""Fusion is selected at the top of the real model menu, not below the composer."""
from __future__ import annotations

import pytest
from PySide6.QtCore import Qt

from tests.e2e_support import open_rig
from tests.chatgpt_support import finish_turn
from tests.fake_responses import say
from tests.fakes import until
from tests.ui_support import run, wait_for

pytestmark = pytest.mark.ui


@pytest.mark.parametrize("theme", ["light", "dark"])
def test_fusion_is_first_in_model_menu_and_normal_model_exits_the_selection(qapp, loop, tmp_path, theme):
    async def scenario():
        rig = await open_rig(tmp_path)
        try:
            await rig.core.call("settings.set", {"key": "theme", "value": theme})
            await until(lambda: any(model["id"] == "chatgpt:gpt-6-luna" for model in rig.core.catalog.models))
            for key, value in {"fusionMain": "chatgpt:gpt-6.1-sol", "fusionWorkers": ["chatgpt:gpt-6-luna"],
                               "fusionLight": "chatgpt:gpt-6-luna", "fusionReasoning": {"chatgpt:gpt-6.1-sol": "high"}}.items():
                await rig.core.call("settings.set", {"key": key, "value": value})
            assert not await rig.exists(".fusion-choice, .fusion-badge")
            await rig.click(".chip-model")
            await rig.wait_text(".menu-item .menu-label", "Fusion")
            assert (await rig.texts(".menu-item .menu-label"))[0] == "Fusion"
            assert await rig.js('document.querySelector(".menu").firstElementChild.querySelector(".menu-label").textContent') == "Fusion"
            assert not await rig.exists(".menu-row .menu-desc")
            assert await rig.js('(() => { const row = document.querySelector(".menu-row").getBoundingClientRect(); const gear = document.querySelector(".menu-configure").getBoundingClientRect(); return Math.abs(row.right - gear.right) < 2 && gear.width >= 30; })()')
            assert rig.window.grab().save(str(tmp_path / "fusion-model-menu.png"))
            await rig.press(Qt.Key.Key_Down)
            await rig.press(Qt.Key.Key_Right)
            await rig.press("Enter")
            await rig.wait_text(".settings-body h2", "Fusion")
            assert not rig.core.order and not rig.plan.requests
            await rig.click('.settings-body button[aria-label="关闭"]')
            await rig.click(".chip-model")
            await rig.click_text(".menu-item", "Fusion")
            await rig.wait_text(".chip-model .chip-v", "Fusion")
            assert not await rig.exists(".chip-reasoning")
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
            main = rig.core.order[0].controller
            await finish_turn(main)
            cid, native, count = rig.core.order[0].cid, main.session_id, len(rig.plan.requests)
            await rig.click(".chip-model")
            await rig.wait_text(".menu-item .menu-label", "Fusion")
            await wait_for(lambda: rig.js('!document.querySelector(".menu-item").disabled'))
            assert not await rig.exists(".menu-row .menu-desc")
            rig.plan.script(say("已检查模块，接下来继续工作。"))
            await rig.click_text(".menu-item", "Fusion")
            await rig.wait_text(".chip-model .chip-v", "Fusion")
            assert main._fusion is not None and rig.core.order[0].cid == cid and main.session_id == native
            assert len(rig.plan.requests) == count + 1 and not await rig.exists(".fusion-choice, .fusion-badge, .chip-reasoning")
            assert rig.plan.requests[-1]["model"] == "gpt-6-luna"
            await rig.click(".chip-model")
            await rig.wait_text(".menu-item .menu-label", "Fusion")
            assert await rig.count(".menu-item .menu-check svg") == 1
            assert "退出 Fusion" in await rig.text(".menu")
            assert rig.window.grab().save(str(tmp_path / "fusion-session-menu.png"))
            await rig.click_text(".menu-item", "GPT-6-Luna")
            await rig.wait_text(".chip-model .chip-v", "GPT-6-Luna")
            assert main._fusion is None and main._paused_fusion is not None
            assert main.session_id == native and rig.core.order[0].cid == cid and len(rig.plan.requests) == count + 1
            assert not await rig.js('document.querySelector(".chip-reasoning").disabled')
            rig.plan.script(say("已回到普通模型。"))
            await rig.send("Continue without Fusion")
            await rig.wait_text(".transcript", "已回到普通模型")
            assert "delegate_task" not in {tool.get("name") for tool in rig.plan.requests[-1]["tools"]}
        finally:
            await rig.finish()
    run(loop, scenario())
