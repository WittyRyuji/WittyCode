"""Connections and the optional Factory CLI, in the real React / QWebChannel / Qt window with local-only peers."""

import asyncio
from pathlib import Path

import pytest
from PySide6.QtCore import Qt

from tests.e2e_support import open_rig
from tests.fakes import until
from tests.test_connections import mcp_script, write_json
from tests.test_factory import engine_for
from tests.ui_support import run

pytestmark = pytest.mark.ui


def test_qt_import_requires_consent_and_really_tests_the_stdio_server(qapp, loop, tmp_path):
    peer = mcp_script(tmp_path)
    import sys
    source = write_json(tmp_path / "connections.json", {"mcpServers": {"local-tools": {"command": sys.executable, "args": [str(peer)]}}})
    async def scenario():
        rig = await open_rig(tmp_path)
        try:
            await rig.press(",", Qt.KeyboardModifier.ControlModifier)
            await rig.wait_for(".settings")
            await rig.click_text(".snav", "Connections")
            await rig.wait_for(".connection-tabs [role=tab]", 4)
            await rig.click_text(".connection-tabs button", "MCP")
            await rig.click('.connection-import input')
            await rig.type(str(source))
            await rig.click_text(".connection-import .btn", "预览导入")
            await rig.wait_for(".connection-preview")
            assert await rig.js("document.querySelector('.connection-preview .primary').disabled")
            assert rig.core.connections.mcp_commands(rig.project) == []
            await rig.click(".connection-preview input[type=checkbox]")
            await rig.click(".connection-preview .primary")
            await rig.wait_for(".connection-row[data-kind=mcp]")
            assert len(rig.core.connections.mcp_commands(rig.project)) == 1
            await rig.click_text(".connection-row .btn", "测试连接")
            await rig.wait_text(".connection-result", "连接成功")
            assert "echo" in await rig.text(".connection-result")
            await rig.click(".connection-row .switch")
            await until(lambda: not rig.core.connections.mcp_commands(rig.project))
            # Neutralize fixture locations before retaining a screenshot; never retain real profile paths.
            await rig.js("document.querySelectorAll('.connection-source').forEach(e => e.textContent = 'C:/Users/me/.codex/config.toml'); document.querySelector('.connection-import input').value = 'C:/Users/me/.codex/config.toml'")
            await asyncio.sleep(0.1)
            folder = Path(__file__).resolve().parents[1] / "sandbox"
            folder.mkdir(exist_ok=True)
            assert rig.window.grab().save(str(folder / "connections-qt.png"))
        finally:
            await rig.finish()
    run(loop, scenario())


def test_qt_factory_menu_uses_the_native_peer_and_not_the_own_loop(qapp, loop, tmp_path):
    factory, _, _ = engine_for(tmp_path)
    factory._locate = lambda _command: "droid"
    async def scenario():
        rig = await open_rig(tmp_path, factory_engine=factory)
        try:
            await until(lambda: any(m["source"] == "factory" for m in rig.core.catalog.models))
            await rig.click(".controls .chip", 0)
            await rig.click_text(".menu-item", "Factory Demo")
            await rig.wait_text(".controls .chip-v", "Factory Demo")
            await rig.send("hello from the Qt window")
            await rig.wait_for(".tab")
            await rig.wait_text(".block.text-block", "Factory local answer.", timeout=15)
            assert rig.plan.requests == []
            live = rig.core.order[0]
            assert live.controller.engine == "factory" and live.controller.usage_series()["self"].total == 130
            assert await rig.text(".controls .src-tag") == "订阅"
            await rig.press(",", Qt.KeyboardModifier.ControlModifier)
            await rig.wait_for(".settings")
            await rig.click_text(".snav", "共享记忆")
            assert await rig.texts(".mem-engine") == ["WittyHarness", "Devin", "Factory"]
        finally:
            await rig.finish()
    run(loop, scenario())
