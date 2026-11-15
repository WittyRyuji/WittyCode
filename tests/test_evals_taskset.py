"""``evals.taskset``: the tasks of the evaluation set are sound without any model (each check fails on the task as given
and passes on its reference solution), a task's folder stands apart from the folders around it, and a check cannot be
passed by changing what it protects."""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path

import pytest

from desktop.core.harness import prompt, skills
from evals import __main__ as cli
from evals import taskset
from evals.taskset import TaskError

TASKS = taskset.all_tasks()


def prepared(task_id: str, tmp_path: Path) -> tuple[taskset.Task, Path, dict[str, bytes]]:
    task = TASKS[task_id]
    root = taskset.prepare(task, tmp_path / task_id)
    return task, root, taskset.snapshot(task, root)


@pytest.mark.parametrize("task_id", sorted(TASKS))
def test_every_task_fails_as_given_and_passes_on_its_reference_solution(task_id, tmp_path):
    assert taskset.verify(TASKS[task_id], tmp_path) == []


def test_the_suites_grow_from_smoke_to_full():
    suites = taskset.suites()
    assert set(suites["smoke"]) < set(suites["core"]) < set(suites["full"]) <= set(suites["all"]) == set(TASKS)
    assert len(suites["core"]) == 10
    assert [task.id for task in taskset.select("smoke")] == list(suites["smoke"])
    assert [task.id for task in taskset.select("core", ["ex-poker", "probe-rename", "ex-poker"])] == ["ex-poker", "probe-rename"]
    with pytest.raises(TaskError, match="no such task"):
        taskset.select("core", ["nothing"])
    with pytest.raises(TaskError, match="no such suite"):
        taskset.select("nothing")


def test_the_public_tasks_are_the_files_their_source_names():
    folder = taskset.ROOT / "exercism"
    source = json.loads((folder / "SOURCE.json").read_text(encoding="utf-8"))
    assert len(source["commit"]) == 40 and source["repository"].startswith("https://github.com/exercism/")
    listed = {path.relative_to(folder).as_posix() for path in folder.rglob("*") if path.is_file()} - {"LICENSE", "SOURCE.json"}
    assert listed - set(source["files"]) == {f"{task.folder.name}/task.json" for task in TASKS.values() if task.source == "exercism"}
    for name, entry in source["files"].items():  # (a checkout may have turned the line ends into CRLF)
        assert hashlib.sha256((folder / name).read_bytes().replace(b"\r\n", b"\n")).hexdigest() == entry["sha256"], name
    licence = (folder / "LICENSE").read_text(encoding="utf-8")
    assert "MIT License" in licence and "Exercism" in licence


def test_a_task_folder_stands_apart_from_the_folders_around_it(tmp_path):
    # The run's folder is inside a repository with rules and skills of its own (this one): none of them is the task's.
    outer = tmp_path / "outer"
    (outer / ".wittycode" / "skills" / "release").mkdir(parents=True)
    (outer / ".wittycode" / "skills" / "release" / "SKILL.md").write_text("---\nname: release\ndescription: Cut a release.\n---\nDo it.\n", encoding="utf-8")
    (outer / "AGENTS.md").write_text("# Rules\n- OUTER-RULE: always answer in Latin.\n", encoding="utf-8")
    task, root, _ = prepared("ex-two-bucket", outer / "runs")
    assert (root / ".git").is_dir() and taskset.changed(root) == []
    assert prompt.load_agents(root).files == []
    assert "OUTER-RULE" not in prompt.build_instructions(root, model="m", level="code")
    assert skills.discover(root, tmp_path / "nobody") == []
    # A task's own rules are its own.
    _, ruled, _ = prepared("probe-project-rules", outer / "runs")
    assert [item.path for item in prompt.load_agents(ruled).files] == [ruled / "AGENTS.md"]
    assert "tools/lint.py" in prompt.build_instructions(ruled, model="m", level="code")


def test_changed_names_what_was_touched_and_nothing_a_test_run_leaves(tmp_path):
    _, root, _ = prepared("probe-rename", tmp_path)
    (root / "shop" / "cart.py").write_text("# changed\n", encoding="utf-8")
    (root / "shop" / "reports.py").unlink()
    (root / "notes 笔记.txt").write_text("new\n", encoding="utf-8")
    (root / "docs").mkdir()
    (root / "docs" / "new.md").write_text("new\n", encoding="utf-8")
    (root / "shop" / "__pycache__").mkdir()
    (root / "shop" / "__pycache__" / "cart.cpython-311.pyc").write_bytes(b"\0")
    assert taskset.changed(root) == ["docs/new.md", "notes 笔记.txt", "shop/cart.py", "shop/reports.py"]


