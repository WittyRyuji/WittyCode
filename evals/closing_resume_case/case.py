"""Fixed month-end inputs, an external checker, and scripted recovery mechanism checks."""

from __future__ import annotations

import csv
import io
import json
import os
import shlex
import shutil
import sqlite3
import subprocess
import sys
from collections import Counter, defaultdict
from contextlib import closing
from pathlib import Path

CASE_ID = "closing-resume-v1"
DB = "books.db"
TOOL = Path(__file__).with_name("wms.py")
PERIOD = "2026-10"
SKIP_TRANSFER_LINE = 5
RESUME_INPUT = "继续"

BATCHES = {
    "batch-01.csv": [("W1", "SKU-A", 32), ("W1", "SKU-B", 18), ("W2", "SKU-A", 15)],
    "batch-02.csv": [("W2", "SKU-B", 24), ("W2", "SKU-C", 16)],
    "batch-03.csv": [("W1", "SKU-C", 27), ("W1", "SKU-D", 19), ("W2", "SKU-D", 21)],
    "batch-04.csv": [("W1", "SKU-E", 36), ("W2", "SKU-E", 12)],
    "batch-05.csv": [("W1", "SKU-F", 25), ("W2", "SKU-F", 34), ("W1", "SKU-G", 20)],
    "batch-06.csv": [("W2", "SKU-G", 23), ("W1", "SKU-H", 11), ("W2", "SKU-H", 7)],
    "batch-07.csv": [("W1", "SKU-A", -6), ("W2", "SKU-C", -3)],
    "batch-08.csv": [("W2", "SKU-D", 14), ("W1", "SKU-E", -4)],
    "batch-09.csv": [("W2", "SKU-E", 8), ("W1", "SKU-B", -5), ("W2", "SKU-F", -7)],
    "batch-10.csv": [("W1", "SKU-F", 13), ("W1", "SKU-G", -2)],
    "batch-11.csv": [("W1", "SKU-C", 9), ("W2", "SKU-B", -4)],
    "batch-12.csv": [("W2", "SKU-A", 17), ("W1", "SKU-D", -7), ("W2", "SKU-G", 5)],
    "batch-13.csv": [("W1", "SKU-H", 8), ("W1", "SKU-B", 6)],
    "batch-14.csv": [("W2", "SKU-E", -3), ("W1", "SKU-G", 7), ("W2", "SKU-C", 5)],
    "batch-15.csv": [("W1", "SKU-D", 10), ("W2", "SKU-F", 9)],
    "batch-16.csv": [("W2", "SKU-H", -4), ("W1", "SKU-A", 7), ("W2", "SKU-D", -5)],
}
TRANSFERS = [
    ("W1", "W2", "SKU-A", 8),
    ("W2", "W1", "SKU-B", 6),
    ("W1", "W2", "SKU-C", 11),
    ("W2", "W1", "SKU-D", 9),
    ("W1", "W2", "SKU-E", 12),
    ("W2", "W1", "SKU-F", 9),
    ("W1", "W2", "SKU-G", 6),
    ("W2", "W1", "SKU-H", 3),
    ("W1", "W2", "SKU-B", 4),
    ("W2", "W1", "SKU-C", 5),
]
ADJUSTMENTS = [
    ("W1", "SKU-A", -2, "damaged"),
    ("W2", "SKU-B", 3, "count-diff"),
    ("W1", "SKU-C", -4, "count-diff"),
    ("W2", "SKU-D", 2, "recount"),
    ("W1", "SKU-E", -3, "damaged"),
    ("W2", "SKU-F", -5, "damaged"),
    ("W1", "SKU-G", 4, "count-diff"),
    ("W1", "SKU-H", -1, "recount"),
]

COMMAND_PREFIX = "python tools/wms.py --db books.db "
CLOSE_COMMAND = COMMAND_PREFIX + f"close --period {PERIOD}"
REPORT_COMMAND = COMMAND_PREFIX + "report --out closing.csv"
QUERY_COMMANDS = tuple(COMMAND_PREFIX + verb for verb in ("batches", "transfers", "adjustments", "balances", "status"))


def post_command(name: str) -> str:
    return COMMAND_PREFIX + f"post inbox/{name}"


