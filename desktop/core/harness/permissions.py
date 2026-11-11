"""Permission levels, the decision for one tool call, and the approval card shown to the user.

Levels (``LEVELS``): ``ask`` lets reads inside the project through and asks for every write and command; ``code`` (the
default) also lets writes inside the project through and runs a command only if it is a single read-only one from the
allowlist below; ``full`` allows everything. Anything outside the project folder (a read, a write) asks at ``ask`` and
``code``. There is no operating-system sandbox: the project check is on resolved paths (``..``, symlinks, junctions and
case are dealt with), and a shell command is judged by its text and by where its arguments resolve to.

``ToolContext.protected`` (settings, credentials) is denied at every level, ``full`` included: a tool whose path
resolves into it, a command whose text names it, and a command one of whose arguments resolves into it (a directory
junction or symlink inside the project is therefore no way in). A deny cannot be overridden by ``remembered``.

"Don't ask again this session" is a key (``remember_key``): ``exec:<program>`` for commands, ``edit`` for writes
inside the project, ``edit:<folder>`` for writes elsewhere, ``read:<folder>`` for reads elsewhere. One deliberate
narrowing: a remembered ``exec:`` key does not cover a command that chains or redirects (``git status; Remove-Item ...``),
because approving ``git`` must not approve whatever follows the semicolon.

The read-only shell allowlist is a recogniser, not a parser: anything it cannot be sure about asks. A command passes only
if it is one allowlisted command, or two joined by a single ``|``, and its text outside quotes has none of
``; & < > ` { } ( ) $`` or a newline (so no chaining, redirection, sub-expression, script block or variable), no word
such as ``Remove-Item`` / ``Invoke-Expression``, and no argument that leads outside the project. Quoted text may hold
those characters (``rg "a|b"``), except ``$`` and a backtick in double quotes, which PowerShell expands.

Every argument of such a command is resolved with its links followed, against the command's ``cwd``; one that cannot be
resolved for certain (a wildcard standing for a folder, a drive-relative name) counts as outside, which means asking.
"""

from __future__ import annotations

import os
import re
import stat
import struct
from collections.abc import Iterator, Mapping
from dataclasses import dataclass
from pathlib import Path, PureWindowsPath
from typing import Any

from desktop.core.diffs import diff_for_new_file, diff_from_fragments
from desktop.core.harness import fs_tools
from desktop.core.harness.shell import (
    argument_pieces,
    argument_targets,
    command_tokens,
    mentioned_protected,
    protected_argument,
)
from desktop.core.harness.tools import ToolContext, ToolError, ToolSpec
from desktop.core.prompts import OPTION_LABELS, ActionView, OptionView

LEVELS = ("ask", "code", "full")


@dataclass(frozen=True)
class Decision:
    verdict: str  # "allow" | "ask" | "deny"
    reason: str = ""


_PATH_ARGS = {
    "read_file": "file_path",
    "list_dir": "directory_path",
    "create_file": "file_path",
    "edit_file": "file_path",
    "glob": "folder",
    "grep": "path",
    "shell": "cwd",
}
_REQUIRED_PATH = frozenset({"read_file", "list_dir", "create_file", "edit_file"})
_FALLBACK_ARGS = ("file_path", "path", "directory_path", "folder", "cwd")


def _args(args: Any) -> Mapping[str, Any]:
    return args if isinstance(args, Mapping) else {}


def _text(args: Mapping[str, Any], key: str) -> str | None:
    value = args.get(key)
    return value if isinstance(value, str) and value.strip() else None


def _command(args: Mapping[str, Any]) -> str:
    value = args.get("command")
    return value if isinstance(value, str) else ""


# --------------------------------------------------------------------------- paths


def _locate(ctx: ToolContext, raw: str | None) -> tuple[Path | None, bool]:
    """The resolved path of a raw argument (the project when absent) and whether it is inside the project.
    An argument that cannot be resolved is (None, False): treated as outside."""
    if raw is None:
        return ctx.project.resolve(), True
    try:
        return fs_tools.resolve_path(ctx, raw)
    except ToolError:
        return None, False


def _pattern_roots(args: Mapping[str, Any]) -> list[str]:
    """The fixed leading part of each glob pattern. ``../x/*.py`` and ``D:/other/*`` point away from the folder."""
    patterns = args.get("patterns")
    roots = []
    for pattern in patterns if isinstance(patterns, (list, tuple)) else ():
        if not isinstance(pattern, str):
            continue
        for item in fs_tools.expand_braces(pattern):
            fixed = []
            for part in item.replace("\\", "/").split("/"):
                if fs_tools.has_magic(part):
                    break
                fixed.append(part)
            if fixed:
                roots.append("/".join(fixed))
    return roots


def _touched(spec: ToolSpec, args: Mapping[str, Any], ctx: ToolContext) -> list[tuple[Path | None, bool]]:
    """Every location a call reads or writes, as ``(resolved path or None, inside the project)``."""
    key = _PATH_ARGS.get(spec.name)
    if key is None:
        # A tool this module does not know (an MCP tool): trust only the conventional argument names.
        key = next((name for name in _FALLBACK_ARGS if _text(args, name)), None)
        if key is None:
            return [(None, False)] if spec.kind in ("read", "edit") else [(ctx.project.resolve(), True)]
    raw = _text(args, key)
    if raw is None and spec.name in _REQUIRED_PATH:
        return [(None, False)]
    found = [_locate(ctx, raw)]
    if spec.name == "glob":
        base = found[0][0] or ctx.project.resolve()
        found += [_locate(ctx, str(base / root)) for root in _pattern_roots(args)]
    return found


