"""Discovery must list local connections, not every package ever cached by another client."""

from pathlib import Path

import pytest

from desktop.core import connections
from desktop.core.connections import ConnectionError
from desktop.core.harness import skills
from tests.test_connections import rig, write_json


def plugin(root, name="demo-plugin"):
    write_json(root / ".codex-plugin/plugin.json", {"name": name})
    file = root / "skills/demo-workflow/SKILL.md"
    file.parent.mkdir(parents=True)
    file.write_text("---\nname: demo-workflow\ndescription: Review a local inventory change\n---\nInspect the local diff.", encoding="utf-8")
    return root


def test_cached_versions_and_marketplace_sources_are_not_installed_connections(rig):
    store, home, project = rig
    plugin(home / ".codex/plugins/local-demo")
    for version in ("1.0.0", "1.1.0"):
        plugin(home / ".codex/plugins/cache/demo-catalog/demo-plugin" / version)
    plugin(home / ".codex/plugins/marketplaces/demo-catalog/plugins/demo-plugin")
    plugin(home / ".codex/plugins/.tmp/demo-plugin")
    scan = store.scan()
    assert len([e for e in scan["items"] if e["kind"] == "plugin"]) == 1
    assert len([e for e in scan["items"] if e["kind"] == "skill"]) == 1
    assert not scan["errors"]
    assert not store.file.exists(), "listing must not create a registry or change external installation state"


def test_codex_home_overrides_the_default_root_instead_of_merging_both(rig, monkeypatch):
    store, home, project = rig
    (project / ".git").mkdir()
    custom = home / "custom-codex"
    for root, name in ((home / ".codex", "old-server"), (custom, "selected-server")):
        write_json(root / ".mcp.json", {"mcpServers": {name: {"command": "python"}}})
        file = root / "skills" / name / "SKILL.md"
        file.parent.mkdir(parents=True)
        file.write_text(f"---\nname: {name}\ndescription: A local demo\n---\nReview the diff.", encoding="utf-8")
    monkeypatch.setattr(Path, "home", lambda: home)
    monkeypatch.setenv("CODEX_HOME", str(custom))
    scan = store.scan()
    assert [e["name"] for e in scan["items"] if e["kind"] == "mcp"] == ["selected-server"]
    assert [e["name"] for e in scan["items"] if e["kind"] == "skill"] == ["selected-server"]
    assert [skill.name for skill in skills.discover(project, home)] == ["selected-server"]


def test_identical_roots_and_unsupported_rows_do_not_repeat_the_same_warning(rig, monkeypatch):
    store, home, project = rig
    root = home / ".codex"
    write_json(root / ".mcp.json", {"mcpServers": {"first": {"url": "https://example.com/one"}, "second": {"url": "https://example.com/two"}}})
    monkeypatch.setattr(Path, "home", lambda: home)
    monkeypatch.setenv("CODEX_HOME", str(root))
    scan = store.scan()
    assert len(scan["errors"]) == 1
    assert not scan["items"]


def test_discovery_does_not_hash_untrusted_plugin_assets_or_raise_the_import_limit(rig, monkeypatch):
    store, home, project = rig
    root = plugin(home / ".codex/plugins/local-demo")
    (root / "large-reference.bin").write_bytes(b"0" * 12_000_001)
    scan = store.scan()
    assert any(e["kind"] == "plugin" for e in scan["items"]), "read-only discovery must not fail on unrelated package assets"
    assert not scan["errors"]
    with pytest.raises(ConnectionError, match="导入上限"):
        store.preview(str(root))


def test_one_unreadable_skill_does_not_prevent_the_settings_page_from_opening(rig, monkeypatch):
    store, home, project = rig
    good = home / ".agents/skills/good/SKILL.md"
    bad = home / ".agents/skills/bad/SKILL.md"
    for file in (good, bad):
        file.parent.mkdir(parents=True)
        file.write_text(f"---\nname: {file.parent.name}\ndescription: A local demo\n---\nInspect a diff.", encoding="utf-8")
    original = connections._linked
    monkeypatch.setattr(connections, "_linked", lambda path: True if path == bad else original(path))
    scan = store.scan()
    assert [e["name"] for e in scan["items"] if e["kind"] == "skill"] == ["good"]
    assert len(scan["errors"]) == 1


def test_explicitly_imported_cached_plugin_is_preserved_without_counting_other_versions(rig):
    store, home, project = rig
    from tests.test_connections import trust

    selected = plugin(home / ".codex/plugins/cache/demo-catalog/demo-plugin/1.0.0")
    plugin(home / ".codex/plugins/cache/demo-catalog/demo-plugin/2.0.0")
    assert store.scan()["items"] == []
    trust(store, selected)
    scan = store.scan()
    assert len([e for e in scan["items"] if e["kind"] == "plugin"]) == 1
    assert len([e for e in scan["items"] if e["kind"] == "skill"]) == 1
    assert [skill.name for skill in skills.discover(project, home)] == ["demo-workflow"]


def test_a_direct_project_plugin_does_not_become_a_global_connection(rig):
    store, home, project = rig
    plugin(project / ".codex/plugins/local-demo")
    scan = store.scan(str(project))
    assert {e["project"] for e in scan["items"]} == {str(project)}
    assert {e["kind"] for e in scan["items"]} == {"skill", "plugin"}
    assert not store.scan()["items"]


def test_an_unreadable_plugin_sibling_does_not_hide_the_next_local_plugin(rig, monkeypatch):
    store, home, project = rig
    bad = plugin(home / ".codex/plugins/a-unreadable", "unreadable")
    plugin(home / ".codex/plugins/b-usable", "usable")
    original = connections._linked

    def inaccessible(path):
        if path == bad:
            raise PermissionError("local fixture is unreadable")
        return original(path)

    monkeypatch.setattr(connections, "_linked", inaccessible)
    scan = store.scan()
    assert [entry["name"] for entry in scan["items"] if entry["kind"] == "plugin"] == ["usable"]
    assert len(scan["errors"]) == 1