def transfer_command(line: int) -> str:
    return COMMAND_PREFIX + f"transfer --line {line}"


def adjust_command(index: int) -> str:
    if not 1 <= index <= len(ADJUSTMENTS):
        raise ValueError(f"no adjustment at index {index}")
    wh, sku, qty, reason = ADJUSTMENTS[index - 1]
    return COMMAND_PREFIX + f"adjust --wh {wh} --sku {sku} --qty {qty} --reason {reason}"


REFERENCE = [
    *(post_command(name) for name in BATCHES),
    *(transfer_command(i) for i in range(1, len(TRANSFERS) + 1) if i != SKIP_TRANSFER_LINE),
    *(adjust_command(i) for i in range(1, len(ADJUSTMENTS) + 1)),
    CLOSE_COMMAND, REPORT_COMMAND,
]

TASK_MD = """# 2026 年 10 月仓库月结任务单

请在工作区根目录按以下顺序处理：

1. 过账 `inbox/` 下的全部批次（表头 `wh,sku,qty`）。
2. 执行 `transfers.csv` 的各行调拨（表头 `src,dst,sku,qty`）。
3. 录入 `adjustments.csv` 的各行盘点调整（表头 `wh,sku,qty,reason`）。
4. 执行 `close --period 2026-10` 关账。
5. 执行 `report --out closing.csv` 生成月结报表。

每批、每行都必须恰好处理一次；关账后不能再记账。报表表头为 `wh,sku,qty`，按仓库和 SKU 排序，包含零结余。

工具用法：`python tools/wms.py --db books.db <命令>`。

- `post inbox/<file>`：将指定批次记入账簿。
- `transfer --line N`：执行调拨表的第 N 条数据行（从 1 开始，不计表头）。
- `adjust --wh W --sku S --qty Q --reason R`：按仓库、SKU、整数数量和单词原因录入一条盘点调整。
- `close --period YYYY-MM`：保存指定期间的结余快照并关账。
- `report --out closing.csv`：导出账面结余。
- `batches`：查询入账批次。
- `transfers`：查询已记账的调拨。
- `adjustments`：查询已记账的盘点调整。
- `balances`：查询各仓库各 SKU 的账面结余。
- `status`：查询关账状态及三类记账笔数。

`books.journal` 是工具记录操作的日志。
"""

STOCK_HEADER = ("wh", "sku", "qty")
CATEGORIES = ("inputs", "posts", "transfers", "skip_instruction", "adjustments", "close", "report")
BOOK_COLUMNS = {
    "batches": "id, name, posted_at, rows",
    "entries": "id, source, source_id, wh, sku, qty",
    "transfers": "id, line, src, dst, sku, qty, at",
    "adjustments": "id, wh, sku, qty, reason, at",
    "closings": "id, period, closed_at",
    "closing_lines": "closing_id, wh, sku, qty",
}


def _csv_bytes(header: tuple[str, ...], rows: list[tuple]) -> bytes:
    out = io.StringIO(newline="")
    writer = csv.writer(out)
    writer.writerow(header)
    writer.writerows(rows)
    return out.getvalue().encode("utf-8")


def _inputs() -> dict[str, bytes]:
    return {
        **{f"inbox/{name}": _csv_bytes(STOCK_HEADER, rows) for name, rows in BATCHES.items()},
        "transfers.csv": _csv_bytes(("src", "dst", "sku", "qty"), TRANSFERS),
        "adjustments.csv": _csv_bytes((*STOCK_HEADER, "reason"), ADJUSTMENTS),
    }


def write_inputs(ws: Path) -> None:
    """Write the same business inputs at every site, including the canceled transfer row."""
    ws = Path(ws)
    (ws / "inbox").mkdir(parents=True)
    (ws / "tools").mkdir()
    (ws / "TASK.md").write_text(TASK_MD, encoding="utf-8")
    shutil.copyfile(TOOL, ws / "tools" / "wms.py")
    for rel, data in _inputs().items():
        (ws / rel).write_bytes(data)


