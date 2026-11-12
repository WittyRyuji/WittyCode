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
            aligned = "(() => { const t = document.querySelector('.transcript').getBoundingClientRect(), c = document.querySelector('.composer').getBoundingClientRect(); return Math.abs(t.left - c.left) < 1 && Math.abs(t.right - c.right) < 1; })()"
            assert await rig.js(aligned), "the message and composer edges share the same reading grid"
            await rig.click(".plan-close")
            await rig.wait_for(".planpanel", 0)
            assert await rig.js("document.querySelector('.conversation-column').getBoundingClientRect().width === document.querySelector('.conversation-body').getBoundingClientRect().width"), "closing releases the whole plan column"
            assert await rig.js(aligned)
            model.go()
            await rig.wait_text(".plan-toggle", "1/3")
            assert not await rig.exists(".planpanel"), "streamed progress must not reopen a dismissed plan"
            await rig.click(".plan-toggle")
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


def test_own_loop_plan_updates_the_open_panel_through_every_step_before_the_turn_finishes(qapp, loop, tmp_path):
    async def scenario():
        rig = await open_rig(tmp_path)
        try:
            model = await new_conversation(rig)
            for done in range(4):
                todos = [{"content": text, "status": "completed" if i < done else "in_progress" if i == done else "pending"} for i, text in enumerate(("检查", "修改", "验证"))]
                model.next(call("todo_write", json.dumps({"todos": todos}), call_id=f"plan-{done}", item_id=f"plan-item-{done}"), hold=done > 0)
            model.next(say("计划完成。"))
            await rig.send("按三步完成检查")
            await rig.wait_text(".plan-count", "0/3")
            for done in range(1, 4):
                model.go()
                await rig.wait_text(".plan-count", f"{done}/3")
                assert await rig.count(".plan-step.st-completed") == done
                assert await rig.exists(".send.stop"), "plan progress updates without ending the running turn"
            model.go()
            await rig.wait_for(".send.stop", 0)
            assert await rig.count(".plan-step.st-completed") == 3
        finally:
            await rig.finish()

    run(loop, scenario())


def test_native_plan_and_tool_input_updates_reach_the_open_panel_without_waiting_for_turn_end(qapp, loop, tmp_path):
    from tests.devin_support import make_engine
    from tests.test_app_e2e import devin_listed

    async def scenario():
        harness = make_engine(tmp_path)
        rig = await open_rig(tmp_path, devin_engine=harness.engine)
        try:
            await devin_listed(rig)
            await rig.core.call("session.new", {"projectId": str(rig.project), "modelId": "devin:swe-2-high"})
            await rig.wait_text(".statusbar .st-left", "就绪")
            await rig.send("[slow] keep the demo turn running")
            await rig.wait_for(".send.stop")
            controller = rig.core.order[0].controller
            turn = controller.transcript.turns[-1]

            def notify(update):
                controller._on_notification("session/update", {"sessionId": controller.session_id, "update": update})

            def steps(done):
                return [{"content": text, "status": "completed" if i < done else "in_progress" if i == done else "pending"} for i, text in enumerate(("检查事件", "修复更新", "运行验证"))]

            notify({"sessionUpdate": "tool_call", "toolCallId": "todo_write:demo", "rawInput": {"todos": steps(0)}})
            await rig.wait_text(".plan-count", "0/3")
            notify({"sessionUpdate": "tool_call_update", "toolCallId": "todo_write:demo", "rawInput": {"todos": steps(1)}, "status": "completed"})
            await rig.wait_text(".plan-count", "1/3")
            assert await rig.text(".plan-step.st-in_progress") == "修复更新"
            tool_count = len(turn.tools)
            notify({"sessionUpdate": "plan", "entries": steps(2)})
            await rig.wait_text(".plan-count", "2/3")
            assert len(turn.tools) == tool_count, "native plans are state updates, not invented tool calls"
            notify({"sessionUpdate": "tool_call_update", "toolCallId": "todo_write:demo", "rawInput": {"todos": steps(3)}, "status": "completed"})
            await rig.wait_text(".plan-count", "3/3")
            assert await rig.count(".plan-step.st-completed") == 3
            notify({"sessionUpdate": "tool_call_update", "toolCallId": "todo_write:demo", "rawInput": {"todos": steps(0)}, "status": "failed"})
            notify({"sessionUpdate": "plan", "entries": [{"content": "partial"}]})
            await rig.wait_text(".plan-count", "3/3")
            assert await rig.exists(".send.stop"), "the update is visible while the original turn is still running"
            notify({"sessionUpdate": "tool_call", "toolCallId": "todo_write:already-complete", "rawInput": {"todos": [{"content": "无需额外结束通知", "status": "completed"}]}, "status": "completed"})
            await rig.wait_text(".plan-count", "1/1")
            await rig.click(".plan-close")
            notify({"sessionUpdate": "plan", "entries": [{"content": "关闭期间的新任务", "status": "in_progress"}]})
            await rig.wait_text(".plan-toggle", "0/1")
            assert not await rig.exists(".planpanel")
            await rig.click(".plan-toggle")
            await rig.wait_text(".plan-step", "关闭期间的新任务")
            notify({"sessionUpdate": "plan", "entries": []})
            await rig.wait_for(".planpanel", 0)
            assert not await rig.exists(".plan-toggle"), "explicit empty state cannot resurrect the old tool checklist"
            await controller.interrupt()
            await rig.wait_for(".send.stop", 0)
            notify({"sessionUpdate": "plan", "entries": steps(0)})
            assert not await rig.exists(".planpanel"), "late notifications from a finished turn are ignored"
        finally:
            await rig.finish()

    run(loop, scenario())
