"""``evals.runner``: tasks given to the application's own loop (the real engine, controller, tools and permissions,
against the loopback fake of the Responses API) with a scripted model: what a task took is measured from what the loop
keeps, the pilot allows, leaves questions and stops a task at its tokens or its time, and a run stays inside its budget."""

from __future__ import annotations

import asyncio
import json
import os
import sys
from pathlib import Path
from typing import Any

import pytest

from desktop.core.harness import skills
from desktop.core.settings import AppSettings
from evals import __main__ as cli
from evals import metrics, report, runner, taskset
from evals.metrics import Price, TaskResult
from evals.runner import Options
from tests.chatgpt_support import make_engine
from tests.fake_responses import FakeResponses, call, http_error, say, usage

MODEL = "gpt-6.1-sol"
TASKS = taskset.all_tasks()


def solving(task_id: str = "ex-two-bucket") -> list[Any]:
    """The answers of a model that solves the exercise in four requests: read the stub, write the solution, run the tests, say so."""
    task = TASKS[task_id]
    stub, test = task.check[-1].replace("_test.py", ".py"), task.check[-1]
    answer = (task.folder / "solution" / stub).read_text(encoding="utf-8")
    return [
        call("read_file", {"file_path": stub}, call_id="c1", item_id="fc_1", used=usage(1000, 50)),
        call("create_file", {"file_path": stub, "content": answer}, call_id="c2", item_id="fc_2", used=usage(1200, 400, cached=1000)),
        call("shell", {"command": f"python -m unittest -q {test}"}, call_id="c3", item_id="fc_3", used=usage(1700, 40, cached=1200)),
        say("Every test passes.", used=usage(1900, 20, cached=1700)),
    ]


def looping(count: int, tokens: int = 30_000) -> list[Any]:
    """The answers of a model that only ever looks at the folder, each request ``tokens`` of input and 100 of output."""
    return [call("list_dir", {"directory_path": "."}, call_id=f"c{n}", item_id=f"fc_{n}", used=usage(tokens, 100)) for n in range(count)]


def evaluate(tmp_path: Path, fake: FakeResponses, ids: list[str], *, out: Path | None = None, settings: AppSettings | None = None, progress: Any = None, **options: Any):
    """Run the tasks with the fake as the model; ``(results, the run's folder, the engine)``."""
    out = out or tmp_path / "run"
    progress = progress or (lambda line: None)

    async def go():
        engine = make_engine(tmp_path / "engine", fake, settings=settings or AppSettings(think_chinese=False))
        try:
            chosen = Options(model=MODEL, **{"seconds": 20.0, **options})  # (a turn that hangs fails its test soon)
            return await runner.run_tasks(engine, [TASKS[i] for i in ids], chosen, out, about={"suite": "test", "commit": "abc1234"}, progress=progress), engine
        finally:
            await engine.close_all()

    results, engine = asyncio.run(go())
    return results, out, engine


def test_a_task_a_scripted_model_solves_is_measured(tmp_path):
    with FakeResponses() as fake:
        fake.script(*solving())
        (result,), out, _ = evaluate(tmp_path, fake, ["ex-two-bucket"])
        requests = fake.requests
    assert (result.passed, result.end, result.check, result.note) == (True, "done", "", "")
    assert (result.requests, result.tool_calls, result.tool_errors, result.questions) == (4, 3, 0, 0)
    assert result.approvals == 1  # (at the application's own level a command that is not read-only waits for the user)
    assert result.tools == {"Create": 1, "Execute": 1, "Read": 1}
    assert result.tokens == {"input": 1900, "cacheWrite": 0, "cacheRead": 3900, "output": 510, "total": 6310}
    assert result.hit_rate == pytest.approx(3900 / 5800) and result.peak_context == 1900
    assert result.cost_units == 1900 + 390 + 2550
    assert (result.prefix_breaks, result.failed_requests, result.summaries, result.compacted) == (0, 0, 0, False)
    assert result.changed == ["two_bucket.py"]
    seconds = result.seconds
    assert seconds["turn"] > seconds["tools"] > 0 and seconds["check"] > 0 and seconds["requests"] >= 0  # (the fake answers within a millisecond)
    assert seconds["requests"] + seconds["tools"] + seconds["other"] == pytest.approx(seconds["turn"], abs=0.05)
    # What the model was given: the task's message as it is, in a conversation about the task's own folder.
    assert requests[0]["input"][0]["content"][0]["text"] == TASKS["ex-two-bucket"].prompt
    assert "ex-two-bucket" in requests[0]["instructions"] and len(requests) == 4
    # What is left of the run: the figures, and the conversation for a person to read. The agent's own test run passed,
    # so `python` in its shell was an interpreter that could run the tests.
    saved = json.loads((out / "result.json").read_text(encoding="utf-8"))
    assert (saved["schema"], saved["options"]["model"], saved["about"]["commit"], saved["tasks"][0]["id"]) == (1, MODEL, "abc1234", "ex-two-bucket")
    assert saved["finished"] >= saved["started"]
    transcript = (out / "transcripts" / "ex-two-bucket.md").read_text(encoding="utf-8")
    assert "### call shell" in transcript and "Ran 11 tests" in transcript and "\nOK" in transcript


