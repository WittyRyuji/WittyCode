r"""G1 and G2: what a repository makes Git run, and never touching a network path to find out.

G1, reproduced end to end with Git 2.50.1.windows.1: ``git status`` and ``git diff`` refresh the index, and refreshing
it runs ``post-index-change`` out of the default folder of hooks with nothing configured at all -- the repository's
own, a linked working tree's shared one, a submodule's, and that of a repository simply added inside this one. And
Git separates a section from its subsection with any whitespace (``[diff\t"review"]``), keeps a value going over the
header after a trailing backslash, and keeps U+0085, U+2028 and a lone carriage return inside a line: spellings of a
``textconv`` that a line-by-line, space-splitting, text-mode reader does not see while Git reads and runs them.

G2: the paths inside ``.git`` (``gitdir:``, ``commondir``, ``core.hooksPath``, ``core.worktree``, the submodule paths
of the index) are the repository's own text. Following them must refuse a network or device path by its text alone,
because resolving or even stat-ing ``\\host\share`` makes Windows connect to that machine and authenticate as this
account before the user has approved anything. "Could not be checked" must read as "ask", never as "nothing runs".

The real-Git tests run only in temporary repositories, write nothing but marker files in ``tmp_path``, and pass the
identity per command (``git -c user.name=...``), so the machine's Git configuration is untouched. The network tests
never reach a host: every file-system entry a path with two leading separators could use is recorded and refused.
"""

from __future__ import annotations

import asyncio
import builtins
import os
import shutil
import struct
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Callable

import pytest

from desktop.core.harness import permissions, shell
from desktop.core.harness.permissions import _STATUS_HOOK, decide, git_runs_own_programs, is_readonly_command
from desktop.core.harness.shell import SHELL
from desktop.core.harness.tools import ToolContext, ToolRegistry

windows_only = pytest.mark.skipif(sys.platform != "win32", reason="runs Windows PowerShell")
needs_git = pytest.mark.skipif(shutil.which("git") is None, reason="no Git on PATH")

HOOK_BODY = '#!/bin/sh\necho ran >> "{marker}"\n'


# --------------------------------------------------------------------------- helpers


def verdict(level: str, args: dict, ctx: ToolContext, remembered=frozenset()) -> str:
    return decide(level, SHELL, args, ctx, remembered=set(remembered)).verdict


def git(cwd: Path, *args: str, stdin: str | None = None) -> str:
    """Git in a temporary repository. The identity is passed per command, so nothing of the user's is read or set.
    Bytes in and out, so that Windows does not turn the ``\\n`` of ``--index-info`` into ``\\r\\n``."""
    done = subprocess.run(
        ["git", "-c", "user.name=t", "-c", "user.email=t@e.invalid", *args],
        cwd=cwd, capture_output=True, timeout=120, input=None if stdin is None else stdin.encode("utf-8"),
    )
    assert done.returncode == 0, f"git {' '.join(args)}: {done.stderr.decode('utf-8', 'replace')}"
    return done.stdout.decode("utf-8", "replace")


def plant_hook(folder: Path, marker: Path, name: str = _STATUS_HOOK) -> Path:
    """A harmless hook: it appends one line to a file in ``tmp_path`` and nothing else."""
    folder.mkdir(parents=True, exist_ok=True)
    hook = folder / name
    hook.write_text(HOOK_BODY.format(marker=marker.as_posix()), encoding="utf-8", newline="\n")
    hook.chmod(0o755)
    return hook


def stale_index(path: Path) -> None:
    """Make the index look out of date, which is what makes Git refresh it and run the hook."""
    stamp = time.time() + 5
    os.utime(path, (stamp, stamp))


def configure(git_dir: Path, text: str, name: str = "config") -> None:
    path = git_dir / name
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("ab") as stream:
        stream.write(text.encode("utf-8"))


def new_repo(folder: Path, name: str = "a.txt", *init: str) -> Path:
    folder.mkdir(parents=True)
    git(folder, "init", "-q", *init)
    (folder / name).write_text("alpha\n", encoding="utf-8")
    git(folder, "add", name)
    git(folder, "commit", "-qm", "one")
    return folder


def add_submodule(repo: Path, tmp_path: Path) -> Path:
    """A ``git submodule add`` of a local repository: its Git folder lies in ``.git/modules/sub``."""
    source = new_repo(tmp_path / "source", "s.txt")
    git(repo, "-c", "protocol.file.allow=always", "submodule", "add", "-q", source.as_posix(), "sub")
    git(repo, "commit", "-qm", "with a submodule")
    return repo / "sub"


def add_gitlinks(repo: Path, paths: list[str]) -> None:
    """Index entries for submodules that are not checked out (``--index-info`` takes any path)."""
    head = git(repo, "rev-parse", "HEAD").strip()
    git(repo, "update-index", "--add", "--index-info", stdin="".join(f"160000 {head}\t{path}\n" for path in paths))


