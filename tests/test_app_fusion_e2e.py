"""Fusion settings, new conversation, and expert panel through the real Qt page."""
from __future__ import annotations

import pytest
from PySide6.QtCore import Qt

from tests.e2e_support import open_rig
from tests.fake_responses import call, say
from tests.fakes import until
from tests.ui_support import run

pytestmark = pytest.mark.ui


async def select_fusion(rig):
    await rig.wait_for(".chip-model")
    await rig.click(".chip-model")
    await rig.wait_text(".menu-item .menu-label", "Fusion")
    assert (await rig.texts(".menu-item .menu-label"))[0] == "Fusion"
    await rig.click_text(".menu-item", "Fusion")


def test_configure_and_start_fusion_with_a_local_fake(qapp, loop, tmp_path):
    async def scenario():
        rig = await open_rig(tmp_path)
        try:
            await select_fusion(rig)  # The incomplete setup opens settings instead of silently enabling Fusion.
            await rig.wait_text(".settings-body h2", "Fusion")
            for index, name in [(0, "GPT-6.1-Sol"), (1, "GPT-6-Luna"), (2, "GPT-6-Luna")]:
                await rig.click(".settings-body .selectbox", index)
                await rig.wait_text(".menu-item", name)
                await rig.click_text(".menu-item", name)
            await until(lambda: rig.core.settings.fusion_main == "chatgpt:gpt-6.1-sol" and rig.core.settings.fusion_light == "chatgpt:gpt-6-luna" and rig.core.settings.fusion_workers == ["chatgpt:gpt-6-luna"])
            await rig.wait_for(".fusion-reasoning-row", 2)  # The worker and lightweight role share one model.
            for model_id, level in [("chatgpt:gpt-6.1-sol", "High"), ("chatgpt:gpt-6-luna", "Low")]:
                await rig.click(f'.fusion-reasoning-row[data-model="{model_id}"] .selectbox')
                if model_id.endswith("luna"):
                    assert await rig.texts(".menu-item .menu-label") == ["默认", "Low"]
                await rig.click_text(".menu-item", level)
            levels = {"chatgpt:gpt-6.1-sol": "high", "chatgpt:gpt-6-luna": "low"}
            await until(lambda: rig.core.settings.fusion_reasoning == levels)
            await rig.click('.settings-body button[aria-label="关闭"]')
            await rig.press(",", Qt.KeyboardModifier.ControlModifier)
            await rig.click_text(".snav", "Fusion")
            await rig.wait_text('.fusion-reasoning-row[data-model="chatgpt:gpt-6.1-sol"] .selectbox', "High")
            await rig.wait_text('.fusion-reasoning-row[data-model="chatgpt:gpt-6-luna"] .selectbox', "Low")
            assert not await rig.exists('input[aria-label="专家调用次数"]')
            assert not await rig.exists('input[aria-label="Token 预算"]')
            await rig.click('input[aria-label="成本与效率倾向"]')
            await rig.press("End")
            await until(lambda: rig.core.settings.fusion_preference == 100)
            await rig.wait_text(".fusion-preference-weights", "效率 ×1.50")
            assert rig.window.grab().save(str(tmp_path / "fusion-settings.png"))
            await rig.click('.settings-body button[aria-label="关闭"]')
            await select_fusion(rig)
            await rig.wait_text(".chip-model .chip-v", "Fusion")
            await rig.wait_text(".chip-reasoning .chip-v", "High")
            assert await rig.js('document.querySelector(".chip-reasoning").disabled')
            assert not await rig.exists(".hint .fusion-choice, .fusion-badge")
            rig.plan.script(call("delegate_task", {"scope": "src", "model": "chatgpt:gpt-6-luna", "role": "worker", "task": "Inspect the module."}),
                            say("专家检查完成。"), say("主代理已检查报告。"))
            await rig.send("请检查模块")
            await rig.wait_text(".chip-model .chip-v", "Fusion")
            await rig.wait_text(".agent-chip", "src")
            await rig.wait_text(".transcript", "主代理已检查报告")
            assert len(rig.core.order) == 1
            assert [request["model"] for request in rig.plan.requests] == ["gpt-6.1-sol", "gpt-6-luna", "gpt-6.1-sol"]
            assert [request["reasoning"]["effort"] for request in rig.plan.requests] == ["high", "low", "high"]
            assert rig.core.order[0].controller._fusion.registry.policy.reasoning == levels
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
            assert not await rig.exists(".fusion-budget")
            assert (main._fusion.registry.policy.max_calls, main._fusion.registry.policy.max_tokens, main._fusion.registry.policy.preference) == (0, 0, 100)
            assert "cost x0.50" in rig.plan.requests[0]["instructions"] and "efficiency x1.50" in rig.plan.requests[0]["instructions"]
            assert len(rig.plan.requests) == 3
        finally:
            await rig.finish()
    run(loop, scenario())


