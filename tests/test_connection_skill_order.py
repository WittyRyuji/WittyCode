"""Stable management rows are not the runtime's enabled-only skill catalog; all homes stay isolated."""

from pathlib import Path

import pytest

from desktop.core.connections import ConnectionStore
from desktop.core.harness import skills
from tests.test_connections import rig, trust


def make_skill(root, folder, name=None):
    file = root / folder / "SKILL.md"
    file.parent.mkdir(parents=True, exist_ok=True)
    file.write_text(f"---\nname: {name or folder}\ndescription: Review a local change\n---\nInspect the local diff.", encoding="utf-8")
    return file


def rows(store, project=""):
    return [entry for entry in store.scan(str(project))["items"] if entry["kind"] == "skill"]


def signature(entries):
    return [(entry["id"], entry["name"], entry["source"], entry["project"]) for entry in entries]


@pytest.mark.parametrize("scope", ["global", "project"])
def test_skill_rows_stay_alphabetical_and_keep_their_identity_across_repeated_switches(rig, scope):
    store, home, project = rig
    (project / ".git").mkdir()
    root = (home if scope == "global" else project) / ".agents/skills"
    for name in ("zeta", "Beta", "alpha"):
        make_skill(root, name)
    selected = "" if scope == "global" else str(project)
    original = rows(store, selected)
    assert [entry["name"] for entry in original] == ["alpha", "beta", "zeta"]
    middle = original[1]
    for enabled in (False, True, False, True):
        store.update(middle["id"], enabled=enabled, project=selected)
        current = rows(ConnectionStore(store.file, home=home), selected)
        assert signature(current) == signature(original)
        assert current[1]["enabled"] is enabled
        assert ("beta" in [item.name for item in skills.discover(project, home)]) is enabled


def test_disabling_a_skill_does_not_surface_or_run_a_lower_priority_copy(rig):
    store, home, project = rig
    (project / ".git").mkdir()
    selected = make_skill(home / ".wittycode/skills", "shared")
    other = make_skill(home / ".codex/skills", "shared")
    before = {file: file.read_bytes() for file in (selected, other)}
    original = rows(store)
    assert len(original) == 1 and original[0]["source"] == str(selected)
    for enabled in (False, True, False, True):
        store.update(original[0]["id"], enabled=enabled)
        assert signature(rows(store)) == signature(original)
        available = skills.discover(project, home)
        assert [item.path for item in available] == ([selected] if enabled else [])
    assert all(file.read_bytes() == value for file, value in before.items())


def test_an_explicit_old_codex_source_is_one_control_not_a_duplicate_of_the_new_home(rig, monkeypatch):
    store, home, project = rig
    (project / ".git").mkdir()
    old = make_skill(home / ".codex/skills", "shared")
    current = make_skill(home / "selected-codex/skills", "shared")
    trust(store, old)
    monkeypatch.setattr(Path, "home", lambda: home)
    monkeypatch.setenv("CODEX_HOME", str(home / "selected-codex"))
    original = rows(store)
    assert len(original) == 1 and original[0]["source"] == str(old)
    assert [item.path for item in skills.discover(project, home)] == [old]
    for enabled in (False, True):
        store.update(original[0]["id"], enabled=enabled)
        assert signature(rows(store)) == signature(original)
        assert [item.path for item in skills.discover(project, home)] == ([old] if enabled else [])
    assert current.is_file(), "discovery and switches never remove another client's copy"


def test_existing_alias_records_remain_on_disk_but_only_the_last_choice_is_listed_and_run(rig):
    store, home, project = rig
    (project / ".git").mkdir()
    first = make_skill(home / "first-import", "shared")
    selected = make_skill(home / "second-import", "shared")
    trust(store, first)
    trust(store, selected)
    before = store.file.read_bytes()
    original = rows(store)
    assert len(original) == 1 and original[0]["source"] == str(selected)
    assert store.file.read_bytes() == before, "listing is not a registry migration"
    for enabled in (False, True, False, True):
        store.update(original[0]["id"], enabled=enabled)
        assert signature(rows(ConnectionStore(store.file, home=home))) == signature(original)
        assert [item.path for item in skills.discover(project, home)] == ([selected] if enabled else [])
        assert len(store._load()) == 2, "old imported records are preserved, not silently deleted"


def test_project_override_is_independent_of_the_global_control_and_does_not_fall_back_when_disabled(rig):
    store, home, project = rig
    (project / ".git").mkdir()
    global_file = make_skill(home / "imported", "shared")
    project_file = make_skill(project / ".agents/skills", "shared")
    trust(store, global_file)
    assert [item.path for item in skills.discover(project, home)] == [project_file]
    entry = next(entry for entry in rows(store, project) if entry["project"])
    store.update(entry["id"], enabled=False, project=str(project))
    assert skills.discover(project, home) == []
    assert [item.path for item in skills.discover(home, home)] == [global_file]
    store.update(entry["id"], enabled=True, project=str(project))
    assert [item.path for item in skills.discover(project, home)] == [project_file]


def test_imported_skill_names_match_the_front_matter_not_the_package_folder(rig):
    store, home, project = rig
    (project / ".git").mkdir()
    file = make_skill(home / ".agents/skills", "package-layout", "shared")
    trust(store, file)
    assert [entry["name"] for entry in rows(store)] == ["shared"]
    assert [item.name for item in skills.discover(project, home)] == ["shared"]
