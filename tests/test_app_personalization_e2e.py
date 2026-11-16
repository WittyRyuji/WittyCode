"""Real Qt/React: merged settings, persistent defaults, rule editing, native OAuth, MCP creation and public skills."""

import asyncio
import json
from pathlib import Path

import pytest
from PySide6.QtCore import Qt

from desktop.core import customization
from desktop.core.harness import skills
from tests.app_support import MODEL, OTHER_MODEL
from tests.e2e_support import open_rig
from tests.fake_responses import say
from tests.test_connector_onboarding import DeviceService
from tests.test_connections import mcp_script
from tests.test_skill_marketplace import OfficialFake
from tests.ui_support import run, wait_for

pytestmark = pytest.mark.ui


async def fill(rig, selector, value):
    await rig.click(selector)
    await rig.press(Qt.Key.Key_A, Qt.KeyboardModifier.ControlModifier)
    for index, line in enumerate(value.split("\n")):
        if index:
            await rig.press("Enter", Qt.KeyboardModifier.ShiftModifier)
        await rig.type(line)


@pytest.mark.parametrize("theme,size", [("light", (1500, 960)), ("dark", (900, 760))])
def test_merged_settings_geometry_languages_and_rules_are_really_editable(qapp, loop, tmp_path, monkeypatch, theme, size):
    async def scenario():
        rig = await open_rig(tmp_path, size=size)
        gate = asyncio.Event()
        try:
            await rig.core.call("settings.set", {"key": "theme", "value": theme})
            await rig.press(",", Qt.KeyboardModifier.ControlModifier)
            await rig.wait_for(".settings")
            assert await rig.texts(".snav") == ["外观", "账号与模型", "会话", "自定义", "共享记忆", "外部程序", "项目", "插件", "关于"]
            await wait_for(lambda: rig.js("(() => { const r=document.querySelector('.settings').getBoundingClientRect(); return Math.abs(r.width-Math.min(1150,innerWidth*.96))<1 && Math.abs(r.height-Math.min(800,innerHeight*.92))<1; })()"), 3, "settled enlarged settings geometry")
            await rig.click_text(".snav", "会话")
            assert (await rig.texts(".sessions-columns > section:first-child .field-label")) == ["模型", "推理", "权限", "模式"]
            assert await rig.js("document.querySelector('.settings-scroll').lastElementChild.classList.contains('sound-settings')")
            assert await rig.js("document.querySelector('.settings-scroll').scrollWidth <= document.querySelector('.settings-scroll').clientWidth")
            await rig.click_text(".snav", "关于")
            assert await rig.count(".shortcuts kbd") >= 10
            await rig.click_text(".snav", "自定义")
            await rig.wait_for(".rules-editor")
            await rig.click(".customization-page .field .segc button", 2)
            await wait_for(lambda: rig.core.settings.answer_language == "en")
            await fill(rig, ".rules-editor", "# Personal rules\nKeep answers short.")
            await rig.click_text(".snav", "关于")
            await rig.wait_for(".dialog")
            await rig.click_text(".dialog .btn", "取消")
            assert await rig.text(".snav.on") == "自定义"
            original = rig.core._m_customization_save
            async def paused_save(params):
                await gate.wait()
                return await original(params)
            monkeypatch.setattr(rig.core, "_m_customization_save", paused_save)
            await rig.click_text(".customization-page .btn", "保存规则")
            await rig.wait_text(".settings-foot", "正在保存")
            await rig.click_text(".snav", "关于")
            await rig.press("Escape")
            assert await rig.text(".snav.on") == "自定义" and not await rig.exists(".dialog")
            gate.set()
            await rig.wait_text(".customization-page [role=status]", "已保存")
            assert customization.global_rules_path().read_text(encoding="utf-8").endswith("Keep answers short.")
            customization.global_rules_path().write_text("# External rules\nKeep answers short.", encoding="utf-8")
            await fill(rig, ".rules-editor", "Do not overwrite the external update.")
            await rig.click_text(".customization-page .btn", "保存规则")
            await rig.wait_text(".customization-page [role=alert]", "其他程序修改")
            await rig.click_text(".customization-page .btn", "重新加载")
            await rig.wait_for(".dialog")
            await rig.click_text(".dialog .btn", "放弃并重新加载")
            await rig.wait_for(".rules-editor")
            assert await rig.js("document.querySelector('.rules-editor').value.startsWith('# External rules')")
            assert customization.global_rules_path().read_text(encoding="utf-8").startswith("# External rules")
            await rig.click('[aria-label="规则作用范围"]')
            await rig.press(Qt.Key.Key_Down)
            await rig.press("Enter")
            await wait_for(lambda: rig.js("document.querySelector('[aria-label=\"规则作用范围\"]').value !== ''"))
            await rig.wait_for(".rules-editor")
            await fill(rig, ".rules-editor", "# Project rules\nRun the tests.")
            await rig.click_text(".customization-page .btn", "保存规则")
            await rig.wait_text(".customization-page [role=status]", "已保存")
            assert (rig.project / "AGENTS.md").read_text(encoding="utf-8").endswith("Run the tests.")
            assert customization.global_rules_path().read_text(encoding="utf-8").endswith("Keep answers short.")
            await rig.click_text(".snav", "关于")
            assert not await rig.exists(".dialog")
            assert rig.plan.requests == []
        finally:
            gate.set()
            await rig.finish()
    run(loop, scenario())


