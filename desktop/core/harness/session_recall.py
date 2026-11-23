"""On-demand recall of this session's archived original text (final design §5-§6,
``outputs/session-context-recall-final.md``).

Two judgements, one hard gate, one shared ledger. A turn never opens the archive for an ordinary ask; a
trigger word inside a quote, a tool result or a negated clause opens nothing; and when the active view
already holds the needed verbatim evidence nothing is read — a summary can support a conclusion but cannot
stand in for the user's own words. When the engine does read, it walks committed generations newest-first
along ``header.parent`` itself — the caller asks once, it does not page generation by generation — stops as
soon as every asked object is resolved, and returns the smallest excerpts that answer the ask, each with its
provenance. A newer generation's revision wins; a withdrawal without a replacement reports insufficiency
instead of resurrecting the withdrawn value, and a record targeted by a revision or withdrawal is never
delivered, whichever generation it sits in.

The archive side is not implemented yet, so it arrives as two synchronous callbacks:

- ``latest()`` -> ``int | None``: the newest committed generation number, ``None`` when nothing is archived.
- ``generation(g)`` -> ``Mapping | None``: that committed snapshot as
  ``{"header": {"parent": int | None, "covers": {"evicted": [id, ...]}}, "records": [...],
    "evicted": [id, ...], "parent": int | None}``. The top-level ``evicted`` is the range ``publish``
  actually moved out of the active window — records in the dump but not in it were archived by an older
  generation (or still live) and are filtered out, so one original is never delivered twice or mistaken
  for a new decision. The dump header's ``covers.evicted`` is only the wider pre-compaction candidate,
  honoured when the published top-level list is absent. ``None`` or a malformed snapshot marks a gap —
  missing archives are reported, never filled in.

A record is one verbatim event: ``{"id": ..., "item": {"type": "message", "role": ..., "content": ...}}``
(a wrapped Responses item) or directly ``{"id": ..., "role": ..., "content": ...}``. Optional extras:
``kind`` — ``record`` (default), ``revise`` or ``withdraw`` — and ``targets``, the record ids a revision
replaces or a withdrawal negates.

The caller supplies what only the runtime knows: ``user_text`` (the real user message, checked for an
explicit or declined verbatim ask), ``current_view`` (the items about to go to the model), ``ledger`` — a
``recall_budget.RecallLedger`` bound to the real user turn (``<sid>\\turn-budget.json`` survives backend
switches, retries and restarts; a corrupt ledger fails closed) — and ``room``, the context margin left for
the request. Charging is UTF-8-conservative by default and happens before delivery; already-delivered
records are never sent again, so a retry or parallel call cannot sneak evidence past the budget.

Wiring example, for the controller that registers a ``recall_history`` tool::

    ledger = RecallLedger(session_dir / FILENAME, turn_id)
    text = recall_history(query=args["question"], reason=args.get("reason", ""),
                          user_text=current_user_text, current_view=view_items,
                          latest=archive.latest, generation=archive.read_generation,
                          ledger=ledger, room=context_margin)
    return ToolResult(text, is_error=text.startswith("[recall] denied"))
"""

from __future__ import annotations

import re
import secrets
from collections.abc import Callable, Mapping, Sequence
from typing import Any

from desktop.core.harness import compact, history
from desktop.core.harness.recall_budget import RecallLedger, utf8_bound

EXCERPT_PAD = 160
"""Context characters kept either side of the matched terms in an excerpt."""
EXCERPT_MAX = 2400
MAX_PER_SUBJECT = 4

_TRIGGERS = re.compile(
    r"原话|原文|原句|逐字|一字不差|原封不动|贴出|贴一下|引用我|"
    r"exact\s+words?|verbatim|word\s+for\s+word|quote[ds]?\s+(?:me|my)|"
    r"what\s+(?:did|was)\s+i\s+(?:say|said|wrote|write)|original\s+(?:text|message|wording)",
    re.IGNORECASE)
_QUOTED = re.compile(r"```.*?```|`[^`]*`|「[^」]*」|『[^』]*』|“[^”]*”|‘[^’]*’|\"[^\"\n]*\"|'[^'\n]*'", re.S)
_NEGATION = re.compile(
    r"不[要用需必会能可敢]?|没[有]?|别|勿|莫|休|无需|不用|不必|不要|"
    r"\b(?:do\s*not|don't|dont|never|no\s+need|without|needn't|not)\b", re.IGNORECASE)
