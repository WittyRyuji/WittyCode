"""``python -m evals``: list the tasks, check them without a model, give them to the loop, compare two runs."""

from __future__ import annotations

import argparse
import asyncio
import re
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Any

from evals import report, runner, taskset
from evals.metrics import Price

REPO = Path(__file__).resolve().parents[1]
RUNS = REPO / "sandbox" / "eval" / "runs"
PROFILE = REPO / "sandbox" / "eval" / "profile"
"""Where a run looks for a ChatGPT sign-in unless told otherwise: the profile of a window started with
``python -m desktop --settings sandbox/eval/profile/settings.json``, never the user's own."""
SUMMARY_FILE = "summary.md"


def tokens(text: str) -> int:
    """``1000000``, ``1_000_000``, ``500k`` or ``1.5m``."""
    found = re.fullmatch(r"([0-9][0-9_]*(?:\.[0-9]+)?)([kKmM]?)", text.strip())
    if not found:
        raise argparse.ArgumentTypeError(f"not a number of tokens: {text!r}")
    return int(float(found.group(1).replace("_", "")) * {"": 1, "k": 1_000, "m": 1_000_000}[found.group(2).lower()])


def model_id(text: str) -> str:
    """The engine's id of a model named as the page names it (``chatgpt:gpt-...``) or as the engine does."""
    return text.strip().removeprefix("chatgpt:")


def price(text: str) -> Price:
    """``read=0.1,write=1.25,output=5``."""
    try:
        return Price(**{name.strip(): float(value) for name, _, value in (part.partition("=") for part in text.split(","))})
    except (TypeError, ValueError) as error:
        raise argparse.ArgumentTypeError(f"a price is read=...,write=...,output=... ({error})") from error


def settings_from(pairs: list[str]) -> tuple[Any, dict[str, Any]]:
    """The application's default settings with ``name=value`` changes (a number, ``true`` / ``false`` or a word, as the
    setting is), and the changes."""
    from desktop.core.settings import AppSettings

    settings, changes = AppSettings(), {}
    for pair in pairs:
        name, _, text = pair.partition("=")
        current = getattr(settings, name.strip(), None)
        if isinstance(current, bool):
            value: Any = text.strip().lower() in ("1", "true", "yes", "on")
        elif isinstance(current, int) and text.strip().lstrip("-").isdigit():
            value = int(text)
        elif isinstance(current, str):
            value = text
        else:
            raise ValueError(f"--set {pair}: no such setting, or its value does not fit")
        setattr(settings, name.strip(), value)
        changes[name.strip()] = value
    return settings, changes


def _git(*args: str) -> str:
    try:
        done = subprocess.run(["git", *args], cwd=REPO, capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.TimeoutExpired):
        return ""
    return done.stdout.strip() if done.returncode == 0 else ""


def _list() -> int:
    tasks = taskset.all_tasks()
    print("suites: " + ", ".join(f"{name} ({len(ids)})" for name, ids in taskset.suites().items()))
    for task in tasks.values():
        print(f"{task.id:22} {task.source:9} {task.title}")
    return 0


def _verify(options: argparse.Namespace) -> int:
    failed = 0
    with tempfile.TemporaryDirectory(prefix="wittycode-evals-") as folder:
        for task in taskset.select(options.suite, options.task):
            problems = taskset.verify(task, Path(folder))
            failed += bool(problems)
            print(f"{task.id:22} {'ok' if not problems else 'PROBLEM: ' + '; '.join(problems)}", flush=True)
    return 1 if failed else 0