def index_file(*names: bytes) -> bytes:
    """A minimal index file (format 2) with one gitlink per name, laid out the way Git writes one."""
    out = bytearray(b"DIRC" + struct.pack(">II", 2, len(names)))
    for name in names:
        entry = struct.pack(">10I", 0, 0, 0, 0, 0, 0, 0o160000, 0, 0, 0) + bytes(20)
        entry += struct.pack(">H", min(len(name), 0xFFF)) + name
        out += entry + b"\0" * (8 - len(entry) % 8)
    return bytes(out + bytes(20))


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    """A real repository with one tracked file, in a folder of its own under ``tmp_path``."""
    return new_repo(tmp_path / "repo")


@pytest.fixture
def made(tmp_path: Path) -> Path:
    """A folder that merely looks like a repository: a ``.git`` folder with an ordinary configuration."""
    folder = tmp_path / "proj"
    (folder / ".git").mkdir(parents=True)
    configure(folder / ".git", "[core]\n\trepositoryformatversion = 0\n")
    (folder / "a.txt").write_text("alpha\n", encoding="utf-8")
    return folder


# --------------------------------------------------------------------------- G1: hooks nobody configured


@needs_git
def test_a_default_hook_is_asked_about_although_no_setting_names_it(repo):
    marker = repo.parent / "hook-ran.txt"
    plant_hook(repo / ".git" / "hooks", marker)
    stale_index(repo / "a.txt")
    ctx = ToolContext(project=repo)
    found = git_runs_own_programs(repo)
    assert found is not None and _STATUS_HOOK in found
    assert is_readonly_command("git status --short", repo, repo), "the command text is still a read-only one"
    for command in ("git status --short", "git diff", "cat a.txt | git status"):
        decision = decide("code", SHELL, {"command": command}, ctx, remembered=set())
        assert decision.verdict == "ask", command
        assert _STATUS_HOOK in decision.reason, command
    assert not marker.exists(), "taking the decision must not run anything of the repository's"
    assert verdict("code", {"command": "cat a.txt"}, ctx) == "allow", "only Git runs what lies in .git"
    assert verdict("full", {"command": "git status --short"}, ctx) == "allow", "full access still allows it"
    assert verdict("code", {"command": "git status --short"}, ctx, remembered={"exec:git"}) == "allow", (
        "the approval of this session still covers it"
    )


@needs_git
@windows_only
def test_the_default_hook_really_runs_once_the_command_is_run(repo):
    """The reason the question is asked: the approval is all that lies between the model and this script."""
    marker = repo.parent / "hook-ran.txt"
    plant_hook(repo / ".git" / "hooks", marker)
    stale_index(repo / "a.txt")
    ctx = ToolContext(project=repo)
    assert verdict("code", {"command": "git status --short"}, ctx) == "ask"
    result = asyncio.run(ToolRegistry.default().run("shell", {"command": "git status --short", "timeout": 60}, ctx))
    assert not result.is_error, result.output
    assert marker.exists(), "Git did not run the default hook, so this scenario proves nothing"


@needs_git
def test_a_configured_folder_of_hooks_replaces_the_default_one(repo):
    """``core.hooksPath`` moves the hooks Git runs; what lies in ``.git/hooks`` is then not run, and not asked about."""
    default_marker, own_marker = repo.parent / "default.txt", repo.parent / "own.txt"
    plant_hook(repo / ".git" / "hooks", default_marker)
    (repo / ".husky").mkdir()
    git(repo, "config", "core.hooksPath", ".husky")
    stale_index(repo / "a.txt")
    ctx = ToolContext(project=repo)
    assert git_runs_own_programs(repo) is None
    assert verdict("code", {"command": "git status --short"}, ctx) == "allow"
    git(repo, "status", "--short")
    assert not default_marker.exists(), "Git ran the hook of the folder it was told to leave"
    plant_hook(repo / ".husky", own_marker)
    stale_index(repo / "a.txt")
    assert git_runs_own_programs(repo) == "core.hookspath"
    assert verdict("code", {"command": "git status --short"}, ctx) == "ask"
    git(repo, "status", "--short")
    assert own_marker.exists(), "Git did not run the hook of the configured folder"


@needs_git
@pytest.mark.parametrize("value", ["false", "0"])
def test_a_folder_of_hooks_named_like_a_boolean_is_still_a_folder(repo, value):
    marker = repo.parent / "boolean-folder.txt"
    plant_hook(repo / value, marker)
    git(repo, "config", "core.hooksPath", value)
    stale_index(repo / "a.txt")
    assert git_runs_own_programs(repo) == "core.hookspath"
    assert verdict("code", {"command": "git status --short"}, ToolContext(project=repo)) == "ask"
    assert not marker.exists()
    git(repo, "status", "--short")
    assert marker.exists(), f"Git did not take hooks from a folder named {value!r}"


