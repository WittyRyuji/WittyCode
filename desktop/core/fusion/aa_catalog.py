"""The public Artificial Analysis model leaderboard: the single external source of Fusion's model profiles.

``source()`` returns the ``profiles.ensure`` fetch: it downloads the leaderboard page once per ``CACHE_SECONDS``
window, parses its HTML table and matches the normalized model name. A scored Max row is preferred; otherwise
use the best measured score. Cost is the USD per benchmark task from that reference, not the user's actual price.

The request is a plain unauthenticated GET to one fixed https URL: no keys, no cookies, no body, and nothing about
the workspace or its sessions ever leaves the machine. Pure: no Qt; the network lives behind the injectable
``download``.
"""
from __future__ import annotations

import math
import re
import threading
import time
import urllib.parse
import urllib.request
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from html.parser import HTMLParser
from typing import Any

URL = "https://artificialanalysis.ai/leaderboards/models"
HOST = "artificialanalysis.ai"
MAX_BYTES = 4 * 1024 * 1024
"""The leaderboard is ~1.5 MB; a much larger answer is not the public table and is refused."""
TIMEOUT_SECONDS = 20.0
CACHE_SECONDS = 600.0
"""One page serves every prefetch in the window; ``profiles.ensure`` throttles per record across runs."""
FAIL_SECONDS = 60.0
"""A failed download is not retried for every seat of the same batch."""

_ANNOTATION = re.compile(r"^(?P<name>.+?)\s*\((?P<inside>[^()]{1,48})\)\s*$")
_TRAILING_NOTE = re.compile(r",\s*(?:default(?:\s+fallback)?|fallback)\s*$", re.IGNORECASE)
_EFFORT_TAIL = re.compile(r"^(?P<name>.+?)\s+(?P<effort>xhigh|max|high|medium|low|minimal|reasoning|standard)\s*$",
                          re.IGNORECASE)
_EFFORT_WORDS = frozenset(("minimal", "low", "medium", "high", "max", "xhigh", "off", "none", "nonreasoning",
                           "noreasoning", "dynamic", "auto", "standard", "adaptive", "reasoning", "thinking",
                           "minimaleffort", "loweffort", "mediumeffort", "higheffort"))
_DEVIN_EFFORT_TAIL = re.compile(r"-(max|xhigh|high|medium|low|minimal)$", re.IGNORECASE)
_NUMBER = re.compile(r"\d[\d,]*\.?\d*")


@dataclass(frozen=True, slots=True)
class Entry:
    """A benchmark row. The effort is only used to choose the representative row, not stored in the profile."""

    name: str
    effort: str | None
    score: float | None
    task_cost: float | None


def native_id(page_id: str) -> str:
    """The model name a page id leaves for the leaderboard: ``chatgpt:x`` -> ``x``, ``api:p:x`` -> ``x``,
    ``api:p:custom:x`` -> ``x`` (the name the custom entry was given). Unknown forms keep their text."""
    for prefix in ("chatgpt:", "devin:", "factory:"):
        if page_id.startswith(prefix):
            return page_id[len(prefix):]
    if page_id.startswith("api:"):
        rest = page_id[len("api:"):]
        marker = ":custom:"
        if marker in rest:
            return rest.partition(marker)[2]
        _, _, native = rest.partition(":")
        return native
    return page_id


def _norm(text: Any) -> str:
    """Fold to the bare key a leaderboard name and a model id can only share: lowercase letters and digits,
    so ``GPT-6.1 Sol``, ``gpt-6.1-sol`` and ``api:demo:GPT 6.1 SOL`` are the same model and only that model."""
    return re.sub(r"[^a-z0-9]+", "", str(text).casefold())


def _number(text: str | None) -> float | None:
    if not text:
        return None
    found = _NUMBER.search(text.replace(",", ""))
    if found is None:
        return None
    try:
        value = float(found.group())
    except ValueError:
        return None
    return value if math.isfinite(value) else None


