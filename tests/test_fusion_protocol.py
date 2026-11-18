"""The fixed gates reject malformed model calls before runtime work begins."""
import pytest

from desktop.core.fusion.protocol import (
    BRIEF_PARAMETERS, DELEGATE_DECLARATION, REPORT_DECLARATION, REPORT_PARAMETERS, REPORT_TOKENS,
    ROUTE_DECLARATION, ROUTE_PARAMETERS, Route, checks_after, parse_brief, parse_report, parse_route,
    required_checks, risk_signals, route_refusal, settle_ratings,
)
from desktop.core.harness.compact import estimate_text
from desktop.core.harness.tools import validate_arguments


BRIEF = {
    "workstream": "Login flow", "identity": "Coder", "goal": "Handle expired sessions.",
    "constraints": "Keep the existing interface.", "acceptance": "Run the session tests.",
    "paths": ["desktop/core/session.py"], "budget": {"steps": 12, "seconds": 90},
}
REPORT = {
    "status": "done", "summary": "Expired sessions are handled.", "evidence": ["session tests: passed"],
    "notes": ["Use the test clock."], "ratings": [{"record": "clock", "score": 1, "reason": "Avoided wall time."}],
}
ROUTE = {"destination": "coder", "reason": "One coherent change.", "verification": "runner"}


def test_valid_brief_and_rendered_text():
    brief, problem = parse_brief({**BRIEF, "workstream": " Login flow ", "resume": True})
    assert not problem and brief is not None
    assert brief.workstream == "Login flow"
    assert brief.identity == "Coder"
    assert brief.paths == ("desktop/core/session.py",)
    assert brief.budget == {"steps": 12, "seconds": 90}
    assert brief.resume and not brief.read_only
    assert brief.text() == (
        "Goal:\nHandle expired sessions.\n\nConstraints:\nKeep the existing interface.\n\n"
        "Acceptance criteria:\nRun the session tests.\n\nBudget: 12 steps, 90 seconds"
    )


@pytest.mark.parametrize("identity, read_only", [("Coder", True), ("Runner", False), ("QualitySampler", True)])
def test_valid_brief_without_write_paths(identity, read_only):
    brief, problem = parse_brief({**BRIEF, "identity": identity, "paths": [], "read_only": read_only,
                                  "budget": {"seconds": 1}})
    assert not problem and brief is not None
    assert brief.paths == () and brief.read_only is read_only and not brief.resume
    assert brief.text().endswith("Budget: 1 second")


def test_brief_allows_exact_limits():
    brief, problem = parse_brief({**BRIEF, "workstream": "x" * 160, "goal": "x" * 49_998,
                                  "constraints": "x", "acceptance": "x", "paths": [f"src/file{i}.py" for i in range(128)],
                                  "budget": {"steps": 1}})
    assert not problem and brief is not None
    assert brief.text().endswith("Budget: 1 step")


@pytest.mark.parametrize("key", BRIEF)
def test_brief_requires_each_field(key):
    brief, problem = parse_brief({name: value for name, value in BRIEF.items() if name != key})
    assert brief is None and problem


@pytest.mark.parametrize("changes, message", [
    ({"workstream": "x" * 161}, "at most 160"),
    ({"workstream": "first\nsecond"}, "single-line"),
    ({"identity": "   "}, "non-empty"),
    ({"goal": 0}, "non-empty"),
    ({"constraints": "  "}, "non-empty"),
    ({"acceptance": []}, "non-empty"),
    ({"goal": "x" * 49_999, "constraints": "x", "acceptance": "x"}, "50,000"),
    ({"identity": "worker"}, "declared identity"),
    ({"identity": "Observer"}, "started by Fusion"),
    ({"read_only": "true"}, "booleans"),
    ({"resume": 1}, "booleans"),
    ({"paths": "src/file.py"}, "project-relative"),
    ({"paths": ["src/file.py"] * 129}, "at most 128"),
    ({"paths": [7]}, "project-relative"),
    ({"paths": [" "]}, "project-relative"),
    ({"paths": ["/src/file.py"]}, "project-relative"),
    ({"paths": ["X:\\src\\file.py"]}, "project-relative"),
    ({"paths": ["X:file.py"]}, "project-relative"),
    ({"paths": ["\\\\host\\share\\file.py"]}, "project-relative"),
    ({"paths": ["src\\..\\file.py"]}, "project-relative"),
    ({"paths": ["src/*.py"]}, "project-relative"),
    ({"paths": ["."]}, "project-relative"),
    ({"paths": ["src/\0file.py"]}, "project-relative"),
    ({"read_only": True}, "no write paths"),
    ({"paths": []}, "Declare write paths"),
    ({"budget": "12 steps"}, "positive integer"),
    ({"budget": {}}, "positive integer"),
    ({"budget": {"steps": 2, "minutes": 3}}, "positive integer"),
    ({"budget": {"seconds": 0}}, "positive integer"),
    ({"budget": {"steps": -1}}, "positive integer"),
    ({"budget": {"steps": True}}, "positive integer"),
    ({"budget": {"steps": 1.0}}, "positive integer"),
])
def test_invalid_brief(changes, message):
    brief, problem = parse_brief({**BRIEF, **changes})
    assert brief is None and message in problem


