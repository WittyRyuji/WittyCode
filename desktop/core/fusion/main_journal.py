"""Durable side-effect receipts for one frozen Main plan. A logical page is 64 consecutive sequence numbers.

Only the binding lives in the conversation snapshot. The call-id and pending-intent indexes are maintained
transactionally with the rows; detecting a replay never needs to load even the most recent history page.
"""
from __future__ import annotations

from contextlib import closing, contextmanager
import re
import sqlite3
import uuid

from desktop.core.fusion.registry import FusionError

PAGE_SIZE = 64
READ_JOURNAL = {
    'name': 'read_recovery_journal',
    'description': 'Read up to two 64-operation pages from this active Main plan, newest pages first. Use next_page to read older pages.',
    'parameters': {'type': 'object', 'properties': {'page': {'type': 'integer', 'minimum': 1}}, 'additionalProperties': False},
}


class MainJournal:
    def __init__(self, outputs, plan, token):
        if not isinstance(token, str) or not re.fullmatch('[a-f0-9]{32}', token):
            raise FusionError('Invalid recovery journal binding.')
        self.outputs, self.plan, self.token = outputs, plan, token
        self.path = outputs.root / f'fusion-journal-{token}.sqlite3'

    @contextmanager
    def _db(self, *, initialize=False):
        self.outputs._safe_root()
        if self.path.is_symlink() or self.path.resolve().parent != self.outputs.root.resolve():
            raise FusionError('Invalid recovery journal location.')
        try:
            # Never create an empty replacement for missing evidence.
            with closing(sqlite3.connect(self.path.as_uri() + '?mode=rw', uri=True)) as db, db:
                if not initialize and db.execute('SELECT plan, token FROM binding').fetchone() != (self.plan, self.token):
                    raise FusionError('The recovery journal does not belong to this plan.')
                yield db
        except sqlite3.Error as error:
            raise FusionError('Recovery journal unavailable; the side effect cannot be confirmed.') from error

    @classmethod
    def create(cls, outputs, plan, legacy):
        ledger = cls(outputs, plan, uuid.uuid4().hex)
        outputs._safe_root()
        outputs.root.mkdir(parents=True, exist_ok=True)
        ledger.path.touch(exist_ok=False)
        with ledger._db(initialize=True) as db:
            db.execute('CREATE TABLE binding(plan TEXT NOT NULL, token TEXT NOT NULL)')
            db.execute('INSERT INTO binding VALUES (?,?)', (plan, ledger.token))
            db.execute('''CREATE TABLE operations(seq INTEGER PRIMARY KEY, call TEXT NOT NULL UNIQUE, intent TEXT NOT NULL,
                       tool TEXT NOT NULL, status TEXT NOT NULL CHECK(status IN ('started','done','error','cancelled')),
                       result TEXT NOT NULL DEFAULT '', output_pending INTEGER NOT NULL DEFAULT 0)''')
            db.execute("CREATE UNIQUE INDEX pending_intent ON operations(intent) WHERE status='started'")
            db.executemany('INSERT INTO operations(call,intent,tool,status,result,output_pending) VALUES (?,?,?,?,?,?)',
                           ((cid, r['intent'], r['tool'], r['status'], r.get('result', ''), bool(r.get('outputPending')))
                            for cid, r in legacy.items()))
        return ledger

    def start(self, call_id, intent, tool):
        with self._db() as db:
            try:
                db.execute("INSERT INTO operations(call,intent,tool,status) VALUES (?,?,?,'started')", (call_id, intent, tool))
            except sqlite3.IntegrityError as error:
                raise FusionError('Duplicate Main call or unconfirmed side effect. Inspect rather than replay it.') from error

    def finish(self, call_id, status, reference, pending):
        with self._db() as db:
            updated = db.execute("UPDATE operations SET status=?,result=?,output_pending=? WHERE call=? AND status='started'",
                                 (status, reference, pending, call_id))
            if updated.rowcount != 1:
                raise FusionError('No matching open Main operation; the existing receipt was not changed.')

    def read(self, page=None):
        if page is not None and (type(page) is not int or not 1 <= page <= 2**53):
            raise FusionError('Provide a positive journal page.')
        with self._db() as db:
            last = (db.execute('SELECT coalesce(max(seq),0) FROM operations').fetchone()[0] + PAGE_SIZE - 1) // PAGE_SIZE
            if not last:
                if page not in (None, 1):
                    raise FusionError('Journal page does not exist.')
                return {'rows': [], 'next_page': None}
            if page is not None and page > last:
                raise FusionError('Journal page does not exist.')
            page = last if page is None else page
            first = max(1, page - 1)
            db.row_factory = sqlite3.Row
            rows = db.execute('SELECT seq,call,intent,tool,status,result,output_pending FROM operations WHERE seq BETWEEN ? AND ? ORDER BY seq',
                              ((first - 1) * PAGE_SIZE + 1, page * PAGE_SIZE))
            return {'rows': [{**dict(row), 'output_pending': bool(row['output_pending'])} for row in rows], 'next_page': first - 1 or None}
