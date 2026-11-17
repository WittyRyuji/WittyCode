"""The tasks: what one is, where they are kept, and how a task becomes a project folder and a verdict.

A task is a folder ``tasks/<source>/<name>`` holding ``task.json``::

    {"id": "ex-bowling", "title": "...", "prompt": ["line", "line"], "check": ["python", "-m", "unittest", "-q"],
     "protect": ["bowling_test.py"], "budget": 200000, "steps": 30, "seconds": 900}

and any of: ``workspace/`` (the project's files as the agent finds them), ``solution/`` (files laid over the workspace by
``verify``: a reference solution), ``task.py`` (``build(root)`` writes files that cannot be kept as they are in a
repository whose text files have their line ends normalised, ``solve(root)`` is a reference solution in code,
``check(root, run)`` returns what is wrong or ``""``, for a check that is more than one command).

``prepare`` makes the folder a Git repository of its own with one commit. That is what keeps a task apart from wherever
its folder stands: the loop looks for ``AGENTS.md`` files and skills from the project folder up to the repository's
root, and a run's folder is inside this repository. It is also how the changes are read afterwards (``changed``).

``protect`` names the files and folders the agent must leave alone (the tests, a checking script). They are taken back
to what they were before the check runs, and files added inside a protected folder are removed, so a check cannot be
passed by editing it.
"""

from __future__ import annotations

import importlib.util
import json
import os
import shutil
import subprocess
import sys
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from types import ModuleType
from typing import Any

ROOT = Path(__file__).resolve().parent / "tasks"
SUITES_FILE = "suites.json"
TASK_FILE = "task.json"
HOOKS_FILE = "task.py"
WORKSPACE = "workspace"
SOLUTION = "solution"
CHECK_SECONDS = 120.0
DETAIL_LIMIT = 1500
"""Characters of a failed check's output kept in the result (its end, where a test runner says what failed)."""
GIT_SECONDS = 60.0
_IDENTITY = ("-c", "user.name=WittyCode Eval", "-c", "user.email=eval@example.com")
_EXCLUDED = "__pycache__/\n*.pyc\n"
_CREATE_NO_WINDOW = 0x08000000 if sys.platform == "win32" else 0

Run = Callable[..., tuple[int, str]]
"""``run(argv, timeout=...)``: a command in the task's folder, its exit code and its output."""


class TaskError(Exception):
    """A task or a suite is not what this module can read; the message says which and why."""


@dataclass(frozen=True)
class Task:
    id: str
    title: str
    source: str
    """The folder the task is kept under: ``exercism`` (taken from a public set) or ``probes`` (written for this harness)."""
    folder: Path
    prompt: str
    """The whole message the agent gets."""
    check: tuple[str, ...] = ()
    """The command that passes when the work is done; ``python`` is the interpreter that runs the evaluation."""
    protect: tuple[str, ...] = ()
    budget: int | None = None
    """Tokens the task may use; ``None``: what the run gives every task."""
    steps: int | None = None
    seconds: float | None = None
    hooks: ModuleType | None = field(default=None, compare=False, repr=False)


@dataclass(frozen=True)
class Verdict:
    ok: bool
    detail: str
    seconds: float


def _hooks(file: Path, task_id: str) -> ModuleType:
    spec = importlib.util.spec_from_file_location("evals_task_" + task_id.replace("-", "_"), file)
    if spec is None or spec.loader is None:
        raise TaskError(f"{file}: cannot be loaded")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _count(raw: dict[str, Any], key: str, file: Path) -> int | None:
    value = raw.get(key)
    if value is None:
        return None
    if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
        raise TaskError(f"{file}: {key!r} must be a positive whole number")
    return value


def _names(names: Any, key: str, file: Path, *, paths: bool) -> tuple[str, ...]:
    if not isinstance(names, list) or not all(isinstance(name, str) and name for name in names):
        raise TaskError(f"{file}: {key!r} must be a list of words")
    for name in names if paths else ():
        path = Path(name)
        if path.is_absolute() or ".." in path.parts:
            raise TaskError(f"{file}: {key!r} names a path outside the task ({name!r})")
    return tuple(names)


