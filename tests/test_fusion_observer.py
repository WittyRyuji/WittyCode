"""Facts and replay-safe judgement for a single Fusion expert."""
import json
from dataclasses import replace

import pytest

from desktop.core.fusion.observer import (
    CONFIDENCE, REPEAT_LIMIT, STALL_ROUNDS, TARGET_RATE, Event, Watch, agreed,
    false_report_rate, judgement, judgement_prompt, parse_verdict, precondition, replay,
)

READ = Event("read_file", "read-call", "read-result", "read")
FAILED = replace(READ, error="missing input")
YES = '{"stuck": true, "confidence": 0.9}'


def test_watch_timeout_on_tick_is_strict_and_once_only():
    watch = Watch([], seconds=5, started=10)
    assert watch.tick(15) is None
    report = watch.tick(15.01)
    assert (report.kind, report.category) == ("timeout", "fact")
    assert report.detail.endswith(".")
    assert watch.tick(30) is None
    assert watch.observe(replace(READ, at=40)) is None
    assert Watch([]).tick(40) is None


def test_watch_timeout_is_also_checked_on_finished_calls():
    watch = Watch([], seconds=5, started=10)
    assert watch.observe(replace(READ, at=15)) is None
    assert watch.observe(replace(READ, at=16)).kind == "timeout"
    assert watch.tick(17) is None


def test_watch_steps_count_all_calls_even_failures_and_never_ticks():
    watch = Watch([], steps=2)
    for now in range(5):
        assert watch.tick(now) is None
    assert watch.observe(READ) is None
    assert watch.observe(replace(FAILED, result="failure")) is None
    report = watch.observe(READ)
    assert (report.kind, report.category) == ("over_budget", "fact")
    assert watch.observe(replace(READ, call="new-call")) is None
    unlimited = Watch([])
    assert all(unlimited.observe(replace(READ, call=str(i))) is None for i in range(20))


def test_watch_declared_paths_match_exactly_or_at_a_directory_boundary():
    watch = Watch(["src/a", "docs/file.md"])
    edit = Event("edit_file", "edit-call", "edit-result", "edit", ("src/a", "src/a/file.py", "docs/file.md"))
    assert watch.observe(edit) is None
    assert watch.observe(replace(edit, call="backslash", written=(r"src\a\file.py",))) is None
    report = watch.observe(replace(edit, written=("src/a", "src/ab/file.py")))
    assert (report.kind, report.category) == ("out_of_scope", "fact")
    assert "src/ab/file.py" in report.detail
    assert watch.observe(replace(edit, call="new-edit", written=("docs/other.md",))) is None


@pytest.mark.parametrize("paths", [(), ("src/a",)])
def test_watch_read_only_counts_writes_even_from_exec(paths):
    watch = Watch(paths, read_only=True)
    assert watch.observe(READ) is None
    report = watch.observe(Event("shell", "exec-call", "exec-error", "exec", ("src/a",), error="command failed"))
    assert (report.kind, report.category) == ("out_of_scope", "fact")
    assert "read-only" in report.detail
    assert watch.observe(Event("edit_file", "edit-call", "edit-result", "edit", ("src/b",), changed=True)) is None


def test_watch_empty_scope_allows_writes_anywhere_inside_project():
    watch = Watch([])
    assert watch.observe(replace(READ, kind="exec", written=("docs/file.md", "src/a/file.py"))) is None
    assert watch.observe(replace(READ, kind="exec", written=("../other/file.md",))).kind == "out_of_scope"


def test_watch_repeat_requires_consecutive_equal_calls_and_results_and_reports_once():
    assert REPEAT_LIMIT == 3
    watch = Watch([])
    for event in (READ, READ, replace(READ, result="new-result"), READ, READ, replace(READ, call="new-call")):
        assert watch.observe(event) is None
    for _ in range(REPEAT_LIMIT - 1):
        assert watch.observe(READ) is None
    report = watch.observe(READ)
    assert (report.kind, report.category) == ("repeat", "fact")
    assert watch.observe(READ) is None
    for _ in range(REPEAT_LIMIT):
        assert watch.observe(FAILED) is None
    for _ in range(REPEAT_LIMIT):
        assert watch.observe(replace(FAILED, result="failure")) is None


def test_watch_preserves_all_coincident_facts_for_subsequent_calls():
    watch = Watch([], steps=2, seconds=5, read_only=True)
    assert watch.observe(READ) is None
    assert watch.observe(READ) is None
    report = watch.observe(replace(READ, at=6, written=("src/a",)))
    reports = [report] + [watch.tick(6) for _ in range(3)]
    assert {item.kind for item in reports} == {"timeout", "over_budget", "out_of_scope", "repeat"}
    assert watch.tick(6) is None


def test_precondition_requires_only_the_last_rounds_to_be_unchanged_with_one_error():
    assert STALL_ROUNDS == 4
    stuck = [FAILED] * STALL_ROUNDS
    assert not precondition([])
    assert not precondition(stuck[:-1])
    assert not precondition(stuck, rounds=0)
    assert not precondition(stuck, rounds=-1)
    assert precondition([FAILED], rounds=1)
    assert precondition([replace(READ, changed=True), *stuck])
    assert not precondition([READ] * STALL_ROUNDS)
    assert not precondition([*stuck[:-1], replace(FAILED, error="different error")])
    assert not precondition([*stuck[:-1], replace(FAILED, changed=True)])


