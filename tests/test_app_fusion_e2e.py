"""Fusion settings, new conversation, and expert panel through the real Qt page."""
from __future__ import annotations

import asyncio
import json
import os
import shutil
import subprocess
import threading
from pathlib import Path

import pytest
from PySide6.QtCore import QRect, Qt

from desktop.core.cache import Cache
from desktop.core.session_state import SessionState
from tests import e2e_support
from tests.e2e_support import open_rig
from tests.fake_responses import call, fail, say
from tests.fakes import until
from tests.ui_support import run, wait_for

pytestmark = pytest.mark.ui

FRONTEND = Path(__file__).resolve().parents[1] / "frontend"


def build_page(tmp_path: Path) -> Path:
    """Build this worktree's page into the test's disposable directory, never the shared app bundle."""
    node = shutil.which("node")
    assert node and (FRONTEND / "node_modules" / "vite").is_dir(), "install the pinned Node and frontend dependencies first"
    env_dir, out_dir = tmp_path / "page-env", tmp_path / "page"
    env_dir.mkdir()
    subprocess.run(
        [node, "--input-type=module", "-e", """
          import { build } from 'vite';
          await build({configLoader: 'runner', envDir: process.argv[1], build: {outDir: process.argv[2], emptyOutDir: false}});
        """, str(env_dir), str(out_dir)],
        cwd=FRONTEND, check=True, timeout=90,
    )
    return out_dir / "index.html"


async def select_fusion(rig):
    await rig.wait_for(".chip-model")
    await rig.click(".chip-model")
    await rig.wait_text(".menu-item .menu-label", "Fusion")
    assert (await rig.texts(".menu-item .menu-label"))[0] == "Fusion"
    assert await rig.text(".menu-head", 0) == "WittyCode"
    assert await rig.js("document.querySelector('.menu-row').previousElementSibling.classList.contains('menu-head') && document.querySelector('.menu-row').nextElementSibling.classList.contains('menu-head')"), "Fusion shares the provider layout, without its own separator"
    await rig.click_text(".menu-item", "Fusion")


def test_factory_catalog_model_can_be_picked_for_each_seat(qapp, loop, tmp_path, monkeypatch):
    from tests.fusion_backend_support import factory_peer

    async def scenario():
        rig = await open_rig(tmp_path, factory_engine=factory_peer(tmp_path, monkeypatch))
        try:
            await select_fusion(rig)
            await rig.wait_text(".settings-body h2", "Fusion")
            for role in ("main", "worker", "light"):
                row = f'.fusion-model-row[data-role="{role}"]'
                await rig.click(row + " .fusion-model-picker .selectbox")
                option = row + " .fusion-model-picker .menu-item:enabled"
                await rig.wait_text(option, "Factory Wire")  # The catalog arrives asynchronously on the page.
                await rig.click_text(option, "Factory Wire")
                await rig.wait_for(row + '[data-model="factory:demo-wire"]')
            s = rig.core.settings
            assert (s.fusion_main, s.fusion_workers, s.fusion_light) == ("factory:demo-wire", ["factory:demo-wire"], "factory:demo-wire")
            if evidence := os.environ.get("WITTYCODE_TEST_EVIDENCE"):
                await rig.shot(".settings-body", Path(evidence) / "factory-seats.png")
            assert not rig.plan.requests and not rig.plan.fake.title_requests
        finally:
            await rig.finish()
    run(loop, scenario())


