"""Minimal real-Plan transport for the inventory resume eval, with one shared serial budget ledger.

``make_engine(account, root, budget, *, model, reasoning)`` builds the frozen ``ChatGPTEngine`` of the
current ``sys.path``: sessions and every other engine state live under ``root``, the single saved active
authorization is read (never written, never refreshed, never echoed) from the ``AccountStore`` at
``account``, and every generation ``POST https://api.openai.com/v1/responses`` is gated by the SQLite
ledger at ``budget`` — the one file both eval sides share, so A + B together may spend at most
``LIMIT`` tokens.

Ledger contract (table ``posts``): before the HTTP call goes out, one ``pending`` row reserves
``RESERVE`` (500000, the provider-confirmed context-window cap). Only a terminal
``response.completed`` / ``response.incomplete`` event carrying valid provider usage settles the row,
to its real ``max(input + output, total)`` tokens; every other ending — HTTP error, stream abort,
crash — leaves the row pending forever. A pending row both blocks every later POST (unknown usage
must not be undercounted) and still counts its full reservation. Nothing is retried silently: the
engine's own 401 second attempt can never pass ``begin`` while the first attempt is unresolved.
"""

from __future__ import annotations

import contextlib
import json
import sqlite3
import time
import urllib.parse
from collections.abc import AsyncIterator, Mapping
from pathlib import Path
from typing import Any

RESERVE = 500_000
"""Per-POST reservation: the provider-confirmed context-window ceiling for the eval model."""
LIMIT = 1_500_000
"""The whole eval's shared ceiling: baseline + candidate together, never reset."""
OFFICIAL_RESPONSES = "https://api.openai.com/v1/responses"
OFFICIAL_MODELS = "https://api.openai.com/v1/models"

SCHEMA = """CREATE TABLE IF NOT EXISTS posts (
    id INTEGER PRIMARY KEY,
    status TEXT NOT NULL,
    reserved INTEGER NOT NULL,
    used INTEGER,
    model TEXT NOT NULL,
    effort TEXT,
    input_tokens INTEGER,
    output_tokens INTEGER,
    cached_tokens INTEGER,
    reasoning_tokens INTEGER,
    cache_write_tokens INTEGER,
    seconds REAL,
    response_id TEXT,
    outcome TEXT,
    error TEXT,
    created_at REAL NOT NULL,
    settled_at REAL
);"""


class MeterBlocked(Exception):
    """A POST refused before anything was sent: unresolved usage, budget spent, or an off-plan request."""