def test_prompt_shows_only_task_and_selected_call_metadata():
    event = Event("read_file", "call-digest", "result-digest", "read", ("private.txt",), 1234, "missing", True)
    events = [replace(event, name=f"tool-{i}") for i in range(15)]
    prompt = judgement_prompt("Check the input.", events)
    evidence = json.loads(prompt.split("\n", 1)[1])
    assert evidence == {
        "task": "Check the input.",
        "calls": [{"name": f"tool-{i}", "kind": "read", "error": "missing", "changed": True}
                  for i in range(3, 15)],
    }
    for absent in ("call-digest", "result-digest", "private.txt", "1234"):
        assert absent not in prompt
    assert len(json.loads(judgement_prompt("", events, last=2).split("\n", 1)[1])["calls"]) == 2
    assert json.loads(judgement_prompt("", events, last=0).split("\n", 1)[1])["calls"] == []


@pytest.mark.parametrize("text, expected", [
    (YES, (True, 0.9)), (' {"confidence": 0, "stuck": false} ', (False, 0.0)),
    ('{"stuck": true, "confidence": 1}', (True, 1.0)),
])
def test_parse_verdict_accepts_one_strict_object(text, expected):
    assert parse_verdict(text) == expected


@pytest.mark.parametrize("text", [
    "", None, "null", "[]", "{}", YES + "\n", YES + " extra", "```json\n" + YES + "\n```",
    '{"stuck": true,\n"confidence": 0.9}', '{"stuck": 1, "confidence": 0.9}',
    '{"stuck": true, "confidence": true}', '{"stuck": true, "confidence": "0.9"}',
    '{"stuck": true, "confidence": -0.01}', '{"stuck": true, "confidence": 1.01}',
    '{"stuck": true, "confidence": NaN}', '{"stuck": true, "confidence": Infinity}',
    '{"stuck": true, "confidence": 1e999}', '{"stuck": true, "confidence": 0.9, "why": "loop"}',
    '{"stuck": true, "confidence": 1.00000000000000000001}', '{"stuck": true, "confidence": -1e-999}',
    '{"stuck": false, "stuck": true, "confidence": 0.9}',
])
def test_parse_verdict_rejects_bad_or_ambiguous_answers(text):
    assert parse_verdict(text) is None


def test_agreement_requires_two_affirmative_verdicts_at_threshold():
    assert CONFIDENCE == 0.9
    assert agreed((True, CONFIDENCE), (True, 1.0))
    assert not agreed((True, 0.89), (True, 1.0))
    assert not agreed((True, 1.0), (False, 1.0))
    assert not agreed(None, (True, 1.0))
    assert not agreed((True, 1.0), None)
    assert agreed((True, 0.5), (True, 0.5), threshold=0.5)


@pytest.mark.parametrize("answers, reported", [
    ((YES, YES), True), ((YES, '{"stuck": true, "confidence": 0.89}'), False),
    ((YES, '{"stuck": false, "confidence": 1}'), False), (("bad", YES), False),
])
def test_judgement_asks_exactly_twice_only_after_precondition(answers, reported):
    prompts = []

    def ask(prompt):
        prompts.append(prompt)
        return answers[len(prompts) - 1]

    assert judgement("Fix the input.", [FAILED] * (STALL_ROUNDS - 1), ask) is None
    assert prompts == []
    watch = Watch([])
    for event in [FAILED] * STALL_ROUNDS:
        report = watch.observe(event)
        assert report is None or report.category == "fact"
    report = judgement("Fix the input.", [FAILED] * STALL_ROUNDS, ask)
    assert len(prompts) == 2 and prompts[0] == prompts[1]
    if reported:
        assert (report.kind, report.category) == ("stuck", "judgement")
    else:
        assert report is None


def test_replay_keeps_only_judgement_reports_and_scores_against_all_runs():
    runs = {"reported": [FAILED] * STALL_ROUNDS, "no-precondition": [READ], "rejected": [FAILED] * STALL_ROUNDS}
    prompted = []

    def ask(prompt):
        task = json.loads(prompt.split("\n", 1)[1])["task"]
        prompted.append(task)
        return YES if task == "reported" else '{"stuck": false, "confidence": 1}'

    reports = replay(runs, ask, task=str)
    assert list(reports) == ["reported"]
    assert reports["reported"].category == "judgement"
    assert prompted == ["reported", "reported", "rejected", "rejected"]
    assert false_report_rate(reports, {"reported", "rejected"}, len(runs)) == pytest.approx(1 / 3)
    assert false_report_rate(["reported", "reported"], ["reported"], 100) == TARGET_RATE == 0.01
    assert false_report_rate(reports, {"no-precondition"}, len(runs)) == 0.0
    assert false_report_rate({}, (), 0) == 0.0
