"""Pinned official-source skill installation uses synthetic archives and never executes their scripts."""

import asyncio
import io
import json
import stat
import zipfile

import pytest

from desktop.core.chatgpt.http import HttpResponse
from desktop.core.connections import ConnectionStore
from desktop.core.harness import skills
from desktop.core.plugin_http import PluginError
from desktop.core.skill_marketplace import SkillMarketplace, SOURCES

COMMIT = "a" * 40
TREE = "b" * 40
SKILL = "---\nname: demo-skill\ndescription: Review local changes\nlicense: MIT\n---\nRead the diff and summarize it.\n"


class OfficialFake:
    def __init__(self, source="openai", extra=None, skill=SKILL):
        self.source, self.calls, self.fail = source, [], False
        prefix = f"demo-repo/{SOURCES[source][1]}/demo-skill/"
        stream = io.BytesIO()
        with zipfile.ZipFile(stream, "w", zipfile.ZIP_DEFLATED) as archive:
            archive.writestr(prefix + "SKILL.md", skill)
            archive.writestr(prefix + "scripts/demo.py", "from pathlib import Path\nPath('should-not-run').write_text('not executed')\n")
            archive.writestr("demo-repo/LICENSE.txt", "MIT license example for this synthetic test archive.")
            if extra:
                name, content, mode = extra
                info = zipfile.ZipInfo(prefix + name)
                info.filename = prefix + name  # preserve hostile raw separators even on Windows
                info.external_attr = mode << 16
                archive.writestr(info, content)
        self.archive = stream.getvalue()

    async def request(self, method, url, **kwargs):
        self.calls.append(url)
        if self.fail:
            raise PluginError("Offline test.")
        if "/commits/" in url:
            value = {"sha": COMMIT, "commit": {"tree": {"sha": TREE}}}
        elif "/git/trees/" in url:
            value = {"tree": [{"path": SOURCES[self.source][1] + "/demo-skill/SKILL.md", "type": "blob"}], "truncated": False}
        else:
            return HttpResponse(200, {}, self.archive)
        return HttpResponse(200, {}, json.dumps(value).encode())


def setup_market(tmp_path, source="openai", **options):
    home = tmp_path / "home"
    store = ConnectionStore(tmp_path / "profile/connections.json", home=home)
    fake = OfficialFake(source, **options)
    return SkillMarketplace(store, http=fake), store, fake, home


@pytest.mark.parametrize("source", list(SOURCES))
def test_installs_both_official_sources_with_resources_licenses_and_explicit_consent(tmp_path, monkeypatch, source):
    async def scenario():
        market, store, fake, home = setup_market(tmp_path, source)
        monkeypatch.setenv("WITTYCODE_SETTINGS", str(store.file.with_name("settings.json")))
        monkeypatch.setattr(skills, "USER_HOME", home)
        catalog = await market.catalog(source)
        assert catalog["items"][0]["commit"] == COMMIT and not catalog["items"][0]["installed"]
        preview = await market.preview(source, "demo-skill")
        assert preview["license"] == "MIT" and "synthetic test archive" in preview["licenseText"]
        assert preview["files"] == 3 and COMMIT in preview["url"]
        with pytest.raises(PluginError, match="确认"):
            market.install(preview["preview"])
        assert not store.file.exists()
        result = market.install(preview["preview"], confirmed=True)
        assert result["ok"] and not market.previews
        found = skills.discover(tmp_path, home)
        assert [s.name for s in found] == ["demo-skill"]
        assert (found[0].path.parent / "scripts/demo.py").is_file()
        assert (found[0].path.parent / "repository-LICENSE.txt").is_file()
        assert not (tmp_path / "should-not-run").exists()
        entry = (await market.catalog(source))["items"][0]
        assert entry["installed"] and entry["active"]
        assert len(fake.calls) == 3, "catalogue is reused; archive downloads are pinned"
        with pytest.raises(PluginError, match="已经安装"):
            await market.preview(source, "demo-skill")
    asyncio.run(scenario())


