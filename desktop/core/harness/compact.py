"""Keeping a conversation inside the context window of the model that reads it.

A conversation is two lists. ``SavedSession.items`` is the log: everything that was said and done, never shortened (the
page rebuilds what it shows from it). The **view** is what the next request sends: the log from ``compaction["upto"]`` on,
behind one message that holds a summary of everything before it. Without a compaction the view is the log.

Which items at the start of a view are not the log is known from the compaction record (``synthetic_count``), never read
from what a message says: a user may paste a summary into a new conversation, and that message is theirs. The functions
that need it are told (``log_index``, ``choose_cut``, ``compact``).

When the next request would not fit (the size is measured by the last response and estimated for what came after it),
the part of the view before a cut is summarised by the model and the view becomes ``[summary] + the part after the cut``.
The cut is where a request could begin: every tool call before it has its output, and it never splits a reasoning item
from the call it led to. Switching to a model with a smaller window is the same thing seen from the other side: the next
request is too big for the new window, and the next step compacts.

A compaction is kept only when it did its job (``Compacted.fits``): the view is smaller than before, and the whole request
(the instructions and the tool declarations too, ``fixed``) leaves the share of the window the answer needs (``budget``).
When even the least a cut keeps word for word is too big (a step whose tool results fill the window by themselves), the
whole view is summarised, that step included; the messages the user wrote in the turn that is running then go with the
summary word for word (``Compacted.kept``), so that what they asked for is not left to a summary.

The summary is asked for in pieces when what is to be summarised is itself too big for one request (the previous summary
goes into each next piece). What the user wrote is never cut for it: a message too big for one request is given in
several (``summary_parts``); the tool results and the assistant's own words are cut in the middle. Should the summary
fail, a mechanical digest (what the user asked) takes its place: a conversation that cannot go on because it cannot be
summarised is worse than one that forgot some detail.
"""

from __future__ import annotations

import json
import logging
import re
from collections.abc import AsyncIterator, Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from desktop.core.chatgpt.responses import Finished, Shape, TextDone, build_request, declare_tools, user_message

log = logging.getLogger("desktop.harness.compact")

TAIL_SHARE = 0.25
"""The newest part of the conversation that stays word for word: up to this share of the window."""
RESERVE_SHARE = 0.05
"""The share of the window a request leaves for the model's answer (its reasoning and its text)."""
SUMMARY_ROOM = 4_000
"""What the summary is expected to take when the place of the cut is chosen (it is asked to stay under about 1,500
words); at most a twentieth of the window."""
CHUNK_SHARE = 0.40
CHUNK_FLOOR = 12_000
CHUNK_CEILING = 150_000
"""What goes to the model in one summarising request, in tokens (a share of the window, between these)."""
IMAGE_TOKENS = 1_500
PROGRESS = 1.2
"""A second compaction in the same turn waits until the view has grown by this factor since the first one."""

SUMMARY_PREFIX = (
    "[Summary of the earlier part of this conversation. WittyCode wrote it because the conversation had grown past what "
    "the model can read at once. It is the record of what happened before; the messages after it are the latest.]"
)
KEPT_HEAD = (
    "[What the user wrote in the turn where this summary stops, word for word. The summary above says what was done "
    "about it.]"
)

INSTRUCTIONS = (
    "You keep the memory of a long conversation between a developer and a coding assistant, so that the assistant can go "
    "on without the conversation itself. You are given the previous summary (when there is one) and the next part of the "
    "conversation. Write the new summary: it replaces the previous one and covers it and this part.\n\n"
    "Keep, as plainly as you can: what the user wants and every instruction, preference or constraint they gave; "
    "decisions taken and the reason; the state of the work (files created or changed and what was done to them, commands "
    "run and what they showed, what works and what does not); exact names that will be needed again (paths, identifiers, "
    "commands, error messages, numbers); open problems and the next step. Drop greetings, repetition, and tool output "
    "that did not matter. Do not invent anything and do not answer the user. Write in the language the user mostly used. "
    "Use short headed sections or bullets, and stay under about 1,500 words."
)

