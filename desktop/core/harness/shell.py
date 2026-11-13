"""The shell tool: one command in Windows PowerShell 5.1 (``/bin/sh`` elsewhere), merged output, hard limits.

How the command reaches PowerShell, and why it is not ``-EncodedCommand``: with ``-EncodedCommand`` Windows PowerShell 5.1
wraps everything its host writes to stderr in CLIXML (``#< CLIXML`` / ``<Objs ...>``, plus a "Preparing modules for first
use" progress record), and no ``-OutputFormat``/``-InputFormat`` setting turns that off. Instead a fixed one-line bootstrap
is passed with ``-Command`` (it contains no double quote, so no quoting problem) and the model's command travels in the
environment variable ``WITTYCODE_SHELL_COMMAND``, which the bootstrap reads and removes before anything runs. That keeps
the command out of the command line (no quoting, no 8K/32K command-line trouble; the limit is one environment variable,
``MAX_COMMAND_CHARS``) and out of the environment of the programs it starts.

The bootstrap sets the console and ``$OutputEncoding`` to UTF-8 without a BOM (so Chinese output survives), parses the
command as a script block (a syntax error is reported after the encoding is set, not as a mangled host error) and
dot-sources it with the commands' own ``$?`` and ``$LASTEXITCODE`` captured by a trailing statement. The process exit
code is: 0 when the last statement succeeded, else the native exit code if there is one, else 1. An ``exit N`` in the
command wins, as usual. The block is piped to ``Out-Default``: PowerShell holds a formatted table (``Get-Location``,
``[pscustomobject]@{...}``) until its pipeline ends, and the ``exit`` that follows would otherwise drop it unprinted.

Python children get ``PYTHONIOENCODING=utf-8`` for the same reason as the console encoding: their pipe output would
otherwise be in the ANSI code page and be decoded wrongly.

Limits: the run is bounded by a timeout and by ``abort`` (a ``threading.Event`` polled every 100 ms from the event loop,
no worker thread). Either one kills the whole process tree (``taskkill /F /T``); output beyond 30,000 characters keeps its
head and tail. Memory stays bounded by keeping only the first and last 64 KiB of raw output.

``ToolContext.protected`` (the engine's own folder: credentials, saved conversations) is refused twice over: once on the
command text (``mentioned_protected``) and once on where each argument actually leads (``protected_argument``, which
resolves links and junctions). There is no operating-system sandbox, so neither check constrains a path the command
builds while it runs; ``permissions`` asks rather than allows when an argument cannot be resolved.
"""

from __future__ import annotations

import asyncio
import codecs
import contextlib
import fnmatch
import os
import re
import shutil
import signal
import sys
import threading
from collections.abc import Iterable, Iterator
from pathlib import Path
from typing import Any

from desktop.core.harness.fs_tools import is_protected, is_within, resolve_path
from desktop.core.harness.tools import ToolContext, ToolError, ToolRegistry, ToolResult, ToolSpec, passthrough_input
from desktop.core.winproc import CREATE_NO_WINDOW

IS_WINDOWS = sys.platform == "win32"

DEFAULT_TIMEOUT = 120
MAX_TIMEOUT = 600
MAX_COMMAND_CHARS = 30_000
MAX_OUTPUT_CHARS = 30_000
HEAD_BYTES = TAIL_BYTES = 64 * 1024
POLL_SECONDS = 0.1
KILL_WAIT_SECONDS = 5.0
DRAIN_SECONDS = 2.0
COMMAND_ENV = "WITTYCODE_SHELL_COMMAND"

_BOOTSTRAP = (
    "$wc_u = New-Object System.Text.UTF8Encoding($false); "
    "[Console]::OutputEncoding = $wc_u; $OutputEncoding = $wc_u; "
    "$ErrorActionPreference = 'Continue'; $ProgressPreference = 'SilentlyContinue'; "
    f"$wc_c = $env:{COMMAND_ENV}; Remove-Item Env:{COMMAND_ENV}; "
    "try { $null = [scriptblock]::Create($wc_c) } "
    "catch { [Console]::Error.WriteLine('ParserError: ' + $_.Exception.InnerException.Message); exit 1 }; "
    "$wc_b = [scriptblock]::Create($wc_c + [char]10 + '$global:wcok = $?; $global:wccode = $LASTEXITCODE'); "
    ". $wc_b | Out-Default; "
    "if ($global:wcok -ne $false) { exit 0 }; "
    "if ($global:wccode) { exit $global:wccode }; "
    "exit 1"
)