def test_fusion_settings_multi_pools_and_expert_editor_use_real_bridge(qapp, loop, tmp_path):
    async def scenario():
        rig = await open_rig(tmp_path)
        try:
            await select_fusion(rig)
            await rig.wait_text(".settings-body h2", "Fusion")
            sol, luna = "chatgpt:gpt-6.1-sol", "chatgpt:gpt-6-luna"
            for role, name in (("main", "GPT-6.1-Sol"), ("worker", "GPT-6-Luna"), ("light", "GPT-6-Luna")):
                row = f'.fusion-model-row[data-role="{role}"]'
                await rig.click(row + " .fusion-model-picker .selectbox")
                await rig.click_text(row + " .fusion-model-picker .menu-item", name)
                await rig.wait_for(row + f'[data-model="{sol if role == "main" else luna}"]')

            for role, model, expected in (
                ("worker", "GPT-6.1-Sol", [luna, sol]),
                ("light", "GPT-6.1-Sol", [luna, sol]),
            ):
                await rig.click(f'[data-pool="{role}"] .fusion-model-add .selectbox')
                await rig.click_text(f'[data-pool="{role}"] .fusion-model-add .menu-item', model)
                actual = (lambda role=role: rig.core.settings.fusion_workers if role == "worker" else rig.core.settings.fusion_lights)
                await until(lambda actual=actual, expected=expected: actual() == expected)
            await rig.wait_for(".fusion-model-row", 5)
            assert rig.core.settings.fusion_light == luna  # the legacy scalar remains the first L3 model

            async def enabled_button(label):
                return await rig.js(f"[...document.querySelectorAll('.expert-catalog button')].some(e => e.textContent.includes({json.dumps(label)}) && !e.disabled)")

            await rig.wait_text(".expert-catalog", "修订")
            await wait_for(lambda: enabled_button("新建专家"), 10, "the enabled new-expert button after the initial catalog load")
            await rig.click_text(".expert-catalog button", "新建专家")
            await rig.wait_for('input[aria-label="专家稳定标识"]:enabled')
            await rig.click('input[aria-label="专家稳定标识"]')
            await rig.type("review-ui")
            await rig.click('input[aria-label="专家显示名"]')
            await rig.type("界面检查")
            await rig.click('input[aria-label="专家 Tags"]')
            await rig.type("security api")
            await rig.js("(function(){var e=document.querySelector('textarea[aria-label=\"专家 Markdown 指令\"]'); e.focus(); e.setSelectionRange(e.value.length, e.value.length);})()")
            await rig.type("只读检查界面证据。")
            await wait_for(lambda: enabled_button("创建专家"), 10, "the enabled create button")
            await rig.click_text(".expert-editor button", "创建专家")
            await until(lambda: rig.core.expert_catalog().get("review-ui") is not None)
            entry = rig.core.expert_catalog().get("review-ui")
            assert (entry.version, entry.enabled, entry.tags) == (1, True, ("security", "api"))
            await rig.wait_text(".expert-tag-chips", "security")
            await rig.wait_text(".connection-result", "专家已创建")
            await rig.wait_text(".expert-editor .pstate", "v1 · 启用")
            await rig.wait_for('input[aria-label="专家显示名"]:enabled')

            await rig.click('input[aria-label="专家显示名"]')
            await rig.js("document.querySelector('input[aria-label=\"专家显示名\"]').select()")
            await rig.type("界面复核")
            assert await rig.js("document.querySelector('input[aria-label=\"专家显示名\"]').value") == "界面复核"
            await wait_for(lambda: enabled_button("保存新版本"), 10, "the enabled save button with the edited name")
            await rig.click_text(".expert-editor button", "保存新版本")
            await until(lambda: rig.core.expert_catalog().get("review-ui").version == 2)
            assert rig.core.expert_catalog().get("review-ui").name == "界面复核"
            await rig.wait_text(".connection-result", "已保存版本 2")
            await rig.wait_text(".expert-editor .pstate", "v2 · 启用")

            await wait_for(lambda: enabled_button("停用"), 10, "the enabled disable button after the save finishes")
            await rig.click_text(".expert-editor button", "停用")
            await until(lambda: rig.core.expert_catalog().get("review-ui").version == 3)
            assert not rig.core.expert_catalog().get("review-ui").enabled
            await rig.wait_text(".connection-result", "专家已停用")
            await rig.wait_text(".expert-editor .pstate", "v3 · 停用")
            await wait_for(lambda: enabled_button("启用"), 10, "the enabled enable button after disabling finishes")

            if evidence := os.environ.get("WITTYCODE_TEST_EVIDENCE"):
                await rig.shot(".expert-editor", Path(evidence) / "fusion-expert-settings.png")
            assert not rig.plan.requests and not rig.plan.fake.title_requests
        finally:
            await rig.finish()
    run(loop, scenario())


