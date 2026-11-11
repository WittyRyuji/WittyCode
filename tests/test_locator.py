from __future__ import annotations

from pathlib import Path

import pytest

from desktop.core.droid_locator import (
    ENV_OVERRIDE,
    DroidNotFoundError,
    bundled_candidates,
    locate_droid,
    version_key,
)


def make_app(root: Path, name: str, *, with_exe: bool = True) -> Path:
    exe = root / name / "resources" / "bin" / "droid.exe"
    exe.parent.mkdir(parents=True)
    if with_exe:
        exe.write_bytes(b"MZ")
    return exe


def test_version_numbers_compare_numerically_not_textually():
    assert version_key("app-0.208.0") > version_key("app-0.99.0")
    assert version_key("app-0.10.0") > version_key("app-0.9.9")
    assert version_key("app-1.0.0") > version_key("app-0.999.999")


def test_release_sorts_after_its_prerelease_and_prerelease_numbers_are_numeric():
    assert version_key("app-1.2.3") > version_key("app-1.2.3-beta.2")
    assert version_key("app-1.2.3-beta.10") > version_key("app-1.2.3-beta.2")
    assert version_key("app-1.2.3-rc.1") > version_key("app-1.2.3-beta.9")
    assert version_key("app-1.2.4-beta.1") > version_key("app-1.2.3")


def test_short_versions_are_padded():
    assert version_key("app-1.2") == version_key("app-1.2.0")


@pytest.mark.parametrize("name", ["foo", "app-", "app-x.y", "application-1.0.0", "app-1.0.0-", "Factory.exe"])
def test_non_app_folders_are_ignored(name):
    assert version_key(name) is None


def test_bundled_candidates_newest_first_and_only_with_exe(tmp_path):
    for name in ("app-0.9.0", "app-0.10.0", "app-0.10.1-beta.1", "app-junk"):
        make_app(tmp_path, name)
    make_app(tmp_path, "app-9.9.9", with_exe=False)
    found = bundled_candidates(tmp_path)
    assert [version for _, _, version in found] == ["0.10.1-beta.1", "0.10.0", "0.9.0"]
    assert all(path.name == "droid.exe" for _, path, _ in found)


def test_bundled_candidates_missing_root_is_empty(tmp_path):
    assert bundled_candidates(tmp_path / "nope") == []


def test_locate_prefers_settings_override_over_everything(tmp_path):
    override = tmp_path / "custom" / "droid.exe"
    override.parent.mkdir()
    override.write_bytes(b"MZ")
    other = tmp_path / "env-droid.exe"
    other.write_bytes(b"MZ")
    make_app(tmp_path / "Factory", "app-1.0.0")
    location = locate_droid(
        str(override),
        env={ENV_OVERRIDE: str(other), "LOCALAPPDATA": str(tmp_path)},
        which=lambda _: "C:/elsewhere/droid.exe",
    )
    assert location.source == "settings"
    assert location.path == override.resolve()


def test_locate_env_variable_beats_bundled(tmp_path):
    other = tmp_path / "env-droid.exe"
    other.write_bytes(b"MZ")
    make_app(tmp_path / "Factory", "app-1.0.0")
    location = locate_droid(None, env={ENV_OVERRIDE: str(other), "LOCALAPPDATA": str(tmp_path)}, which=lambda _: None)
    assert (location.source, location.path) == ("env", other.resolve())


def test_locate_picks_newest_bundled_and_reports_version(tmp_path):
    make_app(tmp_path / "Factory", "app-0.99.0")
    newest = make_app(tmp_path / "Factory", "app-0.208.0")
    location = locate_droid(None, env={"LOCALAPPDATA": str(tmp_path)}, which=lambda _: None)
    assert (location.source, location.app_version, location.path) == ("bundled", "0.208.0", newest)


def test_locate_falls_back_to_path_then_fails(tmp_path):
    on_path = tmp_path / "bin" / "droid.exe"
    on_path.parent.mkdir()
    on_path.write_bytes(b"MZ")
    env = {"LOCALAPPDATA": str(tmp_path / "empty")}
    assert locate_droid(None, env=env, which=lambda _: str(on_path)).source == "path"
    with pytest.raises(DroidNotFoundError):
        locate_droid(None, env=env, which=lambda _: None)


def test_missing_override_is_an_error_not_a_silent_fallback(tmp_path):
    make_app(tmp_path / "Factory", "app-1.0.0")
    with pytest.raises(DroidNotFoundError):
        locate_droid(str(tmp_path / "gone.exe"), env={"LOCALAPPDATA": str(tmp_path)}, which=lambda _: None)
    with pytest.raises(DroidNotFoundError):
        locate_droid(None, env={ENV_OVERRIDE: str(tmp_path / "gone.exe")}, which=lambda _: None)