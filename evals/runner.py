"""Giving the tasks to the loop, and standing in for the user while it works.

Each task gets a conversation of its own with ``ChatGPTEngine.open_new``: the controller, the loop, the tools, the
permissions and the compaction are the application's, with nothing replaced. What a window would add (titles, the shared
memory, the user's skills and extensions) is left out, so that a run measures the loop and costs no request of its own.

``Pilot`` is the user of a task: it allows what the loop asks permission for (and counts it: each is a time a person
would have been interrupted) unless it names a file outside the task's folder, which it refuses; it leaves the model's
questions unanswered, and stops the turn when the task's tokens or its time are used up. The tokens are checked after
every response, before the next request is made: the turn is stopped when that request (as large as the last one, and
``RESERVE`` more) could go over.

There is no sandbox: a command the pilot allows runs on this machine as the user. The tasks are therefore small projects
that need nothing outside their own folder.

A run has a token budget of its own (``Options.budget``): a task is given what is left of it at most, and tasks that
find too little left are not started. ``HARD_BUDGET`` is the most a run may be given at all. A run also stops giving out
tasks when ``DEAD_TASKS`` in a row failed without any answer from the model (the sign-in, the key or the service is the
matter then, not the tasks), and at once when the service answers without saying how many tokens the answer took: a
budget that cannot be counted cannot be kept.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import sys
import time
from collections.abc import Callable, Iterator, Sequence
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from desktop.core.harness import skills
from desktop.core.session_state import SessionState
from desktop.core.settings import SETTINGS_ENV

from evals import SCHEMA, metrics, taskset
from evals.metrics import Price, TaskResult
from evals.taskset import Task

HARD_BUDGET = 5_000_000
DEFAULT_BUDGET = 1_000_000
DEFAULT_TASK_BUDGET = 200_000
DEFAULT_STEPS = 30
MAX_STEPS = 100
"""The most requests a task may be given: the conversation keeps the diagnostics of its last 128 requests (failed and
summarising ones included), and a task's figures are read from them."""
DEFAULT_SECONDS = 900.0
RESERVE = 20_000
"""Tokens a request may be larger than the one before it (what the tools of one step can add) when the pilot decides
whether the next request still fits."""
MIN_START = 2 * RESERVE
"""A task is not started when less than this is left of the run's budget: it could not make two requests."""
DEAD_TASKS = 2
TICK_SECONDS = 0.2
APPROVE, REFUSE = "proceed_once", "cancel"
AUTONOMIES = ("code", "full")
RESULT_FILE = "result.json"


@dataclass(frozen=True)
class Options:
    model: str
    reasoning: str = ""
    autonomy: str = "code"
    budget: int = DEFAULT_BUDGET
    """Tokens of all kinds the whole run may use."""
    task_budget: int = DEFAULT_TASK_BUDGET
    steps: int = DEFAULT_STEPS
    seconds: float = DEFAULT_SECONDS
    price: Price = field(default_factory=Price)

    def __post_init__(self) -> None:
        if not self.model.strip():
            raise ValueError("a model is required")
        if self.autonomy not in AUTONOMIES:
            raise ValueError(f"autonomy must be one of {', '.join(AUTONOMIES)}")
        if not MIN_START <= self.budget <= HARD_BUDGET:
            raise ValueError(f"the run's budget must be between {MIN_START:,} and {HARD_BUDGET:,} tokens")
        if self.task_budget < MIN_START or not 1 <= self.steps <= MAX_STEPS or self.seconds <= 0:
            raise ValueError(f"a task needs at least {MIN_START:,} tokens, 1 to {MAX_STEPS} steps and some time")


@contextlib.contextmanager
def isolated(folder: Path) -> Iterator[None]:
    """While the tasks run, nothing of the user's reaches them: the skills and plugins of their home folder, WittyCode's
    own settings folder (its extensions, its providers). ``python`` in a command the agent runs is the interpreter of the
    evaluation, which leaves no ``__pycache__`` in the task's folder."""
    home = folder / "home"
    home.mkdir(parents=True, exist_ok=True)
    names = (SETTINGS_ENV, "PATH", "PYTHONDONTWRITEBYTECODE")
    before_env, before_home = {name: os.environ.get(name) for name in names}, skills.USER_HOME
    skills.USER_HOME = home
    os.environ[SETTINGS_ENV] = str(folder / "app" / "settings.json")
    os.environ["PATH"] = str(Path(sys.executable).parent) + os.pathsep + os.environ.get("PATH", "")
    os.environ["PYTHONDONTWRITEBYTECODE"] = "1"
    try:
        yield
    finally:
        skills.USER_HOME = before_home
        for name, value in before_env.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value


