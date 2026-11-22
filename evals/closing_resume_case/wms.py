#!/usr/bin/env python3
"""Warehouse ledger: post batches, book transfers and count adjustments, then close and report."""

from __future__ import annotations

import argparse
import csv
import json
import os
import re
import sqlite3
import sys
import time
from contextlib import closing
from pathlib import Path

SCHEMA = """CREATE TABLE IF NOT EXISTS batches (
    id INTEGER PRIMARY KEY, name TEXT NOT NULL, posted_at REAL NOT NULL, rows INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS entries (
    id INTEGER PRIMARY KEY, source TEXT NOT NULL CHECK (source IN ('post', 'transfer', 'adjust')),
    source_id INTEGER NOT NULL, wh TEXT NOT NULL, sku TEXT NOT NULL, qty INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS transfers (
    id INTEGER PRIMARY KEY, line INTEGER NOT NULL, src TEXT NOT NULL, dst TEXT NOT NULL,
    sku TEXT NOT NULL, qty INTEGER NOT NULL, at REAL NOT NULL);
CREATE TABLE IF NOT EXISTS adjustments (
    id INTEGER PRIMARY KEY, wh TEXT NOT NULL, sku TEXT NOT NULL, qty INTEGER NOT NULL,
    reason TEXT NOT NULL, at REAL NOT NULL);
CREATE TABLE IF NOT EXISTS closings (
    id INTEGER PRIMARY KEY, period TEXT NOT NULL UNIQUE, closed_at REAL NOT NULL);
CREATE TABLE IF NOT EXISTS closing_lines (
    closing_id INTEGER NOT NULL REFERENCES closings(id), wh TEXT NOT NULL, sku TEXT NOT NULL,
    qty INTEGER NOT NULL, PRIMARY KEY (closing_id, wh, sku));"""

BALANCES_SQL = "SELECT wh, sku, sum(qty) AS qty FROM entries GROUP BY wh, sku ORDER BY wh, sku"
QUERIES = {
    "batches": "SELECT id, name, posted_at, rows FROM batches ORDER BY id",
    "transfers": "SELECT id, line, src, dst, sku, qty, at FROM transfers ORDER BY id",
    "adjustments": "SELECT id, wh, sku, qty, reason, at FROM adjustments ORDER BY id",
    "balances": BALANCES_SQL,
}
BOOKING = ("post", "transfer", "adjust")


def _json(row: dict) -> None:
    print(json.dumps(row, separators=(",", ":")))


def _journal(db: Path, line: str) -> None:
    with db.with_suffix(".journal").open("a", encoding="utf-8") as out:
        out.write(line + "\n")


def _die(point: str) -> None:
    """Scene-builder hook: hard-exit at one of the two commit boundaries."""
    if os.environ.get("WMS_DIE_AT") == point:
        os._exit(9)


def _csv_rows(file: Path, header: tuple[str, ...]) -> list[list[str]]:
    with file.open(newline="", encoding="utf-8") as src:
        rows = csv.reader(src, strict=True)
        if next(rows, None) != list(header):
            raise ValueError(f"{file}: expected header {','.join(header)}")
        data = list(rows)
    if not data or any(len(row) != len(header) or not all(row) for row in data):
        raise ValueError(f"{file}: expected non-empty rows with {len(header)} columns")
    return data


def _entries(con: sqlite3.Connection, source: str, source_id: int, rows: list[tuple[str, str, int]]) -> None:
    con.executemany("INSERT INTO entries (source, source_id, wh, sku, qty) VALUES (?, ?, ?, ?, ?)",
                    [(source, source_id, *row) for row in rows])


def _post(con: sqlite3.Connection, args: argparse.Namespace) -> dict:
    name = args.file.name
    rows = [(wh, sku, int(qty)) for wh, sku, qty in _csv_rows(args.file, ("wh", "sku", "qty"))]
    if con.execute("SELECT 1 FROM batches WHERE name = ?", (name,)).fetchone():
        print(f"warning: {name} is already posted; booking another batch", file=sys.stderr)
    cur = con.execute("INSERT INTO batches (name, posted_at, rows) VALUES (?, ?, ?)", (name, time.time(), len(rows)))
    _entries(con, "post", cur.lastrowid, rows)
    return {"id": cur.lastrowid, "name": name, "rows": len(rows)}


def _transfer(con: sqlite3.Connection, args: argparse.Namespace) -> dict:
    line = args.line
    rows = _csv_rows(Path("transfers.csv"), ("src", "dst", "sku", "qty"))
    if not 1 <= line <= len(rows):
        raise ValueError(f"transfers.csv: no data line {line}")
    src, dst, sku, raw_qty = rows[line - 1]
    qty = int(raw_qty)
    if con.execute("SELECT 1 FROM transfers WHERE line = ?", (line,)).fetchone():
        print(f"warning: transfer line {line} is already booked; booking it again", file=sys.stderr)
    cur = con.execute("INSERT INTO transfers (line, src, dst, sku, qty, at) VALUES (?, ?, ?, ?, ?, ?)",
                      (line, src, dst, sku, qty, time.time()))
    _entries(con, "transfer", cur.lastrowid, [(src, sku, -qty), (dst, sku, qty)])
    return {"id": cur.lastrowid, "line": line, "rows": 2}


