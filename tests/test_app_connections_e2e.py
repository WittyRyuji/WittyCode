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


@pytest.mark.parametrize("launcher", ["python", "npx"])
def test_qt_import_requires_consent_and_really_tests_the_stdio_server(qapp, loop, tmp_path, monkeypatch, launcher):
    peer = mcp_script(tmp_path)
    import sys
    from tests.test_connection_startup import offline_npx

    config = offline_npx(tmp_path, monkeypatch) if launcher == "npx" else {"command": sys.executable, "args": [str(peer)]}
    source = write_json(tmp_path / "connections.json", {"mcpServers": {"local-tools": config}})
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
            peer.write_text("raise SystemExit(2)\n", encoding="utf-8")
            await rig.click_text(".connection-row .btn", "测试连接")
            await rig.wait_text(".connection-error[role=alert]", "提前退出")
            assert not await rig.exists(".connection-result"), "a failed test must clear the previous success"
            mcp_script(tmp_path)
            await rig.click_text(".connection-row .btn", "测试连接")
            await rig.wait_text(".connection-result", "连接成功")
            assert not await rig.exists(".connection-error[role=alert]")
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


@pytest.mark.parametrize("theme,width", [("light", 1500), ("dark", 900)])
def test_qt_opens_a_real_catalog_without_counting_cached_copies_or_covering_it_with_errors(qapp, loop, tmp_path, monkeypatch, theme, width):
    from desktop.core.harness import skills
    from tests.test_connection_discovery import plugin

    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setattr(skills, "USER_HOME", home)
    root = plugin(home / ".codex/plugins/local-demo")
    (root / "large-reference.bin").write_bytes(b"0" * 12_000_001)
    for version in ("1.0.0", "1.1.0"):
        plugin(home / ".codex/plugins/cache/demo-catalog/demo-plugin" / version)
    plugin(home / ".codex/plugins/marketplaces/demo-catalog/plugins/demo-plugin")
    file = home / ".agents/skills/personal-demo/SKILL.md"
    file.parent.mkdir(parents=True)
    file.write_text("---\nname: personal-demo\ndescription: Review local changes\n---\nInspect the local diff.", encoding="utf-8")
    (home / ".codex/config.toml").write_text('[mcp_servers.remote]\nurl = "https://example.com/mcp"\n[mcp_servers.local]\ncommand = "python"\nstartup_timeout_sec = 12\n', encoding="utf-8")

    async def scenario():
        rig = await open_rig(tmp_path, size=(width, 960))
        try:
            await rig.core.call("settings.set", {"key": "theme", "value": theme})
            await rig.press(",", Qt.KeyboardModifier.ControlModifier)
            await rig.wait_for(".settings")
            await rig.click_text(".snav", "Connections")
            await rig.wait_for(".connection-row", 2)
            assert await rig.texts(".connection-tabs .num") == ["2", "1", "0", "0"]
            assert await rig.text(".connection-issues summary") == "发现提示 · 2"
            assert await rig.js("!document.querySelector('.connection-issues').open")
            assert not await rig.exists(".connection-error[role=alert]")
            await rig.click(".connection-issues summary")
            assert await rig.count(".connection-issue") == 2
            await rig.click(".connection-issues summary")
            await rig.click_text(".connection-tabs button", "Plugins")
            await rig.wait_for(".connection-row", 1)
            assert await rig.text(".connection-info b") == "demo-plugin"
            await rig.click_text(".connection-row .btn", "导入")
            await rig.wait_text(".connection-error[role=alert]", "导入上限")
            assert not await rig.exists(".connection-preview"), "listing must not relax the explicit import trust limit"
            assert not rig.core.connections.file.exists()
            await rig.js("document.querySelectorAll('.connection-source').forEach(e => e.textContent = 'C:/Users/me/.codex/plugins/local-demo')")
            await asyncio.sleep(0.1)
            folder = Path(__file__).resolve().parents[1] / "sandbox"
            folder.mkdir(exist_ok=True)
            assert rig.window.grab().save(str(folder / f"connections-discovery-qt-{theme}.png"))
            assert not any(line.startswith("error:") for line in rig.window.page.console)
        finally:
            await rig.finish()

    run(loop, scenario())
