"""Tool schemas and fixed rules for the Fusion route, brief, report and checks."""
from __future__ import annotations

import json
import ntpath
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from desktop.core.harness.compact import estimate_text

from . import allocation
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
            "description": "Requested limit on steps, seconds, or both; steps are clamped into the configured "
                           "min/max or the adaptive suggestion for the task's size, under the global per-turn cap."},
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
                "reason": {"type": "string", "description": "One sentence for -1 or 1; may be omitted or empty for 0."},
            }, "required": ["record"],
        }},
    },
    "required": [],
}
ROUTE_PARAMETERS: dict[str, Any] = {
    "type": "object",
    "properties": {
        "destination": {"type": "string", "enum": list(_DESTINATIONS), "description": "Do the work yourself, give it to one Coder, or split it into independent deliverables."},
        "reason": {"type": "string", "minLength": 1, "description": "Why this destination fits the work."},
        "verification": {"type": "string", "enum": list(_CHECKS), "description": "How the result will be checked."},
        "items": {"type": "array", "maxItems": 16, "items": {
            "type": "object", "additionalProperties": False,
            "properties": {
                "id": {"type": "string", "minLength": 1, "maxLength": 64, "description": "Stable work-item id that dependencies and later routes refer to."},
                "workstream": {"type": "string", "minLength": 1, "maxLength": _MAX_WORKSTREAM, "description": "The workstream this item binds; a delegation of the same workstream and role uses it."},
                "role": {"type": "string", "enum": ["worker", "light"], "description": "The expert role the item binds (a Coder is worker)."},
                "score": {"type": "number", "description": "Decimal score in 0..1: at or under the low gate the item seats the light model (L3), at or over the high gate the main model (L1), otherwise the worker model (L2)."},
                "reason": {"type": "string", "minLength": 1, "description": "Why this item deserves its score."},
                "paths": {"type": "array", "items": {"type": "string", "minLength": 1}, "maxItems": _MAX_PATHS, "description": "Frozen write scope: delegated write paths must stay inside these."},
                "deps": {"type": "array", "items": {"type": "string"}, "description": "Ids of items that must finish first."},
                "acceptance": {"type": "string", "description": "Frozen acceptance summary kept with the item."},
                "new_facts": {"type": "string", "minLength": 1, "description": "Evidence of facts learned after the item froze; required before a frozen item's score may be re-estimated. Only score and reason may change, and only while the workstream has no owner."},
            },
            "required": ["id", "workstream", "role", "score", "reason"],
        }, "description": "Frozen work items this route allocates: every write workstream needs one before it can be delegated. "
                           "Once frozen they cannot be re-scored or renamed; an approved plan supplies none. An identical "
                           "resubmission stays a no-op even after the preference moved; declared new_facts re-estimate only "
                           "score and reason before the workstream has an owner."},
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
    "description": "Refine one module of the project map: its paths, what it is responsible for and how it is implemented (not tests or pitfalls).",
    "parameters": {"type": "object", "properties": {
        "module": {"type": "string", "description": "The module's name as the map lists it; a new name adds a module."},
        "paths": {"type": "array", "items": {"type": "string"}, "description": "Project-relative paths of the module; leave empty to keep them."},
        "role": {"type": "string", "description": "What the module is responsible for, in one or two sentences; leave empty to keep it."},
        "method": {"type": "string", "description": "How it is implemented, in one or two sentences; leave empty to keep it."},
        "traits": {"type": "string", "description": "Not a field of the module: the one text of engineering traits of the whole project, which this replaces. Leave empty to keep it. To change only this, use module 'traits'."},
    }, "required": ["module"], "additionalProperties": False},
}
RATE_PARAMETERS: dict[str, Any] = {
    "type": "object",
    "properties": {"ratings": {"type": "array", "description": "Rate each supplied record once; use an empty array if none.", "items": {
        "type": "object", "properties": {
            "record": {"type": "string"},
            "score": {"type": "integer", "enum": [-1, 0, 1]},
            "reason": {"type": "string", "description": "One sentence for -1 or 1; may be empty for 0."},
        }, "required": ["record", "score", "reason"],
    }}},
    "required": ["ratings"],
}
RATE_DECLARATION: dict[str, Any] = {
    "name": "rate_records",
    "description": "Rate every Fusion Drive record you read in this turn: 1 helped, 0 made no difference, -1 was wrong or harmful.",
    "parameters": RATE_PARAMETERS,
}


def rating_item(record: str) -> dict[str, Any]:
    """One ratings entry placeholder for the report template; the expert fills in its own score and reason."""
    return {"record": record, "score": 0, "reason": ""}


def rating_item_text(record: str) -> str:
    """The ratings entry shape shown to an expert that reads a record mid-task, pre-filled with its real ID."""
    return json.dumps(rating_item(record), ensure_ascii=False, separators=(",", ":"))


def report_template(records: Sequence[str]) -> str:
    """A one-line legal JSON submit_report argument with every required field and ``records`` already filled in."""
    argument = {
        "status": _STATUSES[0],
        "summary": "your actual conclusion",
        "evidence": ["commands run and their results"],
        "notes": [],
        "ratings": [rating_item(record) for record in records],
    }
    return json.dumps(argument, ensure_ascii=False, separators=(",", ":"))


def report_guidance(records: Sequence[str]) -> str:
    """The submit_report call shape taught wherever an expert is told to report.

    ``records`` are the Drive record IDs due for rating right now; callers pass their current set sorted so a
    record read after the first prompt is still listed. Real IDs appear once, inside the JSON template.
    """
    text = ("Call it with a single JSON object like:\n" + report_template(records) + "\n"
            "Fill the example values from this task's real results and your own judgement of each record. "
            "status is one of " + ", ".join(_STATUSES) + "; summary is a string; evidence and notes are "
            "arrays of strings. ratings holds one object " + rating_item_text("<record ID>")
            + " per Fusion Drive record you used: score is the integer 1 (it helped), 0 (it made no "
            "difference) or -1 (it was wrong or harmful); reason is one sentence for 1 or -1, and for 0 it "
            "may be empty or left out. evidence, notes and ratings may also be left out and become [].")
    if records:
        text += " Every record supplied above is due for a rating, whether or not it helped."
    else:
        text += " No records were supplied with the brief."
    text += (" In addition, each ID that a read_record result marks for report ratings is due once; only "
             "marked IDs join ratings, and reads are not marked when the Drive is not writable. Keep ratings "
             "[] only while nothing is due.")
    text += (" Call it only when you have a verdict; a call missing pieces is kept as a draft and you are told "
             "which pieces are still due — send only those next, no redoing finished work, and the task stays "
             "open meanwhile.")
    return text


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
    budget_note: str = ""

    def budget_label(self) -> str:
        parts = []
        for unit in ("steps", "seconds"):
            if unit in self.budget:
                count = self.budget[unit]
                parts.append(f"{count} {unit[:-1] if count == 1 else unit}")
        label = ", ".join(parts)
        return f"{label} ({self.budget_note})" if self.budget_note else label

    def text(self) -> str:
        """Render the brief body; the caller supplies identity and write scope separately."""
        return (f"Goal:\n{self.goal}\n\nConstraints:\n{self.constraints}\n\n"
                f"Acceptance criteria:\n{self.acceptance}\n\nBudget: {self.budget_label()}")


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
    items: tuple[dict[str, Any], ...] = ()


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


def _ratings(ratings: Any, used: Sequence[str], *, report: bool = False) -> tuple[dict[str, tuple[int, str]], list[str]]:
    """Rating entries for the records in ``used``. ``report`` allows a report to omit the reason of a neutral score."""
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
        elif reason is None and report and "reason" not in item:
            valid[record] = score, ""  # Only a report may omit the reason of a neutral score; an explicit null is invalid.
        elif not isinstance(reason, str):
            problems.append(f"Rating for {record!r} needs a reason string (empty is fine for score 0).")
        else:
            valid[record] = score, reason.strip()
    problems.extend(f"Missing rating for record {record!r}." for record in eligible if record not in seen)
    return valid, problems


def parse_report(args: Mapping[str, Any], used: Sequence[str], *, draft: dict[str, Any] | None = None
                 ) -> tuple[Report | None, tuple[str, ...]]:
    """Keep a valid report body despite rating problems so a retry can settle them neutrally.

    ``draft`` accumulates the pieces a run already supplied across calls: a key left out of a call falls
    back to the kept value, and a re-submitted rating replaces a record's earlier entry as a whole, so a
    stale reason is never reused. Only individually valid values are kept; a key set to null is invalid,
    not omitted.
    """
    if not isinstance(args, Mapping):
        return None, ("Provide the report as an object.",)
    merged = dict(args)
    kept: dict[str, Any] | None = None
    if draft is not None:
        for key in ("status", "summary", "evidence", "notes"):
            if key not in merged and draft.get(key) is not None:
                merged[key] = draft[key]
        if "ratings" in merged:
            if isinstance(merged["ratings"], (list, tuple)):
                kept = dict(draft.get("ratings") or {})
                entries: dict[str, list[Any]] = {}
                leftovers = []
                for entry in merged["ratings"]:
                    record = entry.get("record") if isinstance(entry, Mapping) else None
                    if _nonempty(record):
                        entries.setdefault(record.strip(), []).append(entry)
                    else:
                        leftovers.append(entry)
                for record, patches in entries.items():
                    if len(patches) > 1:
                        # A record rated twice in one call is invalid and cannot hide behind the kept draft.
                        kept.pop(record, None)
                        leftovers.extend(patches)
                        continue
                    patch = dict(kept.get(record) or {"record": record})
                    entry = patches[0]
                    if "score" in entry and entry["score"] != patch.get("score"):
                        patch.pop("reason", None)  # A changed score must bring its own reason.
                    patch.update(entry)
                    patch["record"] = record
                    kept[record] = patch
                merged["ratings"] = [*kept.values(), *leftovers]
        elif draft.get("ratings"):
            merged["ratings"] = list(draft["ratings"].values())
    status, summary = merged.get("status"), merged.get("summary")
    evidence = merged["evidence"] if "evidence" in merged else []
    notes = merged["notes"] if "notes" in merged else []
    problems: list[str] = []
    if status not in _STATUSES:
        problems.append("Use a status of done, concerns, needs_context or blocked.")
    elif draft is not None:
        draft["status"] = status
    if not _nonempty(summary):
        problems.append("Provide a non-empty summary string.")
    elif draft is not None:
        draft["summary"] = summary.strip()
    for key, values in (("evidence", evidence), ("notes", notes)):
        if not isinstance(values, list) or not all(isinstance(value, str) for value in values):
            problems.append(f"Provide {key} as an array of strings, even if it is empty.")
        elif draft is not None:
            draft[key] = list(values)
    if _nonempty(summary) and isinstance(notes, list) and all(isinstance(note, str) for note in notes):
        if estimate_text("\n".join((summary, *notes))) > REPORT_TOKENS:
            problems.append(f"Shorten the summary and notes to {REPORT_TOKENS:,} tokens or less and point to evidence instead.")
    ratings, rating_problems = _ratings(merged.get("ratings", []), used, report=True)
    if draft is not None and kept is not None:
        # Persist the merged entries as the draft state: pending pieces (a score still missing its reason, an
        # invalid new value) stay due and an empty call cannot silently resurrect what a patch replaced.
        draft["ratings"] = kept
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


def parse_route(args: Mapping[str, Any], N: object = None) -> tuple[Route | None, str]:
    """Validate a route; ``N`` is the session preference the runtime supplies, never the model's slider."""
    if not isinstance(args, Mapping):
        return None, "Provide the route as an object."
    destination, reason, verification = (args.get(key) for key in ("destination", "reason", "verification"))
    if destination not in _DESTINATIONS:
        return None, "Use a destination of self, coder or split."
    if not _nonempty(reason):
        return None, "Give a non-empty reason for the route."
    if verification not in tuple(_CHECKS):
        return None, "Use a verification depth of self_check, runner or runner_and_sampler."
    raw_items = args.get("items", [])
    if not isinstance(raw_items, (list, tuple)) or len(raw_items) > 16:
        return None, "Provide at most 16 work items."
    items: list[dict[str, Any]] = []
    for raw in raw_items:
        try:
            items.append(allocation.seal(raw, N))
        except ValueError as error:
            return None, str(error)
    binds = [allocation.key(item["workstream"], item["role"]) for item in items]
    if len(set(binds)) != len(binds):
        return None, "Each work item needs a distinct workstream and role."
    ids = [item["id"] for item in items]
    if len(set(ids)) != len(ids):
        return None, "Each work item needs a distinct id."
    return Route(destination, reason.strip(), verification, tuple(items)), ""


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