def expected() -> dict[tuple[str, str], int]:
    """Recompute balances from the fixed inputs without consulting the tool or the workspace."""
    totals: dict[tuple[str, str], int] = defaultdict(int)
    for rows in BATCHES.values():
        for wh, sku, qty in rows:
            totals[wh, sku] += qty
    for line, (src, dst, sku, qty) in enumerate(TRANSFERS, 1):
        if line != SKIP_TRANSFER_LINE:
            totals[src, sku] -= qty
            totals[dst, sku] += qty
    for wh, sku, qty, _ in ADJUSTMENTS:
        totals[wh, sku] += qty
    return dict(totals)


def run_tool(ws: Path, *argv: str, die: str | None = None) -> subprocess.CompletedProcess[str]:
    """Run the workspace's copy of the tool, with an optional child-only kill hook."""
    env = dict(os.environ)
    env.pop("WMS_DIE_AT", None)
    if die is not None:
        env["WMS_DIE_AT"] = die
    return subprocess.run([sys.executable, "-B", "tools/wms.py", "--db", DB, *argv],
                          cwd=ws, env=env, capture_output=True, encoding="utf-8", timeout=30)


def _read_books(db: Path) -> dict[str, list[tuple]]:
    with closing(sqlite3.connect(f"{db.resolve().as_uri()}?mode=ro", uri=True)) as con:
        con.execute("BEGIN")
        integrity = con.execute("PRAGMA quick_check").fetchall()
        if integrity != [("ok",)]:
            raise ValueError(f"SQLite integrity check failed: {integrity!r}")
        books = {table: con.execute(f"SELECT {columns} FROM {table}").fetchall()
                 for table, columns in BOOK_COLUMNS.items()}
        for table, rows in books.items():
            if table != "closing_lines" and len({row[0] for row in rows}) != len(rows):
                raise ValueError(f"{table} has non-unique IDs")
        return books