def _protected_reason(spec: ToolSpec, args: Mapping[str, Any], ctx: ToolContext) -> str | None:
    if not ctx.protected:
        return None
    for path, _ in _touched(spec, args, ctx):
        if path is not None and fs_tools.is_protected(ctx, path):
            return f"Access to {path} is not allowed: it is a protected location (application settings or credentials). No tool may read or change it."
    if spec.kind == "exec":
        command = _command(args)
        hit = mentioned_protected(command, ctx.protected)
        if hit is not None:
            return f"The command mentions {hit}, a protected location (application settings or credentials). It will not be run."
        cwd, _ = _locate(ctx, _text(args, "cwd"))
        leads = protected_argument(command, cwd or ctx.project.resolve(), ctx.protected)
        if leads is not None:
            return (
                f"The argument {leads[0]!r} leads to {leads[1]}, a protected location (application settings or "
                "credentials). The command will not be run."
            )
    return None


def _remote_path(spec: ToolSpec, args: Mapping[str, Any]) -> str | None:
    """A path argument that names a network share or a device, by its text alone.

    Refused at every level, ``full`` included, because no tool acts on one either: the message is the whole answer,
    and asking the user about it would mean resolving the path (which connects to that host) just to show what it is.
    """
    key = _PATH_ARGS.get(spec.name) or next((name for name in _FALLBACK_ARGS if _text(args, name)), None)
    raw = _text(args, key) if key else None
    return raw if raw is not None and fs_tools.is_remote_path(raw) else None


# --------------------------------------------------------------------------- shell text

_FORBIDDEN = re.compile(r"[;&<>`{}()$\r\n]")
_DANGEROUS_WORDS = re.compile(
    r"(?<![\w-])(?:invoke-expression|iex|remove-item|ri|rm|rmdir|rd|del|erase|set-content|add-content|clear-content|out-file|"
    r"new-item|move-item|copy-item|rename-item|set-item|start-process|invoke-command|invoke-webrequest|invoke-restmethod|"
    r"iwr|irm|stop-process|set-location)(?![\w-])",
    re.IGNORECASE,
)
_CHAINING = re.compile(r"[;&|<>`\r\n]|\$\(|\$\{")

_SIMPLE_COMMANDS = frozenset(
    {
        "dir", "ls", "get-childitem", "gci", "cat", "type", "get-content", "gc", "pwd", "get-location", "echo",
        "write-output", "where", "get-command", "rg", "findstr", "select-string", "sls", "select-object",
        "measure-object", "sort-object", "head", "tail", "wc",
    }
)  # fmt: skip
_GIT_READ = frozenset({"status", "diff", "log", "show", "blame", "rev-parse", "ls-files"})
_GIT_BRANCH_FLAGS = frozenset({"-a", "-r", "-v", "-vv", "--all", "--remotes", "--list", "--show-current", "--verbose"})
_RG_EXECUTES = ("--pre", "--hostname-bin", "-z", "--search-zip")
_EXECUTABLE_SUFFIXES = (".exe", ".cmd", ".bat", ".com", ".ps1")


def _follows_links(item: str) -> bool:
    """ripgrep's ``-L`` / ``--follow``, which makes it walk through a directory link; ``-l`` is another flag, so the
    short form is matched with its case, cluster included (``-Ln``)."""
    return item == "--follow" or (item.startswith("-") and not item.startswith("--") and "L" in item)


def _mask(command: str) -> tuple[str, bool] | None:
    """The command with the inside of quotes blanked, and whether a double-quoted part expands something (``$``, a
    backtick). None when a quote is left open. Any quote character opens a string, as in PowerShell."""
    out: list[str] = []
    quote = ""
    expands = False
    for char in command:
        if quote:
            if char == quote:
                quote = ""
                out.append(char)
            else:
                if quote == '"' and char in "$`":
                    expands = True
                out.append("_")
        elif char in "'\"":
            quote = char
            out.append(char)
        else:
            out.append(char)
    if quote:
        return None
    return "".join(out), expands


def _program(token: str) -> str | None:
    """The bare command name, lower-cased and without ``.exe``; None for a path (``.\\git``, ``C:\\x\\git.exe``)."""
    if "\\" in token or "/" in token:
        return None
    name = token.lower()
    return name.removesuffix(".exe")


def _points_outside(token: str, cwd: Path, project: Path) -> bool:
    """Does an argument lead outside the project? ``/s``, ``-Recurse`` and ``--format=%h`` are options, not paths; the
    value of ``--opt=value`` / ``-Path:value`` / ``/f:value`` is checked on its own.

    Every argument is resolved, not only the ones that look like a path: ``linked-private/x.txt`` is an ordinary
    relative name and a junction all the same. A wildcard is judged by what it actually picks, not by the folder in
    front of it: ``linked*`` names no folder of its own, and the entry it stands for may be a link out of the project.
    An argument whose target cannot be known counts as outside, so the command is asked about rather than allowed.
    """
    for value in argument_pieces(token):
        for target in argument_targets(value, cwd):
            if target is None or not fs_tools.is_within(target, project):
                return True
    return False


