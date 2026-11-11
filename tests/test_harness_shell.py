"""The shell tool on the real Windows PowerShell: encoding, exit codes, merged stderr, limits, process-tree kill."""

from __future__ import annotations

import asyncio
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

from desktop.core.harness import shell as shell_module
from desktop.core.harness.shell import MAX_OUTPUT_CHARS, cap_output, mentioned_protected, run_shell
from desktop.core.harness.tools import ToolContext, ToolRegistry, ToolResult
from desktop.core.winproc import CREATE_NO_WINDOW

windows_only = pytest.mark.skipif(sys.platform != "win32", reason="runs Windows PowerShell")


def run(coro):
    return asyncio.run(coro)


@pytest.fixture
def project(tmp_path: Path) -> Path:
    folder = tmp_path / "proj"
    folder.mkdir()
    return folder


@pytest.fixture
def ctx(project: Path) -> ToolContext:
    return ToolContext(project=project)


def shell(ctx: ToolContext, command: str, **extra) -> ToolResult:
    return run(ToolRegistry.default().run("shell", {"command": command, **extra}, ctx))


def pid_alive(pid: int) -> bool:
    listing = subprocess.run(["tasklist", "/FI", f"PID eq {pid}", "/FO", "CSV", "/NH"], capture_output=True, text=True).stdout
    return f'"{pid}"' in listing


def wait_gone(pid: int, seconds: float = 5.0) -> bool:
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        if not pid_alive(pid):
            return True
        time.sleep(0.1)
    return not pid_alive(pid)


# --------------------------------------------------------------------------- output handling (no process)


def test_cap_output_keeps_head_and_tail():
    text = "".join(f"line {n}\n" for n in range(10_000))
    capped = cap_output(text)
    half = MAX_OUTPUT_CHARS // 2
    assert capped.startswith(text[:half]) and capped.endswith(text[-half:])
    omitted = len(text) - 2 * half
    assert f"\n[... {omitted} characters omitted ...]\n" in capped
    assert cap_output("short") == "short"
    exact = "y" * MAX_OUTPUT_CHARS
    assert cap_output(exact) == exact


def test_capture_keeps_everything_when_small_and_ends_cleanly_when_large():
    capture = shell_module._Capture()
    capture.feed("你好\r\n世界  \r\n\r\n".encode("utf-8"))
    assert capture.text() == "你好\n世界"
    big = shell_module._Capture()
    chunk = ("中" * 100).encode("utf-8")
    for _ in range(3000):
        big.feed(chunk)
    text = big.text()
    assert "\ufffd" not in text
    assert len(big.head) == shell_module.HEAD_BYTES and len(big.tail) == shell_module.TAIL_BYTES
    assert text.count("中") == MAX_OUTPUT_CHARS
    omitted = int(text.split("[... ")[1].split(" characters")[0])
    assert abs(omitted - (300_000 - MAX_OUTPUT_CHARS)) < 2_000


def test_capture_splits_a_multibyte_character_at_the_cut_without_garbage():
    capture = shell_module._Capture()
    capture.feed(b"a" * (shell_module.HEAD_BYTES - 1) + "中".encode("utf-8") + b"b" * (shell_module.TAIL_BYTES + 5))
    capture.feed("文".encode("utf-8") * 10)
    assert "\ufffd" not in capture.text()


def test_mentioned_protected(tmp_path: Path, monkeypatch):
    secret = tmp_path / "WittyCode" / "chatgpt"
    secret.mkdir(parents=True)
    protected = (secret,)
    assert mentioned_protected("cat " + str(secret / "a.json"), protected) == secret
    assert mentioned_protected("cat " + str(secret).replace("\\", "/").upper(), protected) == secret
    assert mentioned_protected("cat " + str(secret).replace("\\", "\\\\"), protected) == secret
    assert mentioned_protected("cat " + str(tmp_path / "WittyCode" / "other"), protected) is None
    assert mentioned_protected("anything", ()) is None
    monkeypatch.setenv("WC_TEST_ROOT", str(tmp_path))
    assert mentioned_protected("type $env:WC_TEST_ROOT\\WittyCode\\chatgpt\\x", protected) == secret
    assert mentioned_protected("type %WC_TEST_ROOT%\\WittyCode\\chatgpt\\x", protected) == secret
    assert mentioned_protected("type ${env:WC_TEST_ROOT}\\WittyCode\\chatgpt\\x", protected) == secret