_CLAUSE = re.compile(r"[，。；！？、,.;!?\n]+")
_FILLER = re.compile(
    r"那句话|那句|这句|一句|一段话|当时|那时|之前|以前|刚才|刚刚|刚|曾经|"
    r"原话|原文|原句|逐字逐句|逐字|一字不差|原封不动|贴出来|贴出|贴一下|贴|"
    r"找出来|找出|找到|找一下|找|翻出来|翻出|翻一下|翻|查一下|查看|查|引用我|引用|引|复述|重复|"
    r"多少|多|少|什么|怎么|怎样|是不是|是否|有没有|把|将|请|求|帮我|帮忙|给我|告诉我|和我说|跟我说|"
    r"我们|我|你|您|他|她|它|大家|一下|的|了|呢|吗|吧|啊|呀|嘛|在|是|有|说|讲|写|提|提到|说过|讲过|写过|"
    r"看看|看一下|看|有些|些|点|"
    r"exact\s+words?|verbatim|word\s+for\s+word|quote[ds]?|"
    r"what\s+(?:did|was)\s+i\s+(?:say|said|wrote|write)|original\s+(?:text|message|wording)s?|"
    r"please|the|an?|my|me|you(?:r)?|i|find|show|give|tell|session|conversation|"
    r"this|that|these|those|in|on|at|of|to|for|again|out|back|earlier|before|just|now|did|do|is|are|was|were",
    re.IGNORECASE)
_GENERIC = re.compile(
    r"背景|上下文|资料|信息|内容|东西|更多|多点|一点|一些|任何|全部|所有|"
    r"background|context|more|information|details?|stuff|earlier|previous", re.IGNORECASE)
_TERM = re.compile(r"[a-z0-9_]{2,}|[\u4e00-\u9fff]")
_STOPCHARS = frozenset("的了着过们和或及与就又再还也最很")


# ---------------------------------------------------------------------- terms and intent


def _terms(text: str) -> frozenset[str]:
    return frozenset(_TERM.findall(text.lower()))


def _content_terms(text: str) -> frozenset[str]:
    """Terms that can refer to concrete evidence: glue, question and generic words removed."""
    return _terms(_GENERIC.sub(" ", _FILLER.sub(" ", text))) - _STOPCHARS


def _clauses(text: str) -> list[str]:
    return [clause.strip() for clause in _CLAUSE.split(_QUOTED.sub(" ", text)) if clause.strip()]


def _asks_verbatim(text: str) -> bool:
    """A verbatim ask in an unquoted, unnegated clause."""
    return any(_TRIGGERS.search(c) and not _NEGATION.search(c) for c in _clauses(text))


def _declines_verbatim(text: str) -> bool:
    """The user actively declined: a trigger whose clause carries a negation."""
    return any(_TRIGGERS.search(c) and _NEGATION.search(c) for c in _clauses(text))


def _hit(terms: frozenset[str], text: str) -> tuple[bool, frozenset[str]]:
    """Exact: every content term appears. Fuzzy: at least half, and at least two shared — anything weaker is
    not evidence ("弱关联"), just a word in common."""
    if not terms:
        return False, frozenset()
    shared = terms & _terms(text)
    score = len(shared) / len(terms)
    return (score >= 0.999 or (score >= 0.5 and len(shared) >= 2)), shared


# ---------------------------------------------------------------------- records and view


def _item_text(item: Mapping[str, Any]) -> tuple[str, str]:
    """(author, verbatim text) of a record payload: a wrapped Responses item or direct role/content."""
    kind = item.get("type")
    if kind == "message" or (kind is None and "content" in item):
        content = item.get("content")
        text = history.message_text(item) if isinstance(content, list) else str(content or "")
        return str(item.get("role") or ""), text
    if "output" in item:
        return "tool", str(item.get("output") or "")
    return "", ""


def _record(rec: Any) -> tuple[str, str, str, str, frozenset[str]] | None:
    """(id, author, text, kind, targets) of one archived record, or None when malformed."""
    if not isinstance(rec, Mapping) or not rec.get("id"):
        return None
    wrapped = rec.get("item")
    author, text = _item_text(wrapped if isinstance(wrapped, Mapping) else rec)
    if not text:
        return None
    kind = str(rec.get("kind") or "record")
    return (str(rec["id"]), author, text, kind if kind in ("record", "revise", "withdraw") else "record",
            frozenset(str(t) for t in (rec.get("targets") or ())))