def test_parsers_refuse_non_objects():
    assert parse_brief([]) == (None, "Provide the task brief as an object.")
    assert parse_report([], ()) == (None, ("Provide the report as an object.",))
    assert parse_route([]) == (None, "Provide the route as an object.")


@pytest.mark.parametrize("status", ["done", "concerns", "needs_context", "blocked"])
def test_valid_report_drops_ratings_for_records_not_used(status):
    ratings = [*REPORT["ratings"], {"record": "unseen", "score": 2}, {"record": "extra", "score": -1}]
    report, problems = parse_report({**REPORT, "status": status, "ratings": ratings}, ["clock"])
    assert not problems and report is not None
    assert report.status == status and report.summary == REPORT["summary"]
    assert report.evidence == tuple(REPORT["evidence"]) and report.notes == tuple(REPORT["notes"])
    assert report.ratings == tuple(REPORT["ratings"])


def test_report_token_limit_excludes_evidence_and_allows_empty_arrays():
    summary = "x" * 6999
    assert estimate_text(summary) == REPORT_TOKENS
    report, problems = parse_report({"status": "done", "summary": summary, "evidence": ["x" * 10_000],
                                     "notes": [], "ratings": []}, ())
    assert not problems and report is not None and report.summary == summary
    report, problems = parse_report({**REPORT, "evidence": [], "notes": [], "ratings": []}, ())
    assert not problems and report is not None
    assert report.evidence == report.notes == report.ratings == ()


@pytest.mark.parametrize("key", ["status", "summary", "evidence", "notes"])
def test_report_body_requires_each_field(key):
    report, problems = parse_report({name: value for name, value in REPORT.items() if name != key}, ("clock",))
    assert report is None and len(problems) == 1 and key in problems[0]


@pytest.mark.parametrize("changes, field", [
    ({"status": "complete"}, "status"),
    ({"summary": " "}, "summary"),
    ({"summary": []}, "summary"),
    ({"evidence": "passed"}, "evidence"),
    ({"evidence": [False]}, "evidence"),
    ({"notes": {"note": "text"}}, "notes"),
    ({"notes": [1]}, "notes"),
    ({"summary": "x" * 3499, "notes": ["x" * 3500]}, "Shorten"),
])
def test_invalid_report_body(changes, field):
    report, problems = parse_report({**REPORT, **changes}, ("clock",))
    assert report is None and len(problems) == 1 and field in problems[0]
    if field == "Shorten":
        assert "point to evidence" in problems[0]


def test_report_lists_all_body_and_rating_problems():
    report, problems = parse_report({}, ["clock"])
    assert report is None and len(problems) == 6
    assert "ratings array" in problems[-2] and "Missing rating" in problems[-1]


@pytest.mark.parametrize("ratings, problem_text", [
    (None, "ratings array"),
    ({"clock": 0}, "ratings array"),
    ([], "Missing rating"),
    (["clock"], "record string"),
    ([{"record": []}], "record string"),
    ([{"score": 0, "reason": ""}], "record string"),
    ([{"record": "clock", "reason": ""}], "must use a score"),
    ([{"record": "clock", "score": 2, "reason": ""}], "must use a score"),
    ([{"record": "clock", "score": True, "reason": ""}], "must use a score"),
    ([{"record": "clock", "score": 1.0, "reason": ""}], "must use a score"),
    ([{"record": "clock", "score": -1, "reason": " "}], "needs a reason"),
    ([{"record": "clock", "score": 1}], "needs a reason"),
    ([{"record": "clock", "score": 0, "reason": False}], "reason string"),
    ([{"record": "clock", "score": 0}], "reason string"),
    (REPORT["ratings"] * 2, "only once"),
])
def test_rating_problems_keep_the_report_body_for_retry(ratings, problem_text):
    used = ["clock"]
    report, problems = parse_report({**REPORT, "ratings": ratings}, used)
    assert report is not None and report.summary == REPORT["summary"]
    assert any(problem_text in problem for problem in problems)
    assert settle_ratings(report.ratings, used) == settle_ratings(ratings, used) == ({"clock": (0, "")}, 1)


