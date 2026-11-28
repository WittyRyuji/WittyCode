"""The real bridge from a declared complex topic to a read-only L1–L2 debate.

``discuss`` is called by the controller's ``declare_topic`` handler only when the main Fusion model declared the
discussion complex. It runs ``debate.run_debate`` with one tool-free, read-only request per seat — L1 on the policy
main model, L2 on the first worker — at each seat's configured reasoning level, over ``manager.stream`` for ChatGPT
and API models or ``AcpSummary`` for Devin. Budget is checked before every seat request, usage is charged like the
title path, a seat that calls a tool, times out, or returns nothing usable is a failure, never a success.
The ledger is keyed by a fingerprint of the turn's real user request, never by the topic string the model can
rename at will: a retried turn, a renamed topic or a subtask without its own user request lands on the same
account, and a round is spent the moment its L2 request starts, so a failed seat cannot hand it back.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import logging
from collections.abc import Mapping
from typing import TYPE_CHECKING, Any

from desktop.core.chatgpt.errors import ChatGPTError, RequestNotSent
from desktop.core.chatgpt.responses import CallBegun, CallDone, Finished, Started, TextDelta, TextDone, build_request, user_message
from desktop.core.fusion.allocation import SEAT_ROLE
from desktop.core.fusion.debate import STATUS_CONCLUDED, DebateError, DebateResult, run_debate
from desktop.core.fusion.registry import FusionError
from desktop.core.harness import compact
from desktop.core.harness.diagnostics import MAX_RECORDS

if TYPE_CHECKING:
    from desktop.core.chatgpt.controller import ChatGPTController

log = logging.getLogger(__name__)
SEAT_SECONDS = 120.0
MAX_INPUT = 24_000
MAX_OUTPUT = 16_000


def _bound(text: str) -> str:
    """Keep the topic line and the latest context inside one request's input limit."""
    if len(text) <= MAX_INPUT:
        return text
    head = text.partition("\n")[0]
    return head + "\n…\n" + text[-(MAX_INPUT - len(head) - 4):]


def _norm(text: str) -> str:
    return " ".join(text.split()).lower()


_LEDGER = "fusionDebate"
_LEDGER_ENTRIES = 24  # The most recent user-request entries kept; the oldest evict first.
_LEDGER_ISSUES = 8    # Remembered open-issue keys per entry.
_LEDGER_TEXT = 400    # Stored issue keys and the declared topic stay short — never user input.


def _ledger(controller: ChatGPTController) -> dict[str, dict[str, Any]]:
    """The session's bounded durable ledger in ``saved.origin``: fingerprints and short fields only.

    Entries keyed ``u:<hash>`` belong to a real user request (its text is hashed, never stored); keyed ``t:<norm>``
    to a declared topic used when no user request or pending entry could be identified. The dict reads back from
    disk sanitized: the type, count and length bounds below are re-applied to whatever the file carried.
    """
    raw = controller.saved.origin.get(_LEDGER)
    entries = raw.get("entries") if isinstance(raw, dict) else None
    clean: dict[str, dict[str, Any]] = {}
    if isinstance(entries, dict):
        for key, item in entries.items():
            if not isinstance(key, str) or not isinstance(item, dict):
                continue
            rounds, issues, conclusion = item.get("rounds"), item.get("issues"), item.get("conclusion")
            clean[key] = {
                "rounds": rounds if isinstance(rounds, int) and not isinstance(rounds, bool) and rounds >= 0 else 0,
                "issues": [i[:_LEDGER_TEXT] for i in issues if isinstance(i, str)][:_LEDGER_ISSUES]
                          if isinstance(issues, list) else [],
                "conclusion": conclusion[:2000] if isinstance(conclusion, str) else "",
                "topic": item["topic"][:_LEDGER_TEXT] if isinstance(item.get("topic"), str) else "",
            }
    while len(clean) > _LEDGER_ENTRIES:
        clean.pop(next(iter(clean)))
    controller.saved.origin[_LEDGER] = {"entries": clean}
    return clean


def _request_key(controller: ChatGPTController) -> str | None:
    """The trusted ledger key: a fingerprint of this turn's real user request — not of any model-chosen string."""
    block = getattr(getattr(controller, "_turn", None), "user_block", None)
    text = getattr(block, "text", None)
    if not isinstance(text, str) or not text.strip():
        return None
    return "u:" + hashlib.sha256(_norm(text).encode("utf-8", "ignore")).hexdigest()[:24]


def _pending_key(entries: dict[str, dict[str, Any]]) -> str | None:
    """The newest still-open entry, where a declare without its own user request (a retry or a subtask) belongs."""
    for key in reversed(list(entries)):
        if not entries[key].get("conclusion"):
            return key
    return None