def test_a_check_cannot_be_passed_by_changing_what_it_protects(tmp_path):
    task, root, kept = prepared("probe-rename", tmp_path)
    given = (root / "tests" / "test_shop.py").read_bytes()
    for name in ("test_shop.py", "test_names.py"):
        (root / "tests" / name).write_text("import unittest\n\n\nclass Fine(unittest.TestCase):\n    def test_fine(self):\n        pass\n", encoding="utf-8")
    (root / "tests" / "sitecustomize.py").write_text("raise SystemExit(0)\n", encoding="utf-8")
    verdict = taskset.check(task, root, kept)
    assert not verdict.ok
    assert verdict.detail.startswith("Protected files were changed and put back before the check: tests/sitecustomize.py, tests/test_names.py, tests/test_shop.py.")
    assert (root / "tests" / "test_shop.py").read_bytes() == given and not (root / "tests" / "sitecustomize.py").exists()
    # A protected file that was deleted is back as well, and the real solution then passes.
    (root / "tests" / "test_names.py").unlink()
    taskset.solve(task, root)
    assert taskset.check(task, root, kept).ok and (root / "tests" / "test_names.py").read_bytes() == kept["tests/test_names.py"]


def test_the_long_module_must_be_fixed_and_otherwise_left_alone(tmp_path):
    task, root, kept = prepared("probe-big-file", tmp_path)
    rules = root / "pricing" / "rules.py"
    fixed = task.hooks.rules_text(wrong=None)
    assert rules.read_text(encoding="utf-8").count("\n") > 1400
    assert "rule='rule_097'" in taskset.check(task, root, kept).detail  # (the failing test names the rule: no need to read the file)
    rules.write_text(fixed.replace("def rule_150(quantity: int, unit_price: int) -> int:", "def rule_150(quantity, unit_price):"), encoding="utf-8")
    assert taskset.check(task, root, kept).detail == "Other parts of pricing/rules.py were changed: rule_150."
    rules.write_text(fixed[: fixed.index("def rule_170(")], encoding="utf-8")  # (a file written back with its end missing)
    assert not taskset.check(task, root, kept).ok
    rules.write_text(fixed + "\n\ndef rule_181(quantity: int, unit_price: int) -> int:\n    return 0\n", encoding="utf-8")
    assert taskset.check(task, root, kept).detail == "pricing/rules.py no longer holds the same rules in the same order (181 rules, 180 were given)."
    rules.write_text(fixed.replace("\n", "\r\n"), encoding="utf-8", newline="")  # (other line ends are not a change of the rules)
    assert taskset.check(task, root, kept).ok


def test_the_stock_file_must_keep_its_encoding_and_its_line_ends(tmp_path):
    task, root, kept = prepared("probe-gbk-csv", tmp_path)
    file = root / "data" / "stock.csv"
    assert file.read_bytes().startswith("品名,库存,单位\r\n".encode("gbk"))
    wanted = "".join(line + "\r\n" for line in task.hooks.WANTED)
    file.write_bytes(wanted.encode("utf-8"))  # (the right records, written as UTF-8: the reading code and the legacy system both fail)
    assert not taskset.check(task, root, kept).ok
    file.write_bytes(wanted.replace("\r\n", "\n").encode("gbk"))
    assert taskset.check(task, root, kept).detail == "data/stock.csv no longer has CRLF line ends throughout."
    file.write_bytes(wanted.replace("扳手,18,把\r\n", "").encode("gbk"))
    assert not taskset.check(task, root, kept).ok
    moved = [task.hooks.WANTED[0], task.hooks.WANTED[-1], *task.hooks.WANTED[1:-1]]  # (the new record is not the last one)
    file.write_bytes("".join(line + "\r\n" for line in moved).encode("gbk"))
    assert taskset.check(task, root, kept).detail.startswith("data/stock.csv does not hold the wanted records")
    file.write_bytes(wanted.encode("gbk"))
    assert taskset.check(task, root, kept).ok