@pytest.mark.parametrize("value", ["~/hooks", "%(prefix)/hooks"])
def test_a_folder_of_hooks_that_git_expands_itself_is_asked_about(made, value):
    configure(made / ".git", f"[core]\n\thooksPath = {value}\n")
    assert git_runs_own_programs(made) == "core.hookspath"
    assert verdict("code", {"command": "git status"}, ToolContext(project=made)) == "ask"


@needs_git
def test_a_hook_of_the_shared_folder_of_a_linked_working_tree_is_asked_about(repo, tmp_path):
    tree = tmp_path / "linked"
    git(repo, "worktree", "add", "-q", str(tree))
    marker = repo.parent / "shared-hook.txt"
    plant_hook(repo / ".git" / "hooks", marker)
    stale_index(tree / "a.txt")
    assert (tree / ".git").is_file(), "a linked working tree points at its Git folder with a file"
    found = git_runs_own_programs(tree)
    assert found is not None and _STATUS_HOOK in found
    assert verdict("code", {"command": "git status --short"}, ToolContext(project=tree)) == "ask"
    assert not marker.exists()
    git(tree, "status", "--short")
    assert marker.exists(), "Git did not run the shared folder's hook for the linked tree"


@needs_git
def test_a_hook_of_a_submodule_is_asked_about_in_the_superproject(repo, tmp_path):
    checkout = add_submodule(repo, tmp_path)
    marker = repo.parent / "submodule-hook.txt"
    plant_hook(repo / ".git" / "modules" / "sub" / "hooks", marker)
    stale_index(checkout / "s.txt")
    found = git_runs_own_programs(repo)
    assert found is not None and _STATUS_HOOK in found
    assert verdict("code", {"command": "git status --short"}, ToolContext(project=repo)) == "ask"
    assert not marker.exists()
    git(repo, "status", "--short")
    assert marker.exists(), "the superproject's status did not refresh the submodule's index"


@needs_git
def test_a_submodule_without_its_worktree_setting_takes_its_hooks_from_its_checkout(repo, tmp_path):
    """Without ``core.worktree`` a submodule's relative ``core.hooksPath`` belongs to where it is checked out, which
    only the superproject's index says."""
    checkout = add_submodule(repo, tmp_path)
    config = str(repo / ".git" / "modules" / "sub" / "config")
    git(repo, "config", "-f", config, "--unset", "core.worktree")
    git(repo, "config", "-f", config, "core.hooksPath", "myhooks")
    marker = repo.parent / "checkout-hook.txt"
    plant_hook(checkout / "myhooks", marker)
    stale_index(checkout / "s.txt")
    assert git_runs_own_programs(repo) == "core.hookspath"
    assert verdict("code", {"command": "git status --short"}, ToolContext(project=repo)) == "ask"
    assert not marker.exists()
    git(repo, "status", "--short")
    assert marker.exists(), "Git did not take the submodule's hooks from its checkout"


@needs_git
def test_a_hook_of_a_repository_added_inside_the_superproject_is_asked_about(repo):
    """``git add`` of a folder that is a repository records a gitlink and leaves its ``.git`` folder where it is."""
    inner = new_repo(repo / "vendor" / "inner", "s.txt")
    git(repo, "add", "vendor/inner")
    git(repo, "commit", "-qm", "an embedded repository")
    marker = repo.parent / "embedded-hook.txt"
    plant_hook(inner / ".git" / "hooks", marker)
    stale_index(inner / "s.txt")
    found = git_runs_own_programs(repo)
    assert found is not None and _STATUS_HOOK in found
    ctx = ToolContext(project=repo)
    assert verdict("code", {"command": "git status --short"}, ctx) == "ask"
    assert verdict("code", {"command": "git diff"}, ctx) == "ask"
    assert not marker.exists()
    git(repo, "status", "--short")
    assert marker.exists(), "the superproject's status did not refresh the embedded repository's index"


@needs_git
def test_submodules_that_are_not_checked_out_keep_a_read_only_command_allowed(repo):
    add_gitlinks(repo, [f"vendor/lib{index}" for index in range(10)])
    assert git_runs_own_programs(repo) is None
    assert verdict("code", {"command": "git status --short"}, ToolContext(project=repo)) == "allow"


@needs_git
def test_an_index_with_more_submodules_than_are_looked_for_is_asked_about(repo):
    add_gitlinks(repo, [f"vendor/lib{index}" for index in range(permissions._GIT_LINKS + 1)])
    found = git_runs_own_programs(repo)
    assert found is not None and "Git folders" in found
    assert verdict("code", {"command": "git status --short"}, ToolContext(project=repo)) == "ask"


