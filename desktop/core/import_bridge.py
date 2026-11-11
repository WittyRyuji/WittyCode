"""What the memory page's import wizard asks of the other coding agents (``core/importers``).

The page only ever sees a preview: the notes an app's memory files would become (split by heading, de-clashed against the
memory and against each other) and its conversations. Nothing is written until the user ticks and confirms; then the
files are read again, so the page sends keys and never the text itself, and what lands in the memory is what the files
say now. Conversations land in the ChatGPT Plan engine's store (``harness.store``) and appear in the sidebar as its own.

Everything here is synchronous and called from a worker thread by ``AppCore``; a source is not for two threads at once,
so every call makes its own (``make_sources``).
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Iterable, Mapping, Sequence
from pathlib import Path
from typing import Any

from desktop.core.harness.store import SessionStore
from desktop.core.importers import (
    APPS,
    ImportFailed,
    MemoryCandidate,
    SessionInfo,
    Source,
    saved_id,
    sources,
    text_digest,
    tidy,
    to_saved_session,
)
from desktop.core.memory.bridge import explain
from desktop.core.memory.store import MemoryRejected, MemoryStore, agent

log = logging.getLogger(__name__)

ALL_NOTES = 100_000
"""The import wizard compares the candidates with every note there is; the memory holds a few hundred."""


def _keys(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    return [str(item) for item in value if isinstance(item, str) and item]


def _ms(seconds: float) -> int:
    return int(seconds * 1000)


def _apps(value: Any) -> tuple[str, ...]:
    """The apps the page ticked (all of them when it sent none it knows)."""
    if not isinstance(value, list):
        return APPS
    chosen = tuple(name for name in value if name in APPS)
    return chosen or APPS


class ImportBridge:
    def __init__(
        self,
        store: MemoryStore,
        sessions: SessionStore | None,
        *,
        make_sources: Callable[[], Sequence[Source]] = sources,
        row: Callable[[str], str] = lambda native: native,
        defaults: Callable[[], Mapping[str, str]] = lambda: {"model": "", "reasoning": "", "autonomy": ""},
    ) -> None:
        self.store = store
        self.sessions = sessions
        """The ChatGPT Plan engine's saved conversations; ``None``: this build has no such engine to keep them in."""
        self.make_sources = make_sources
        self.row = row
        self.defaults = defaults

    # ------------------------------------------------------------------ the preview

    def _chosen(self, apps: Iterable[str]) -> list[Source]:
        wanted = set(apps)
        return [source for source in self.make_sources() if source.app in wanted]

    def _read(self, what: Callable[[Source], Any], source: Source) -> Any:
        """What one source says, or nothing when its files cannot be read: one app's trouble does not stop the rest."""
        try:
            return what(source)
        except Exception:
            log.warning("could not read what %s keeps", source.app, exc_info=True)
            return []

    @staticmethod
    def _available(source: Source) -> bool:
        try:
            return bool(source.available())
        except Exception:
            log.warning("could not look for %s", source.app, exc_info=True)
            return False

    def _existing(self) -> list[Any]:
        return self.store.entries(status="active", limit=ALL_NOTES)

    def scan(self, params: Mapping[str, Any]) -> dict[str, Any]:
        out = []
        for source in self.make_sources():
            entry: dict[str, Any] = {"app": source.app, "label": source.label, "available": False, "memories": 0, "sessions": 0}
            entry["available"] = available = self._available(source)
            if available:
                entry["memories"] = len(self._read(lambda s: s.memories(), source))
                entry["sessions"] = len(self._read(lambda s: s.sessions(), source))
            problem = next(iter(source.report.problems), "")
            if problem:
                entry["error"] = problem
            out.append(entry)
        return {"sources": out}

    def _candidates(self, apps: Iterable[str]) -> list[MemoryCandidate]:
        found: list[MemoryCandidate] = []
        for source in self._chosen(apps):
            if self._available(source):
                found.extend(self._read(lambda s: s.memories(), source))
        return found

    def _planned(self, candidates: Sequence[MemoryCandidate], existing: Sequence[Any]) -> tuple[dict[str, MemoryCandidate], set[str]]:
        """The candidates as the memory will take them: the names ``tidy`` gives (one text once, no title the memory would
        refuse), and the digests of those it already holds."""
        have = {text_digest(entry.text) for entry in existing}
        fresh = [candidate for candidate in candidates if candidate.digest not in have]
        planned = {candidate.key: candidate for candidate in tidy(fresh, [(entry.scope, entry.project, entry.title) for entry in existing])}
        return planned, have

    def memories(self, params: Mapping[str, Any]) -> dict[str, Any]:
        candidates = self._candidates(_apps(params.get("apps")))
        planned, have = self._planned(candidates, self._existing())
        items: list[dict[str, Any]] = []
        seen: set[str] = set()
        for candidate in candidates:
            if candidate.digest in have:
                if candidate.digest not in seen:  # (the memory holds this text once, however many files say it)
                    seen.add(candidate.digest)
                    items.append(self._memory_json(candidate, True))
                continue
            kept = planned.get(candidate.key)
            if kept is None or candidate.digest in seen:
                continue  # (the same text came from somewhere else too: it is imported once, under its first name)
            seen.add(candidate.digest)
            items.append(self._memory_json(kept, False))
        return {"items": items}

    def sessions_list(self, params: Mapping[str, Any]) -> dict[str, Any]:
        items: list[dict[str, Any]] = []
        for source in self._chosen(_apps(params.get("apps"))):
            if not self._available(source):
                continue
            for info in self._read(lambda s: s.sessions(), source):
                items.append(self._session_json(info))
        return {"items": items}

    # ------------------------------------------------------------------ the shapes the page reads

    def _memory_json(self, candidate: MemoryCandidate, exists: bool) -> dict[str, Any]:
        return {
            "key": candidate.key,
            "app": candidate.app,
            "title": candidate.title,
            "text": candidate.text,
            "scope": candidate.scope,
            "project": candidate.project,
            "projectName": Path(candidate.project).name if candidate.project else "",
            "kind": candidate.kind,
            "tags": list(candidate.tags),
            "origin": candidate.origin,
            "exists": exists,
        }

    def _session_json(self, info: SessionInfo) -> dict[str, Any]:
        return {
            "key": info.key,
            "app": info.app,
            "title": info.title,
            "project": info.project,
            "projectName": Path(info.project).name if info.project else "",
            "started": _ms(info.started),
            "updated": _ms(info.updated),
            "messages": info.messages,
            "imported": self.sessions is not None and self.sessions.load(saved_id(info.key)) is not None,
        }

    # ------------------------------------------------------------------ the import itself

    def commit(self, params: Mapping[str, Any]) -> dict[str, Any]:
        notes: list[dict[str, Any]] = []
        sessions: list[dict[str, Any]] = []
        skipped: list[dict[str, str]] = []
        projects: list[str] = []
        wanted = _keys(params.get("memories"))
        if wanted:
            self._import_memories(wanted, notes, skipped)
        wanted = _keys(params.get("sessions"))
        if wanted:
            self._import_sessions(wanted, sessions, skipped, projects)
        return {"notes": notes, "sessions": sessions, "skipped": skipped, "projects": projects}

    def _import_memories(self, wanted: Sequence[str], notes: list[dict[str, Any]], skipped: list[dict[str, str]]) -> None:
        existing = self._existing()
        found: dict[str, MemoryCandidate] = {}
        for source in self.make_sources():
            if not self._available(source):
                continue
            for candidate in self._read(lambda s: s.memories(), source):
                found.setdefault(candidate.key, candidate)
        planned, have = self._planned([found[key] for key in wanted if key in found], existing)
        for key in wanted:
            candidate = found.get(key)
            if candidate is None:
                skipped.append({"key": key, "title": "", "reason": "来源文件变了，找不到这条了。"})
                continue
            if candidate.digest in have:
                skipped.append({"key": key, "title": candidate.title, "reason": "记忆里已经有同样的内容了。"})
                continue
            kept = planned.get(key)
            if kept is None:
                skipped.append({"key": key, "title": candidate.title, "reason": "和这次要导入的另一条内容相同。"})
                continue
            try:
                entry = self.store.create(
                    title=kept.title, text=kept.text, scope=kept.scope, project=kept.project, kind=kept.kind, tags=kept.tags,
                    by=agent(kept.app),
                )
            except MemoryRejected as rejected:
                skipped.append({"key": key, "title": candidate.title, "reason": explain(rejected)})
                continue
            except OSError:
                log.warning("could not write an imported note", exc_info=True)
                skipped.append({"key": key, "title": candidate.title, "reason": "记忆正被别的程序写入，请稍后再试。"})
                continue
            have.add(kept.digest)
            notes.append({"key": key, "id": entry.id, "title": entry.title})

    def _import_sessions(
        self, wanted: Sequence[str], saved: list[dict[str, Any]], skipped: list[dict[str, str]], projects: list[str]
    ) -> None:
        if self.sessions is None:
            for key in wanted:
                skipped.append({"key": key, "title": "", "reason": "这个版本里没有 ChatGPT Plan 引擎，会话没地方放。"})
            return
        found: dict[str, tuple[Source, SessionInfo]] = {}
        for source in self.make_sources():
            if not self._available(source):
                continue
            for info in self._read(lambda s: s.sessions(), source):
                found.setdefault(info.key, (source, info))
        settings = {key: str(self.defaults().get(key) or "") for key in ("model", "reasoning", "autonomy")}
        for key in wanted:
            pair = found.get(key)
            if pair is None:
                skipped.append({"key": key, "title": "", "reason": "来源文件变了，找不到这个会话了。"})
                continue
            source, info = pair
            if not info.project:
                skipped.append({"key": key, "title": info.title, "reason": "不知道它在哪个项目里，导入后侧栏没有地方放它。"})
                continue
            try:
                messages = source.read(info)
            except ImportFailed as failed:
                skipped.append({"key": key, "title": info.title, "reason": failed.message})
                continue
            conversation = to_saved_session(info, messages, **settings)
            try:
                self.sessions.save(conversation)
            except (OSError, ValueError):
                log.warning("could not save an imported conversation", exc_info=True)
                skipped.append({"key": key, "title": info.title, "reason": "没能写进 ChatGPT Plan 的存档，请稍后再试。"})
                continue
            saved.append({"key": key, "id": conversation.id, "title": conversation.title, "row": self.row(conversation.id)})
            if conversation.project not in projects:
                projects.append(conversation.project)

    def undo(self, params: Mapping[str, Any]) -> dict[str, Any]:
        """Forget the notes an import made (they stay in the journal, so 显示已忘记 can bring them back)."""
        forgotten = 0
        for note_id in _keys(params.get("ids")):
            try:
                self.store.forget(note_id, by="user")
            except MemoryRejected:
                continue
            except OSError:
                log.warning("could not forget an imported note", exc_info=True)
                continue
            forgotten += 1
        return {"forgotten": forgotten}