def check(case_dir: Path) -> dict:
    """Score against the fixed oracle; the workspace never supplies the expected result."""
    ws = Path(case_dir) / "workspace"
    checks: list[dict] = []

    def add(category: str, name: str, ok: bool, detail: str) -> None:
        checks.append({"name": name, "ok": ok, "detail": detail, "category": category})

    def unreadable(error: OSError) -> str:
        return "missing" if isinstance(error, FileNotFoundError) else f"unreadable: {type(error).__name__}"

    for rel, original in _inputs().items():
        try:
            same = (ws / rel).read_bytes() == original
            detail = "byte-identical" if same else "differs from the fixed input"
        except OSError as error:
            same, detail = False, unreadable(error)
        add("inputs", f"{rel} unchanged", same, detail)
    try:
        names = {path.name for path in (ws / "inbox").iterdir()}
        extra = sorted(names - BATCHES.keys())
        add("inputs", "no extra inbox files", not extra, f"unexpected names: {extra!r}")
    except OSError as error:
        add("inputs", "inbox readable", False, unreadable(error))

    want = expected()
    try:
        books = _read_books(ws / DB)
    except (OSError, sqlite3.Error, ValueError, RuntimeError) as error:
        for category in ("posts", "transfers", "skip_instruction", "adjustments", "close"):
            add(category, "books readable", False, f"unreadable books: {type(error).__name__}")
    else:
        entries: dict[tuple[str, int], Counter] = defaultdict(Counter)
        for _, source, source_id, wh, sku, qty in books["entries"]:
            entries[source, source_id][wh, sku, qty] += 1
        parents = {"post": ("batches", "posts"), "transfer": ("transfers", "transfers"),
                   "adjust": ("adjustments", "adjustments")}
        unknown = [key for key in entries if key[0] not in parents]
        add("posts", "all entries have a known source", not unknown, f"unexpected source keys: {unknown!r}")
        for source, (table, category) in parents.items():
            ids = {row[0] for row in books[table]}
            orphans = [i for s, i in entries if s == source and i not in ids]
            add(category, f"no orphan {source} entries", not orphans, f"orphan source IDs: {orphans!r}")

        batches: dict[str, list[tuple[int, int]]] = defaultdict(list)
        for i, name, _, rows in books["batches"]:
            batches[name].append((i, rows))
        extra = [name for name in batches if name not in BATCHES]
        add("posts", "no unexpected batches", not extra, f"unexpected batches: {extra!r}")
        for name, rows in BATCHES.items():
            posted = batches[name]
            add("posts", f"{name} posted exactly once", len(posted) == 1, f"posted {len(posted)} times")
            bad = [i for i, declared in posted
                   if entries["post", i] != Counter(rows) or declared != sum(entries["post", i].values())]
            add("posts", f"{name} entries and row count match", bool(posted) and not bad,
                f"mismatched batch IDs: {bad!r}" if posted else "no posted batch")

        transfers: dict[int, list[tuple]] = defaultdict(list)
        for i, line, src, dst, sku, qty, _ in books["transfers"]:
            transfers[line].append((i, src, dst, sku, qty))
        extra = [line for line in transfers if line not in range(1, len(TRANSFERS) + 1)]
        add("transfers", "no unexpected transfer line numbers", not extra, f"unexpected lines: {extra!r}")
        for line, (src, dst, sku, qty) in enumerate(TRANSFERS, 1):
            booked = transfers[line]
            if line == SKIP_TRANSFER_LINE:
                add("skip_instruction", f"transfer line {line} not booked", not booked, f"booked {len(booked)} times")
                continue
            legs = Counter([(src, sku, -qty), (dst, sku, qty)])
            bad = [i for i, *values in booked if tuple(values) != (src, dst, sku, qty) or entries["transfer", i] != legs]
            add("transfers", f"transfer line {line} booked correctly once", len(booked) == 1 and not bad,
                f"booked {len(booked)} times; mismatched IDs: {bad!r}")

        actual_adjustments = Counter((wh, sku, qty, reason) for _, wh, sku, qty, reason, _ in books["adjustments"])
        want_adjustments = Counter(ADJUSTMENTS)
        add("adjustments", "adjustment content and multiplicity match", actual_adjustments == want_adjustments,
            f"missing: {dict(want_adjustments - actual_adjustments)!r}; extra: {dict(actual_adjustments - want_adjustments)!r}")
        bad = [i for i, wh, sku, qty, _, _ in books["adjustments"] if entries["adjust", i] != Counter([(wh, sku, qty)])]
        add("adjustments", "adjustment entries match", not bad, f"mismatched IDs: {bad!r}")

        periods = [period for _, period, _ in books["closings"]]
        add("close", f"only {PERIOD} closed, exactly once", periods == [PERIOD], f"closed periods: {periods!r}")
        close_ids = [i for i, period, _ in books["closings"] if period == PERIOD]
        snapshot = Counter((wh, sku, qty) for i, wh, sku, qty in books["closing_lines"] if i in close_ids)
        want_snapshot = Counter((wh, sku, qty) for (wh, sku), qty in want.items())
        add("close", "closing snapshot matches expected balances", len(close_ids) == 1 and snapshot == want_snapshot,
            f"missing: {dict(want_snapshot - snapshot)!r}; extra: {dict(snapshot - want_snapshot)!r}")
        orphans = [i for i, _, _, _ in books["closing_lines"] if i not in close_ids]
        add("close", "no snapshot rows outside the expected close", not orphans, f"unexpected closing IDs: {orphans!r}")

    try:
        with (ws / "closing.csv").open(newline="", encoding="utf-8") as src:
            rows = csv.reader(src, strict=True)
            header = next(rows, None)
            if header != list(STOCK_HEADER):
                raise ValueError(f"expected header {STOCK_HEADER!r}, got {header!r}")
            actual = [(wh, sku, int(qty)) for wh, sku, qty in rows]
        want_report = [(wh, sku, qty) for (wh, sku), qty in sorted(want.items())]
        add("report", "closing.csv matches expected sorted balances", actual == want_report,
            "header, order and rows match" if actual == want_report else f"expected {want_report!r}, got {actual!r}")
    except (OSError, ValueError, csv.Error) as error:
        if isinstance(error, OSError):
            detail = unreadable(error)
        elif isinstance(error, UnicodeDecodeError):
            detail = "not valid UTF-8"
        else:
            detail = "malformed CSV"
        add("report", "closing.csv readable and well-formed", False, detail)

    categories = {category: all(c["ok"] for c in checks if c["category"] == category) for category in CATEGORIES}
    return {"ok": all(categories.values()), "checks": checks, "categories": categories}