def test_the_hook_of_a_submodule_folder_is_found_without_a_real_repository(made):
    hook = plant_hook(made / ".git" / "modules" / "sub" / "hooks", made.parent / "m.txt")
    assert git_runs_own_programs(made) == str(hook)
    assert verdict("code", {"command": "git status"}, ToolContext(project=made)) == "ask"


def test_a_submodule_reads_its_own_folder_of_hooks_against_its_own_tree(made):
    """A submodule's Git folder lies in the superproject's, and its ``core.hooksPath`` belongs to its own tree."""
    module = made / ".git" / "modules" / "sub"
    configure(module, "[core]\n\tworktree = ../../../sub\n\thooksPath = myhooks\n")
    ctx = ToolContext(project=made)
    assert git_runs_own_programs(made) is None, "the folder it names holds no such hook yet"
    plant_hook(made / "sub" / "myhooks", made.parent / "m.txt")
    assert git_runs_own_programs(made) == "core.hookspath"
    assert verdict("code", {"command": "git status"}, ctx) == "ask"


def test_a_hook_named_with_the_windows_extension_counts(made):
    (made / ".git" / "hooks").mkdir()
    (made / ".git" / "hooks" / (_STATUS_HOOK + ".exe")).write_bytes(b"MZ")
    assert git_runs_own_programs(made) is not None, "Git looks for <hook>.exe as well (traced)"
    assert verdict("code", {"command": "git status"}, ToolContext(project=made)) == "ask"


def test_an_ordinary_repository_is_still_allowed(made):
    """What ``git init`` leaves behind (sample hooks only) and the folders projects use must not start asking."""
    hooks = made / ".git" / "hooks"
    hooks.mkdir()
    for name in ("pre-commit.sample", "fsmonitor-watchman.sample", _STATUS_HOOK + ".sample"):
        (hooks / name).write_text("#!/bin/sh\n", encoding="utf-8")
    plant_hook(made / ".husky", made.parent / "m.txt", name="pre-commit")
    configure(made / ".git", "[core]\n\thooksPath = .husky\n\tignorecase = true\n")
    ctx = ToolContext(project=made)
    assert git_runs_own_programs(made) is None
    for command in ("git status --short", "git diff", "git log --oneline -5", "cat a.txt | git status"):
        assert verdict("code", {"command": command}, ctx) == "allow", command


@needs_git
def test_a_real_repository_without_hooks_or_programs_is_still_allowed(repo, tmp_path):
    add_submodule(repo, tmp_path)
    ctx = ToolContext(project=repo)
    assert git_runs_own_programs(repo) is None
    for command in ("git status --short", "git diff", "git log --oneline -5"):
        assert verdict("code", {"command": command}, ctx) == "allow", command


def test_a_repository_with_more_git_folders_than_are_examined_is_asked_about(made):
    modules = made / ".git" / "modules"
    for index in range(permissions._GIT_FOLDERS + 1):
        (modules / f"sub{index}").mkdir(parents=True)
    found = git_runs_own_programs(made)
    assert found is not None and "Git folders" in found
    assert verdict("code", {"command": "git status"}, ToolContext(project=made)) == "ask"


def test_a_folder_in_no_repository_asks_about_nothing(tmp_path):
    plain = tmp_path / "plain"
    plain.mkdir()
    assert git_runs_own_programs(plain) is None
    assert verdict("code", {"command": "git status"}, ToolContext(project=plain)) == "allow"


# --------------------------------------------------------------------------- G1: the configuration as Git reads it

