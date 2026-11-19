"""Tool schemas and fixed rules for the Fusion route, brief, report and checks."""
from __future__ import annotations

import ntpath
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from desktop.core.harness.compact import estimate_text

from .identity import load

_IDENTITIES = load(None)
_MAX_WORKSTREAM = 160
_MAX_PATHS = 128
_MAX_BRIEF_TEXT = 50_000
REPORT_TOKENS = 2000
_STATUSES = ("done", "concerns", "needs_context", "blocked")
_DESTINATIONS = ("self", "coder", "split")
_CHECKS: dict[str, tuple[str, ...]] = {"self_check": (), "runner": ("runner",), "runner_and_sampler": ("runner", "sampler")}

BRIEF_PARAMETERS: dict[str, Any] = {
    "type": "object",
    "properties": {
        "workstream": {"type": "string", "minLength": 1, "maxLength": _MAX_WORKSTREAM, "description": "Stable responsibility or deliverable; do not split by file."},
        "identity": {"type": "string", "enum": list(_IDENTITIES.names), "description": "Expert identity, not a model. Observer is started by Fusion, not delegated."},
        "goal": {"type": "string", "minLength": 1, "description": "Outcome to achieve, not implementation steps."},
        "constraints": {"type": "string", "minLength": 1, "description": "Limits, dependencies and relevant context."},
        "acceptance": {"type": "string", "minLength": 1, "description": "Acceptance criteria and the commands that check them, when available."},
        "paths": {"type": "array", "items": {"type": "string", "minLength": 1}, "maxItems": _MAX_PATHS, "description": "Concrete project-relative files or directories this expert may change."},
        "read_only": {"type": "boolean", "default": False, "description": "Research by a Coder that must change nothing: leave paths empty; it gets no editing tools, may run only read-only commands and needs no route. Leave false for Runner and QualitySampler: they never edit and must be able to run the checks."},
        "budget": {"type": "object", "properties": {
            "steps": {"type": "integer", "minimum": 1},
            "seconds": {"type": "integer", "minimum": 1},
        }, "anyOf": [{"required": ["steps"]}, {"required": ["seconds"]}], "additionalProperties": False,
            "description": "Limit on steps, seconds, or both."},
        "resume": {"type": "boolean", "default": False, "description": "Continue an interrupted workstream only after inspecting the evidence; give a new brief."},
    },
    "required": ["workstream", "identity", "goal", "constraints", "acceptance", "paths", "budget"],
}
REPORT_PARAMETERS: dict[str, Any] = {
    "type": "object",
    "properties": {
        "status": {"type": "string", "enum": list(_STATUSES)},
        "summary": {"type": "string", "minLength": 1, "description": "What changed or was found. Use at most 2,000 tokens for summary and notes together."},
        "evidence": {"type": "array", "items": {"type": "string"}, "description": "Evidence pointers, paths and commands with their outcomes; use an empty array if none."},
        "notes": {"type": "array", "items": {"type": "string"}, "description": "Lessons worth keeping; use an empty array if none."},
        "ratings": {"type": "array", "description": "Rate each supplied record once; use an empty array if none.", "items": {
            "type": "object", "properties": {
                "record": {"type": "string"},
                "score": {"type": "integer", "enum": [-1, 0, 1]},
                "reason": {"type": "string", "description": "One sentence for -1 or 1; may be empty for 0."},
            }, "required": ["record", "score", "reason"],
        }},
    },
    "required": ["status", "summary", "evidence", "notes", "ratings"],
}
ROUTE_PARAMETERS: dict[str, Any] = {
    "type": "object",
    "properties": {
        "destination": {"type": "string", "enum": list(_DESTINATIONS), "description": "Do the work yourself, give it to one Coder, or split it into independent deliverables."},
        "reason": {"type": "string", "minLength": 1, "description": "Why this destination fits the work."},
        "verification": {"type": "string", "enum": list(_CHECKS), "description": "How the result will be checked."},
    },
    "required": ["destination", "reason", "verification"],
}
DELEGATE_DECLARATION: dict[str, Any] = {
    "name": "delegate_task",
    "description": "Delegate a self-contained brief to a Fusion expert. Submit a route before delegating writes.",
    "parameters": BRIEF_PARAMETERS,
}
REPORT_DECLARATION: dict[str, Any] = {
    "name": "submit_report",
    "description": "End the expert task with results, evidence, proposed notes and record ratings.",
    "parameters": REPORT_PARAMETERS,
}
ROUTE_DECLARATION: dict[str, Any] = {
    "name": "submit_route",
    "description": "Choose a destination and verification depth before editing or delegating writes.",
    "parameters": ROUTE_PARAMETERS,
}
CHOOSE_MAP_DECLARATION: dict[str, Any] = {
    "name": "choose_map_location",
    "description": "Before the first writing delegation, ask the user where this project's map should live. Does not ask again once chosen.",
    "parameters": {"type": "object", "properties": {}, "additionalProperties": False},
}
UPDATE_MAP_DECLARATION: dict[str, Any] = {
    "name": "update_map_entry",
    "description": "Refine one map module (responsibility and implementation, not tests or pitfalls); use module 'traits' for engineering traits.",
    "parameters": {"type": "object", "properties": {
        "module": {"type": "string"}, "paths": {"type": "array", "items": {"type": "string"}},
        "role": {"type": "string"}, "method": {"type": "string"}, "traits": {"type": "string"},
    }, "required": ["module"], "additionalProperties": False},
}
RATE_PARAMETERS: dict[str, Any] = {
    "type": "object",
    "properties": {"ratings": REPORT_PARAMETERS["properties"]["ratings"]},
    "required": ["ratings"],
}
RATE_DECLARATION: dict[str, Any] = {
    "name": "rate_records",
    "description": "Rate every Fusion Drive record you read in this turn: 1 helped, 0 made no difference, -1 was wrong or harmful.",
    "parameters": RATE_PARAMETERS,
}