class BudgetLedger:
    """The serial token budget shared by both eval sides: one SQLite file, one ``BEGIN IMMEDIATE`` writer."""

    def __init__(self, path: Path | str) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        con = self._connect()
        try:
            con.execute("PRAGMA journal_mode=WAL")
            con.execute("BEGIN IMMEDIATE")
            con.executescript(SCHEMA)
            con.commit()
        finally:
            con.close()

    def _connect(self) -> sqlite3.Connection:
        return sqlite3.connect(self.path, timeout=30.0, isolation_level=None)

    def begin(self, model: str, effort: str | None) -> int:
        """Reserve ``RESERVE`` for one POST. Refused while any earlier POST is unresolved (unknown usage
        stops everything) or when the reservation would cross ``LIMIT``; a crash between ``begin`` and
        ``settle`` keeps the row pending, so the reservation is never lost."""
        con = self._connect()
        try:
            con.execute("BEGIN IMMEDIATE")
            if con.execute("SELECT count(*) FROM posts WHERE status = 'pending'").fetchone()[0]:
                raise MeterBlocked("an earlier generation POST has no settled usage; later requests stop here")
            spent = con.execute(
                "SELECT COALESCE(SUM(CASE WHEN status = 'settled' THEN used ELSE reserved END), 0) FROM posts"
            ).fetchone()[0]
            if spent + RESERVE > LIMIT:
                raise MeterBlocked(f"the shared budget is spent ({int(spent)} of {LIMIT}); later requests stop here")
            cur = con.execute(
                "INSERT INTO posts (status, reserved, model, effort, created_at) VALUES ('pending', ?, ?, ?, ?)",
                (RESERVE, model, effort, time.time()),
            )
            con.commit()
            return int(cur.lastrowid)
        except BaseException:
            con.rollback()
            raise
        finally:
            con.close()

    def settle(
        self,
        post_id: int,
        *,
        used: int,
        input_tokens: int,
        output_tokens: int,
        cached_tokens: int,
        reasoning_tokens: int,
        cache_write_tokens: int | None,
        seconds: float,
        response_id: str,
        outcome: str,
    ) -> None:
        """Replace the reservation with the provider-reported usage (the only way a row stops blocking)."""
        con = self._connect()
        try:
            con.execute("BEGIN IMMEDIATE")
            con.execute(
                "UPDATE posts SET status='settled', used=?, input_tokens=?, output_tokens=?, cached_tokens=?, "
                "reasoning_tokens=?, cache_write_tokens=?, seconds=?, response_id=?, outcome=?, settled_at=? "
                "WHERE id=? AND status='pending'",
                (used, input_tokens, output_tokens, cached_tokens, reasoning_tokens, cache_write_tokens,
                 seconds, response_id, outcome, time.time(), post_id),
            )
            con.commit()
        finally:
            con.close()

    def fail(self, post_id: int, error: str, seconds: float) -> None:
        """Record how a POST ended without usage; the row stays ``pending`` — it keeps blocking and counting."""
        con = self._connect()
        try:
            con.execute("BEGIN IMMEDIATE")
            con.execute(
                "UPDATE posts SET seconds=?, error=? WHERE id=? AND status='pending'",
                (seconds, error[:200], post_id),
            )
            con.commit()
        finally:
            con.close()

    def totals(self) -> dict[str, int]:
        con = self._connect()
        try:
            spent = con.execute(
                "SELECT COALESCE(SUM(CASE WHEN status = 'settled' THEN used ELSE reserved END), 0) FROM posts"
            ).fetchone()[0]
            pending, settled = con.execute(
                "SELECT COALESCE(SUM(status = 'pending'), 0), COALESCE(SUM(status = 'settled'), 0) FROM posts"
            ).fetchone()
            return {"spent": int(spent), "pending": int(pending), "settled": int(settled), "limit": LIMIT}
        finally:
            con.close()

    def rows(self) -> list[dict[str, Any]]:
        con = self._connect()
        try:
            con.row_factory = sqlite3.Row
            return [dict(row) for row in con.execute("SELECT * FROM posts ORDER BY id").fetchall()]
        finally:
            con.close()


def _number(value: Any) -> int:
    return int(value) if isinstance(value, (int, float)) and not isinstance(value, bool) and value > 0 else 0


def _terminal_event(data_lines: list[str]) -> dict[str, Any] | None:
    """The terminal SSE event of one data block, when it carries a provider response."""
    text = "\n".join(data_lines).strip()
    if not text or text == "[DONE]":
        return None
    try:
        event = json.loads(text)
    except ValueError:
        return None
    if isinstance(event, Mapping) and event.get("type") in ("response.completed", "response.incomplete"):
        return dict(event)
    return None


def _usage_of(event: Mapping[str, Any], model: str) -> dict[str, Any] | None:
    """Provider usage from a terminal event, strictly checked: a response object, the same model when
    echoed, a usage mapping and real input/output counts. Anything else is unknown and must not settle."""
    response = event.get("response")
    if not isinstance(response, Mapping):
        return None
    echoed = response.get("model")
    if isinstance(echoed, str) and echoed and echoed != model:
        return None  # a silently substituted model is unknown usage
    usage = response.get("usage")
    if not isinstance(usage, Mapping):
        return None
    raw_input, raw_output = usage.get("input_tokens"), usage.get("output_tokens")
    # Strictly integers (a bool is not one here): input must be positive, output nonnegative; either
    # missing or mistyped is unknown usage, and unknown usage never settles.
    if type(raw_input) is not int or type(raw_output) is not int or raw_input <= 0 or raw_output < 0:
        return None
    input_tokens, output_tokens = raw_input, raw_output
    total = usage.get("total_tokens")
    details_in = usage.get("input_tokens_details")
    details_in = details_in if isinstance(details_in, Mapping) else {}
    details_out = usage.get("output_tokens_details")
    details_out = details_out if isinstance(details_out, Mapping) else {}
    return {
        "used": max(input_tokens + output_tokens, total if type(total) is int and total > 0 else 0),
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "cached_tokens": _number(details_in.get("cached_tokens")),
        "reasoning_tokens": _number(details_out.get("reasoning_tokens")),
        "cache_write_tokens": _number(details_in["cache_write_tokens"]) if "cache_write_tokens" in details_in else None,
        "response_id": str(response.get("id") or ""),
        "outcome": "incomplete" if event.get("type") == "response.incomplete" else "completed",
    }


