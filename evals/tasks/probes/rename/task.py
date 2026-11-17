"""One name in fourteen places of six files. What it asks of a harness: find every place and change them in few steps
(an edit tool that takes one place per call needs a round trip to the model for each)."""

from __future__ import annotations

from pathlib import Path


def solve(root: Path) -> None:
    for file in [root / "README.md", *sorted((root / "shop").glob("*.py"))]:
        with file.open(encoding="utf-8", newline="") as source:
            text = source.read()
        with file.open("w", encoding="utf-8", newline="") as target:
            target.write(text.replace("calc_total", "order_total"))
