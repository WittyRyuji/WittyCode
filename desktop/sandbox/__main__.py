"""Entry point: ``python -m desktop.sandbox``.

Runs a single command inside the OS sandbox. The workspace is read-only
unless ``--write`` is given; the network is always off. Everything after a
bare ``--`` is the command and its arguments.
"""

from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path

from desktop.sandbox import core


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m desktop.sandbox",
        description="Run one command inside the OS sandbox (workspace read-only by default; network always off).",
    )
    parser.add_argument("--workspace", type=Path,
                        help="existing private directory; read-only unless --write is given")
    parser.add_argument("--write", action="store_true",
                        help="allow the command to modify the workspace")
    parser.add_argument("--protect", type=Path, action="append", default=[], metavar="PATH",
                        help="file or directory inside the workspace that stays read-only (repeatable)")
    parser.add_argument("--read", type=Path, action="append", default=[], metavar="PATH", dest="readable",
                        help="extra trusted read-only runtime path, e.g. a tool directory (repeatable)")
    parser.add_argument("--tool", type=Path,
                        help="absolute path of the program to run (recommended over a bare argv[0])")
    parser.add_argument("--env", action="append", default=[], metavar="NAME=VALUE",
                        help="extra environment variable on top of the minimal whitelist (repeatable)")
    parser.add_argument("--timeout", type=float, default=60.0,
                        help="seconds before the child is killed (default: 60)")
    parser.add_argument("--scratch-root", type=Path,
                        help="parent for the per-run scratch directory "
                             "(default: WITTYCODE_TEST_WORK or the system temp directory)")
    parser.add_argument("--probe", action="store_true",
                        help="only report whether this platform's backend is usable, then exit")
    return parser


def _split(argv: list[str]) -> tuple[list[str], list[str]]:
    if "--" in argv:
        index = argv.index("--")
        return argv[:index], argv[index + 1:]
    return argv, []


def _env_pairs(items: list[str]) -> dict[str, str]:
    pairs: dict[str, str] = {}
    for item in items:
        name, sep, value = item.partition("=")
        if not sep:
            raise core.PolicyError(f"--env expects NAME=VALUE, got {item!r}")
        pairs[name] = value
    return pairs


def main(argv: list[str] | None = None, *, backend=None) -> int:
    options, command = _split(list(sys.argv[1:] if argv is None else argv))
    parser = _parser()
    args = parser.parse_args(options)

    if args.probe:
        try:
            name = core.backend_module_name()
        except core.SandboxError as error:
            print(f"sandbox: {error}", file=sys.stderr)
            return 1
        ok, detail = core.probe(backend=backend)
        print(f"{name}: {'supported' if ok else 'unavailable'} ({detail})")
        return 0 if ok else 1

    if args.workspace is None:
        parser.error("--workspace is required")
    tool = [str(args.tool)] if args.tool is not None else []
    if not tool and not command:
        parser.error("a command is required after '--' (or pass --tool)")

    try:
        result = core.run(
            tuple(tool + command),
            workspace=args.workspace,
            writable=args.write,
            protected=tuple(args.protect),
            readable=tuple(args.readable),
            env=_env_pairs(args.env),
            timeout=args.timeout,
            scratch_root=args.scratch_root,
            backend=backend,
        )
    except subprocess.TimeoutExpired:
        print("sandbox: the command timed out", file=sys.stderr)
        return 124
    except core.SandboxError as error:
        print(f"sandbox: {error}", file=sys.stderr)
        return 2
    sys.stdout.buffer.write(result.stdout or b"")
    sys.stdout.buffer.flush()
    sys.stderr.buffer.write(result.stderr or b"")
    sys.stderr.buffer.flush()
    return int(result.returncode)


if __name__ == "__main__":
    sys.exit(main())
