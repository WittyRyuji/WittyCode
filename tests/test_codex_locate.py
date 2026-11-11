"""Finding Codex: npm shims resolved to the native exe, the search order, the version probe."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from desktop.core.codex.locate import CodexBinary, codex_candidates, locate_codex, probe_version
from desktop.core.codex.rpc import JsonRpcClient, launch_command, shim_script
from tests.codex_support import FAKE, fake_env, run

ROOT = Path(__file__).resolve().parent.parent
REAL_SHIM = ROOT / "sandbox" / "codex-cli" / "node_modules" / ".bin" / "codex.cmd"
TRIPLE = "x86_64-pc-windows-msvc" if sys.platform == "win32" else "x86_64-unknown-linux-musl"
EXE = "codex.exe" if sys.platform == "win32" else "codex"

# What npm 11 writes for a local install (node_modules/.bin) and a global one (%APPDATA%\npm), copied from real shims.
LOCAL_CMD = '@ECHO off\r\nGOTO start\r\n:find_dp0\r\nSET dp0=%~dp0\r\nEXIT /b\r\n:start\r\nSETLOCAL\r\nCALL :find_dp0\r\n\r\nIF EXIST "%dp0%\\node.exe" (\r\n  SET "_prog=%dp0%\\node.exe"\r\n) ELSE (\r\n  SET "_prog=node"\r\n  SET PATHEXT=%PATHEXT:;.JS;=;%\r\n)\r\n\r\nendLocal & goto #_undefined_# 2>NUL || title %COMSPEC% & "%_prog%"  "%dp0%\\..\\@openai\\codex\\bin\\codex.js" %*\r\n'
GLOBAL_CMD = LOCAL_CMD.replace("%dp0%\\..\\@openai", "%dp0%\\node_modules\\@openai")
LOCAL_PS1 = '#!/usr/bin/env pwsh\n$basedir=Split-Path $MyInvocation.MyCommand.Definition -Parent\n& "$basedir/node$exe"  "$basedir/../@openai/codex/bin/codex.js" $args\n'
LOCAL_SH = '#!/bin/sh\nbasedir=$(dirname "$(echo "$0" | sed -e \'s,\\\\,/,g\')")\nexec node  "$basedir/../@openai/codex/bin/codex.js" "$@"\n'


def write(path: Path, text: str = "") -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def npm_package(node_modules: Path, *, nested: bool = False, platform_package: bool = True) -> Path:
    """``@openai/codex`` with its launcher, and the native exe where npm 11 puts it; returns the exe."""
    package = node_modules / "@openai" / "codex"
    write(package / "bin" / "codex.js", "// launcher")
    if not platform_package:
        return write(package / "vendor" / TRIPLE / "codex" / EXE, "exe")
    scope = package / "node_modules" / "@openai" if nested else node_modules / "@openai"
    return write(scope / "codex-win32-x64" / "vendor" / TRIPLE / "bin" / EXE, "exe")


@pytest.mark.skipif(sys.platform != "win32", reason="npm's .cmd shims are a Windows thing")
def test_a_local_npm_shim_runs_the_native_exe(tmp_path):
    node_modules = tmp_path / "proj" / "node_modules"
    native = npm_package(node_modules)
    shim = write(node_modules / ".bin" / "codex.cmd", LOCAL_CMD)
    assert shim_script(shim) == (node_modules / "@openai" / "codex" / "bin" / "codex.js").resolve()
    assert launch_command(shim) == [str(native)]
    assert launch_command(write(node_modules / ".bin" / "codex.ps1", LOCAL_PS1)) == [str(native)]
    assert launch_command(write(node_modules / ".bin" / "codex", LOCAL_SH)) == [str(native)]


@pytest.mark.skipif(sys.platform != "win32", reason="npm's .cmd shims are a Windows thing")
def test_the_global_npm_layout_and_the_older_vendor_layout(tmp_path):
    npm = tmp_path / "Roaming" / "npm"
    native = npm_package(npm / "node_modules", nested=True)
    assert launch_command(write(npm / "codex.cmd", GLOBAL_CMD)) == [str(native)]
    old = tmp_path / "old" / "node_modules"
    old_native = npm_package(old, platform_package=False)
    assert launch_command(write(old / ".bin" / "codex.cmd", LOCAL_CMD)) == [str(old_native)]


@pytest.mark.skipif(sys.platform != "win32", reason="npm's .cmd shims are a Windows thing")
def test_without_a_native_exe_node_runs_the_launcher_and_otherwise_cmd_runs_the_shim(tmp_path, monkeypatch):
    node_modules = tmp_path / "node_modules"
    write(node_modules / "@openai" / "codex" / "bin" / "codex.js", "// launcher")
    shim = write(node_modules / ".bin" / "codex.cmd", LOCAL_CMD)
    node = write(node_modules / ".bin" / "node.exe", "node")
    assert launch_command(shim) == [str(node), str((node_modules / "@openai" / "codex" / "bin" / "codex.js").resolve())]
    node.unlink()
    monkeypatch.setattr("desktop.core.codex.rpc.shutil.which", lambda name: None)
    monkeypatch.setenv("COMSPEC", r"C:\Windows\system32\cmd.exe")
    assert launch_command(shim) == [r"C:\Windows\system32\cmd.exe", "/d", "/c", str(shim)]
    unknown = write(tmp_path / "other" / "codex.cmd", "@echo off\r\nsomething else %*\r\n")
    assert launch_command(unknown) == [r"C:\Windows\system32\cmd.exe", "/d", "/c", str(unknown)]
    assert launch_command(write(tmp_path / "other" / "codex.ps1", "Write-Host hi"))[:2] == ["powershell.exe", "-NoLogo"]
    assert launch_command(tmp_path / "codex.exe") == [str(tmp_path / "codex.exe")]


@pytest.mark.skipif(sys.platform != "win32", reason="cmd.exe")
def test_the_cmd_fallback_really_carries_the_protocol(tmp_path):
    """A .cmd that is not an npm shim runs through cmd.exe, and JSON-RPC still flows through it both ways."""
    shim = write(tmp_path / "codex wrapper.cmd", f'@"{sys.executable}" "{FAKE}" %*\r\n')
    command = launch_command(shim)
    assert command[1:4] == ["/d", "/c", str(shim)]

    async def scenario():
        client = JsonRpcClient([*command, "app-server"], env=fake_env(tmp_path / "home"))
        await client.start()
        result = await client.request("initialize", {"clientInfo": {"name": "t", "version": "0"}, "capabilities": None}, timeout=20)
        assert result["platformOs"] == "windows"
        await client.close()
        assert client.exit_code == 0

    run(scenario())


@pytest.mark.skipif(not REAL_SHIM.is_file(), reason="the real npm install in sandbox/codex-cli is not there")
def test_the_real_npm_shim_resolves_to_codex_exe():
    command = launch_command(REAL_SHIM)
    assert len(command) == 1 and command[0].endswith(r"vendor\x86_64-pc-windows-msvc\bin\codex.exe")
    assert Path(command[0]).is_file()


def runner(versions: dict[str, str]):
    """A ``subprocess.run`` stand-in: ``versions`` maps the first argument to what ``--version`` prints."""
    calls: list[list[str]] = []

    def fake_run(argv, **options):
        calls.append(list(argv))
        assert argv[-1] == "--version" and options.get("timeout")
        output = versions.get(argv[0])
        if output is None:
            raise FileNotFoundError(argv[0])
        return SimpleNamespace(returncode=0, stdout=output, stderr="")

    fake_run.calls = calls  # type: ignore[attr-defined]
    return fake_run


def machine(tmp_path: Path) -> tuple[dict[str, str], Path]:
    home = tmp_path / "home"
    env = {"APPDATA": str(home / "AppData" / "Roaming"), "LOCALAPPDATA": str(home / "AppData" / "Local"), "ProgramFiles": str(tmp_path / "pf")}
    return env, home


def test_the_search_order_npm_editor_desktop(tmp_path):
    env, home = machine(tmp_path)
    npm_exe = npm_package(Path(env["APPDATA"]) / "npm" / "node_modules", nested=True)
    npm_shim = write(Path(env["APPDATA"]) / "npm" / "codex.cmd", GLOBAL_CMD)
    editor_exe = write(home / ".vscode" / "extensions" / "openai.chatgpt-26.1.0-win32-x64" / "bin" / "windows-x86_64" / "codex.exe", "exe")
    desktop_exe = write(Path(env["LOCALAPPDATA"]) / "OpenAI" / "Codex" / "bin" / "08d187884f2330d1" / "codex.exe", "exe")
    store_exe = write(tmp_path / "pf" / "WindowsApps" / "OpenAI.Codex_26.1023.6123.0_x64__2p2nqsd0c76g0" / "app" / "resources" / "codex.exe", "exe")
    candidates = codex_candidates(env=env, which=lambda name: None, home=home)
    assert [source for _, source in candidates] == ["npm", "editor", "desktop", "desktop"]
    assert [path for path, _ in candidates] == [npm_shim, editor_exe, desktop_exe, store_exe]
    run_version = runner({str(npm_exe): "codex-cli 0.165.0\n", str(editor_exe): "codex-cli 0.160.0", str(desktop_exe): "codex-cli 0.165.0-alpha.1.2"})
    found = locate_codex(env=env, which=lambda name: None, home=home, run=run_version)
    if sys.platform == "win32":
        assert found == CodexBinary(npm_shim, "0.165.0", "npm", (str(npm_exe),))
    npm_shim.unlink()
    found = locate_codex(env=env, which=lambda name: None, home=home, run=run_version)
    assert found is not None and (found.source, found.path, found.version) == ("editor", editor_exe, "0.160.0")
    editor_exe.unlink()
    found = locate_codex(env=env, which=lambda name: None, home=home, run=run_version)
    assert found is not None and (found.source, found.version) == ("desktop", "0.165.0-alpha.1.2")


def test_path_comes_first_and_a_broken_candidate_is_skipped(tmp_path):
    env, home = machine(tmp_path)
    on_path = write(tmp_path / "bin" / "codex.exe", "exe")
    desktop_exe = write(Path(env["LOCALAPPDATA"]) / "OpenAI" / "Codex" / "bin" / "abc" / "codex.exe", "exe")
    run_version = runner({str(desktop_exe): "codex-cli 0.165.0"})
    found = locate_codex(env=env, which=lambda name: str(on_path), home=home, run=run_version)
    assert found is not None and found.source == "desktop", "the one on PATH does not answer --version"
    run_version = runner({str(on_path): "codex-cli 0.166.1", str(desktop_exe): "codex-cli 0.165.0"})
    found = locate_codex(env=env, which=lambda name: str(on_path), home=home, run=run_version)
    assert found is not None and (found.source, found.version, found.argv("app-server")) == ("path", "0.166.1", [str(on_path), "app-server"])


def test_an_explicit_path_is_the_only_candidate(tmp_path):
    env, home = machine(tmp_path)
    desktop_exe = write(Path(env["LOCALAPPDATA"]) / "OpenAI" / "Codex" / "bin" / "abc" / "codex.exe", "exe")
    explicit = write(tmp_path / "mine" / "codex.exe", "exe")
    run_version = runner({str(desktop_exe): "codex-cli 0.165.0", str(explicit): "codex-cli 0.150.0"})
    assert locate_codex(str(tmp_path / "missing.exe"), env=env, home=home, run=run_version) is None
    found = locate_codex(f'  "{explicit}"  ', env=env, home=home, run=run_version)
    assert found is not None and (found.source, found.version) == ("settings", "0.150.0")


def test_the_version_probe(tmp_path):
    def answer(code: int, stdout: str):
        return lambda argv, **options: SimpleNamespace(returncode=code, stdout=stdout, stderr="")

    assert probe_version(["codex"], run=answer(0, "codex-cli 0.165.0-alpha.1.2\n")) == "0.165.0-alpha.1.2"
    assert probe_version(["codex"], run=answer(0, "codex 1.2.3")) == "1.2.3"
    assert probe_version(["codex"], run=answer(0, "dev build\n")) == "dev build"
    assert probe_version(["codex"], run=answer(1, "codex-cli 0.165.0")) is None

    def fails(argv, **options):
        raise subprocess.TimeoutExpired(argv, 1)

    assert probe_version(["codex"], run=fails) is None
    assert probe_version([sys.executable, str(FAKE)]) == "0.165.0", "a real subprocess (the fake prints codex-cli 0.165.0)"
