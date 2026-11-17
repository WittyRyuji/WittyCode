"""Mutation checks for the evaluation set (``evals/``): each mutant undoes one part of how a task is prepared, checked,
run inside its budget or measured; ``tests/test_evals_*.py`` must catch it.

    python sandbox/mutate_evals.py            run every mutant, in a temporary copy of the repository
    python sandbox/mutate_evals.py --check    only check that every pattern is found exactly once (read only)
    python sandbox/mutate_evals.py 3 7        run just those mutants

This working tree is never written to: ``desktop``, ``tests`` and ``evals`` go to a temporary folder, each mutant is
applied there and undone, and the copy is removed at the end.
"""
from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PY = str(ROOT / ".venv" / "Scripts" / "python.exe") if (ROOT / ".venv").exists() else sys.executable
TESTS = ["tests/test_evals_taskset.py", "tests/test_evals_runner.py"]
E = Path("evals")
TASKSET, RUNNER, METRICS, REPORT, MAIN = (E / name for name in ("taskset.py", "runner.py", "metrics.py", "report.py", "__main__.py"))
PROBES = E / "tasks" / "probes"

M: list[tuple[str, Path, str, str]] = []


def _plain(path: Path) -> str:
    return path.read_bytes().decode("utf-8").replace("\r\n", "\n")


def _pytest(copy: Path) -> tuple[bool, str]:
    done = subprocess.run(
        [PY, "-m", "pytest", "-q", "-x", "-rf", "-p", "no:cacheprovider", *TESTS], cwd=copy, capture_output=True, text=True,
        encoding="utf-8", errors="replace", timeout=600, env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"},
    )
    failed = re.findall(r"^FAILED \S+::(\S+)", done.stdout, re.MULTILINE)
    return done.returncode != 0, ", ".join(failed) or (done.stdout.strip().splitlines() or ["?"])[-1]


def main() -> int:
    args = sys.argv[1:]
    only = {int(arg) for arg in args if arg.isdigit()}
    chosen = [(number, mutant) for number, mutant in enumerate(M, 1) if not only or number in only]
    missing = [(number, name, count) for number, (name, path, old, _) in chosen if (count := _plain(ROOT / path).count(old)) != 1]
    for number, name, count in missing:
        print(f"[{number}] {name}: PATTERN FOUND {count} TIMES", flush=True)
    if "--check" in args or missing:
        print(f"{len(chosen) - len(missing)} of {len(chosen)} patterns found once")
        return 1 if missing else 0
    copy = Path(tempfile.mkdtemp(prefix="wittycode-evals-mutants-"))
    survivors: list[str] = []
    try:
        for folder in ("desktop", "tests", "evals"):
            shutil.copytree(ROOT / folder, copy / folder, ignore=shutil.ignore_patterns("__pycache__", "*.pyc", "ui_dist"))
        shutil.copy2(ROOT / "pyproject.toml", copy / "pyproject.toml")
        broken, tail = _pytest(copy)
        if broken:
            print(f"the copy does not pass its tests as it is: {tail}")
            return 1
        print(f"copy at {copy}: {tail}", flush=True)
        for number, (name, path, old, new) in chosen:
            target = copy / path
            original = target.read_bytes()
            target.write_bytes(_plain(target).replace(old, new).encode("utf-8"))
            started = time.time()
            try:
                caught, by = _pytest(copy)
            except subprocess.TimeoutExpired:
                caught, by = True, "timeout (hangs: caught)"
            finally:
                target.write_bytes(original)
            print(f"[{number}] {'caught  ' if caught else 'SURVIVED'} {name}  ({time.time() - started:.0f}s) {by}", flush=True)
            if not caught:
                survivors.append(name)
    finally:
        shutil.rmtree(copy, ignore_errors=True)
    print(f"\n{len(chosen) - len(survivors)} of {len(chosen)} caught")
    for name in survivors:
        print("  survivor:", name)
    print("copy removed" if not copy.exists() else f"copy NOT removed: {copy}")
    return 1 if survivors else 0