_WALKERS = frozenset({"dir", "ls", "get-childitem", "gci"})
_FILE_READERS = frozenset({"cat", "type", "get-content", "gc", "select-string", "sls"})
_RECURSE = re.compile(r"-r(e(c(u(r(s(e)?)?)?)?)?)?|-dep(t(h)?)?")


def _walks_tree(tokens: list[str]) -> bool:
    """A directory listing that goes down into the folders (``-Recurse`` in any abbreviation, ``-Depth``, ``dir /s``)."""
    if not tokens or _program(tokens[0]) not in _WALKERS:
        return False
    for item in tokens[1:]:
        lowered = item.lower().split(":", 1)[0]
        if _RECURSE.fullmatch(lowered) or lowered.startswith("/s"):
            return True
    return False


def _reads_walked_files(first: list[str], second: list[str]) -> bool:
    """``<recursive listing> | <command that reads the files it is given>``. No argument of such a command names a link that
    lies somewhere in the tree, yet the walk goes through it and the second command reads what it finds there. Listing
    names alone gives nothing of the files away and stays free."""
    return _walks_tree(first) and bool(second) and _program(second[0]) in _FILE_READERS


def _segment_is_readonly(tokens: list[str], cwd: Path, project: Path) -> bool:
    if not tokens:
        return False
    name = _program(tokens[0])
    rest = tokens[1:]
    if name is None:
        return False
    if name == "git":
        if not rest:
            return False
        sub = rest[0].lower()
        args = rest[1:]
        if sub == "remote":
            if [item.lower() for item in args] not in (["-v"], ["--verbose"]):
                return False
        elif sub == "branch":
            if any(item.lower() not in _GIT_BRANCH_FLAGS for item in args):
                return False
        elif sub in _GIT_READ:
            if any(item.lower().startswith(("--output", "--ext-diff")) for item in args):
                return False
        else:
            return False
    elif name in ("python", "node"):
        if rest != ["--version"]:
            return False
    elif name in _SIMPLE_COMMANDS:
        if name == "rg" and any(item.lower().startswith(_RG_EXECUTES) or _follows_links(item) for item in rest):
            return False
        if name == "where" and any(item.lower() in ("/r", "-r") for item in rest):
            return False  # ``where /r <folder> <pattern>`` walks the tree, links included
        if name == "findstr" and any(item.lower().startswith("/s") for item in rest):
            return False  # ``findstr /s`` reads every file of the tree, whatever links lie in it
    else:
        return False
    return not any(_points_outside(token, cwd, project) for token in rest)


def is_readonly_command(command: str, cwd: Path, project: Path) -> bool:
    """True when the command is one read-only command from the allowlist (or two joined by a single pipe)."""
    if not command.strip() or len(command) > 2000:
        return False
    project = project.resolve()
    try:
        cwd = cwd.resolve()
    except (OSError, ValueError, RuntimeError):
        return False
    if not fs_tools.is_within(cwd, project):
        return False
    masked = _mask(command)
    if masked is None:
        return False
    flat, expands = masked
    if expands or _FORBIDDEN.search(flat) or _DANGEROUS_WORDS.search(command):
        return False
    cuts = [index for index, char in enumerate(flat) if char == "|"]
    if len(cuts) > 1:
        return False
    pieces = [command[: cuts[0]], command[cuts[0] + 1 :]] if cuts else [command]
    parts = [command_tokens(piece) for piece in pieces]
    if len(parts) == 2 and _reads_walked_files(parts[0], parts[1]):
        return False
    return all(_segment_is_readonly(tokens, cwd, project) for tokens in parts)


def _is_compound(command: str) -> bool:
    """Does the command chain, redirect, pipe or substitute? An unreadable one counts as compound."""
    masked = _mask(command)
    if masked is None:
        return True
    flat, expands = masked
    return expands or bool(_CHAINING.search(flat))


def _program_key(command: str) -> str:
    tokens = command_tokens(command.strip().removeprefix("&").strip())
    if not tokens:
        return ""
    name = re.split(r"[\\/]", tokens[0])[-1].lower()
    for suffix in _EXECUTABLE_SUFFIXES:
        if name.endswith(suffix):
            return name[: -len(suffix)]
    return name


# --------------------------------------------------------------------------- what a repository makes Git run

_GIT_RUNS_PROGRAM: dict[str, frozenset[str] | None] = {
    "core": frozenset({"fsmonitor", "hookspath", "sshcommand", "askpass", "editor", "pager", "gitproxy", "alternaterefscommand"}),
    "credential": frozenset({"helper"}),
    "diff": frozenset({"external", "textconv", "command"}),
    "merge": frozenset({"driver"}),
    "filter": frozenset({"clean", "smudge", "process"}),
    "difftool": frozenset({"cmd", "path"}),
    "mergetool": frozenset({"cmd", "path"}),
    "trailer": frozenset({"command"}),
    "remote": frozenset({"uploadpack", "receivepack"}),
    "uploadpack": frozenset({"packobjectshook"}),
    "protocol": frozenset({"command"}),
    "init": frozenset({"templatedir"}),
    "gpg": frozenset({"program"}),
    "browser": frozenset({"cmd", "path"}),
    "web": frozenset({"browser"}),
    "include": frozenset({"path"}),  # another file, which may hold any of the above
    "includeif": frozenset({"path"}),
    "pager": None,  # pager.<command> is itself a command, whatever it is called
}
"""Configuration keys by section and last name whose value is a program, a hook folder or a file that may name one."""