def _run(options: argparse.Namespace) -> int:
    tasks = taskset.select(options.suite, options.task)
    model = model_id(options.model)
    try:
        settings, changes = settings_from(options.set)
        chosen = runner.Options(
            model=model, reasoning=options.reasoning, autonomy=options.autonomy, budget=options.budget, task_budget=options.task_budget,
            steps=options.steps, seconds=options.seconds, price=options.price,
        )
    except ValueError as error:
        print(f"error: {error}", file=sys.stderr)
        return 2
    label = re.sub(r"[^A-Za-z0-9._-]+", "-", f"{model}-{options.name}" if options.name else model).strip("-")
    out = Path(options.out) / f"{time.strftime('%Y%m%d-%H%M%S')}-{label}"
    about = {
        "suite": "custom" if options.task else options.suite,
        "commit": _git("rev-parse", "--short", "HEAD") or "?",
        "dirty": bool(_git("status", "--porcelain")),
        "settings": changes,
        "python": sys.version.split()[0],
    }

    async def go() -> int:
        engine = runner.real_engine(model, settings, account=Path(options.account), models=Path(options.models) if options.models else None, sessions=out / "sessions")
        problem = await runner.preflight(engine, model, options.reasoning)
        if problem:
            print(f"error: {problem}", file=sys.stderr)
            return 2
        print(f"{len(tasks)} tasks, model {model}, budget {chosen.budget:,} tokens; writing to {out}", flush=True)
        try:
            await runner.run_tasks(engine, tasks, chosen, out, about=about, progress=lambda line: print(line, flush=True))
        finally:
            await engine.close_all()
        return 0

    code = asyncio.run(go())
    if code == 0:
        text = report.table(report.load(out))
        (out / SUMMARY_FILE).write_text(text, encoding="utf-8")
        print("\n" + text + f"\n{out / SUMMARY_FILE}")
    return code


def _compare(options: argparse.Namespace) -> int:
    text = report.compare(report.load(options.before), report.load(options.after))
    if options.out:
        Path(options.out).write_text(text, encoding="utf-8")
    print(text)
    return 0


def parser() -> argparse.ArgumentParser:
    root = argparse.ArgumentParser(prog="python -m evals", description="A small task set for measuring WittyCode's own agent loop.")
    commands = root.add_subparsers(dest="command", required=True)
    commands.add_parser("list", help="the tasks and the suites")
    verify = commands.add_parser("verify", help="check the tasks themselves, without a model: each check fails as given and passes on the reference solution")
    run = commands.add_parser("run", help="give the tasks to the loop (calls the model; stops at the token budget)")
    for command, suite in ((verify, "all"), (run, "core")):
        command.add_argument("--suite", default=suite, help=f"which tasks (default: {suite}); see list")
        command.add_argument("--task", action="append", default=[], help="a task by id instead of a suite (may be given several times)")
    run.add_argument("--model", required=True, help="a model of the ChatGPT plan (gpt-...) or of an API provider (api:<provider>:<id>)")
    run.add_argument("--reasoning", default="", help="the reasoning effort (default: the model's own)")
    run.add_argument("--autonomy", default="code", choices=runner.AUTONOMIES, help="the permission level of the conversations (default: code, the application's)")
    run.add_argument("--budget", type=tokens, default=runner.DEFAULT_BUDGET, help=f"tokens the whole run may use (default 1m, at most {runner.HARD_BUDGET // 1_000_000}m)")
    run.add_argument("--task-budget", type=tokens, default=runner.DEFAULT_TASK_BUDGET, help="tokens one task may use unless it says otherwise (default 200k)")
    run.add_argument("--steps", type=int, default=runner.DEFAULT_STEPS, help=f"requests one task may make (default 30, at most {runner.MAX_STEPS})")
    run.add_argument("--seconds", type=float, default=runner.DEFAULT_SECONDS, help="seconds one task may take (default 900)")
    run.add_argument("--set", action="append", default=[], metavar="NAME=VALUE", help="a setting of the application other than its default, e.g. chatgpt_compact_at=70")
    run.add_argument("--price", type=price, default=Price(), help="token prices in uncached input tokens (default read=0.1,write=1.25,output=5)")
    run.add_argument("--account", default=str(PROFILE / "chatgpt"), help="the folder with the ChatGPT sign-in (default: the evaluation's own profile in sandbox/)")
    run.add_argument("--models", default="", help="the models.json of the API providers, for an api: model")
    run.add_argument("--out", default=str(RUNS), help="where runs are kept (default: sandbox/eval/runs)")
    run.add_argument("--name", default="", help="a word added to the run's folder name")
    compare = commands.add_parser("compare", help="two runs side by side, task by task")
    compare.add_argument("before", help="a run's folder or its result.json")
    compare.add_argument("after")
    compare.add_argument("--out", default="", help="also write the table to this file")
    return root


def main(argv: list[str] | None = None) -> int:
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(errors="replace")  # (a console that cannot show a character must not end a run)
    options = parser().parse_args(argv)
    try:
        if options.command == "list":
            return _list()
        if options.command == "verify":
            return _verify(options)
        if options.command == "run":
            return _run(options)
        return _compare(options)
    except (taskset.TaskError, report.ReportError) as error:
        print(f"error: {error}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