BIG, GBK, RULES = PROBES / "big-file" / "task.py", PROBES / "gbk-csv" / "task.py", PROBES / "project-rules" / "task.py"

M += [
    # ---- taskset: the project folder
    ("prepare: what a test run leaves is listed as a change", TASKSET, '    (info / "exclude").write_text(_EXCLUDED, encoding="utf-8")\n', "    pass\n"),
    ("prepare: the files keep their own times", TASKSET, 'ignore=shutil.ignore_patterns("__pycache__", "*.pyc"), copy_function=shutil.copyfile)', 'ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))'),
    ("changed: new files are not listed", TASKSET, '"--untracked-files=all"', '"--untracked-files=no"'),
    ("restore: a file added to a protected folder stays", TASKSET, "                file.unlink()\n                undone.append(relative)\n", "                undone.append(relative)\n"),
    ("restore: a changed protected file stays", TASKSET, "        if not file.is_file() or file.read_bytes() != data:\n", "        if not file.is_file():\n"),
    ("check: the protected files are not put back", TASKSET, "    undone = restore(task, root, kept)\n", "    undone: list[str] = []\n"),
    ("check: what was done to the protected files is not said", TASKSET, "(note + _short(problem)).strip()", "_short(problem)"),
    ("check: a check that raises passes", TASKSET, '        problem = f"The check itself failed: {type(error).__name__}: {error}"\n', '        problem = ""\n'),
    ("check: the head of a long output is kept", TASKSET, r'"[...]\n" + text[-DETAIL_LIMIT:]', "text[:DETAIL_LIMIT]"),
    ("run_command: python is whatever the path has", TASKSET, '    command = [sys.executable, "-B", *argv[1:]] if argv and argv[0] == "python" else list(argv)\n', "    command = list(argv)\n"),
    ("load: a protected path may lie outside the task", TASKSET, '        if path.is_absolute() or ".." in path.parts:\n', "        if False:\n"),
    ("suites: a suite may name a task that is not there", TASKSET, " or any(task_id not in tasks for task_id in ids)", ""),
    ("select: a task named twice is run twice", TASKSET, "for task_id in dict.fromkeys(ids)]", "for task_id in ids]"),
    ("verify: a check that passes as given is fine", TASKSET, "    if before.ok:\n", "    if False:\n"),
    # ---- runner: the budget
    ("options: more than five million is accepted", RUNNER, "        if not MIN_START <= self.budget <= HARD_BUDGET:\n", "        if not MIN_START <= self.budget:\n"),
    ("pilot: the reserve is not counted", RUNNER, " + RESERVE > self.allowance", " > self.allowance"),
    ("pilot: the next request is not counted", RUNNER, "used + (metrics.request_total(records[-1]) if records else 0) + RESERVE", "used + RESERVE"),
    ("pilot: a task is never stopped for its tokens", RUNNER, "        if not self.stopped and self._over():\n", "        if False:\n"),
    ("pilot: a task is never stopped for its time", RUNNER, "            if not self.stopped and time.monotonic() >= deadline:\n", "            if False:\n"),
    ("run: a task's own budget is ignored", RUNNER, "allowance=min(allowance, task.budget or options.task_budget)", "allowance=min(allowance, options.task_budget)"),
    ("run: a task's own step limit is ignored", RUNNER, "    engine.settings.max_steps = min(task.steps or options.steps, MAX_STEPS)\n", "    engine.settings.max_steps = options.steps\n"),
    ("options: any number of steps is accepted", RUNNER, "not 1 <= self.steps <= MAX_STEPS", "self.steps < 1"),
    ("run: a task is given the whole budget, not what is left", RUNNER, "                    result = await run_task(engine, task, options, out, left)\n", "                    result = await run_task(engine, task, options, out, options.budget)\n"),
    ("run: a task starts with too little left", RUNNER, "                elif left < MIN_START:\n", "                elif False:\n"),
    ("run: a service that does not answer does not stop the run", RUNNER, "                    elif dead >= DEAD_TASKS:\n", "                    elif False:\n"),
    ("run: a service that reports no usage does not stop the run", RUNNER, '                    if result.end == "unmetered":\n', "                    if False:\n"),
    ("pilot: an answer without its tokens is not noticed", RUNNER, 'any(not record.get("failed") and not metrics.request_input(record) for record in controller.saved.requests)', "False"),
    ("pilot: a failed request counts as an answer without its tokens", RUNNER, 'any(not record.get("failed") and not metrics.request_input(record)', 'any(not metrics.request_input(record)'),
    ("run: one failure stops the run", RUNNER, "dead = dead + 1 if result.end == \"failed\" and not result.tokens[\"total\"] else 0", "dead = dead + 2 if result.end == \"failed\" and not result.tokens[\"total\"] else 0"),
    ("run: the pilot's reason is lost", RUNNER, "    result.end = pilot.stopped or result.end\n", "    pass\n"),
    ("run: the tokens of a task that could not be read are lost", RUNNER, "        result.tokens = metrics.tokens_of(controller.usage)  # (first: whatever else goes wrong, what was used is counted)\n", ""),
    ("run: the changes are read after the tests were put back", RUNNER, "    result.changed = taskset.changed(root)  # (before the check puts the protected files back)\n    verdict = taskset.check(task, root, kept)\n", "    verdict = taskset.check(task, root, kept)\n    result.changed = taskset.changed(root)\n"),
    ("run: the settings keep the evaluation's step limit", RUNNER, "        engine.settings.max_steps = steps\n", "        pass\n"),
    ("run: nothing is written until the run is over", RUNNER, "                write_result(out, options, about or {}, results, started, None)\n", ""),
    # ---- runner: the pilot as the user
    ("pilot: a file outside the task's folder is allowed", RUNNER, "            if action.path and not Path(action.path).resolve().is_relative_to(self.root):\n", "            if False:\n"),
    ("pilot: allowed requests are not counted", RUNNER, "self.approvals + allowed, self.refusals + (not allowed)", "self.approvals, self.refusals + (not allowed)"),
    ("pilot: a question is left waiting", RUNNER, "                self.questions += bool(controller.answer_question(prompt.id, None))\n", "                pass\n"),
]

