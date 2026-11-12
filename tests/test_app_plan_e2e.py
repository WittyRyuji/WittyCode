"""The real Qt page follows structured plan updates without repeating the checklist inside thinking."""

from __future__ import annotations

import json

import pytest

from desktop.core.session_state import SessionState
from tests.e2e_support import open_rig
from tests.fake_responses import call, say
from tests.test_app_e2e import new_conversation
from tests.ui_support import run, wait_for

pytestmark = pytest.mark.ui


def test_the_plan_is_live_on_the_right_and_follows_only_its_conversation(qapp, loop, tmp_path):
    async def scenario():
        rig = await open_rig(tmp_path)
        try:
            model = await new_conversation(rig)
            assert not await rig.exists(".planpanel"), "no steps means no empty plan column"
            steps = [
                {"content": "检查输入与现有测试", "status": "in_progress"},
                {"content": "实现修改", "status": "pending"},
                {"content": "运行回归", "status": "pending"},
            ]
            model.next(call("todo_write", json.dumps({"todos": steps}), call_id="plan1"), hold=False)
            steps[0]["status"], steps[1]["status"] = "completed", "in_progress"
            model.next(call("todo_write", json.dumps({"todos": steps}), call_id="plan2", item_id="fc_2"))
            model.next(say("修改完成。"))
            await rig.send("处理这个三步任务")
            await rig.wait_for(".plan-step", 3)
            assert await rig.text(".plan-count") == "0/3"
            assert await rig.count(".plan-step.st-in_progress") == 1
            assert await rig.js("document.querySelector('.planpanel').getBoundingClientRect().left >= document.querySelector('.stage').getBoundingClientRect().right - 1")
            model.go()
            await rig.wait_text(".plan-count", "1/3")
            assert await rig.count(".planpanel") == 1 and await rig.count(".plan-step") == 3, "updates replace the plan rather than append copies"
            assert await rig.text(".plan-step.st-in_progress") == "实现修改"
            model.go()
            await rig.wait_for(".send.stop", 0)
            await rig.click(".plan-head")
            await rig.wait_for(".plan-steps", 0)
            await rig.click(".plan-head")
            await rig.wait_for(".plan-step", 3)
            await rig.click(".block.activity .gsum")
            await rig.click(".block.activity .irow")
            await rig.wait_for(".plan-record")
            assert not await rig.js("document.querySelector('.plan-record').open"), "the old input/output is retained, but its full checklist is folded"
            await rig.click(".plan-record > summary")
            assert await rig.js("document.querySelector('.plan-record .todos').getBoundingClientRect().height > 0"), "historical plan steps remain inspectable"
            await new_conversation(rig, tab_count=2)
            assert await rig.count(".main .transcript") == 1, "switching tabs must remove the previous transcript, not leave it before the new one"
            assert await rig.count(".main .transcript .block.user") == 0, "the empty conversation must not show another tab's messages"
            assert not await rig.exists(".planpanel"), "the other tab has no plan"
            await rig.click(".tab", 0)
            await rig.wait_text(".plan-count", "1/3")
            assert await rig.text(".plan-step.st-in_progress") == "实现修改"
            await rig.click(".tab .tab-x", 1)
            await rig.wait_for(".tab", 1)
            await rig.js("window.__planReloadMarker = true")
            rig.window.page.loading_allowed = True  # the shell normally refuses even a reload; permit this test-controlled load
            rig.window.view.reload()

            async def restored():
                return await rig.js("window.__planReloadMarker !== true && document.querySelectorAll('.tab').length === 1 && !document.querySelector('.boot')")

            await wait_for(restored, 10, "the page reloaded its conversation tab")
            rig.window.page.loading_allowed = False
            await rig.click(".tab", 0)
            await rig.wait_text(".plan-count", "1/3")
            controller = rig.core.order[0].controller
            native = controller.session_id
            await rig.click(".tab.current .tab-x")
            await rig.wait_for(".tab", 0)
            await wait_for(lambda: controller.state is SessionState.CLOSED)
            await rig.core.call("session.openSaved", {"projectId": str(rig.project), "id": "cg:" + native})
            await rig.wait_text(".statusbar .st-left", "就绪")
            await rig.wait_text(".plan-count", "1/3")
        finally:
            await rig.finish()

    run(loop, scenario())