def test_an_answer_without_the_work_fails_the_check_and_says_why(tmp_path):
    with FakeResponses() as fake:
        fake.script(say("It is done."))
        (result,), _, _ = evaluate(tmp_path, fake, ["probe-project-rules"])
        instructions = fake.requests[0]["instructions"]
    assert (result.passed, result.end, result.requests, result.tool_calls, result.changed) == (False, "done", 1, 0, [])
    assert "slugify" in result.check
    assert "tools/lint.py" in instructions  # (the task's own AGENTS.md is part of what the model is told)


def test_a_task_is_stopped_before_the_request_that_could_go_over_its_tokens(tmp_path):
    with FakeResponses() as fake:
        fake.script(*looping(10))
        (result,), _, _ = evaluate(tmp_path, fake, ["ex-two-bucket"], task_budget=100_000)
        sent = len(fake.requests)
    # After the second response 60,200 are used; a third request like it and the reserve would pass 100,000: it is never sent.
    assert (result.end, result.passed, result.requests, sent) == ("budget", False, 2, 2)
    assert result.tokens["total"] == 60_200


def test_a_run_stops_giving_out_tasks_when_its_tokens_are_used_up(tmp_path):
    with FakeResponses() as fake:
        fake.script(*looping(10))
        results, out, _ = evaluate(tmp_path, fake, ["ex-two-bucket", "ex-wordy", "ex-poker"], budget=120_000, task_budget=100_000)
        sent = len(fake.requests)
    assert [(result.end, result.tokens["total"]) for result in results] == [("budget", 60_200), ("budget", 30_100), ("skipped", 0)]
    assert sent == 3 and sum(result.tokens["total"] for result in results) <= 120_000
    assert results[2].note == "only 29,700 tokens were left of the run's budget" and not (out / "work" / "ex-poker").exists()
    assert [task["end"] for task in json.loads((out / "result.json").read_text(encoding="utf-8"))["tasks"]] == ["budget", "budget", "skipped"]


def test_a_task_that_takes_too_long_is_stopped(tmp_path):
    with FakeResponses() as fake:
        fake.script([*say("Thinking")[:1], ("hold", "never")])
        (result,), _, _ = evaluate(tmp_path, fake, ["ex-two-bucket"], seconds=0.6)
    assert (result.end, result.passed, result.tokens["total"]) == ("timeout", False, 0)
    assert result.seconds["turn"] < 15


def test_a_question_of_the_model_is_left_unanswered_and_counted(tmp_path):
    with FakeResponses() as fake:
        fake.script(
            call("ask_user", {"questions": [{"question": "Which way?", "options": ["This", "That"]}]}, call_id="q1"),
            say("I chose myself."),
        )
        (result,), _, _ = evaluate(tmp_path, fake, ["ex-two-bucket"])
        answer = fake.requests[1]["input"][-1]
    assert (result.questions, result.approvals, result.end, result.requests) == (1, 0, "done", 2)
    assert answer["type"] == "function_call_output" and "skipped the question" in answer["output"]


def test_a_turn_that_fails_is_reported_and_the_run_goes_on(tmp_path):
    with FakeResponses() as fake:
        fake.script(http_error(400, {"error": {"message": "the request is not valid", "code": "invalid_request_error"}}), *solving())
        first, second = evaluate(tmp_path, fake, ["ex-wordy", "ex-two-bucket"])[0]
    assert (first.end, first.passed, first.requests, first.tokens["total"]) == ("failed", False, 1, 0)
    assert first.failed_requests == 1 and "the request is not valid" in first.note
    assert (second.end, second.passed) == ("done", True)