CONFIGS = {
    "a space between the section and its subsection": '[diff "review"]\n\ttextconv = sh helper.sh\n',
    "a tab between them": '[diff\t"review"]\n\ttextconv = sh helper.sh\n',
    "tabs and spaces between them": '[diff \t  "review"]\n\ttextconv = sh helper.sh\n',
    "a carriage return between them": '[diff\r"review"]\n\ttextconv = sh helper.sh\n',
    "a value that continues over the next header": '[diff "x"]\n\tfoo = bar \\\n[core]\n\ttextconv = sh helper.sh\n',
    "a quoted hash before the backslash": '[diff "x"]\n\tfoo = "a#b" \\\n[core]\n\ttextconv = sh helper.sh\n',
    "a comment that ends in a backslash": '[core]\n\t# a comment \\\n[diff "x"]\n\ttextconv = sh helper.sh\n',
    "a semicolon comment that ends in a backslash": '[core]\n\t; a comment \\\n[diff "x"]\n\ttextconv = sh helper.sh\n',
    "a value whose comment ends in a backslash": '[core]\n\tfoo = bar # c \\\n[diff "x"]\n\ttextconv = sh helper.sh\n',
    "an escaped backslash, which ends the value": '[diff "x"]\n\tfoo = bar\\\\\n[core]\n\ttextconv = sh helper.sh\n',
    "a value continued onto the next line": "[core]\n\tfsmonitor = part1 \\\n\tpart2\n",
    "U+0085 in the subsection": '[diff "a\u0085b"]\n\ttextconv = sh helper.sh\n',
    "U+2028 in the subsection": '[diff "a\u2028b"]\n\ttextconv = sh helper.sh\n',
    "a header and a key on one line": '[core][diff "x"]textconv = sh helper.sh\n',
    "a bracket inside the subsection": '[diff "a]b"]\n\ttextconv = sh helper.sh\n',
    "capitals": '[DIFF "X"]\n\tTEXTCONV = sh helper.sh\n',
    "a byte order mark": '\ufeff[diff "x"]\n\ttextconv = sh helper.sh\n',
    "Windows line ends": '[diff "x"]\r\n\ttextconv = sh helper.sh\r\n',
    "a hash inside the value": '[diff "x"]\n\ttextconv = "sh #helper.sh"\n',
    "lone carriage returns, which Git reads as one value": '[core]\r\tfoo = 1\r[diff "x"]\r\ttextconv = sh helper.sh\n',
    "nothing of the repository's": "[core]\n\trepositoryformatversion = 0\n\tautocrlf = input\n",
    "a setting turned off": "[core]\n\tfsmonitor = false # off\n",
    "a setting undone": "[diff]\n\texternal =\n",
    "a folder of hooks, which is not a program": "[core]\n\thooksPath = .husky\n",
}
"""Configuration texts, each with its spelling. What Git reads from them is asked of Git itself below."""


def program_kinds(settings) -> set[tuple[str, str]]:
    """The section and last name of every setting that names a program: what the policy actually keys on, so that
    ``diff.a]b.textconv`` and ``diff.a.textconv`` (the same program, a subsection name read differently) agree."""
    return {
        (key.split(".", 1)[0], key.rpartition(".")[2])
        for key, value in settings
        if permissions._names_a_program(key.lower(), value)
    }


def git_settings_of(path: Path) -> list[tuple[str, str]] | None:
    """Every setting Git itself reads from one file, or None when Git rejects the file (then it runs nothing)."""
    done = subprocess.run(["git", "config", "-f", str(path), "-l", "-z"], capture_output=True, timeout=60)
    if done.returncode:
        return None
    out = []
    for record in done.stdout.decode("utf-8", "replace").split("\0"):
        if record:
            key, _, value = record.partition("\n")
            out.append((key, value))
    return out


@needs_git
@pytest.mark.parametrize("spelling", sorted(CONFIGS))
def test_the_check_finds_the_programs_git_reads_and_no_others(tmp_path, spelling):
    """Asked of the real Git rather than of a second opinion: for each spelling, the programs Git reads out of the
    file and the ones the check reads out of the same file must be the same set -- a miss is a program that runs
    unasked, and an extra one is an approval card for a repository that configures nothing."""
    path = tmp_path / "config"
    path.write_bytes(CONFIGS[spelling].encode("utf-8"))
    listed = git_settings_of(path)
    if listed is None:
        pytest.skip("Git rejects this file, so it reads nothing out of it")
    assert program_kinds(permissions._git_settings(permissions._git_text(path))) == program_kinds(listed), spelling


@pytest.mark.parametrize(
    "spelling",
    [
        "a tab between them",
        "tabs and spaces between them",
        "a carriage return between them",
        "a value that continues over the next header",
        "a quoted hash before the backslash",
        "a comment that ends in a backslash",
        "U+0085 in the subsection",
        "U+2028 in the subsection",
    ],
)
def test_a_textconv_in_an_unusual_spelling_is_asked_about(made, spelling):
    configure(made / ".git", CONFIGS[spelling])
    ctx = ToolContext(project=made)
    found = git_runs_own_programs(made)
    assert found is not None and found.endswith(".textconv"), spelling
    decision = decide("code", SHELL, {"command": "git diff"}, ctx, remembered=set())
    assert decision.verdict == "ask" and found in decision.reason
    assert verdict("code", {"command": "git status --short"}, ctx) == "ask", "the same configuration, any Git command"


@pytest.mark.parametrize(
    "spelling",
    [
        "an escaped backslash, which ends the value",
        "nothing of the repository's",
        "a setting turned off",
        "a setting undone",
        "lone carriage returns, which Git reads as one value",
    ],
)
def test_a_configuration_git_runs_nothing_from_keeps_a_read_only_command_allowed(made, spelling):
    configure(made / ".git", CONFIGS[spelling])
    assert git_runs_own_programs(made) is None, spelling
    assert verdict("code", {"command": "git status --short"}, ToolContext(project=made)) == "allow", spelling