def test_configure_and_start_fusion_with_a_local_fake(qapp, loop, tmp_path, monkeypatch):
    monkeypatch.setattr(e2e_support, "PAGE_FILE", build_page(tmp_path))
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
            slider = 'input[aria-label="成本与质量倾向"]'
            pref = json.loads(await rig.js("""JSON.stringify((() => {
                const el = document.querySelector('input[aria-label="成本与质量倾向"]');
                return { step: el.step, min: el.min, max: el.max, text: document.querySelector('.fusion-preference').innerText };
            })())"""))
            assert (pref["min"], pref["max"], pref["step"]) == ("0", "100", "1")
            assert "0 · 成本优先" in pref["text"] and "100 · 质量优先" in pref["text"]
            assert "×" not in pref["text"], "the retired cost/quality weights must not be shown as model-order semantics"
            assert "参与派工与审核时的偏好判断" in await rig.text(".settings-body")
            assert "不承诺价格、结果质量、费用上限或账单金额" in await rig.text(".settings-body")

            await rig.click(slider)
            await rig.press(Qt.Key.Key_Home)
            await until(lambda: rig.core.settings.fusion_preference == 0)
            assert await rig.js("document.querySelector('input[aria-label=\"成本与质量倾向\"]').getAttribute('aria-valuetext')") == "0：成本优先"
            await rig.press(Qt.Key.Key_End)
            await until(lambda: rig.core.settings.fusion_preference == 100)
            assert await rig.js("document.querySelector('input[aria-label=\"成本与质量倾向\"]').getAttribute('aria-valuetext')") == "100：质量优先"

            for expected in range(99, 70, -1):
                await rig.press(Qt.Key.Key_Left)
                await until(lambda expected=expected: rig.core.settings.fusion_preference == expected)
            await rig.press(Qt.Key.Key_Right)
            await until(lambda: rig.core.settings.fusion_preference == 72)
            assert await rig.js("document.querySelector('input[aria-label=\"成本与质量倾向\"]').getAttribute('aria-valuetext')") == "72；0 为成本优先，100 为质量优先"
            await rig.wait_text(".fusion-preference-weights", "当前 72")
            await rig.press(Qt.Key.Key_End)
            await until(lambda: rig.core.settings.fusion_preference == 100)
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
            title = await rig.text(".srow.open .stitle")
            assert title.strip() and title != "新会话"
            assert await rig.text(".tab-title") == title
            assert title not in await rig.text(".transcript")
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
            assert "cost x0.50" in rig.plan.requests[0]["instructions"] and "quality x1.50" in rig.plan.requests[0]["instructions"]
            assert len(rig.plan.requests) == 3
            await rig.click(".tab.current .tab-x")
            await rig.wait_text(".srow:not(.open) .stitle", title)
            await rig.click_text(".srow:not(.open)", title)
            await rig.wait_text(".tab-title", title)
            await rig.wait_text(".transcript", "主代理已检查报告")
            assert len(rig.plan.fake.title_requests) == 1
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