_GIT_NO_PROGRAM = frozenset({"", "true", "false", "yes", "no", "on", "off", "1", "0"})
"""Values that cannot be a program: a boolean turns a setting on or off, and an empty one undoes it."""

_STATUS_HOOK = "post-index-change"
"""The only hook a command from the read-only allowlist runs, so the only one a folder of hooks is asked about for.

``git status`` and ``git diff`` refresh the index, and refreshing it runs this hook out of the default folder with
nothing configured at all (Git 2.50.1.windows.1, traced in isolated repositories). A hook lying in
``<Git folder>/hooks`` therefore counts even though no setting names it.
"""

_HOOK_SUFFIXES = ("", ".exe")
"""The names Git looks for on Windows: the bare one and ``<name>.exe`` (both appear in ``GIT_TRACE``; ``.bat``,
``.cmd``, ``.ps1`` and ``.sh`` are not tried, and a ``.sample`` from the template is not a hook)."""

_GIT_FOLDERS = 64
"""How many Git folders one check looks at: the working tree's own, the shared one of a linked tree, and one per
submodule. A repository with more of them is asked about rather than examined in part."""

_GIT_LINKS = 16 * _GIT_FOLDERS
"""How many submodules one index may record before the check stops looking for which of them are checked out."""

_GITLINK = 0o160000
"""The mode the index records a submodule with (a "gitlink")."""

_GITLINK_MODE = _GITLINK.to_bytes(4, "big")
"""That mode as the index stores it. An index file without these four bytes anywhere records no submodule, which
spares reading its entries one by one."""

_GIT_UNREADABLE = "unreadable Git metadata"
_GIT_REMOTE = "a network or device path in the repository's Git metadata"
_GIT_TOO_MANY = "more Git folders than one check looks at"


class _GitUnknown(Exception):
    """What the repository makes Git run cannot be established, so the command is asked about.

    Raised rather than returned so that no caller can read "this could not be checked" as "nothing of the
    repository's runs"; ``reason`` is what the user is told.
    """

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


@dataclass(frozen=True)
class _GitFolder:
    """One Git folder whose configuration and hooks apply to a Git command run in the working tree.

    ``own`` holds this working tree's own files (``config.worktree``, and ``config`` unless the tree is a linked one),
    ``shared`` the ones a linked working tree shares with the repository (``config``, ``hooks``, ``modules``), and
    ``root`` is the working tree a relative path in that configuration is read against.
    """

    own: Path
    shared: Path
    root: Path


def _metadata_path(told: str, base: Path) -> Path:
    """The local path a piece of Git metadata names (``gitdir:``, ``commondir``, ``core.hooksPath``,
    ``core.worktree``), a relative one read against ``base``.

    By its text, and before anything touches it: a network or a device path here is not a path to look at but a
    reason to ask, because resolving or even stat-ing ``\\\\host\\share`` makes Windows connect to that machine and
    authenticate as this account, and the user has not approved the command yet. The repository chose this text, so
    it is as untrusted as a path the model wrote.
    """
    if fs_tools.is_remote_path(told):
        raise _GitUnknown(_GIT_REMOTE)
    path = Path(told)
    joined = path if path.is_absolute() else base / path
    if fs_tools.is_remote_path(str(joined)):
        raise _GitUnknown(_GIT_REMOTE)
    return joined


def _git_stat(path: Path) -> os.stat_result | None:
    """What one file of Git metadata is, or None when there is no such file. Not ``Path.exists`` or ``is_dir``, which
    answer False to failures of some kinds: here that would be an answer about what Git runs that nobody has."""
    if fs_tools.is_remote_path(str(path)):
        raise _GitUnknown(_GIT_REMOTE)
    try:
        return os.stat(path)
    except (FileNotFoundError, NotADirectoryError):
        return None
    except (OSError, ValueError):
        raise _GitUnknown(_GIT_UNREADABLE) from None


def _git_bytes(path: Path) -> bytes | None:
    """The bytes of one file of Git metadata, or None when there is no such file."""
    if fs_tools.is_remote_path(str(path)):
        raise _GitUnknown(_GIT_REMOTE)
    try:
        return path.read_bytes()
    except (FileNotFoundError, NotADirectoryError):
        return None
    except (OSError, ValueError):
        raise _GitUnknown(_GIT_UNREADABLE) from None


def _git_text(path: Path) -> str | None:
    """The text of one file of Git metadata, or None when there is no such file.

    Decoded from its bytes rather than read in text mode, which ends a line at a lone carriage return: Git keeps one
    inside the line, so ``[diff\\r"review"]`` is the ``diff.review`` section to Git and two lines naming none to text
    mode.
    """
    data = _git_bytes(path)
    return None if data is None else data.decode("utf-8", errors="replace")


def _git_dir_at(folder: Path) -> Path | None:
    """The Git folder of the working tree at ``folder``: its ``.git`` folder, or the one its ``.git`` file names
    (``gitdir: <path>``). None when ``folder`` holds neither."""
    marker = folder / ".git"
    found = _git_stat(marker)
    if found is None:
        return None
    if stat.S_ISDIR(found.st_mode):
        return marker
    told = (_git_text(marker) or "").strip()
    if not told.lower().startswith("gitdir:"):
        return None
    return _metadata_path(told[len("gitdir:") :].strip(), folder)


