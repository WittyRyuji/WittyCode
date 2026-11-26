"""The real Fusion page handles API effort controls, default resets and selected hidden models."""
from __future__ import annotations

import json
import os
from pathlib import Path

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
        long_name = "DeepSeek Flash Reasoner With A Very Long Model Name For Every Fusion Seat"
        short_name = "DeepSeek Flash"
        store = make_store(tmp_path)
        store.save(draft(name="Demo API", baseUrl="https://example.com/v1", keyMode="none", keyHint="",
                         models=[model("reasoner", displayName=long_name, contextWindow=64000, reasoning="high"),
                                 model("plain", displayName=short_name, contextWindow=64000, reasoning="")]))
        rig = await open_rig(tmp_path, providers=store, size=size)
        try:
            await until(lambda: any(item["label"] == short_name for item in rig.core.catalog.models))
            api = {item["label"]: item["id"] for item in rig.core.catalog.models if item["source"].startswith("api:")}
            reasoner, plain = api[long_name], api[short_name]
            levels = {"main:chatgpt:gpt-6.1-sol": "high", f"worker:{reasoner}": "off"}
            for key, value in {"fusionMain": "chatgpt:gpt-6.1-sol", "fusionWorkers": [reasoner],
                               "fusionLight": plain, "fusionReasoning": levels,
                               "fontScale": "lg" if size[0] == 900 else "md"}.items():
                await rig.core.call("settings.set", {"key": key, "value": value})
            await rig.core.call("models.setVisible", {"ids": [reasoner], "visible": False})
            await rig.press(",", Qt.KeyboardModifier.ControlModifier)
            await rig.wait_for(".settings")
            await wait_for(lambda: rig.js(
                "document.querySelector('.settings').getAnimations({subtree: true})"
                ".every(a => a.playState !== 'running' && a.playState !== 'pending')"), 30, "settings animation")
            normal_width = await rig.js("document.querySelector('.settings').getBoundingClientRect().width")
            viewport_width = await rig.js("window.innerWidth")
            assert abs(normal_width - min(1280, viewport_width * 0.96)) <= 1
            await rig.click_text(".snav", "Fusion")
            await rig.wait_for(".fusion-model-row", 3)
            fusion_width = await rig.js("document.querySelector('.settings').getBoundingClientRect().width")
            assert abs(fusion_width - normal_width) <= 1
            assert not await rig.exists(".fusion-reasoning")
            assert "各模型思考等级" not in await rig.text(".settings-body")
            reasoner_selector = f'.fusion-model-row[data-model="{reasoner}"] .fusion-effort-picker .selectbox'
            plain_selector = f'.fusion-model-row[data-model="{plain}"] .fusion-effort-picker .selectbox'
            await rig.wait_text(reasoner_selector, "Off")
            assert await rig.js(f'document.querySelector({json.dumps(plain_selector)}).disabled')
            assert await rig.text(plain_selector) == "不支持"
            evidence_dir = os.environ.get("WITTYCODE_TEST_EVIDENCE")
            shot = Path(evidence_dir or tmp_path) / f"fusion-model-efforts-{size[0]}.png"
            await rig.shot(".fusion-seats", shot)
            await rig.click(reasoner_selector)
            assert "Max" in await rig.texts(".menu-item .menu-label")
            await wait_for(lambda: rig.js("[...document.querySelectorAll('.settings, .menu')].every(e => e.getAnimations({subtree: true}).length === 0)"))
            await rig.click_text(".menu-item", "Max")
            await until(lambda: rig.core.settings.fusion_reasoning.get(f"worker:{reasoner}") == "max")
            await rig.click(reasoner_selector)
            await rig.click_text(".menu-item", "默认")
            await until(lambda: rig.core.settings.fusion_reasoning == {"main:chatgpt:gpt-6.1-sol": "high"})
            await rig.wait_text(reasoner_selector, "默认")
            bounds = json.loads(await rig.js(
                "JSON.stringify([...document.querySelectorAll('.fusion-model-row')].map(e => {"
                "const row = e.getBoundingClientRect(), boxes = [...e.querySelectorAll('.selectbox')], "
                "modelBox = boxes[0].getBoundingClientRect(), effortBox = boxes[1].getBoundingClientRect(), "
                "label = boxes[0].querySelector('span'), labelBox = label.getBoundingClientRect(), "
                "remove = e.querySelector('.fusion-model-remove'), ctl = e.closest('.field-ctl').getBoundingClientRect(), "
                "field = e.closest('.field').getBoundingClientRect(); "
                "return {role: e.dataset.role, modelId: e.dataset.model, count: boxes.length, left: row.left, right: row.right, "
                "ctlRight: ctl.right, fieldRight: field.right, modelLeft: modelBox.left, modelRight: modelBox.right, modelWidth: modelBox.width, effortLeft: effortBox.left, "
                "effortRight: effortBox.right, modelTop: modelBox.top, modelBottom: modelBox.bottom, effortTop: effortBox.top, "
                "effortBottom: effortBox.bottom, modelHeight: modelBox.height, effortHeight: effortBox.height, "
                "labelOverflowX: label.scrollWidth - label.clientWidth, labelOverflowY: label.scrollHeight - label.clientHeight, "
                "labelHeight: labelBox.height, labelWhiteSpace: getComputedStyle(label).whiteSpace, remove: remove ? {"
                "text: remove.textContent.trim(), label: remove.getAttribute('aria-label') || '', title: remove.getAttribute('title') || '', "
                "type: remove.type, width: remove.getBoundingClientRect().width, height: remove.getBoundingClientRect().height} : null}; }))"))
            width = await rig.js("window.innerWidth")
            adds = json.loads(await rig.js(
                "JSON.stringify([...document.querySelectorAll('.fusion-model-list')].map(list => {"
                "const effort = list.querySelector('.fusion-effort-picker .selectbox').getBoundingClientRect(), "
                "add = list.querySelector('.fusion-model-add .selectbox').getBoundingClientRect(), "
                "label = list.querySelector('.fusion-model-add .selectbox span'), labelStyle = getComputedStyle(label); "
                "return {pool: list.dataset.pool, leftDelta: add.left - effort.left, rightDelta: add.right - effort.right, "
                "labelHeight: label.getBoundingClientRect().height, labelLineHeight: parseFloat(labelStyle.lineHeight), "
                "labelOverflowX: label.scrollWidth - label.clientWidth, labelOverflowY: label.scrollHeight - label.clientHeight}; }))"))
            assert len(bounds) == 3
            assert {item["pool"] for item in adds} == {"worker", "light"}
            assert all(abs(item["leftDelta"]) < 1 and abs(item["rightDelta"]) < 1 for item in adds), adds
            assert all(item["labelHeight"] <= item["labelLineHeight"] + 1 for item in adds), adds
            assert all(item["labelOverflowX"] <= 1 and item["labelOverflowY"] <= 1 for item in adds), adds
            assert all(abs(item["ctlRight"] - item["fieldRight"]) < 1 for item in bounds)
            first = bounds[0]
            for key in ("modelLeft", "modelRight", "effortLeft", "effortRight"):
                assert all(abs(item[key] - first[key]) < 1 for item in bounds), key
            assert all(item["count"] == 2 and 0 <= item["left"] < item["right"] <= width
                       and item["right"] <= item["ctlRight"] + 1 and item["ctlRight"] <= width
                       and item["modelRight"] <= item["effortLeft"]
                       and abs(item["modelTop"] - item["effortTop"]) < 1
                       and abs(item["modelBottom"] - item["effortBottom"]) < 1
                       and abs(item["modelHeight"] - item["effortHeight"]) < 1 for item in bounds)
            assert all(item["modelWidth"] <= 205 for item in bounds)
            assert all(item["labelOverflowX"] <= 1 and item["labelOverflowY"] <= 1
                       and item["labelWhiteSpace"] != "nowrap" for item in bounds)
            long_row = next(item for item in bounds if item["modelId"] == reasoner)
            short_row = next(item for item in bounds if item["modelId"] == plain)
            assert long_row["modelHeight"] > 30
            assert short_row["modelHeight"] <= 32
            removable = [item for item in bounds if item["remove"]]
            assert len(removable) == 2
            assert all(not item["remove"]["text"] and item["remove"]["type"] == "button"
                       and abs(item["remove"]["width"] - 30) < 1 and abs(item["remove"]["height"] - 30) < 1 for item in removable)
            worker_remove = next(item["remove"] for item in bounds if item["role"] == "worker")
            light_remove = next(item["remove"] for item in bounds if item["role"] == "light")
            assert "工作模型 1" in worker_remove["label"] and long_name in worker_remove["label"]
            assert worker_remove["label"] == worker_remove["title"]
            assert "轻量模型 1" in light_remove["label"] and short_name in light_remove["label"]
            assert light_remove["label"] == light_remove["title"]
            (tmp_path / "fusion-inline-bounds.json").write_text(json.dumps(bounds, indent=2), encoding="utf-8")
            await rig.click('.fusion-model-list[data-pool="worker"] .fusion-model-add .selectbox')
            assert "GPT-6-Luna" in await rig.texts(".menu-item .menu-label")
            assert await rig.text(".settings-body > header h2") == "Fusion"
            await rig.click(".settings-body > header h2")
            await rig.wait_for(".menu-item", 0)
            await rig.click('.fusion-model-row[data-role="worker"] .fusion-model-remove')
            await until(lambda: rig.core.settings.fusion_workers == [])
            assert rig.core.settings.fusion_lights == [plain]
            assert rig.core.settings.fusion_light == plain
            await rig.wait_for('.fusion-model-row[data-role="worker"][data-model=""]', 1)
            assert await rig.count('.fusion-model-row[data-role="worker"][data-model=""] .selectbox') == 2
            assert await rig.count('.fusion-model-row[data-role="worker"][data-model=""] .fusion-model-remove') == 0
            assert await rig.count('.fusion-model-row[data-role="light"]') == 1
            await rig.click_text(".snav", "账号与模型")
            await rig.wait_text(".prov .btn", "编辑")
            await rig.click_text(".prov .btn", "编辑")
            await rig.wait_for(".pe")
            api_width = await rig.js("document.querySelector('.settings').getBoundingClientRect().width")
            assert abs(api_width - fusion_width) <= 1
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
            async def settled(selector: str) -> bool:
                return await rig.js(
                    f"[...document.querySelectorAll({json.dumps(selector)})].every(e => "
                    "e.getAnimations({subtree: true}).every(a => a.playState !== 'running' && a.playState !== 'pending'))"
                )

            async def menu_item_ready(label: str) -> bool:
                return await rig.js(
                    "(function(){"
                    f"const item = [...document.querySelectorAll('.menu-item')].find(e => e.textContent.includes({json.dumps(label)}));"
                    "if (!item || item.disabled) return false;"
                    "const menu = item.closest('.menu');"
                    "return !!menu && getComputedStyle(menu).visibility === 'visible' && "
                    "menu.getAnimations({subtree: true}).every(a => a.playState !== 'running' && a.playState !== 'pending');"
                    "})()"
                )

            async def choose_sol(role: str) -> None:
                row = f'.fusion-model-row[data-role="{role}"]'
                await rig.click(row + " .fusion-model-picker .selectbox")
                await wait_for(lambda: menu_item_ready("GPT-6.1-Sol"), 30, f"GPT-6.1-Sol menu item for {role}")
                await rig.click_text(".menu-item", "GPT-6.1-Sol")
                await rig.wait_for(f'{row}[data-model="chatgpt:gpt-6.1-sol"]', 1)

            await wait_for(lambda: settled(".settings"), 30, "settings animations")
            for role in ("main", "worker", "light"):
                await choose_sol(role)
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