@pytest.mark.parametrize("preview", [False, True], ids=["no-preview", "cached-preview"])
def test_devin_fusion_main_and_plan_through_the_real_page(qapp, loop, tmp_path, monkeypatch, preview):
    from tests.fusion_acp_support import make_guarded_engine

    async def scenario():
        harness = make_guarded_engine(tmp_path)
        cache = Cache(tmp_path / "cache") if preview else None
        rig = await open_rig(tmp_path, devin_engine=harness.engine, cache=cache)
        entered, release = threading.Event(), threading.Event()
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
            saved_row = f'.srow:not(.open)[data-session-id="{row_id}"]'
            await rig.wait_text(f"{saved_row} .ssub", "WittyFusion")
            assert (await rig.text(f"{saved_row} .ssub")).split(" · ")[-1] == "WittyFusion"
            if cache is not None:
                await until(lambda: cache.load_session(row_id) is not None)

            load = rig.engine.sessions.load

            def slow_load(native):
                if native == main.session_id:
                    entered.set()
                    assert release.wait(30), "the test did not release the archive read"
                return load(native)

            monkeypatch.setattr(rig.engine.sessions, "load", slow_load)
            await rig.click(saved_row)
            await until(entered.is_set)
            (reopened,) = rig.core.order
            assert reopened.controller.state is SessionState.OPENING and reopened.preview is preview
            row = f'.srow.open[data-session-id="{reopened.cid}"]'
            opening_row = f"{row}.m-opening"
            await rig.wait_for(opening_row)
            label = await rig.text(f"{opening_row} .ssub")
            assert label.startswith("连接中 · ") and label.endswith(" · WittyFusion") and "Devin" not in label, label
            # The DOM is already correct; let Qt paint it while the archive read is still blocked.
            await asyncio.sleep(0.2)
            # Keep only the synthetic row as evidence, not the parent project's local path.
            bounds = json.loads(await rig.js(f'''JSON.stringify((() => {{
                const r = document.querySelector({json.dumps(opening_row)}).getBoundingClientRect();
                return [Math.floor(r.x), Math.floor(r.y), Math.ceil(r.width), Math.ceil(r.height)];
            }})())'''))
            assert rig.window.view.grab(QRect(*bounds)).save(str(tmp_path / "fusion-sidebar-connecting.png"))
            release.set()
            await rig.wait_text(f"{row}.m-ready .ssub", "已打开")
            ready_label = await rig.text(f"{row} .ssub")
            assert ready_label.endswith(" · WittyFusion") and "Devin" not in ready_label, ready_label
            assert reopened.controller.session_id == main.session_id and reopened.controller.model == "devin:swe-2-high"
            assert not rig.plan.requests
        finally:
            release.set()
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