def _git_dir_of(cwd: Path) -> tuple[Path, Path] | None:
    """The working tree that holds ``cwd`` and its Git folder, or None when ``cwd`` is in no repository."""
    for folder in (cwd, *cwd.parents):
        git_dir = _git_dir_at(folder)
        if git_dir is not None:
            return folder, git_dir
    return None


def _shared_folder(git_dir: Path) -> Path:
    """The folder a linked working tree shares with the repository (``commondir``): where its ``config``, ``hooks``
    and ``modules`` are. The Git folder itself when the tree is not a linked one."""
    told = (_git_text(git_dir / "commondir") or "").strip()
    return _metadata_path(told, git_dir) if told else git_dir


def _submodule_folders(shared: Path) -> list[Path]:
    """The Git folders of the submodules kept inside this one.

    A superproject's ``git status`` refreshes its submodules' indexes, which runs the hook in
    ``modules/<name>/hooks`` (and the one its own ``core.hooksPath`` names instead); a submodule's configuration and
    hooks are therefore part of what the repository makes Git run.
    """
    folder = shared / "modules"
    if fs_tools.is_remote_path(str(folder)):
        raise _GitUnknown(_GIT_REMOTE)
    try:
        with os.scandir(folder) as entries:
            return [Path(entry.path) for _, entry in zip(range(_GIT_FOLDERS + 1), entries) if entry.is_dir()]
    except (FileNotFoundError, NotADirectoryError):
        return []
    except OSError:
        raise _GitUnknown(_GIT_UNREADABLE) from None


def _git_config_files(own: Path, shared: Path) -> list[Path]:
    """The configuration files of one Git folder: its own, and the shared one of a linked working tree."""
    folders = [own] if own == shared else [own, shared]
    return [folder / name for folder in folders for name in ("config", "config.worktree")]


def _git_folder(tree: Path, git_dir: Path) -> tuple[_GitFolder, list[tuple[str, str]]]:
    """One Git folder, found from the working tree ``tree``, and the settings of its configuration.

    Its own ``core.worktree`` names the working tree its relative paths (``core.hooksPath``) belong to, as it does for
    a submodule's folder inside the superproject's.
    """
    shared = _shared_folder(git_dir)
    settings = [pair for path in _git_config_files(git_dir, shared) for pair in _git_settings(_git_text(path) or "")]
    told = next((value for key, value in reversed(settings) if key == "core.worktree"), None)
    root = _metadata_path(told, git_dir) if told else tree
    return _GitFolder(own=git_dir, shared=shared, root=root), settings


def _shared_indexes(git_dir: Path) -> list[Path]:
    """The ``sharedindex.<hash>`` files beside a split index, which hold most of its entries."""
    if fs_tools.is_remote_path(str(git_dir)):
        raise _GitUnknown(_GIT_REMOTE)
    try:
        with os.scandir(git_dir) as entries:
            return [Path(entry.path) for entry in entries if entry.name.lower().startswith("sharedindex.")]
    except (FileNotFoundError, NotADirectoryError):
        return []
    except OSError:
        raise _GitUnknown(_GIT_UNREADABLE) from None


def _varint(data: bytes, at: int) -> tuple[int, int]:
    """A number in Git's offset encoding (index version 4) and where the data after it starts: seven bits a byte,
    and each byte that continues the number adds one before the shift."""
    byte = data[at]
    value = byte & 0x7F
    while byte & 0x80:
        at += 1
        byte = data[at]
        value = ((value + 1) << 7) | (byte & 0x7F)
    return value, at + 1


def _index_gitlinks(data: bytes, size: int) -> list[bytes]:
    """The paths of the gitlink entries of one index file, format versions 2 to 4. ``size`` is the length of an
    object name: 20 bytes, or 32 in a SHA-256 repository.

    Read the way Git reads them: the length in an entry's flags bounds its name unless it is 0xFFF (then the name
    runs to a NUL), version 4 keeps the start of the previous name, and a name is used up to its first NUL.
    """
    if data[:4] != b"DIRC":
        raise ValueError("not an index file")
    version, count = struct.unpack_from(">II", data, 4)
    if version not in (2, 3, 4):
        raise ValueError(f"index version {version}")
    links: list[bytes] = []
    offset, previous = 12, b""
    flags_at = 40 + size  # ten 32-bit fields of stat data, the mode the seventh, then the object name
    for _ in range(count):
        (mode,) = struct.unpack_from(">I", data, offset + 24)
        (flags,) = struct.unpack_from(">H", data, offset + flags_at)
        start = offset + flags_at + (4 if flags & 0x4000 else 2)  # an extended entry has two more bytes of flags
        length = flags & 0xFFF
        if version == 4:
            strip, start = _varint(data, start)
            if strip > len(previous):
                raise ValueError("malformed name in the index")
            kept = previous[: len(previous) - strip]
            end = data.index(b"\0", start) if length == 0xFFF else start + length - len(kept)
            name, after = kept + data[start:end], end + 1
        else:
            end = data.index(b"\0", start) if length == 0xFFF else start + length
            name, after = data[start:end], offset + ((end - offset + 8) & ~7)  # padded with NULs to eight bytes
        if end < start or end >= len(data):
            raise ValueError("malformed name in the index")
        offset, previous = after, name
        if mode & 0o170000 == _GITLINK:
            links.append(name.split(b"\0", 1)[0])
    return links


