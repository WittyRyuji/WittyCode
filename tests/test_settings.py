from __future__ import annotations

import json
import os
from types import SimpleNamespace

import pytest

from desktop.core import settings as settings_module
from desktop.core.settings import (
    DEFAULT_MAX_OPEN_SESSIONS,
    SETTINGS_ENV,
    AppSettings,
    default_settings_path,
    legacy_settings_path,
    load_settings,
    migrate_legacy_settings,
    same_folder,
    save_settings,
    settings_from_dict,
    settings_to_dict,
)


def test_round_trip_keeps_every_field(tmp_path):
    original = AppSettings(
        devin_path=str(tmp_path / "devin.exe"),
        default_model="chatgpt:gpt-6.1-sol",
        reasoning_effort="low",
        chatgpt_autonomy="full",
        mode="spec",
        projects=[str(tmp_path / "a"), str(tmp_path / "b")],
        max_open_sessions=7,
        window_geometry="AAEC",
        splitter_sizes=[240, 760],
        hidden_models=["chatgpt:gpt-6-luna", "devin:swe-2-high"],
        chatgpt_compact=False,
        chatgpt_compact_at=70,
        max_steps=240,
    )
    target = tmp_path / "nested" / "settings.json"
    save_settings(original, target)
    assert load_settings(target) == original


def test_save_is_atomic_and_writes_a_version(tmp_path):
    target = tmp_path / "settings.json"
    save_settings(AppSettings(), target)
    save_settings(AppSettings(chatgpt_autonomy="full"), target)
    assert sorted(path.name for path in tmp_path.iterdir()) == ["settings.json"]
    assert json.loads(target.read_text(encoding="utf-8"))["version"] == 2
    assert load_settings(target).chatgpt_autonomy == "full"


def test_chatgpt_plan_settings_default_and_are_validated():
    from desktop.core.settings import CHATGPT_PERMISSIONS, settings_from_dict

    defaults = settings_from_dict({})
    assert (defaults.chatgpt_enabled, defaults.chatgpt_autonomy, defaults.chatgpt_welcomed) == (True, "code", False)
    assert CHATGPT_PERMISSIONS == ("ask", "code", "full")
    odd = settings_from_dict({"chatgpt_enabled": "yes", "chatgpt_autonomy": "root", "chatgpt_welcomed": 1})
    assert (odd.chatgpt_enabled, odd.chatgpt_autonomy, odd.chatgpt_welcomed) == (True, "code", False)
    kept = settings_from_dict({"chatgpt_enabled": False, "chatgpt_autonomy": "full", "chatgpt_welcomed": True})
    assert (kept.chatgpt_enabled, kept.chatgpt_autonomy, kept.chatgpt_welcomed) == (False, "full", True)


def test_the_compaction_threshold_of_the_chatgpt_plan_defaults_to_85_percent_and_is_validated():
    from desktop.core.settings import COMPACT_AT_RANGE, DEFAULT_COMPACT_AT

    assert COMPACT_AT_RANGE == (50, 95) and DEFAULT_COMPACT_AT == 85
    for raw in ({}, AppSettings().__dict__):
        defaults = settings_from_dict(raw)
        assert (defaults.chatgpt_compact, defaults.chatgpt_compact_at) == (True, 85)
    for value in (49, 96, 0, 100, True, False, "85", 85.0, None, -70):
        assert settings_from_dict({"chatgpt_compact_at": value}).chatgpt_compact_at == 85, value
    for value in (50, 60, 95):
        assert settings_from_dict({"chatgpt_compact_at": value}).chatgpt_compact_at == value
    for value in ("no", 0, 1, None):
        assert settings_from_dict({"chatgpt_compact": value}).chatgpt_compact is True, value
    assert settings_from_dict({"chatgpt_compact": False}).chatgpt_compact is False
    assert settings_to_dict(AppSettings(chatgpt_compact=False, chatgpt_compact_at=60))["chatgpt_compact_at"] == 60