def test_settle_ratings_keeps_explicit_zero_and_counts_defaulted_records_once():
    used = ["helpful", "harmful", "neutral", "missing", "missing", "invalid", "no-reason"]
    ratings = [
        {"record": "helpful", "score": 1, "reason": " Saved time. "},
        {"record": "harmful", "score": -1, "reason": "Stale."},
        {"record": "neutral", "score": 0, "reason": ""},
        {"record": "invalid", "score": "1", "reason": "Ignored."},
        {"record": "no-reason", "score": 1},
        {"record": "not-used", "score": 1, "reason": "Ignored."},
    ]
    assert settle_ratings(ratings, used) == ({
        "helpful": (1, "Saved time."), "harmful": (-1, "Stale."), "neutral": (0, ""),
        "missing": (0, ""), "invalid": (0, ""), "no-reason": (0, ""),
    }, 3)
    assert settle_ratings(ratings, ()) == ({}, 0)


def test_valid_route():
    route, problem = parse_route({**ROUTE, "reason": " One coherent change. "})
    assert route == Route("coder", "One coherent change.", "runner") and not problem


@pytest.mark.parametrize("field, value", [
    ("destination", "worker"), ("destination", []), ("reason", "  "), ("reason", True),
    ("verification", "tests"), ("verification", {}),
])
def test_route_requires_valid_fields(field, value):
    missing = {key: item for key, item in ROUTE.items() if key != field}
    for args in (missing, {**ROUTE, field: value}):
        route, problem = parse_route(args)
        assert route is None and field in problem


@pytest.mark.parametrize("destination, action, count, message", [
    (None, "edit", 0, "submit a route first"),
    (None, "delegate", 0, "submit a route first"),
    ("self", "edit", 0, ""),
    ("self", "delegate", 0, "Route again"),
    ("coder", "edit", 0, "Send fixes back to the Coder"),
    ("coder", "delegate", 0, ""),
    ("coder", "delegate", 1, ""),
    ("coder", "delegate", 2, "one write workstream"),
    ("split", "edit", 0, "route again"),
    ("split", "delegate", 2, ""),
    ("self", "unknown", 0, "edit or delegate"),
])
def test_route_refusal(destination, action, count, message):
    route = Route(destination, "Reason.", "runner") if destination else None
    problem = route_refusal(route, action, workstreams=count)
    assert message in problem if message else problem == ""


@pytest.mark.parametrize("verification, checks", [
    ("self_check", ()), ("runner", ("runner",)), ("runner_and_sampler", ("runner", "sampler")),
])
def test_required_checks(verification, checks):
    route = Route("coder", "Reason.", verification)
    assert required_checks(route) == checks
    assert checks_after(route, ()) == checks


@pytest.mark.parametrize("changes, signals", [
    ({}, ()), ({"status": "concerns"}, ("concerns",)), ({"status": "blocked"}, ()),
    ({"observer_reported": True}, ("observer_reported",)), ({"out_of_scope": True}, ("out_of_scope",)),
    ({"repaired": True}, ("repaired",)),
    ({"status": "concerns", "observer_reported": True, "out_of_scope": True, "repaired": True},
     ("concerns", "observer_reported", "out_of_scope", "repaired")),
])
def test_risk_signals(changes, signals):
    args = {"status": "done", "observer_reported": False, "out_of_scope": False, "repaired": False}
    assert risk_signals(**{**args, **changes}) == signals


@pytest.mark.parametrize("destination, verification, checks", [
    ("coder", "runner", ("runner", "sampler")), ("coder", "runner_and_sampler", ("runner", "sampler")),
    ("coder", "self_check", ("sampler",)), ("self", "self_check", ()), ("split", "runner", ("runner",)),
])
def test_checks_after_risk(destination, verification, checks):
    assert checks_after(Route(destination, "Reason.", verification), ("concerns",)) == checks


@pytest.mark.parametrize("declaration, name, parameters, args", [
    (DELEGATE_DECLARATION, "delegate_task", BRIEF_PARAMETERS, BRIEF),
    (REPORT_DECLARATION, "submit_report", REPORT_PARAMETERS, REPORT),
    (ROUTE_DECLARATION, "submit_route", ROUTE_PARAMETERS, ROUTE),
])
def test_declarations_validate_with_harness(declaration, name, parameters, args):
    assert declaration["name"] == name and declaration["description"]
    assert declaration["parameters"] is parameters
    assert validate_arguments(parameters, args) == args