@dataclass(frozen=True, slots=True)
class Brief:
    workstream: str
    identity: str
    goal: str
    constraints: str
    acceptance: str
    paths: tuple[str, ...]
    budget: dict[str, int]
    read_only: bool = False
    resume: bool = False

    def text(self) -> str:
        """Render the brief body; the caller supplies identity and write scope separately."""
        parts = []
        for unit in ("steps", "seconds"):
            if unit in self.budget:
                count = self.budget[unit]
                parts.append(f"{count} {unit[:-1] if count == 1 else unit}")
        budget = ", ".join(parts)
        return (f"Goal:\n{self.goal}\n\nConstraints:\n{self.constraints}\n\n"
                f"Acceptance criteria:\n{self.acceptance}\n\nBudget: {budget}")


@dataclass(frozen=True, slots=True)
class Report:
    status: str
    summary: str
    evidence: tuple[str, ...]
    notes: tuple[str, ...]
    ratings: tuple[dict[str, Any], ...]


@dataclass(frozen=True, slots=True)
class Route:
    destination: str
    reason: str
    verification: str


def _nonempty(value: Any) -> bool:
    return isinstance(value, str) and bool(value.strip())


def _write_path(value: Any) -> bool:
    # Match the registry's lexical rules without resolving anything in the workspace.
    if not _nonempty(value) or any(char in value for char in "*?\0"):
        return False
    path = value.strip().replace("\\", "/")
    return not ntpath.splitdrive(path)[0] and all(part not in ("", ".", "..") for part in path.split("/"))