def textconv_repo(repo: Path, marker: Path) -> None:
    (repo / "helper.sh").write_text(
        f'#!/bin/sh\necho ran >> "{marker.as_posix()}"\ncat "$1"\n', encoding="utf-8", newline="\n"
    )
    (repo / ".gitattributes").write_text("*.txt diff=review\n", encoding="utf-8", newline="\n")
    git(repo, "add", ".gitattributes")
    configure(repo / ".git", '[diff\t"review"]\n\ttextconv = sh helper.sh\n')
    (repo / "a.txt").write_text("beta\n", encoding="utf-8")


@needs_git
def test_a_tab_separated_textconv_is_one_git_reads_and_runs(repo):
    """The whole way round with the real Git: the setting is read, the script runs, and the check asks about it."""
    marker = repo.parent / "textconv-ran.txt"
    textconv_repo(repo, marker)
    assert git(repo, "config", "--get", "diff.review.textconv").strip() == "sh helper.sh", "Git reads the tab"
    ctx = ToolContext(project=repo)
    assert git_runs_own_programs(repo) == "diff.review.textconv"
    assert verdict("code", {"command": "git diff"}, ctx) == "ask"
    assert not marker.exists(), "taking the decision must not run the driver"
    git(repo, "diff")
    assert marker.exists(), "Git did not run the textconv, so this scenario proves nothing"


@needs_git
@windows_only
def test_a_tab_separated_textconv_runs_through_the_shell_tool_too(repo):
    marker = repo.parent / "textconv-ran.txt"
    textconv_repo(repo, marker)
    ctx = ToolContext(project=repo)
    assert verdict("code", {"command": "git diff"}, ctx) == "ask"
    result = asyncio.run(ToolRegistry.default().run("shell", {"command": "git diff", "timeout": 60}, ctx))
    assert not result.is_error, result.output
    assert marker.exists(), "the neutralised environment does not stop a textconv; the approval does"


def test_the_settings_the_shell_neutralises_are_still_neutralised():
    """The other half of the answer, unchanged: what has a harmless value is set for every command."""
    _, env = shell._launch("git status")
    pairs = {
        env[f"GIT_CONFIG_KEY_{index}"]: env[f"GIT_CONFIG_VALUE_{index}"]
        for index in range(int(env["GIT_CONFIG_COUNT"]))
    }
    assert pairs["core.fsmonitor"] == "false"
    assert "core.hooksPath" not in pairs, "it would move the hooks Git runs into the project"


def test_a_configured_fsmonitor_is_still_asked_about(made):
    configure(made / ".git", '[core]\n\tfsmonitor = "watch.exe"\n')
    assert git_runs_own_programs(made) == "core.fsmonitor"
    assert verdict("code", {"command": "git status --short"}, ToolContext(project=made)) == "ask"


# --------------------------------------------------------------------------- the submodules an index records


def links_git_lists(repo: Path) -> list[str]:
    out = git(repo, "-c", "core.quotepath=off", "ls-files", "--stage", "-z")
    return sorted(record.split("\t", 1)[1] for record in out.split("\0") if record.startswith("160000 "))


LONG_NAME = "/".join(["d" * 200] * 25) + "/end"  # past 0xFFF bytes, where the flags stop holding the length


def index_version_2(base: Path) -> tuple[Path, list]:
    repo = new_repo(base / "r")
    add_gitlinks(repo, ["vendor/a", "vendor/b", "z/last", "vendor/\u00fc", LONG_NAME])
    return repo, []


def index_version_3(base: Path) -> tuple[Path, list]:
    repo = new_repo(base / "r")
    add_gitlinks(repo, ["vendor/a", "m/n/o", "vendor/z"])
    # An extended flag, which makes the index version 3 and gives these entries two more bytes before their names.
    git(repo, "update-index", "--skip-worktree", "a.txt", "m/n/o")
    return repo, []


def index_version_4(base: Path) -> tuple[Path, list]:
    repo = new_repo(base / "r")
    add_gitlinks(repo, ["vendor/a", "vendor/ab", "vendor/abc", "x", LONG_NAME])
    git(repo, "update-index", "--index-version", "4")
    return repo, []


def index_split(base: Path) -> tuple[Path, list]:
    repo = new_repo(base / "r")
    add_gitlinks(repo, ["vendor/in-shared"])
    git(repo, "update-index", "--split-index")
    add_gitlinks(repo, ["vendor/in-main"])
    return repo, []


def index_sha256(base: Path) -> tuple[Path, list]:
    repo = new_repo(base / "r", "a.txt", "--object-format=sha256")
    add_gitlinks(repo, ["vendor/a", "q"])
    return repo, [("extensions.objectformat", "sha256")]