def test_the_step_limit_defaults_to_80_and_accepts_larger_positive_integers():
    from desktop.core.harness.loop import MAX_STEPS

    assert settings_from_dict({}).max_steps == MAX_STEPS == 80
    for value in (1, 200, 1000, 2**53 - 1):
        assert settings_from_dict({"max_steps": value}).max_steps == value
    for value in (0, -1, True, False, "200", 200.5, None, 2**53):
        assert settings_from_dict({"max_steps": value}).max_steps == 80


def test_archived_sessions_their_names_and_the_last_project_survive_the_file_and_bad_entries_fall_away(tmp_path):
    original = AppSettings(last_project="D:/work/p", archived_sessions={"s-1": 5, "cx:t-2": 9}, session_titles={"s-1": "名字"})
    target = tmp_path / "settings.json"
    save_settings(original, target)
    assert load_settings(target) == original

    messy = settings_from_dict({
        "archived_sessions": {"a": 1, "": 2, "b": "x", "c": True, "d": -1, 5: 3, "e": 1.5},
        "session_titles": {"a": "  two   words ", "b": "   ", "c": 5, "": "x", "d": "x" * 300},
        "last_project": 5,
    })
    assert messy.archived_sessions == {"a": 1}
    assert messy.session_titles == {"a": "two words", "d": "x" * settings_module.MAX_SESSION_TITLE}
    assert messy.last_project == ""
    for odd in (None, [], "s-1", 5):
        loose = settings_from_dict({"archived_sessions": odd, "session_titles": odd})
        assert loose.archived_sessions == {} and loose.session_titles == {}
    crowded = settings_from_dict({"archived_sessions": {f"s-{n}": n for n in range(settings_module.MAX_REMEMBERED_SESSIONS + 10)}})
    assert len(crowded.archived_sessions) == settings_module.MAX_REMEMBERED_SESSIONS


def test_the_shared_memory_engines_are_the_ones_there_are():
    from desktop.core.settings import settings_from_dict

    assert settings_from_dict({}).memory_engines == ["devin", "chatgpt", "factory"], "on for every engine to begin with"
    assert settings_from_dict({"version": 2, "memory_engines": ["chatgpt"]}).memory_engines == ["chatgpt"], "a choice is kept"
    # a file from before the own loop joined the memory, with every engine then known on: the own loop gets it too
    assert settings_from_dict({"version": 1, "memory_engines": ["devin"]}).memory_engines == ["devin", "chatgpt"]
    # names of engines this version does not have are dropped
    assert settings_from_dict({"version": 2, "memory_engines": ["old-engine", "devin"]}).memory_engines == ["devin"]


def test_missing_file_gives_defaults(tmp_path):
    settings = load_settings(tmp_path / "none.json")
    assert settings == AppSettings()
    assert settings.max_open_sessions == DEFAULT_MAX_OPEN_SESSIONS == 4


def test_corrupt_file_is_set_aside_not_overwritten(tmp_path):
    target = tmp_path / "settings.json"
    target.write_text("{not json", encoding="utf-8")
    assert load_settings(target) == AppSettings()
    assert (tmp_path / "settings.json.bad").read_text(encoding="utf-8") == "{not json"
    assert not target.exists()


@pytest.mark.parametrize("value", [0, 17, -1, True, "4", 2.5, None])
def test_out_of_range_session_limit_falls_back_to_default(value):
    assert settings_from_dict({"max_open_sessions": value}).max_open_sessions == DEFAULT_MAX_OPEN_SESSIONS


@pytest.mark.parametrize("value", [1, 16])
def test_session_limit_bounds_are_inclusive(value):
    assert settings_from_dict({"max_open_sessions": value}).max_open_sessions == value


def test_invalid_fields_fall_back_one_by_one():
    settings = settings_from_dict(
        {"chatgpt_autonomy": "bogus", "mode": "spec", "splitter_sizes": [1, -2], "projects": "nope", "default_model": 5}
    )
    assert settings.chatgpt_autonomy == "code"
    assert settings.mode == "spec"
    assert settings.splitter_sizes == []
    assert settings.projects == []
    assert settings.default_model == ""