class _MeteredHttp:
    """``HttpClient`` stand-in for this eval only: generation POSTs are gated and metered by the shared
    ledger; every other POST (and any redirect the transport might suggest) is refused outright."""

    def __init__(self, inner: Any, ledger: BudgetLedger, *, model: str, effort: str) -> None:
        self._inner = inner
        self._ledger = ledger
        self._model = model
        self._effort = effort
        self._window: int | None = None
        """Proof from the real ``GET /models`` answer: the frozen model's declared context window."""

    async def request(self, method: str, url: str, **kwargs: Any) -> Any:
        if method.upper() != "GET" or url != OFFICIAL_MODELS:
            raise MeterBlocked(f"plan_meter: only GET {_safe_url(OFFICIAL_MODELS)} is allowed ({method} {_safe_url(url)})")
        response = await self._inner.request(method, url, **kwargs)
        self._window = None  # every catalog answer re-decides the proof; a stale one never survives it
        if getattr(response, "status", 0) == 200:
            self.note_catalog(response.json())
        return response

    def note_catalog(self, payload: Any) -> int:
        """Record the provider-declared context window for the frozen model, from a real ``/models``
        answer — the proof every POST requires. A catalog that does not carry it is a refusal, and a
        failed re-check drops whatever proof was stored before."""
        self._window = None
        self._window = _catalog_window(payload, self._model, self._effort)
        return self._window

    async def stream_lines(
        self,
        method: str,
        url: str,
        *,
        headers: Mapping[str, str] | None = None,
        json_body: Any = None,
        body: bytes | None = None,
        timeout: float = 120.0,
    ) -> AsyncIterator[str]:
        if method.upper() != "POST" or url != OFFICIAL_RESPONSES:
            raise MeterBlocked(f"plan_meter: only POST {_safe_url(OFFICIAL_RESPONSES)} is allowed ({method} {_safe_url(url)})")
        post_id = self._begin(json_body)
        started = time.monotonic()
        inner = self._inner.stream_lines("POST", url, headers=headers, json_body=json_body, body=body, timeout=timeout)
        terminal: dict[str, Any] | None = None
        data: list[str] = []
        try:
            async with contextlib.aclosing(inner) as stream:
                async for line in stream:
                    if line == "":
                        found = _terminal_event(data)
                        data.clear()
                        if found is not None:
                            terminal = found
                    elif line.startswith("data:"):
                        data.append(line[5:].lstrip(" "))
                    yield line
            found = _terminal_event(data)
            if found is not None:
                terminal = found
        except GeneratorExit:
            # Closed early (e.g. the consumer stops after response.completed): usage already seen still
            # settles; an abort before any terminal event leaves the row pending.
            self._finish(post_id, terminal, time.monotonic() - started)
            raise
        except BaseException as error:
            self._ledger.fail(post_id, type(error).__name__, time.monotonic() - started)
            raise
        self._finish(post_id, terminal, time.monotonic() - started)

    def _finish(self, post_id: int, terminal: dict[str, Any] | None, seconds: float) -> None:
        usage = _usage_of(terminal, self._model) if terminal is not None else None
        if usage is not None:
            self._ledger.settle(post_id, seconds=seconds, **usage)
        else:
            self._ledger.fail(post_id, "no_terminal_event" if terminal is None else "invalid_usage", seconds)

    def _begin(self, json_body: Any) -> int:
        """Freeze-check the request, then take the reservation — the last step before the wire."""
        body = json_body if isinstance(json_body, Mapping) else {}
        if body.get("model") != self._model:
            raise MeterBlocked("plan_meter: the request model is not the eval's frozen model")
        reasoning = body.get("reasoning")
        effort = reasoning.get("effort") if isinstance(reasoning, Mapping) else None
        expected = self._effort if self._effort and self._effort != "off" else None
        if effort != expected:
            raise MeterBlocked("plan_meter: the request reasoning effort is not the eval's frozen effort")
        if self._window is None:
            raise MeterBlocked("plan_meter: no verified GET /models catalog yet; the POST is refused")
        return self._ledger.begin(self._model, self._effort)

    def __getattr__(self, name: str) -> Any:
        return getattr(self._inner, name)


