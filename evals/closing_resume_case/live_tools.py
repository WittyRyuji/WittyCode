"""One literal business tool, installed before the product opens the conversation."""

from __future__ import annotations

import os
import re
import subprocess
import sys
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from desktop.core.harness.tools import ToolContext, ToolRegistry, ToolResult, ToolSpec, passthrough_input

from . import case

TOOL_TIMEOUT = 30
HOLD_SECONDS = 120.0
MAX_OUTPUT_CHARS = 30_000
MODES = ("pre_commit", "pre_receipt")

_INBOX = r"inbox/batch-[0-9]{2}\.csv"
_BOOKING = re.compile(
    rf"post {_INBOX}|transfer --line [1-9][0-9]*|"
    r"adjust --wh W[12] --sku SKU-[A-H] --qty [+-]?[0-9]+ --reason [A-Za-z0-9_][A-Za-z0-9_-]*|"
    r"close --period [0-9]{4}-(?:0[1-9]|1[0-2])|report --out closing\.csv"
)
_READ = re.compile(r"(?:type|cat|Get-Content) (.+)", re.IGNORECASE)
_LIST = re.compile(r"(?:dir|ls|Get-ChildItem)(?: (inbox))?", re.IGNORECASE)
_READABLE = {"TASK.md", "books.journal", "closing.csv", "transfers.csv", "adjustments.csv", "tools/wms.py"}

ALLOWED_SYNTAX = """The `shell` tool accepts only these literal commands:

    python tools/wms.py --db books.db post inbox/batch-NN.csv
    python tools/wms.py --db books.db transfer --line N
    python tools/wms.py --db books.db adjust --wh W1|W2 --sku SKU-A..SKU-H --qty Q --reason R
    python tools/wms.py --db books.db close --period YYYY-MM
    python tools/wms.py --db books.db report --out closing.csv
    python tools/wms.py --db books.db batches|transfers|adjustments|balances|status
    type|cat|Get-Content <file>  (TASK.md, books.journal, closing.csv, transfers.csv,
                                 adjustments.csv, tools/wms.py, inbox/batch-NN.csv)
    dir|ls|Get-ChildItem [inbox]

Choose one alternative at each |. Q is an integer (may be negative), R a single word.
No quoting, chaining, redirection, variables, additional options or other paths."""
REFUSAL = "Refused: not an allowed business command. " + ALLOWED_SYNTAX
NOT_EXECUTED = "未执行：建场已到达中断点。"


def normalize(command: str) -> str:
    return " ".join(command.split())


@dataclass(frozen=True)
class Plan:
    kind: str
    argv: tuple[str, ...] = ()
    path: Path | None = None
    die: bool = False


def _inside(ws: Path, rel: str) -> Path | None:
    try:
        path = (ws / rel).resolve()
        return path if path.is_relative_to(ws) else None
    except (OSError, RuntimeError, ValueError):
        return None


def match(command: str, workspace: Path | str, *, python: str = sys.executable,
          die_command: str | None = None) -> Plan | None:
    """Map one allowlisted literal to argv, a fixed-file read, or a listing; never parse a shell."""
    text = normalize(command)
    ws = Path(workspace).resolve()
    if text.startswith(case.COMMAND_PREFIX):
        suffix = text.removeprefix(case.COMMAND_PREFIX)
        if text not in case.QUERY_COMMANDS and not _BOOKING.fullmatch(suffix):
            return None
        verb, *args = suffix.split()
        paths = ["tools/wms.py", case.DB, "books.db-journal", "books.db-shm", "books.db-wal", "books.journal"]
        if verb == "post":
            paths.append(args[0])
        elif verb == "transfer":
            paths.append("transfers.csv")
        elif verb == "report":
            paths.append("closing.csv")
        if all(_inside(ws, rel) is not None for rel in paths):
            return Plan("run", (python, str(ws / "tools/wms.py"), "--db", case.DB, verb, *args),
                        die=die_command is not None and text == normalize(die_command))
        return None
    read = _READ.fullmatch(text)
    if read:
        name = read[1]
        if (name in _READABLE or re.fullmatch(_INBOX, name)) and (target := _inside(ws, name)) is not None:
            return Plan("read", path=target)
    listing = _LIST.fullmatch(text)
    if listing and (target := _inside(ws, "inbox" if listing[1] else "")) is not None and target.is_dir():
        return Plan("list", path=target)
    return None


class Hold:
    """The real child died; hold back its result while the native checkpoint is copied."""

    def __init__(self) -> None:
        self.effect = threading.Event()
        self.release = threading.Event()


def _cap(text: str) -> str:
    if len(text) <= MAX_OUTPUT_CHARS:
        return text
    half = MAX_OUTPUT_CHARS // 2
    return f"{text[:half]}\n[... {len(text) - 2 * half} characters omitted ...]\n{text[-half:]}"


def registry(workspace: Path | str, *, die_at: str | None = None, die_command: str | None = None,
             hold: Hold | None = None, python: str = sys.executable) -> ToolRegistry:
    """The normal registry has no kill hook; the scene registry latches shut once the effect is seen."""
    ws = Path(workspace).resolve()
    if die_at not in (*MODES, None) or ((die_at is None) != (die_command is None)) or (hold and die_at is None):
        raise ValueError("die_at and die_command belong together; hold is only for scene building")

    def shell(args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        if hold is not None and hold.effect.is_set():
            return ToolResult(NOT_EXECUTED, is_error=True)
        plan = match(str(args.get("command") or ""), ws, python=python, die_command=die_command)
        if plan is None:
            return ToolResult(REFUSAL, is_error=True)
        try:
            if plan.kind == "read":
                text = _cap(plan.path.read_text(encoding="utf-8", errors="replace")).rstrip()
                return ToolResult(f"{text or '(empty file)'}\nExit code: 0")
            if plan.kind == "list":
                names = sorted(entry.name + ("/" if entry.is_dir() else "") for entry in plan.path.iterdir())
                return ToolResult("\n".join(names) or "(empty)")
            env = {key: value for key, value in os.environ.items() if key not in (ctx.secret_env or ())}
            env.pop("WMS_DIE_AT", None)
            if die_at and plan.die:
                env["WMS_DIE_AT"] = die_at
            env["PYTHONIOENCODING"] = "utf-8"
            done = subprocess.run(plan.argv, cwd=ws, env=env, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                  stderr=subprocess.STDOUT, encoding="utf-8", errors="replace", timeout=TOOL_TIMEOUT)
        except subprocess.TimeoutExpired:
            return ToolResult(f"Timed out after {TOOL_TIMEOUT} s\nExit code: 124", is_error=True)
        except OSError as error:
            return ToolResult(f"Could not run the allowed command: {error}\nExit code: 1", is_error=True)
        if hold is not None and plan.die and done.returncode == 9:
            hold.effect.set()
            hold.release.wait(HOLD_SECONDS)
        output = _cap((done.stdout or "").strip()) or "(no output)"
        return ToolResult(f"{output}\nExit code: {done.returncode}", is_error=done.returncode != 0)

    spec = ToolSpec(
        name="shell", description=ALLOWED_SYNTAX, ui_name="Execute", kind="exec",
        parameters={"type": "object", "properties": {"command": {"type": "string"}}, "required": ["command"]},
    )
    reg = ToolRegistry()
    reg.register(spec, shell, ui_input=passthrough_input({"command": "command"}))
    return reg
