"""Task-local Fusion counts and summaries from JSON-ready snapshots."""
import json

import pytest

from desktop.core.fusion.metrics import Recorder, TaskMetrics, Usage, load, summary


@pytest.mark.parametrize("method, args, kwargs", [
    ("route", ("self",), {}), ("expert", ("Coder",), {"tokens": 1, "seconds": 1}),
    ("main", (1, 1), {}), ("runner", (True,), {}), ("sampler", (False,), {}),
    ("misroute", (), {}), ("observer", (), {}), ("compaction", (), {}),
    ("drive", ({"unrated": 1},), {}), ("accept", (True,), {}), ("finish", (), {}),
])
def test_recorder_operations_need_an_open_task(method, args, kwargs):
    recorder = Recorder()
    with pytest.raises(ValueError):
        getattr(recorder, method)(*args, **kwargs)
    recorder.begin("turn-1")
    assert recorder.finish()["task_id"] == "turn-1"
    with pytest.raises(ValueError):
        getattr(recorder, method)(*args, **kwargs)


def test_begin_rejects_an_empty_id_or_an_open_task_and_finish_resets_state():
    recorder = Recorder()
    with pytest.raises(ValueError):
        recorder.begin(" ")
    recorder.begin("turn-1")
    recorder.route("self")
    with pytest.raises(ValueError):
        recorder.begin("turn-rejected")
    first = recorder.finish()
    assert first["task_id"] == "turn-1"
    recorder.begin("turn-2")
    assert load(recorder.finish()) == TaskMetrics()
    assert first["destination"] == "self"


def test_recorder_json_round_trip_preserves_usage_and_counts():
    recorder = Recorder()
    recorder.begin("turn-1")
    recorder.route("split")
    recorder.expert("Coder", tokens=11, seconds=2.5, first_request_tokens=5)
    recorder.expert("Coder", tokens=0, seconds=1.5, first_request_tokens=0)
    recorder.expert("Runner", tokens=None, seconds=3)
    recorder.expert("Runner", tokens=7, seconds=1, first_request_tokens=2)
    recorder.expert("Runner", tokens=None, seconds=0)
    recorder.main(0, 0.5)
    recorder.main(None, 1.5)
    recorder.runner(False)
    recorder.runner(True)
    recorder.sampler(False)
    recorder.sampler(True)
    recorder.sampler(False)
    recorder.misroute()
    recorder.observer()
    recorder.observer(wrong=True)
    recorder.compaction()
    recorder.compaction()
    recorder.drive({"shared_hits": 2, "out_of_use": 1, "above_qk": 2, "rating_-1": 1, "rating_0": 1})
    recorder.drive({"shared_hits": 1, "rating_1": 2, "unrated": 3})
    recorder.accept(False)
    raw = json.loads(json.dumps(recorder.finish(), allow_nan=False))
    assert raw["task_id"] == "turn-1"
    assert load(raw) == TaskMetrics(
        destination="split",
        experts={"Coder": Usage(2, 11, 4.0, 5, 0), "Runner": Usage(3, None, 4.0, None, 2)},
        main=Usage(2, None, 2.0, None, 1),
        runner_first_pass=False,
        sampler_blocking=True,
        misroutes=1,
        observer_reports=2,
        observer_wrong=1,
        compactions=2,
        drive={"shared_hits": 3, "out_of_use": 1, "above_qk": 2, "rating_-1": 1,
               "rating_0": 1, "rating_1": 2, "unrated": 3},
        accepted=False,
    )


def test_known_zero_tokens_and_first_request_missing_are_independent_of_total():
    recorder = Recorder()
    recorder.begin("turn-1")
    recorder.expert("Coder", tokens=0, seconds=0, first_request_tokens=0)
    recorder.expert("Coder", tokens=0, seconds=0)
    recorder.expert("Runner", tokens=None, seconds=0, first_request_tokens=0)
    recorder.main(0, 0)
    recorder.runner(True)
    recorder.runner(False)
    recorder.sampler(False)
    task = load(recorder.finish())
    assert task.main == Usage(1, 0, 0.0, None, 0)
    assert task.experts == {"Coder": Usage(2, 0, 0.0, None, 0), "Runner": Usage(1, None, 0.0, 0, 1)}
    assert task.runner_first_pass is True
    assert task.sampler_blocking is False


@pytest.mark.parametrize("method, args, kwargs", [
    ("route", ("other",), {}), ("expert", ("",), {"tokens": 1, "seconds": 0}),
    ("expert", ("Coder",), {"tokens": -1, "seconds": 0}),
    ("expert", ("Coder",), {"tokens": 1, "seconds": 0, "first_request_tokens": True}),
    ("main", (True, 0), {}), ("main", (0, -1), {}), ("main", (0, float("inf")), {}),
    ("runner", (1,), {}), ("sampler", (None,), {}), ("observer", (1,), {}),
    ("drive", ({"unrated": 1, "above_qk": -1},), {}), ("accept", ("yes",), {}),
])
def test_invalid_measurements_do_not_partly_mutate_the_task(method, args, kwargs):
    recorder = Recorder()
    recorder.begin("turn-1")
    with pytest.raises(ValueError):
        getattr(recorder, method)(*args, **kwargs)
    assert load(recorder.finish()) == TaskMetrics()


