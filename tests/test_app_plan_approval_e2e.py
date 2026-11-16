"""The plan decision card through the real Qt page and a local fake model."""
from __future__ import annotations

import pytest

from tests.e2e_support import open_rig
from tests.fake_responses import call, say
from tests.test_app_e2e import new_conversation
from tests.ui_support import run

pytestmark = pytest.mark.ui


def test_plan_card_allows_revision_then_explicit_execution(qapp, loop, tmp_path):
    async def scenario():
        rig = await open_rig(tmp_path)
        try:
            model = await new_conversation(rig)
            cid = rig.core.order[0].cid
            await rig.core.call("session.choose", {"id": cid, "field": "mode", "value": "spec"})
            model.next(call("submit_plan", {"plan": "# 初版计划\n\n1. 检查代码。"}), hold=False)
            await rig.send("先规划")
            await rig.wait_text(".plan-approval", "初版计划")
            assert len(model.requests) == 1
            note = tmp_path / "note.txt"
            note.write_text("Additional constraint", encoding="utf-8")
            await rig.drop_files([note])
            await rig.wait_for(".att-row .att", 1)
            assert await rig.js('document.querySelector(".plan-approval .primary").disabled')
            await rig.click(".att-row .att-x")
            await rig.wait_for(".att-row .att", 0)
            await rig.click_text(".plan-approval button", "继续修改")
            model.next(call("submit_plan", {"plan": "# 修改后的计划\n\n1. 检查代码。\n2. 运行测试。"}), hold=False)
            await rig.send("加上验证")
            await rig.wait_text(".plan-approval", "修改后的计划")
            assert "运行测试" in await rig.text(".plan-approval")
            assert len(model.requests) == 2
            model.next(say("执行完成。"), hold=False)
            await rig.click_text(".plan-approval button", "执行方案")
            await rig.wait_for(".plan-approval", 0)
            await rig.wait_text(".transcript", "执行完成")
            assert len(model.requests) == 3
            assert rig.core.order[0].controller.mode == "auto"
        finally:
            await rig.finish()
    run(loop, scenario())
