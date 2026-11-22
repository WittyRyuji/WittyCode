"""The ``recall_history`` tool: on-demand recall of this session's archived original text.

The gate, the walk and the shared turn ledger live in ``session_recall`` / ``recall_budget``; this module is the
production adapter that gives the tools layer a runtime-authorized way in. ``bind_history`` installs a
``ToolContext.history_recall`` callback bound to the controller's ``_archive_binding``: the tool is declared only
while that binding carries a live ``archive`` (a ``SessionArchive`` — ``.latest()[0]`` is the newest committed
generation, ``read_generation`` yields its snapshot). With no binding nothing changes: no declaration, the name is
unknown to the model, and every test that counted the tool list stays green.

What the service needs and only the runtime has is read at call time, never at bind time: the real user message
(``turn.user_block.text`` — the injected ``sent`` text is not what the user wrote), the active view
(``compact.view_of`` of the session log, so an answer already in view reads nothing), the turn's own id for the
shared ledger (``<sid>/turn-budget.json`` survives a restart, a retry and a backend switch), and the context
margin the next request still has. ``tool_data`` (call arguments) can ask for an object but can never open the
archive by itself: the gate still requires a concrete need.

Fusion experts never get the parent session's archive: an expert's own context is never bound and
``toolset.identity_refusal`` refuses the name outright for an expert identity.

The result is session evidence delivered under the turn ledger, so it must not become retained output the ledger
never charged — ``read_output`` would otherwise re-serve the archive past the budget (final design §6.5). Two
layers keep that from happening: the observer skips retention for the names in ``outputs.UNRETAINED``, and a
result big enough to be retained carries a real reference to ``outputs.NOT_REPLAYABLE`` instead, so the archive
text never enters the store at all.

Wiring, in the controller once its archive binding exists::

    recall_tool.bind_history(self._context, self)   # in _ensure_tools, after the ToolContext is made
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from typing import Any

from desktop.core.harness import compact
from desktop.core.harness.recall_budget import FILENAME, RecallLedger
from desktop.core.harness.session_recall import _asks_verbatim, _content_terms, _declines_verbatim, recall_history
from desktop.core.harness.tools import ToolContext, ToolError, ToolResult

NAME = "recall_history"
UNAVAILABLE = "This session has no archive to recall."

DECLARATION: dict[str, Any] = {
    "name": NAME,
    "description": (
        "Recall this conversation's archived original text when the answer needs the session's own words and the "
        "active view no longer holds them (after compaction or in a long session). `question` must name the "
        "concrete object — a decision, quote, path or value; vague asks are refused. Give `reason` as the concrete "
        "gap the archive must fill — unless the user explicitly asked for this session's original words, a vague "
        "or missing reason opens nothing. Excerpts come with their source and are capped per user turn. Not for "
        "anything still in view, and never for another session."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "question": {"type": "string", "description": "The concrete object to recall: the terms the original text must contain."},
            "reason": {"type": "string", "description": "The concrete gap this must fill — required unless the user explicitly asked for the session's original words."},
        },
        "required": ["question"],
    },
}


def parse(args: Mapping[str, Any]) -> tuple[str, str, str]:
    """``(question, reason, problem)`` of a call; a non-empty ``problem`` means nothing may run."""
    if not isinstance(args, Mapping):
        return "", "", "The arguments must be a JSON object."
    question = args.get("question")
    if not isinstance(question, str) or not question.strip():
        return "", "", "Provide a non-empty `question`: the concrete object to recall (a decision, quote or value)."
    reason = args.get("reason")
    return question.strip(), reason.strip() if isinstance(reason, str) else "", ""


def _latest_of(archive: Any) -> Callable[[], "int | None"]:
    """The ``latest`` callback the service wants: ``archive.latest()`` answers ``(generation, parent)``."""

    def read() -> int | None:
        found = archive.latest()
        if isinstance(found, (tuple, list)):
            found = found[0] if found else None
        try:
            return int(found) if found is not None else None
        except (TypeError, ValueError):
            return None

    return read


class HistoryRecall:
    """The ``ToolContext.history_recall`` callback. Resolves the controller's ``_archive_binding`` at call time,
    so a binding that appears after the tools are made still opens the archive, and one that loses its ``archive``
    disables the tool without another wiring step."""

    def __init__(self, controller: Any) -> None:
        self.controller = controller

    # ---- the runtime authorization ----------------------------------------

    def _archive(self) -> Any:
        binding = getattr(self.controller, "_archive_binding", None)
        return getattr(binding, "archive", None) if binding is not None else None

    @property
    def enabled(self) -> bool:
        """A live archive authorizes recall; without one the tool is not even declared."""
        return self._archive() is not None

    # ---- what only the runtime knows ----------------------------------------

    def _user_text(self) -> str:
        turn = getattr(self.controller, "_turn", None)
        block = getattr(turn, "user_block", None) if turn is not None else None
        text = getattr(block, "text", "")
        return text if isinstance(text, str) else ""

    def _turn_id(self) -> str:
        """The live turn's own id, or "" — no turn means the ledger has nothing real to bind to, and a
        detached book would silently uncap the delivery, so the caller refuses rather than opening one."""
        turn = getattr(self.controller, "_turn", None)
        ident = getattr(turn, "id", "") if turn is not None else ""
        return str(ident) if ident else ""

    def _view(self) -> Sequence[Mapping[str, Any]]:
        saved = getattr(self.controller, "saved", None)
        items = getattr(saved, "items", None)
        if not isinstance(items, list):
            return ()
        try:
            return compact.view_of(items, getattr(saved, "compaction", None))
        except Exception:
            return ()

    def _room(self, view: Sequence[Mapping[str, Any]]) -> "int | None":
        """The context margin the next request still has; ``None`` leaves the ledger's own ceiling in charge."""
        try:
            manager = getattr(self.controller, "manager", None)
            window = manager.model_window(self.controller.running_model) if manager is not None else None
            if not window:
                return None
            saved = getattr(self.controller, "saved", None)
            prompt = getattr(saved, "prompt", None)
            instructions = str(prompt.get("instructions") or "") if isinstance(prompt, Mapping) else ""
            tools = getattr(self.controller, "_tools", None)
            declared = tools.declarations() if tools is not None else ()
            shape = getattr(manager, "shape", None)
            fixed = (compact.fixed_tokens(instructions, declared, shape) if shape is not None
                     else compact.fixed_tokens(instructions, declared))
            return max(0, compact.budget(int(window)) - compact.estimate_items(view) - fixed)
        except Exception:
            return None

    def _ledger(self, archive: Any, turn_id: str) -> RecallLedger:
        directory = getattr(archive, "dir", None)
        try:
            path = Path(directory) / FILENAME if directory is not None else None
        except (TypeError, ValueError):
            path = None
        return RecallLedger(path, turn_id)

    # ---- read_output under the same ledger -----------------------------------

    def output_provenance(self) -> "tuple[frozenset[str], frozenset[str], str] | None":
        """(refs of this turn's tool results, refs of earlier turns, the live turn id) — the runtime's own proof
        of which turn a retained output came from. A pasted ``o-…`` hash or a call argument proves nothing;
        ``None`` when there is no live turn to prove anything against."""
        turn_id = self._turn_id()
        if not turn_id:
            return None
        saved = getattr(self.controller, "saved", None)
        items = getattr(saved, "items", None)
        turns = getattr(saved, "turns", None)
        start = len(items) if isinstance(items, list) else 0
        if isinstance(turns, (list, tuple)) and turns:
            open_turn = turns[-1] if getattr(turns[-1], "finished", 0) is None else None
            boundary = getattr(open_turn, "start", None) if open_turn is not None else None
            if isinstance(boundary, int) and 0 <= boundary <= start:
                start = boundary
        this_turn, older = set(), set()
        for index, item in enumerate(items or ()):
            ref = item.get("wc_output_ref") if isinstance(item, Mapping) else None
            if isinstance(ref, str) and ref:
                (this_turn if index >= start else older).add(ref)
        return frozenset(this_turn), frozenset(older), turn_id

    def authorize_output_read(self, reference: str, reason: str = "") -> str:
        """Authorization for ``read_output`` in an archive-bound session — decided before the file is ever read.
        ``"free"`` for this turn's own tool outputs — the ``wc_output_ref`` in the live turn's log is the
        runtime's proof, not anything the model claims — or when no archive authorizes gating at all;
        ``"gated"`` for a reference an earlier turn's log recorded: session-archive evidence that must pass the
        same need gate and then the shared ledger. A hash that was never attached to a tool result of this
        session is refused outright: a listed or pasted ``o-…`` is not authorization, not even under the
        user's own explicit ask."""
        if not self.enabled:
            return "free"  # no archive authorization: the plain read stays as it was
        provenance = self.output_provenance()
        if provenance is None:
            raise ToolError("read_output cannot prove which turn this output belongs to; it is refused rather than guessed.")
        this_turn, older, _ = provenance
        if reference in this_turn:
            return "free"
        text = self._user_text()
        if text and _declines_verbatim(text):
            raise ToolError("[recall] denied: the user declined verbatim recall of this session.")
        if reference not in older:
            raise ToolError("[recall] denied: this reference is not a tool result the runtime recorded for this "
                            "session; a listed or pasted hash is not authorization to read it.")
        if not _asks_verbatim(text) and not _content_terms(reason):
            raise ToolError("[recall] denied: give a concrete reason for reading an earlier turn's retained output; "
                            "without the user's explicit verbatim ask, a vague or missing reason opens nothing.")
        return "gated"

    def charge_output_read(self, reference: str, offset: int, end: int, page_text: str) -> ToolResult:
        """Charges the final page — frame included — to the turn's shared recall ledger before it is handed
        over. A range once delivered is never sent or billed again; a page the budget cannot fit is reported,
        not delivered."""
        ledger = self._ledger(self._archive(), self._turn_id())
        charged = ledger.spend(f"#out:{reference}:{offset}-{end}", page_text, room=self._room(self._view()))
        if charged is None:
            return ToolResult("[recall] insufficient: the turn's recall budget cannot fit this page; "
                              "narrow the range or answer without it.")
        if not charged:
            return ToolResult(f"{reference} — characters {offset}-{end}: already delivered this turn; "
                              "answer from the earlier page without reading again.")
        return ToolResult(page_text)

    # ---- the call -----------------------------------------------------------

    def __call__(self, question: str, reason: str = "") -> str:
        archive = self._archive()
        if archive is None:
            return "[recall] denied: " + UNAVAILABLE
        turn_id = self._turn_id()
        if not turn_id:
            return "[recall] denied: there is no live user turn to charge the recall to."
        view = self._view()
        generation = getattr(archive, "read_generation", None)
        latest = _latest_of(archive) if callable(getattr(archive, "latest", None)) else None
        try:
            return recall_history(
                query=question, reason=reason,
                user_text=self._user_text(), current_view=view,
                latest=latest, generation=generation if callable(generation) else None,
                ledger=self._ledger(archive, turn_id), room=self._room(view))
        except Exception as error:
            return f"[recall] insufficient: the archive could not be read ({type(error).__name__})."


def bind_history(ctx: ToolContext, controller: Any) -> HistoryRecall:
    """Install this session's recall callback on the tools' context — one line in the controller once
    ``_archive_binding`` exists. Idempotent: rebinding the same controller keeps the callback already installed."""
    bound = ctx.history_recall
    if not isinstance(bound, HistoryRecall) or bound.controller is not controller:
        bound = HistoryRecall(controller)
        ctx.history_recall = bound
    return bound