@pytest.mark.parametrize("source", list(SOURCES))
def test_installed_skill_is_immediately_callable_and_uses_its_declared_name(tmp_path, monkeypatch, source):
    from desktop.core.harness import skill_tool
    from desktop.core.harness.tools import ToolContext
    from desktop.core.hooks import PromptEvent
    async def scenario():
        text = SKILL.replace("name: demo-skill", "name: Actual Skill")
        market, store, _, home = setup_market(tmp_path, source, skill=text)
        monkeypatch.setenv("WITTYCODE_SETTINGS", str(store.file.with_name("settings.json")))
        monkeypatch.setattr(skills, "USER_HOME", home)
        preview = await market.preview(source, "demo-skill")
        assert preview["skillName"] == "actual-skill" and preview["userInvocable"] and preview["modelInvocable"]
        market.install(preview["preview"], confirmed=True)
        assert [item["name"] for item in skills.listing(tmp_path, home)] == ["actual-skill"]
        context = skills.prompt_hook(PromptEvent("chatgpt", tmp_path, "/actual-skill review"))
        assert context and "Read the diff and summarize it." in context.text
        result = skill_tool.run({"skill": "actual-skill"}, ToolContext(tmp_path))
        assert not result.is_error and "Read the diff and summarize it." in result.output
        assert [skill.name for skill in skills.discover(tmp_path, home)] == ["actual-skill"]
        assert len(ConnectionStore(store.file, home=home).active("skill", "")) == 1
        assert not (tmp_path / "should-not-run").exists()
    asyncio.run(scenario())


@pytest.mark.parametrize("source", list(SOURCES))
def test_existing_but_unregistered_skill_can_be_confirmed_without_overwriting_or_redownloading(tmp_path, monkeypatch, source):
    async def scenario():
        market, store, fake, home = setup_market(tmp_path, source)
        monkeypatch.setenv("WITTYCODE_SETTINGS", str(store.file.with_name("settings.json")))
        monkeypatch.setattr(skills, "USER_HOME", home)
        first = await market.preview(source, "demo-skill")
        market.install(first["preview"], confirmed=True)
        target = market._target(source, "demo-skill")
        original = (target / "SKILL.md").read_bytes()
        store.file.unlink()  # An interrupted registration leaves the already downloaded directory in place.
        row = (await market.catalog(source))["items"][0]
        assert row["installed"] and not row["active"]
        fake.fail = True  # Re-enabling local files must not require another network request.
        preview = await market.preview(source, "demo-skill")
        assert preview["existing"] and preview["commit"] == ""
        market.cancel(preview["preview"])
        assert (target / "SKILL.md").read_bytes() == original
        preview = await market.preview(source, "demo-skill")
        (target / "scripts/demo.py").write_text("changed after preview", encoding="utf-8")
        with pytest.raises(PluginError, match="变化"):
            market.install(preview["preview"], confirmed=True)
        market.cancel(preview["preview"])
        assert (target / "scripts/demo.py").read_text(encoding="utf-8") == "changed after preview"
        preview = await market.preview(source, "demo-skill")
        assert market.install(preview["preview"], confirmed=True)["ok"]
        assert (target / "SKILL.md").read_bytes() == original and len(fake.calls) == 3
        assert (await market.catalog(source))["items"][0]["active"]
        assert [skill.name for skill in skills.discover(tmp_path, home)] == ["demo-skill"]
        market.close()
        assert target.is_dir(), "cancelling or closing an existing preview must never delete installed files"
    asyncio.run(scenario())


def test_closing_an_existing_skill_preview_never_deletes_installed_files(tmp_path):
    async def scenario():
        market, store, _, _ = setup_market(tmp_path)
        preview = await market.preview("openai", "demo-skill")
        market.install(preview["preview"], confirmed=True)
        target = market._target("openai", "demo-skill")
        entry = store.active("skill", "")[0]
        store.update(entry["id"], enabled=False)
        preview = await market.preview("openai", "demo-skill")
        assert preview["existing"]
        market.close()
        assert (target / "SKILL.md").is_file() and not market.previews
    asyncio.run(scenario())


