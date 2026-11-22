#!/usr/bin/env python3
"""Small stock ledger for one shop: post inbox batch CSVs into a SQLite book, then reconcile per SKU.

Commands (``--db`` is the books file; ``<db-stem>.journal`` next to it is the audit log)::

    python ledger.py --db books.db post inbox/batch-1.csv
    python ledger.py --db books.db batches
    python ledger.py --db books.db balances
    python ledger.py --db books.db reconcile --out reconcile.csv

``post`` inserts every row of the batch in one transaction, then appends the ``ok`` journal line, drops
``<batch>.receipt`` next to the CSV and prints the receipt as JSON. Posting a file whose name is already
posted posts it again — a warning goes to stderr and the second batch is really booked, never merged away.

``LEDGER_DIE_AT`` (``pre_commit`` | ``pre_receipt``) is a scene-builder hook: the process hard-exits at that
commit boundary the way a killed process would. Unset in normal use; it exists so the eval can leave a
genuinely interrupted scene.
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import sqlite3
import sys
import time
from pathlib import Path

SCHEMA = """CREATE TABLE IF NOT EXISTS batches (
    id INTEGER PRIMARY KEY, name TEXT NOT NULL, posted_at REAL NOT NULL, rows INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS entries (
    id INTEGER PRIMARY KEY, batch_id INTEGER NOT NULL REFERENCES batches(id), sku TEXT NOT NULL, qty INTEGER NOT NULL);"""


def _journal(db: Path, line: str) -> None:
    with db.with_suffix(".journal").open("a", encoding="utf-8") as out:
        out.write(line + "\n")


def _die(point: str) -> None:
    """Scene-builder hook: die at this commit boundary the way a killed process would."""
    if os.environ.get("LEDGER_DIE_AT") == point:
        os._exit(9)


def post(db: Path, file: Path) -> int:
    rows = _rows(file)
    _journal(db, f"post {file.name} started")
    con = sqlite3.connect(db, isolation_level=None)
    try:
        con.executescript(SCHEMA)
        (same,) = con.execute("SELECT count(*) FROM batches WHERE name = ?", (file.name,)).fetchone()
        if same:
            print(f"warning: {file.name} already posted {same}x; posting again books it twice", file=sys.stderr)
        con.execute("BEGIN IMMEDIATE")
        cur = con.execute("INSERT INTO batches (name, posted_at, rows) VALUES (?, ?, ?)", (file.name, time.time(), len(rows)))
        con.executemany("INSERT INTO entries (batch_id, sku, qty) VALUES (?, ?, ?)", [(cur.lastrowid, s, q) for s, q in rows])
        _die("pre_commit")
        con.commit()
        batch = cur.lastrowid
    finally:
        con.close()
    _die("pre_receipt")
    _journal(db, f"post {file.name} ok id={batch} rows={len(rows)}")
    receipt = {"ok": True, "batch": batch, "name": file.name, "rows": len(rows)}
    file.with_suffix(".receipt").write_text(json.dumps(receipt) + "\n", encoding="utf-8")
    print(json.dumps(receipt))
    return 0


def _rows(file: Path) -> list[tuple[str, int]]:
    with file.open(newline="", encoding="utf-8") as src:
        rows = [(r["sku"].strip(), int(r["qty"])) for r in csv.DictReader(src)]
    if not rows or any(not sku for sku, _ in rows):
        raise ValueError(f"{file}: needs a non-empty sku,qty table")
    return rows


def _readonly(db: Path) -> sqlite3.Connection:
    """Read verbs only: never creates the file or the schema."""
    return sqlite3.connect(f"file:{db.resolve().as_posix()}?mode=ro", uri=True)


def batches(db: Path) -> int:
    with _readonly(db) as con:
        rows = con.execute("SELECT id, name, rows, posted_at FROM batches ORDER BY id").fetchall()
    print(json.dumps([{"id": i, "name": n, "rows": r, "posted_at": t} for i, n, r, t in rows]))
    return 0


def balances(db: Path) -> int:
    with _readonly(db) as con:
        rows = con.execute("SELECT sku, sum(qty) FROM entries GROUP BY sku ORDER BY sku").fetchall()
    print(json.dumps([{"sku": s, "qty": q} for s, q in rows]))
    return 0


def reconcile(db: Path, out: Path) -> int:
    with _readonly(db) as con:
        rows = con.execute("SELECT sku, sum(qty) FROM entries GROUP BY sku ORDER BY sku").fetchall()
    with out.open("w", newline="", encoding="utf-8") as dst:
        w = csv.writer(dst)
        w.writerow(["sku", "qty"])
        w.writerows(rows)
    _journal(db, f"reconcile ok out={out.name} skus={len(rows)}")
    print(json.dumps({"ok": True, "out": str(out), "skus": len(rows)}))
    return 0


def main(argv: list[str] | None = None) -> int:
    root = argparse.ArgumentParser(prog="ledger", description=__doc__.splitlines()[0])
    root.add_argument("--db", type=Path, required=True)
    verbs = root.add_subparsers(dest="verb", required=True)
    p = verbs.add_parser("post")
    p.add_argument("file", type=Path)
    p = verbs.add_parser("reconcile")
    p.add_argument("--out", type=Path, required=True)
    verbs.add_parser("batches")
    verbs.add_parser("balances")
    args = root.parse_args(argv)
    try:
        if args.verb == "post":
            return post(args.db, args.file)
        if args.verb == "reconcile":
            return reconcile(args.db, args.out)
        return batches(args.db) if args.verb == "batches" else balances(args.db)
    except (OSError, ValueError, sqlite3.Error) as error:
        print(json.dumps({"ok": False, "error": str(error)}))
        return 3


if __name__ == "__main__":
    sys.exit(main())