def _gitlinks(git_dir: Path, settings: list[tuple[str, str]]) -> list[str]:
    """The paths this Git folder's index records as submodules.

    Read out of the index files themselves, because asking Git would run what is being checked for. Every
    ``sharedindex.<hash>`` beside a split index is read too, an outdated one included, which can only add a submodule
    that is no longer there.
    """
    sha256 = any(key == "extensions.objectformat" and value.lower() == "sha256" for key, value in settings)
    links: dict[str, None] = {}
    for path in [git_dir / "index", *_shared_indexes(git_dir)]:
        data = _git_bytes(path)
        if not data or _GITLINK_MODE not in data:
            continue
        try:
            links.update(dict.fromkeys(name.decode("utf-8") for name in _index_gitlinks(data, 32 if sha256 else 20)))
        except (ValueError, IndexError, struct.error):  # a UnicodeDecodeError is a ValueError
            raise _GitUnknown(_GIT_UNREADABLE) from None
        if len(links) > _GIT_LINKS:
            raise _GitUnknown(_GIT_TOO_MANY)
    return list(links)


def _checked_out_submodules(where: _GitFolder, settings: list[tuple[str, str]]) -> list[tuple[Path, Path]]:
    """The working tree and Git folder of each submodule the index records and that is checked out.

    A status or a diff refreshes their indexes as well, which runs their hooks: both those of a ``git submodule``,
    whose Git folder lies in ``modules/<name>``, and those of a repository simply added inside this one, whose
    ``.git`` is a folder in its checkout (traced with Git 2.50.1.windows.1).
    """
    found: list[tuple[Path, Path]] = []
    for link in _gitlinks(where.own, settings):
        tree = _metadata_path(link, where.root)
        git_dir = _git_dir_at(tree)
        if git_dir is not None:
            found.append((tree, git_dir))
    return found


def _continues(line: str) -> bool:
    """Does the value on this line go on with the next one? Git ends a value at a backslash and a newline and reads
    the two of them as nothing at all, so the text after them belongs to the same value and to the same section.

    A backslash takes the character after it with it (``bar\\\\`` is one backslash and ends the line), and a ``#`` or
    ``;`` outside quotes starts a comment, in which a backslash continues nothing.
    """
    quoted = False
    index = 0
    while index < len(line):
        char = line[index]
        if char == "\\":
            if index == len(line) - 1:
                return True
            index += 2
            continue
        if char == '"':
            quoted = not quoted
        elif char in "#;" and not quoted:
            return False
        index += 1
    return False


def _git_lines(text: str) -> Iterator[str]:
    """The lines of a Git configuration file as Git reads them: only ``\\n`` (or ``\\r\\n``) ends one, and a value that
    ends in a backslash carries the next line along.

    Both decide which section a key lands in, and ``str.splitlines`` gets both wrong: it also breaks at U+0085 and
    U+2028, which Git keeps inside a subsection name (the keys after the break would seem to belong to no section at
    all), and it leaves a continued value behind, whose text swallows the header that follows it (after ``foo = bar \\``
    a ``[core]`` line is part of that value, and Git reads the next key in the section above it).
    """
    pending = ""
    for raw in text.replace("\r\n", "\n").split("\n"):
        line = pending + raw
        if _continues(line):
            pending = line[:-1]
            continue
        pending = ""
        yield line
    if pending:
        yield pending


def _header_parts(inside: str) -> tuple[str, str]:
    """The name and the subsection of ``[name "sub"]``. Any run of whitespace separates them, a Tab included:
    ``[diff\\t"review"]`` is the ``diff.review`` section and Git reads its ``textconv`` from there."""
    parts = inside.strip().split(None, 1)
    return (parts[0] if parts else ""), (parts[1] if len(parts) > 1 else "")


def _git_settings(text: str) -> Iterator[tuple[str, str]]:
    """The ``section.subsection.key`` names and values of one Git configuration file, lower-cased.

    Written to be tolerant rather than exact: a key it does not see is a program it does not notice, so anything it
    cannot read becomes a value it reports. A section header and a key share a line in ``[core] fsmonitor = x``, which
    Git reads that way too.
    """
    section = ""
    for raw in _git_lines(text):
        line = raw.strip().lstrip("\ufeff")
        while line.startswith("["):
            end = line.find("]")
            if end < 0:
                break
            name, sub = _header_parts(line[1:end])
            section = ".".join(part for part in (name.lower(), sub.strip().strip('"').lower()) if part)
            line = line[end + 1 :].strip()
        if not line or line[0] in "#;" or not section:
            continue
        key, sep, value = line.partition("=")
        key = key.strip().lower()
        if key:
            yield f"{section}.{key}", _git_value(value) if sep else "true"


def _git_value(text: str) -> str:
    """The value as Git reads it: quotes are dropped, and a ``#`` or ``;`` outside quotes starts a comment."""
    out: list[str] = []
    quoted = False
    for index, char in enumerate(text):
        if char == '"' and (index == 0 or text[index - 1] != "\\"):
            quoted = not quoted
        elif char in "#;" and not quoted:
            break
        else:
            out.append(char)
    return "".join(out).strip()