def load(folder: Path) -> Task:
    file = folder / TASK_FILE
    try:
        raw = json.loads(file.read_text(encoding="utf-8"))
    except (OSError, ValueError) as error:
        raise TaskError(f"{file}: {error}") from error
    if not isinstance(raw, dict):
        raise TaskError(f"{file}: must hold an object")
    task_id, title, prompt = raw.get("id"), raw.get("title"), raw.get("prompt")
    if not isinstance(task_id, str) or not task_id or task_id != task_id.lower() or not all(c.isalnum() or c == "-" for c in task_id):
        raise TaskError(f"{file}: 'id' must be lower-case letters, digits and hyphens")
    if isinstance(prompt, list) and all(isinstance(line, str) for line in prompt):
        prompt = "\n".join(prompt)
    if not isinstance(title, str) or not title.strip() or not isinstance(prompt, str) or not prompt.strip():
        raise TaskError(f"{file}: 'title' and 'prompt' are required")
    hooks = _hooks(folder / HOOKS_FILE, task_id) if (folder / HOOKS_FILE).is_file() else None
    check = _names(raw["check"], "check", file, paths=False) if raw.get("check") else ()
    if not check and not hasattr(hooks, "check"):
        raise TaskError(f"{file}: needs a 'check' command or a check() in {HOOKS_FILE}")
    if not (folder / WORKSPACE).is_dir() and not hasattr(hooks, "build"):
        raise TaskError(f"{file}: needs a {WORKSPACE}/ folder or a build() in {HOOKS_FILE}")
    return Task(
        id=task_id,
        title=title.strip(),
        source=folder.parent.name,
        folder=folder,
        prompt=prompt.strip(),
        check=check,
        protect=_names(raw.get("protect", []), "protect", file, paths=True),
        budget=_count(raw, "budget", file),
        steps=_count(raw, "steps", file),
        seconds=_count(raw, "seconds", file),
        hooks=hooks,
    )


def all_tasks(root: Path = ROOT) -> dict[str, Task]:
    """Every task below ``root``, by id, in the order of their folders."""
    tasks: dict[str, Task] = {}
    for file in sorted(root.glob(f"*/*/{TASK_FILE}")):
        task = load(file.parent)
        if task.id in tasks:
            raise TaskError(f"{file}: the id {task.id!r} is also that of {tasks[task.id].folder}")
        tasks[task.id] = task
    return tasks


def suites(root: Path = ROOT) -> dict[str, tuple[str, ...]]:
    """The named selections of ``suites.json``, and ``all``."""
    tasks = all_tasks(root)
    try:
        raw = json.loads((root / SUITES_FILE).read_text(encoding="utf-8"))
    except (OSError, ValueError) as error:
        raise TaskError(f"{root / SUITES_FILE}: {error}") from error
    found: dict[str, tuple[str, ...]] = {}
    for name, ids in raw.items():
        if not isinstance(ids, list) or not ids or len(set(ids)) != len(ids) or any(task_id not in tasks for task_id in ids):
            raise TaskError(f"{root / SUITES_FILE}: the suite {name!r} must list existing tasks, each once")
        found[name] = tuple(ids)
    found["all"] = tuple(tasks)
    return found


def select(suite: str = "", ids: Sequence[str] = (), root: Path = ROOT) -> list[Task]:
    """The tasks of a suite, or the named ones (in the order given)."""
    tasks = all_tasks(root)
    if ids:
        missing = [task_id for task_id in ids if task_id not in tasks]
        if missing:
            raise TaskError(f"no such task: {', '.join(missing)}")
        return [tasks[task_id] for task_id in dict.fromkeys(ids)]
    named = suites(root)
    if suite not in named:
        raise TaskError(f"no such suite: {suite!r} (there are: {', '.join(named)})")
    return [tasks[task_id] for task_id in named[suite]]


# ---------------------------------------------------------------------- the project folder


def _git(root: Path, *args: str) -> str:
    """Git in ``root`` with nobody's configuration: no hooks or templates of the machine, no signing, no line-end
    conversion, and the identity the arguments give."""
    env = {**os.environ, "GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_SYSTEM": os.devnull, "GIT_CONFIG_NOSYSTEM": "1", "GIT_TERMINAL_PROMPT": "0"}
    try:
        done = subprocess.run(["git", *args], cwd=root, env=env, capture_output=True, timeout=GIT_SECONDS, creationflags=_CREATE_NO_WINDOW)
    except (OSError, subprocess.TimeoutExpired) as error:
        raise TaskError(f"git {args[0]} could not run in {root}: {error}") from error
    if done.returncode:
        raise TaskError(f"git {args[0]} failed in {root}: {done.stderr.decode('utf-8', 'replace').strip()[:300]}")
    return done.stdout.decode("utf-8", "replace")


def prepare(task: Task, root: Path) -> Path:
    """The task as a fresh project folder at ``root`` (which must not exist yet)."""
    if root.exists():
        raise TaskError(f"{root} already exists")
    static = task.folder / WORKSPACE
    if static.is_dir():
        # (the files get the time of the copy, not their own: Git takes a file as large and as old as the one it knows
        # for unchanged, and a later copy that keeps a file's time must not pass for no change)
        shutil.copytree(static, root, ignore=shutil.ignore_patterns("__pycache__", "*.pyc"), copy_function=shutil.copyfile)
    else:
        root.mkdir(parents=True)
    if hasattr(task.hooks, "build"):
        task.hooks.build(root)
    _git(root, "init", "--quiet", "--template=")
    info = root / ".git" / "info"
    info.mkdir(exist_ok=True)
    (info / "exclude").write_text(_EXCLUDED, encoding="utf-8")
    _git(root, "add", "--all")
    _git(root, *_IDENTITY, "commit", "--quiet", "--message", "The task as given")
    return root


