"""Bridge a controller's append-only model log to one runtime-authorised logical archive.

Indices in a controller log are local to its native session. Do not put archive ids into the
provider's request, or deduce time order from the names of the instance directories.
"""
from __future__ import annotations

from bisect import bisect_right
from dataclasses import dataclass
from typing import Any, Callable, Mapping, Sequence

from desktop.core.session_archive import ArchiveError, Instance, PreparedDump, SessionArchive


@dataclass(frozen=True)
class PendingSnapshot:
    staged: PreparedDump
    start: int
    candidates: tuple[str, ...]


class ArchiveBinding:
    def __init__(self, archive: SessionArchive, native_id: str) -> None:
        self.archive = archive
        self.native_id = native_id
        self.instance = archive.current()
        self._ids: list[str] | None = None

    @classmethod
    def create(cls, manager: Any, native_id: str) -> ArchiveBinding:
        meter = getattr(manager, 'archive_meter', None)
        return cls(SessionArchive.create(manager.root.parent / 'session', meter=meter() if meter else None), native_id)

    @classmethod
    def restore(cls, manager: Any, sid: str, native_id: str) -> ArchiveBinding:
        meter = getattr(manager, 'archive_meter', None)
        return cls(SessionArchive.open(manager.root.parent / 'session', sid, meter=meter() if meter else None), native_id)

    def ensure_instance(self, backend: str, native_id: str, connection: str) -> Instance:
        item = self.instance
        if not item or (item.backend, item.connection, item.native_id) != (backend, connection, native_id):
            self.instance = self.archive.enter(backend, connection, native_id)
        return self.instance

    def _log_ids(self) -> list[str]:
        if self._ids is None:
            found: dict[int, str] = {}
            for identity in self.archive.manifest['identities']:
                inst = Instance(self.archive.sid, identity['backend'], identity['iid'], identity['connection'], identity['native'])
                for event in self.archive.events(inst):
                    if event.get('source') == self.native_id:
                        index = event['index']
                        if not isinstance(index, int) or index < 0 or index in found:
                            raise ArchiveError('Ambiguous controller event position in the session archive.')
                        found[index] = event['id']
            if set(found) != set(range(len(found))):
                raise ArchiveError('The session archive has a gap in its event log.')
            self._ids = [found[index] for index in range(len(found))]
        return self._ids

    def sync(self, items: Sequence[dict], turns: Sequence[Any]) -> None:
        if self.instance is None:
            raise ArchiveError('Cannot archive a controller without an authorised instance.')
        ids = self._log_ids()
        if len(ids) > len(items):
            raise ArchiveError('The active log ends before its archived event boundary.')
        if len(ids) == len(items):
            return
        starts = [turn.start for turn in turns]
        for index in range(len(ids), len(items)):
            event = self.archive.append_event(self.instance, {'source': self.native_id, 'index': index,
                'turn': bisect_right(starts, index), 'item': items[index]})
            ids.append(event['id'])

    def prepare(self, history: Sequence[dict], start: int, synthetic: int) -> PendingSnapshot:
        ids = self._log_ids()
        if self.instance is None or start < 0 or not 0 <= synthetic <= len(history) or len(ids) - start != len(history) - synthetic:
            raise ArchiveError('The compaction view does not match the persisted source log.')
        candidates = tuple(ids[start:])
        records = [({'item': item} if i < synthetic else {'id': candidates[i - synthetic], 'item': item})
                   for i, item in enumerate(history)]
        prepared = self.archive.prepare_dump(self.instance, records, evicted=candidates)
        return PendingSnapshot(prepared, start, candidates)

    def publish(self, pending: PendingSnapshot, entry: Mapping[str, Any],
                *, checkpoint: Callable[[Mapping[str, Any]], None] | None = None) -> None:
        cut = int(entry['upto']) - pending.start
        if not 0 <= cut <= len(pending.candidates):
            raise ArchiveError('The summary ends outside the staged snapshot.')
        self.archive.publish(pending.staged, entry, evicted=pending.candidates[:cut], checkpoint=checkpoint)

    def abort(self, pending: PendingSnapshot) -> None:
        self.archive.abort(pending.staged)