def _powershell() -> str:
    root = os.environ.get("SystemRoot")
    if root:
        candidate = Path(root) / "System32" / "WindowsPowerShell" / "v1.0" / "powershell.exe"
        if candidate.is_file():
            return str(candidate)
    return shutil.which("powershell.exe") or "powershell.exe"


_GIT_NEUTRAL = (
    ("core.fsmonitor", "false"),
    ("core.pager", "cat"),
    ("core.askPass", ""),
    ("core.gitProxy", ""),
    ("core.alternateRefsCommand", ""),
    ("uploadpack.packObjectsHook", ""),
)
"""Repository settings that would let a repository choose a program for Git to run, with a value that stops it.

Only the ones that no ordinary command needs: ``diff.external``, a ``textconv`` or ``filter`` driver, ``gpg.program``,
``core.hooksPath`` and ``credential.helper`` have no neutral value here. Emptying ``diff.external`` makes every
``git diff`` fail ("cannot spawn"), and pointing ``core.hooksPath`` at a folder that does not exist would move the
hooks Git runs from ``.git/hooks`` (which a write has to be confirmed for) to a name inside the project (which it does
not). ``permissions.git_runs_own_programs`` asks about those instead.
"""


def _git_env(env: dict[str, str]) -> None:
    """Add the neutralising settings as ``GIT_CONFIG_*`` entries, after any the environment already carries.

    Git applies them like ``-c`` options, which beat the repository's own ``.git/config``, and passes them on to every
    Git it starts itself. Done for every command, not only for one that names Git: a script, a build or a hook may run
    Git too, and these values take nothing away from it.
    """
    try:
        start = int(env.get("GIT_CONFIG_COUNT", "0"))
    except ValueError:
        start = 0
    for offset, (key, value) in enumerate(_GIT_NEUTRAL):
        env[f"GIT_CONFIG_KEY_{start + offset}"] = key
        env[f"GIT_CONFIG_VALUE_{start + offset}"] = value
    env["GIT_CONFIG_COUNT"] = str(start + len(_GIT_NEUTRAL))


