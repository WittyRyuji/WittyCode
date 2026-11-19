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
from tests.test_provider_keys import KEY, direct
from tests.test_providers import Opener, entries_of, make_store
from tests.ui_support import run

pytestmark = [pytest.mark.ui, pytest.mark.skipif(sys.platform != "win32", reason="Windows current-user DPAPI")]


def test_provider_editor_round_trips_the_write_only_key_over_qwebchannel(qapp, loop, tmp_path, monkeypatch):
    if work := os.environ.get("WITTYCODE_TEST_WORK"):
        built = Path(work) / "dist" / "index.html"
        if built.is_file():
            monkeypatch.setattr(e2e_support, "PAGE_FILE", built)

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
