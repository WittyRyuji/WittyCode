"""Review regressions: legacy names, changed metadata, unrelated MCP order and scope-limited reads."""

from desktop.core import connections
from desktop.core.harness import skills
from tests.test_connection_skill_order import make_skill, rows
from tests.test_connections import rig, trust, write_json


def test_legacy_folder_names_are_normalized_without_changing_ids_or_migrating_the_registry(rig):
    store, home, project = rig
    (project / ".git").mkdir()
    selected = make_skill(home / ".agents/skills", "package-layout", "shared")
    make_skill(home / ".wittycode/skills", "other-layout", "shared")
    trust(store, selected)
    legacy = store._load()
    legacy[0]["name"] = "package-layout"
    store._save(legacy)
    before = store.file.read_bytes()
    original = rows(store)
    assert len(original) == 1 and original[0]["name"] == "shared"
    assert original[0]["id"] == legacy[0]["id"] and store.file.read_bytes() == before
    for enabled in (False, True):
        store.update(original[0]["id"], enabled=enabled)
        assert len(rows(store)) == 1
        assert [item.path for item in skills.discover(project, home)] == ([selected] if enabled else [])


def test_changed_skill_metadata_cannot_bypass_a_disabled_control_via_a_same_name_copy(rig):
    store, home, project = rig
    (project / ".git").mkdir()
    selected = make_skill(home / ".wittycode/skills", "original", "original")
    make_skill(home / ".codex/skills", "fallback", "renamed")
    trust(store, selected)
    entry = rows(store)[0]
    selected.write_text(selected.read_text().replace("name: original", "name: renamed"), encoding="utf-8")
    store.update(entry["id"], enabled=False)
    assert skills.discover(project, home) == []
    assert [entry["name"] for entry in rows(store)] == ["renamed"]
    store.update(entry["id"], enabled=True)
    assert [item.path for item in skills.discover(project, home)] == [selected]
    assert len(rows(store)) == 1


def test_skill_choice_order_does_not_change_the_order_of_mcp_commands_on_switch_or_reimport(rig):
    store, home, project = rig
    source = write_json(home / ".codex/.mcp.json", {"mcpServers": {"first": {"command": "python"}, "second": {"command": "python"}}})
    trust(store, source)
    entry = next(entry for entry in store.active("mcp", project) if entry["name"] == "first")
    store.update(entry["id"], enabled=False)
    store.update(entry["id"], enabled=True)
    assert [command.name for command in store.mcp_commands(project)] == ["first", "second"]
    single = store.preview(str(source))
    store._preview[single["preview"]] = [next(item for item in single["items"] if item["name"] == "first")]
    store.commit(single["preview"], True)
    assert [command.name for command in store.mcp_commands(project)] == ["first", "second"]


def test_name_normalization_does_not_read_skills_from_another_project_scope(rig, monkeypatch):
    store, home, project = rig
    other = home / "other-project"
    selected = make_skill(other, "shared")
    trust(store, selected, other)
    original = connections._skill_name

    def scoped(path, fallback):
        assert path != selected, "global listing must not parse another project's definition"
        return original(path, fallback)

    monkeypatch.setattr(connections, "_skill_name", scoped)
    assert rows(store) == []
