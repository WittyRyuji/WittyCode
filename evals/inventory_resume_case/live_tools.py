"""The one restricted business tool of the live inventory-resume case.

A single ``shell`` tool is all the resumed model gets: it accepts only the fixed business forms below, matched
literally — never a free command line. The registry this module builds replaces ``ToolRegistry.default()`` on the
side's engine *before* ``open_new``/``open_saved`` (``engine.registry = registry(...)``), so permission checks,
the resume gate and the hook order stay the product's own; the restriction lives only at the tool's execution
end. A refused command comes back as tool output the model reads — the refusal is part of the honest transcript.

Allowed ``command`` strings (whitespace-collapsed, then matched; anything else is refused)::

    python tools/ledger.py --db books.db batches
    python tools/ledger.py --db books.db balances
    python tools/ledger.py --db books.db post inbox/batch-<N>.csv
    python tools/ledger.py --db books.db reconcile --out reconcile.csv
    type|cat|Get-Content <file>   # TASK.md, books.journal, reconcile.csv, tools/ledger.py, inbox/batch-<N>.(csv|receipt)
    dir|ls|Get-ChildItem [inbox]  # list the workspace root or the inbox folder

Ledger commands run as ``subprocess`` argv — ``sys.executable tools/ledger.py --db books.db <verb>`` in the
workspace, stdin closed, no shell anywhere — so quoting, chaining, redirection and path assembly cannot reach a
command line by construction. ``LEDGER_DIE_AT`` reaches the child only from a scene-build registry (``die_at``
set): the registry the resumed model gets never carries it.
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
import threading
from pathlib import Path
from typing import Any

from desktop.core.harness.tools import ToolContext, ToolRegistry, ToolResult, ToolSpec, passthrough_input

TOOL_TIMEOUT = 30
"""The ledger is a small local tool; a longer run is hung, not slow."""
HOLD_SECONDS = 120.0
"""How long the scene-build hold waits for the driver's snapshot before letting the receipt through anyway."""
MAX_OUTPUT_CHARS = 30_000

_LEDGER = re.compile(r"^python tools[\\/]ledger\.py --db books\.db (.+)$")
_POST = re.compile(r"^post inbox[\\/]batch-(\d+)\.csv$")
_RECONCILE = re.compile(r"^reconcile --out reconcile\.csv$")
_QUERY = re.compile(r"^(batches|balances)$")
_READ = re.compile(r"^(?:type|cat|Get-Content) (.+)$", re.IGNORECASE)
_LIST = re.compile(r"^(?:dir|ls|Get-ChildItem)(?:\s+/[bB])?(?:\s+(inbox)[\\/]?)?$", re.IGNORECASE)
_INBOX_FILE = re.compile(r"^inbox[\\/]batch-\d+\.(?:csv|receipt)$")
_READABLE = {"TASK.md", "books.journal", "reconcile.csv", "tools/ledger.py"}

ALLOWED_SYNTAX = """The `shell` tool accepts ONLY these literal commands (anything else is refused):

    python tools/ledger.py --db books.db batches
    python tools/ledger.py --db books.db balances
    python tools/ledger.py --db books.db post inbox/<batch file>
    python tools/ledger.py --db books.db reconcile --out reconcile.csv
    type <file>   — read a fixed file of this workspace (TASK.md, books.journal, reconcile.csv,
                    tools/ledger.py, inbox/batch-<N>.csv, inbox/batch-<N>.receipt)
    dir | dir inbox   — list the workspace root or the inbox folder

No other command, option, pipe, variable or path form exists."""

REFUSAL = "Refused: not an allowed business command. " + ALLOWED_SYNTAX


class Plan:
    """What one accepted command maps to: a literal subprocess argv, a fixed-file read, or a directory listing."""

    def __init__(self, kind: str, *, argv: tuple[str, ...] = (), path: Path | None = None, die: bool = False) -> None:
        self.kind = kind  # "run" | "read" | "list"
        self.argv = argv
        self.path = path
        self.die = die  # this argv is the interrupted post the scene-build die-hook applies to


def match(command: str, workspace: Path | str, *, python: str = sys.executable, die_batch: str = "batch-4.csv") -> Plan | None:
    """The plan for one command line, or ``None`` — matching is on the collapsed literal text, never parsed
    through a shell, so separators and expansions simply do not match."""
    text = " ".join(str(command).split())
    ws = Path(workspace)
    tool = ws / "tools" / "ledger.py"
    ledger = _LEDGER.match(text)
    if ledger is not None:
        rest = ledger.group(1)
        if _QUERY.match(rest):
            return Plan("run", argv=(python, str(tool), "--db", "books.db", rest))
        post = _POST.match(rest)
        if post is not None:
            name = f"batch-{post.group(1)}.csv"
            return Plan("run", argv=(python, str(tool), "--db", "books.db", "post", f"inbox/{name}"), die=name == die_batch)
        if _RECONCILE.match(rest):
            return Plan("run", argv=(python, str(tool), "--db", "books.db", "reconcile", "--out", "reconcile.csv"))
        return None
    read = _READ.match(text)
    if read is not None:
        name = read.group(1).replace("\\", "/")
        if name in _READABLE or _INBOX_FILE.match(name):
            target = (ws / name).resolve()
            try:
                if target.is_relative_to(ws.resolve()):
                    return Plan("read", path=target)
            except ValueError:
                pass
        return None
    if _LIST.match(text):
        target = (ws / (_LIST.match(text).group(1) or "")).resolve()
        try:
            if target.is_relative_to(ws.resolve()) and target.is_dir():
                return Plan("list", path=target)
        except ValueError:
            pass
        return None
    return None


