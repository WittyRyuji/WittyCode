"""The real Fusion page handles API effort controls, default resets and selected hidden models."""
from __future__ import annotations

import json

import pytest
from PySide6.QtCore import Qt

from tests.e2e_support import open_rig
from tests.fakes import until
from tests.test_providers import draft, make_store, model
from tests.ui_support import run

pytestmark = pytest.mark.ui


def test_fusion_efforts_are_per_model_and_default_clears_only_that_model(qapp, loop, tmp_path):
    async def scenario():
        store = make_store(tmp_path)
        store.save(draft(name="Demo API", baseUrl="https://example.com/v1", keyMode="none", keyHint="",
                         models=[model("reasoner", displayName="API Reasoner", contextWindow=64000, reasoning="high"),
                                 model("plain", displayName="API Plain", contextWindow=64000, reasoning="")]))
        rig = await open_rig(tmp_path, providers=store, size=(1040, 800))
        try:
            await until(lambda: any(item["label"] == "API Plain" for item in rig.core.catalog.models))
            api = {item["label"]: item["id"] for item in rig.core.catalog.models if item["source"].startswith("api:")}
            reasoner, plain = api["API Reasoner"], api["API Plain"]
            levels = {"chatgpt:gpt-6.1-sol": "high", reasoner: "off"}
            for key, value in {"fusionMain": "chatgpt:gpt-6.1-sol", "fusionWorkers": [reasoner, plain],
                               "fusionLight": "chatgpt:gpt-6-luna", "fusionReasoning": levels}.items():
                await rig.core.call("settings.set", {"key": key, "value": value})
            await rig.core.call("models.setVisible", {"ids": [reasoner], "visible": False})
            await rig.press(",", Qt.KeyboardModifier.ControlModifier)
            await rig.click_text(".snav", "Fusion")
            await rig.wait_for(".fusion-reasoning-row", 4)
            reasoner_selector = f'.fusion-reasoning-row[data-model="{reasoner}"] .selectbox'
            plain_selector = f'.fusion-reasoning-row[data-model="{plain}"] .selectbox'
            await rig.wait_text(reasoner_selector, "Off")
            assert await rig.js(f'document.querySelector({json.dumps(plain_selector)}).disabled')
            assert await rig.text(plain_selector) == "不支持"
            await rig.click(reasoner_selector)
            assert "Max" in await rig.texts(".menu-item .menu-label")
            await rig.click_text(".menu-item", "Max")
            await until(lambda: rig.core.settings.fusion_reasoning.get(reasoner) == "max")
            await rig.click(reasoner_selector)
            await rig.click_text(".menu-item", "默认")
            await until(lambda: rig.core.settings.fusion_reasoning == {"chatgpt:gpt-6.1-sol": "high"})
            await rig.wait_text(reasoner_selector, "默认")
            bounds = json.loads(await rig.js("JSON.stringify([...document.querySelectorAll('.fusion-reasoning-row')].map(e => {"
                                            "const row = e.getBoundingClientRect(), label = e.firstElementChild.getBoundingClientRect(), "
                                            "button = e.querySelector('button').getBoundingClientRect(); "
                                            "return {left: row.left, right: row.right, labelRight: label.right, buttonLeft: button.left}; }))"))
            width = await rig.js("window.innerWidth")
            assert all(0 <= item["left"] < item["right"] <= width and item["labelRight"] <= item["buttonLeft"] for item in bounds)
            assert rig.window.grab().save(str(tmp_path / "fusion-model-efforts.png"))
            assert not rig.plan.requests
        finally:
            await rig.finish()
    run(loop, scenario())