RESULT_LIMIT = 1_200
ARGS_LIMIT = 500
TEXT_LIMIT = 6_000
"""Characters of one answer of the assistant the summary request reads (the middle is left out). The user's messages are
not cut (``summary_parts``)."""
DIGEST_LIMIT = 6_000
DIGEST_ITEM = 300
PIECE_LABEL = 40
"""Tokens set aside for the label of one piece of a long message of the user."""


class CompactionFailed(Exception):
    """The summary could not be written (the request failed, or the answer was empty or cut short)."""


# ---------------------------------------------------------------------- how big


def estimate_text(text: str) -> int:
    """Tokens of a text, on the high side: Latin text is about one token in 3.5 characters, any other character about one."""
    plain = len(text.encode("ascii", "ignore"))
    return int(plain / 3.5) + (len(text) - plain) + 1


def estimate_item(item: Mapping[str, Any]) -> int:
    kind = item.get("type")
    if kind == "message":
        total = 4
        for part in item.get("content") or ():
            if not isinstance(part, Mapping):
                continue
            if part.get("type") == "input_image":
                total += IMAGE_TOKENS
            else:
                total += estimate_text(str(part.get("text") or ""))
        return total
    if kind == "function_call":
        return 8 + estimate_text(str(item.get("name") or "")) + estimate_text(str(item.get("arguments") or ""))
    if kind == "function_call_output":
        return 6 + estimate_text(str(item.get("output") or ""))
    if kind == "reasoning":
        summary = " ".join(str(part.get("text") or "") for part in item.get("summary") or () if isinstance(part, Mapping))
        return 4 + estimate_text(summary) + len(str(item.get("encrypted_content") or "")) // 5
    return estimate_text(json.dumps(item, ensure_ascii=False))


def estimate_items(items: Sequence[Mapping[str, Any]]) -> int:
    return sum(estimate_item(item) for item in items)


def fixed_tokens(instructions: str, declarations: Sequence[Mapping[str, Any]], shape: Shape = Shape()) -> int:
    """Tokens of what every request holds besides the conversation: the instructions and the tools, as they are sent."""
    return estimate_text(instructions) + estimate_text(json.dumps(declare_tools(declarations, shape), ensure_ascii=False))


def reserve(window: int) -> int:
    """What a request leaves of a window of this size for the answer."""
    return int(window * RESERVE_SHARE)


def budget(window: int) -> int:
    """The most a whole request may hold in a window of this size (the instructions and the tools counted)."""
    return window - reserve(window)


def keep_tokens(window: int) -> int:
    """The most of the newest part of the conversation a compaction keeps word for word."""
    return int(window * TAIL_SHARE)


# ---------------------------------------------------------------------- the view


def _text_of(item: Mapping[str, Any]) -> str:
    return "".join(
        str(part.get("text") or "") for part in item.get("content") or () if isinstance(part, Mapping) and part.get("type") in ("input_text", "output_text")
    )


def _images_of(item: Mapping[str, Any]) -> int:
    return sum(1 for part in item.get("content") or () if isinstance(part, Mapping) and part.get("type") == "input_image")


def is_user(item: Mapping[str, Any]) -> bool:
    return item.get("type") == "message" and item.get("role") == "user"


def is_summary(item: Mapping[str, Any]) -> bool:
    """Whether a message reads like a summary of ours. A hint for people and tests only: whether an item of a view is the
    summary follows from where it is (``synthetic_count``), never from what it says (a user may paste one)."""
    return is_user(item) and _text_of(item).startswith(SUMMARY_PREFIX)


def summary_item(text: str, kept: str = "") -> dict[str, Any]:
    """The message that stands for the summarised part: the summary, then what the user wrote word for word (``kept``)."""
    body = f"{SUMMARY_PREFIX}\n\n{text.strip()}"
    if kept.strip():
        body += f"\n\n{KEPT_HEAD}\n\n{kept.strip()}"
    return user_message(body)


