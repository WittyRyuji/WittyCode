"""One validation entry point for local clean checkouts, CI and releases (no model calls).

Run with --clean locally to copy only versioned/non-ignored files into a disposable checkout, install the pinned
dependencies in an isolated venv, and execute exactly the same checks as the GitHub jobs. Existing build output and
node_modules are never copied. Test temporary folders stay outside the repository for correct Git/AGENTS discovery.
"""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import time

PYTHON_VERSION = (3, 11, 9)
NODE_VERSION = "v24.22.3"
ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from desktop.core.test_runs import ManagedTestRun


def commands(part: str, python: str = sys.executable, root: Path = ROOT) -> list[tuple[str, list[str]]]:
    rows: list[tuple[str, list[str]]] = []
    if part == "windows":
        # Keep the local whole-window list aligned with the modules deliberately excluded by CI.
        files = sorted(p.relative_to(root).as_posix() for p in (root / "tests").glob("test_app*e2e*.py"))
        files.append("tests/test_shell.py")
        return [("frontend", ["npm", "ci"]), ("frontend", ["npm", "run", "build:app"]), (".", [python, "-m", "pytest", *files, "-q", "-p", "no:cacheprovider"])]
    if part in ("frontend", "all"):
        rows += [("frontend", ["npm", "ci"]), ("frontend", ["npm", "run", "build:app"]), ("frontend", ["npm", "run", "unit"])]
        rows += [("frontend", ["node", "scripts/" + name]) for name in ("check-page-readiness.mjs", "check.mjs", "check-live.mjs")]
    if part in ("python", "all"):
        rows.append((".", [python, "-m", "pytest", "tests", "-q", "-p", "no:cacheprovider", "--ignore-glob=tests/test_app*e2e*.py", "--ignore=tests/test_shell.py"]))
    return rows


def run(argv: list[str], cwd: Path, env: dict[str, str]) -> None:
    executable = shutil.which(argv[0]) if argv[0] in ("npm", "node") else argv[0]
    if not executable:
        raise RuntimeError(f"Executable not found: {argv[0]}")
    print("RUN", cwd.name, *argv, flush=True)
    subprocess.run([executable, *argv[1:]], cwd=cwd, env=env, check=True)


def clean_copy(source: Path, destination: Path) -> None:
    listed = subprocess.check_output(["git", "ls-files", "-z", "--cached", "--others", "--exclude-standard"], cwd=source)
    for name in listed.decode("utf-8").split("\0"):
        if not name:
            continue
        relative = Path(name)
        if relative.is_absolute() or ".." in relative.parts:
            raise RuntimeError("Invalid repository file path")
        file = source / relative
        if not file.is_file():
            continue
        target = destination / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(file, target)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--part", choices=("frontend", "python", "all", "windows"), default="all")
    parser.add_argument("--clean", action="store_true")
    options = parser.parse_args()
    if sys.version_info[:3] != PYTHON_VERSION:
        raise RuntimeError("Use Python 3.11.9, matching CI")
    if options.part in ("frontend", "all", "windows"):
        node = subprocess.check_output([shutil.which("node") or "node", "--version"], text=True).strip()
        if node != NODE_VERSION:
            raise RuntimeError(f"Use Node {NODE_VERSION}, matching CI (found {node})")
    root, python = ROOT, sys.executable
    with ManagedTestRun(ROOT, part=options.part, clean=options.clean) as managed:
        env = {**os.environ, **managed.environment(), "PYTHONUTF8": "1", "PYTHONIOENCODING": "utf-8"}
        if options.clean:
            destination = managed.work
            root = destination / "repo"
            clean_copy(ROOT, root)
            run(["git", "init", "--quiet", str(root)], ROOT, env)
            run([sys.executable, "-m", "venv", str(destination / "venv")], ROOT, env)
            python = str(destination / "venv" / "Scripts" / "python.exe")
            run([python, "-m", "pip", "install", "-r", "requirements-dev.txt", "-c", "constraints.txt"], root, env)
            print("Clean checkout:", root, flush=True)
        env["WITTYCODE_TEST_PYTHON"] = python
        env["WITTYCODE_REQUIRE_QT"] = "1"
        env["WITTYCODE_SETTINGS"] = str(managed.work / "ci-profile" / "settings.json")
        for folder, argv in commands(options.part, python, root):
            if argv[1:3] == ["-m", "pytest"]:
                # Outside both checkouts; skip pytest's numbered nesting to stay clear of Windows path limits.
                argv = [*argv, "--basetemp", str(managed.work / "p")]
            run(argv, root / folder, env)
    print("PASS", json.dumps({"part": options.part, "clean": options.clean}), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