def test_switching_between_same_named_installed_sources_keeps_both_files(tmp_path, monkeypatch):
    async def scenario():
        market, store, _, home = setup_market(tmp_path, "openai")
        monkeypatch.setenv("WITTYCODE_SETTINGS", str(store.file.with_name("settings.json")))
        monkeypatch.setattr(skills, "USER_HOME", home)
        first = await market.preview("openai", "demo-skill")
        market.install(first["preview"], confirmed=True)
        market.http = OfficialFake("anthropic")
        second = await market.preview("anthropic", "demo-skill")
        market.install(second["preview"], confirmed=True)
        assert not (await market.catalog("openai"))["items"][0]["active"]
        assert (await market.catalog("anthropic"))["items"][0]["active"]
        preview = await market.preview("openai", "demo-skill")
        assert preview["existing"]
        market.install(preview["preview"], confirmed=True)
        assert (await market.catalog("openai"))["items"][0]["active"]
        assert not (await market.catalog("anthropic"))["items"][0]["active"]
        assert "openai" in str(skills.discover(tmp_path, home)[0].path)
        assert all((market._target(source, "demo-skill") / "SKILL.md").is_file() for source in SOURCES)
    asyncio.run(scenario())


@pytest.mark.parametrize("flags", ["enabled: false", "user-invocable: false\ndisable-model-invocation: true"])
def test_store_refuses_a_skill_which_cannot_be_used(tmp_path, flags):
    async def scenario():
        text = SKILL.replace("license: MIT", "license: MIT\n" + flags)
        market, store, _, _ = setup_market(tmp_path, skill=text)
        with pytest.raises(PluginError, match="停用|禁止调用"):
            await market.preview("openai", "demo-skill")
        assert not store.file.exists() and not market.previews
    asyncio.run(scenario())


@pytest.mark.parametrize("name,mode", [("../escape.txt", stat.S_IFREG), ("C:escape.txt", stat.S_IFREG), ("linked", stat.S_IFLNK), ("bad\\escape.txt", stat.S_IFREG), ("name. ", stat.S_IFREG)])
def test_archive_traversal_windows_streams_and_links_are_rejected_without_installing(tmp_path, name, mode):
    async def scenario():
        market, store, _, _ = setup_market(tmp_path, extra=(name, "outside", mode))
        with pytest.raises(PluginError, match="越界|无效"):
            await market.preview("openai", "demo-skill")
        assert not store.file.exists() and not market.previews
        assert not (tmp_path / "escape.txt").exists()
    asyncio.run(scenario())


def test_modified_previews_and_existing_destinations_are_never_overwritten(tmp_path):
    async def scenario():
        market, store, _, _ = setup_market(tmp_path)
        preview = await market.preview("openai", "demo-skill")
        record = market.previews[preview["preview"]]
        (record["path"] / "scripts/demo.py").write_text("changed", encoding="utf-8")
        with pytest.raises(PluginError, match="变化"):
            market.install(preview["preview"], confirmed=True)
        assert not store.file.exists()
        market.cancel(preview["preview"])
        assert not record["path"].exists()
        preview = await market.preview("openai", "demo-skill")
        target = market.previews[preview["preview"]]["target"]
        target.mkdir(parents=True)
        (target / "own.txt").write_text("keep", encoding="utf-8")
        with pytest.raises(PluginError, match="目标已存在"):
            market.install(preview["preview"], confirmed=True)
        assert (target / "own.txt").read_text(encoding="utf-8") == "keep"
    asyncio.run(scenario())