def index_in_blocks(base: Path) -> tuple[Path, list]:
    """Version 4 written by threads, whose blocks start their names afresh (an ``IEOT`` extension)."""
    repo = new_repo(base / "r")
    blob = git(repo, "hash-object", "-w", "a.txt").strip()
    paths = [f"p/{index:05}/file" for index in range(12000)]
    git(repo, "update-index", "--add", "--index-info", stdin="".join(f"100644 {blob}\t{path}\n" for path in paths))
    add_gitlinks(repo, ["p/03000/sub", "p/09999/sub", "zz/sub"])
    git(repo, "-c", "index.threads=4", "-c", "index.recordOffsetTable=true", "update-index", "--index-version", "4")
    return repo, []


INDEXES = {
    "version 2": index_version_2,
    "version 3": index_version_3,
    "version 4": index_version_4,
    "a split index": index_split,
    "SHA-256": index_sha256,
    "version 4 in blocks": index_in_blocks,
}


@needs_git
@pytest.mark.parametrize("layout", sorted(INDEXES))
def test_the_submodules_the_index_records_are_the_ones_git_lists(tmp_path, layout):
    repo, settings = INDEXES[layout](tmp_path)
    wanted = links_git_lists(repo)
    assert wanted, "the layout records no submodule, so it proves nothing"
    assert sorted(permissions._gitlinks(repo / ".git", settings)) == wanted
    if layout == "a split index":
        main = permissions._index_gitlinks((repo / ".git" / "index").read_bytes(), 20)
        assert b"vendor/in-shared" not in main, "the main file of a split index holds only what changed since"
    if layout == "version 4 in blocks":
        assert b"IEOT" in (repo / ".git" / "index").read_bytes(), "Git wrote the index in one block"


