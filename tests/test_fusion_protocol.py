"""The fixed gates reject malformed model calls before runtime work begins."""
import json

import pytest

from desktop.core.fusion.protocol import (
    BRIEF_PARAMETERS, DELEGATE_DECLARATION, RATE_DECLARATION, REPORT_DECLARATION, REPORT_PARAMETERS, REPORT_TOKENS,
    ROUTE_DECLARATION, ROUTE_PARAMETERS, Route, brief_writes, checks_after, parse_brief, parse_ratings, parse_report,
    parse_route, rating_item_text, report_guidance, report_template, required_checks, risk_signals, route_refusal,
    settle_ratings,
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


@pytest.mark.parametrize("key", [key for key in BRIEF if key != "budget"])
def test_brief_requires_each_field(key):
    brief, problem = parse_brief({name: value for name, value in BRIEF.items() if name != key})
    assert brief is None and problem


@pytest.mark.parametrize("empty", [False, True], ids=["omitted", "empty-object"])
def test_brief_accepts_a_default_budget(empty):
    args = dict(BRIEF)
    if empty:
        args["budget"] = {}
    else:
        del args["budget"]
    brief, problem = parse_brief(args)
    assert not problem and brief is not None and brief.budget == {}
    assert validate_arguments(BRIEF_PARAMETERS, args) == args


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
    ({"budget": None}, "positive integer"),
    ({"budget": {"steps": 2, "minutes": 3}}, "positive integer"),
    ({"budget": {"seconds": 0}}, "positive integer"),
    ({"budget": {"steps": -1}}, "positive integer"),
    ({"budget": {"steps": True}}, "positive integer"),
    ({"budget": {"steps": 1.0}}, "positive integer"),
])
def test_invalid_brief(changes, message):
    brief, problem = parse_brief({**BRIEF, **changes})
    assert brief is None and message in problem
    if "budget" in changes:
        assert "steps, seconds, or both" in problem and "Omit budget" in problem


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


@pytest.mark.parametrize("key", ["status", "summary"])
def test_report_body_requires_each_field(key):
    report, problems = parse_report({name: value for name, value in REPORT.items() if name != key}, ("clock",))
    assert report is None and len(problems) == 1 and key in problems[0]


def test_report_mechanically_defaults_omittable_fields():
    report, problems = parse_report({"status": "done", "summary": "Checked."}, ())
    assert not problems and report is not None and report.evidence == report.notes == report.ratings == ()
    report, problems = parse_report({**REPORT, "ratings": [{"record": "clock", "score": 0}]}, ("clock",))
    assert not problems and report is not None and report.ratings == ({"record": "clock", "score": 0, "reason": ""},)


@pytest.mark.parametrize("key", ["evidence", "notes"])
def test_report_explicit_null_is_invalid_not_defaulted(key):
    report, problems = parse_report({**REPORT, key: None}, ())
    assert report is None and any(key in problem for problem in problems)


def test_report_explicit_null_ratings_is_invalid_not_defaulted():
    report, problems = parse_report({**REPORT, "ratings": None}, ("clock",))
    assert "ratings array" in problems[0] and "Missing rating" in problems[-1]


def test_omitted_notes_does_not_skip_the_token_limit():
    report, problems = parse_report({"status": "done", "summary": "x" * 7000}, ())
    assert report is None and "Shorten" in problems[0]


def test_report_draft_merges_missing_pieces_across_calls():
    used = ("clock", "map")
    draft: dict = {}
    report, problems = parse_report({"status": "done", "ratings": [{"record": "clock", "score": 1, "reason": "helped"}]},
                                    used, draft=draft)
    assert report is None and "summary" in problems[0]  # an incomplete body stays unaccepted
    report, problems = parse_report({"summary": "Counted.", "ratings": [{"record": "map", "score": 0}]},
                                    used, draft=draft)
    assert report is not None and not problems
    assert report.status == "done" and report.summary == "Counted."
    assert report.ratings == ({"record": "clock", "score": 1, "reason": "helped"},
                              {"record": "map", "score": 0, "reason": ""})


