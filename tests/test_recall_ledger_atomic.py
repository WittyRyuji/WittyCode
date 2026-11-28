"""The turn ledger on disk: one book shared across processes, strictly validated.

Synthetic only — subprocesses charge a tmp_path file and corruption is written bytes; nothing touches
real sessions, archives or models.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

from desktop.core.harness.recall_budget import FILENAME, LIMIT, RecallLedger

ROOT = Path(__file__).resolve().parents[1]

# A single 5,000-byte charge in a fresh process: under the shared lock only one of two can ever fit.
_WORKER = (
    "import json, sys\n"
    "sys.path.insert(0, sys.argv[3])\n"
    "from desktop.core.harness.recall_budget import RecallLedger\n"
    "print(json.dumps(RecallLedger(sys.argv[1], 't7').spend(sys.argv[2], 'x' * 5000)))\n"
)


def test_two_processes_charging_one_turn_never_exceed_the_limit(tmp_path):
    path = tmp_path / FILENAME
    workers = [
        subprocess.Popen([sys.executable, "-c", _WORKER, str(path), tag, str(ROOT)],
                         stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        for tag in ("p1", "p2")
    ]
    charged = []
    for proc in workers:
        out, err = proc.communicate(timeout=120)
        assert proc.returncode == 0, err
        charged.append(json.loads(out))
    # 5,000 + 5,000 cannot both fit the turn's 8,192: exactly one process wins; the other reads the
    # committed charge under the lock and is refused — the file shows one turn and one charge.
    assert sorted(c is None for c in charged) == [False, True]
    book = json.loads(path.read_text("utf-8"))
    assert book["turn"] == "t7" and book["total"] == 5000 <= LIMIT
    winner = next(tag for tag, c in zip(("p1", "p2"), charged) if c)
    assert book["spent"] == {winner: 5000}


def test_a_corrupt_page_fails_closed_and_is_never_rewritten(tmp_path):
    path = tmp_path / FILENAME
    for raw in (
        json.dumps({"turn": "t7", "total": 10, "spent": {"e1": 4}}),   # total ≠ the spent map's sum
        json.dumps({"turn": "t7", "total": 0, "spent": "abc"}),       # spent is not a map at all
        "{not json",
    ):
        path.write_text(raw, encoding="utf-8")
        ledger = RecallLedger(path, "t7")
        assert ledger.corrupt and ledger.remaining() == 0 and ledger.spend("e2", "x") is None
        assert path.read_text("utf-8") == raw    # the file is evidence: nothing rewrites it at zero