def summary_text(item: Mapping[str, Any]) -> str:
    return _text_of(item).removeprefix(SUMMARY_PREFIX).strip()


def synthetic_count(compaction: Mapping[str, Any] | None) -> int:
    """How many items ``view_of`` puts before the log: one, the summary, when there is a compaction."""
    return 1 if compaction else 0


def kept_of(compaction: Mapping[str, Any]) -> str:
    """The words of the user that go with the summary of a compaction (``Compacted.kept``)."""
    kept = compaction.get("kept")
    return kept if isinstance(kept, str) else ""


def view_of(items: Sequence[dict[str, Any]], compaction: Mapping[str, Any] | None) -> list[dict[str, Any]]:
    """What the next request sends: the log, or the summary and the log after what it covers. A new list."""
    if not compaction:
        return list(items)
    upto = max(0, min(int(compaction.get("upto") or 0), len(items)))
    return [summary_item(str(compaction.get("summary") or ""), kept_of(compaction)), *items[upto:]]


def log_index(cut: int, upto: int, synthetic: int) -> int:
    """The place in the log of ``view[cut]``, for a view that begins with ``synthetic`` items of its own
    (``synthetic_count``) and then holds the log from ``upto`` on."""
    return upto + cut - synthetic


def boundary_ok(items: Sequence[Mapping[str, Any]], upto: int) -> bool:
    """Whether a view may begin at ``items[upto]`` (the end included): every call before it has its output before it, and
    it does not part a reasoning item from what follows it. A place that splits a step would send an output without the
    call it answers."""
    if not 0 <= upto <= len(items):
        return False
    if upto == 0:
        return True
    pending: set[str] = set()
    for item in items[:upto]:
        kind = item.get("type")
        if kind == "function_call":
            pending.add(str(item.get("call_id") or ""))
        elif kind == "function_call_output":
            pending.discard(str(item.get("call_id") or ""))
    return not pending and items[upto - 1].get("type") != "reasoning"


def mend_boundary(items: Sequence[Mapping[str, Any]], upto: int) -> int:
    """``upto`` moved forward to the first place a view may begin (``boundary_ok``). A summary is made of whole steps, so
    outputs that follow the place while their calls lie before it were summarised with their calls."""
    upto = max(0, min(upto, len(items)))
    while upto < len(items) and not boundary_ok(items, upto):
        upto += 1
    return upto


# ---------------------------------------------------------------------- where to cut


def cut_points(view: Sequence[Mapping[str, Any]]) -> list[int]:
    """Indices at which a request could begin: every call before has its output, and the item before is not a reasoning
    item (it belongs to what follows it)."""
    pending: set[str] = set()
    points: list[int] = []
    for index, item in enumerate(view):
        if index > 0 and not pending and view[index - 1].get("type") != "reasoning":
            points.append(index)
        kind = item.get("type")
        if kind == "function_call":
            pending.add(str(item.get("call_id") or ""))
        elif kind == "function_call_output":
            pending.discard(str(item.get("call_id") or ""))
    return points


def ends_whole(view: Sequence[Mapping[str, Any]], synthetic: int = 0) -> bool:
    """Whether all of the view after its own ``synthetic`` items could be summarised: every call in it has its output,
    and it ends with neither a message of the user (that is what is being answered, not a step to fold in) nor a
    reasoning item."""
    if len(view) <= synthetic:
        return False
    last = view[-1]
    return not is_user(last) and last.get("type") != "reasoning" and boundary_ok(view, len(view))