def _view_text(item: Mapping[str, Any]) -> str:
    """Text of a view item that can stand as the user's or the assistant's own words: a message, never a
    summary, never a tool result, never a question (a question asks; it is not the answer's evidence)."""
    if compact.is_summary(item) or item.get("type") != "message":
        return ""
    text = history.message_text(item)
    return "" if text.strip().endswith(("?", "？")) else text


def _excerpt(text: str, terms: frozenset[str], pad: int) -> str:
    """The smallest span covering the matched terms, ``pad`` context characters either side, ellipsised."""
    if not terms:
        return text[:EXCERPT_MAX]
    starts, ends = [], []
    for term in terms:
        for match in re.finditer(re.escape(term), text, re.IGNORECASE):
            starts.append(match.start())
            ends.append(match.end())
    if not starts:
        return text[:EXCERPT_MAX]
    start, end = min(starts), max(ends)
    if end - start > EXCERPT_MAX:
        end = start + EXCERPT_MAX
    lo, hi = max(0, start - pad), min(len(text), end + pad)
    if hi - lo > EXCERPT_MAX:
        hi = lo + EXCERPT_MAX
    return ("…" if lo else "") + text[lo:hi] + ("…" if hi < len(text) else "")


def _clip_bytes(text: str, budget: int) -> str:
    """Fit inside a UTF-8 byte budget — marker included — without splitting a character."""
    if budget <= 0:
        return ""
    out = text.encode("utf-8")[:budget].decode("utf-8", "ignore")
    if len(out) == len(text):
        return out
    return text.encode("utf-8")[:max(0, budget - len("…".encode("utf-8")))].decode("utf-8", "ignore") + "…"


# ---------------------------------------------------------------------- the service


def _subjects(text: str) -> list[tuple[str, frozenset[str]]]:
    """(label, content terms) pairs — clauses that still carry concrete terms once glue and generic words
    are removed; nothing left is a vague request, which never opens the archive."""
    return [(clause, terms) for clause in _clauses(text) if (terms := _content_terms(clause))]


def _covered(need: list[tuple[str, frozenset[str]]], view: Sequence[Mapping[str, Any]],
             ) -> tuple[list[str], list[tuple[str, frozenset[str]]]]:
    """Judgement 2: subjects the active view already answers. A recall ask itself is not evidence."""
    texts = [text for item in view if (text := _view_text(item)) and not _asks_verbatim(text)]
    covered, open_subjects = [], []
    for subject, terms in need:
        if any(len(terms & _terms(text)) == len(terms) for text in texts):
            covered.append(subject)
        else:
            open_subjects.append((subject, terms))
    return covered, open_subjects


def _deliver(rec: tuple[str, str, str, str, frozenset[str]], terms: frozenset[str], gen: int,
             ledger: RecallLedger, room: int | None, out: list[str], gaps: list[str]) -> bool:
    """Charge first, deliver second; the piece — provenance, excerpt and its line break — is charged as
    the bytes returned, and shrinks to the room left rather than overflowing it. A ``spend`` that answers
    0 means the record was already delivered this turn, possibly by a parallel call that won the race:
    nothing is sent again. Returns False when the record could not be delivered at all."""
    rid, author, text, _, _ = rec
    if rid in ledger.delivered:
        gaps.append(f"{rid} was already delivered this turn")
        return True
    provenance = f"[g{gen} · {rid} · {author}]\n"
    piece = ""
    for pad in (EXCERPT_PAD, 40, 0):
        block = provenance + _excerpt(text, terms, pad) + "\n"
        if utf8_bound(block) <= ledger.remaining(room):
            piece = block
            break
    if not piece:
        piece = (provenance
                 + _clip_bytes(_excerpt(text, terms, 0),
                               ledger.remaining(room) - utf8_bound(provenance + "\n")) + "\n")
    if len(piece) <= len(provenance) + 1:
        gaps.append(f"{rid} does not fit the remaining recall budget")
        return False
    charged = ledger.spend(rid, piece, room=room)
    if charged is None:
        gaps.append(f"{rid} does not fit the remaining recall budget")
        return False
    if not charged:
        gaps.append(f"{rid} was already delivered this turn")
        return True
    out.append(piece)
    return True