def test_an_index_that_cannot_be_read_is_asked_about(made):
    whole = index_file(b"vendor/a", b"vendor/b")
    (made / ".git" / "index").write_bytes(whole[: len(whole) // 2])  # the gitlink's mode is there, its name is not
    assert git_runs_own_programs(made) == permissions._GIT_UNREADABLE
    assert verdict("code", {"command": "git status"}, ToolContext(project=made)) == "ask"


def test_a_submodule_path_that_is_not_utf8_is_asked_about(made):
    (made / ".git" / "index").write_bytes(index_file(b"vendor/\xe9"))
    assert git_runs_own_programs(made) == permissions._GIT_UNREADABLE


def test_a_checked_out_submodule_found_through_a_hand_made_index_is_examined(made):
    (made / ".git" / "index").write_bytes(index_file(b"vendor/x"))
    inner = made / "vendor" / "x"
    (inner / ".git").mkdir(parents=True)
    assert git_runs_own_programs(made) is None
    configure(inner / ".git", "[diff \"x\"]\n\ttextconv = sh helper.sh\n")
    assert git_runs_own_programs(made) == "diff.x.textconv"


# --------------------------------------------------------------------------- G2: no entry to a network path


class Watcher:
    """Records and refuses every file-system entry a path with two leading separators could use.

    Recording alone would not do: a check that swallows the error would look as if it had asked for nothing, and a
    test that counted on there being no such host would pass on a machine where there is one. Both the ``os``
    functions and the ``pathlib`` methods are taken, because they do not go through each other.
    """

    ENTRIES: tuple[tuple[Any, str], ...] = (
        (os, "stat"),
        (os, "lstat"),
        (os, "scandir"),
        (os, "listdir"),
        (builtins, "open"),
        (Path, "read_text"),
        (Path, "read_bytes"),
        (Path, "exists"),
        (Path, "is_file"),
        (Path, "is_dir"),
        (Path, "stat"),
        (Path, "lstat"),
        (Path, "open"),
        (Path, "resolve"),
        (Path, "iterdir"),
    )

    def __init__(self) -> None:
        self.reached: list[str] = []

    def install(self, monkeypatch: pytest.MonkeyPatch) -> None:
        for owner, name in self.ENTRIES:
            monkeypatch.setattr(owner, name, self._guard(getattr(owner, name), f"{owner.__name__}.{name}"))

    def _guard(self, original: Callable[..., Any], label: str) -> Callable[..., Any]:
        def guarded(path: Any = None, *args: Any, **kwargs: Any) -> Any:
            if str(path).replace("/", "\\").startswith("\\\\"):
                self.reached.append(f"{label}({path})")
                raise OSError(53, "refused by the test: nothing may reach a network or device path")
            return original(path, *args, **kwargs)

        return guarded


def git_file(project: Path, told: str) -> None:
    (project / ".git").write_text(f"gitdir: {told}\n", encoding="utf-8", newline="\n")


def setting(text: str) -> Callable[[Path], None]:
    return lambda project: configure(project / ".git", f"[core]\n\t{text}\n")


def index_naming(name: bytes, checkout_gitdir: str | None = None) -> Callable[[Path], None]:
    def plant(project: Path) -> None:
        (project / ".git").mkdir(parents=True, exist_ok=True)
        (project / ".git" / "index").write_bytes(index_file(name))
        if checkout_gitdir is not None:
            (project / name.decode()).mkdir(parents=True)
            git_file(project / name.decode(), checkout_gitdir)

    return plant


REMOTE: dict[str, Callable[[Path], None]] = {
    "a gitdir on a share": lambda project: git_file(project, "\\\\review.invalid\\share\\repo"),
    "a gitdir on a share with forward slashes": lambda project: git_file(project, "//review.invalid/share/repo"),
    "a gitdir with the extended-length prefix": lambda project: git_file(
        project, "\\\\?\\UNC\\review.invalid\\share\\repo"
    ),
    "a commondir on a share": lambda project: configure(
        project / ".git", "\\\\review.invalid\\share\\common\n", name="commondir"
    ),
    "a folder of hooks on a share": setting("hooksPath = \\\\review.invalid\\share\\hooks"),
    "a folder of hooks on a device": setting("hooksPath = \\\\.\\pipe\\hooks"),
    "a working tree on a share": setting("worktree = \\\\review.invalid\\share\\tree"),
    "a submodule's gitdir on a share": index_naming(b"vendor/x", "//review.invalid/share/module"),
    "a submodule path on a share": index_naming(b"//review.invalid/share/x"),
}
"""Each one plants, in a local file, a piece of Git metadata that names a path off this machine."""


@pytest.mark.parametrize("named", sorted(REMOTE))
def test_a_network_path_in_the_git_metadata_is_never_entered_and_is_asked_about(tmp_path, monkeypatch, named):
    project = tmp_path / "proj"
    project.mkdir()
    (project / "a.txt").write_text("alpha\n", encoding="utf-8")
    REMOTE[named](project)
    watcher = Watcher()
    watcher.install(monkeypatch)
    found = git_runs_own_programs(project)
    decision = decide("code", SHELL, {"command": "git status --short"}, ToolContext(project=project), remembered=set())
    assert watcher.reached == [], named
    assert found == permissions._GIT_REMOTE, named
    assert decision.verdict == "ask" and "network" in decision.reason, named


def test_a_working_tree_on_a_share_is_asked_about_without_being_entered(monkeypatch):
    """Not even the ``.git`` of a working tree on a share is looked at before the user has approved anything."""
    watcher = Watcher()
    watcher.install(monkeypatch)
    assert git_runs_own_programs(Path("\\\\review.invalid\\share\\proj")) == permissions._GIT_REMOTE
    assert git_runs_own_programs(Path("//review.invalid/share/proj/sub")) == permissions._GIT_REMOTE
    assert watcher.reached == []


def test_an_ordinary_repository_is_untouched_by_the_same_watch(tmp_path, monkeypatch):
    """The watch itself must not be what makes the answer: a local repository still answers "allow"."""
    project = tmp_path / "proj"
    (project / ".git" / "hooks").mkdir(parents=True)
    configure(project / ".git", "[core]\n\trepositoryformatversion = 0\n")
    (project / "a.txt").write_text("alpha\n", encoding="utf-8")
    watcher = Watcher()
    watcher.install(monkeypatch)
    assert git_runs_own_programs(project) is None
    assert verdict("code", {"command": "git status --short"}, ToolContext(project=project)) == "allow"
    assert watcher.reached == []


def test_a_configuration_that_cannot_be_read_is_asked_about_not_passed(made):
    """A failure is not a licence: an unreadable configuration is a question, not an empty one."""
    config = made / ".git" / "config"
    config.unlink()
    config.mkdir()  # reading a folder fails with something other than "no such file"
    assert git_runs_own_programs(made) == permissions._GIT_UNREADABLE
    assert verdict("code", {"command": "git status --short"}, ToolContext(project=made)) == "ask"


def test_a_commondir_that_cannot_be_read_is_asked_about(made):
    (made / ".git" / "commondir").mkdir()
    assert git_runs_own_programs(made) == permissions._GIT_UNREADABLE


def test_a_hook_that_cannot_be_looked_at_is_asked_about(made, monkeypatch):
    original = os.stat

    def refusing(path, *args, **kwargs):
        if str(path).endswith(_STATUS_HOOK):
            raise PermissionError(13, "access denied by the test")
        return original(path, *args, **kwargs)

    monkeypatch.setattr(os, "stat", refusing)
    assert git_runs_own_programs(made) == permissions._GIT_UNREADABLE
    assert verdict("code", {"command": "git status --short"}, ToolContext(project=made)) == "ask"