def choose_cut(view: Sequence[Mapping[str, Any]], keep_tokens: int, room: int | None = None, *, synthetic: int = 0) -> int | None:
    """Where the kept part begins, after the view's own ``synthetic`` items (the earlier summary is never cut off or
    summarised by itself): the earliest cut whose rest fits ``keep_tokens`` (at a message of the user when there is one
    that does, so that a turn is not split without need), else the last cut, which keeps the least. Given ``room`` (what
    the kept part may hold at most for the request to fit), when even that least is too big, the end of the view:
    everything is summarised, the step that filled the window included (``ends_whole``). ``None``: nothing before a cut
    worth summarising."""
    points = [point for point in cut_points(view) if point > synthetic]
    end = len(view) if room is not None and ends_whole(view, synthetic) else None
    if not points:
        return end
    sizes = [estimate_item(item) for item in view]
    rest = [0] * (len(view) + 1)
    for index in range(len(view) - 1, -1, -1):
        rest[index] = rest[index + 1] + sizes[index]
    users = [point for point in points if is_user(view[point])]
    for pool in (users, points):
        fitting = [point for point in pool if rest[point] <= keep_tokens]
        if fitting:
            return fitting[0]
    if end is not None and rest[points[-1]] > room:
        return end
    return points[-1]


# ---------------------------------------------------------------------- what the model reads


def _clip(text: str, limit: int) -> str:
    text = text.strip()
    if len(text) <= limit:
        return text
    head = limit * 2 // 3
    return f"{text[:head]}\n[... {len(text) - limit} characters left out ...]\n{text[-(limit - head):]}"


def _said(item: Mapping[str, Any]) -> str:
    text = _text_of(item)
    images = _images_of(item)
    if images:
        text += f"\n[{images} image(s) attached]"
    return text.strip()


def render(item: Mapping[str, Any]) -> str:
    """One item of the log as a line or two of plain text for the summary request ('' for what carries nothing). The
    user's words are given whole; the assistant's answers, the arguments of its calls and their results are cut in the
    middle when they are long."""
    kind = item.get("type")
    if kind == "message":
        text = _said(item)
        if not text:
            return ""
        if item.get("role") == "user":
            return f"USER: {text}"
        return f"ASSISTANT: {_clip(text, TEXT_LIMIT)}"
    if kind == "function_call":
        return f"ASSISTANT CALLED TOOL {item.get('name')}: {_clip(str(item.get('arguments') or ''), ARGS_LIMIT)}"
    if kind == "function_call_output":
        return f"TOOL RESULT: {_clip(str(item.get('output') or ''), RESULT_LIMIT)}"
    return ""


def render_parts(items: Sequence[Mapping[str, Any]]) -> list[str]:
    return [part for part in (render(item) for item in items) if part]


def _longest(text: str, start: int, budget: int) -> int:
    """The largest ``end`` (at least one character past ``start``) with ``estimate_text(text[start:end]) <= budget``."""
    low, high = start + 1, min(len(text), start + int(max(budget, 1) * 3.5) + 1)
    while low < high:
        middle = (low + high + 1) // 2
        if estimate_text(text[start:middle]) <= budget:
            low = middle
        else:
            high = middle - 1
    return low