def test_offline_catalogue_is_labeled_and_never_trusts_cached_source_links(tmp_path):
    async def scenario():
        market, store, fake, _ = setup_market(tmp_path)
        await market.catalog("openai")
        cache = store.file.parent / "skill-catalog/openai.json"
        raw = json.loads(cache.read_text(encoding="utf-8"))
        raw["items"][0]["url"] = "https://example.com/not-the-source"
        cache.write_text(json.dumps(raw), encoding="utf-8")
        restarted = SkillMarketplace(store, http=fake)
        fake.fail = True
        result = await restarted.catalog("openai", refresh=True)
        assert result["warning"] and result["items"][0]["url"].startswith("https://github.com/openai/skills/tree/")
        with pytest.raises(PluginError):
            await restarted.preview("openai", "demo-skill")
        assert not restarted.previews
    asyncio.run(scenario())


def test_skill_metadata_is_required_and_failed_downloads_leave_no_registered_skill(tmp_path):
    async def scenario():
        market, store, _, _ = setup_market(tmp_path, skill="# Missing metadata")
        with pytest.raises(PluginError, match="缺少"):
            await market.preview("openai", "demo-skill")
        assert not store.file.exists() and not market.previews
    asyncio.run(scenario())


def test_abandoned_previews_are_bounded_and_shutdown_reclaims_only_downloads(tmp_path):
    async def scenario():
        market, store, fake, _ = setup_market(tmp_path)
        paths, tokens = [], []
        for _ in range(6):
            result = await market.preview("openai", "demo-skill")
            tokens.append(result["preview"])
            paths.append(market.previews[tokens[-1]]["path"])
        assert len(market.previews) == 5 and not paths[0].exists()
        with pytest.raises(PluginError, match="过期"):
            market.install(tokens[0], confirmed=True)
        market.close()
        assert not market.previews and all(not path.exists() for path in paths)
        calls = len(fake.calls)
        with pytest.raises(PluginError, match="关闭"):
            await market.preview("openai", "demo-skill")
        assert len(fake.calls) == calls and not store.file.exists()
    asyncio.run(scenario())


def test_shutdown_while_unpacking_cannot_register_a_late_preview(tmp_path, monkeypatch):
    import threading
    from desktop.core import skill_marketplace
    async def scenario():
        market, store, _, _ = setup_market(tmp_path)
        entered, release = threading.Event(), threading.Event()
        original = skill_marketplace._source_hash
        def paused(path):
            entered.set()
            assert release.wait(3)
            return original(path)
        monkeypatch.setattr(skill_marketplace, "_source_hash", paused)
        work = asyncio.create_task(market.preview("openai", "demo-skill"))
        try:
            assert await asyncio.to_thread(entered.wait, 2), "unpacking must not block the application loop"
            market.close()
            release.set()
            with pytest.raises(PluginError, match="关闭"):
                await work
            assert not market.previews and not list((store.file.parent / "skill-downloads").iterdir())
        finally:
            release.set()
            await asyncio.gather(work, return_exceptions=True)
    asyncio.run(scenario())


def test_cancel_and_shutdown_never_delete_an_already_confirmed_installation(tmp_path, monkeypatch):
    import threading
    async def scenario():
        market, store, _, _ = setup_market(tmp_path)
        preview = await market.preview("openai", "demo-skill")
        record = market.previews[preview["preview"]]
        entered, release = threading.Event(), threading.Event()
        original = store.commit
        def paused(key, confirmed):
            entered.set()
            assert release.wait(3)
            return original(key, confirmed)
        monkeypatch.setattr(store, "commit", paused)
        work = asyncio.create_task(asyncio.to_thread(market.install, preview["preview"], confirmed=True))
        try:
            assert await asyncio.to_thread(entered.wait, 2)
            market.cancel(preview["preview"])
            market.close()
            release.set()
            assert (await work)["ok"]
            assert (record["target"] / "SKILL.md").is_file()
            assert len(store.active("skill", "")) == 1
        finally:
            release.set()
            await asyncio.gather(work, return_exceptions=True)
    asyncio.run(scenario())