def test_the_step_band_saves_as_one_group_in_fusion_settings(qapp, loop, tmp_path):
    from desktop.core.app_core import AppError

    async def scenario():
        rig = await open_rig(tmp_path)
        try:
            await rig.press(Qt.Key.Key_Comma, Qt.KeyboardModifier.ControlModifier)
            await rig.wait_for(".settings")
            await rig.click_text(".snav", "Fusion")
            steps = '[role="group"][aria-label="委派步数范围"]'
            seconds = '[role="group"][aria-label="单个专家最长运行秒数"]'
            button = steps + " .step-save"
            await rig.wait_for(button, 1)
            await rig.wait_for(seconds + " .step-save", 1)
            box = steps + ' input[aria-label="委派最小步数 Adaptive"]'
            low = steps + ' input[aria-label="委派最小步数"]'
            hi_box = steps + ' input[aria-label="委派最大步数 Adaptive"]'
            high = steps + ' input[aria-label="委派最大步数"]'
            state = f"JSON.stringify([document.querySelector({json.dumps(box)}).checked," \
                    f"document.querySelector({json.dumps(hi_box)}).checked," \
                    f"document.querySelector({json.dumps(low)}).disabled, document.querySelector({json.dumps(low)}).value," \
                    f"document.querySelector({json.dumps(high)}).disabled, document.querySelector({json.dumps(high)}).value])"
            assert json.loads(await rig.js(state)) == [True, True, True, "16", True, "32"], "both sides adaptive, inputs off, base numbers"
            adaptive_bg = await rig.js(f"getComputedStyle(document.querySelector({json.dumps(low)})).backgroundColor")
            if evidence := os.environ.get("WITTYCODE_TEST_EVIDENCE"):
                await rig.js(f"document.querySelector({json.dumps(steps)}).scrollIntoView()")
                await rig.shot(".settings", Path(evidence) / "fusion-step-budget.png")
            # Unchecking lets the user type; checking again remembers the typed number.
            await rig.click(box)
            assert json.loads(await rig.js(state))[:3] == [False, True, False], "each side's Adaptive is independent"
            manual_bg = await rig.js(f"getComputedStyle(document.querySelector({json.dumps(low)})).backgroundColor")
            assert manual_bg != adaptive_bg, "a manual number box is visibly different from the adaptive grey"
            await rig.click(low)
            await rig.press(Qt.Key.Key_A, Qt.KeyboardModifier.ControlModifier)
            await rig.type("60")
            await rig.click(box)
            await rig.click(box)
            assert await rig.js(f'document.querySelector({json.dumps(low)}).value') == "60", "the typed number is remembered"
            # A both-manual inversion disables the save; a manual floor beside an adaptive ceiling does not.
            await rig.click(hi_box)
            await rig.click(high)
            await rig.press(Qt.Key.Key_A, Qt.KeyboardModifier.ControlModifier)
            await rig.type("30")
            assert await rig.js(f'document.querySelector({json.dumps(button)}).disabled')
            await rig.click(hi_box)  # back to adaptive: no inversion, save works again
            assert not await rig.js(f'document.querySelector({json.dumps(button)}).disabled')
            await rig.click(hi_box)
            await rig.click(high)
            await rig.press(Qt.Key.Key_A, Qt.KeyboardModifier.ControlModifier)
            await rig.type("90")
            # The grid lines both rows up; the save key sits under the number column at the same x, width and height.
            rect = await rig.js(
                "JSON.stringify((function(){"
                f"var n=document.querySelector({json.dumps(low)}),h=document.querySelector({json.dumps(high)}),"
                f"c=document.querySelector({json.dumps(box)}),k=document.querySelector({json.dumps(hi_box)}),"
                f"b=document.querySelector({json.dumps(button)});"
                "var rn=n.getBoundingClientRect(),rh=h.getBoundingClientRect(),rc=c.getBoundingClientRect(),"
                "rk=k.getBoundingClientRect(),rb=b.getBoundingClientRect();"
                "return [rn.x,rh.x,rc.x,rk.x,rb.x,rn.width,rb.width,rn.height,rb.height];})())")
            nx, hx, cx, kx, bx, nw, bw, nh, bh = json.loads(rect)
            assert abs(nx - hx) <= 1 and abs(cx - kx) <= 1, "the two Adaptive rows line up"
            assert abs(bx - nx) <= 1 and abs(bw - nw) <= 1 and abs(bh - nh) <= 1, "the save key matches the number box"
            # Both saves can be enabled at once; applying one group must not commit the other's draft.
            await rig.click(seconds + ' input[aria-label="最长秒数 Adaptive"]')
            await rig.wait_for(seconds + " .step-save:enabled", 1)
            await rig.click(button)
            await until(lambda: rig.core.settings.fusion_steps_min == 60 and not rig.core.settings.fusion_steps_min_adaptive
                        and rig.core.settings.fusion_steps_max == 90 and not rig.core.settings.fusion_steps_max_adaptive)
            assert rig.core.settings.fusion_max_seconds_adaptive
            await rig.click(seconds + " .step-save:enabled")
            await until(lambda: not rig.core.settings.fusion_max_seconds_adaptive)
            # The backend refuses a crafted bad group on its own and keeps the last valid one.
            for value in ({"min": 91, "minAdaptive": False, "max": 90, "maxAdaptive": False},
                          {"min": 5, "minAdaptive": False, "max": 15}):
                with pytest.raises(AppError):
                    await rig.core.call("settings.set", {"key": "fusionSteps", "value": value})
            assert (rig.core.settings.fusion_steps_min, rig.core.settings.fusion_steps_max) == (60, 90)
            await rig.press("Escape")
            await rig.press(Qt.Key.Key_Comma, Qt.KeyboardModifier.ControlModifier)
            await rig.wait_for(".settings")
            await rig.click_text(".snav", "Fusion")
            await rig.wait_for(button, 1)
            assert json.loads(await rig.js(state)) == [False, False, False, "60", False, "90"]
        finally:
            await rig.finish()
    run(loop, scenario())
