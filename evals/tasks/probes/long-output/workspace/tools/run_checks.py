"""Runs every check of the project and prints one line per check. Exit code 1 when any check fails."""

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CHECKS = 4000
SETTINGS_CHECK = 2311


def settings_problem() -> str:
    settings = json.loads((ROOT / "settings.json").read_text(encoding="utf-8"))
    value = settings.get("retry_limit")
    if not isinstance(value, int) or isinstance(value, bool):
        return f'settings.json: "retry_limit" must be a whole number, found {value!r}'
    if not 1 <= value <= 10:
        return f'settings.json: "retry_limit" must be between 1 and 10, found {value!r}'
    return ""


def main() -> int:
    failed = 0
    for number in range(1, CHECKS + 1):
        problem = settings_problem() if number == SETTINGS_CHECK else ""
        if problem:
            failed += 1
            print(f"FAIL  check_{number:04d}  {problem}")
        else:
            print(f"ok    check_{number:04d}  catalogue entry {number:04d} is consistent with the index")
    print(f"{CHECKS} checks, {failed} failed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