def test_hidden_models_keep_only_distinct_non_empty_strings():
    assert settings_from_dict({"hidden_models": ["a", "b", "a", "", "  ", 3, None, "c"]}).hidden_models == ["a", "b", "c"]
    for bad in ("a", 5, {"a": 1}, None):
        assert settings_from_dict({"hidden_models": bad}).hidden_models == []
    assert AppSettings().hidden_models == [], "nothing is hidden until the user hides it"


def test_non_object_json_gives_defaults():
    assert settings_from_dict([1, 2]) == AppSettings()
    assert settings_from_dict(None) == AppSettings()


def test_projects_are_deduplicated_when_loading_and_adding(tmp_path):
    folder = tmp_path / "proj"
    folder.mkdir()
    loaded = settings_from_dict({"projects": [str(folder), str(folder) + os.sep, str(folder / ".." / "proj"), ""]})
    assert len(loaded.projects) == 1
    settings = AppSettings()
    assert settings.add_project(folder) is True
    assert settings.add_project(str(folder) + os.sep) is False
    assert settings.remove_project(folder) is True
    assert settings.remove_project(folder) is False
    assert settings.projects == []


@pytest.mark.skipif(os.name != "nt", reason="Windows paths are case-insensitive")
def test_same_folder_ignores_case_on_windows(tmp_path):
    assert same_folder(str(tmp_path).upper(), str(tmp_path).lower())


def test_settings_path_resolution():
    assert default_settings_path({SETTINGS_ENV: "C:/x/s.json", "APPDATA": "C:/ignored"}).as_posix() == "C:/x/s.json"
    assert default_settings_path({"APPDATA": "C:/Users/u/AppData/Roaming"}).as_posix().endswith(
        "AppData/Roaming/WittyCode/settings.json"
    )
    assert default_settings_path({}).name == "settings.json"
    assert legacy_settings_path({"APPDATA": "C:/Users/u/AppData/Roaming"}).as_posix().endswith(
        "AppData/Roaming/droid-desk/settings.json"
    )
    assert legacy_settings_path({SETTINGS_ENV: "C:/x/s.json", "APPDATA": "C:/a"}).as_posix() == "C:/a/droid-desk/settings.json"


# ------------------------------------------------------------------ the move from %APPDATA%\droid-desk to %APPDATA%\WittyCode
# Every test passes its own environment mapping (APPDATA = a temporary folder): nothing here can read or write a real profile.


def profile(tmp_path, *, old: str | None = None, new: str | None = None):
    env = {"APPDATA": str(tmp_path)}
    for folder, content in (("droid-desk", old), ("WittyCode", new)):
        if content is not None:
            (tmp_path / folder).mkdir(exist_ok=True)
            (tmp_path / folder / "settings.json").write_text(content, encoding="utf-8")
    return env, tmp_path / "droid-desk" / "settings.json", tmp_path / "WittyCode" / "settings.json"


def test_the_old_settings_are_copied_to_the_new_folder_and_left_where_they_were(tmp_path):
    old_settings = AppSettings(
        default_model="chatgpt:gpt-6.1-sol", chatgpt_autonomy="full", projects=[str(tmp_path / "proj")], splitter_sizes=[200, 800]
    )
    save_settings(old_settings, tmp_path / "droid-desk" / "settings.json")
    env, old, new = profile(tmp_path)
    before = old.read_bytes()
    assert not new.parent.exists()
    assert migrate_legacy_settings(env) == new
    assert new.read_bytes() == before
    assert old.read_bytes() == before, "the old file is copied, never moved or changed"
    assert load_settings(new) == old_settings
    assert sorted(path.name for path in new.parent.iterdir()) == ["settings.json"], "no staging file is left behind"


def test_the_copy_happens_once_and_later_edits_of_either_file_are_left_alone(tmp_path):
    env, old, new = profile(tmp_path, old='{"chatgpt_autonomy": "full"}')
    assert migrate_legacy_settings(env) == new
    new.write_text('{"chatgpt_autonomy": "ask"}', encoding="utf-8")
    old.write_text('{"chatgpt_autonomy": "code"}', encoding="utf-8")
    assert migrate_legacy_settings(env) is None
    assert load_settings(new).chatgpt_autonomy == "ask"
    assert load_settings(old).chatgpt_autonomy == "code"