def recall_history(*, query: str, reason: str = "", user_text: str = "",
                   current_view: Sequence[Mapping[str, Any]] = (),
                   latest: Callable[[], int | None] | None = None,
                   generation: Callable[[int], Mapping[str, Any] | None] | None = None,
                   ledger: RecallLedger | None = None, room: int | None = None) -> str:
    """Gate + search + spend, in one call: the model (or the user's own ask) gets the smallest attributed
    excerpts the turn can still afford, and nothing else. Returns the text the caller hands to the model."""
    ledger = ledger if ledger is not None else RecallLedger()
    if user_text and _declines_verbatim(user_text):
        return "[recall] denied: the user declined verbatim recall of this session."
    asked = _asks_verbatim(user_text)
    if not asked and not _content_terms(reason):
        return ("[recall] denied: give a concrete reason for reading this session's archive; "
                "without the user's explicit verbatim ask, a vague or missing reason opens nothing.")
    need = _subjects(query) or (_subjects(user_text) if asked else [])
    if not need:
        return ("[recall] denied: name the concrete object to recall (a decision, quote or value); "
                "vague requests do not open the archive.")
    if ledger.remaining(room) <= 0:
        return "[recall] insufficient: the turn's recall budget is spent; answer without it."
    covered, open_subjects = _covered(need, current_view)
    if not open_subjects:
        return ("[recall] the needed original text is already in the active view; "
                "answer from it without reading the archive.")
    if latest is None or generation is None or (gid := latest()) is None:
        return "[recall] insufficient: no committed generation to read."
    out, gaps, blocked, seen = [], [f"already in the active view: {c}" for c in covered], set(), set()
    while gid is not None and open_subjects:
        if gid in seen:
            gaps.append(f"generation {gid} cycles back; stopped")
            break
        seen.add(gid)
        try:
            snap = generation(gid)
        except Exception:
            snap = None
        if not isinstance(snap, Mapping):
            gaps.append(f"generation {gid} is missing or unreadable")
            break
        header = snap.get("header") if isinstance(snap.get("header"), Mapping) else {}
        covers = header.get("covers") if isinstance(header.get("covers"), Mapping) else {}
        # The top-level ``evicted`` is what publish() actually moved out of the window; the dump header's
        # ``covers.evicted`` is the wider pre-compaction candidate — fallback for callers that only stage
        # the header shape. A staged record outside the real range still lives in the window: never read.
        covered = snap.get("evicted") if isinstance(snap.get("evicted"), list) else covers.get("evicted")
        evicted = set(str(x) for x in covered) if isinstance(covered, list) else None
        records = [r for r in (_record(item) for item in snap.get("records") or ()) if r
                   and (evicted is None or r[0] in evicted)]
        blocked.update(t for r in records if r[3] in ("revise", "withdraw") for t in r[4])
        for subject, terms in list(open_subjects):
            hits = [r for r in records if r[0] not in blocked and _hit(terms, r[2])[0]]
            if not hits:
                continue
            open_subjects.remove((subject, terms))
            withdrawn = [r for r in hits if r[3] == "withdraw"]
            for rec in (withdrawn or [r for r in hits if r[3] != "withdraw"][:MAX_PER_SUBJECT]):
                shared = terms & _terms(rec[2])
                if not _deliver(rec, shared, gid, ledger, room, out, gaps):
                    break
            if withdrawn:
                gaps.append(f"'{subject}' was withdrawn at generation {gid} without a replacement")
        gid = snap.get("parent", header.get("parent"))
        gid = int(gid) if isinstance(gid, (int, str)) and str(gid).isdigit() else None
    gaps += [f"no original text found for '{subject}'" for subject, _ in open_subjects]
    hard = [gap for gap in gaps if "already" not in gap]
    status = "delivered" if not open_subjects and not hard else "insufficient"
    if not out:
        return f"[recall] {status}: " + ("; ".join(gaps) + "." if gaps else "nothing found.")
    frame_lines = [f"[gap] {gap}" for gap in gaps]
    frame_lines.append(f"[recall] {status}: {len(out)} excerpt(s).")
    frame = "".join(line + "\n" for line in frame_lines)
    # Status and gaps ride along with the evidence, so they are charged like it — under a unique key, since
    # every call's frame is new text. When they do not fit, the evidence still goes back without them.
    if ledger.spend(f"#frame:{secrets.token_hex(8)}", frame, room=room) is not None:
        return "".join(out) + frame
    minimal = f"[recall] {status}.\n"
    if ledger.spend(f"#frame:{secrets.token_hex(8)}", minimal, room=room) is not None:
        return "".join(out) + minimal
    return "".join(out)
