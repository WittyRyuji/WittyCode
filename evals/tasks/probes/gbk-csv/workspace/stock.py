"""Reads the warehouse stock file (GBK text with CRLF line ends, written by a legacy system)."""

import csv
from pathlib import Path

STOCK_FILE = Path(__file__).parent / "data" / "stock.csv"


def load() -> dict[str, int]:
    """The stock of every product, by name."""
    with STOCK_FILE.open(encoding="gbk", newline="") as file:
        rows = list(csv.reader(file))
    return {name: int(count) for name, count, _unit in rows[1:]}


def total() -> int:
    return sum(load().values())