def test_existing_new_settings_win_over_the_old_ones(tmp_path):
    env, old, new = profile(tmp_path, old='{"chatgpt_autonomy": "full"}', new='{"chatgpt_autonomy": "ask"}')
    assert migrate_legacy_settings(env) is None
    assert load_settings(new).chatgpt_autonomy == "ask"
    assert old.read_text(encoding="utf-8") == '{"chatgpt_autonomy": "full"}'


def test_without_old_settings_nothing_is_created(tmp_path):
    env, old, new = profile(tmp_path)
    assert migrate_legacy_settings(env) is None
    assert not (tmp_path / "WittyCode").exists() and not (tmp_path / "droid-desk").exists()
    (tmp_path / "droid-desk").mkdir()
    (tmp_path / "droid-desk" / "droid-desk.log").write_text("old log", encoding="utf-8")
    assert migrate_legacy_settings(env) is None, "only settings.json is carried over, not the log"
    assert not new.exists()


def test_an_explicit_settings_file_is_never_filled_from_the_old_settings(tmp_path):
    env, old, new = profile(tmp_path, old='{"chatgpt_autonomy": "full"}')
    chosen = tmp_path / "somewhere" / "mine.json"
    assert migrate_legacy_settings({**env, SETTINGS_ENV: str(chosen)}) is None
    assert not chosen.exists() and not new.exists() and not chosen.parent.exists()


def test_a_copy_that_fails_leaves_no_half_written_settings(tmp_path, monkeypatch):
    env, old, new = profile(tmp_path, old='{"chatgpt_autonomy": "full"}')

    def dying_copy(source, destination, **_kwargs):
        with open(destination, "w", encoding="utf-8") as handle:
            handle.write('{"autonom')
        raise OSError("disk full")

    real_shutil = settings_module.shutil
    monkeypatch.setattr(settings_module, "shutil", SimpleNamespace(copyfile=dying_copy))
    assert migrate_legacy_settings(env) is None
    assert not new.exists(), "a truncated file here would count as 'the new settings exist' from then on"
    assert sorted(path.name for path in new.parent.iterdir()) == []
    assert old.read_text(encoding="utf-8") == '{"chatgpt_autonomy": "full"}'
    monkeypatch.setattr(settings_module, "shutil", real_shutil)
    assert migrate_legacy_settings(env) == new, "the next start tries again"


def test_a_copy_does_not_overwrite_settings_another_instance_wrote_meanwhile(tmp_path, monkeypatch):
    env, old, new = profile(tmp_path, old='{"chatgpt_autonomy": "full"}')
    real_copy = settings_module.shutil.copyfile

    def copy_then_lose_the_race(source, destination, **kwargs):
        real_copy(source, destination, **kwargs)
        new.write_text('{"chatgpt_autonomy": "ask"}', encoding="utf-8")

    monkeypatch.setattr(settings_module, "shutil", SimpleNamespace(copyfile=copy_then_lose_the_race))
    assert migrate_legacy_settings(env) is None
    assert load_settings(new).chatgpt_autonomy == "ask"
    assert [path.name for path in new.parent.iterdir()] == ["settings.json"]


def test_the_default_location_is_what_the_application_loads_after_the_move(tmp_path, monkeypatch):
    env, old, new = profile(tmp_path, old='{"chatgpt_autonomy": "full", "mode": "spec"}')
    monkeypatch.setenv("APPDATA", env["APPDATA"])
    monkeypatch.delenv(SETTINGS_ENV, raising=False)
    assert default_settings_path() == new
    assert migrate_legacy_settings() == new
    loaded = load_settings()
    assert (loaded.chatgpt_autonomy, loaded.mode) == ("full", "spec")
    save_settings(AppSettings(chatgpt_autonomy="ask"))
    assert load_settings(new).chatgpt_autonomy == "ask" and load_settings(old).chatgpt_autonomy == "full", "saves go to the new folder only"


def test_settings_to_dict_is_json_serializable():
    json.dumps(settings_to_dict(AppSettings(projects=["x"])))