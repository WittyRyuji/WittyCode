"""Fusion settings, new conversation, and expert panel through the real Qt page."""
from __future__ import annotations

import pytest
from PySide6.QtCore import Qt

from tests.e2e_support import open_rig
from tests.fake_responses import call, fail, say
from tests.fakes import until
from tests.ui_support import run

pytestmark = pytest.mark.ui


async def select_fusion(rig):
    await rig.wait_for(".chip-model")
    await rig.click(".chip-model")
    await rig.wait_text(".menu-item .menu-label", "Fusion")
    assert (await rig.texts(".menu-item .menu-label"))[0] == "Fusion"
    assert await rig.text(".menu-head", 0) == "WittyCode"
    assert await rig.js("document.querySelector('.menu-row').previousElementSibling.classList.contains('menu-head') && document.querySelector('.menu-row').nextElementSibling.classList.contains('menu-head')"), "Fusion shares the provider layout, without its own separator"
    await rig.click_text(".menu-item", "Fusion")


def test_configure_and_start_fusion_with_a_local_fake(qapp, loop, tmp_path):
    async def scenario():
        rig = await open_rig(tmp_path)
        try:
            await select_fusion(rig)  # The incomplete setup opens settings instead of silently enabling Fusion.
            await rig.wait_text(".settings-body h2", "Fusion")
            for role, name in [("main", "GPT-6.1-Sol"), ("worker", "GPT-6-Luna"), ("light", "GPT-6-Luna")]:
                await rig.click(f'.fusion-model-row[data-role="{role}"] .fusion-model-picker .selectbox')
                await rig.wait_text(".menu-item", name)
                await rig.click_text(".menu-item", name)
            await until(lambda: rig.core.settings.fusion_main == "chatgpt:gpt-6.1-sol" and rig.core.settings.fusion_light == "chatgpt:gpt-6-luna" and rig.core.settings.fusion_workers == ["chatgpt:gpt-6-luna"])
            await rig.wait_for(".fusion-model-row", 3)
            for role, level in [("main", "High"), ("worker", "Low"), ("light", "Low")]:
                await rig.click(f'.fusion-model-row[data-role="{role}"] .fusion-effort-picker .selectbox')
                if role == "worker":
                    assert await rig.texts(".menu-item .menu-label") == ["默认", "Low"]
                await rig.click_text(".menu-item", level)
            await rig.wait_text('.fusion-model-row[data-role="light"] .fusion-effort-picker .selectbox', "Low")
            levels = {"main:chatgpt:gpt-6.1-sol": "high", "worker:chatgpt:gpt-6-luna": "low", "light:chatgpt:gpt-6-luna": "low"}
            await until(lambda: rig.core.settings.fusion_reasoning == levels)
            await rig.click('.settings-body button[aria-label="关闭"]')
            await rig.press(",", Qt.KeyboardModifier.ControlModifier)
            await rig.click_text(".snav", "Fusion")
            await rig.wait_text('.fusion-model-row[data-role="main"] .fusion-effort-picker .selectbox', "High")
            await rig.wait_text('.fusion-model-row[data-role="worker"] .fusion-effort-picker .selectbox', "Low")
            await rig.wait_text('.fusion-model-row[data-role="light"] .fusion-effort-picker .selectbox', "Low")
            assert not await rig.exists(".fusion-reasoning")
            assert "各模型思考等级" not in await rig.text(".settings-body")
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
            assert not await rig.exists(".chip-reasoning")
            assert not await rig.exists(".hint .fusion-choice, .fusion-badge")
            rig.plan.script(call("delegate_task", {"scope": "src", "model": "chatgpt:gpt-6-luna", "role": "worker", "task": "Inspect the module."}),
                            say("专家检查完成。"), say("主代理已检查报告。"))
            await rig.send("请检查模块")
            await rig.wait_text(".chip-model .chip-v", "Fusion")
            await rig.wait_text(".transcript", "主代理已检查报告")
            await rig.wait_for(".agent-history-toggle")
            await rig.click(".agent-history-toggle")
            await rig.wait_text(".agent-chip", "src")
            assert len(rig.core.order) == 1
            assert [request["model"] for request in rig.plan.requests] == ["gpt-6.1-sol", "gpt-6-luna", "gpt-6.1-sol"]
            assert [request["reasoning"]["effort"] for request in rig.plan.requests] == ["high", "low", "high"]
            assert rig.core.order[0].controller._fusion.registry.policy.reasoning == levels
            await rig.click(".agent-chip")
            await rig.wait_text(".agentpanel", "专家检查完成")
            assert rig.window.grab().save(str(tmp_path / "fusion-expert.png"))
            main = rig.core.order[0].controller
            await until(lambda: main.state.value == "ready")
            assert not await rig.exists(".chip-reasoning")
            assert "更换主模型" not in await rig.text(".hint") and "授权模型" not in await rig.text(".hint")
            await rig.click(".chip-model")
            await rig.click('.menu-configure[aria-label="Fusion 设置"]')
            await rig.wait_text(".settings-body h2", "Fusion")
            await rig.click('.fusion-model-row[data-role="main"] .fusion-model-picker .selectbox')
            await rig.click_text(".menu-item", "GPT-6-Luna")
            await until(lambda: rig.core.settings.fusion_main == "chatgpt:gpt-6-luna")
            assert main.model == "gpt-6.1-sol" and main._fusion.registry.policy.main == "chatgpt:gpt-6.1-sol"
            assert "chatgpt:gpt-6.1-sol" not in main._fusion.registry.policy.workers
            assert rig.window.grab().save(str(tmp_path / "fusion-settings-shortcut.png"))
            await rig.click('.settings-body button[aria-label="关闭"]')
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