class Pilot:
    """The user of one task (see the module's text)."""

    def __init__(self, controller: Any, root: Path, *, allowance: int, seconds: float) -> None:
        self.controller = controller
        self.root = root.resolve()
        self.allowance = allowance
        self.seconds = seconds
        self.stopped = ""
        """``budget``, ``timeout`` or ``unmetered`` when the pilot stopped the turn."""
        self.approvals = 0
        self.refusals = 0
        self.questions = 0
        self._woken = asyncio.Event()
        self._stopping: asyncio.Future[Any] | None = None
        controller.add_listener(self._on_change)

    def _on_change(self, controller: Any, kind: str) -> None:
        self._woken.set()

    def detach(self) -> None:
        self.controller.remove_listener(self._on_change)

    def _over(self) -> bool:
        usage = self.controller.usage
        used = metrics.tokens_of(usage)["total"]
        records = self.controller.saved.requests
        return used + (metrics.request_total(records[-1]) if records else 0) + RESERVE > self.allowance

    def _allowed(self, prompt: Any) -> bool:
        """Whether every file the request names is inside the task's folder (a command names none)."""
        for action in prompt.actions:
            if action.path and not Path(action.path).resolve().is_relative_to(self.root):
                return False
        return True

    def _stop(self, why: str) -> None:
        self.stopped = why
        self._stopping = asyncio.ensure_future(self.controller.interrupt())

    def _tick(self) -> None:
        controller = self.controller
        for prompt in controller.transcript.pending_prompts():
            if prompt.kind == "permission":
                allowed = self._allowed(prompt)
                if controller.answer_permission(prompt.id, APPROVE if allowed else REFUSE):
                    self.approvals, self.refusals = self.approvals + allowed, self.refusals + (not allowed)
            else:
                self.questions += bool(controller.answer_question(prompt.id, None))
        if not self.stopped and any(not record.get("failed") and not metrics.request_input(record) for record in controller.saved.requests):
            self._stop("unmetered")  # (an answer whose tokens the service did not report: what the task uses cannot be counted)
        if not self.stopped and self._over():
            self._stop("budget")

    async def wait(self) -> None:
        """Until the turn is over."""
        deadline = time.monotonic() + self.seconds
        while self.controller.state is SessionState.RUNNING:
            self._tick()
            if not self.stopped and time.monotonic() >= deadline:
                self._stop("timeout")
            self._woken.clear()
            if self.controller.state is not SessionState.RUNNING:
                break
            with contextlib.suppress(asyncio.TimeoutError):
                await asyncio.wait_for(self._woken.wait(), TICK_SECONDS)
        if self._stopping is not None:
            await self._stopping


async def run_task(engine: Any, task: Task, options: Options, out: Path, allowance: int) -> TaskResult:
    """Give one task to the loop and say what came of it. ``allowance``: what is left of the run's tokens. A task the
    evaluation itself fails on (its folder cannot be made, the conversation cannot be read) is a result too, with the
    tokens it had used: one such task must not end the run or go uncounted."""
    result = TaskResult(id=task.id, title=task.title, end="error")
    try:
        await _run_task(engine, task, options, out, allowance, result)
    except Exception as error:
        result.end, result.passed, result.note = "error", False, f"{type(error).__name__}: {error}"
    return result


async def _run_task(engine: Any, task: Task, options: Options, out: Path, allowance: int, result: TaskResult) -> None:
    root = taskset.prepare(task, out / "work" / task.id)
    kept = taskset.snapshot(task, root)
    engine.settings.max_steps = min(task.steps or options.steps, MAX_STEPS)
    controller = await engine.open_new(root, model=options.model, reasoning=options.reasoning or None, autonomy=options.autonomy)
    pilot = Pilot(controller, root, allowance=min(allowance, task.budget or options.task_budget), seconds=task.seconds or options.seconds)
    try:
        if not controller.send_prompt(task.prompt):
            raise RuntimeError("the conversation did not take the task's message")
        await pilot.wait()
        result.tokens = metrics.tokens_of(controller.usage)  # (first: whatever else goes wrong, what was used is counted)
        for name, value in metrics.measure(controller, options.price).items():
            setattr(result, name, value)
        (out / "transcripts").mkdir(exist_ok=True)
        (out / "transcripts" / f"{task.id}.md").write_text(metrics.transcript_text(controller.saved.items), encoding="utf-8")
    finally:
        pilot.detach()
        await controller.close()
    result.end = pilot.stopped or result.end
    result.approvals, result.refusals, result.questions = pilot.approvals, pilot.refusals, pilot.questions
    result.changed = taskset.changed(root)  # (before the check puts the protected files back)
    verdict = taskset.check(task, root, kept)
    result.passed, result.check = verdict.ok, verdict.detail
    result.seconds["check"] = round(verdict.seconds, 2)


