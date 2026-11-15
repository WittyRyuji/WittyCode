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
        assert (await market.catalog(source))["items"][0]["installed"]
        assert len(fake.calls) == 3, "catalogue is reused; archive downloads are pinned"
        with pytest.raises(PluginError, match="已.*存在"):
            await market.preview(source, "demo-skill")
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