# --------------------------------------------------------------------------- real PowerShell


@windows_only
def test_output_and_exit_code_line(ctx):
    result = shell(ctx, "Write-Output hello")
    assert result.output == "hello\nExit code: 0" and not result.is_error


@windows_only
def test_no_output(ctx):
    assert shell(ctx, "$null = 1").output == "(no output)\nExit code: 0"


@windows_only
def test_chinese_round_trip(ctx, project):
    result = shell(ctx, "Write-Output '你好，世界 ✓'")
    assert result.output == "你好，世界 ✓\nExit code: 0"
    made = shell(ctx, "Set-Content -Path '测试文件.txt' -Value '内容：你好' -Encoding UTF8; Get-Content '测试文件.txt' -Encoding UTF8")
    assert made.output == "内容：你好\nExit code: 0"
    assert (project / "测试文件.txt").read_text(encoding="utf-8-sig").strip() == "内容：你好"
    listing = shell(ctx, "Get-ChildItem -Name")
    assert "测试文件.txt" in listing.output


@windows_only
def test_native_python_chinese_output(ctx):
    command = f"& '{sys.executable}' -c \"print('原生程序：你好')\""
    assert shell(ctx, command).output == "原生程序：你好\nExit code: 0"


@windows_only
@pytest.mark.parametrize(
    "command, code",
    [
        ("exit 0", 0),
        ("exit 7", 7),
        ("cmd /c exit 3", 3),
        ("Get-Item .\\no-such-file-here", 1),
        ("throw 'boom'", 1),
        ("Write-Error 'bad'", 1),
        ("cmd /c exit 4; Write-Output ok", 0),
        ("Write-Output ok; cmd /c exit 5", 5),
        ("git definitely-not-a-command", 1),
    ],
)
def test_exit_codes(ctx, command, code):
    result = shell(ctx, command)
    assert result.output.splitlines()[-1] == f"Exit code: {code}"
    assert result.is_error is (code != 0)


@windows_only
def test_stderr_is_merged_with_stdout(ctx):
    result = shell(ctx, "[Console]::Error.WriteLine('to stderr 中文'); Write-Output 'to stdout'; cmd /c 'echo native err 1>&2'")
    lines = result.output.splitlines()
    assert "to stderr 中文" in lines and "to stdout" in lines and "native err" in lines
    assert "CLIXML" not in result.output
    errors = shell(ctx, "Get-Item .\\missing-thing")
    assert "Cannot find path" in errors.output and "CLIXML" not in errors.output and "<Objs" not in errors.output


@windows_only
def test_syntax_error_is_reported_before_anything_runs(ctx):
    result = shell(ctx, "Set-Content -Path ran.txt -Value x; echo (")
    assert result.is_error and "ParserError" in result.output
    assert result.output.splitlines()[-1] == "Exit code: 1"
    assert not (ctx.project / "ran.txt").exists()


@windows_only
def test_tables_are_printed_before_exit(ctx):
    result = shell(ctx, "Get-Location")
    assert "Path" in result.output and str(ctx.project.resolve()) in result.output


@windows_only
def test_runs_in_the_project_or_the_given_cwd(ctx, project):
    (project / "sub").mkdir()
    assert shell(ctx, "(Get-Location).Path").output.splitlines()[0] == str(project.resolve())
    assert shell(ctx, "(Get-Location).Path", cwd="sub").output.splitlines()[0] == str((project / "sub").resolve())
    assert shell(ctx, "(Get-Location).Path", cwd=str(project / "sub")).output.splitlines()[0] == str((project / "sub").resolve())


@windows_only
def test_cwd_must_be_an_existing_folder(ctx, project):
    (project / "afile").write_text("x", encoding="utf-8")
    missing = shell(ctx, "echo hi", cwd="nope")
    assert missing.is_error and "does not exist" in missing.output
    file = shell(ctx, "echo hi", cwd="afile")
    assert file.is_error and "must be a folder" in file.output


