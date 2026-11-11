"""Which patterns of the old mutation scripts no longer match the code (read only).

The old scripts (``mutate_followup.py`` and the like) write each mutant into the real file; they take their arguments as a filter on
the mutants' names (``only = sys.argv[1:]``) and do not know ``--check``. A word that no mutant's name contains selects nothing,
so running such a script with one runs no mutant at all but still builds its table ``M``; this script does that and counts how often
each pattern is found. Nothing is written (the page-side scripts rebuild the page once, which changes nothing).

    python sandbox/check_old_patterns.py

Only scripts that read their arguments that way are listed here; do not add one that does not (a script that reads them otherwise
may run its mutants in place, and one killed half way leaves a mutant in the source).
"""
from __future__ import annotations

import runpy
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SCRIPTS = ["followup", "frontend", "envvar", "devin", "devin_page", "memory_page", "cache"]


def main() -> int:
    stale_total = 0
    for name in SCRIPTS:
        sys.argv = ["mutate", "no mutant is called this"]
        try:
            table = runpy.run_path(str(ROOT / "sandbox" / f"mutate_{name}.py"), run_name="__check__").get("M")
        except SystemExit:
            table = None
        if table is None:
            print(f"{name}: no table M")
            continue
        stale = []
        for number, entry in enumerate(table, 1):
            label, path, old = entry[0], Path(entry[1]), entry[2]
            found = path.read_bytes().decode("utf-8").replace("\r\n", "\n").count(old)
            if found != 1:
                stale.append((number, label, found))
        stale_total += len(stale)
        print(f"{name}: {len(table)} mutants, " + ("all patterns found once" if not stale else f"{len(stale)} patterns not found once"))
        for number, label, found in stale:
            print(f"    [{number}] {label}: found {found} times")
    return 1 if stale_total else 0


if __name__ == "__main__":
    sys.exit(main())