def test_report_draft_replaces_a_record_entry_so_a_stale_reason_does_not_linger():
    draft: dict = {}
    parse_report({**REPORT, "ratings": [{"record": "clock", "score": 1, "reason": "helped"}]}, ("clock",), draft=draft)
    report, problems = parse_report({**REPORT, "ratings": [{"record": "clock", "score": 0}]}, ("clock",), draft=draft)
    assert report is not None and not problems
    assert report.ratings == ({"record": "clock", "score": 0, "reason": ""},)


def test_report_draft_keeps_only_individually_valid_values():
    draft: dict = {}
    report, problems = parse_report({**REPORT, "status": "later", "evidence": "oops"}, ("clock",), draft=draft)
    assert report is None and "status" not in draft and "evidence" not in draft
    assert draft["summary"] == REPORT["summary"] and draft["ratings"]["clock"]["score"] == 1


def test_rate_records_schema_and_parser_stay_strict():
    items = RATE_DECLARATION["parameters"]["properties"]["ratings"]["items"]
    assert items["required"] == ["record", "score", "reason"]
    assert REPORT_PARAMETERS["properties"]["ratings"]["items"]["required"] == ["record"]
    valid, problems = parse_ratings({"ratings": [{"record": "clock", "score": 0}]}, ("clock",))
    assert valid == {} and "reason" in problems[0]


def test_report_schema_allows_patch_only_calls():
    parameters = REPORT_DECLARATION["parameters"]
    patch = {"ratings": [{"record": "clock", "reason": "helped"}]}
    assert validate_arguments(parameters, patch) == patch
    assert validate_arguments(parameters, {"summary": "Counted."}) == {"summary": "Counted."}
    # The parser still enforces the pieces a schema-valid patch leaves out.
    report, problems = parse_report(patch, ("clock",))
    assert report is None and any("score" in problem for problem in problems)


def test_report_draft_reason_patch_keeps_the_drafted_score():
    draft: dict = {}
    report, problems = parse_report({**REPORT, "ratings": [{"record": "clock", "score": 1}]},
                                    ("clock",), draft=draft)
    assert report is not None and any("needs a reason" in problem for problem in problems)
    assert draft["ratings"]["clock"] == {"record": "clock", "score": 1}
    report, problems = parse_report({**REPORT, "ratings": [{"record": "clock", "reason": "helped"}]},
                                    ("clock",), draft=draft)
    assert report is not None and not problems
    assert report.ratings == ({"record": "clock", "score": 1, "reason": "helped"},)


def test_report_draft_changed_score_drops_the_old_reason():
    draft: dict = {}
    parse_report({**REPORT, "ratings": [{"record": "clock", "score": 1, "reason": "helped"}]},
                 ("clock",), draft=draft)
    report, problems = parse_report({**REPORT, "ratings": [{"record": "clock", "score": -1}]},
                                    ("clock",), draft=draft)
    assert report is not None and any("needs a reason" in problem for problem in problems)
    report, problems = parse_report({**REPORT, "ratings": [{"record": "clock", "reason": "misleading"}]},
                                    ("clock",), draft=draft)
    assert report is not None and not problems
    assert report.ratings == ({"record": "clock", "score": -1, "reason": "misleading"},)


def test_report_draft_same_call_duplicate_stays_rejected_after_an_empty_patch():
    draft: dict = {}
    report, problems = parse_report({**REPORT, "ratings": [{"record": "clock", "score": 1, "reason": "a"},
                                                          {"record": "clock", "score": -1, "reason": "b"}]},
                                    ("clock",), draft=draft)
    assert report is not None and any("only once" in problem for problem in problems)
    report, problems = parse_report({**REPORT, "ratings": []}, ("clock",), draft=draft)
    assert report is not None and any("Missing rating for record 'clock'" in problem for problem in problems)


def test_report_draft_invalid_patch_replaces_the_older_valid_entry():
    draft: dict = {}
    parse_report({**REPORT, "ratings": [{"record": "clock", "score": 1, "reason": "helped"}]},
                 ("clock",), draft=draft)
    report, problems = parse_report({**REPORT, "ratings": [{"record": "clock", "score": "bad"}]},
                                    ("clock",), draft=draft)
    assert report is not None and any("must use a score" in problem for problem in problems)
    # The stale valid rating must not quietly settle an empty follow-up call.
    report, problems = parse_report({**REPORT, "ratings": []}, ("clock",), draft=draft)
    assert report is not None and any("must use a score" in problem for problem in problems)


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
    assert report is None and len(problems) == 3
    assert "status" in problems[0] and "summary" in problems[1] and "Missing rating" in problems[-1]


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
    ([{"record": "clock", "score": 0, "reason": None}], "reason string"),
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


