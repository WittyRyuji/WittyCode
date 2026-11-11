"""How the app-server is started: the desktop app's own binary gets the flag the Codex desktop app gives it."""

from __future__ import annotations

from pathlib import Path

from desktop.core.codex.engine import DESKTOP_FLAGS, default_client_factory
from desktop.core.codex.locate import CodexBinary


def binary(source: str) -> CodexBinary:
    return CodexBinary(path=Path("codex.exe"), version="0.166.0", source=source, command=("C:\\codex.exe",))


def test_the_desktop_binary_is_started_like_the_desktop_app_starts_it():
    client = default_client_factory(binary("desktop"))
    assert client.argv == ["C:\\codex.exe", "-c", "features.prefer_mxc=true", "app-server"]
    assert list(DESKTOP_FLAGS) == ["-c", "features.prefer_mxc=true"]


def test_any_other_codex_is_started_plainly():
    for source in ("settings", "path", "test"):
        assert default_client_factory(binary(source)).argv == ["C:\\codex.exe", "app-server"], source
