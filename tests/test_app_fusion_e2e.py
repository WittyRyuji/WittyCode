"""Fusion settings, new conversation, and expert panel through the real Qt page."""
from __future__ import annotations

import pytest
from PySide6.QtCore import Qt

from tests.e2e_support import open_rig
from tests.fake_responses import call, say
from tests.fakes import until
from tests.ui_support import run

pytestmark = pytest.mark.ui


def test_configure_and_start_fusion_with_a_local_fake(qapp, loop, tmp_path):
    async def scenario():
        rig = await open_rig(tmp_path)
        try:
            await rig.wait_for(".fusion-choice")
            await rig.click(".fusion-choice")  # The incomplete setup opens settings instead of silently enabling Fusion.
            await rig.wait_text(".settings-body h2", "Fusion")
            for index, name in [(0, "GPT-6.1-Sol"), (1, "GPT-6-Luna"), (2, "GPT-6-Luna")]:
                await rig.click(".settings-body .selectbox", index)
                await rig.wait_text(".menu-item", name)
                await rig.click_text(".menu-item", name)
            await until(lambda: rig.core.settings.fusion_main == "chatgpt:gpt-6.1-sol" and rig.core.settings.fusion_light == "chatgpt:gpt-6-luna" and rig.core.settings.fusion_workers == ["chatgpt:gpt-6-luna"])
            assert rig.window.grab().save(str(tmp_path / "fusion-settings.png"))
            await rig.click('.settings-body button[aria-label="关闭"]')
            await rig.click(".fusion-choice")
            assert await rig.js('document.querySelector(".fusion-choice").getAttribute("aria-pressed")') == "true"
            rig.plan.script(call("delegate_task", {"scope": "src", "model": "chatgpt:gpt-6-luna", "role": "worker", "task": "Inspect the module."}),
                            say("专家检查完成。"), say("主代理已检查报告。"))
            await rig.send("请检查模块")
            await rig.wait_for(".fusion-badge")
            await rig.wait_text(".agent-chip", "src")
            await rig.wait_text(".transcript", "主代理已检查报告")
            assert len(rig.core.order) == 1
            assert [request["model"] for request in rig.plan.requests] == ["gpt-6.1-sol", "gpt-6-luna", "gpt-6.1-sol"]
            await rig.click(".agent-chip")
            await rig.wait_text(".agentpanel", "专家检查完成")
            assert rig.window.grab().save(str(tmp_path / "fusion-expert.png"))
            main = rig.core.order[0].controller
            await until(lambda: main.state.value == "ready")
            await rig.click_text(".hint button", "更换主模型")
            await rig.click_text(".menu-item", "GPT-6-Luna")
            await rig.wait_text(".dialog", "更换主模型为 GPT-6-Luna")
            assert main.model == "gpt-6.1-sol" and len(rig.plan.requests) == 3
            assert rig.window.grab().save(str(tmp_path / "fusion-change-main.png"))
            await rig.click_text(".dialog button", "更换主模型")
            await until(lambda: main.model == "gpt-6-luna")
            assert len(rig.plan.requests) == 3 and main._fusion.registry.policy.main == "chatgpt:gpt-6-luna"
            await rig.click_text(".hint button", "授权模型")
            await rig.click_text(".menu-item", "GPT-6.1-Sol")
            await rig.wait_text(".dialog", "授权 GPT-6.1-Sol")
            assert "chatgpt:gpt-6.1-sol" not in main._fusion.registry.policy.workers
            await rig.click_text(".dialog button", "授权模型")
            await until(lambda: "chatgpt:gpt-6.1-sol" in main._fusion.registry.policy.workers)
            assert len(rig.plan.requests) == 3
            await rig.wait_for(".agentpanel-actions .btn")
            await rig.click_text(".agentpanel-actions .btn", "转交专家")
            await rig.click_text(".menu-item", "轻量专家")
            await rig.wait_text(".dialog", "确认转交")
            assert len(main.agents) == 1 and len(rig.plan.requests) == 3
            await rig.click_text(".dialog button", "确认转交")
            await until(lambda: len(main.agents) == 2)
            assert len(rig.plan.requests) == 3 and any(view.record.status == "retired" for view in main.agents.values())
            await rig.click(".fusion-budget")
            await rig.wait_text(".dialog", "专家调用总上限")
            await rig.press("a", Qt.KeyboardModifier.ControlModifier)
            await rig.type("9")
            await rig.click_text(".dialog button", "下一步")
            await rig.wait_text(".dialog", "已知 Token 总上限")
            await rig.press("a", Qt.KeyboardModifier.ControlModifier)
            await rig.type("600000")
            await rig.click_text(".dialog button", "下一步")
            await rig.wait_text(".dialog", "确认本会话预算")
            assert main._fusion.registry.policy.max_calls == 8
            await rig.click_text(".dialog button", "确认预算")
            await until(lambda: main._fusion.registry.policy.max_calls == 9)
            assert main._fusion.registry.policy.max_tokens == 600000 and len(rig.plan.requests) == 3
        finally:
            await rig.finish()
    run(loop, scenario())
