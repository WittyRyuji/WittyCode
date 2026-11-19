"""The real Fusion page handles API effort controls, default resets and selected hidden models."""
from __future__ import annotations

import json

import pytest
from PySide6.QtCore import Qt

from tests.e2e_support import open_rig
from tests.fakes import until
from tests.test_providers import draft, make_store, model
from tests.ui_support import run, wait_for

pytestmark = pytest.mark.ui


@pytest.mark.parametrize("size", [(900, 600), (1320, 860)])
def test_fusion_efforts_are_per_model_and_default_clears_only_that_model(qapp, loop, tmp_path, size):
    async def scenario():
        store = make_store(tmp_path)
        store.save(draft(name="Demo API", baseUrl="https://example.com/v1", keyMode="none", keyHint="",
                         models=[model("reasoner", displayName="API Reasoner With A Long Model Name", contextWindow=64000, reasoning="high"),
                                 model("plain", displayName="API Plain", contextWindow=64000, reasoning="")]))
        rig = await open_rig(tmp_path, providers=store, size=size)
        try:
            await until(lambda: any(item["label"] == "API Plain" for item in rig.core.catalog.models))
            api = {item["label"]: item["id"] for item in rig.core.catalog.models if item["source"].startswith("api:")}
            reasoner, plain = api["API Reasoner With A Long Model Name"], api["API Plain"]
            levels = {"main:chatgpt:gpt-6.1-sol": "high", f"worker:{reasoner}": "off"}
            for key, value in {"fusionMain": "chatgpt:gpt-6.1-sol", "fusionWorkers": [reasoner],
                               "fusionLight": plain, "fusionReasoning": levels,
                               "fontScale": "lg" if size[0] == 900 else "md"}.items():
                await rig.core.call("settings.set", {"key": key, "value": value})
            await rig.core.call("models.setVisible", {"ids": [reasoner], "visible": False})
            await rig.press(",", Qt.KeyboardModifier.ControlModifier)
            await rig.click_text(".snav", "Fusion")
            await rig.wait_for(".fusion-model-row", 3)
            assert not await rig.exists(".fusion-reasoning")
            assert "各模型思考等级" not in await rig.text(".settings-body")
            reasoner_selector = f'.fusion-model-row[data-model="{reasoner}"] .fusion-effort-picker .selectbox'
            plain_selector = f'.fusion-model-row[data-model="{plain}"] .fusion-effort-picker .selectbox'
            await rig.wait_text(reasoner_selector, "Off")
            assert await rig.js(f'(() => {{ const label = document.querySelector(\'.fusion-model-row[data-model="{reasoner}"] .fusion-model-picker .selectbox > span\'); return label.scrollWidth > label.clientWidth && getComputedStyle(label).textOverflow === "ellipsis"; }})()')
            assert await rig.js(f'document.querySelector({json.dumps(plain_selector)}).disabled')
            assert await rig.text(plain_selector) == "不支持"
            await rig.click(reasoner_selector)
            assert "Max" in await rig.texts(".menu-item .menu-label")
            await wait_for(lambda: rig.js("[...document.querySelectorAll('.settings, .menu')].every(e => e.getAnimations({subtree: true}).length === 0)"))
            await rig.click_text(".menu-item", "Max")
            await until(lambda: rig.core.settings.fusion_reasoning.get(f"worker:{reasoner}") == "max")
            await rig.click(reasoner_selector)
            await rig.click_text(".menu-item", "默认")
            await until(lambda: rig.core.settings.fusion_reasoning == {"main:chatgpt:gpt-6.1-sol": "high"})
            await rig.wait_text(reasoner_selector, "默认")
            bounds = json.loads(await rig.js("JSON.stringify([...document.querySelectorAll('.fusion-model-row')].map(e => {"
                                            "const row = e.getBoundingClientRect(), buttons = e.querySelectorAll('.selectbox'), "
                                            "model = buttons[0].getBoundingClientRect(), effort = buttons[1].getBoundingClientRect(); "
                                            "return {count: buttons.length, left: row.left, right: row.right, modelRight: model.right, "
                                            "effortLeft: effort.left, modelTop: model.top, effortTop: effort.top}; }))"))
            width = await rig.js("window.innerWidth")
            assert all(item["count"] == 2 and 0 <= item["left"] < item["right"] <= width
                       and item["modelRight"] <= item["effortLeft"] and abs(item["modelTop"] - item["effortTop"]) < 1 for item in bounds)
            (tmp_path / "fusion-inline-bounds.json").write_text(json.dumps(bounds, indent=2), encoding="utf-8")
            assert rig.window.grab().save(str(tmp_path / "fusion-model-efforts.png"))
            assert not rig.plan.requests
        finally:
            await rig.finish()
    run(loop, scenario())