def _names_a_program(key: str, value: str) -> bool:
    """Does this setting give Git a program of the repository's choosing to run? ``core.hooksPath`` is a folder of
    hooks rather than a program and is answered for by ``_folder_runs_programs``."""
    if value.lower() in _GIT_NO_PROGRAM:
        return False
    section = key.split(".", 1)[0]
    if section not in _GIT_RUNS_PROGRAM:
        return False
    names = _GIT_RUNS_PROGRAM[section]
    last = key.rpartition(".")[2]
    if names is not None and last not in names:
        return False
    return key != "core.hookspath"


def _status_hook(folder: Path) -> Path | None:
    """The hook in this folder that a command from the read-only allowlist runs, or None when there is none."""
    for suffix in _HOOK_SUFFIXES:
        hook = folder / (_STATUS_HOOK + suffix)
        if _git_stat(hook) is not None:
            return hook
    return None


def _folder_runs_programs(where: _GitFolder, settings: list[tuple[str, str]]) -> str | None:
    """What one Git folder makes Git run: the first setting that names a program, or else the hook Git would run out
    of its folder of hooks."""
    hooks, hooks_key = where.shared / "hooks", None
    for key, value in settings:
        if key == "core.hookspath" and value:
            # Every value is a folder, ``false`` and ``0`` included (Git runs ``false/post-index-change``), and it
            # replaces ``<Git folder>/hooks``: what lies in the default one is not run any more.
            if value.startswith(("~", "%(")):
                return key  # Git expands these itself; the folder is asked about rather than guessed
            hooks, hooks_key = _metadata_path(value, where.root), key
            continue
        if _names_a_program(key, value):
            return key
    hook = _status_hook(hooks)
    if hook is None:
        # Projects point ``core.hooksPath`` at a folder of their own (husky and the like), and no read-only command
        # runs anything else from it.
        return None
    return hooks_key or str(hook)


def git_runs_own_programs(cwd: Path) -> str | None:
    """The first setting of the repository's own configuration, or hook of its own folder of hooks, that makes Git run
    a program the repository chose; None when there is none, and the reason when that cannot be established.

    A Git command that only reads still runs what the repository brought with it: ``git status`` calls a
    ``core.fsmonitor`` command and runs ``post-index-change`` when it refreshes the index, ``git diff`` an external
    diff or a ``textconv`` driver. Both the settings and the hooks live in ``.git``, which a working tree may arrive
    with, so "the command is read-only" does not mean "nothing of the repository's runs". ``shell`` neutralises the
    settings it can without breaking ordinary work; this is for the rest.
    """
    try:
        found = _git_dir_of(cwd)
        if found is None:
            return None
        pending, seen = [found], set()
        while pending:
            tree, git_dir = pending.pop()
            key = os.path.normcase(os.path.normpath(git_dir))
            if key in seen:
                continue
            seen.add(key)
            if len(seen) > _GIT_FOLDERS:
                raise _GitUnknown(_GIT_TOO_MANY)
            where, settings = _git_folder(tree, git_dir)
            program = _folder_runs_programs(where, settings)
            if program is not None:
                return program
            # A status or a diff here refreshes the submodules' indexes too. The checked-out ones the index records
            # are pushed last so that they are looked at first, with the working tree their relative paths belong
            # to; the folders in ``modules`` are looked at as well, whether or not anything is checked out from them.
            pending += [(where.root, module) for module in _submodule_folders(where.shared)]
            pending += _checked_out_submodules(where, settings)
        return None
    except _GitUnknown as unknown:
        return unknown.reason


def _runs_git(command: str) -> bool:
    return any(_program(token) == "git" for token in command_tokens(command))


def _git_internals(spec: ToolSpec, args: Mapping[str, Any], ctx: ToolContext) -> Path | None:
    """The first path of the call that lies in a Git folder (``.git``). What is written there is not project text but
    what Git runs next: ``config`` names programs, ``hooks`` holds them."""
    for path, _ in _touched(spec, args, ctx):
        if path is not None and any(os.path.normcase(part) == ".git" for part in path.parts):
            return path
    return None


# --------------------------------------------------------------------------- decisions


def remember_key(spec: ToolSpec, args: Any, ctx: ToolContext) -> str:
    args = _args(args)
    if spec.kind == "exec":
        return "exec:" + _program_key(_command(args))
    touched = _touched(spec, args, ctx)
    outside = next(((path, inside) for path, inside in touched if not inside), None)
    if spec.kind == "edit":
        if outside is None:
            internals = _git_internals(spec, args, ctx)
            # Not the plain ``edit`` key: approving edits of project files must not approve edits of what Git runs.
            return "edit:" + os.path.normcase(str(internals.parent)) if internals is not None else "edit"
        path = outside[0]
        return "edit:" + os.path.normcase(str(path.parent)) if path is not None else "edit:?"
    if spec.kind == "read":
        if outside is None:
            return "read"
        path = outside[0]
        if path is None:
            return "read:?"
        folder = path if path.is_dir() else path.parent
        return "read:" + os.path.normcase(str(folder))
    return f"tool:{spec.name}"


_NO_EFFECT = frozenset({"todo_write", "skill"})
"""Tools that change nothing and run nothing (``todo_write`` writes down what the model is doing, ``skill`` reads the skill the
user has chosen to keep): allowed at every level."""


def _readable(path: Path | None, ctx: ToolContext) -> bool:
    """Whether ``path`` is inside a folder reads may enter without asking (``ToolContext.readable``)."""
    if path is None or not ctx.readable:
        return False
    return any(path == root or root in path.parents for root in ctx.readable)