def test_the_project_rules_are_checked_though_the_message_does_not_name_them(tmp_path):
    task, root, kept = prepared("probe-project-rules", tmp_path)
    assert "docstring" not in task.prompt and "CHANGELOG" not in task.prompt and "lint" not in task.prompt
    words = root / "textkit" / "words.py"
    given = words.read_text(encoding="utf-8")
    words.write_text(given + "\n\ndef slugify(title):\n    return '-'.join(word.lower() for word in words(title))\n", encoding="utf-8")
    assert taskset.check(task, root, kept).detail.startswith("The project's lint fails")
    words.write_text(given + '\n\ndef slugify(title: str) -> str:\n    """A slug."""\n    return "-".join(word.lower() for word in words(title))\n', encoding="utf-8")
    assert taskset.check(task, root, kept).detail.startswith("CHANGELOG.md has no new line")
    with (root / "CHANGELOG.md").open("a", encoding="utf-8") as log:
        log.write("- Added `slugify`.\n")
    assert taskset.check(task, root, kept).ok
    (root / "CHANGELOG.md").unlink()  # (a check that cannot read what was left has not passed)
    assert taskset.check(task, root, kept).detail.startswith("The check itself failed: FileNotFoundError")


def test_the_failing_check_hides_in_the_middle_of_a_long_output(tmp_path):
    task, root, kept = prepared("probe-long-output", tmp_path)
    code, output = taskset.run_command(root, task.check)
    assert code == 1 and len(output) > 200_000 and output.count("FAIL") == 1
    assert 60_000 < output.index("FAIL") < len(output) - 60_000  # (far from both ends: a tool that keeps the head and the tail cuts it)
    (root / "settings.json").write_text('{"name": "inventory-api", "retry_limit": 99, "timeout_seconds": 30}\n', encoding="utf-8")
    assert not taskset.check(task, root, kept).ok
    (root / "settings.json").write_text('{"name": "inventory-api", "retry_limit": 3, "timeout_seconds": 30}\n', encoding="utf-8")
    assert taskset.check(task, root, kept).ok


def test_a_check_runs_with_the_interpreter_of_the_evaluation(tmp_path):
    import sys

    code, output = taskset.run_command(tmp_path, ["python", "-c", "import sys; print(sys.executable); print(sys.dont_write_bytecode)"])
    assert (code, output.splitlines()) == (0, [sys.executable, "True"])
    assert taskset.run_command(tmp_path, ["python", "-c", "import time; time.sleep(30)"], timeout=0.5) == (-1, "The check did not end within 0 s.")
    assert taskset.run_command(tmp_path, ["no-such-program-of-the-evaluation"])[0] == -1


def test_a_long_output_of_a_failed_check_keeps_its_end():
    assert taskset._short("short") == "short"
    kept = taskset._short("start " + "x" * 3000 + " FAILED (failures=2)")
    assert kept.startswith("[...]\n") and kept.endswith("FAILED (failures=2)") and len(kept) < 1600


def write_task(folder: Path, **changes: object) -> Path:
    (folder / "workspace").mkdir(parents=True, exist_ok=True)
    (folder / "workspace" / "a.txt").write_text("a\n", encoding="utf-8")
    task = {"id": "demo-task", "title": "A demo", "prompt": ["Do it.", "Then stop."], "check": ["python", "-c", "pass"], **changes}
    (folder / "task.json").write_text(json.dumps({key: value for key, value in task.items() if value is not None}), encoding="utf-8")
    return folder


def test_a_task_file_is_read_strictly(tmp_path):
    root = tmp_path / "tasks"
    task = taskset.load(write_task(root / "set" / "one", protect=["a.txt"], budget=90_000))
    assert (task.id, task.source, task.prompt, task.check, task.protect, task.budget) == ("demo-task", "set", "Do it.\nThen stop.", ("python", "-c", "pass"), ("a.txt",), 90_000)
    for number, (changes, message) in enumerate([
        ({"id": "Demo_Task"}, "'id' must be"),
        ({"prompt": "  "}, "'title' and 'prompt' are required"),
        ({"check": None}, "needs a 'check' command"),
        ({"protect": ["../outside.txt"]}, "outside the task"),
        ({"budget": 0}, "'budget' must be a positive whole number"),
    ]):
        with pytest.raises(TaskError, match=message):
            taskset.load(write_task(tmp_path / "bad" / str(number), **changes))
    assert list(taskset.all_tasks(root)) == ["demo-task"]
    for number, (suites, message) in enumerate([({"one": ["demo-task", "missing-task"]}, "must list existing tasks"), ({"one": ["demo-task", "demo-task"]}, "each once"), ({"one": []}, "must list")]):
        (root / "suites.json").write_text(json.dumps(suites), encoding="utf-8")
        with pytest.raises(TaskError, match=message):
            taskset.suites(root)
    (root / "suites.json").write_text(json.dumps({"one": ["demo-task"]}), encoding="utf-8")
    assert taskset.suites(root) == {"one": ("demo-task",), "all": ("demo-task",)}
    write_task(root / "set" / "two")
    with pytest.raises(TaskError, match="is also that of"):
        taskset.all_tasks(root)
    task = taskset.load(write_task(tmp_path / "other" / "three", id="other-task", protect=["missing.txt"]))
    with pytest.raises(TaskError, match="is not in the workspace"):
        taskset.snapshot(task, taskset.prepare(task, tmp_path / "work"))
    with pytest.raises(TaskError, match="already exists"):
        taskset.prepare(task, tmp_path / "work")


