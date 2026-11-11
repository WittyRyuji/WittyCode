"""F4: a read-only Git command still runs whatever the repository's own configuration tells Git to run.

``git status`` was auto-allowed at the ``code`` level, and in a repository whose ``.git/config`` sets ``core.fsmonitor``
to a command, Git ran that command. Two answers: ``shell`` starts every Git with the settings it can neutralise set to
a harmless value, and ``permissions`` asks about a repository that configures one of the settings that has no harmless
value. Writing into ``.git`` is a question of its own, since that is where those settings live.
"""

from __future__ import annotations

import asyncio
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from desktop.core.harness import fs_tools, shell
from desktop.core.harness.permissions import decide, git_runs_own_programs, is_readonly_command, remember_key
from desktop.core.harness.shell import SHELL
from desktop.core.harness.tools import ToolContext, ToolRegistry

windows_only = pytest.mark.skipif(sys.platform != "win32", reason="runs Windows PowerShell")
needs_git = pytest.mark.skipif(shutil.which("git") is None, reason="no Git on PATH")


@pytest.fixture
def project(tmp_path: Path) -> Path:
    folder = tmp_path / "proj"
    (folder / ".git").mkdir(parents=True)
    (folder / ".git" / "config").write_text("[core]\n\trepositoryformatversion = 0\n", encoding="utf-8")
    (folder / "a.txt").write_text("alpha\n", encoding="utf-8")
    return folder


@pytest.fixture
def ctx(project: Path) -> ToolContext:
    return ToolContext(project=project)


def configure(project: Path, text: str) -> None:
    config = project / ".git" / "config"
    config.write_text(config.read_text(encoding="utf-8") + text, encoding="utf-8")


def verdict(level: str, args: dict, ctx: ToolContext, remembered=frozenset()) -> str:
    return decide(level, SHELL, args, ctx, remembered=set(remembered)).verdict


# --------------------------------------------------------------------------- reading the repository's configuration


def test_a_plain_repository_configures_no_program(project, ctx):
    assert git_runs_own_programs(project) is None
    assert verdict("code", {"command": "git status --short"}, ctx) == "allow"
    assert verdict("code", {"command": "git log --oneline -5"}, ctx) == "allow"
    assert verdict("code", {"command": "cat a.txt | git status"}, ctx) == "allow"


RUNS_A_PROGRAM = [
    ('[core]\n\tfsmonitor = "watch.exe"\n', "core.fsmonitor"),
    ("[core] fsmonitor = watch.exe\n", "core.fsmonitor"),
    ("[CORE]\n\tFsMonitor = watch.exe\n", "core.fsmonitor"),
    ('[diff]\n\texternal = "my-diff"\n', "diff.external"),
    ('[diff "evil"]\n\ttextconv = "cat"\n', "diff.evil.textconv"),
    ('[filter "evil"]\n\tclean = "cmd /c x"\n', "filter.evil.clean"),
    ('[filter "evil"]\n\tprocess = "x"\n', "filter.evil.process"),
    ("[core]\n\tsshCommand = ssh-evil\n", "core.sshcommand"),
    ("[core]\n\tpager = less-evil\n", "core.pager"),
    ("[core]\n\talternateRefsCommand = x\n", "core.alternaterefscommand"),
    ('[credential]\n\thelper = "!evil"\n', "credential.helper"),
    ('[credential "https://host/"]\n\thelper = "!evil"\n', "credential.https://host/.helper"),
    ("[pager]\n\tlog = evil\n", "pager.log"),
    ('[merge "x"]\n\tdriver = evil\n', "merge.x.driver"),
    ('[mergetool "x"]\n\tcmd = evil\n', "mergetool.x.cmd"),
    ('[difftool "x"]\n\tcmd = evil\n', "difftool.x.cmd"),
    ('[trailer "sign"]\n\tcommand = evil\n', "trailer.sign.command"),
    ("[init]\n\ttemplateDir = ./tpl\n", "init.templatedir"),
    ("[gpg]\n\tprogram = evil\n", "gpg.program"),
    ('[remote "origin"]\n\tuploadpack = evil\n', "remote.origin.uploadpack"),
    ("[uploadpack]\n\tpackObjectsHook = evil\n", "uploadpack.packobjectshook"),
    ('[protocol "ext"]\n\tcommand = evil\n', "protocol.ext.command"),
    ("[include]\n\tpath = extra.cfg\n", "include.path"),
    ('[includeIf "gitdir:x"]\n\tpath = extra.cfg\n', "includeif.gitdir:x.path"),
    ("[core]\n\taskPass = evil\n", "core.askpass"),
    ("[core]\n\teditor = evil\n", "core.editor"),
    ("[core]\n\tgitProxy = evil\n", "core.gitproxy"),
    ('[core]\n\tfsmonitor = "echo \\\n', "core.fsmonitor"),
    ("[core]\n\tfsmonitor = watch.exe ; a comment\n", "core.fsmonitor"),
]


