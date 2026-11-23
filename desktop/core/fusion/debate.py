"""Pre-Plan read-only L1–L2 debate coordinator for Fusion discussions.

A debate is pure discussion: it ends in an answer or an honestly reported open
issue, never in Plan or an execution delegation. The caller injects the seat
responder — an async ``response(seat, instructions, input) -> str`` that runs a
controlled, tool-free request per seat — so this module makes no model calls
itself. The harness owns the structure: the quality slider decides whether a
debate runs at all and fixes the default and maximum rounds; L2 challenges,
L1 hands down a JSON decision; no open issue stops the debate early, only a
non-empty *new* issue may extend it past the default, and the cap is a hard
stop. Responder errors and malformed decisions propagate as failures — a
stopped or failed debate never reports a conclusion it did not reach.
"""

from __future__ import annotations

import json
import re
from collections.abc import Awaitable, Callable, Iterable
from dataclasses import dataclass, field
from typing import Any

__all__ = [
    "DEBATE_SEATS", "L1_INSTRUCTIONS", "L2_INSTRUCTIONS",
    "STATUS_CONCLUDED", "STATUS_SKIPPED", "STATUS_UNRESOLVED",
    "DebateError", "DebateResult", "debate_band", "run_debate",
]

SeatResponder = Callable[[str, str, str], Awaitable[str]]
"""The injected read-only model call: ``response(seat, instructions, input) -> str``."""

SEAT_L1 = "l1"
SEAT_L2 = "l2"
DEBATE_SEATS = (SEAT_L1, SEAT_L2)

STATUS_CONCLUDED = "concluded"
STATUS_UNRESOLVED = "unresolved"
STATUS_SKIPPED = "skipped"

MIN_SLIDER = 60  # A slider value at or below this never starts a debate.

L2_INSTRUCTIONS = (
    "You are the L2 adversarial seat of a read-only pre-plan debate. You may not call tools or change anything. "
    "Attack the strongest current answer to the topic: state the single most decision-changing objection, or say "
    "plainly that no decisive objection remains. Be concrete; a rename or rephrase of an already-answered point is "
    "not a new objection."
)
L1_INSTRUCTIONS = (
    "You are the L1 arbiter of a read-only pre-plan debate. You may not call tools or change anything. "
    "Weigh the topic against the latest L2 challenge and the record so far. Reply with exactly one JSON object and "
    "no other text: {\"conclusion\": \"<the settled answer, or empty>\", \"open_issue\": \"<one concrete question "
    "that would still change the decision, or empty>\"}. An open issue must be specific enough to decide the debate; "
    "do not repeat an issue already raised."
)

_FENCE = re.compile(r"```[a-zA-Z]*\s*(.*?)\s*```", re.DOTALL)


def debate_band(slider: int) -> tuple[int, int]:
    """Map the quality slider to ``(default rounds, hard cap)``; ``(0, 0)`` means no debate runs.

    N<=60 never starts; 61–70 get 1/2, 71–80 get 2/3, 81–95 get 3/4, 96–100 get 3/5.
    """
    n = max(0, min(100, int(slider)))
    if n <= MIN_SLIDER:
        return (0, 0)
    if n <= 70:
        return (1, 2)
    if n <= 80:
        return (2, 3)
    if n <= 95:
        return (3, 4)
    return (3, 5)


class DebateError(Exception):
    """A seat reply broke the debate protocol (e.g. L1 returned no JSON decision)."""


@dataclass
class DebateResult:
    """What one ``run_debate`` call produced. ``conclusion`` is set only when ``status`` is ``concluded``."""

    status: str
    topic: str
    rounds: int = 0
    """Rounds completed by this call."""
    total_rounds: int = 0
    """``prior_rounds`` plus ``rounds`` — the per-request count a rename cannot reset."""
    conclusion: str = ""
    open_issue: str = ""
    """The unresolved question when the debate stopped without a conclusion."""
    transcript: list[dict[str, Any]] = field(default_factory=list)

    @property
    def concluded(self) -> bool:
        return self.status == STATUS_CONCLUDED

    @property
    def unresolved(self) -> bool:
        return self.status == STATUS_UNRESOLVED