def _turn_cache(controller: ChatGPTController) -> dict[str, DebateResult]:
    """Repeat declares inside one turn get the stored result; only the turn's id is kept, never the Turn."""
    state = getattr(controller, "_fusion_debate", None)
    turn_id = getattr(getattr(controller, "_turn", None), "id", None)
    if not isinstance(state, dict) or state.get("turn") != turn_id or not isinstance(state.get("results"), dict):
        state = controller._fusion_debate = {"turn": turn_id, "results": {}}
    return state["results"]


def result_text(result: DebateResult) -> str:
    """The tool-result text the model sees for one debate outcome."""
    if result.status == "skipped":
        return "The topic was not debated: the quality slider is too low or its rounds are spent. Answer directly."
    if result.status == "concluded":
        return (f"Debate concluded after {result.total_rounds} round(s). Return this as the discussion's answer; "
                f"do not delegate or plan execution unless the user asked for it.\n\n{result.conclusion}")
    open_issue = f"\n\nOpen issue: {result.open_issue}" if result.open_issue else ""
    return (f"Debate stopped after {result.total_rounds} round(s) without a conclusion; report this honestly, "
            f"do not call it settled.{open_issue}")


DECLARE_TOPIC = "declare_topic"
_MAX_OPENING_CHARS = 240
DECLARE_TOPIC_DECLARATION: dict[str, Any] = {
    "name": DECLARE_TOPIC,
    "description": (
        "Declare what this user turn is before acting on it. kind: 'simple' when no debate is needed, "
        "'complex' for an open question that needs the read-only L1\u2013L2 debate the harness runs. intent: "
        "'discussion' when the user only wants an answer, 'plan' when they want a plan, 'execute' when they want the "
        "change carried out. A complex kind runs the debate once; its verdict comes back as this tool's result. "
        "plan/execute intents also return the team's model profile snapshot for planning. One declaration per turn: "
        "renaming the topic does not reopen it. For substantive work, use next_step to tell the user what you will do first."
    ),
    "parameters": {"type": "object", "properties": {
        "kind": {"type": "string", "enum": ["simple", "complex"], "description": "Whether this turn needs the debate."},
        "intent": {"type": "string", "enum": ["discussion", "plan", "execute"], "description": "What the user asked for."},
        "topic": {"type": "string", "description": "The question under discussion, in one line."},
        "next_step": {"type": "string", "maxLength": _MAX_OPENING_CHARS, "description": "Optional one-line update in the reply "
                      "language: what you will do next, not hidden reasoning, results or an announcement that experts started."},
    }, "required": ["kind", "intent", "topic"]},
}
DECLARE_TOPIC_GUIDANCE = (
    "Answer plain chat directly, without an opening or declaration. Before follow-up work, use declare_topic once "
    "and put one short, useful next step for the user in next_step (do not send a separate message). "
    "Use kind=simple when no debate is needed, "
    "kind=complex for an open question that benefits from the read-only L1\u2013L2 debate; intent says whether the "
    "user wants a discussion, a plan or execution. The harness owns the debate's rounds and verdict — you only "
    "declare. Simple execution can still delegate to Coder. A discussion intent ends in the answer: do not plan or "
    "delegate. Plan/execute intents receive the team's model snapshot after any debate; in Plan mode you still "
    "finish by submitting the plan for the user's approval."
)


SUBMIT_WORK_PLAN = "submit_work_plan"
SUBMIT_WORK_PLAN_DECLARATION: dict[str, Any] = {
    "name": SUBMIT_WORK_PLAN,
    "description": (
        "Submit this turn's V3 work plan: the `data` JSON object the planning contract appended to the team model "
        "snapshot describes. The harness validates it, freezes its items for routing and keeps it as this turn's "
        "planning record. Only the main agent calls it, and only after a plan or complex-execute declaration. "
        "The JSON is internal planning data — the user-facing answer stays prose."
    ),
    "parameters": {"type": "object", "properties": {
        "data": {"type": "object", "description": "The work plan as one JSON object."}},
        "required": ["data"]},
}


def parse_topic(args: Mapping[str, Any]) -> tuple[tuple[str, str, str, str] | None, str]:
    """Validate the routing fields; ignore unusable display text rather than changing the route."""
    kind, intent, topic = args.get("kind"), args.get("intent"), args.get("topic")
    if kind not in ("simple", "complex") or intent not in ("discussion", "plan", "execute"):
        return None, "kind must be simple|complex and intent must be discussion|plan|execute."
    if not isinstance(topic, str) or not topic.strip() or len(topic) > 2000:
        return None, "Provide a non-empty topic of at most 2000 characters."
    next_step = args.get("next_step")
    next_step = " ".join(next_step.split()) if isinstance(next_step, str) and len(next_step) <= _MAX_OPENING_CHARS else ""
    return (kind, intent, " ".join(topic.split()), next_step), ""


