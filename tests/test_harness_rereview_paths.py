"""F5: a network or device path must be refused before anything resolves it.

``\\\\host\\share`` went through ``resolve_path`` (and so ``Path.resolve``) on the way to the permission decision, which
makes Windows connect to that host and authenticate as this account before anyone approved the call. The check is now
lexical and comes first. It also covers ``\\\\?\\C:\\...``: that form survives ``resolve``, so a protected path written
that way used to look like an ordinary folder outside the project.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from desktop.core.harness import fs_tools
from desktop.core.harness.permissions import LEVELS, decide
from desktop.core.harness.shell import SHELL
from desktop.core.harness.tools import ToolContext, ToolError, ToolRegistry, ToolSpec

HOST = r"\\review.invalid\share"
"""``.invalid`` can never resolve, and the tests assert that nothing even tries."""


@pytest.fixture
def project(tmp_path: Path) -> Path:
    folder = tmp_path / "proj"
    folder.mkdir()
    (folder / "a.txt").write_text("alpha\n", encoding="utf-8")
    return folder


@pytest.fixture
def secret(tmp_path: Path) -> Path:
    folder = tmp_path / "WittyCode" / "chatgpt"
    folder.mkdir(parents=True)
    (folder / "marker.txt").write_text("SENTINEL-NOT-A-REAL-SECRET", encoding="utf-8")
    return folder


@pytest.fixture
def ctx(project: Path, secret: Path) -> ToolContext:
    return ToolContext(project=project, protected=(secret,))


@pytest.fixture
def no_resolve(monkeypatch):
    """Nothing may resolve a path naming the host; collects every attempt so a test can show it never happened."""
    real = Path.resolve
    tried: list[str] = []

    def spy(self, *args, **kwargs):
        if "review.invalid" in str(self):
            tried.append(str(self))
            raise AssertionError(f"a network path was resolved: {self}")
        return real(self, *args, **kwargs)

    monkeypatch.setattr(Path, "resolve", spy)
    return tried


REMOTE = [
    HOST,
    HOST + r"\x.txt",
    r"\\review.invalid\share\..\other",
    r"//review.invalid/share/x.txt",
    r"\\?\UNC\review.invalid\share\x.txt",
    r"\\.\PhysicalDrive0",
    r"\\?\C:\Windows\win.ini",
    r"\\127.0.0.1\c$\x.txt",
]


@pytest.mark.parametrize("raw", REMOTE)
def test_a_network_or_device_path_is_refused_without_being_resolved(ctx, raw, no_resolve):
    assert fs_tools.is_remote_path(raw)
    with pytest.raises(ToolError) as problem:
        fs_tools.resolve_path(ctx, raw)
    assert "network" in str(problem.value) and "local" in str(problem.value)
    assert not no_resolve


@pytest.mark.parametrize("raw", [r"C:\Windows\win.ini", "a.txt", "./a.txt", r".\a.txt", "sub/a.txt", "sub//a.txt", r"C:/x", "~/x"])
def test_an_ordinary_path_is_not_taken_for_a_network_one(ctx, raw):
    assert not fs_tools.is_remote_path(raw)
    assert fs_tools.resolve_path(ctx, raw)


def test_a_quoted_network_path_is_refused_too(ctx, no_resolve):
    assert fs_tools.is_remote_path(f'"{HOST}"')
    with pytest.raises(ToolError):
        fs_tools.resolve_path(ctx, f'  "{HOST}"  ')
    for level in LEVELS:
        decision = decide(level, fs_tools.READ_FILE, {"file_path": f'"{HOST}\\x.txt"'}, ctx, remembered=set())
        assert decision.verdict == "deny", level
    assert not no_resolve


def test_a_tool_this_module_does_not_know_is_refused_a_network_path_too(ctx, no_resolve):
    """An MCP tool: only the conventional argument names are trusted, and they are read the same way."""
    spec = ToolSpec(name="mcp_reader", description="", parameters={}, ui_name="Read", kind="read")
    for level in LEVELS:
        assert decide(level, spec, {"path": HOST + r"\x.txt"}, ctx, remembered={"read:?"}).verdict == "deny", level
    assert decide("code", spec, {"path": "a.txt"}, ctx, remembered=set()).verdict == "allow"
    assert not no_resolve


def test_a_command_that_names_a_network_folder_is_asked_about_without_listing_it(ctx, no_resolve, monkeypatch):
    """The text of a command is the user's to approve; working out where its wildcard leads must not list the share."""
    real = fs_tools.os.scandir
    listed: list[str] = []

    def spy(path=".", *args, **kwargs):
        if "review.invalid" in str(path):
            listed.append(str(path))
            raise AssertionError(f"a network folder was listed: {path}")
        return real(path, *args, **kwargs)

    monkeypatch.setattr(fs_tools.os, "scandir", spy)
    for command in (f"cat {HOST}\\x.txt", f"cat {HOST}\\*", f"Get-ChildItem {HOST}\\*\\x.txt", "cat //review.invalid/share/*"):
        assert decide("code", SHELL, {"command": command}, ctx, remembered=set()).verdict == "ask", command
    assert not listed and not no_resolve


def calls(ctx: ToolContext) -> list[tuple[str, object, dict]]:
    return [
        ("read_file", fs_tools.READ_FILE, {"file_path": HOST + r"\x.txt"}),
        ("list_dir", fs_tools.LIST_DIR, {"directory_path": HOST}),
        ("glob", fs_tools.GLOB, {"patterns": ["*"], "folder": HOST}),
        ("grep", fs_tools.GREP, {"pattern": "x", "path": HOST}),
        ("create_file", fs_tools.CREATE_FILE, {"file_path": HOST + r"\x.txt", "content": "x"}),
        ("edit_file", fs_tools.EDIT_FILE, {"file_path": HOST + r"\x.txt", "old_str": "a", "new_str": "b"}),
        ("shell", SHELL, {"command": "dir", "cwd": HOST}),
    ]


def test_no_tool_resolves_a_network_path_while_the_level_is_decided(ctx, no_resolve):
    for name, spec, args in calls(ctx):
        for level in LEVELS:
            decision = decide(level, spec, args, ctx, remembered={"read", "edit", "exec:dir", "read:?", "edit:?"})
            assert decision.verdict == "deny", (name, level, decision)
            assert "network or device path" in decision.reason, (name, decision.reason)
        assert not no_resolve, name


def test_every_tool_refuses_a_network_path_when_it_runs(ctx, no_resolve):
    registry = ToolRegistry.default()
    for name, spec, args in calls(ctx):
        result = asyncio.run(registry.run(spec.name, args, ctx))
        assert result.is_error, (name, result.output)
        assert "network" in result.output or "device" in result.output, (name, result.output)
        assert not no_resolve, name


def test_a_protected_path_written_as_an_extended_length_path_is_refused(ctx, secret):
    """``Path.resolve`` keeps the ``\\\\?\\`` prefix, so such a path used to miss the protected check as well."""
    raw = "\\\\?\\" + str(secret / "marker.txt")
    result = asyncio.run(ToolRegistry.default().run("read_file", {"file_path": raw}, ctx))
    assert result.is_error and "SENTINEL" not in result.output
    for level in LEVELS:
        assert decide(level, fs_tools.READ_FILE, {"file_path": raw}, ctx, remembered=set()).verdict == "deny", level


def test_an_ordinary_call_still_works(ctx):
    result = asyncio.run(ToolRegistry.default().run("read_file", {"file_path": "a.txt"}, ctx))
    assert not result.is_error and "alpha" in result.output
