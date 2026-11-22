"""``python -m evals.inventory_resume_case``: build, score and self-check the inventory crash/resume scene.

Commands (run from the repository root)::

    python -m evals.inventory_resume_case prepare --dir CASE --mode pre_commit|pre_receipt
    python -m evals.inventory_resume_case check --dir CASE
    python -m evals.inventory_resume_case selfcheck [--work DIR] [--evidence DIR]

``prepare`` refuses an existing directory. ``selfcheck`` defaults ``--work`` to ``WITTYCODE_TEST_WORK`` and
``--evidence`` to ``WITTYCODE_TEST_EVIDENCE`` — the managed test runner's scratch and evidence folders.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

from evals.inventory_resume_case import case


def _prepare(args: argparse.Namespace) -> int:
    try:
        meta = case.prepare(Path(args.dir), args.mode)
    except (FileExistsError, ValueError, RuntimeError) as error:
        print(f"error: {error}", file=sys.stderr)
        return 2
    print(json.dumps(meta, ensure_ascii=False))
    print(f"scene ready: {Path(args.dir) / 'workspace'}  (give the model TASK.md, then send: {case.RESUME_INPUT})")
    return 0


def _check(args: argparse.Namespace) -> int:
    verdict = case.check(Path(args.dir))
    print(json.dumps(verdict, ensure_ascii=False, indent=1))
    return 0 if verdict["ok"] else 1


def _selfcheck(args: argparse.Namespace) -> int:
    work = args.work or os.environ.get("WITTYCODE_TEST_WORK", "")
    if not work:
        print("error: selfcheck needs --work or WITTYCODE_TEST_WORK", file=sys.stderr)
        return 2
    evidence = args.evidence or os.environ.get("WITTYCODE_TEST_EVIDENCE", "") or None
    try:
        report = case.selfcheck(Path(work), Path(evidence) if evidence else None)
    except FileExistsError as error:
        print(f"error: {error}", file=sys.stderr)
        return 2
    print(json.dumps(report, ensure_ascii=False, indent=1))
    print("\nmechanism check only — not a model score")
    return 0 if report["ok"] else 1


def main(argv: list[str] | None = None) -> int:
    root = argparse.ArgumentParser(prog="python -m evals.inventory_resume_case", description=__doc__)
    commands = root.add_subparsers(dest="command", required=True)
    p = commands.add_parser("prepare", help="build a fresh interrupted scene; refuses an existing --dir")
    p.add_argument("--dir", required=True, help="new case directory to create (pick a neutral name)")
    p.add_argument("--mode", required=True, choices=case.MODES)
    c = commands.add_parser("check", help="score a scene: books + reconcile.csv vs the inbox ground truth")
    c.add_argument("--dir", required=True)
    s = commands.add_parser("selfcheck", help="mechanism check: both scenes + scripted stand-ins (not a model score)")
    s.add_argument("--work", default="", help="scratch root (default: $WITTYCODE_TEST_WORK)")
    s.add_argument("--evidence", default="", help="where to put the JSON report (default: $WITTYCODE_TEST_EVIDENCE)")
    args = root.parse_args(argv)
    return {"prepare": _prepare, "check": _check, "selfcheck": _selfcheck}[args.command](args)


if __name__ == "__main__":
    sys.exit(main())