def test_the_command_line_lists_and_verifies_without_a_model(capsys):
    assert cli.main(["list"]) == 0
    listed = capsys.readouterr().out
    assert listed.startswith("suites: smoke (3), core (10), full (13), all (13)") and "probe-gbk-csv" in listed
    assert cli.main(["verify", "--task", "probe-gbk-csv", "--task", "ex-wordy"]) == 0
    assert capsys.readouterr().out.split() == ["probe-gbk-csv", "ok", "ex-wordy", "ok"]
    assert cli.main(["verify", "--suite", "nothing"]) == 2
    assert "no such suite" in capsys.readouterr().err


def test_a_run_is_refused_before_any_request_when_it_could_not_be_paid_for_or_signed_in(tmp_path, capsys):
    assert (cli.tokens("1m"), cli.tokens("1.5M"), cli.tokens("500k"), cli.tokens("1_000_000")) == (1_000_000, 1_500_000, 500_000, 1_000_000)
    assert (cli.model_id("chatgpt:gpt-6-luna"), cli.model_id("gpt-6-luna"), cli.model_id("api:demo:custom:m-0")) == ("gpt-6-luna", "gpt-6-luna", "api:demo:custom:m-0")
    out = tmp_path / "runs"
    assert cli.main(["run", "--model", "gpt-6-luna", "--budget", "6m", "--out", str(out)]) == 2  # (five million is the most a run may be given)
    assert "between 40,000 and 5,000,000" in capsys.readouterr().err
    assert cli.main(["run", "--model", "gpt-6-luna", "--suite", "smoke", "--out", str(out), "--account", str(tmp_path / "nobody")]) == 2
    assert "no usable ChatGPT sign-in" in capsys.readouterr().err
    assert cli.main(["run", "--model", "api:demo:m", "--suite", "smoke", "--out", str(out)]) == 2
    assert "needs --models" in capsys.readouterr().err
    assert cli.main(["run", "--model", "gpt-6-luna", "--set", "no_such_setting=1", "--out", str(out)]) == 2
    assert "no such setting" in capsys.readouterr().err
    assert not out.exists()


def test_verify_says_what_is_wrong_with_a_task(tmp_path):
    # A check that passes before any work is done measures nothing; a solution that does not pass is no reference.
    idle = taskset.load(write_task(tmp_path / "set" / "idle"))
    (idle.folder / "solution").mkdir()
    (idle.folder / "solution" / "a.txt").write_text("b\n", encoding="utf-8")
    # (the solution's file is as large and as old as the file it replaces, as two files of one checkout can be: it is
    # still seen as a change)
    for file in (idle.folder / "workspace" / "a.txt", idle.folder / "solution" / "a.txt"):
        os.utime(file, (1_700_000_000, 1_700_000_000))
    assert taskset.verify(idle, tmp_path / "work") == ["the check passes before any work is done"]
    assert taskset.changed(tmp_path / "work" / "demo-task") == ["a.txt"]
    strict = taskset.load(write_task(tmp_path / "set" / "strict", id="strict-task", check=["python", "-c", "import sys; sys.exit(open('a.txt').read().strip() != 'done')"]))
    with pytest.raises(TaskError, match="has neither solution/ nor a solve"):
        taskset.verify(strict, tmp_path / "work")
    (strict.folder / "solution").mkdir()
    (strict.folder / "solution" / "a.txt").write_text("almost\n", encoding="utf-8")
    (problem,) = taskset.verify(strict, tmp_path / "again")
    assert problem.startswith("the check fails on the reference solution")
    (strict.folder / "solution" / "a.txt").write_text("done\n", encoding="utf-8", newline="\n")
    assert taskset.verify(strict, tmp_path / "third") == []