def _split(name: str) -> tuple[str, str | None]:
    """The model's base name, the effort the row declares, and that tier's text as displayed, in the spellings the
    leaderboard uses: ``GPT-6-Luna (high)``, ``Claude Fable 5.2 Max, Default Fallback``, ``Grok 4.7 Xhigh``.
    A trailing parenthesis without a known effort word (``(Chat)``) stays part of the name."""
    text, effort = _TRAILING_NOTE.sub("", name.strip()).strip(), None
    annotated = _ANNOTATION.match(text)
    if annotated is not None:
        inside = [word for word in re.split(r"[^a-z]+", annotated["inside"].casefold()) if word]
        found = next((word for word in inside if word in _EFFORT_WORDS), None)
        if found is not None:
            text, effort = annotated["name"], found
    tail = _EFFORT_TAIL.match(text)
    if effort is None and tail is not None:
        text, effort = tail["name"], tail["effort"]
    return _norm(text), effort.casefold() if effort else None


class _Tables(HTMLParser):
    """The rows (cell texts, tags dropped) of every ``<table>`` in the page."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.tables: list[list[list[str]]] = []
        self._depth = 0
        self._table: list[list[str]] | None = None
        self._row: list[str] | None = None
        self._cell: list[str] | None = None

    def handle_starttag(self, tag: str, attrs: Any) -> None:
        if tag == "table":
            self._depth += 1
            if self._depth == 1:
                self._table = []
        elif self._depth:
            if tag == "tr":
                self._row = []
            elif tag in ("td", "th") and self._row is not None:
                self._cell = []

    def handle_data(self, data: str) -> None:
        if self._cell is not None:
            self._cell.append(data)

    def handle_endtag(self, tag: str) -> None:
        if tag in ("td", "th") and self._cell is not None:
            if self._row is not None:
                self._row.append(" ".join("".join(self._cell).split()))
            self._cell = None
        elif tag == "tr" and self._row is not None:
            if self._row and self._table is not None:
                self._table.append(self._row)
            self._row = None
        elif tag == "table" and self._depth:
            self._depth -= 1
            if not self._depth and self._table is not None:
                self.tables.append(self._table)
                self._table = None


def parse(html: str) -> list[Entry]:
    """The leaderboard rows of every table that has an Intelligence Index column; other tables are ignored.

    The ``Model``, ``Artificial Analysis Intelligence Index`` and ``Cost per Task USD`` positions come from
    the column header, not fixed indices. Unparseable or absent figures stay ``None``.
    """
    parser = _Tables()
    parser.feed(html)
    parser.close()
    entries: list[Entry] = []
    for table in parser.tables:
        header_at = next((index for index, row in enumerate(table)
                          if "model" in {_norm(cell) for cell in row}
                          and any("intelligence" in _norm(cell) for cell in row)), None)
        if header_at is None:
            continue
        header = [_norm(cell) for cell in table[header_at]]
        model_at = header.index("model")
        score_at = next((i for i, cell in enumerate(header) if "intelligence" in cell), None)
        cost_at = next((i for i, cell in enumerate(header) if "cost" in cell and "task" in cell), None)
        for row in table[header_at + 1:]:
            if len(row) <= model_at or not row[model_at]:
                continue
            name, effort = _split(row[model_at])
            if not name:
                continue
            def cell(index: int | None) -> float | None:
                return _number(row[index]) if index is not None and index < len(row) else None
            entries.append(Entry(name, effort, cell(score_at), cell(cost_at)))
    return entries


def offer(entries: list[Entry], model: str) -> dict[str, float | None] | None:
    """Choose a Max measurement, or the best scored row when there is no measured Max."""
    native = native_id(model)
    want = _norm(native)
    candidates = [entry for entry in entries if entry.name == want]
    if not candidates and model.startswith("devin:"):
        # Devin sometimes puts its default effort in the native model ID (e.g. swe-2-1-medium).
        base = _norm(_DEVIN_EFFORT_TAIL.sub("", native))
        candidates = [entry for entry in entries if entry.name == base]
    scored = [entry for entry in candidates if entry.score is not None]
    if scored:
        reference = [entry for entry in scored if entry.effort == "max"] or scored
        score = max(entry.score for entry in reference)
        costs = {entry.task_cost for entry in reference if entry.score == score and entry.task_cost is not None}
    else:
        score = None
        priced = [entry for entry in candidates if entry.task_cost is not None]
        reference = [entry for entry in priced if entry.effort == "max"] or priced
        costs = {entry.task_cost for entry in reference}
    cost = next(iter(costs)) if len(costs) == 1 else None
    return {"score": score, "cost": cost} if score is not None or cost is not None else None


class _SameHostRedirect(urllib.request.HTTPRedirectHandler):
    """A redirect may only stay on the leaderboard host, still over https; anything else is refused."""

    def redirect_request(self, req: Any, fp: Any, code: int, msg: str, headers: Any, newurl: str) -> Any:
        parts = urllib.parse.urlsplit(newurl)
        if parts.scheme != "https" or parts.netloc != HOST:
            return None
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def _urlopen(request: Any, timeout: float) -> Any:
    """The one place this module touches the network: an opener that cannot be redirected off the host.
    Tests replace this function — never urllib's global ``urlopen``, which other code relies on."""
    return urllib.request.build_opener(_SameHostRedirect()).open(request, timeout=timeout)


