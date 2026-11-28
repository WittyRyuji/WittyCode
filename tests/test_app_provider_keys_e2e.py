"""Provider editor round trip through the real Qt WebChannel, using only a fake API."""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import pytest
from PySide6.QtCore import Qt

from desktop.core.provider_secrets import PROTECTED_KEY_FIELD
from tests import e2e_support
from tests.test_provider_keys import KEY, NEXT_KEY, direct
from tests.test_providers import Opener, entries_of, make_store, model
from tests.ui_support import run

pytestmark = [pytest.mark.ui, pytest.mark.skipif(sys.platform != "win32", reason="Windows current-user DPAPI")]


@pytest.fixture(autouse=True)
def use_managed_page(monkeypatch):
    if work := os.environ.get("WITTYCODE_TEST_WORK"):
        built = Path(work) / "dist" / "index.html"
        if built.is_file():
            monkeypatch.setattr(e2e_support, "PAGE_FILE", built)


def test_provider_editor_round_trips_the_write_only_key_over_qwebchannel(qapp, loop, tmp_path):
    async def scenario():
        opener = Opener({"data": [{"id": "remote-demo-model"}]})
        store = make_store(tmp_path, opener=opener)
        store.save(direct(keyMode="none", newApiKey=""))
        before = store.models_file.read_bytes()
        rig = await e2e_support.open_rig(tmp_path, providers=store)
        password = 'input[aria-label="API Key"]'
        try:
            await rig.press(",", Qt.KeyboardModifier.ControlModifier)
            await rig.wait_for(".settings")
            await rig.click_text(".snav", "账号与模型")
            await rig.click_text(".prov .btn", "编辑")
            await rig.click_text(".pe .segc button", "直接输入（不推荐）")
            await rig.click(password)
            await rig.type(KEY)
            await rig.click_text(".pe-foot .btn", "测试连接")
            await rig.wait_text(".test-res.ok", "连接成功")
            await rig.click_text(".pe-btns .btn", "从服务端获取列表")
            await rig.wait_text(".id-chip", "remote-demo-model")
            assert opener.headers["authorization"] == "Bearer " + KEY and store.models_file.read_bytes() == before
            await rig.click_text(".pe-foot .btn", "保存")
            await rig.wait_for(".pe", 0)
            await rig.wait_text(".prov-meta", "已保存加密 API Key")
            assert KEY not in store.models_file.read_text(encoding="utf-8")
            assert store.api_key(store.api_models()[0]) == KEY
            blob = entries_of(store)[0][PROTECTED_KEY_FIELD]
            await rig.click_text(".prov .btn", "编辑")
            await rig.wait_for(password)
            assert await rig.js(f"document.querySelector({json.dumps(password)}).value") == ""
            await rig.click_text(".pe-foot .btn", "保存")
            await rig.wait_for(".pe", 0)
            assert entries_of(store)[0][PROTECTED_KEY_FIELD] == blob
        finally:
            await rig.finish()

    run(loop, scenario())


def test_provider_editor_adds_named_keys_and_persists_each_models_selection(qapp, loop, tmp_path):
    async def scenario():
        store = make_store(tmp_path, environ={"DEMO_FIRST": "first", "DEMO_SECOND": "second"})
        store.save(direct(keyMode="env", keyHint="${DEMO_FIRST}", newApiKey="", models=[model("alpha", contextWindow=64000), model("beta", contextWindow=64000)]))
        rig = await e2e_support.open_rig(tmp_path, providers=store, size=(1360, 1020))
        beta = 'button[aria-label="模型 beta 的 API Key"]'

        async def fill(label, value):
            await rig.click(f'input[aria-label="{label}"]')
            await rig.press("A", Qt.KeyboardModifier.ControlModifier)
            await rig.type(value)

        try:
            await rig.press(",", Qt.KeyboardModifier.ControlModifier)
            await rig.wait_for(".settings")
            await rig.click_text(".snav", "账号与模型")
            await rig.click_text(".prov .btn", "编辑")
            await fill("环境变量", "DEMO_FIRST, DEMO_SECOND")
            await rig.wait_for(".envline.set", 2)
            await rig.click(beta)
            await rig.click_text(".menu-item", "DEMO_SECOND")
            await fill("环境变量", "DEMO_SECOND, DEMO_FIRST")
            await rig.wait_text(beta, "DEMO_SECOND")
            await rig.click_text(".pe-foot .btn", "保存")
            await rig.wait_for(".pe", 0)
            assert [store.api_key(spec) for spec in store.api_models()] == ["first", "second"]

            await rig.click_text(".prov .btn", "编辑")
            await rig.click_text(".pe .segc button", "直接输入（不推荐）")
            await fill("密钥名称 1", "Primary")
            await fill("API Key", KEY)
            await rig.click_text(".pe .btn", "添加 Key")
            await fill("密钥名称 2", "Secondary")
            await fill("API Key 2", NEXT_KEY)
            await rig.click(beta)
            await rig.click_text(".menu-item", "Secondary")
            await rig.click_text(".pe-foot .btn", "保存")
            await rig.wait_for(".pe", 0)
            assert [store.api_key(spec) for spec in store.api_models()] == [KEY, NEXT_KEY]

            await rig.click_text(".prov .btn", "编辑")
            await rig.wait_text(beta, "Secondary")
            assert await rig.js('[...document.querySelectorAll(".pe input[type=password]")].every(e => !e.value)')
            if evidence := os.environ.get("WITTYCODE_TEST_EVIDENCE"):
                await rig.js('document.querySelector(".mtable").scrollIntoView({block:"nearest"})')
                await rig.shot(".settings", Path(evidence) / "provider-model-keys.png")
            await rig.click('button[aria-label="删除密钥 1"]')
            await rig.click_text(".pe-foot .btn", "保存")
            await rig.wait_for(".pe", 0)
            assert [store.api_key(spec) for spec in store.api_models()] == [NEXT_KEY, NEXT_KEY]
        finally:
            await rig.finish()

    run(loop, scenario())