def parse_brief(args: Mapping[str, Any]) -> tuple[Brief | None, str]:
    """Validate a new delegation brief; paths coordinate writes, not filesystem permissions."""
    if not isinstance(args, Mapping):
        return None, "Provide the task brief as an object."
    values = [args.get(key) for key in ("workstream", "identity", "goal", "constraints", "acceptance")]
    if not all(_nonempty(value) for value in values):
        return None, "Provide non-empty workstream, identity, goal, constraints and acceptance strings."
    workstream, name, goal, constraints, acceptance = values
    if len(workstream) > _MAX_WORKSTREAM or any(ord(char) < 32 for char in workstream):
        return None, f"Provide a single-line workstream of at most {_MAX_WORKSTREAM} characters."
    if sum(map(len, (goal, constraints, acceptance))) > _MAX_BRIEF_TEXT:
        return None, f"Keep goal, constraints and acceptance within {_MAX_BRIEF_TEXT:,} characters combined."
    if name not in _IDENTITIES.names:
        return None, "Use a declared identity: Coder, Runner, QualitySampler or Observer."
    if name == "Observer":
        return None, "Observer is started by Fusion, not delegated by the main agent."
    read_only, resume = args.get("read_only", False), args.get("resume", False)
    if not isinstance(read_only, bool) or not isinstance(resume, bool):
        return None, "read_only and resume must be booleans."
    paths = args.get("paths")
    if not isinstance(paths, list) or len(paths) > _MAX_PATHS or not all(map(_write_path, paths)):
        return None, f"Provide an array of at most {_MAX_PATHS} concrete project-relative write paths."
    if read_only and paths:
        return None, "A read-only brief must have no write paths."
    # Overrides can remove edit, but cannot make any other identity an editor.
    identity = _IDENTITIES.get(name)
    if identity is not None and "edit" in identity.tools and not read_only and not paths:
        return None, "Declare write paths for this identity, or set read_only to true for research."
    budget = args.get("budget")
    if not isinstance(budget, Mapping) or not budget or any(
        key not in ("steps", "seconds") or type(value) is not int or value < 1 for key, value in budget.items()
    ):
        return None, "Provide a budget with steps, seconds, or both, each a positive integer."
    return Brief(workstream.strip(), name, goal.strip(), constraints.strip(), acceptance.strip(),
                 tuple(path.strip() for path in paths), dict(budget), read_only, resume), ""


def _ratings(ratings: Any, used: Sequence[str]) -> tuple[dict[str, tuple[int, str]], list[str]]:
    eligible = dict.fromkeys(used)
    valid: dict[str, tuple[int, str]] = {}
    seen: set[str] = set()
    problems: list[str] = []
    if not isinstance(ratings, (list, tuple)):
        problems.append("Provide a ratings array, even if it is empty.")
        ratings = ()
    for item in ratings:
        record = item.get("record") if isinstance(item, Mapping) else None
        if not _nonempty(record):
            problems.append("Each rating needs a non-empty record string.")
            continue
        if record not in eligible:
            continue  # Only supplied records can affect quality.
        if record in seen:
            valid.pop(record, None)
            problems.append(f"Rate record {record!r} only once.")
            continue
        seen.add(record)
        score, reason = item.get("score"), item.get("reason")
        if type(score) is not int or score not in (-1, 0, 1):
            problems.append(f"Rating for {record!r} must use a score of -1, 0 or 1.")
        elif score != 0 and not _nonempty(reason):
            problems.append(f"A score of -1 or 1 for {record!r} needs a reason.")
        elif not isinstance(reason, str):
            problems.append(f"Rating for {record!r} needs a reason string (empty is fine for score 0).")
        else:
            valid[record] = score, reason.strip()
    problems.extend(f"Missing rating for record {record!r}." for record in eligible if record not in seen)
    return valid, problems


def parse_report(args: Mapping[str, Any], used: Sequence[str]) -> tuple[Report | None, tuple[str, ...]]:
    """Keep a valid report body despite rating problems so a retry can settle them neutrally."""
    if not isinstance(args, Mapping):
        return None, ("Provide the report as an object.",)
    status, summary, evidence, notes = (args.get(key) for key in ("status", "summary", "evidence", "notes"))
    problems: list[str] = []
    if status not in _STATUSES:
        problems.append("Use a status of done, concerns, needs_context or blocked.")
    if not _nonempty(summary):
        problems.append("Provide a non-empty summary string.")
    for key, values in (("evidence", evidence), ("notes", notes)):
        if not isinstance(values, list) or not all(isinstance(value, str) for value in values):
            problems.append(f"Provide {key} as an array of strings, even if it is empty.")
    if _nonempty(summary) and isinstance(notes, list) and all(isinstance(note, str) for note in notes):
        if estimate_text("\n".join((summary, *notes))) > REPORT_TOKENS:
            problems.append(f"Shorten the summary and notes to {REPORT_TOKENS:,} tokens or less and point to evidence instead.")
    ratings, rating_problems = _ratings(args.get("ratings"), used)
    if problems:
        return None, tuple(problems + rating_problems)
    items = tuple({"record": record, "score": score, "reason": reason} for record, (score, reason) in ratings.items())
    return Report(status, summary.strip(), tuple(evidence), tuple(notes), items), tuple(rating_problems)