@pytest.mark.parametrize("records", [(), ("clock",), ("clock", "map")])
def test_report_template_is_one_legal_json_line_with_required_fields(records):
    line = report_template(records)
    assert "\n" not in line
    argument = json.loads(line)
    assert set(argument) == {"status", "summary", "evidence", "notes", "ratings"}
    assert set(REPORT_PARAMETERS["required"]) <= set(argument) and "evidence" not in REPORT_PARAMETERS["required"]
    assert argument["status"] in REPORT_PARAMETERS["properties"]["status"]["enum"]
    assert isinstance(argument["summary"], str)
    assert all(isinstance(item, str) for item in [*argument["evidence"], *argument["notes"]])
    for index, item in enumerate(argument["ratings"]):
        assert set(item) == {"record", "score", "reason"} and item["record"] == records[index]
        assert type(item["score"]) is int and item["score"] in (-1, 0, 1)
        assert isinstance(item["reason"], str)
    # The template stays acceptable to the tool schema and the parser once the real record IDs stand in it.
    assert validate_arguments(REPORT_DECLARATION["parameters"], argument) == argument
    report, problems = parse_report(argument, records)
    assert report is not None and not problems


def test_rating_item_text_prefills_the_real_record_id():
    assert rating_item_text("clock") == '{"record":"clock","score":0,"reason":""}'
    assert json.loads(rating_item_text("map"))["record"] == "map"


def test_report_guidance_restates_the_template_and_every_rule():
    guidance = report_guidance(("clock", "map"))
    assert json.loads(guidance.splitlines()[1]) == json.loads(report_template(("clock", "map")))
    for snippet in ("done", "concerns", "needs_context", "blocked", "summary is a string", "arrays of strings",
                    "score is the integer", "reason is one sentence for 1 or -1", "your own judgement",
                    "left out and become []", "kept as a draft", "send only those next",
                    "Every record supplied above is due for a rating, whether or not it helped.",
                    "marks for report ratings is due once", "not writable",
                    "Keep ratings [] only while nothing is due."):
        assert snippet in guidance
    # The real IDs appear once, inside the legal JSON; the prose does not list them again.
    assert all("clock" not in line and "map" not in line for line in guidance.splitlines()[2:])


def test_report_guidance_without_records_still_teaches_the_rating_item():
    guidance = report_guidance(())
    assert json.loads(guidance.splitlines()[1])["ratings"] == []
    assert '{"record":"<record ID>","score":0,"reason":""}' in guidance
    assert "No records were supplied with the brief." in guidance
    assert "read_record" in guidance and "not writable" in guidance
    assert "Keep ratings [] only while nothing is due." in guidance
    assert "due for a rating" not in guidance


@pytest.mark.parametrize("changes, writes", [({}, True), ({"read_only": True, "paths": []}, False),
                                           ({"identity": "Runner", "paths": []}, False)])
def test_only_declared_structured_writes_reserve_a_workstream(changes, writes):
    brief, problem = parse_brief({**BRIEF, **changes})
    assert brief is not None and not problem and brief_writes(brief) is writes


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
    ({}, ()), ({"status": "concerns"}, ("concerns",)), ({"status": "blocked"}, ("concerns",)),
    ({"status": "needs_context"}, ("concerns",)), ({"status": ""}, ()),
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


def test_route_self_allows_two_edits_and_sends_the_rest_to_a_coder():
    from desktop.core.fusion.protocol import SELF_EDITS

    own = Route("self", "Small.", "self_check")
    assert SELF_EDITS == 2
    assert route_refusal(own, "edit", edits=0) == "" and route_refusal(own, "edit", edits=1) == ""
    assert "must go to a Coder" in route_refusal(own, "edit", edits=2)
    assert "must go to a Coder" not in route_refusal(Route("coder", "Whole change.", "runner"), "edit", edits=5)