@pytest.mark.parametrize("text,key", RUNS_A_PROGRAM)
def test_a_setting_that_makes_git_run_a_program_is_found(project, ctx, text, key):
    configure(project, text)
    assert git_runs_own_programs(project) == key
    assert is_readonly_command("git status --short", project, project), "the command text is still read-only"
    decision = decide("code", SHELL, {"command": "git status --short"}, ctx, remembered=set())
    assert decision.verdict == "ask" and key in decision.reason
    assert verdict("code", {"command": "git status", "cwd": "."}, ctx) == "ask"
    assert verdict("code", {"command": "cat a.txt | git status"}, ctx) == "ask", "Git as the second command counts too"
    assert verdict("full", {"command": "git status --short"}, ctx) == "allow", "full access still allows it"
    assert verdict("code", {"command": "cat a.txt"}, ctx) == "allow", "only Git reads that configuration"


HARMLESS = [
    "[core]\n\tfsmonitor = true\n",
    "[core]\n\tfsmonitor = false\n",
    "[core]\n\tfsmonitor\n",
    "[core]\n\tfsmonitor =\n",
    "[core]\n\tfsmonitor = TRUE\n",
    "[core]\n\t# fsmonitor = evil\n",
    "[core]\n\t; fsmonitor = evil\n",
    "[core]\n\tautocrlf = input\n\tignorecase = true\n",
    '[branch "feature.x"]\n\tremote = origin\n',
    '[remote "origin"]\n\turl = https://example.invalid/x.git\n',
    "[include]\n\tpath =\n",
    "[core]\n\tfsmonitor = false # off\n",
    '[core]\n\tfsmonitor = "false"\n',
]


@pytest.mark.parametrize("text", HARMLESS)
def test_ordinary_settings_keep_a_read_only_git_command_allowed(project, ctx, text):
    configure(project, text)
    assert git_runs_own_programs(project) is None, text
    assert verdict("code", {"command": "git status --short"}, ctx) == "allow", text


def test_a_hooks_folder_is_asked_about_only_when_it_holds_a_hook_a_read_only_command_runs(project, ctx):
    configure(project, "[core]\n\thooksPath = .husky\n")
    (project / ".husky").mkdir()
    (project / ".husky" / "pre-commit").write_text("#!/bin/sh\n", encoding="utf-8")
    assert git_runs_own_programs(project) is None, "no allowed command runs pre-commit"
    assert verdict("code", {"command": "git status --short"}, ctx) == "allow"
    (project / ".husky" / "post-index-change").write_text("#!/bin/sh\n", encoding="utf-8")
    assert git_runs_own_programs(project) == "core.hookspath"
    assert verdict("code", {"command": "git status --short"}, ctx) == "ask"


def test_the_configuration_of_the_repository_above_the_folder_counts(project, ctx):
    configure(project, '[core]\n\tfsmonitor = "watch.exe"\n')
    deeper = project / "src"
    deeper.mkdir()
    assert git_runs_own_programs(deeper) == "core.fsmonitor"
    assert verdict("code", {"command": "git status", "cwd": "src"}, ctx) == "ask"