def test_a_run_stops_when_the_model_does_not_answer_at_all(tmp_path):
    refused = http_error(429, {"error": {"message": "the quota is used up", "code": "insufficient_quota"}})
    with FakeResponses() as fake:
        fake.script(refused, refused, *solving())
        results, _, _ = evaluate(tmp_path, fake, ["ex-wordy", "ex-poker", "ex-two-bucket", "ex-bowling"])
        sent = len(fake.requests)
    assert [result.end for result in results] == ["failed", "failed", "skipped", "skipped"] and sent == 2  # (a quota that ran out is not asked again)
    assert results[2].note == "the 2 tasks before it failed without an answer from the model"
    text = report.table({"name": "x", "options": {}, "about": {}, "tasks": results})
    assert "- 通过：**0 / 2**（另有 2 个未运行）" in text and "### ex-two-bucket（未过，未运行）" in text
    # One failure between tasks that were answered does not stop a run: only failures in a row do.
    with FakeResponses() as fake:
        fake.script(refused, *solving(), refused, say("Done."))
        results, _, _ = evaluate(tmp_path, fake, ["ex-wordy", "ex-two-bucket", "ex-poker", "ex-bowling"], out=tmp_path / "second")
    assert [result.end for result in results] == ["failed", "done", "failed", "done"]


def test_a_run_that_is_cut_short_has_left_what_it_had(tmp_path):
    seen: list[tuple[str, list[str], Any]] = []

    def progress(line: str) -> None:
        saved = json.loads((tmp_path / "run" / "result.json").read_text(encoding="utf-8"))
        seen.append((line, [task["id"] for task in saved["tasks"]], saved["finished"]))

    with FakeResponses() as fake:
        fake.script(*solving(), say("Done."))
        evaluate(tmp_path, fake, ["ex-two-bucket", "ex-wordy"], progress=progress)
    assert [(ids, finished) for _, ids, finished in seen] == [(["ex-two-bucket"], None), (["ex-two-bucket", "ex-wordy"], None)]
    assert seen[0][0].startswith("[1/2] ex-two-bucket: PASS (done), 6,310 tokens") and seen[0][0].endswith("run total 6,310")
    assert seen[1][0].startswith("[2/2] ex-wordy: FAIL (done), 150 tokens") and seen[1][0].endswith("run total 6,460")
    assert json.loads((tmp_path / "run" / "result.json").read_text(encoding="utf-8"))["finished"] is not None


def test_a_task_that_cannot_be_set_up_does_not_end_the_run(tmp_path):
    (tmp_path / "run" / "work" / "ex-wordy").mkdir(parents=True)  # (its folder is taken)
    with FakeResponses() as fake:
        fake.script(*solving())
        first, second = evaluate(tmp_path, fake, ["ex-wordy", "ex-two-bucket"])[0]
    assert (first.end, first.ran, first.passed, first.tokens["total"]) == ("error", True, False, 0) and "already exists" in first.note
    assert second.passed


def test_nothing_of_the_user_reaches_a_task_and_nothing_is_left_changed(tmp_path, monkeypatch):
    home = tmp_path / "home"
    (home / ".wittycode" / "skills" / "weekly-report").mkdir(parents=True)
    (home / ".wittycode" / "skills" / "weekly-report" / "SKILL.md").write_text("---\nname: weekly-report\ndescription: Write the weekly report.\n---\nWrite it.\n", encoding="utf-8")
    monkeypatch.setattr(skills, "USER_HOME", home)
    outer = tmp_path / "outer"
    outer.mkdir()
    (outer / "AGENTS.md").write_text("# Rules\n- OUTER-RULE: read docs/handoff.md first.\n", encoding="utf-8")
    before = {name: os.environ.get(name) for name in ("WITTYCODE_SETTINGS", "PATH", "PYTHONDONTWRITEBYTECODE")}
    settings = AppSettings(think_chinese=False, max_steps=7)
    during: dict[str, Any] = {}
    prepare = taskset.prepare

    def watched(task, root):
        during.update(settings=os.environ.get("WITTYCODE_SETTINGS"), python=os.environ["PATH"].split(os.pathsep)[0], bytecode=os.environ.get("PYTHONDONTWRITEBYTECODE"), home=skills.USER_HOME)
        return prepare(task, root)

    monkeypatch.setattr(taskset, "prepare", watched)
    with FakeResponses() as fake:
        fake.script(say("Nothing to do."))
        _, out, engine = evaluate(tmp_path, fake, ["ex-two-bucket"], out=outer / "runs" / "one", settings=settings, steps=12)
        instructions = fake.requests[0]["instructions"]
    # While the task ran: WittyCode's own folder and the user's home were the run's, and `python` was this interpreter.
    assert during == {"settings": str(out / "app" / "settings.json"), "python": str(Path(sys.executable).parent), "bytecode": "1", "home": out / "home"}
    assert "weekly-report" not in instructions and "OUTER-RULE" not in instructions
    assert [skill.name for skill in skills.discover(out / "work" / "ex-two-bucket")] == ["weekly-report"]  # (the user's again, after the run)
    assert {name: os.environ.get(name) for name in before} == before and skills.USER_HOME == home
    assert settings.max_steps == 7 and engine.settings is settings
    assert not (out / "app").exists()  # (the run's stand-in for WittyCode's own folder: nothing wrote there)