async def discuss(controller: ChatGPTController, topic: str) -> DebateResult:
    """Run the read-only L1–L2 debate for one declared complex topic and charge its real usage.

    Raises ``FusionError`` outside Fusion mode or without a usable seat model; a stopped controller, a seat that
    touches tools or a malformed reply ends the debate in an honest failure (``DebateError``), not a conclusion.
    """
    fusion = controller._fusion
    if fusion is None:
        raise FusionError("A debate only runs in Fusion mode.")
    topic = " ".join(topic.split())
    if not topic:
        raise FusionError("A debate needs a topic.")
    policy = fusion.registry.policy
    turn = controller._turn
    entries = _ledger(controller)
    key = _request_key(controller) or _pending_key(entries) or "t:" + _norm(topic)
    entry = entries.pop(key, {"rounds": 0, "issues": [], "conclusion": "", "topic": ""})
    entries[key] = entry  # A touched entry becomes the newest account.
    if not entry["topic"]:
        entry["topic"] = topic[:_LEDGER_TEXT]
    cache = _turn_cache(controller)
    if key in cache:
        return cache[key]  # One declare per request per turn: repeated asks must not reopen rounds.
    if entry["conclusion"]:
        # The issue was settled before, possibly in an earlier session: re-read the answer, do not re-debate it.
        result = DebateResult(status=STATUS_CONCLUDED, topic=topic,
                              total_rounds=entry["rounds"], conclusion=entry["conclusion"])
        cache[key] = result
        return result

    def alive() -> bool:
        return (not controller._close_requested and controller._fusion is fusion
                and controller._turn is turn and not getattr(turn, "stopping", False))

    async def seat(seat: str, instructions: str, text: str) -> str:
        from desktop.core.fusion.runtime import native_model
        if not alive():
            raise DebateError("The debate was stopped.")
        page = policy.seat_model(seat.upper())
        model = native_model(page)
        from desktop.core.fusion.backends import GUARDED
        from desktop.core.fusion.summary import seat_stream
        native = model.startswith(GUARDED)
        items = [user_message(_bound(text))]
        fusion.check_budget(compact.estimate_items(items) + compact.estimate_text(instructions))
        request = build_request(model=model, instructions=instructions, items=items, tools=[],
                                reasoning=fusion.reasoning_for(page, SEAT_ROLE[seat.upper()]), shape=controller.manager.shape)
        stream = seat_stream(controller, model, before_prompt=alive)
        body, finished, tool_call, requested = "", None, False, not native
        try:
            async with asyncio.timeout(SEAT_SECONDS), contextlib.aclosing(stream(request)) as events:
                async for event in events:
                    if isinstance(event, Started):
                        requested = True
                    elif isinstance(event, TextDelta):
                        body += event.text
                        if len(body) > MAX_OUTPUT:
                            raise DebateError("The debate seat exceeded the reply limit.")
                    elif isinstance(event, TextDone):
                        body = event.text or body
                        if len(body) > MAX_OUTPUT:
                            raise DebateError("The debate seat exceeded the reply limit.")
                    elif isinstance(event, (CallBegun, CallDone)):
                        tool_call = True  # A debate seat has no declarations and no executor.
                    elif isinstance(event, Finished):
                        finished = event
                        break
            if finished is not None and not finished.incomplete and not tool_call and alive() and body.strip():
                return body
            raise DebateError("The debate seat did not produce a usable reply.")
        except (DebateError, asyncio.CancelledError):
            raise
        except Exception as error:
            if isinstance(error, RequestNotSent):
                requested = False  # A proven local failure cannot have consumed tokens.
            raise DebateError(f"The debate seat failed: {type(error).__name__}.") from error
        finally:
            if requested:
                usage = finished.usage if finished is not None else None
                fusion.record_request(finished, failed=finished is None or finished.incomplete or tool_call)
                if usage is not None:
                    controller._add_usage(usage, measures_context=False, counts_speed=False, model=model)
                controller.saved.requests.append({"kind": "debate", "seat": seat, "model": model,
                                                  "usage": usage.tokens().to_json() if usage is not None else None})
                del controller.saved.requests[:-MAX_RECORDS]

    def round_started(round_no: int) -> None:
        if round_no > entry["rounds"]:
            entry["rounds"] = round_no  # A started round is spent even when its seat then fails.
            controller._save_now()

    result = await run_debate(policy.preference, topic, seat,
                              prior_rounds=entry["rounds"], prior_issues=entry["issues"],
                              on_round=round_started)
    entry["rounds"] = max(entry["rounds"], result.total_rounds)
    issues = {_norm(item["open_issue"]) for item in result.transcript if item["open_issue"]} - {""}
    entry["issues"] = sorted({*entry["issues"], *issues})[:_LEDGER_ISSUES]
    if result.concluded:
        entry["conclusion"] = result.conclusion[:2000]
    cache[key] = result
    controller._save_now()
    return result