def _launch(command: str) -> tuple[list[str], dict[str, str]]:
    env = dict(os.environ)
    env.update(GIT_PAGER="cat", PAGER="cat", TERM="dumb", NO_COLOR="1", PYTHONIOENCODING="utf-8")
    _git_env(env)
    if IS_WINDOWS:
        env[COMMAND_ENV] = command
        argv = [_powershell(), "-NoLogo", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-Command", _BOOTSTRAP]
        return argv, env
    return ["/bin/sh", "-c", command], env


# --------------------------------------------------------------------------- output


def _normalise(text: str) -> str:
    """LF line ends, no trailing blanks (PowerShell pads its tables to the console width), no blank lines around."""
    return "\n".join(line.rstrip() for line in text.replace("\r\n", "\n").split("\n")).strip("\n")


def cap_output(text: str, limit: int = MAX_OUTPUT_CHARS) -> str:
    if len(text) <= limit:
        return text
    half = limit // 2
    return f"{text[:half]}\n[... {len(text) - 2 * half} characters omitted ...]\n{text[-half:]}"


class _Capture:
    """The first and the last bytes of a stream; whatever lies between is only counted."""

    def __init__(self) -> None:
        self.head = bytearray()
        self.tail = bytearray()
        self.dropped = 0

    def feed(self, chunk: bytes) -> None:
        room = HEAD_BYTES - len(self.head)
        if room > 0:
            self.head += chunk[:room]
            chunk = chunk[room:]
        if not chunk:
            return
        self.tail += chunk
        extra = len(self.tail) - TAIL_BYTES
        if extra > 0:
            del self.tail[:extra]
            self.dropped += extra

    def text(self) -> str:
        if not self.dropped:
            return cap_output(_normalise(bytes(self.head + self.tail).decode("utf-8", "replace")))
        # An incomplete character at either cut would decode to U+FFFD: drop it instead.
        head = codecs.getincrementaldecoder("utf-8")("replace").decode(bytes(self.head), final=False)
        tail_bytes = bytes(self.tail)
        skip = 0
        while skip < min(4, len(tail_bytes)) and tail_bytes[skip] & 0xC0 == 0x80:
            skip += 1
        tail = tail_bytes[skip:].decode("utf-8", "replace")
        kept_bytes = len(self.head) + len(self.tail)
        head, tail = _normalise(head), _normalise(tail)
        half = MAX_OUTPUT_CHARS // 2
        # The dropped middle is only known in bytes; scale by the characters-per-byte of what was kept.
        lost_chars = round(self.dropped * (len(head) + len(tail)) / kept_bytes)
        omitted = (len(head) - half) + (len(tail) - half) + lost_chars
        return f"{head[:half]}\n[... {omitted} characters omitted ...]\n{tail[-half:]}"


async def _pump(stream: asyncio.StreamReader, capture: _Capture) -> None:
    while True:
        chunk = await stream.read(65536)
        if not chunk:
            return
        capture.feed(chunk)


# --------------------------------------------------------------------------- process


async def _kill_tree(process: asyncio.subprocess.Process) -> None:
    """End the process and everything it started. ``taskkill /T`` walks the parent links, so it must run while the
    shell is still alive."""
    if process.returncode is None:
        if IS_WINDOWS:
            try:
                killer = await asyncio.create_subprocess_exec(
                    shutil.which("taskkill") or "taskkill",
                    "/F",
                    "/T",
                    "/PID",
                    str(process.pid),
                    stdin=asyncio.subprocess.DEVNULL,
                    stdout=asyncio.subprocess.DEVNULL,
                    stderr=asyncio.subprocess.DEVNULL,
                    creationflags=CREATE_NO_WINDOW,
                )
                await asyncio.wait_for(killer.wait(), KILL_WAIT_SECONDS)
            except (OSError, asyncio.TimeoutError):
                pass
        else:
            with contextlib.suppress(ProcessLookupError, PermissionError):
                os.killpg(process.pid, signal.SIGKILL)
        with contextlib.suppress(ProcessLookupError):
            process.kill()


async def _reap(process: asyncio.subprocess.Process, waiter: asyncio.Future[Any], reader: asyncio.Future[Any]) -> None:
    with contextlib.suppress(asyncio.TimeoutError, OSError):
        await asyncio.wait_for(asyncio.shield(waiter), KILL_WAIT_SECONDS)
    # A program the shell left running may still hold the pipe open; what it printed so far is all there will be.
    _, pending = await asyncio.wait({reader}, timeout=DRAIN_SECONDS)
    for task in pending:
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError, Exception):
            await task


async def run_shell(
    command: str,
    cwd: Path,
    timeout: float,
    abort_event: threading.Event | None = None,
) -> tuple[str, int, bool, bool]:
    """Run ``command``; returns ``(output, exit_code, timed_out, aborted)``. ``exit_code`` is -1 when there is none."""
    if abort_event is not None and abort_event.is_set():
        return "", -1, False, True
    argv, env = _launch(command)
    extra: dict[str, Any] = {"creationflags": CREATE_NO_WINDOW} if IS_WINDOWS else {"start_new_session": True}
    process = await asyncio.create_subprocess_exec(
        *argv,
        stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
        cwd=str(cwd),
        env=env,
        **extra,
    )
    capture = _Capture()
    assert process.stdout is not None
    reader = asyncio.ensure_future(_pump(process.stdout, capture))
    waiter = asyncio.ensure_future(process.wait())
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    timed_out = aborted = False
    try:
        while True:
            if abort_event is not None and abort_event.is_set():
                aborted = True
                break
            remaining = deadline - loop.time()
            if remaining <= 0:
                timed_out = True
                break
            done, _ = await asyncio.wait({waiter}, timeout=min(POLL_SECONDS, remaining))
            if done:
                break
        if timed_out or aborted:
            await _kill_tree(process)
        await _reap(process, waiter, reader)
    except asyncio.CancelledError:
        await asyncio.shield(_cancel_cleanup(process, waiter, reader))
        raise
    code = process.returncode if process.returncode is not None else -1
    return capture.text(), code, timed_out, aborted


async def _cancel_cleanup(process: asyncio.subprocess.Process, waiter: asyncio.Future[Any], reader: asyncio.Future[Any]) -> None:
    await _kill_tree(process)
    await _reap(process, waiter, reader)


# --------------------------------------------------------------------------- protected paths in command text