def test_the_step_limit_of_a_task_is_the_evaluation_s(tmp_path):
    with FakeResponses() as fake:
        fake.script(*looping(6, tokens=1000))
        (result,), _, _ = evaluate(tmp_path, fake, ["ex-two-bucket"], steps=3)
    assert (result.end, result.requests, result.passed) == ("limit", 3, False)
    assert "3" in result.note


def test_a_run_s_options_are_bounded():
    assert Options(model="m").budget == 1_000_000 and runner.HARD_BUDGET == 5_000_000
    Options(model="m", budget=5_000_000)
    for wrong in ({"budget": 5_000_001}, {"budget": 39_999}, {"task_budget": 100}, {"steps": 0}, {"steps": 101}, {"seconds": 0}, {"autonomy": "ask"}, {"model": " "}):
        with pytest.raises(ValueError):
            Options(**{"model": "m", **wrong})


def test_the_figures_of_a_request_record():
    record = {"input": 100, "cacheWrite": 40, "cacheRead": 900, "output": 60, "input_items": 5, "shared_prefix_items": 0}
    assert (metrics.request_input(record), metrics.request_total(record)) == (1040, 1100)
    assert metrics.tokens_of(None) == metrics.no_tokens() and metrics.tokens_of({"input": 7, "cacheWrite": None})["total"] == 7
    assert Price().units({"input": 100, "cacheWrite": 40, "cacheRead": 900, "output": 60}) == 100 + 50 + 90 + 300
    assert Price(read=0.05, write=2.0, output=6.0).units({"input": 0, "cacheWrite": 10, "cacheRead": 100, "output": 1}) == 31
    grows = [{"input_items": 3, "shared_prefix_items": 0}, {"input_items": 5, "shared_prefix_items": 3}, {"input_items": 7, "shared_prefix_items": 5}]
    assert metrics.prefix_breaks(grows) == 0
    again = [*grows, {"input_items": 7, "shared_prefix_items": 7, "failed": True}, {"input_items": 7, "shared_prefix_items": 7}]
    assert metrics.prefix_breaks(again) == 0  # (the same request sent again)
    rebuilt = [*grows, {"input_items": 8, "shared_prefix_items": 7, "kind": "summary"}, {"input_items": 4, "shared_prefix_items": 0}, {"input_items": 6, "shared_prefix_items": 2}]
    assert metrics.prefix_breaks(rebuilt) == 2  # (after the summary the conversation begins anew; then an earlier item was changed)
    assert metrics._covered([(0.0, 2.0), (1.0, 3.0), (5.0, 6.0), (5.5, 5.8)]) == 4.0
    kept = TaskResult.from_json({**TaskResult(id="t", passed=True, end="done").to_json(), "a_later_field": 1})
    assert (kept.id, kept.passed, kept.ran, kept.hit_rate) == ("t", True, True, None)
    assert TaskResult(id="t", tokens={"input": 100, "cacheWrite": 100, "cacheRead": 800, "output": 5, "total": 1005}).hit_rate == 0.8
    assert not TaskResult(id="t").ran and TaskResult(id="t", end="error").ran
    assert "### call shell" in metrics.transcript_text([{"type": "function_call", "name": "shell", "arguments": "{}"}, {"type": "function_call_output", "output": "x" * 5000}])