def settle_ratings(ratings: Any, used: Sequence[str]) -> tuple[dict[str, tuple[int, str]], int]:
    """Settle raw tool ratings or Report.ratings; missing or invalid used records are neutral."""
    valid, _ = _ratings(ratings, used)
    settled = {record: valid.get(record, (0, "")) for record in used}
    return settled, len(settled) - len(valid)


def parse_ratings(args: Mapping[str, Any], used: Sequence[str]) -> tuple[dict[str, tuple[int, str]], tuple[str, ...]]:
    """The main agent's ratings of the records it read, under the same rules as the ratings of a report."""
    if not isinstance(args, Mapping):
        return {}, ("Provide the ratings as an object.",)
    valid, problems = _ratings(args.get("ratings"), used)
    return valid, tuple(problems)


def parse_route(args: Mapping[str, Any]) -> tuple[Route | None, str]:
    if not isinstance(args, Mapping):
        return None, "Provide the route as an object."
    destination, reason, verification = (args.get(key) for key in ("destination", "reason", "verification"))
    if destination not in _DESTINATIONS:
        return None, "Use a destination of self, coder or split."
    if not _nonempty(reason):
        return None, "Give a non-empty reason for the route."
    if verification not in tuple(_CHECKS):
        return None, "Use a verification depth of self_check, runner or runner_and_sampler."
    return Route(destination, reason.strip(), verification), ""


SELF_EDITS = 2
"""How many edits the main agent may make itself in one user turn. More is multi-step work, and that goes to a Coder."""


def route_refusal(route: Route | None, action: str, *, workstreams: int = 0, edits: int = 0) -> str:
    """For delegation, count distinct write workstreams including the proposed workstream; ``edits`` are those already made."""
    if action not in ("edit", "delegate"):
        return "Use the edit or delegate action for write routing."
    if route is None:
        return "You must submit a route first before editing or delegating writes."
    if route.destination == "self" and action == "edit" and edits >= SELF_EDITS:
        return (f"You have made {SELF_EDITS} edits yourself in this turn. A change that needs more is multi-step work and must go "
                "to a Coder: submit a route with destination coder (or split) and delegate the rest of the change.")
    if route.destination == "self" and action == "delegate":
        return "This route assigns writes to you. Route again before delegating writes."
    if route.destination in ("coder", "split") and action == "edit":
        return "Send fixes back to the Coder or route again before editing the workspace yourself."
    if route.destination == "coder" and action == "delegate" and workstreams > 1:
        return "This route allows one write workstream. Reuse it or route again to split the work."
    return ""


def brief_writes(brief: Brief) -> bool:
    """Only a structured delegation with declared write paths reserves writing work."""
    return not brief.read_only and bool(brief.paths)


def required_checks(route: Route) -> tuple[str, ...]:
    return _CHECKS[route.verification]


def risk_signals(*, status: str, observer_reported: bool, out_of_scope: bool, repaired: bool) -> tuple[str, ...]:
    """A repair is a fix after a failed acceptance check, not an ordinary iteration."""
    return tuple(name for name, present in (("concerns", status in ("concerns", "needs_context", "blocked")), ("observer_reported", observer_reported),
                                            ("out_of_scope", out_of_scope), ("repaired", repaired)) if present)


def checks_after(route: Route, signals: Sequence[str]) -> tuple[str, ...]:
    checks = required_checks(route)
    if route.destination == "coder" and signals and "sampler" not in checks:
        return (*checks, "sampler")
    return checks