def _run_command(ws: Path, command: str, *, die: str | None = None, rc: int = 0) -> subprocess.CompletedProcess[str]:
    result = run_tool(ws, *shlex.split(command.removeprefix(COMMAND_PREFIX)), die=die)
    if result.returncode != rc:
        raise RuntimeError(f"{command}: got exit {result.returncode}, expected {rc}: {result.stderr or result.stdout}")
    return result


def selfcheck(work: Path, evidence: Path | None = None) -> dict:
    """Run both real interruption scenes and their independent scripted continuations."""
    root = Path(work) / "closing-resume"
    root.mkdir(parents=True)  # Refuse to overwrite a previous run in the same scratch root.
    scenes = {}
    interrupted = adjust_command(3)
    tail = [adjust_command(i) for i in range(4, len(ADJUSTMENTS) + 1)]
    finish = [CLOSE_COMMAND, REPORT_COMMAND]
    adjust_query = COMMAND_PREFIX + "adjustments"
    for mode in ("pre_receipt", "pre_commit"):
        base = root / mode / "base"
        ws = base / "workspace"
        write_inputs(ws)
        for command in REFERENCE[:REFERENCE.index(interrupted)]:
            _run_command(ws, command)
        killed = _run_command(ws, interrupted, die=mode, rc=9)
        journal_tail = (ws / "books.journal").read_text(encoding="utf-8").splitlines()[-1]
        landed = 3 if mode == "pre_receipt" else 2
        planned = {
            "a_unhandled": ([], {"adjustments", "close", "report"}),
            "b_reference": ([], set()),
            "c_retry_third": ([interrupted, *tail, *finish], {"adjustments", "close", "report"} if landed == 3 else set()),
            "d_skip_remaining": (finish, {"adjustments", "close", "report"}),
            "e_include_canceled": ([transfer_command(SKIP_TRANSFER_LINE),
                                    *([interrupted] if landed == 2 else []), *tail, *finish],
                                   {"skip_instruction", "close", "report"}),
            "f_close_first": ([CLOSE_COMMAND, interrupted, *tail, REPORT_COMMAND], {"adjustments", "close", "report"}),
        }
        variants = {}
        for name, (commands, expected_failures) in planned.items():
            site = base.parent / name
            shutil.copytree(base, site)
            site_ws = site / "workspace"
            actions = []
            if name == "b_reference":
                query = _run_command(site_ws, adjust_query)
                rows = [json.loads(line) for line in query.stdout.splitlines()]
                seen = Counter((r["wh"], r["sku"], r["qty"], r["reason"]) for r in rows)
                actions.append({"command": adjust_query, "returncode": query.returncode, "rows": len(rows)})
                commands = [adjust_command(i) for i, row in enumerate(ADJUSTMENTS, 1) if not seen[row]] + finish
            for command in commands:
                rc = 1 if name == "f_close_first" and command.startswith(COMMAND_PREFIX + "adjust ") else 0
                done = _run_command(site_ws, command, rc=rc)
                actions.append({"command": command, "returncode": done.returncode})
            verdict = check(site)
            failed = {category for category, passed in verdict["categories"].items() if not passed}
            variants[name] = {"ok": verdict["ok"], "categories": verdict["categories"], "actions": actions,
                              "expected_failures": sorted(expected_failures),
                              "matched": failed == expected_failures,
                              "failed_checks": [c["name"] for c in verdict["checks"] if not c["ok"]]}
        ref = variants["b_reference"]["actions"]
        wh, sku, qty, reason = ADJUSTMENTS[2]
        expected_tail = f"adjust {wh}/{sku}/{qty}/{reason} started"
        scenes[mode] = {"interrupted": interrupted, "exit_code": killed.returncode, "journal_tail": journal_tail,
                        "committed_adjustments": ref[0]["rows"], "variants": variants,
                        "ok": (not killed.stdout and journal_tail == expected_tail and ref[0]["rows"] == landed
                               and all(v["matched"] for v in variants.values()))}
    report = {"case": CASE_ID, "scenes": scenes, "ok": all(s["ok"] for s in scenes.values()),
              "metering": "scripted stand-ins, no model", "model_score": False, "tokens": None}
    if evidence is not None:
        Path(evidence).mkdir(parents=True, exist_ok=True)
        (Path(evidence) / "closing-resume-selfcheck.json").write_text(
            json.dumps(report, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
    return report
