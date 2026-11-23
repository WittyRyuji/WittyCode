"""Score a month-end workspace or run both scripted recovery scenes."""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

from . import case


def main(argv: list[str] | None = None) -> int:
    root = argparse.ArgumentParser(prog="python -m evals.closing_resume_case", description=__doc__)
    commands = root.add_subparsers(dest="command", required=True)
    check = commands.add_parser("check", help="check <dir>/workspace against the fixed inputs and oracle")
    check.add_argument("--dir", required=True, type=Path)
    selfcheck = commands.add_parser("selfcheck", help="run the scripted stand-ins, without a model")
    selfcheck.add_argument("--work", help="scratch root (default: WITTYCODE_TEST_WORK)")
    selfcheck.add_argument("--evidence", help="evidence directory (default: WITTYCODE_TEST_EVIDENCE)")
    args = root.parse_args(argv)
    if args.command == "check":
        result = case.check(args.dir)
    else:
        work = args.work or os.environ.get("WITTYCODE_TEST_WORK")
        evidence = args.evidence or os.environ.get("WITTYCODE_TEST_EVIDENCE")
        if not work:
            selfcheck.error("selfcheck needs --work or WITTYCODE_TEST_WORK")
        try:
            result = case.selfcheck(Path(work), Path(evidence) if evidence else None)
        except (OSError, RuntimeError, ValueError) as error:
            print(f"error: {error}", file=sys.stderr)
            return 2
    print(json.dumps(result, indent=1))
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