@windows_only
def test_environment_for_non_interactive_use(ctx):
    result = shell(ctx, "'{0}|{1}|{2}|{3}' -f $env:GIT_PAGER, $env:PAGER, $env:TERM, $env:NO_COLOR")
    assert result.output.splitlines()[0] == "cat|cat|dumb|1"
    assert shell(ctx, "if ($env:WITTYCODE_SHELL_COMMAND) { 'leaked' } else { 'clean' }").output.splitlines()[0] == "clean"


@windows_only
def test_stdin_is_closed(ctx):
    started = time.monotonic()
    result = shell(ctx, "$line = [Console]::In.ReadLine(); if ($null -eq $line) { 'eof' } else { 'got ' + $line }", timeout=30)
    assert result.output.splitlines()[0] == "eof"
    assert time.monotonic() - started < 25


@windows_only
def test_no_console_window_flag_and_launch_arguments(ctx, monkeypatch):
    calls = []
    real = asyncio.create_subprocess_exec

    async def spy(*args, **kwargs):
        calls.append((args, kwargs))
        return await real(*args, **kwargs)

    monkeypatch.setattr(asyncio, "create_subprocess_exec", spy)
    assert shell(ctx, "Write-Output spied").output.startswith("spied")
    run(run_shell("Start-Sleep 30", ctx.project, 1.0, None))
    assert len(calls) == 3 and calls[2][0][1:4] == ("/F", "/T", "/PID"), "the second run is killed with taskkill"
    assert all(kwargs["creationflags"] == CREATE_NO_WINDOW for _, kwargs in calls), "no call may open a console window"
    args, kwargs = calls[0]
    assert kwargs["creationflags"] == CREATE_NO_WINDOW == 0x08000000
    assert kwargs["stdin"] == asyncio.subprocess.DEVNULL
    assert kwargs["stdout"] == asyncio.subprocess.PIPE and kwargs["stderr"] == asyncio.subprocess.STDOUT
    assert args[0].lower().endswith("powershell.exe")
    for flag in ("-NoLogo", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass"):
        assert flag in args
    assert "Write-Output spied" not in " ".join(args), "the command must not travel on the command line"
    assert kwargs["env"]["NO_COLOR"] == "1"


@windows_only
def test_long_commands_travel_in_the_environment(ctx):
    command = "$total = 0; " + "".join(f"$total += {n}; " for n in range(1, 1501)) + "Write-Output $total"
    assert 20_000 < len(command) < 30_000
    assert shell(ctx, command).output == f"{sum(range(1, 1501))}\nExit code: 0"
    too_long = shell(ctx, "x" * 30_001)
    assert too_long.is_error and "limit" in too_long.output and "create_file" in too_long.output
    assert shell(ctx, "   ").is_error


@windows_only
def test_output_cap_keeps_head_and_tail(ctx):
    result = shell(ctx, "1..5000 | ForEach-Object { 'line number ' + $_ }")
    body = "\n".join(f"line number {n}" for n in range(1, 5001))
    half = MAX_OUTPUT_CHARS // 2
    assert result.output.startswith(body[:half])
    assert f"\n[... {len(body) - MAX_OUTPUT_CHARS} characters omitted ...]\n{body[-half:]}\nExit code: 0" in result.output
    assert len(result.output) < MAX_OUTPUT_CHARS + 100


@windows_only
def test_huge_output_stays_bounded(ctx, tmp_path):
    script = tmp_path / "gen.py"
    script.write_text("import sys\nsys.stdout.write('abcdefghi\\n' * 500000)\n", encoding="utf-8")
    result = shell(ctx, f"& '{sys.executable}' '{script}'", timeout=60)
    assert result.output.splitlines()[-1] == "Exit code: 0"
    assert len(result.output) < MAX_OUTPUT_CHARS + 100
    assert result.output.startswith("abcdefghi\nabcdefghi")
    omitted = int(result.output.split("[... ")[1].split(" characters")[0])
    assert 4_900_000 < omitted < 5_000_000


@windows_only
def test_timeout_kills_the_whole_process_tree(ctx, project):
    pid_file = project / "child.pid"
    command = (
        "$child = Start-Process -FilePath powershell.exe -ArgumentList '-NoProfile','-Command','Start-Sleep 120' "
        "-PassThru -WindowStyle Hidden; "
        f"$child.Id | Out-File -Encoding ascii '{pid_file}'; Write-Output \"child=$($child.Id)\"; Start-Sleep 120"
    )
    started = time.monotonic()
    result = shell(ctx, command, timeout=5)
    elapsed = time.monotonic() - started
    assert result.is_error
    lines = result.output.splitlines()
    assert lines[-2] == "Timed out after 5 s" and lines[-1].startswith("Exit code: ")
    assert elapsed < 15
    child = int(pid_file.read_text(encoding="ascii").strip())
    assert f"child={child}" in result.output
    assert wait_gone(child), "the child process outlived the timeout"


@windows_only
def test_abort_stops_a_long_command_quickly_and_kills_children(ctx, project):
    pid_file = project / "child.pid"
    command = (
        "$child = Start-Process -FilePath powershell.exe -ArgumentList '-NoProfile','-Command','Start-Sleep 120' "
        "-PassThru -WindowStyle Hidden; "
        f"$child.Id | Out-File -Encoding ascii '{pid_file}'; Write-Output ready; Start-Sleep 120"
    )
    stamps: dict[str, float] = {}

    def stopper() -> None:
        end = time.monotonic() + 20
        while not pid_file.exists() and time.monotonic() < end:
            time.sleep(0.05)
        time.sleep(0.3)
        stamps["set"] = time.monotonic()
        ctx.abort.set()

    thread = threading.Thread(target=stopper)
    thread.start()
    result = shell(ctx, command, timeout=120)
    stamps["done"] = time.monotonic()
    thread.join()
    assert stamps["done"] - stamps["set"] < 2.5
    assert result.is_error
    lines = result.output.splitlines()
    assert "ready" in lines and lines[-2] == "Stopped by the user" and lines[-1].startswith("Exit code: ")
    assert wait_gone(int(pid_file.read_text(encoding="ascii").strip()))


@windows_only
def test_abort_already_set_runs_nothing(ctx, project):
    ctx.abort.set()
    result = shell(ctx, "Set-Content -Path ran.txt -Value x")
    assert result.is_error and "Stopped by the user" in result.output
    assert not (project / "ran.txt").exists()


@windows_only
def test_cancelling_the_task_kills_the_tree(ctx, project):
    pid_file = project / "child.pid"
    command = f"$p = Start-Process -FilePath powershell.exe -ArgumentList '-NoProfile','-Command','Start-Sleep 120' -PassThru -WindowStyle Hidden; $p.Id | Out-File -Encoding ascii '{pid_file}'; Start-Sleep 120"

    async def scenario() -> None:
        task = asyncio.ensure_future(run_shell(command, project, 120, None))
        end = time.monotonic() + 20
        while not pid_file.exists() and time.monotonic() < end:
            await asyncio.sleep(0.05)
        await asyncio.sleep(0.3)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    run(scenario())
    assert wait_gone(int(pid_file.read_text(encoding="ascii").strip()))


@windows_only
def test_run_shell_returns_the_four_values(project):
    output, code, timed_out, aborted = run(run_shell("Write-Output hi; exit 2", project, 30, threading.Event()))
    assert (output, code, timed_out, aborted) == ("hi", 2, False, False)
    output, code, timed_out, aborted = run(run_shell("Start-Sleep 60", project, 1.5, None))
    assert timed_out and not aborted and code != 0


@windows_only
def test_protected_paths_in_the_command_text_are_refused(project, tmp_path):
    secret = tmp_path / "WittyCode" / "chatgpt"
    secret.mkdir(parents=True)
    (secret / "acct.json").write_text("TOKEN-VALUE", encoding="utf-8")
    context = ToolContext(project, protected=(secret,))
    for command in (
        f"Get-Content '{secret}\\acct.json'",
        f"Get-Content {str(secret).replace(chr(92), '/')}/acct.json",
        f"type \"{str(secret).upper()}\\ACCT.JSON\"",
    ):
        result = shell(context, command)
        assert result.is_error and "protected" in result.output and "TOKEN-VALUE" not in result.output, command
    inside = shell(context, "Get-Location", cwd=str(secret))
    assert inside.is_error and "protected" in inside.output
    assert not shell(context, "Write-Output fine").is_error