_ENV_REFERENCE = re.compile(r"%([A-Za-z_][A-Za-z0-9_]*)%|\$\{?env:([A-Za-z_][A-Za-z0-9_]*)\}?", re.IGNORECASE)


def _flat(text: str) -> str:
    return re.sub(r"\\+", r"\\", text.replace("/", "\\")).lower()


def _expand_environment(text: str) -> str:
    return _ENV_REFERENCE.sub(lambda match: os.environ.get(match.group(1) or match.group(2), match.group(0)), text)


def mentioned_protected(command: str, protected: Iterable[Path]) -> Path | None:
    """The first protected path the command text names, or None.

    Only the text: ``%VAR%`` and ``$env:VAR`` are expanded, and ``/`` equals ``\\`` and case is ignored. Where an argument
    actually leads is ``protected_argument``'s question.
    """
    roots = list(protected)
    if not roots:
        return None
    expanded = _expand_environment(command)
    flat = _flat(expanded)
    for root in roots:
        variants = {str(root)}
        with contextlib.suppress(OSError, ValueError, RuntimeError):
            variants.add(str(Path(root).resolve()))
        # Not followed by a name character: "...\chatgpt-notes" is another folder than "...\chatgpt".
        if any(re.search(re.escape(_flat(variant)) + r"(?![\w.\-])", flat) for variant in variants):
            return Path(root)
    return None


# --------------------------------------------------------------------------- where a command's arguments lead

_OPTION = re.compile(r"^(?:/[A-Za-z?]|--?[A-Za-z][\w-]*)(?:[:=](.*))?$", re.DOTALL)
_WILDCARD = "*?["
_GLUED = re.compile(r"[;|&,]+")
_DEVICE_PREFIX = re.compile(r"^[\\/]{2}[?.][\\/](?=[A-Za-z]:)")
_NESTED_DEPTH = 2
_LISTING_LIMIT = 5000
_EXPANSION_LISTINGS = 256
"""How many folders the wildcards of one argument may list; ``*/*/*`` in a large tree would otherwise list them all."""
_EXPANSION_TARGETS = 5000


def command_tokens(text: str) -> list[str]:
    """The command split the way a shell splits it: whitespace separates, quotes group and are dropped."""
    tokens: list[str] = []
    current: list[str] = []
    quote = ""
    started = False
    for char in text:
        if quote:
            if char == quote:
                quote = ""
            else:
                current.append(char)
        elif char in "'\"":
            quote = char
            started = True
        elif char.isspace():
            if started or current:
                tokens.append("".join(current))
            current, started = [], False
        else:
            current.append(char)
    if started or current:
        tokens.append("".join(current))
    return tokens


def argument_pieces(token: str) -> list[str]:
    """The names a token can stand for: itself or the value of ``--name=value`` / ``-Path:value`` / ``/f:value``, plus the
    parts of a list (``a.txt,b.txt`` is two files to PowerShell) and of text glued to a separator (``a.txt;cat``). A bare
    option (``/s``, ``-Recurse``, ``--version``) names nothing."""
    option = _OPTION.match(token)
    value = (option.group(1) or "") if option else token
    if not value:
        return []
    parts = [part for part in _GLUED.split(value) if part]
    return [value, *parts] if len(parts) > 1 else [value]


def _fixed_part(value: str) -> str | None:
    """The part of a path before any wildcard (``src/*.py`` -> ``src``), or None when a wildcard stands for a folder
    (``*/x.txt``, ``linked-*/x.txt``): what it expands to is not known here."""
    segments = re.split(r"[\\/]", value)
    for index, segment in enumerate(segments):
        if any(char in segment for char in _WILDCARD):
            if index < len(segments) - 1:
                return None
            return "/".join(segments[:index]) or "."
    return value


_FILESYSTEM_PROVIDER = re.compile(r"^(?:Microsoft\.PowerShell\.Core[\\/])?FileSystem::", re.IGNORECASE)


def _filesystem_argument(value: str) -> str | None:
    """Recognise only ordinary paths or PowerShell's known filesystem provider, never an arbitrary PSDrive."""
    expanded = _DEVICE_PREFIX.sub("", _FILESYSTEM_PROVIDER.sub("", _expand_environment(value)))
    if ":" in expanded and not re.match(r"^[A-Za-z]:[\\/]", expanded):
        return None
    return expanded


