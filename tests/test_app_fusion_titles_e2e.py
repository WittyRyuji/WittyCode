"""A Fusion title appears in the real page before the successful main request ends."""
from __future__ import annotations

import pytest

from desktop.core.session_state import SessionState
from tests.e2e_support import open_rig
from tests.fake_responses import say
from tests.fakes import until
from tests.test_app_fusion_e2e import select_fusion
from tests.ui_support import run

pytestmark = pytest.mark.ui


@pytest.mark.parametrize("entry", ["project-new", "empty-tab"])
def test_project_new_fusion_titles_while_the_main_still_streams(qapp, loop, tmp_path, entry):
    async def scenario():
        rig = await open_rig(tmp_path)
        try:
            for key, value in {"fusionMain": "chatgpt:gpt-6.1-sol", "fusionWorkers": ["chatgpt:gpt-6-luna"],
                               "fusionLight": "chatgpt:gpt-6-luna"}.items():
                await rig.core.call("settings.set", {"key": key, "value": value})
            if entry == "project-new":
                await rig.wait_for('button[aria-label="新会话"]')
                await rig.click('button[aria-label="新会话"]')
                await rig.wait_for(".tab.current", 0)  # Landing has no tab before the first send.
            else:
                await rig.core.call("session.new", {"projectId": str(rig.project)})
                await rig.wait_text(".tab.current .tab-title", "新会话")
            await select_fusion(rig)
            await rig.wait_text(".chip-model .chip-v", "Fusion")
            assert not rig.plan.requests and not rig.plan.fake.title_requests
            assert not await rig.exists(".srow.open")

            main = say("正在核对入库记录。")
            rig.plan.script([*main[:-1], ("hold", "main"), main[-1]])
            rig.plan.fake.title_answer = "库存导入去重"
            await rig.send("导入库存时重复条目不应计数两次")
            await rig.wait_text(".transcript", "正在核对入库记录。")
            ctl = rig.core.order[-1].controller
            assert ctl.state is SessionState.RUNNING
            try:
                await rig.wait_text(".tab.current .tab-title", "库存导入去重", timeout=4)
                await rig.wait_text(".srow.open .stitle", "库存导入去重", timeout=4)
            except AssertionError as error:
                raise AssertionError(f"tab={await rig.text('.tab.current .tab-title')!r}; "
                                     f"sidebar={await rig.text('.srow.open .stitle')!r}; "
                                     f"title requests={len(rig.plan.fake.title_requests)}") from error
            assert ctl.state is SessionState.RUNNING
            assert rig.engine.sessions.load(ctl.session_id).title == "库存导入去重"
            rig.plan.release("main")
            await until(lambda: ctl.state is SessionState.READY)
            await rig.wait_for(".srow.open.m-ready")  # closeTab uses the rendered running state for its confirmation.
            await rig.click(".tab.current .tab-x")
            await rig.wait_for(".tab", 0)
            await until(lambda: ctl.state is SessionState.CLOSED)
            await rig.wait_text(".srow:not(.open) .stitle", "库存导入去重")
            await rig.click_text(".srow:not(.open)", "库存导入去重")
            await rig.wait_text(".tab.current .tab-title", "库存导入去重")
            assert len(rig.plan.fake.title_requests) == 1
        finally:
            rig.plan.release("main")
            await rig.finish()

    run(loop, scenario())
