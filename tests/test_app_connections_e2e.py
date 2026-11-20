"""Connections and the optional Factory CLI, in the real React / QWebChannel / Qt window with local-only peers."""

import asyncio
import os
from pathlib import Path

import pytest
from PySide6.QtCore import Qt

from tests.e2e_support import open_rig
from tests.fakes import until
from tests.test_connections import mcp_script, write_json
from tests.test_factory import engine_for
from tests.ui_support import run

pytestmark = pytest.mark.ui


def test_qt_shows_the_bundled_test_hook_in_every_scope(qapp, loop, tmp_path, monkeypatch):
    from desktop.core.hooks import MANAGED_TEST_HOOK

    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "codex"))

    async def scenario():
        rig = await open_rig(tmp_path)
        try:
            rig.core.hooks.on_prompt(MANAGED_TEST_HOOK)
            config = rig.project / ".wittycode" / "hooks.json"
            config.parent.mkdir()
            write_json(config, {"hooks": {"Stop": [{"hooks": [{"type": "command", "command": "echo demo"}]}]}})
            await rig.press(",", Qt.KeyboardModifier.ControlModifier)
            await rig.wait_for(".settings")
            await rig.click_text(".snav", "插件")
            await rig.click_text(".connection-tabs button", "Hooks")
            builtin = '.connection-list [data-builtin-hook="wittycode.test-cleanup"]'
            external = '.connection-list [data-kind="hook"][data-connection]'
            await rig.wait_for(builtin, 1)
            assert await rig.text(f"{builtin} b") == "测试收尾"
            assert await rig.text(f"{builtin} .connection-state") == "WittyCode 官方内置 · 始终启用，不可停用"
            assert "只清理本次测试" in await rig.text(f"{builtin} p")
            assert await rig.text('.connection-tabs [data-kind="hook"] .num') == "1"
            assert await rig.count(f"{builtin} button") == 1
            assert await rig.js(f"""(() => {{ const toggle = document.querySelector({f'{builtin} [role=switch]'!r});
                toggle.click(); return toggle.disabled && toggle.getAttribute('aria-checked') === 'true'; }})()""")
            await rig.js("""{ const select = document.querySelector('.connection-toolbar select');
                select.selectedIndex = 1; select.dispatchEvent(new Event('change', {bubbles: true})); }""")
            await rig.wait_for(external, 1)
            assert "尚未信任" in await rig.text(f"{external} .connection-state")
            assert await rig.exists(f"{external} [role=switch]:enabled")
            assert await rig.exists(f"{builtin} [role=switch]:disabled")
            assert await rig.text('.connection-tabs [data-kind="hook"] .num') == "2"
            if evidence := os.environ.get("WITTYCODE_TEST_EVIDENCE"):
                await asyncio.sleep(0.4)  # Let the settings transition finish before grabbing the Qt surface.
                assert rig.window.grab().save(str(Path(evidence) / "builtin-test-hook.png"))
            assert not rig.core.connections.file.exists(), "listing bundled hooks must not import any configuration"
        finally:
            await rig.finish()

    run(loop, scenario())


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
            await rig.click_text(".snav", "插件")
            await rig.wait_for(".connection-tabs [role=tab]", 6)
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
            if evidence := os.environ.get("WITTYCODE_TEST_EVIDENCE"):
                assert rig.window.grab().save(str(Path(evidence) / "connections-qt.png"))
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
    (home / ".codex/config.toml").write_text('[mcp_servers.remote]\nurl = "http://example.com/mcp"\n[mcp_servers.local]\ncommand = "python"\nstartup_timeout_sec = 12\n', encoding="utf-8")

    async def scenario():
        rig = await open_rig(tmp_path, size=(width, 960))
        try:
            await rig.core.call("settings.set", {"key": "theme", "value": theme})
            await rig.press(",", Qt.KeyboardModifier.ControlModifier)
            await rig.wait_for(".settings")
            await rig.click_text(".snav", "插件")
            await rig.click_text(".connection-tabs button", "Skills")
            await rig.wait_for(".connection-row", 3)
            assert await rig.texts(".connection-tabs .num") == ["2", "3", "1", "0", "0"]
            assert await rig.text(".connection-issues summary") == "发现提示 · 2"
            assert await rig.js("!document.querySelector('.connection-issues').open")
            assert not await rig.exists(".connection-error[role=alert]")
            await rig.click(".connection-issues summary")
            assert await rig.count(".connection-issue") == 2
            await rig.click(".connection-issues summary")
            await rig.click_text(".connection-tabs button", "插件包")
            await rig.wait_for(".connection-row", 1)
            assert await rig.text(".connection-info b") == "demo-plugin"
            await rig.click_text(".connection-row .btn", "导入")
            await rig.wait_text(".connection-error[role=alert]", "导入上限")
            assert not await rig.exists(".connection-preview"), "listing must not relax the explicit import trust limit"
            assert not rig.core.connections.file.exists()
            await rig.js("document.querySelectorAll('.connection-source').forEach(e => e.textContent = 'C:/Users/me/.codex/plugins/local-demo')")
            await asyncio.sleep(0.1)
            if evidence := os.environ.get("WITTYCODE_TEST_EVIDENCE"):
                assert rig.window.grab().save(str(Path(evidence) / f"connections-discovery-qt-{theme}.png"))
            assert not any(line.startswith("error:") for line in rig.window.page.console)
        finally:
            await rig.finish()

    run(loop, scenario())


