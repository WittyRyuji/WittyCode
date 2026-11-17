"""The real Provider editor validates, persists and restores per-effort request overrides."""
from __future__ import annotations

import json

import pytest
from PySide6.QtCore import Qt

from desktop.core.llm.backend import ApiBackend
from tests.e2e_support import open_rig
from tests.llm_support import FakeApi
from tests.test_llm_backend import ask, model_id_of, request_for
from tests.test_providers import draft, make_store, model
from tests.test_reasoning_args import answer
from tests.ui_support import run

pytestmark = pytest.mark.ui


def test_editor_saves_mapping_and_each_selected_effort_reaches_local_http(qapp, loop, tmp_path):
    async def scenario():
        with FakeApi() as api:
            store = make_store(tmp_path)
            store.save(draft(name="Demo Gateway", baseUrl=api.base_url, keyMode="none", keyHint="",
                             models=[model("demo", contextWindow=64000, reasoning="high")]))
            before = store.models_file.read_bytes()
            rig = await open_rig(tmp_path, providers=store)
            try:
                await rig.press(",", Qt.KeyboardModifier.ControlModifier)
                await rig.wait_for(".settings")
                await rig.click_text(".snav", "账号与模型")
                await rig.click_text(".prov .btn", "编辑")
                await rig.wait_for(".pe")
                assert not await rig.exists('textarea[aria-label="思考档位参数"]')
                await rig.click(".pe .disclose")
                await rig.click('textarea[aria-label="思考档位参数"]')
                await rig.type('{"off": []}')
                await rig.click_text(".pe-foot .btn", "保存")
                await rig.wait_text(".stack-err", "思考档位参数")
                assert store.models_file.read_bytes() == before
                mapping = {"off": {"thinking": {"type": "disabled"}}, "low": {"thinking": {"type": "enabled"}}}
                await rig.click('textarea[aria-label="思考档位参数"]')
                await rig.press("a", Qt.KeyboardModifier.ControlModifier)
                await rig.type(json.dumps(mapping))
                await rig.click_text(".pe-foot .btn", "保存")
                await rig.wait_for(".pe", 0)
                assert json.loads(store.models_file.read_text(encoding="utf-8"))["customModels"][0]["reasoningArgs"] == mapping
                await rig.click_text(".prov .btn", "编辑")
                await rig.wait_for('textarea[aria-label="思考档位参数"]')  # Existing mapping opens Advanced itself.
                assert json.loads(await rig.js('document.querySelector(\'textarea[aria-label="思考档位参数"]\').value')) == mapping
                assert rig.window.grab().save(str(tmp_path / "provider-reasoning-args.png"))
                backend = ApiBackend(store)
                for effort in ("off", "low"):
                    api.script(answer("generic-chat-completion-api"))
                    await ask(backend, request_for(model_id_of(store), effort))
                assert [body["thinking"]["type"] for _, _, body in api.posts] == ["disabled", "enabled"]
            finally:
                await rig.finish()
    run(loop, scenario())