def resolved_argument(value: str, cwd: Path) -> Path | None:
    """Where an argument leads, with links followed (``Path.resolve`` uses ``os.path.realpath``, which follows a Windows
    junction as well as a symlink), or None when that cannot be known."""
    expanded = _filesystem_argument(value)
    if expanded is None or re.match(r"[\\/]{2}", expanded):
        return None  # resolving \\host\share would connect to that host before anyone approved the command
    fixed = _fixed_part(expanded)
    if fixed is None:
        return None
    try:
        path = Path(os.path.expanduser(fixed))
        if path.drive and not path.is_absolute():
            return None  # "C:name" is relative to the process's folder on drive C:, not to ours
        return (path if path.is_absolute() else cwd / path).resolve()
    except (OSError, ValueError, RuntimeError):
        return None


def _next_wildcard(segments: list[str], start: int) -> int | None:
    return next((index for index in range(start, len(segments)) if any(char in segments[index] for char in _WILDCARD)), None)


def _resolved(path: Path) -> Path | None:
    try:
        return path.resolve()
    except (OSError, ValueError, RuntimeError):
        return None


def _wildcard_targets(value: str, cwd: Path) -> list[Path | None]:
    """Where the wildcards of an argument lead, as the shell would expand them, from a listing of every folder they
    stand in: ``linked*`` -> where ``linked-private`` leads, ``*/linked*/*`` -> what is inside ``src/linked-private``.

    Every wildcard segment is expanded, the last one too: ``_fixed_part`` answers for a wildcard with the folder in
    front of it, which says nothing about what the pattern picks, and what it picks may be a link out of the project
    or into a protected folder, at any depth. Each match is resolved before the next segment is listed, so a link is
    followed where the shell would follow it. A folder that cannot be listed adds nothing. A listing cut off at
    ``_LISTING_LIMIT``, and anything past ``_EXPANSION_LISTINGS`` listings or ``_EXPANSION_TARGETS`` targets, adds
    None ("not knowable"), which every caller reads as outside the project.
    """
    expanded = _filesystem_argument(value)
    if expanded is None or re.match(r"[\\/]{2}", expanded):
        return []
    segments = re.split(r"[\\/]", expanded)
    index = _next_wildcard(segments, 0)
    if index is None:
        return []
    folder = resolved_argument("/".join(segments[:index]) or ".", cwd)
    if folder is None:
        return []
    targets: list[Path | None] = []
    pending = [(folder, index)]
    listings = 0
    while pending:
        folder, index = pending.pop()
        listings += 1
        if listings > _EXPANSION_LISTINGS:
            return [*targets, None]
        try:
            with os.scandir(folder) as entries:
                names = [entry.name for _, entry in zip(range(_LISTING_LIMIT + 1), entries)]
        except OSError:
            continue
        if len(names) > _LISTING_LIMIT:
            targets.append(None)
            names = names[:_LISTING_LIMIT]
        following = _next_wildcard(segments, index + 1)
        fixed = segments[index + 1 : following]
        for name in names:
            if not fnmatch.fnmatch(name, segments[index]):
                continue
            if len(targets) >= _EXPANSION_TARGETS:
                return [*targets, None]
            target = _resolved(folder.joinpath(name, *fixed))
            targets.append(target)
            if target is not None and following is not None:
                pending.append((target, following))
    return targets


def argument_targets(value: str, cwd: Path) -> list[Path | None]:
    """Where one argument can lead: itself, and whatever its wildcards stand for. None for a target that cannot be
    known, which every caller must read as "outside the project"."""
    return [resolved_argument(value, cwd), *_wildcard_targets(value, cwd)]


def _argument_targets(command: str, cwd: Path, depth: int = 0) -> Iterator[tuple[str, Path | None]]:
    """Every argument of the command and where it leads (None: not knowable), relative names read against ``cwd``."""
    for token in command_tokens(command):
        for piece in argument_pieces(token):
            for target in argument_targets(piece, cwd):
                yield piece, target
            if depth < _NESTED_DEPTH and any(char.isspace() for char in piece):
                # A command handed to another shell as one quoted argument: ``cmd /c "type x"``, ``powershell -c "gc x"``.
                yield from _argument_targets(piece, cwd, depth + 1)


