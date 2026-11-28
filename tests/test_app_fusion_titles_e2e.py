"""The page waits for a title before dispatching the first turn."""
from __future__ import annotations

import os
from pathlib import Path

import pytest

from desktop.core.session_state import SessionState
from tests.e2e_support import open_rig
from tests.fake_responses import say
from tests.fakes import until
from tests.test_app_fusion_e2e import select_fusion
from tests.ui_support import run

pytestmark = pytest.mark.ui


@pytest.mark.parametrize("entry", ["project-new", "empty-tab"])
@pytest.mark.parametrize("mode", ["plan", "fusion"])
def test_new_session_titles_before_the_first_turn(qapp, loop, tmp_path, entry, mode):
    async def scenario():
        rig = await open_rig(tmp_path)
        try:
            if mode == "fusion":
                for key, value in {"fusionMain": "chatgpt:gpt-6-luna", "fusionWorkers": ["chatgpt:gpt-6.1-sol"],
                                   "fusionLight": "chatgpt:gpt-6.1-sol"}.items():
                    await rig.core.call("settings.set", {"key": key, "value": value})
            if entry == "project-new":
                await rig.wait_for('button[aria-label="新会话"]')
                await rig.click('button[aria-label="新会话"]')
                await rig.wait_for(".tab.current", 0)  # Landing has no tab before the first send.
            else:
                await rig.core.call("session.new", {"projectId": str(rig.project)})
                await rig.wait_text(".tab.current .tab-title", "新会话")
            if mode == "fusion":
                await select_fusion(rig)
                await rig.wait_text(".chip-model .chip-v", "Fusion")
            assert not rig.plan.requests and not rig.plan.fake.title_requests
            assert not await rig.exists(".srow.open")

            main = say("正在核对入库记录。")
            rig.plan.script([("hold", "main"), *main])
            rig.plan.fake.title_turn = [("hold", "title"), *say("库存导入去重")]
            await rig.send("导入库存时重复条目不应计数两次")
            await rig.wait_text(".statusbar", "正在生成会话名称…")
            await until(lambda: bool(rig.plan.fake.title_requests))
            conv = rig.core.order[-1]
            ctl = conv.controller
            assert not rig.plan.requests and not conv.controller.transcript.turns
            assert not await rig.exists(".srow")  # a draft still being named is not a sidebar row
            await rig.wait_text(".tab.current .tab-title", "新会话")
            rig.plan.release("title")
            await until(lambda: bool(rig.plan.requests))  # the turn started; the model has not said a word yet
            assert ctl.state is SessionState.RUNNING and not conv.in_history
            try:
                await rig.wait_text(".srow .stitle", "库存导入去重", timeout=4)
                await rig.wait_text(".tab.current .tab-title", "库存导入去重", timeout=4)
            except AssertionError as error:
                raise AssertionError(f"tab={await rig.text('.tab.current .tab-title')!r}; "
                                     f"sidebar={await rig.text('.srow .stitle')!r}; "
                                     f"title requests={len(rig.plan.fake.title_requests)}") from error
            assert await rig.count(".srow") == 1 and ctl.state is SessionState.RUNNING
            if evidence := os.environ.get("WITTYCODE_TEST_EVIDENCE"):
                await rig.shot(".srow.open", Path(evidence) / "titled-before-reply.png")
            rig.plan.release("main")
            await rig.wait_text(".transcript", "正在核对入库记录。")
            await until(lambda: conv.in_history)
            assert await rig.count(".srow") == 1  # the same row was promoted, not listed twice
            expected_model = "gpt-6-luna" if mode == "fusion" else "gpt-6.1-sol"
            (title_request,) = rig.plan.fake.title_requests
            assert title_request["model"] == rig.plan.requests[0]["model"] == expected_model
            if mode == "fusion":
                assert rig.engine.sessions.load(ctl.session_id).title == "库存导入去重"
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
            rig.plan.release("title")
            rig.plan.release("main")
            await rig.finish()

    run(loop, scenario())


def test_invalid_first_title_restores_draft_after_three_attempts(qapp, loop, tmp_path):
    async def scenario():
        rig = await open_rig(tmp_path)
        try:
            await rig.core.call("session.new", {"projectId": str(rig.project)})
            await rig.wait_text(".tab.current .tab-title", "新会话")
            rig.plan.fake.title_answer = "不是\n单行标题"
            question = "检查库存接口"
            await rig.send(question)
            await until(lambda: len(rig.plan.fake.title_requests) == 3)
            await rig.wait_for(".preparing-turn", 0)
            await until(lambda: not rig.core.order[-1].naming)
            assert await rig.js("document.querySelector('.input').value") == question
            assert not rig.plan.requests and not await rig.exists(".srow")  # a failed title buys no sidebar row
            await rig.wait_text(".tab.current .tab-title", "新会话")
            rig.plan.fake.title_answer = "库存接口检查"
            rig.plan.script(say("已检查库存接口。"))
            await rig.focus_composer()
            await rig.press("Enter")
            await rig.wait_text(".srow.open .stitle", "库存接口检查")
            await rig.wait_text(".transcript", "已检查库存接口。")
            assert len(rig.plan.requests) == 1 and len(rig.plan.fake.title_requests) == 4
        finally:
            await rig.finish()

    run(loop, scenario())
