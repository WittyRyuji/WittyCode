"""Set every Windows icon group to the app's ICO, without replacing manifests or version resources.

Run with the release-build interpreter, which already has PyInstaller's Windows resource helpers.
"""

from __future__ import annotations

import argparse
from pathlib import Path
import struct


def icon_resources(file: Path) -> tuple[bytes, list[bytes]]:
    content = file.read_bytes()
    if len(content) < 6:
        raise ValueError("The application icon is not an ICO file")
    reserved, kind, count = struct.unpack_from("<HHH", content)
    if reserved or kind != 1 or not count or len(content) < 6 + 16 * count:
        raise ValueError("The application icon has an invalid ICO directory")
    group = bytearray(content[:6])
    images = []
    for index in range(count):
        w, h, colors, reserved, planes, bits, size, offset = struct.unpack_from("<BBBBHHII", content, 6 + 16 * index)
        if not size or offset < 6 + 16 * count or offset + size > len(content):
            raise ValueError("An application icon image is missing or truncated")
        images.append(content[offset:offset + size])
        group.extend(struct.pack("<BBBBHHIH", w, h, colors, reserved, planes, bits, size, index + 1))
    return bytes(group), images


def set_icon(executable: Path, icon: Path, *, api=None) -> None:
    group, images = icon_resources(icon)
    if api is None:
        from PyInstaller.compat import win32api
        api = win32api
    module = api.LoadLibraryEx(str(executable), 0, 2)
    try:
        groups = [
            (name, language)
            for name in api.EnumResourceNames(module, 14)
            for language in api.EnumResourceLanguages(module, 14, name)
        ] if 14 in api.EnumResourceTypes(module) else []
    finally:
        api.FreeLibrary(module)
    groups = groups or [(1, 0)]
    handle = api.BeginUpdateResource(str(executable), False)
    try:
        for name, language in groups:
            api.UpdateResource(handle, 14, name, group, language)
        for language in {language for _, language in groups}:
            for identifier, image in enumerate(images, 1):
                api.UpdateResource(handle, 3, identifier, image, language)
    except BaseException:
        api.EndUpdateResource(handle, True)
        raise
    api.EndUpdateResource(handle, False)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("executable", type=Path)
    parser.add_argument("icon", type=Path)
    given = parser.parse_args()
    set_icon(given.executable, given.icon)


if __name__ == "__main__":
    main()