def sample(task_id: str, *, passed: bool, total: int, end: str = "done", requests: int = 4, seconds: float = 10.0) -> TaskResult:
    tokens = {"input": total // 10, "cacheWrite": 0, "cacheRead": total - total // 10 - 100, "output": 100, "total": total}
    return TaskResult(
        id=task_id, passed=passed, end=end, requests=requests, tool_calls=3, approvals=1, tokens=tokens, cost_units=Price().units(tokens),
        seconds={"turn": seconds, "requests": seconds - 2, "tools": 1.5, "other": 0.5, "first_output": 1.0, "check": 0.2},
        check="" if passed else "AssertionError: 1 != 2", note="" if end == "done" else "stopped",
    )


def write_run(folder: Path, name: str, tasks: list[TaskResult], *, model: str = MODEL) -> Path:
    runner.write_result(folder / name, Options(model=model, reasoning="low"), {"suite": "core", "commit": "abc1234", "dirty": True}, tasks, 100.0, 160.0)
    return folder / name


def test_a_run_is_a_table_a_person_can_read(tmp_path):
    run = write_run(tmp_path, "one", [sample("ex-a", passed=True, total=50_000), sample("ex-b", passed=False, total=80_000, end="budget"), TaskResult(id="ex-c", note="only 100 tokens were left")])
    text = report.table(report.load(run))
    assert "- 模型：`gpt-6.1-sol`，推理档位：low，权限档：code，任务集：core" in text
    assert "- Harness 版本：abc1234（工作树有未提交改动）" in text
    assert "- 通过：**1 / 2**（另有 1 个未运行）" in text and "- Token 合计：**130,000**（预算 1,000,000）" in text
    rows = [line for line in text.splitlines() if line.startswith("| ex-")]
    assert rows[0].startswith("| ex-a | 通过 | 正常结束 | 4 | 3 | 1 | 50.0K | 5.0K / 0.0K / 44.9K / 0.1K | 90.0% |")
    assert rows[1].startswith("| ex-b | 未过 | Token 用尽 |") and rows[2].startswith("| ex-c | - | 未运行 |")
    assert "### ex-b（未过，Token 用尽）" in text and "AssertionError: 1 != 2" in text and "### ex-a" not in text
    assert report.totals(report.load(run)["tasks"])["seconds"]["turn"] == 20.0
    # A task whose check passes although its turn was stopped is worth a look too: it is listed with what the harness said.
    late = report.table({"name": "x", "options": {}, "about": {}, "tasks": [sample("ex-d", passed=True, total=10_000, end="limit")]})
    assert "- 通过：**1 / 1**" in late and "### ex-d（通过，步数上限）" in late and "stopped" in late
    for wrong in ('{"schema": 99, "tasks": []}', "not json"):
        (tmp_path / "bad.json").write_text(wrong, encoding="utf-8")
        with pytest.raises(report.ReportError):
            report.load(tmp_path / "bad.json")


def test_two_runs_are_compared_task_by_task(tmp_path, capsys):
    before = write_run(tmp_path, "before", [sample("ex-a", passed=False, total=100_000, requests=10, seconds=40), sample("ex-b", passed=True, total=60_000), sample("ex-only-before", passed=True, total=9_000)])
    after = write_run(tmp_path, "after", [sample("ex-a", passed=True, total=50_000, requests=5, seconds=20), sample("ex-b", passed=True, total=60_000), TaskResult(id="ex-only-before")])
    assert cli.main(["compare", str(before), str(after / "result.json"), "--out", str(tmp_path / "compare.md")]) == 0
    text = capsys.readouterr().out
    assert text.strip() == (tmp_path / "compare.md").read_text(encoding="utf-8").strip()
    assert "- 两边都跑了的任务：2 个（A 跑了 3 个，B 跑了 2 个；只比较共同的）" in text
    assert "| ex-a | 未过 → 通过 | 100.0K | 50.0K | -50% |" in text and "| 10 → 5 | 40 → 20 |" in text
    assert "| **合计** | 1 → 2 | 160.0K | 110.0K | -31% |" in text
    assert "结果变了的任务：ex-a（未过 → 通过）。" in text and "ex-only-before" not in text
    assert cli.main(["compare", str(before), str(tmp_path / "missing")]) == 2


def test_a_real_run_of_an_api_model_from_the_command_line(tmp_path, monkeypatch, capsys):
    from desktop.core.providers import ProviderStore
    from tests.llm_support import FakeApi

    monkeypatch.setenv("EVAL_TEST_KEY", "sk-" + "eval-test")
    done = [{"id": "c", "choices": [{"index": 0, "delta": {"content": "Nothing to change."}, "finish_reason": "stop"}]},
            {"id": "c", "choices": [], "usage": {"prompt_tokens": 900, "completion_tokens": 12, "prompt_tokens_details": {"cached_tokens": 300}}}, "[DONE]"]
    with FakeApi() as api:
        api.script(done, done)
        models = tmp_path / "models.json"
        entry = {"model": "demo-chat", "displayName": "Demo Chat", "baseUrl": api.base_url, "apiKey": "${EVAL_TEST_KEY}", "provider": "generic-chat-completion-api", "maxContextLimit": 64_000}
        models.write_text(json.dumps({"customModels": [entry]}), encoding="utf-8")
        model = ProviderStore(models_file=models, sidecar=tmp_path / "providers.json").api_models()[0].id
        # WittyCode's own folder (here the test's) names this provider otherwise: a run must not read it.
        from desktop.core.settings import default_settings_path

        named = {"version": 1, "providers": [{"id": "the-users-own", "name": "Mine", "kind": "generic-chat-completion-api", "baseUrl": api.base_url}]}
        default_settings_path().parent.mkdir(parents=True, exist_ok=True)
        default_settings_path().with_name("providers.json").write_text(json.dumps(named), encoding="utf-8")
        assert ProviderStore(models_file=models).api_models()[0].id.startswith("api:the-users-own:") and model.startswith("api:auto-")
        arguments = ["run", "--model", model, "--models", str(models), "--task", "probe-gbk-csv", "--task", "probe-long-output", "--out", str(tmp_path / "runs"),
                     "--account", str(tmp_path / "nobody"), "--name", "try", "--budget", "300k", "--set", "think_chinese=false", "--set", "chatgpt_compact_at=70"]
        assert cli.main(arguments) == 0
        posts = api.posts
    printed = capsys.readouterr().out
    (out,) = (tmp_path / "runs").iterdir()
    assert out.name.endswith("-try") and (out / "summary.md").read_text(encoding="utf-8") in printed
    assert "[2/2] probe-long-output: FAIL (done), 912 tokens" in printed and "- 通过：**0 / 2**" in printed, [line for line in printed.splitlines() if "probe" in line or "通过" in line]
    saved = report.load(out)
    assert saved["about"]["suite"] == "custom" and saved["about"]["settings"] == {"think_chinese": False, "chatgpt_compact_at": 70}
    assert [(task.id, task.end, task.tokens["total"], task.tokens["cacheRead"]) for task in saved["tasks"]] == [("probe-gbk-csv", "done", 912, 300), ("probe-long-output", "done", 912, 300)]
    assert len(posts) == 2 and posts[0][1]["authorization"] == "Bearer sk-" + "eval-test"
    assert "螺丝刀" in json.dumps(posts[0][2], ensure_ascii=False)  # (the task's message reached the provider as it is)
    assert not (tmp_path / "nobody").exists()  # (an API model needs no ChatGPT sign-in: nothing is looked for or left there)
    # A model the file does not have is refused before anything is sent.
    assert cli.main(["run", "--model", "api:none:custom:x-0", "--models", str(models), "--suite", "smoke", "--out", str(tmp_path / "runs")]) == 2
    assert "is not in the models file" in capsys.readouterr().err
    assert cli.main(["run", "--model", "gpt-6-luna", "--reasoning", "max", "--suite", "smoke", "--out", str(tmp_path / "runs"), "--account", str(tmp_path / "nobody")]) == 2
    assert "no usable ChatGPT sign-in" in capsys.readouterr().err


def test_the_pilot_refuses_what_names_a_file_outside_the_task_s_folder(tmp_path):
    outside = tmp_path / "outside.txt"
    outside.write_text("the user's own file\n", encoding="utf-8")
    with FakeResponses() as fake:
        fake.script(
            call("read_file", {"file_path": str(outside)}, call_id="c1", item_id="fc_1"),
            call("create_file", {"file_path": str(tmp_path / "new.txt"), "content": "x"}, call_id="c2", item_id="fc_2"),
            call("shell", {"command": "python --version"}, call_id="c3", item_id="fc_3"),
            say("Done."),
        )
        (result,), _, _ = evaluate(tmp_path, fake, ["ex-two-bucket"])
        outputs = [item["output"] for item in fake.requests[-1]["input"] if item["type"] == "function_call_output"]
    assert (result.refusals, result.approvals, result.tool_calls, result.changed) == (2, 0, 3, [])  # (a read-only command is not asked about)
    assert "declined" in outputs[0] and "declined" in outputs[1] and "Python 3" in outputs[2]
    assert not (tmp_path / "new.txt").exists() and outside.read_text(encoding="utf-8") == "the user's own file\n"


def test_a_task_the_evaluation_cannot_read_is_an_error_with_its_tokens_counted(tmp_path, monkeypatch):
    def broken(controller, price):
        raise KeyError("a record without its figures")

    with FakeResponses() as fake:
        fake.script(say("Done.", used=usage(700, 30)), *solving())
        monkeypatch.setattr(metrics, "measure", broken)
        first = evaluate(tmp_path, fake, ["ex-wordy"])[0][0]
        monkeypatch.undo()
        second = evaluate(tmp_path, fake, ["ex-two-bucket"], out=tmp_path / "second")[0][0]
    assert (first.end, first.passed, first.tokens["total"]) == ("error", False, 730) and "a record without its figures" in first.note
    assert second.passed


def test_a_model_or_an_effort_the_account_does_not_have_is_refused_before_any_request(tmp_path):
    async def go() -> list[str]:
        with FakeResponses() as fake:
            engine = make_engine(tmp_path, fake)
            try:
                found = [await runner.preflight(engine, MODEL), await runner.preflight(engine, MODEL, "low"), await runner.preflight(engine, MODEL, "max"), await runner.preflight(engine, "gpt-0")]
                assert fake.requests == []
                return found
            finally:
                await engine.close_all()

    usable, low, effort, model = asyncio.run(go())
    assert (usable, low) == ("", "")
    assert effort == "gpt-6.1-sol has no reasoning effort 'max' (it has: low, medium, high)"
    assert model.startswith("gpt-0 is not one of the plan's models (gpt-6.1-sol, gpt-6-luna")


def test_a_model_that_rewrites_the_tests_is_not_given_the_task(tmp_path):
    cheat = "import unittest\n\n\nclass Fine(unittest.TestCase):\n    def test_fine(self):\n        pass\n"
    with FakeResponses() as fake:
        fake.script(
            call("read_file", {"file_path": "two_bucket_test.py"}, call_id="c1", item_id="fc_1"),
            call("create_file", {"file_path": "two_bucket_test.py", "content": cheat}, call_id="c2", item_id="fc_2"),
            say("Every test passes."),
        )
        (result,), out, _ = evaluate(tmp_path, fake, ["ex-two-bucket"])
    assert (result.passed, result.end) == (False, "done")
    assert result.changed == ["two_bucket_test.py"]  # (what the agent did is kept as it was found, before the tests are put back)
    assert result.check.startswith("Protected files were changed and put back before the check: two_bucket_test.py.")
    assert "class TwoBucketTest" in (out / "work" / "ex-two-bucket" / "two_bucket_test.py").read_text(encoding="utf-8")


def conversation(*, requests: list[dict[str, Any]], turns: list[Any], tools: list[Any] = (), blocks: list[Any] = (), compaction: dict[str, Any] | None = None, status: str = "done"):
    """What ``metrics.measure`` reads of a controller, made by hand."""
    from types import SimpleNamespace as N

    from desktop.core.usage import Tokens

    turn = N(tools={str(n): tool for n, tool in enumerate(tools)}, blocks=list(blocks), duration=20.0, started_at=100.0, first_output_at=103.5, status=N(value=status))
    saved = N(requests=requests, turns=turns, compaction=compaction or {})
    return N(saved=saved, usage=Tokens(500, 300, 200, 4000), transcript=N(turns=[turn]))


def test_the_figures_of_a_conversation_with_a_summary_a_retry_and_tools_at_the_same_time():
    from types import SimpleNamespace as N

    requests = [
        {"input": 300, "cacheRead": 0, "cacheWrite": 200, "output": 50, "seconds": 2.0, "input_items": 1, "shared_prefix_items": 0},
        {"seconds": 1.0, "failed": True, "input_items": 3, "shared_prefix_items": 1},
        {"input": 100, "cacheRead": 1500, "output": 100, "seconds": 3.0, "input_items": 3, "shared_prefix_items": 3},
        {"input": 50, "cacheRead": 1600, "output": 120, "seconds": 4.0, "input_items": 4, "shared_prefix_items": 3, "kind": "summary"},
        {"input": 50, "cacheRead": 900, "output": 30, "seconds": 1.5, "input_items": 2, "shared_prefix_items": 0},
    ]
    tools = [N(name="Read", started_at=105.0, finished_at=106.0, is_error=False), N(name="Read", started_at=105.5, finished_at=107.0, is_error=True), N(name="Execute", started_at=110.0, finished_at=None, is_error=False)]
    turns = [N(finished=120.0, status="limit", error="", note="stopped at the step limit")]
    found = metrics.measure(conversation(requests=requests, turns=turns, tools=tools, compaction={"upto": 3}))
    assert (found["end"], found["note"]) == ("limit", "stopped at the step limit")
    assert (found["requests"], found["summaries"], found["failed_requests"], found["prefix_breaks"]) == (4, 1, 1, 1)
    assert (found["tool_calls"], found["tool_errors"], found["tools"], found["compacted"]) == (3, 1, {"Execute": 1, "Read": 2}, True)
    assert found["tokens"] == {"input": 500, "cacheWrite": 200, "cacheRead": 4000, "output": 300, "total": 5000}
    assert found["peak_context"] == 1600 and found["cost_units"] == 500 + 250 + 400 + 1500
    assert found["seconds"] == {"turn": 20.0, "requests": 11.5, "tools": 2.0, "other": 6.5, "first_output": 3.5, "check": 0.0}
    # A turn of which nothing came is not in the saved conversation: the page's turn says how it ended and why.
    failed = metrics.measure(conversation(requests=[], turns=[], blocks=[N(kind="notice", text="x"), N(kind="error", message="the key was refused")], status="failed"))
    assert (failed["end"], failed["note"], failed["requests"], failed["peak_context"]) == ("failed", "the key was refused", 0, 0)
    unfinished = metrics.measure(conversation(requests=[], turns=[N(finished=None, status="running", error="", note="")], status="interrupted"))
    assert unfinished["end"] == "interrupted"


def test_a_task_s_own_limits_come_before_the_run_s(tmp_path):
    import dataclasses

    small = dataclasses.replace(TASKS["ex-two-bucket"], budget=70_000)
    short = dataclasses.replace(TASKS["ex-wordy"], steps=2)

    async def go():
        with FakeResponses() as fake:
            fake.script(*looping(10), *looping(6, tokens=1000))
            engine = make_engine(tmp_path / "engine", fake, settings=AppSettings(think_chinese=False))
            try:
                return await runner.run_tasks(engine, [small, short], Options(model=MODEL, seconds=20.0), tmp_path / "run")
            finally:
                await engine.close_all()

    first, second = asyncio.run(go())
    assert (first.end, first.requests, first.tokens["total"]) == ("budget", 1, 30_100)  # (a second request could pass 70,000)
    assert (second.end, second.requests) == ("limit", 2)


def test_a_service_that_does_not_report_its_tokens_ends_the_run(tmp_path):
    # Without the figures a budget cannot be kept: the task is stopped after the first answer, and no other is started.
    with FakeResponses() as fake:
        fake.script(*[call("list_dir", {"directory_path": "."}, call_id=f"c{n}", item_id=f"fc_{n}", used={}) for n in range(6)])
        results, _, _ = evaluate(tmp_path, fake, ["ex-two-bucket", "ex-wordy", "ex-poker"])
        sent = len(fake.requests)
    assert [(result.end, result.requests, result.tokens["total"]) for result in results] == [("unmetered", 1, 0), ("skipped", 0, 0), ("skipped", 0, 0)]
    assert sent == 1 and results[1].note == "the model's service reported no token usage, so the run could not be kept inside its budget"
    assert "| ex-two-bucket | 未过 | 无用量数据 |" in report.table({"name": "x", "options": {}, "about": {}, "tasks": results})


def test_a_request_that_failed_and_was_sent_again_is_part_of_the_task(tmp_path):
    busy = http_error(503, {"error": {"message": "the service is overloaded", "code": "server_error"}})
    with FakeResponses() as fake:
        fake.script(busy, *solving())
        (result,), _, _ = evaluate(tmp_path, fake, ["ex-two-bucket"])
    # The failed request has no token figures of its own: that is not a service that does not report them.
    assert (result.end, result.passed, result.requests, result.failed_requests) == ("done", True, 5, 1)
    assert result.prefix_breaks == 0 and result.tokens["total"] == 6310
    assert result.seconds["other"] >= 1.0  # (the pause before asking again is neither the model's time nor a tool's)