def test_a_working_tree_whose_git_is_a_file_is_read_through(tmp_path, project, ctx):
    """A linked working tree or a submodule: ``.git`` is a file pointing at the real Git folder."""
    real = tmp_path / "elsewhere.git"
    real.mkdir()
    (real / "config").write_text('[core]\n\tfsmonitor = "watch.exe"\n', encoding="utf-8")
    worktree = tmp_path / "proj2"
    worktree.mkdir()
    (worktree / ".git").write_text(f"gitdir: {real}\n", encoding="utf-8")
    assert git_runs_own_programs(worktree) == "core.fsmonitor"
    shared = tmp_path / "proj3"
    shared.mkdir()
    (shared / ".git").write_text("gitdir: ../linked.git\n", encoding="utf-8")
    linked = tmp_path / "linked.git"
    linked.mkdir()
    (linked / "config").write_text("[core]\n\tbare = false\n", encoding="utf-8")
    (linked / "commondir").write_text("../elsewhere.git\n", encoding="utf-8")
    assert git_runs_own_programs(shared) == "core.fsmonitor", "the shared configuration applies too"


def test_a_per_worktree_configuration_counts(project):
    (project / ".git" / "config.worktree").write_text('[diff]\n\texternal = "x"\n', encoding="utf-8")
    assert git_runs_own_programs(project) == "diff.external"


def test_a_configuration_that_cannot_be_read_is_asked_about(project, ctx):
    config = project / ".git" / "config"
    config.unlink()
    config.mkdir()  # reading a folder fails with an error other than "not found"
    assert git_runs_own_programs(project) is not None
    assert verdict("code", {"command": "git status --short"}, ctx) == "ask"


def test_a_folder_in_no_repository_asks_about_nothing(tmp_path):
    folder = tmp_path / "plain"
    folder.mkdir()
    assert git_runs_own_programs(folder) is None


# --------------------------------------------------------------------------- the environment Git is started in


def test_the_shell_starts_git_with_the_dangerous_settings_neutralised():
    _, env = shell._launch("git status")
    count = int(env["GIT_CONFIG_COUNT"])
    pairs = {env[f"GIT_CONFIG_KEY_{index}"]: env[f"GIT_CONFIG_VALUE_{index}"] for index in range(count)}
    assert pairs["core.fsmonitor"] == "false"
    assert pairs["core.pager"] == "cat"
    for key in ("core.askPass", "core.gitProxy", "core.alternateRefsCommand", "uploadpack.packObjectsHook"):
        assert pairs[key] == ""
    assert "diff.external" not in pairs, "emptying it makes every git diff fail"
    assert "core.hooksPath" not in pairs, "it would move the hooks Git runs into the project"
    assert "credential.helper" not in pairs, "a confirmed push still needs the user's credentials"


def test_settings_the_environment_already_carries_are_kept(monkeypatch):
    monkeypatch.setenv("GIT_CONFIG_COUNT", "1")
    monkeypatch.setenv("GIT_CONFIG_KEY_0", "user.name")
    monkeypatch.setenv("GIT_CONFIG_VALUE_0", "someone")
    _, env = shell._launch("git status")
    count = int(env["GIT_CONFIG_COUNT"])
    assert count == 1 + len(shell._GIT_NEUTRAL)
    assert env["GIT_CONFIG_KEY_0"] == "user.name" and env["GIT_CONFIG_VALUE_0"] == "someone"
    assert env["GIT_CONFIG_KEY_1"] == "core.fsmonitor" and env["GIT_CONFIG_VALUE_1"] == "false"


def test_a_broken_count_in_the_environment_does_not_break_the_hardening(monkeypatch):
    monkeypatch.setenv("GIT_CONFIG_COUNT", "not a number")
    _, env = shell._launch("git status")
    assert env["GIT_CONFIG_COUNT"] == str(len(shell._GIT_NEUTRAL))
    assert env["GIT_CONFIG_KEY_0"] == "core.fsmonitor"


# --------------------------------------------------------------------------- writing what Git runs


def test_writing_into_a_git_folder_is_asked_about(project, ctx):
    for raw in (".git/config", ".git/hooks/pre-commit", ".git/config.worktree", str(project / ".git" / "info" / "attributes")):
        args = {"file_path": raw, "content": "x"}
        decision = decide("code", fs_tools.CREATE_FILE, args, ctx, remembered=set())
        assert decision.verdict == "ask", raw
        assert ".git" in decision.reason
        assert decide("code", fs_tools.CREATE_FILE, args, ctx, remembered={"edit"}).verdict == "ask", raw
        key = remember_key(fs_tools.CREATE_FILE, args, ctx)
        assert key != "edit"
        assert decide("code", fs_tools.CREATE_FILE, args, ctx, remembered={key}).verdict == "allow", raw
    edit = {"file_path": ".git/config", "old_str": "a", "new_str": "b"}
    assert decide("code", fs_tools.EDIT_FILE, edit, ctx, remembered=set()).verdict == "ask"


