"""A page's live transcript window; the engine/archive still owns the complete history."""
from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from typing import Any, Generic, TypeVar

from desktop.core.turn_delta import changed_blocks

PAGE_SIZE = 5
T = TypeVar("T")
View = dict[str, Any]


@dataclass
class TurnWindow(Generic[T]):
    identify: Callable[[T], str]
    version: Callable[[T], int]
    generation: int = 0
    start: int = 0
    earlier_on_disk: int = 0
    source: Sequence[T] | None = None
    expanded: bool = False
    ids: list[str] = field(default_factory=list)
    sent: dict[str, tuple[int, View]] = field(default_factory=dict)

    def reset(self) -> None:
        self.generation += 1
        self.source = None
        self.expanded = False
        self.ids.clear()
        self.sent.clear()

    def _history(self) -> View:
        return {"generation": self.generation, "more": self.start + self.earlier_on_disk > 0}

    def _matches(self, source: Sequence[T]) -> bool:
        return self.source is source and len(source) >= self.start + len(self.ids) and all(
            self.identify(source[self.start + i]) == ident for i, ident in enumerate(self.ids))

    def _record(self, items: Sequence[T], render: Callable[[T], View], *, full: bool) -> list[View]:
        out = []
        for item in items:
            ident, version = self.identify(item), self.version(item)
            old = self.sent.get(ident)
            if full or old is None or old[0] != version:
                shown = render(item)
                out.append(changed_blocks(shown, old[1] if old is not None and not full else None))
                self.sent[ident] = version, shown
        return out

    def sync(self, source: Sequence[T], render: Callable[[T], View], *, earlier_on_disk: int = 0,
             preserve_expansion: bool = False) -> View | None:
        self.earlier_on_disk = earlier_on_disk
        replace = not self._matches(source)
        trim = 0
        if replace:
            keep = max(PAGE_SIZE, len(self.ids)) if preserve_expansion and self.expanded else PAGE_SIZE
            expanded = keep > PAGE_SIZE
            self.reset()
            self.expanded = expanded
            self.source = source
            self.start = max(0, len(source) - keep)
        elif not self.expanded:
            start = max(self.start, len(source) - PAGE_SIZE)
            trim = min(start - self.start, len(self.ids))
            for ident in self.ids[:trim]:
                self.sent.pop(ident, None)
            del self.ids[:trim]
            self.start = start
        self.ids.extend(self.identify(source[i]) for i in range(self.start + len(self.ids), len(source)))
        turns = self._record(source[self.start:], render, full=replace)
        if replace or turns or trim:
            return {"turns": turns, "all": replace, "history": self._history(), **({"trim": trim} if trim else {})}
        return None

    def accepts(self, source: Sequence[T], generation: Any, before: Any) -> bool:
        # The cursor describes what the page has. A late request cannot mix archives or duplicate a page.
        return type(generation) is int and generation == self.generation and bool(self.ids) and before == self.ids[0] and self._matches(source)

    def older(self, source: Sequence[T], render: Callable[[T], View], *, generation: Any, before: Any) -> View | None:
        if self.start == 0 or not self.accepts(source, generation, before):
            return None
        start = max(0, self.start - PAGE_SIZE)
        items = source[start:self.start]
        turns = self._record(items, render, full=True)
        self.ids[0:0] = [self.identify(item) for item in items]
        self.start = start
        self.expanded = True
        return {"turns": turns, "all": False, "prepend": True, "history": self._history()}

    def prepend_disk(self, source: list[T], page: list[T], render: Callable[[T], View]) -> View | None:
        """A validated disk page immediately before this cache snapshot's first in-memory turn."""
        if self.start or len(page) != min(PAGE_SIZE, self.earlier_on_disk) or not page:
            return None
        turns = self._record(page, render, full=True)
        source[:0] = page
        self.ids[:0] = [self.identify(turn) for turn in page]
        self.earlier_on_disk -= len(page)
        self.expanded = True
        return {"turns": turns, "all": False, "prepend": True, "history": self._history()}


def view_window() -> TurnWindow[Any]:
    return TurnWindow(lambda turn: turn["id"] if isinstance(turn, dict) else turn.id,
                      lambda turn: 0 if isinstance(turn, dict) else turn.version)