def test_landing_and_live_choices_never_overwrite_persistent_defaults_even_after_reload(qapp, loop, tmp_path):
    async def scenario():
        rig = await open_rig(tmp_path)
        try:
            rig.core.persist = True
            await rig.press(",", Qt.KeyboardModifier.ControlModifier)
            await rig.click_text(".snav", "会话")
            await rig.click(".sessions-columns > section:first-child .field-ctl button", 1)
            await rig.click_text(".menu-item", "High")
            await wait_for(lambda: rig.core.settings.reasoning_effort == "high")
            expected = dict(rig.core.defaults())
            await rig.press("Escape")
            await rig.click(".controls .chip", 0)
            await rig.click_text(".menu-item", rig.core.catalog.find(OTHER_MODEL)["label"])
            await rig.wait_text(".controls .chip-v", rig.core.catalog.find(OTHER_MODEL)["label"])
            assert not rig.core.order and rig.core.defaults() == expected
            rig.plan.script(say("demo answer"))
            await rig.send("check temporary defaults")
            await rig.wait_for(".tab", 1)
            await rig.wait_for(".send.stop", 0)
            assert rig.core._page_settings(rig.core.order[0])["model"] == OTHER_MODEL
            assert rig.core.defaults() == expected
            await rig.click(".controls .chip", 0)
            await rig.click_text(".menu-item", rig.core.catalog.find(MODEL)["label"])
            await rig.wait_text(".controls .chip-v", rig.core.catalog.find(MODEL)["label"])
            await rig.click(".controls .chip", 1)
            await rig.click_text(".menu-item", "Medium")
            await wait_for(lambda: rig.core._page_settings(rig.core.order[0])["reasoning"] == "medium")
            assert rig.core.defaults() == expected
            await rig.click(".newbtn")
            await rig.wait_text(".controls .chip-v", "High")
            rig.window.view.reload()
            await rig.wait_for(".boot", 0)
            await rig.wait_for(".sidebar")
            await rig.click(".newbtn")
            await rig.wait_text(".controls .chip-v", "High")
            assert rig.core.defaults() == expected
            from desktop.core.settings import default_settings_path
            stored = json.loads(default_settings_path().read_text(encoding="utf-8"))
            assert stored["reasoning_effort"] == "high"
        finally:
            await rig.finish()
    run(loop, scenario())


def test_native_connector_oauth_and_custom_mcp_work_in_the_real_window(qapp, loop, tmp_path, monkeypatch):
    from desktop.core import connector_auth, connector_clients
    monkeypatch.setattr(connector_clients, "BUNDLED", tmp_path / "no-clients.json")
    monkeypatch.setenv(connector_clients.CLIENT_ENV["github"], "demo-client")
    monkeypatch.delenv(connector_clients.CLIENT_ENV["google-workspace"], raising=False)
    async def fast_pause(self, seconds):
        pass
    monkeypatch.setattr(connector_auth.GitHubDeviceFlow, "_pause", fast_pause)
    async def scenario():
        rig = await open_rig(tmp_path)
        release, opened = asyncio.Event(), []
        class HeldDevice(DeviceService):
            async def request(self, method, url, **kwargs):
                if url.endswith("/access_token"):
                    await release.wait()
                return await super().request(method, url, **kwargs)
        try:
            rig.core.connectors.http = HeldDevice()
            rig.window.platform.open_url = lambda url: opened.append(url)
            await rig.press(",", Qt.KeyboardModifier.ControlModifier)
            await rig.click_text(".snav", "插件")
            await rig.wait_for(".connector-card", 2)
            assert await rig.js("!document.querySelector('.connector-card input') && !document.querySelector('.connector-card details')")
            assert "暂未开放" in await rig.text('[data-connector="google-workspace"]')
            assert await rig.js("document.querySelector('[data-connector=google-workspace] .primary').disabled")
            await rig.click_text('[data-connector="github"] .btn', "浏览器授权并连接")
            await rig.wait_text('[data-connector="github"] [role=status]', "ABCD-EFGH")
            assert opened == ["https://github.com/login/device"]
            release.set()
            await rig.wait_text('[data-connector="github"] .connection-state', "已连接 · 已启用")
            assert [c.name for c in rig.core.connections.mcp_commands(rig.project)] == ["wittycode-github"]
            await rig.click_text(".connection-tabs button", "MCP")
            await rig.click_text(".connections .btn", "添加自定义 MCP")
            peer = mcp_script(tmp_path)
            import sys
            await fill(rig, '[aria-label="新 MCP 名称"]', "manual-demo")
            await fill(rig, '[aria-label="MCP JSON 或 TOML 配置"]', json.dumps({"command": sys.executable, "args": [str(peer)]}))
            assert await rig.js("document.querySelector('.connection-editor .primary').disabled")
            await rig.click(".connection-editor input[type=checkbox]")
            await rig.click_text(".connection-editor .btn", "添加并启用")
            await rig.wait_for(".connection-row[data-kind=mcp]", 1)
            await rig.click_text(".connection-row .btn", "测试连接")
            await rig.wait_text(".connection-result", "echo")
            assert rig.plan.requests == []
        finally:
            release.set()
            await rig.finish()
    run(loop, scenario())