def _base(level: str, spec: ToolSpec, args: Mapping[str, Any], ctx: ToolContext) -> Decision:
    if level == "full":
        return Decision("allow", "完全访问：所有操作直接放行")
    if spec.name in _NO_EFFECT:
        return Decision("allow", "只记录待办清单，不碰文件也不运行命令")
    if spec.kind == "exec":
        if level == "code":
            cwd, _ = _locate(ctx, _text(args, "cwd"))
            command = _command(args)
            if cwd is not None and is_readonly_command(command, cwd, ctx.project):
                setting = git_runs_own_programs(cwd) if _runs_git(command) else None
                if setting is not None:
                    return Decision("ask", f"这个仓库的 Git 配置或钩子（{setting}）可能让 Git 运行仓库自己的程序，需要确认")
                return Decision("allow", "只读命令，直接放行")
        return Decision("ask", "运行命令需要确认")
    touched = _touched(spec, args, ctx)
    inside = all(flag for _, flag in touched)
    if spec.kind == "read":
        if inside:
            return Decision("allow", "读取项目目录内的文件")
        if all(flag or _readable(path, ctx) for path, flag in touched):
            return Decision("allow", "读取技能所在的文件夹")
        return Decision("ask", "读取项目目录之外的位置需要确认")
    if spec.kind == "edit":
        if inside and level == "code":
            if _git_internals(spec, args, ctx) is not None:
                return Decision("ask", "修改 Git 仓库内部（.git）的文件需要确认：那里的配置和钩子决定 Git 会运行什么")
            return Decision("allow", "写代码：项目目录内的修改直接放行")
        return Decision("ask", "修改项目目录之外的文件需要确认" if not inside else "修改文件需要确认")
    return Decision("ask", "需要确认")


def decide(level: str, spec: ToolSpec, args: Any, ctx: ToolContext, *, remembered: set[str]) -> Decision:
    args = _args(args)
    denied = _protected_reason(spec, args, ctx)
    if denied is not None:
        return Decision("deny", denied)
    remote = _remote_path(spec, args)
    if remote is not None:
        return Decision(
            "deny",
            f"{remote} is a network or device path (it starts with two separators). The tools only work on local paths: "
            "even resolving this one would make Windows connect to that machine and authenticate as this account. Use a "
            "local path.",
        )
    verdict = _base(level if level in LEVELS else "ask", spec, args, ctx)
    if verdict.verdict == "ask" and remembered and remember_key(spec, args, ctx) in remembered:
        if spec.kind != "exec" or not _is_compound(_command(args)):
            return Decision("allow", "本会话内已允许同类操作")
    return verdict


# --------------------------------------------------------------------------- what the user sees


def _name(path: str) -> str:
    return PureWindowsPath(path).name or path


def _shown_path(spec: ToolSpec, args: Mapping[str, Any], ctx: ToolContext) -> str | None:
    key = _PATH_ARGS.get(spec.name)
    raw = _text(args, key) if key else None
    if raw is None:
        return None
    path, _ = _locate(ctx, raw)
    return str(path) if path is not None else raw


def action_view(tool_use_id: str, spec: ToolSpec, args: Any, ctx: ToolContext) -> ActionView:
    args = _args(args)
    base = {"tool_use_id": tool_use_id, "tool_name": spec.ui_name}
    if spec.kind == "exec":
        return ActionView(kind="execute", title="运行命令", command=_command(args), **base)
    path = _shown_path(spec, args, ctx)
    if spec.name == "edit_file":
        diff = fs_tools.preview_diff(ctx, spec, args)
        if diff is None:
            old, new = args.get("old_str"), args.get("new_str")
            diff = diff_from_fragments(path or "", old if isinstance(old, str) else "", new if isinstance(new, str) else "")
        return ActionView(kind="edit", title=f"编辑文件 {_name(path or '')}", path=path, diffs=(diff,), **base)
    if spec.name == "create_file":
        diff = fs_tools.preview_diff(ctx, spec, args)
        if diff is None:
            content = args.get("content")
            diff = diff_for_new_file(path or "", content if isinstance(content, str) else "")
        return ActionView(kind="create", title=f"创建文件 {_name(path or '')}", path=path, diffs=(diff,), **base)
    if spec.name == "read_file":
        title = f"读取文件 {_name(path or '')}"
    elif spec.name == "list_dir":
        title = f"列出文件夹 {_name(path or '')}"
    elif spec.name == "glob":
        patterns = args.get("patterns")
        shown = ", ".join(str(item) for item in patterns) if isinstance(patterns, (list, tuple)) else ""
        title = f"查找文件 {shown}".strip()
    elif spec.name == "grep":
        title = f"搜索 “{_text(args, 'pattern') or ''}”"
    else:
        title = f"使用工具 {spec.ui_name}"
    return ActionView(kind="other", title=title, path=path, **base)


def permission_options(spec: ToolSpec, args: Any, ctx: ToolContext) -> tuple[OptionView, ...]:
    """The three buttons of an approval card. ``proceed_always`` means: do not ask again this session for this key."""
    return (
        OptionView("proceed_once", OPTION_LABELS["proceed_once"], "Allow once", "primary"),
        OptionView("proceed_always", "允许，本会话内同类操作不再询问", "Allow, and do not ask again this session", "normal"),
        OptionView("cancel", OPTION_LABELS["cancel"], "Deny", "danger"),
    )