@pytest.mark.parametrize(("gesture", "outcome"), [("enter", "done"), ("click", "done"), ("click", "stop"), ("enter", "fail")])
def test_submitted_message_leaves_composer_while_fusion_handoff_is_blocked(qapp, loop, tmp_path, gesture, outcome):
    async def scenario():
        rig = await open_rig(tmp_path)
        try:
            rig.plan.script(say("Original response."))
            await rig.send("Source conversation")
            await rig.wait_text(".transcript", "Original response.")
            main = rig.core.order[0].controller
            await until(lambda: main.state.value == "ready")
            for key, value in {"fusionMain": "chatgpt:gpt-6-luna", "fusionWorkers": ["chatgpt:gpt-6.1-sol"],
                               "fusionLight": "chatgpt:gpt-6.1-sol"}.items():
                await rig.core.call("settings.set", {"key": key, "value": value})
            await select_fusion(rig)
            await rig.wait_text(".chip-model .chip-v", "Fusion")
            attachment = tmp_path / "handoff.txt"
            attachment.write_text("Attached to the new request, not its summary.", encoding="utf-8")
            await rig.drop_files([attachment])
            await rig.wait_for(".att-row .att", 1)
            summary = fail("invalid_request", "Summary unavailable") if outcome == "fail" else say("Handoff summary.")
            rig.plan.script([("hold", "handoff"), *summary], say("Destination response."))
            await rig.focus_composer()
            await rig.type("Submitted user request")
            if gesture == "enter":
                await rig.press("Enter")
            else:
                await rig.click('button[aria-label="发送"]')
            await rig.wait_text(".preparing-turn .body", "Submitted user request")
            await rig.wait_text(".preparing-turn [role=status]", "正在压缩并切换模型")
            await rig.wait_text(".preparing-turn .fname", "handoff.txt")
            assert await rig.js("document.querySelector('.input').value") == ""
            assert not await rig.exists(".att-row .att")
            await until(lambda: len(rig.plan.requests) == 2)
            assert main.model == "gpt-6.1-sol" and len(main.transcript.turns) == 1
            assert "Submitted user request" not in str(rig.plan.requests[1])
            assert "Attached to the new request" not in str(rig.plan.requests[1])
            assert rig.window.grab().save(str(tmp_path / "fusion-preparing.png"))

            # Same text typed again on purpose must not be deleted a second time when the pending RPC replies.
            followup = "Submitted user request" if outcome == "done" else "A new draft during the handoff"
            await rig.focus_composer()
            await rig.type(followup)
            await rig.press("Enter")
            assert await rig.js("document.querySelector('.input').value") == followup
            assert len(rig.plan.requests) == 2 and not await rig.exists(".send.how")
            if outcome == "stop":
                await rig.click(".send.stop")
            else:
                rig.plan.release("handoff")
            await rig.wait_for(".preparing-turn", 0)
            if outcome == "done":
                await rig.wait_text(".text-block", "Destination response.")
                assert await rig.js("document.querySelector('.input').value") == followup
                assert [r["model"] for r in rig.plan.requests] == ["gpt-6.1-sol", "gpt-6.1-sol", "gpt-6-luna"]
                assert (await rig.texts(".block.user .body")).count("Submitted user request") == 1
            else:
                await rig.wait_for(".att-row .att", 1)
                assert await rig.js("document.querySelector('.input').value") == "Submitted user request\n\n" + followup
                assert await rig.texts(".att-row .fname") == ["handoff.txt"]
                assert len(rig.plan.requests) == 2 and len(main.transcript.turns) == 1
                assert rig.core.order[0].pending_model["model"] == "fusion"
        finally:
            rig.plan.release("handoff")
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
            open_sidebar = ".srow.open .ssub"
            await rig.wait_text(open_sidebar, "WittyFusion")
            assert (await rig.text(open_sidebar)).split(" · ")[-1] == "WittyFusion"
            await rig.core.call("session.choose", {"id": rig.core.order[0].cid, "field": "mode", "value": "spec"})
            await rig.send("[fusion-plan] Propose a plan")
            await rig.wait_for(".plan-approval", timeout=30)
            assert main.saved.plan["status"] == "pending"
            assert rig.window.grab().save(str(tmp_path / "fusion-acp-main-plan.png"))
            await rig.click_text(".plan-approval button", "执行方案")
            await until(lambda: main.state.value == "ready" and main.saved.plan.get("status") == "approved", timeout=30)
            assert main.saved.plan["execution"] == "started" and not rig.plan.requests
            await rig.core.call("session.close", {"id": rig.core.order[0].cid})
            row_id = f"cg:{main.session_id}"
            await until(lambda: row_id in rig.core._saved_sessions_json())
            saved = rig.core._saved_sessions_json()[row_id]
            assert saved["fusion"] is True and saved["settings"]["model"] == "devin:swe-2-high"
            saved_sidebar = f'.srow:not(.open)[data-session-id="{row_id}"] .ssub'
            await rig.wait_for(saved_sidebar)
            assert (await rig.text(saved_sidebar)).split(" · ")[-1] == "WittyFusion"
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
            await rig.click('.fusion-model-row[data-role="main"] .fusion-model-picker .selectbox')
            assert "GPT-6-Luna" not in await rig.text(".menu") and "Hidden API Demo" not in await rig.text(".menu")
            await rig.wait_text(".menu-item", "SWE-2")
            assert not await rig.js("[...document.querySelectorAll('.menu-item')].find(e => e.textContent.includes('SWE-2')).disabled")
            await rig.click_text(".menu-item", "SWE-2")
            await until(lambda: rig.core.settings.fusion_main == "devin:swe-2-high")
            await rig.click(".settings-body h2")
            await rig.click('.fusion-model-row[data-role="worker"] .fusion-model-picker .selectbox')
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