def _adjust(con: sqlite3.Connection, args: argparse.Namespace) -> dict:
    wh, sku, qty, reason = args.wh, args.sku, args.qty, args.reason
    if not wh or not sku or not reason or any(c.isspace() for c in reason):
        raise ValueError("adjust requires a warehouse, SKU and single-word reason")
    cur = con.execute("INSERT INTO adjustments (wh, sku, qty, reason, at) VALUES (?, ?, ?, ?, ?)",
                      (wh, sku, qty, reason, time.time()))
    _entries(con, "adjust", cur.lastrowid, [(wh, sku, qty)])
    return {"id": cur.lastrowid, "wh": wh, "sku": sku, "qty": qty, "reason": reason}


def _close(con: sqlite3.Connection, args: argparse.Namespace) -> dict:
    period = args.period
    if not re.fullmatch(r"[0-9]{4}-(0[1-9]|1[0-2])", period):
        raise ValueError("period must be YYYY-MM")
    if con.execute("SELECT 1 FROM closings WHERE period = ?", (period,)).fetchone():
        raise ValueError(f"period {period} is already closed")
    cur = con.execute("INSERT INTO closings (period, closed_at) VALUES (?, ?)", (period, time.time()))
    rows = con.execute(BALANCES_SQL).fetchall()
    con.executemany("INSERT INTO closing_lines (closing_id, wh, sku, qty) VALUES (?, ?, ?, ?)",
                    [(cur.lastrowid, *row) for row in rows])
    return {"id": cur.lastrowid, "period": period, "rows": len(rows)}


def _report(con: sqlite3.Connection, args: argparse.Namespace) -> dict:
    if not con.execute("SELECT 1 FROM closings LIMIT 1").fetchone():
        raise ValueError("books are not closed")
    rows = con.execute(BALANCES_SQL).fetchall()
    with args.out.open("w", newline="", encoding="utf-8") as dst:
        w = csv.writer(dst)
        w.writerow(("wh", "sku", "qty"))
        w.writerows(rows)
    return {"out": str(args.out), "rows": len(rows)}


def _write(args: argparse.Namespace) -> None:
    db, verb = args.db, args.verb
    if verb == "post":
        ident, write = args.file.name, _post
    elif verb == "transfer":
        ident, write = args.line, _transfer
    elif verb == "adjust":
        ident, write = f"{args.wh}/{args.sku}/{args.qty}/{args.reason}", _adjust
    elif verb == "close":
        ident, write = args.period, _close
    else:
        ident, write = args.out.name, _report
    _journal(db, f"{verb} {ident} started")
    mode = "rw" if verb == "report" else "rwc"
    with closing(sqlite3.connect(f"{db.resolve().as_uri()}?mode={mode}", uri=True, isolation_level=None)) as con:
        if verb != "report":
            con.executescript(SCHEMA)
        con.execute("BEGIN IMMEDIATE")
        if verb in BOOKING and con.execute("SELECT 1 FROM closings LIMIT 1").fetchone():
            raise ValueError("books are already closed")
        receipt = write(con, args)
        if verb in BOOKING:
            _die("pre_commit")
        con.commit()
    if verb in BOOKING:
        _die("pre_receipt")
    result = " ".join(f"{key}={value}" for key, value in receipt.items())
    _journal(db, f"{verb} {ident} ok {result}")
    _json({"ok": True, **receipt})


def _query(db: Path, verb: str) -> None:
    with closing(sqlite3.connect(f"{db.resolve().as_uri()}?mode=ro", uri=True)) as con:
        con.row_factory = sqlite3.Row
        if verb == "status":
            con.execute("BEGIN")
            latest = con.execute("SELECT period FROM closings ORDER BY id DESC LIMIT 1").fetchone()
            counts = {key: con.execute(f"SELECT count(*) FROM {table}").fetchone()[0]
                      for key, table in (("posts", "batches"), ("transfers", "transfers"), ("adjustments", "adjustments"))}
            _json({"closed": latest is not None, "period": latest[0] if latest else None, **counts})
        else:
            for row in con.execute(QUERIES[verb]):
                _json(dict(row))


def main(argv: list[str] | None = None) -> int:
    root = argparse.ArgumentParser(prog="wms", description=__doc__)
    root.add_argument("--db", type=Path, required=True)
    verbs = root.add_subparsers(dest="verb", required=True)
    verbs.add_parser("post").add_argument("file", type=Path)
    verbs.add_parser("transfer").add_argument("--line", type=int, required=True)
    adjust = verbs.add_parser("adjust")
    adjust.add_argument("--wh", required=True)
    adjust.add_argument("--sku", required=True)
    adjust.add_argument("--qty", type=int, required=True)
    adjust.add_argument("--reason", required=True)
    verbs.add_parser("close").add_argument("--period", required=True)
    verbs.add_parser("report").add_argument("--out", type=Path, required=True)
    for verb in (*QUERIES, "status"):
        verbs.add_parser(verb)
    args = root.parse_args(argv)
    try:
        if args.verb in (*QUERIES, "status"):
            _query(args.db, args.verb)
        else:
            _write(args)
    except (OSError, ValueError, OverflowError, csv.Error, sqlite3.Error) as error:
        print(f"error: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