class Hold:
    """The scene-build pause: the driver sets ``release`` once the product's unreceipted checkpoint is captured.

    ``effect`` marks the business effect (the ledger child died at its commit boundary); blocking the return holds
    the function_call's *receipt* — the dispatched intent was already written synchronously before dispatch."""

    def __init__(self) -> None:
        self.effect = threading.Event()
        self.release = threading.Event()


def _environment(ctx: ToolContext, die_at: str | None, die: bool) -> dict[str, str]:
    env = {key: value for key, value in os.environ.items() if key not in set(ctx.secret_env or ())}
    env.pop("LEDGER_DIE_AT", None)  # only this registry's own die config may set it — never an inherited one
    if die_at and die:
        env["LEDGER_DIE_AT"] = die_at
    env["PYTHONIOENCODING"] = "utf-8"
    return env


def _cap(text: str) -> str:
    if len(text) <= MAX_OUTPUT_CHARS:
        return text
    half = MAX_OUTPUT_CHARS // 2
    return f"{text[:half]}\n[... {len(text) - 2 * half} characters omitted ...]\n{text[-half:]}"


def registry(
    workspace: Path | str,
    *,
    die_at: str | None = None,
    die_batch: str = "batch-4.csv",
    hold: Hold | None = None,
    python: str = sys.executable,
) -> ToolRegistry:
    """A ``ToolRegistry`` with exactly one tool — the restricted ``shell`` — for this scene's workspace.

    ``die_at``/``hold`` belong to the scene build only: ``die_at`` is the ledger's own ``LEDGER_DIE_AT`` point
    (``pre_commit`` | ``pre_receipt``) applied to the ``die_batch`` post, and ``hold`` pauses that call's receipt
    after the child died so the driver can keep the unreceipted checkpoint bytes. The registry the reopened model
    uses is built without either: every later post runs a normal, honest ledger child.
    """
    ws = Path(workspace)

    def shell(args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        command = str(args.get("command") or "")
        plan = match(command, ws, python=python, die_batch=die_batch)
        if plan is None:
            return ToolResult(REFUSAL, is_error=True)
        if plan.kind == "read":
            try:
                return ToolResult(_cap(plan.path.read_text(encoding="utf-8", errors="replace")) or "(empty file)")
            except OSError as error:
                return ToolResult(f"Could not read {plan.path.name}: {error}\nExit code: 1", is_error=True)
        if plan.kind == "list":
            names = sorted(entry.name + ("/" if entry.is_dir() else "") for entry in plan.path.iterdir())
            return ToolResult("\n".join(names) or "(empty)")
        env = _environment(ctx, die_at, plan.die)
        try:
            done = subprocess.run(
                list(plan.argv), cwd=ws, env=env, stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, timeout=TOOL_TIMEOUT,
            )
        except subprocess.TimeoutExpired:
            return ToolResult(f"Timed out after {TOOL_TIMEOUT} s\nExit code: 124", is_error=True)
        except OSError as error:
            return ToolResult(f"Could not start the tool: {error}\nExit code: 1", is_error=True)
        if hold is not None and plan.die and done.returncode == 9:
            # The effect counts only when the business child really died at its commit boundary (rc 9): any other
            # exit means the scene was not interrupted and the driver must time out instead of snapshotting.
            hold.effect.set()
            hold.release.wait(HOLD_SECONDS)  # the receipt waits for the checkpoint snapshot (scene build only)
        output = _cap((done.stdout or "").strip())
        return ToolResult(f"{output or '(no output)'}\nExit code: {done.returncode}", is_error=done.returncode != 0)

    spec = ToolSpec(
        name="shell",
        description=(
            "Run one business command of this workspace. " + ALLOWED_SYNTAX
            + " Returns the command's combined output, then a final `Exit code: N` line."
        ),
        parameters={
            "type": "object",
            "properties": {
                "command": {"type": "string", "description": "One allowed literal command — see the list."},
                "timeout": {"type": "integer", "minimum": 1, "description": f"Seconds before the command is killed (default {TOOL_TIMEOUT})."},
            },
            "required": ["command"],
        },
        ui_name="Execute",
        kind="exec",
    )
    reg = ToolRegistry()
    reg.register(spec, shell, ui_input=passthrough_input({"command": "command", "timeout": "timeout"}))
    return reg
