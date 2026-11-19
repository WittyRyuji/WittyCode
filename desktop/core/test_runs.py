"""Built-in test lifecycle. Cleanup owns a directory it created, never a path nominated by a session."""

from __future__ import annotations

import json
import os
import shutil
import stat
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path
from types import TracebackType

WORK = "WITTYCODE_TEST_WORK"
EVIDENCE = "WITTYCODE_TEST_EVIDENCE"


def _identity(path: Path) -> tuple[int, int]:
    info = path.lstat()
    if (not stat.S_ISDIR(info.st_mode) or not info.st_ino
            or getattr(info, "st_file_attributes", 0) & stat.FILE_ATTRIBUTE_REPARSE_POINT):
        raise OSError(f"Not a verifiable ordinary directory: {path}")
    return info.st_dev, info.st_ino


class ManagedTestRun:
    """One foreground test invocation; the owner alone calls the completion hook on leaving the scope."""

    def __init__(self, project: Path, **details: str | bool) -> None:
        self.project = project.resolve(strict=True)
        self.details = details
        self._owner: tuple[int, int] | None = None

    def __enter__(self) -> ManagedTestRun:
        if self._owner is not None or hasattr(self, "work"):
            raise RuntimeError("A test run can only be entered once.")
        if WORK in os.environ:
            raise RuntimeError("Do not nest managed runs; start each session's test separately.")
        base = Path(tempfile.gettempdir()).resolve(strict=True)
        if base.is_relative_to(self.project) or any((p / ".git").exists() for p in (base, *base.parents)):
            raise RuntimeError("The system temp directory must be outside the project and any Git working tree.")
        self._started = time.monotonic()
        self.work = Path(tempfile.mkdtemp(prefix="wittycode-test-", dir=base))
        self._owner = _identity(self.work)
        self._owner_thread = threading.get_ident()
        try:
            parent = self.project
            for name in ("outputs", "test-runs"):
                parent = parent / name
                parent.mkdir(exist_ok=True)
                _identity(parent)
            self.evidence = parent / self.work.name
            self.evidence.mkdir()
            self._evidence_owner = _identity(self.evidence)
            self.temp = self.work / "tmp"
            self.temp.mkdir()
            self._report({**self.details, "status": "running", "cleanup": "pending"})
        except BaseException:
            self._cleanup()
            raise
        return self

    def environment(self) -> dict[str, str]:
        return {WORK: str(self.work), EVIDENCE: str(self.evidence),
                **dict.fromkeys(("TEMP", "TMP", "TMPDIR", "PYTEST_DEBUG_TEMPROOT"), str(self.temp)),
                "WITTYCODE_SETTINGS": str(self.work / "profile" / "settings.json")}

    def _cleanup(self) -> None:
        try:
            current = _identity(self.work)
        except FileNotFoundError:
            return
        if current != self._owner or self.work.resolve(strict=True) != self.work:
            raise OSError(f"Test directory ownership changed; refusing cleanup: {self.work}")

        def onerror(function, path, exc) -> None:
            error = exc[1]
            # Python 3.11 rmtree still reports files independently removed during teardown.
            if isinstance(error, FileNotFoundError):
                return
            if os.name == "nt" and function is os.unlink and isinstance(error, PermissionError):
                # Git creates read-only objects on Windows. Never chmod a link or a shared inode.
                file = Path(path)
                try:
                    info = file.lstat()
                    attrs = info.st_file_attributes
                    if (stat.S_ISREG(info.st_mode) and info.st_nlink == 1
                            and attrs & stat.FILE_ATTRIBUTE_READONLY and not attrs & stat.FILE_ATTRIBUTE_REPARSE_POINT
                            and file.is_relative_to(self.work) and file.resolve(strict=True) == file):
                        file.chmod(info.st_mode | stat.S_IWRITE)
                        file.unlink(missing_ok=True)
                        return
                except FileNotFoundError:
                    return
            raise error

        # On supported Windows Python, rmtree removes junctions without traversing their targets.
        shutil.rmtree(self.work, onerror=onerror)

    def _report(self, result: dict) -> None:
        if self.evidence.resolve(strict=True) != self.evidence or _identity(self.evidence) != self._evidence_owner:
            raise OSError("Evidence directory changed during the test.")
        with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=self.evidence, delete=False) as pending:
            path = Path(pending.name)
            try:
                json.dump(result, pending, ensure_ascii=False, indent=2)
                pending.write("\n")
                pending.close()
                path.replace(self.evidence / "result.json")
            finally:
                pending.close()
                path.unlink(missing_ok=True)

    def __exit__(self, kind: type[BaseException] | None, error: BaseException | None,
                 trace: TracebackType | None) -> None:
        """Official completion hook: save the conclusion, then remove only this invocation's scratch."""
        if threading.get_ident() != self._owner_thread:
            raise RuntimeError("Only the thread that started this test can finish it.")
        exit_code = (0 if error is None else 130 if isinstance(error, KeyboardInterrupt)
                     else error.returncode if isinstance(error, subprocess.CalledProcessError) else 1)
        result = {**self.details, "exit_code": exit_code, "seconds": round(time.monotonic() - self._started, 2),
                  "status": "passed" if error is None else "cancelled" if exit_code == 130 else "failed",
                  "cleanup": "pending"}
        report_error = cleanup_error = None
        try:
            self._report(result)
        except OSError as exc:
            report_error = exc
        try:
            self._cleanup()
        except OSError as exc:
            cleanup_error = exc
        result["cleanup"] = "blocked" if cleanup_error else "done"
        if cleanup_error:
            result["remaining_directory"] = str(self.work)
        try:
            self._report(result)
        except OSError as exc:
            report_error = exc
        else:
            report_error = None
            print("Test evidence:", self.evidence / "result.json", flush=True)
        for issue in (report_error, cleanup_error):
            if issue is not None:
                print(f"Test finalizer: {issue}", file=sys.stderr, flush=True)
        if cleanup_error:
            print(f"Test directory not fully removed: {self.work}", file=sys.stderr, flush=True)
        if error is None and (cleanup_error or report_error):
            raise RuntimeError("The command passed, but test finalization did not complete.") from (cleanup_error or report_error)


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    if not args or args[0] != "--" or len(args) < 2:
        print("Usage: python test_runs.py -- <foreground executable> [arguments ...]", file=sys.stderr)
        return 2
    try:
        with ManagedTestRun(Path.cwd()) as run:
            subprocess.run(args[1:], env={**os.environ, **run.environment()}, check=True)
    except KeyboardInterrupt:
        return 130
    except subprocess.CalledProcessError as error:
        return error.returncode
    except (RuntimeError, OSError) as error:
        print(f"Managed test: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