def download(url: str = URL, *, timeout: float = TIMEOUT_SECONDS, limit: int = MAX_BYTES,
             opener: Callable[..., Any] | None = None) -> str:
    """The leaderboard page. The URL is fixed and https-locked to the public host, and the default opener cannot
    be redirected off it either: nothing else is ever asked. ``opener`` is resolved when called so the suite's
    no-network guard covers the default path too."""
    parts = urllib.parse.urlsplit(url)
    if parts.scheme != "https" or parts.netloc != HOST:
        raise ValueError("Model profiles come only from the public Artificial Analysis leaderboard.")
    request = urllib.request.Request(url, headers={"User-Agent": "WittyCode Fusion model profiles"})
    opener = _urlopen if opener is None else opener
    with opener(request, timeout=timeout) as response:
        data = response.read(limit + 1)
    if len(data) > limit:
        raise ValueError("The leaderboard answer is larger than expected; it was not used.")
    return data.decode("utf-8", errors="replace")


class Leaderboard:
    """The parsed page, downloaded at most once per ``ttl``; a failure is not retried for ``FAIL_SECONDS``."""

    def __init__(self, *, loader: Callable[[], str] = download, ttl: float = CACHE_SECONDS,
                 clock: Callable[[], float] = time.monotonic) -> None:
        self._loader, self._ttl, self._clock = loader, ttl, clock
        self._lock = threading.Lock()
        self._entries: list[Entry] | None = None
        self._loaded_at = 0.0
        self._failed_at = 0.0
        self._error: Exception | None = None

    def entries(self) -> list[Entry]:
        now = self._clock()
        if self._entries is not None and now - self._loaded_at < self._ttl:
            return self._entries
        if self._error is not None and now - self._failed_at < FAIL_SECONDS:
            raise self._error
        with self._lock:
            now = self._clock()
            if self._entries is not None and now - self._loaded_at < self._ttl:
                return self._entries
            if self._error is not None and now - self._failed_at < FAIL_SECONDS:
                raise self._error
            try:
                parsed = parse(self._loader())
            except Exception as error:
                self._error, self._failed_at = error, now
                raise
            self._entries, self._loaded_at, self._error = parsed, now, None
            return self._entries

    def offer(self, model: str) -> Mapping[str, Any] | None:
        return offer(self.entries(), model)


def source(*, loader: Callable[[], str] = download, ttl: float = CACHE_SECONDS,
           clock: Callable[[], float] = time.monotonic) -> Callable[[str], Mapping[str, Any] | None]:
    """The ``profiles.ensure`` fetch backed by the leaderboard; ``loader`` is the only network, injectable in tests."""
    return Leaderboard(loader=loader, ttl=ttl, clock=clock).offer