def write_result(out: Path, options: Options, about: dict[str, Any], results: Sequence[TaskResult], started: float, finished: float | None) -> Path:
    """``result.json`` of a run (written after every task, so a run that is cut short leaves what it had)."""
    data = {
        "schema": SCHEMA,
        "name": out.name,
        "started": round(started, 3),
        "finished": None if finished is None else round(finished, 3),
        "options": asdict(options),
        "about": about,
        "tasks": [result.to_json() for result in results],
    }
    out.mkdir(parents=True, exist_ok=True)
    file = out / RESULT_FILE
    temporary = file.with_suffix(".tmp")
    temporary.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    os.replace(temporary, file)
    return file


async def run_tasks(
    engine: Any, tasks: Sequence[Task], options: Options, out: Path, *, about: dict[str, Any] | None = None, progress: Callable[[str], None] = lambda line: None
) -> list[TaskResult]:
    """Run the tasks one after another inside the run's token budget; ``out`` gets the project folders (``work/``), the
    conversations as text (``transcripts/``) and ``result.json``."""
    results: list[TaskResult] = []
    started, used, dead, halted, steps = time.time(), 0, 0, "", engine.settings.max_steps
    out.mkdir(parents=True, exist_ok=True)
    try:
        with isolated(out):
            for number, task in enumerate(tasks, 1):
                left = options.budget - used
                if halted:
                    result = TaskResult(id=task.id, title=task.title, note=halted)
                elif left < MIN_START:
                    result = TaskResult(id=task.id, title=task.title, note=f"only {max(left, 0):,} tokens were left of the run's budget")
                else:
                    result = await run_task(engine, task, options, out, left)
                    dead = dead + 1 if result.end == "failed" and not result.tokens["total"] else 0
                    if result.end == "unmetered":
                        halted = "the model's service reported no token usage, so the run could not be kept inside its budget"
                    elif dead >= DEAD_TASKS:
                        halted = f"the {DEAD_TASKS} tasks before it failed without an answer from the model"
                used += result.tokens["total"]
                results.append(result)
                write_result(out, options, about or {}, results, started, None)
                state = "skipped" if result.end == "skipped" else ("PASS" if result.passed else "FAIL")
                progress(f"[{number}/{len(tasks)}] {task.id}: {state} ({result.end}), {result.tokens['total']:,} tokens, {result.seconds['turn']:.0f} s; run total {used:,}")
    finally:
        engine.settings.max_steps = steps
        write_result(out, options, about or {}, results, started, time.time())
    return results


def real_engine(model: str, settings: Any, *, account: Path, models: Path | None, sessions: Path) -> Any:
    """The engine of a real run: the ChatGPT sign-in kept in ``account`` (the ``chatgpt`` folder beside a settings file),
    or, for an ``api:`` model, the providers of the ``models.json`` given. The conversations are saved in ``sessions``."""
    from desktop.core.chatgpt.engine import ChatGPTEngine
    from desktop.core.harness.store import SessionStore
    from desktop.core.llm.backend import ApiBackend, is_api_model
    from desktop.core.providers import ProviderStore

    # (the providers' names are kept beside the models file, as the application keeps them: never read from the user's folder)
    store = ProviderStore(models_file=models, sidecar=models.with_name("providers.json")) if is_api_model(model) and models is not None else None
    api = ApiBackend(store) if store is not None else None
    engine = ChatGPTEngine(settings, account, api=api)
    engine.sessions = SessionStore(sessions)
    return engine


async def preflight(engine: Any, model: str, reasoning: str = "") -> str:
    """Why a run with this model cannot start (``""``: it can), found without asking a model anything."""
    from desktop.core.chatgpt.errors import ChatGPTError
    from desktop.core.llm.backend import is_api_model

    if is_api_model(model):
        if engine.api is None:
            return "an api: model needs --models (a models.json with the provider)"
        if engine.model_info(model) is None:
            known = ", ".join(spec.id for spec in engine.api.specs()) or "none"
            return f"{model} is not in the models file (it has: {known})"
        return _effort_problem(engine, model, reasoning)
    try:
        await engine.ensure_ready()
    except ChatGPTError as error:
        return f"no usable ChatGPT sign-in in {engine.root} ({error})"
    known = [item.id for item in await engine.list_models()]
    if model not in known:
        return f"{model} is not one of the plan's models ({', '.join(known)})"
    return _effort_problem(engine, model, reasoning)


def _effort_problem(engine: Any, model: str, reasoning: str) -> str:
    efforts = tuple(getattr(engine.model_info(model), "efforts", ()) or ())
    return f"{model} has no reasoning effort {reasoning!r} (it has: {', '.join(efforts)})" if reasoning and efforts and reasoning not in efforts else ""