def changed(root: Path) -> list[str]:
    """The files that are not as the task gave them (changed, added or deleted), relative to ``root``."""
    listed = _git(root, "status", "--porcelain", "-z", "--untracked-files=all", "--no-renames")
    return sorted(entry[3:].replace("\\", "/") for entry in listed.split("\0") if len(entry) > 3)


def _files(root: Path, name: str) -> list[Path]:
    path = root / name
    if path.is_dir():
        return sorted(p for p in path.rglob("*") if p.is_file() and "__pycache__" not in p.parts)
    return [path] if path.is_file() else []


def snapshot(task: Task, root: Path) -> dict[str, bytes]:
    """What the protected files hold now (before the agent has touched the folder), by path relative to ``root``."""
    kept: dict[str, bytes] = {}
    for name in task.protect:
        files = _files(root, name)
        if not files:
            raise TaskError(f"{task.id}: the protected {name!r} is not in the workspace")
        for file in files:
            kept[file.relative_to(root).as_posix()] = file.read_bytes()
    return kept


def restore(task: Task, root: Path, kept: dict[str, bytes]) -> list[str]:
    """Take the protected files back to ``kept``; the names of those that had been changed, removed or added."""
    undone: list[str] = []
    for name in task.protect:
        for file in _files(root, name):
            relative = file.relative_to(root).as_posix()
            if relative not in kept:
                file.unlink()
                undone.append(relative)
    for relative, data in kept.items():
        file = root / relative
        if not file.is_file() or file.read_bytes() != data:
            file.parent.mkdir(parents=True, exist_ok=True)
            file.write_bytes(data)
            undone.append(relative)
    return sorted(undone)


def run_command(root: Path, argv: Sequence[str], timeout: float = CHECK_SECONDS) -> tuple[int, str]:
    """A command in ``root``: its exit code (``-1``: it could not start or did not end in time) and what it printed."""
    command = [sys.executable, "-B", *argv[1:]] if argv and argv[0] == "python" else list(argv)
    env = {**os.environ, "PYTHONDONTWRITEBYTECODE": "1", "PYTHONIOENCODING": "utf-8", "PYTHONUTF8": "1"}
    try:
        done = subprocess.run(
            command, cwd=root, env=env, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            timeout=timeout, creationflags=_CREATE_NO_WINDOW,
        )
    except subprocess.TimeoutExpired:
        return -1, f"The check did not end within {timeout:.0f} s."
    except OSError as error:
        return -1, f"The check could not start: {error}"
    return done.returncode, done.stdout.decode("utf-8", "replace").replace("\r\n", "\n")


def _short(text: str) -> str:
    text = text.strip()
    return text if len(text) <= DETAIL_LIMIT else "[...]\n" + text[-DETAIL_LIMIT:]


def check(task: Task, root: Path, kept: dict[str, bytes]) -> Verdict:
    """Whether the work is done. The protected files are put back first, and the verdict says which had been changed
    (also when the check then passes): a task solved by changing its tests was not solved."""
    undone = restore(task, root, kept)
    note = f"Protected files were changed and put back before the check: {', '.join(undone)}.\n" if undone else ""
    started = time.monotonic()

    def run(argv: Sequence[str], timeout: float = CHECK_SECONDS) -> tuple[int, str]:
        return run_command(root, argv, timeout)

    try:
        if hasattr(task.hooks, "check"):
            problem = str(task.hooks.check(root, run) or "")
        else:
            code, output = run(task.check)
            problem = "" if code == 0 else (output.strip() or f"exit code {code}")
    except Exception as error:  # (a check that cannot read what the agent left is a failed check, not a failed run)
        problem = f"The check itself failed: {type(error).__name__}: {error}"
    return Verdict(not problem, (note + _short(problem)).strip(), time.monotonic() - started)


def solve(task: Task, root: Path) -> None:
    """Apply the task's reference solution to its folder."""
    overlay = task.folder / SOLUTION
    if not overlay.is_dir() and not hasattr(task.hooks, "solve"):
        raise TaskError(f"{task.id}: has neither {SOLUTION}/ nor a solve() in {HOOKS_FILE}")
    if overlay.is_dir():
        shutil.copytree(overlay, root, dirs_exist_ok=True)
    if hasattr(task.hooks, "solve"):
        task.hooks.solve(root)


def verify(task: Task, folder: Path) -> list[str]:
    """What is wrong with the task itself, found without any model: the check must fail on the task as given and pass
    once the reference solution is applied (which must not need the protected files changed)."""
    problems: list[str] = []
    root = prepare(task, folder / task.id)
    kept = snapshot(task, root)
    before = check(task, root, kept)
    if before.ok:
        problems.append("the check passes before any work is done")
    solve(task, root)
    after = check(task, root, kept)
    if not after.ok:
        problems.append(f"the check fails on the reference solution: {after.detail}")
    return problems