@pytest.mark.parametrize("raw", [None, 5, [], "{}", {}, {"destination": "coder"}])
def test_load_discards_non_snapshots(raw):
    assert load(raw) is None


@pytest.mark.parametrize("damage", [
    {"destination": "other"}, {"experts": []}, {"experts": {"Coder": {}}},
    {"runner_first_pass": 1}, {"sampler_blocking": "false"}, {"accepted": 0},
    {"compactions": -1}, {"misroutes": True}, {"observer_wrong": 1},
    {"drive": {"unrated": "1"}}, {"drive": {1: 2}},
    {"main": {"runs": 1, "seconds": float("nan"), "tokens": 0, "unknown": 0, "first_request_tokens": None}},
    {"main": {"runs": 1, "seconds": 1, "tokens": None, "unknown": 0, "first_request_tokens": None}},
    {"main": {"runs": 1, "seconds": 1, "tokens": None, "unknown": 2, "first_request_tokens": None}},
    {"main": {"runs": 0, "seconds": 0, "tokens": 1, "unknown": 0, "first_request_tokens": None}},
])
def test_load_discards_damaged_fields(damage):
    recorder = Recorder()
    recorder.begin("turn-1")
    raw = recorder.finish()
    assert load(raw | damage) is None


def test_summary_reports_every_measure_with_failed_costs_included():
    recorder = Recorder()
    recorder.begin("turn-1")
    recorder.main(7, 1)
    unrouted = recorder.finish()
    recorder.begin("turn-2")
    recorder.route("self")
    recorder.main(13, 3)
    recorder.accept(True)
    small = recorder.finish()
    recorder.begin("turn-3")
    recorder.route("coder")
    recorder.main(5, 2)
    recorder.expert("Coder", tokens=11, seconds=4)
    recorder.runner(False)
    recorder.runner(True)
    recorder.misroute()
    recorder.misroute()
    recorder.observer()
    recorder.observer(wrong=True)
    recorder.drive({"rating_-1": 1, "rating_0": 1, "rating_1": 2, "unrated": 3})
    recorder.accept(False)
    coder = recorder.finish()
    recorder.begin("turn-4")
    recorder.route("split")
    recorder.main(17, 5)
    recorder.expert("Coder", tokens=19, seconds=6)
    recorder.expert("Runner", tokens=23, seconds=7)
    recorder.runner(True)
    recorder.misroute()
    recorder.drive({"rating_-1": 1, "rating_1": 1, "unrated": 2})
    recorder.accept(True)
    split = recorder.finish()
    assert summary([unrouted, small, coder, split, {"damaged": True}]) == {
        "tasks": 4,
        "accepted": 2,
        "tokens_per_accepted": 47.5,
        "seconds_per_accepted": 14.0,
        "destinations": {"": 1, "self": 1, "coder": 1, "split": 1},
        "unrouted": {"tokens": 7, "seconds": 1.0},
        "self": {"tokens": 13, "seconds": 3.0},
        "runner_first_pass_rate": 0.5,
        "misroutes": 3,
        "observer_false_report_rate": 1 / 3,
        "rating_shares": {"-1": 2 / 6, "0": 1 / 6, "1": 3 / 6},
        "unrated": 5,
    }


def test_summary_propagates_unknown_tokens_to_affected_costs_not_time():
    recorder = Recorder()
    recorder.begin("turn-1")
    recorder.main(None, 2)
    unrouted = recorder.finish()
    recorder.begin("turn-2")
    recorder.route("self")
    recorder.main(0, 1)
    recorder.accept(True)
    small = recorder.finish()
    result = summary([unrouted, small])
    assert result["tokens_per_accepted"] is None
    assert result["seconds_per_accepted"] == 3.0
    assert result["unrouted"] == {"tokens": None, "seconds": 2.0}
    assert result["self"] == {"tokens": 0, "seconds": 1.0}


def test_summary_zero_cases():
    assert summary([]) == {
        "tasks": 0, "accepted": 0, "tokens_per_accepted": 0.0, "seconds_per_accepted": 0.0,
        "destinations": {"": 0, "self": 0, "coder": 0, "split": 0},
        "unrouted": {"tokens": 0, "seconds": 0.0}, "self": {"tokens": 0, "seconds": 0.0},
        "runner_first_pass_rate": 0.0, "misroutes": 0, "observer_false_report_rate": 0.0,
        "rating_shares": {"-1": 0.0, "0": 0.0, "1": 0.0}, "unrated": 0,
    }
    recorder = Recorder()
    recorder.begin("turn-1")
    recorder.route("self")
    recorder.main(None, 5)
    recorder.accept(False)
    result = summary([recorder.finish()])
    assert result["accepted"] == 0
    assert result["tokens_per_accepted"] == result["seconds_per_accepted"] == 0.0
    assert result["self"] == {"tokens": None, "seconds": 5.0}