def pieces(text: str, budget: int) -> list[str]:
    """``text`` in consecutive pieces of at most about ``budget`` tokens, nothing left out (a piece ends at a line break
    when there is one in its last tenth)."""
    out: list[str] = []
    start = 0
    while start < len(text):
        end = _longest(text, start, budget)
        if end < len(text):
            newline = text.rfind("\n", start + (end - start) * 9 // 10, end)
            if newline > start:
                end = newline + 1
        out.append(text[start:end])
        start = end
    return out


def summary_parts(items: Sequence[Mapping[str, Any]], budget: int) -> list[str]:
    """What the summary requests read (``render``): a message of the user too big for one request is given in labelled
    pieces of it, so that none of what the user asked for is left out before the model has read it."""
    parts: list[str] = []
    for item in items:
        text = render(item)
        if not text:
            continue
        if is_user(item) and estimate_text(text) > budget:
            split = pieces(_said(item), budget - PIECE_LABEL)
            parts += [f"USER (one message, part {number} of {len(split)}): {piece}" for number, piece in enumerate(split, 1)]
        else:
            parts.append(text)
    return parts


def chunks(parts: Sequence[str], budget: int) -> list[list[str]]:
    """``parts`` in order, grouped so that each group is about ``budget`` tokens; a part bigger than that is cut down."""
    out: list[list[str]] = []
    current: list[str] = []
    used = 0
    for part in parts:
        if estimate_text(part) > budget:
            part = _clip(part, max(budget * 3, 200))
        size = estimate_text(part)
        if current and used + size > budget:
            out.append(current)
            current, used = [], 0
        current.append(part)
        used += size
    if current:
        out.append(current)
    return out


def chunk_budget(window: int) -> int:
    return max(CHUNK_FLOOR, min(CHUNK_CEILING, int(window * CHUNK_SHARE)))


def digest(items: Sequence[Mapping[str, Any]], previous: str = "") -> str:
    """What the user asked, one line each: the summary of last resort."""
    lines = [f"- {_clip(_text_of(item), DIGEST_ITEM)}" for item in items if is_user(item) and _text_of(item).strip()]
    while lines and sum(len(line) for line in lines) > DIGEST_LIMIT:
        lines.pop(0)
    head = "(The earlier part of the conversation could not be summarised. This is only what the user asked, oldest first; the rest was left out.)"
    body = "\n".join(lines)
    return "\n\n".join(part for part in (previous.strip(), head, body) if part)


def _joined(*texts: str) -> str:
    return "\n\n".join(text.strip() for text in texts if text and text.strip())


# ---------------------------------------------------------------------- asking the model


Stream = Callable[[dict[str, Any]], AsyncIterator[Any]]


def _request(model: str, reasoning: str | None, previous: str, part: Sequence[str]) -> dict[str, Any]:
    body = "<previous_summary>\n" + (previous.strip() or "(none: this is the beginning)") + "\n</previous_summary>\n\n"
    body += "<conversation_part>\n" + "\n\n".join(part) + "\n</conversation_part>\n\n"
    body += "Write the new summary now."
    return build_request(
        model=model,
        instructions=INSTRUCTIONS,
        items=[user_message(body)],
        tools=[],
        reasoning=reasoning,
        shape=Shape(encrypted_reasoning=False, reasoning_summary=None, parallel_tool_calls=False),
    )


OnUsage = Callable[[Any], None]
"""Told the usage of each summary request (they cost tokens like any other)."""


async def ask(stream: Stream, model: str, reasoning: str | None, previous: str, part: Sequence[str], on_usage: OnUsage | None = None) -> str:
    texts: list[str] = []
    async for event in stream(_request(model, reasoning, previous, part)):
        if isinstance(event, TextDone):
            texts.append(event.text)
        elif isinstance(event, Finished):
            if event.usage is not None and on_usage is not None:
                on_usage(event.usage)
            if event.incomplete:
                raise CompactionFailed(f"the summary was cut short ({event.incomplete})")
    text = "\n".join(texts).strip()
    if not text:
        raise CompactionFailed("the summary came back empty")
    return text


async def summarise(
    stream: Stream, model: str, reasoning: str | None, previous: str, parts: Sequence[str], budget: int, on_usage: OnUsage | None = None
) -> str:
    summary = previous
    for group in chunks(parts, budget):
        summary = await ask(stream, model, reasoning, summary, group, on_usage)
    return summary


# ---------------------------------------------------------------------- the whole thing


@dataclass(frozen=True, slots=True)
class Compacted:
    view: list[dict[str, Any]]
    """The new view: the summary and what was kept."""
    cut: int
    """In the old view: its items before this one (after its own) are what the summary covers."""
    summary: str
    """What the model wrote (or the digest)."""
    before: int
    after: int
    """Estimated tokens of the view before and after."""
    mechanical: bool
    """The summary is a digest of what the user asked, not the model's (``problem`` says why)."""
    problem: str = ""
    kept: str = ""
    """What the user wrote in the turn that is running, where the summary covers it: it goes with the summary word for
    word (``summary_item``)."""
    fits: bool = True
    """It did its job: the view got smaller and the whole request fits. A result that does not is not to be kept."""


async def compact(
    view: Sequence[dict[str, Any]],
    *,
    stream: Stream,
    model: str,
    reasoning: str | None,
    window: int,
    fixed: int = 0,
    synthetic: int = 0,
    previous: str | None = None,
    kept: str = "",
    turn: int | None = None,
    on_usage: OnUsage | None = None,
    whole: bool = False,
    fallback: bool = True,
) -> Compacted | None:
    """Summarise the front of ``view`` so that a request of it fits ``window``; ``fixed``: the tokens every request holds
    besides the view (instructions, tools). ``None``: there is nothing that could be cut.

    ``synthetic``: how many items at the start of ``view`` are its own (the summary of an earlier compaction, whose text
    is ``previous``, the text of ``view[0]`` when not given, and whose words of the user are ``kept``). ``turn``: where the
    turn that is running begins in ``view`` (its first message); below ``synthetic`` it began in what the earlier summary
    covers, and ``kept`` is its own; ``None``: at the last message of the user.

    ``whole``: the user asked for it (``/compact``), no turn is running: the whole conversation becomes the summary when it
    ends at a finished step, rather than only what does not fit. ``fallback``: ``False`` lets a summary that could not be
    written raise instead of becoming the digest of what the user asked (the digest is for a conversation that must
    shrink; a user who asked for a summary is told it failed)."""
    room = budget(window) - fixed
    if whole and ends_whole(view, synthetic):
        cut: int | None = len(view)
    else:
        cut = choose_cut(view, keep_tokens(window), room - min(SUMMARY_ROOM, window // 20), synthetic=synthetic)
    if cut is None:
        return None
    if previous is None:
        previous = summary_text(view[0]) if synthetic else ""
    if turn is None:
        # (``whole``: no turn is running, so none of the user's words is the running turn's to keep)
        turn = len(view) if whole else next((index for index in range(len(view) - 1, synthetic - 1, -1) if is_user(view[index])), len(view))
    # (the words kept with the earlier summary are the running turn's when it began in what that summary covers: kept
    # again; else they are of an earlier turn, and the new summary takes them in)
    carried = kept if turn < synthetic else ""
    told = _joined(previous, "" if carried or not kept.strip() else f"{KEPT_HEAD}\n\n{kept}")
    covered = view[synthetic:cut]
    chunk = chunk_budget(window)
    parts = summary_parts(covered, chunk)
    problem = ""
    mechanical = False
    try:
        summary = await summarise(stream, model, reasoning, told, parts, chunk, on_usage)
    except CompactionFailed as error:
        if not fallback:
            raise
        problem, mechanical = str(error), True
    except Exception as error:  # (a stop is a CancelledError and passes through) a failed request must not end the conversation
        if not fallback:
            raise
        log.warning("the conversation could not be summarised: %s", error)
        problem, mechanical = f"{type(error).__name__}: {error}", True
    if mechanical:
        summary = digest(covered, told)
    words = _joined(carried, *(_said(item) for item in view[max(turn, synthetic) : cut] if is_user(item)))
    rest = list(view[cut:])
    new_view = [summary_item(summary, words), *rest]
    before, after = estimate_items(view), estimate_items(new_view)
    return Compacted(new_view, cut, summary, before, after, mechanical, problem, words, after < before and after <= room)


# ---------------------------------------------------------------------- the server says no

_OVERFLOW_TEXT = re.compile(
    r"context[ _](?:length|window)|maximum context|input (?:is )?too (?:long|large)|prompt (?:is )?too (?:long|large)|"
    r"too many (?:input )?tokens|exceeds? (?:the )?(?:model'?s )?(?:context|maximum (?:context|input|token))",
    re.IGNORECASE,
)
_OVERFLOW_CODES = frozenset({"context_length_exceeded", "input_too_long", "string_above_max_length"})


def is_overflow(error: BaseException) -> bool:
    """Whether the server refused a request because the conversation is too big for the model."""
    code = str(getattr(error, "code", "") or "")
    if code.startswith("subscription_sharing"):
        return False
    if code in _OVERFLOW_CODES:
        return True
    message = str(getattr(error, "message", "") or error)
    return bool(_OVERFLOW_TEXT.search(message))