def test_worker_is_one_fixed_selector_and_preserves_independent_role_efforts(qapp, loop, tmp_path):
    async def scenario():
        rig = await open_rig(tmp_path)
        try:
            await rig.press(",", Qt.KeyboardModifier.ControlModifier)
            await rig.click_text(".snav", "Fusion")
            await rig.wait_for(".fusion-model-row", 3)
            assert await rig.count(".fusion-effort-picker .selectbox:disabled") == 3
            assert await rig.texts(".fusion-effort-picker .selectbox") == ["思考等级"] * 3
            for role in ("main", "worker", "light"):
                await rig.click(f'.fusion-model-row[data-role="{role}"] .fusion-model-picker .selectbox')
                await rig.click_text(".menu-item", "GPT-6.1-Sol")
            await until(lambda: rig.core.settings.fusion_main == rig.core.settings.fusion_light == "chatgpt:gpt-6.1-sol"
                        and rig.core.settings.fusion_workers == ["chatgpt:gpt-6.1-sol"])
            worker = '.fusion-model-row[data-role="worker"][data-model="chatgpt:gpt-6.1-sol"]'
            await rig.click(worker + " .fusion-effort-picker .selectbox")
            await rig.click_text(".menu-item", "High")
            await until(lambda: rig.core.settings.fusion_reasoning == {"worker:chatgpt:gpt-6.1-sol": "high"})
            assert await rig.texts(".fusion-effort-picker .selectbox") == ["默认", "High", "默认"]
            await rig.click(worker + " .fusion-model-picker .selectbox")
            await rig.click_text(".menu-item", "GPT-6-Luna")
            await rig.wait_for('.fusion-model-row[data-role="worker"]', 1)
            await until(lambda: rig.core.settings.fusion_workers == ["chatgpt:gpt-6-luna"])
            luna = '.fusion-model-row[data-role="worker"][data-model="chatgpt:gpt-6-luna"]'
            await rig.click(luna + " .fusion-effort-picker .selectbox")
            await rig.click_text(".menu-item", "Low")
            await until(lambda: rig.core.settings.fusion_reasoning.get("worker:chatgpt:gpt-6-luna") == "low")
            assert await rig.texts('.fusion-model-row[data-role="worker"] .fusion-model-picker .selectbox') == ["GPT-6-Luna"]
            await rig.click('.fusion-model-row[data-role="main"] .fusion-effort-picker .selectbox')
            await rig.click_text(".menu-item", "Medium")
            await until(lambda: rig.core.settings.fusion_reasoning.get("main:chatgpt:gpt-6.1-sol") == "medium")
            await rig.click('.fusion-model-row[data-role="light"] .fusion-effort-picker .selectbox')
            await rig.click_text(".menu-item", "Low")
            await until(lambda: rig.core.settings.fusion_reasoning.get("light:chatgpt:gpt-6.1-sol") == "low")
            assert await rig.texts(".fusion-effort-picker .selectbox") == ["Medium", "Low", "Low"]
            assert rig.window.grab().save(str(tmp_path / "fusion-inline-workers.png"))
            await rig.click('.settings-body button[aria-label="关闭"]')
            await rig.press(",", Qt.KeyboardModifier.ControlModifier)
            await rig.click_text(".snav", "Fusion")
            await rig.wait_text('.fusion-model-row[data-role="main"] .fusion-effort-picker .selectbox', "Medium")
            assert await rig.texts(".fusion-effort-picker .selectbox") == ["Medium", "Low", "Low"]
            await rig.click(luna + " .fusion-model-picker .selectbox")
            await rig.click_text(".menu-item", "GPT-6-Luna")
            assert rig.core.settings.fusion_workers == ["chatgpt:gpt-6-luna"], "reselecting does not remove the worker"
            await rig.wait_for('.fusion-model-row[data-role="worker"]', 1)
            await rig.click('.fusion-model-row[data-role="light"] .fusion-effort-picker .selectbox')
            await rig.click_text(".menu-item", "默认")
            await until(lambda: "light:chatgpt:gpt-6.1-sol" not in rig.core.settings.fusion_reasoning)
            assert await rig.texts(".fusion-effort-picker .selectbox") == ["Medium", "Low", "默认"]
            await rig.click(luna + " .fusion-model-picker .selectbox")
            await rig.click_text(".menu-item", "GPT-6.1-Sol")
            await until(lambda: rig.core.settings.fusion_workers == ["chatgpt:gpt-6.1-sol"])
            await rig.wait_text(worker + " .fusion-effort-picker .selectbox", "High")
            assert await rig.count('.fusion-model-row[data-role="worker"]') == 1
            await rig.click(worker + " .fusion-model-picker .selectbox")
            await rig.click_text(".menu-item", "GPT-6.1-Sol")
            assert rig.core.settings.fusion_workers == ["chatgpt:gpt-6.1-sol"]
            await rig.click('.fusion-model-row[data-role="worker"] .fusion-model-picker .selectbox')
            await rig.click_text(".menu-item", "GPT-6-Luna")
            await rig.wait_text(luna + " .fusion-effort-picker .selectbox", "Low")
            assert rig.core.settings.fusion_workers == ["chatgpt:gpt-6-luna"]
            assert not rig.plan.requests
        finally:
            await rig.finish()
    run(loop, scenario())