M += [
    # ---- runner: apart from the user
    ("isolated: the user's skills reach the task", RUNNER, "    skills.USER_HOME = home\n", "    pass\n"),
    ("isolated: WittyCode's own folder is the user's", RUNNER, '    os.environ[SETTINGS_ENV] = str(folder / "app" / "settings.json")\n', "    pass\n"),
    ("isolated: python is whatever the path has", RUNNER, '    os.environ["PATH"] = str(Path(sys.executable).parent) + os.pathsep + os.environ.get("PATH", "")\n', "    pass\n"),
    ("isolated: the environment is not put back", RUNNER, "                os.environ[name] = value\n", "                pass\n"),
    ("isolated: the user's home is not put back", RUNNER, "        skills.USER_HOME = before_home\n", "        pass\n"),
    ("real_engine: the providers' names are read from the user's folder", RUNNER, 'ProviderStore(models_file=models, sidecar=models.with_name("providers.json"))', "ProviderStore(models_file=models)"),
    ("preflight: an effort the model does not have is let through", RUNNER, "if reasoning and efforts and reasoning not in efforts else", "if False else"),
    ("preflight: a model the plan does not have is let through", RUNNER, "    if model not in known:\n", "    if False:\n"),
    # ---- metrics
    ("metrics: a request sent again is a break", METRICS, '        if _number(record.get("shared_prefix_items")) < _number(before.get("input_items")):\n', '        if _number(record.get("shared_prefix_items")) <= _number(before.get("input_items")):\n'),
    ("metrics: cache writes are not counted", METRICS, '            "cacheWrite": _number(usage.cache_creation_tokens),\n', '            "cacheWrite": 0,\n'),
    ("metrics: a summary is a step", METRICS, '    steps = [record for record in records if record.get("kind") != "summary"]\n', "    steps = list(records)\n"),
    ("metrics: tools at the same time count twice", METRICS, "    worked = _covered(spans)\n", "    worked = sum(end - start for start, end in spans)\n"),
    ("metrics: the price of output is that of input", METRICS, '+ tokens["output"] * self.output', '+ tokens["output"]'),
    ("metrics: the price of a cache read is that of input", METRICS, '+ tokens["cacheRead"] * self.read', '+ tokens["cacheRead"]'),
    ("metrics: a turn nothing came of is done", METRICS, '{"failed": "failed", "interrupted"', '{"failed": "done", "interrupted"'),
    ("metrics: the hit rate leaves out the cache writes", METRICS, 'self.tokens["input"] + self.tokens["cacheWrite"] + self.tokens["cacheRead"]', 'self.tokens["input"] + self.tokens["cacheRead"]'),
    ("metrics: a task that was skipped ran", METRICS, '        return self.end != "skipped"\n', "        return True\n"),
    ("metrics: the largest request counts its output", METRICS, "max((request_input(record) for record in steps), default=0)", "max((request_total(record) for record in steps), default=0)"),
    ("metrics: the first output is not measured", METRICS, "first_output=round(max(first, 0.0), 2)", "first_output=0.0"),
    # ---- report and the command line
    ("report: tasks that did not run are in the sums", REPORT, "    ran = [task for task in tasks if task.ran]\n", "    ran = list(tasks)\n"),
    ("report: a result of another version is read", REPORT, 'data.get("schema") != SCHEMA or ', ""),
    ("report: a task that passed but did not end by itself is not shown", REPORT, '    troubled = [task for task in tasks if not task.passed or task.end != "done"]\n', "    troubled = [task for task in tasks if not task.passed]\n"),
    ("compare: the change is against the later run", REPORT, 'return "-" if not before else f"{(after - before) / before:+.0%}"', 'return "-" if not before else f"{(after - before) / after:+.0%}"'),
    ("cli: m is a thousand", MAIN, '{"": 1, "k": 1_000, "m": 1_000_000}', '{"": 1, "k": 1_000, "m": 1_000}'),
    ("cli: a model named as the page names it is not found", MAIN, '    return text.strip().removeprefix("chatgpt:")\n', "    return text.strip()\n"),
    ("cli: a setting that is not there is ignored", MAIN, '            raise ValueError(f"--set {pair}: no such setting, or its value does not fit")\n', "            continue\n"),
    ("cli: the summary is not written", MAIN, '        (out / SUMMARY_FILE).write_text(text, encoding="utf-8")\n', ""),
    # ---- the checks of the probes
    ("big-file: other rules may be changed", BIG, "    return f\"Other parts of pricing/rules.py were changed: {', '.join(touched[:8])}.\" if touched else \"\"\n", '    return ""\n'),
    ("big-file: rules may be added or lost", BIG, "    if list(given) != list(now):\n", "    if False:\n"),
    ("gbk-csv: the line ends are not looked at", GBK, r'    if data.replace(b"\r\n", b"").count(b"\n") or data.replace(b"\r\n", b"").count(b"\r"):' + "\n", "    if False:\n"),
    ("gbk-csv: the records are not compared", GBK, r'    if text != "".join(line + "\r\n" for line in WANTED):' + "\n", "    if False:\n"),
    ("project-rules: the changelog is not looked at", RULES, '    if len(_notes((root / "CHANGELOG.md").read_text(encoding="utf-8"))) < 2:\n', "    if False:\n"),
    ("project-rules: the lint is not run", RULES, '    code, output = run(["python", "tools/lint.py"])\n    if code:\n', '    code, output = run(["python", "tools/lint.py"])\n    if False:\n'),
]


if __name__ == "__main__":
    sys.exit(main())
