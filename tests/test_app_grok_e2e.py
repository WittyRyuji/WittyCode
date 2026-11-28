"""Sign in to a fake Grok subscription through the real Qt and React page and run a Harness tool turn."""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest
from PySide6.QtCore import Qt

from desktop.core.llm import grok
from desktop.core.llm.backend import ApiBackend
from tests import e2e_support
from tests.chatgpt_support import make_engine
from tests.fake_responses import call, say
from tests.test_grok_subscription import ACCESS, MODEL, REFRESH, VERIFY, FakeGrok, make_store
from tests.ui_support import run, wait_for

pytestmark = [pytest.mark.ui, pytest.mark.skipif(sys.platform != "win32", reason="Windows current-user DPAPI")]


def test_grok_device_login_model_selection_harness_tools_and_logout(qapp, loop, tmp_path, monkeypatch):
    if work := os.environ.get("WITTYCODE_TEST_WORK"):
        page = Path(work) / "dist" / "index.html"
        if page.is_file():
            monkeypatch.setattr(e2e_support, "PAGE_FILE", page)

    async def scenario():
        http, pages = FakeGrok(), []
        http.auth.clear()
        http.responses = [say("File check"), call("read_file", {"file_path": "sample.txt"}), say("Grok saw the local file.")]
        subscription = grok.GrokSubscription(tmp_path / "grok.bin", open_browser=pages.append, http=http)
        providers = make_store(tmp_path)
        engine = make_engine(tmp_path, None, signed_in=False, api=ApiBackend(providers, http, grok_subscription=subscription))
        rig = await e2e_support.open_rig(tmp_path, chatgpt_engine=engine, providers=providers, plan=False)
        (rig.project / "sample.txt").write_text("Local sample for Grok.", encoding="utf-8")
        card = ".grok-card"
        try:
            await rig.press(",", Qt.KeyboardModifier.ControlModifier)
            await rig.wait_for(".settings")
            await rig.click_text(".snav", "账号与模型")
            await rig.wait_text(card, "Grok 订阅（实验性）")
            await rig.click_text(f"{card} .btn", "登录 Grok 订阅")
            await rig.wait_text(f"{card} code.selectable", "DEMO-2048")
            await rig.click_text(f"{card} .btn", "打开登录页")
            await wait_for(lambda: pages == [VERIFY, VERIFY], 5, "the browser to be reopened")
            if evidence := os.environ.get("WITTYCODE_TEST_EVIDENCE"):
                await rig.shot(card, Path(evidence) / "grok-subscription-device-code.png")
            await rig.click_text(f"{card} .btn", "取消")
            await rig.wait_text(card, "未登录")
            assert not subscription.path.exists()

            await rig.click_text(f"{card} .btn", "登录 Grok 订阅")
            await rig.wait_text(card, "DEMO-2048")
            http.auth.set()
            await rig.wait_text(card, "已登录")
            await rig.wait_for(f'{card} .mvis[data-source="api:grok-subscription"]')
            assert ACCESS.encode() not in subscription.path.read_bytes() and REFRESH.encode() not in subscription.path.read_bytes()
            assert ACCESS not in await rig.text(card)
            await rig.press("Escape")
            await rig.wait_for(".settings", 0)

            await rig.core.call("session.new", {"projectId": str(rig.project), "autonomy": "full"})
            await rig.wait_for(".tab")
            await rig.wait_text(".statusbar .st-left", "就绪")
            await rig.click(".controls .chip", 0)
            await rig.wait_text(".menu-head", "GROK 订阅")
            await rig.click_text(".menu-item", "grok-4.7")
            await rig.wait_text(".controls .src-tag", "订阅")
            await rig.send("Read sample.txt")
            await rig.wait_text(".text-block", "Grok saw the local file.")
            assert rig.core.catalog.engine_of(MODEL) == "chatgpt"
            assert len(http.inference) == 3  # title, prompt with tools, and tool output
            for method, url, headers, body in http.inference:
                assert (method, url, headers["Authorization"], body["model"]) == ("POST", grok.SUBSCRIPTION_URL + "/responses", "Bearer " + ACCESS, "grok-4.7")
            assert any(item.get("type") == "function_call_output" and "Local sample for Grok." in str(item.get("output"))
                       for item in http.inference[-1][3]["input"])

            await rig.press(",", Qt.KeyboardModifier.ControlModifier)
            await rig.wait_for(".settings")
            await rig.click_text(".snav", "账号与模型")
            await rig.wait_text(card, "已登录")
            await rig.click_text(f"{card} .btn", "退出登录")
            await rig.wait_text(card, "未登录")
            assert not subscription.path.exists()
            await rig.wait_for(f"{card} .mvis", 0)
        finally:
            await rig.finish()

    run(loop, scenario())