def protected_argument(command: str, cwd: Path, protected: Iterable[Path]) -> tuple[str, Path] | None:
    """The first argument that leads into a protected location, as ``(the argument, where it leads)``.

    This is what catches a directory junction or symlink inside the project: the text names no protected path, but the
    resolved target is inside one. A path built at run time (a variable, a sub-expression) still cannot be seen; the
    permission policy asks instead of allowing in that case.
    """
    roots = [_real(Path(item)) for item in protected]
    if not roots:
        return None
    for raw, target in _argument_targets(command, cwd):
        if target is not None and any(is_within(target, root) for root in roots):
            return raw, target
    return None


def _real(path: Path) -> Path:
    try:
        return path.resolve()
    except (OSError, ValueError, RuntimeError):
        return Path(os.path.abspath(path))


# --------------------------------------------------------------------------- the tool


def _cwd(ctx: ToolContext, raw: str | None) -> Path:
    if raw is None:
        return ctx.project.resolve()
    path, _ = resolve_path(ctx, raw)
    if ctx.protected and is_protected(ctx, path):
        raise ToolError(f"Access denied: {path} is a protected location (application settings or credentials).")
    if not path.exists():
        raise ToolError(f"cwd does not exist: {path}")
    if not path.is_dir():
        raise ToolError(f"cwd must be a folder, but {path} is a file.")
    return path


async def shell(args: dict[str, Any], ctx: ToolContext) -> ToolResult:
    command: str = args["command"]
    if not command.strip():
        raise ToolError("command is empty.")
    if len(command) > MAX_COMMAND_CHARS:
        raise ToolError(
            f"command is {len(command)} characters; the limit is {MAX_COMMAND_CHARS}. "
            "Write the script to a file with create_file and run that file."
        )
    cwd = _cwd(ctx, args.get("cwd"))
    hit = mentioned_protected(command, ctx.protected)
    if hit is not None:
        raise ToolError(f"Access denied: the command mentions {hit}, a protected location (application settings or credentials).")
    leads = protected_argument(command, cwd, ctx.protected)
    if leads is not None:
        raise ToolError(
            f"Access denied: the argument {leads[0]!r} leads to {leads[1]}, a protected location "
            "(application settings or credentials)."
        )
    timeout = args.get("timeout", DEFAULT_TIMEOUT)
    try:
        output, code, timed_out, aborted = await run_shell(command, cwd, timeout, ctx.abort)
    except OSError as error:
        raise ToolError(f"Could not start the shell: {error}") from None
    lines = [output or "(no output)"]
    if timed_out:
        lines.append(f"Timed out after {timeout} s")
    if aborted:
        lines.append("Stopped by the user")
    lines.append(f"Exit code: {code}")
    return ToolResult("\n".join(lines), is_error=bool(code) or timed_out or aborted)


_DIALECT = (
    "Windows PowerShell 5.1 (not cmd, not bash): chain commands with `;` rather than `&&`, and native programs work as usual."
    if IS_WINDOWS
    else "/bin/sh."
)

SHELL = ToolSpec(
    name="shell",
    description=(
        f"Run one command in {_DIALECT} Returns the combined stdout and stderr, then a final `Exit code: N` line. "
        "Runs in `cwd` (default: the project folder). stdin is closed, so the command must not wait for input. "
        f"Output beyond {MAX_OUTPUT_CHARS:,} characters is cut in the middle. After `timeout` seconds (default "
        f"{DEFAULT_TIMEOUT}, at most {MAX_TIMEOUT}) the command and every process it started are killed. Do not start "
        "servers or watchers that never exit. Prefer read_file, list_dir, glob, grep, create_file and edit_file to shell "
        "equivalents."
    ),
    parameters={
        "type": "object",
        "properties": {
            "command": {"type": "string", "description": "The command line (or small script) to run."},
            "cwd": {
                "type": "string",
                "description": "Existing folder to run in; absolute, or relative to the project folder (default: the project folder).",
            },
            "timeout": {
                "type": "integer",
                "minimum": 1,
                "maximum": MAX_TIMEOUT,
                "description": f"Seconds before the command is killed (default {DEFAULT_TIMEOUT}, max {MAX_TIMEOUT}).",
            },
        },
        "required": ["command"],
    },
    ui_name="Execute",
    kind="exec",
)


def register(registry: ToolRegistry) -> None:
    registry.register(
        SHELL,
        shell,
        ui_input=passthrough_input({"command": "command", "cwd": "cwd", "timeout": "timeout"}),
        is_async=True,
    )