def _catalog_window(payload: Any, model: str, effort: str) -> int:
    """The provider-declared context window of ``model`` — the proof required before any POST.

    RESERVE is this eval's confirmed ceiling, not a constant to hand future models or sign-ins: the
    model must be in the real catalog, its declared window must exist and not exceed ``RESERVE``, and a
    frozen effort must be among the declared levels. Anything less is unknown, and unknown is refused."""
    from desktop.core.chatgpt.models import models_from_result

    entry = next((item for item in models_from_result(payload) if item.id == model), None)
    if entry is None:
        raise MeterBlocked("plan_meter: the frozen model is not in the account's real model catalog")
    window = entry.context_window
    if window is None:
        raise MeterBlocked("plan_meter: the catalog declares no context window for the frozen model")
    if window > RESERVE:
        raise MeterBlocked(f"plan_meter: the catalog window {window} exceeds the eval reservation {RESERVE}")
    expected = effort if effort and effort != "off" else None
    if expected is not None and expected not in entry.efforts:
        raise MeterBlocked("plan_meter: the catalog does not declare the frozen reasoning effort for the model")
    return window


def _safe_url(url: str) -> str:
    """Host and path only: no query, no fragment, nothing a caller could hide credentials in."""
    parts = urllib.parse.urlsplit(url)
    return f"{parts.scheme}://{parts.netloc}{parts.path}"


def make_engine(account: Path, root: Path, budget: Path, *, model: str, reasoning: str) -> Any:
    """The frozen ``ChatGPTEngine`` of the current ``sys.path``, bound to the saved active Plan
    authorization under ``account`` (read-only) and metered by the shared ledger at ``budget``.

    - sessions and all other engine state live under ``root`` (``SessionStore`` at ``root/chatgpt``);
    - the account store may only be read: ``_read_state().get('active')`` plus ``get`` — no login, no
      switch, no token write;
    - ``TokenProvider._renewal`` is disabled: a token that needs refreshing fails closed instead of
      rewriting the account;
    - ``model`` and ``reasoning`` are frozen into the transport guard: a request carrying anything
      else never reaches the wire."""
    from desktop.core.chatgpt.accounts import AccountStore, TokenProvider
    from desktop.core.chatgpt.engine import ChatGPTEngine
    from desktop.core.chatgpt.errors import AuthError
    from desktop.core.chatgpt.http import HttpClient
    from desktop.core.harness.store import SessionStore
    from desktop.core.settings import AppSettings

    class _ReadOnlyStore(AccountStore):
        """``AccountStore`` restricted to the saved active authorization: read verbs only, no mutation."""

        def active_id(self) -> str | None:
            saved = self._read_state().get("active")
            return saved if isinstance(saved, str) else None

        def accounts(self) -> list[Any]:
            record = self.get(self.active_id())
            return [record] if record is not None else []

        def save(self, record: Any) -> None:
            raise MeterBlocked("plan_meter: the account store is read-only")

        def set_active(self, client_id: str | None) -> None:
            raise MeterBlocked("plan_meter: the account store is read-only")

        def remove(self, client_id: str) -> bool:
            raise MeterBlocked("plan_meter: the account store is read-only")

        def clear_tokens(self, client_id: str, **kwargs: Any) -> None:
            raise MeterBlocked("plan_meter: the account store is read-only")

    root = Path(root)
    engine = ChatGPTEngine(AppSettings(), root, http=HttpClient())
    engine.sessions = SessionStore(root / "chatgpt")
    engine.accounts = _ReadOnlyStore(Path(account))
    tokens = TokenProvider(engine.accounts, engine.http, engine.endpoints)

    async def _no_renewal(client_id: str, rejected: str | None = None) -> str:
        raise AuthError(
            "plan_meter: refreshing the sign-in would rewrite the account; failing closed instead",
            terminal=True,
            code="refresh_disabled",
        )

    async def _no_sign_out(client_id: str | None = None) -> bool:
        return False

    tokens._renewal = _no_renewal  # type: ignore[method-assign]
    tokens.sign_out = _no_sign_out  # type: ignore[method-assign]
    engine.tokens = tokens
    engine.http = _MeteredHttp(engine.http, BudgetLedger(budget), model=model, effort=reasoning)
    return engine