def test_skill_store_installs_both_sources_with_consent_and_selects_the_latest_same_name(qapp, loop, tmp_path, monkeypatch):
    async def scenario():
        rig = await open_rig(tmp_path)
        download, release = asyncio.Event(), asyncio.Event()
        class PausedDownload(OfficialFake):
            async def request(self, method, url, **kwargs):
                if "codeload.github.com" in url:
                    download.set()
                    await release.wait()
                return await super().request(method, url, **kwargs)
        try:
            rig.core.skill_marketplace.http = PausedDownload("openai")
            cancelled = []
            original_cancel = rig.core.skill_marketplace.cancel
            def record_cancel(token):
                cancelled.append(token)
                return original_cancel(token)
            monkeypatch.setattr(rig.core.skill_marketplace, "cancel", record_cancel)
            await rig.press(",", Qt.KeyboardModifier.ControlModifier)
            await rig.click_text(".snav", "插件")
            await rig.click_text(".connection-tabs button", "技能商店")
            await rig.wait_for(".skill-card", 1)
            await rig.click_text(".skill-card .btn", "安装")
            await wait_for(download.is_set)
            await rig.click_text(".snav", "关于")
            release.set()
            downloads = rig.core.skill_marketplace.root.parent / "skill-downloads"
            await wait_for(lambda: cancelled and not rig.core.skill_marketplace.previews and not list(downloads.iterdir()))
            await rig.click_text(".snav", "插件")
            await rig.click_text(".connection-tabs button", "技能商店")
            for source in ("openai", "anthropic"):
                if source == "anthropic":
                    rig.core.skill_marketplace.http = OfficialFake(source)
                    await rig.click_text(".skill-store .segc button", "Anthropic")
                await rig.wait_for(".skill-card", 1)
                await rig.click_text(".skill-card .btn", "安装")
                await rig.wait_for(".skill-store .connection-preview")
                assert await rig.js("document.querySelector('.skill-store .connection-preview .primary').disabled")
                await rig.click(".skill-store .connection-consent input")
                await rig.click_text(".skill-store .btn", "确认安装")
                await rig.wait_text(".skill-store [role=status]", "已安装 demo-skill")
                assert "/demo-skill" in await rig.text(".skill-store [role=status]")
                selected = [s for s in skills.discover(rig.project) if s.name == "demo-skill"]
                assert len(selected) == 1 and source in str(selected[0].path)
            await rig.click_text(".skill-store .segc button", "OpenAI")
            await rig.wait_text(".skill-card .btn", "启用")
            await rig.click_text(".skill-card .btn", "启用")
            await rig.wait_text(".skill-store .connection-preview", "原版本未经验证")
            await rig.click(".skill-store .connection-consent input")
            await rig.click_text(".skill-store .btn", "确认启用")
            await rig.wait_text(".skill-store [role=status]", "已启用 demo-skill")
            assert "openai" in str(next(s for s in skills.discover(rig.project) if s.name == "demo-skill").path)
            await rig.press("Escape")
            rig.plan.script(say("Skill loaded."))
            await rig.send("/demo-skill inspect")
            await rig.wait_text(".block.text-block", "Skill loaded.")
            assert any("Read the diff and summarize it." in json.dumps(request) for request in rig.plan.requests)
        finally:
            release.set()
            await rig.finish()
    run(loop, scenario())