def _normalize(text: str) -> str:
    return " ".join(text.split()).lower()


def _decision(text: str) -> tuple[str, str]:
    """Parse L1's JSON verdict into ``(conclusion, open_issue)``; a malformed verdict is a failure."""
    raw = text.strip()
    fenced = _FENCE.search(raw)
    if fenced is not None:
        raw = fenced.group(1)
    try:
        data = json.loads(raw)
    except ValueError:
        start, end = raw.find("{"), raw.rfind("}")
        if not 0 <= start < end:
            raise DebateError("L1 returned no JSON decision.") from None
        try:
            data = json.loads(raw[start:end + 1])
        except ValueError:
            raise DebateError("L1 returned no JSON decision.") from None
    if not isinstance(data, dict):
        raise DebateError("L1 returned a non-object decision.")
    conclusion, issue = data.get("conclusion", ""), data.get("open_issue", "")
    if not isinstance(conclusion, str) or not isinstance(issue, str):
        raise DebateError("L1 decision fields must be strings.")
    return conclusion.strip(), issue.strip()


def _round_input(topic: str, transcript: list[dict[str, Any]], challenge: str | None = None) -> str:
    parts = ["Topic: " + topic]
    for entry in transcript:
        parts.append(f"L2 challenge, round {entry['round']}: {entry['challenge']}")
        parts.append(f"L1 decision, round {entry['round']}: "
                     f"conclusion={entry['conclusion'] or '(none)'} open_issue={entry['open_issue'] or '(none)'}")
    if challenge is not None:
        parts.append("L2 challenge, current round: " + challenge)
    return "\n\n".join(parts)


async def run_debate(slider: int, topic: str, response: SeatResponder, *,
                     prior_rounds: int = 0, prior_issues: Iterable[str] = (),
                     on_round: Callable[[int], None] | None = None) -> DebateResult:
    """Run the read-only L1–L2 debate for one topic and report its verdict honestly.

    ``response`` performs one controlled tool-free call per seat. ``prior_rounds`` and ``prior_issues`` keep the
    count of a topic that was debated before: they count toward the default and the cap, so renaming the topic
    cannot buy new rounds. ``on_round`` is called with the absolute round number the moment a round starts —
    before its L2 request — so a round a seat failed on is still spent. N<=60 (or a cap already spent) returns
    ``skipped`` without touching the responder. Stops early on a conclusion; rounds beyond the default run only
    while L1 names a non-empty new issue; the cap is a hard stop reported as ``unresolved``. Responder and
    decision errors propagate.
    """
    default, cap = debate_band(slider)
    result = DebateResult(status=STATUS_SKIPPED, topic=topic, total_rounds=max(0, prior_rounds))
    if cap <= 0 or result.total_rounds >= cap or not topic.strip():
        return result

    seen = {_normalize(issue) for issue in prior_issues if _normalize(issue)}
    result.status = STATUS_UNRESOLVED
    while result.total_rounds < cap:
        round_no = result.total_rounds + 1
        if on_round is not None:
            on_round(round_no)
        challenge = await response(SEAT_L2, L2_INSTRUCTIONS, _round_input(topic, result.transcript))
        if not isinstance(challenge, str) or not challenge.strip():
            raise DebateError("L2 returned an empty challenge.")
        conclusion, issue = _decision(await response(SEAT_L1, L1_INSTRUCTIONS,
                                                   _round_input(topic, result.transcript, challenge.strip())))
        result.transcript.append({"round": round_no, "challenge": challenge.strip(),
                                  "conclusion": conclusion, "open_issue": issue})
        result.rounds += 1
        result.total_rounds += 1
        if conclusion and not issue:
            result.status, result.conclusion, result.open_issue = STATUS_CONCLUDED, conclusion, ""
            return result
        new_issue = bool(issue) and _normalize(issue) not in seen
        if issue:
            result.open_issue = issue
            seen.add(_normalize(issue))
        if result.total_rounds >= default and not new_issue:
            break  # Past the default only a non-empty new issue may extend the debate.
    return result
