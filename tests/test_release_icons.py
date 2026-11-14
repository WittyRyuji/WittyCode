"""The launcher and its GUI interpreter must not fall back to a Python icon."""

from pathlib import Path
import struct
from types import SimpleNamespace

import pytest

from scripts import build_release
from scripts.set_exe_icon import icon_resources, set_icon


@pytest.fixture
def ico(tmp_path):
    file = tmp_path / "app.ico"
    images = [b"small-icon-image", b"large-icon-image"]
    offset = 6 + 16 * len(images)
    entries = []
    for size, image in zip((16, 32), images):
        entries.append(struct.pack("<BBBBHHII", size, size, 0, 0, 1, 32, len(image), offset))
        offset += len(image)
    file.write_bytes(struct.pack("<HHH", 0, 1, len(images)) + b"".join(entries) + b"".join(images))
    return file


class Resources:
    def __init__(self, *, fail=False, groups=True):
        self.fail = fail
        self.groups = groups
        self.freed = False
        self.finished = []
        self.updates = []
        self.values = {(16, 1, 0): b"version-info", (24, 1, 0): b"application-manifest"}

    def LoadLibraryEx(self, file, handle, flags):
        assert flags == 2
        return 1

    def EnumResourceTypes(self, module):
        return [3, 14, 16, 24] if self.groups else [16, 24]

    def EnumResourceNames(self, module, kind):
        assert kind == 14
        return [1, 101]

    def EnumResourceLanguages(self, module, kind, name):
        return [0, 1033]

    def FreeLibrary(self, module):
        self.freed = True

    def BeginUpdateResource(self, file, delete):
        assert self.freed and delete is False
        return 2

    def UpdateResource(self, handle, kind, name, data, language):
        if self.fail:
            raise OSError("The example resource is not writable")
        self.updates.append((kind, name, language, data))

    def EndUpdateResource(self, handle, discard):
        self.finished.append(discard)
        if not discard:
            self.values.update({(kind, name, language): data for kind, name, language, data in self.updates})


def test_ico_images_become_a_group_with_matching_resource_ids(ico):
    group, images = icon_resources(ico)
    assert struct.unpack_from("<HHH", group) == (0, 1, 2)
    assert images == [b"small-icon-image", b"large-icon-image"]
    assert [struct.unpack_from("<BBBBHHIH", group, 6 + 14 * index)[-1] for index in range(2)] == [1, 2]


@pytest.mark.parametrize("content", [b"", b"not-an-icon", struct.pack("<HHH", 0, 1, 0), struct.pack("<HHH", 0, 1, 2), struct.pack("<HHHBBBBHHII", 0, 1, 1, 16, 16, 0, 0, 1, 32, 100, 22)])
def test_invalid_or_truncated_ico_is_rejected_before_updating_an_executable(tmp_path, content):
    file = tmp_path / "broken.ico"
    file.write_bytes(content)
    with pytest.raises(ValueError):
        set_icon(tmp_path / "app.exe", file, api=SimpleNamespace())


def test_all_existing_icon_groups_and_languages_are_replaced_without_changing_other_resources(ico, tmp_path):
    api = Resources()
    original = dict(api.values)
    group, images = icon_resources(ico)
    set_icon(tmp_path / "pythonw.exe", ico, api=api)
    assert api.finished == [False]
    assert all(api.values[key] == data for key, data in original.items())
    for name in (1, 101):
        for language in (0, 1033):
            assert api.values[14, name, language] == group
            for identifier, image in enumerate(images, 1):
                assert api.values[3, identifier, language] == image
    assert {kind for kind, _, _, _ in api.updates} == {3, 14}


def test_an_executable_without_icons_gets_a_default_group(ico, tmp_path):
    api = Resources(groups=False)
    set_icon(tmp_path / "app.exe", ico, api=api)
    assert (14, 1, 0) in api.values


def test_a_failed_icon_update_is_discarded(ico, tmp_path):
    api = Resources(fail=True)
    before = dict(api.values)
    with pytest.raises(OSError, match="not writable"):
        set_icon(tmp_path / "app.exe", ico, api=api)
    assert api.finished == [True] and api.values == before


def test_the_real_application_ico_has_small_and_high_dpi_images():
    group, images = icon_resources(Path(__file__).resolve().parents[1] / "desktop/assets/icon.ico")
    count = struct.unpack_from("<HHH", group)[2]
    sizes = {struct.unpack_from("<BB", group, 6 + 14 * index)[0] or 256 for index in range(count)}
    assert {16, 24, 32, 48, 64, 128, 256} <= sizes
    assert len(images) == count


def test_release_notices_disclose_the_gui_interpreter_icon_change(tmp_path):
    target = tmp_path / "release"
    target.mkdir()
    build_release.write_notices([], target, tmp_path / "python")
    notices = (target / "THIRD_PARTY_NOTICES.txt").read_text(encoding="utf-8")
    assert "pythonw.exe" in notices and "icon resources" in notices


def test_release_copies_the_ico_and_brands_only_its_own_gui_interpreter(tmp_path, monkeypatch, ico):
    root = tmp_path / "repo"
    asset = root / "desktop/assets/icon.ico"
    asset.parent.mkdir(parents=True)
    asset.write_bytes(ico.read_bytes())
    target = tmp_path / "release"
    target.mkdir()
    monkeypatch.setattr(build_release, "ROOT", root)
    calls = []
    monkeypatch.setattr(build_release.subprocess, "run", lambda argv, **kwargs: calls.append((argv, kwargs)))
    build_release.brand_gui_runtime(target)
    assert (target / "WittyCode.ico").read_bytes() == ico.read_bytes()
    assert calls == [([
        str(root / ".venv-build/Scripts/python.exe"), str(root / "scripts/set_exe_icon.py"),
        str(target / "python/pythonw.exe"), str(target / "WittyCode.ico"),
    ], {"check": True})]