def test_devin_fusion_main_and_plan_through_the_real_page(qapp, loop, tmp_path):
    from tests.fusion_acp_support import make_guarded_engine

    async def scenario():
        harness = make_guarded_engine(tmp_path)
        rig = await open_rig(tmp_path, devin_engine=harness.engine)
        try:
            await until(lambda: any(model["source"] == "devin" for model in rig.core.catalog.models), timeout=20)
            for key, value in {"fusionMain": "devin:swe-2-high", "fusionWorkers": ["chatgpt:gpt-6-luna"], "fusionLight": "chatgpt:gpt-6-luna"}.items():
                await rig.core.call("settings.set", {"key": key, "value": value})
            await select_fusion(rig)
            await rig.send("Inspect this module")
            await rig.wait_text(".chip-model .chip-v", "Fusion")
            await rig.wait_text(".transcript", "ACP report.", timeout=30)
            main = rig.core.order[0].controller
            await until(lambda: main.state.value == "ready")
            assert main.model == "devin:swe-2-high" and not rig.plan.requests
            await rig.core.call("session.choose", {"id": rig.core.order[0].cid, "field": "mode", "value": "spec"})
            await rig.send("[fusion-plan] Propose a plan")
            await rig.wait_for(".plan-approval", timeout=30)
            assert main.saved.plan["status"] == "pending"
            assert rig.window.grab().save(str(tmp_path / "fusion-acp-main-plan.png"))
            await rig.click_text(".plan-approval button", "执行方案")
            await until(lambda: main.state.value == "ready" and main.saved.plan.get("status") == "approved", timeout=30)
            assert main.saved.plan["execution"] == "started" and not rig.plan.requests
        finally:
            await rig.finish()
    run(loop, scenario())


def test_fusion_model_menus_respect_visibility_and_include_devin(qapp, loop, tmp_path):
    import json
    from desktop.core.providers import ProviderStore
    from tests.devin_support import make_engine

    async def scenario():
        harness = make_engine(tmp_path)
        path = tmp_path / "models.json"
        path.write_text(json.dumps({"customModels": [{"model": "hidden-demo", "displayName": "Hidden API Demo",
                         "baseUrl": "https://example.com/v1", "provider": "generic-chat-completion-api", "maxContextLimit": 64000}]}), encoding="utf-8")
        providers = ProviderStore(models_file=path, sidecar=tmp_path / "providers.json", environ={}, registry_env=lambda: {})
        rig = await open_rig(tmp_path, devin_engine=harness.engine, providers=providers)
        try:
            await until(lambda: any(model["source"] == "devin" for model in rig.core.catalog.models)
                        and any(model["label"] == "Hidden API Demo" for model in rig.core.catalog.models), timeout=20)
            api_id = next(model["id"] for model in rig.core.catalog.models if model["label"] == "Hidden API Demo")
            await rig.core.call("models.setVisible", {"ids": ["chatgpt:gpt-6-luna", api_id], "visible": False})
            await select_fusion(rig)
            await rig.wait_text(".settings-body h2", "Fusion")
            await rig.click(".settings-body .selectbox", 0)
            assert "GPT-6-Luna" not in await rig.text(".menu") and "Hidden API Demo" not in await rig.text(".menu")
            await rig.wait_text(".menu-item", "SWE-2")
            assert not await rig.js("[...document.querySelectorAll('.menu-item')].find(e => e.textContent.includes('SWE-2')).disabled")
            await rig.click_text(".menu-item", "SWE-2")
            await until(lambda: rig.core.settings.fusion_main == "devin:swe-2-high")
            await rig.click(".settings-body h2")
            await rig.click(".settings-body .selectbox", 1)
            await rig.wait_text(".menu-item", "SWE-2")
            assert "GPT-6-Luna" not in await rig.text(".menu") and "Hidden API Demo" not in await rig.text(".menu")
            assert not await rig.js("[...document.querySelectorAll('.menu-item')].find(e => e.textContent.includes('SWE-2')).disabled")
            await rig.click_text(".menu-item", "SWE-2")
            await until(lambda: rig.core.settings.fusion_workers == ["devin:swe-2-high"])
            assert not harness.requests("session/prompt")
            assert rig.window.grab().save(str(tmp_path / "fusion-models.png"))
        finally:
            await rig.finish()
    run(loop, scenario())