@pytest.mark.parametrize("theme,width", [("light", 1500), ("dark", 900)])
def test_qt_skill_switches_keep_sorted_rows_scroll_and_runtime_state_and_settings_have_no_preview(qapp, loop, tmp_path, monkeypatch, theme, width):
    from desktop.core.harness import skills
    from tests.test_connection_skill_order import make_skill
    from tests.test_connections import trust
    from tests.ui_support import wait_for

    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setattr(skills, "USER_HOME", home)
    names = ["zeta", "Alpha", "middle", *(f"demo-{index:02}" for index in range(10))]
    for name in names:
        make_skill(home / ".agents/skills", name)
    make_skill(home / ".codex/skills", "middle")
    first = make_skill(home / "first-import", "shared")
    selected = make_skill(home / "second-import", "shared")
    expected = sorted([name.lower() for name in names] + ["shared"])

    async def scenario():
        rig = await open_rig(tmp_path, size=(width, 960))
        try:
            trust(rig.core.connections, first)
            trust(rig.core.connections, selected)
            await rig.core.call("settings.set", {"key": "theme", "value": theme})
            await rig.press(",", Qt.KeyboardModifier.ControlModifier)
            await rig.wait_for(".settings")
            await rig.click_text(".snav", "插件")
            await rig.click_text(".connection-tabs button", "Skills")
            # The bundled official ``numbered-reply`` row leads the list; the user's skills follow it, sorted.
            shown = ["numbered-reply", *expected]
            await rig.wait_for(".connection-row", len(shown))
            assert await rig.texts(".connection-info b") == shown
            at = shown.index("middle")
            selector = f".connection-row:nth-child({at + 1}) .switch"
            await rig.js(f"document.querySelector({selector!r}).scrollIntoView({{block:'center'}})")
            await asyncio.sleep(.1)
            await rig.js("window.__skillRows = [...document.querySelectorAll('.connection-row')]; window.__skillScroll = document.querySelector('.settings-scroll').scrollTop")
            for enabled in (False, True, False, True):
                await rig.click(selector)
                await wait_for(lambda: rig.js(f"!document.querySelector({selector!r}).disabled && document.querySelector({selector!r}).getAttribute('aria-checked') === '{str(enabled).lower()}'"), 5, "skill switch result")
                assert await rig.texts(".connection-info b") == shown
                assert await rig.js("__skillRows.every((e, i) => e === document.querySelectorAll('.connection-row')[i])")
                assert await rig.js("Math.abs(document.querySelector('.settings-scroll').scrollTop - __skillScroll) < 2")
                assert await rig.text('.connection-tabs [aria-selected="true"] .num') == str(len(shown))
                available = skills.discover(rig.project, home)
                assert ("middle" in [item.name for item in available]) is enabled
                assert next(item.path for item in available if item.name == "shared") == selected
            await rig.click_text(".connection-toolbar .btn", "刷新发现")
            assert await rig.texts(".connection-info b") == shown
            await rig.press("Escape")
            await rig.wait_for(".settings", 0)
            await rig.press(",", Qt.KeyboardModifier.ControlModifier)
            await rig.click_text(".snav", "插件")
            await rig.click_text(".connection-tabs button", "Skills")
            await rig.wait_for(".connection-row", len(shown))
            assert await rig.texts(".connection-info b") == shown
            await rig.click_text(".snav", "外观")
            assert await rig.texts(".field-label") == ["主题", "文字大小"]
            assert not await rig.exists(".preview, .preview-k")
            assert "session_manager.py" not in await rig.text(".settings-scroll")
            await rig.click_text(".snav", "关于")
            assert await rig.text(".about .kvs b") == "0.3.0b1"
            assert rig.plan.requests == []
            assert not any(line.startswith("error:") for line in rig.window.page.console)
        finally:
            await rig.finish()

    run(loop, scenario())


def test_qt_switches_the_official_numbered_reply_skill(qapp, loop, tmp_path, monkeypatch):
    """The bundled ``numbered-reply`` row leads the Skills tab; its switch is the persisted settings key, live."""
    from desktop.core.harness import skills
    from tests.ui_support import wait_for

    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setattr(skills, "USER_HOME", home)

    async def scenario():
        rig = await open_rig(tmp_path)
        try:
            await rig.press(",", Qt.KeyboardModifier.ControlModifier)
            await rig.wait_for(".settings")
            await rig.click_text(".snav", "插件")
            await rig.click_text(".connection-tabs button", "Skills")
            row = '.connection-list [data-builtin-skill="numbered-reply"]'
            await rig.wait_for(row, 1)
            assert await rig.text(f"{row} b") == "numbered-reply"
            assert await rig.text(f"{row} .connection-state") == "WittyCode 官方内置 · 已启用"
            assert await rig.text('.connection-tabs [data-kind="skill"] .num') == "1", "the fixed row counts once"
            assert await rig.text(".connection-row b") == "numbered-reply", "the official row leads the list"
            assert "没有发现外部技能" in await rig.text(".connection-list .snote")
            for enabled in (False, True):
                await rig.click(f"{row} .switch")
                await wait_for(lambda: rig.js(f"document.querySelector({f'{row} [role=switch]'!r}).getAttribute('aria-checked') === '{str(enabled).lower()}'"), 5, "official skill switch")
                assert rig.core.settings.numbered_reply is enabled
                assert await rig.text(f"{row} .connection-state") == f"WittyCode 官方内置 · {'已启用' if enabled else '已停用'}"
            await rig.js("document.querySelectorAll('.connection-source').forEach(e => e.textContent = 'C:/Users/me/skills')")
            if evidence := os.environ.get("WITTYCODE_TEST_EVIDENCE"):
                await rig.shot(".settings", Path(evidence) / "builtin-numbered-reply-skill.png")
            assert not any(line.startswith("error:") for line in rig.window.page.console)
        finally:
            await rig.finish()

    run(loop, scenario())