@windows_only
def test_a_git_folder_written_in_other_letters_is_the_same_folder(tmp_path):
    """NTFS ignores case, so ``.GIT`` is where Git looks; the folder does not exist yet, so nothing corrects the case."""
    fresh = tmp_path / "fresh"
    fresh.mkdir()
    ctx = ToolContext(project=fresh)
    for raw in (".GIT/config", ".Git/hooks/post-index-change"):
        args = {"file_path": raw, "content": "x"}
        assert decide("code", fs_tools.CREATE_FILE, args, ctx, remembered={"edit"}).verdict == "ask", raw


def test_approving_one_git_folder_does_not_approve_another(project, ctx):
    hooks = {"file_path": ".git/hooks/pre-commit", "content": "x"}
    config = {"file_path": ".git/config", "content": "x"}
    allowed = {remember_key(fs_tools.CREATE_FILE, hooks, ctx)}
    assert decide("code", fs_tools.CREATE_FILE, hooks, ctx, remembered=allowed).verdict == "allow"
    assert decide("code", fs_tools.CREATE_FILE, config, ctx, remembered=allowed).verdict == "ask"


def test_ordinary_writes_in_the_project_are_still_allowed(project, ctx):
    for raw in ("a.txt", "src/new.py", "digit.gitignore", ".gitignore", "notgit/x.txt", "git/x.txt"):
        args = {"file_path": raw, "content": "x"}
        assert decide("code", fs_tools.CREATE_FILE, args, ctx, remembered=set()).verdict == "allow", raw
        assert remember_key(fs_tools.CREATE_FILE, args, ctx) == "edit", raw
    assert decide("code", fs_tools.READ_FILE, {"file_path": ".git/config"}, ctx, remembered=set()).verdict == "allow", (
        "reading is not what decides what Git runs"
    )


# --------------------------------------------------------------------------- with a real Git


@needs_git
@windows_only
def test_a_configured_fsmonitor_does_not_run_through_the_shell_tool(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    assert subprocess.run(["git", "init", "-q"], cwd=repo, capture_output=True).returncode == 0
    (repo / "a.txt").write_text("alpha\n", encoding="utf-8")
    marker = tmp_path / "fsmonitor-ran.txt"
    config = repo / ".git" / "config"
    config.write_text(config.read_text(encoding="utf-8") + f'\n[core]\n\tfsmonitor = "cmd /c echo ran > {marker.as_posix()}"\n', encoding="utf-8")
    ctx = ToolContext(project=repo)
    assert decide("code", SHELL, {"command": "git status --short"}, ctx, remembered=set()).verdict == "ask"
    result = asyncio.run(ToolRegistry.default().run("shell", {"command": "git status --short", "timeout": 60}, ctx))
    assert not result.is_error, result.output
    assert "a.txt" in result.output
    assert not marker.exists(), "Git ran the command the repository configured"


@needs_git
@windows_only
def test_an_ordinary_git_command_still_works_with_the_neutralised_settings(tmp_path):
    repo = tmp_path / "repo2"
    repo.mkdir()
    assert subprocess.run(["git", "init", "-q"], cwd=repo, capture_output=True).returncode == 0
    (repo / "a.txt").write_text("alpha\n", encoding="utf-8")
    subprocess.run(["git", "add", "-A"], cwd=repo, capture_output=True)
    subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@e.invalid", "commit", "-qm", "one"], cwd=repo, capture_output=True)
    (repo / "a.txt").write_text("beta\n", encoding="utf-8")
    ctx = ToolContext(project=repo)
    registry = ToolRegistry.default()
    for command, wanted in (("git status --short", "a.txt"), ("git log --oneline", "one"), ("git diff", "beta")):
        for level in ("code", "full"):  # the ``ask`` level asks about every command, configuration or not
            assert decide(level, SHELL, {"command": command}, ctx, remembered=set()).verdict == "allow", (command, level)
        result = asyncio.run(registry.run("shell", {"command": command, "timeout": 60}, ctx))
        assert not result.is_error, (command, result.output)
        assert wanted in result.output, (command, result.output)
