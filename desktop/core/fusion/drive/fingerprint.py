"""Read-only workspace stamps for Fusion Drive reuse (design 8.5)."""
from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import stat
import subprocess
from collections.abc import Iterator, Sequence
from dataclasses import dataclass
from pathlib import Path, PureWindowsPath

_SKIP = frozenset({".git", "node_modules", "__pycache__", ".venv", "venv", "dist", "build", ".mypy_cache", ".pytest_cache"})
_MAX_FILES = 20_000
_MAX_HASH_BYTES = 8 * 1024 * 1024
_SHA256 = re.compile(r"[0-9a-f]{64}\Z")
_DRIVE = re.compile(r"[a-zA-Z]:")


def _relative(path: str) -> str:
    windows = PureWindowsPath(path)
    if windows.anchor or "\0" in path or any(part == ".." or _DRIVE.match(part) for part in windows.parts):
        raise ValueError("Workspace paths must be project-relative and must not contain '..'.")
    return "/".join(part for part in path.replace("\\", "/").split("/") if part and part != ".")


def _under(name: str, paths: Sequence[str]) -> bool:
    return not paths or any(name == path or name.startswith(path + "/") for path in paths)


def _linked(info: os.stat_result) -> bool:
    return stat.S_ISLNK(info.st_mode) or bool(getattr(info, "st_file_attributes", 0) & stat.FILE_ATTRIBUTE_REPARSE_POINT)


def _digest(value: object) -> str:
    return hashlib.sha256(json.dumps(value, separators=(",", ":")).encode("utf-8")).hexdigest()


def _file_hash(path: Path) -> str:
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def _plain_files(project: Path) -> Iterator[tuple[Path, os.stat_result]]:
    pending = [project]
    while pending:
        folder = pending.pop()
        try:
            if _linked(folder.lstat()):
                continue
            with os.scandir(folder) as scan:
                children = sorted(scan, key=lambda entry: entry.name)
        except OSError:
            continue  # A directory may disappear while scanning a live workspace.
        directories = []
        for child in children:
            if child.name.lower() in _SKIP:
                continue
            try:
                info = child.stat(follow_symlinks=False)
            except OSError:
                continue
            if _linked(info):
                continue
            path = Path(child.path)
            if stat.S_ISDIR(info.st_mode):
                directories.append(path)
            elif stat.S_ISREG(info.st_mode):
                yield path, info
        pending.extend(reversed(directories))


@dataclass(frozen=True, slots=True)
class _File:
    size: int
    mtime_ns: int
    sha256: str | None

    def fields(self) -> tuple[int, int, str | None]:
        return self.size, self.mtime_ns, self.sha256


@dataclass(slots=True)
class Manifest:
    """A bounded inventory; a truncated scan cannot establish that omitted files are unchanged."""

    entries: dict[str, _File]
    truncated: bool = False

    @classmethod
    def scan(cls, project: Path, *, previous: Manifest | None = None) -> Manifest:
        """Reuse hashes for files with unchanged metadata, and only read small files."""
        entries: dict[str, _File] = {}
        for path, info in _plain_files(project):
            if len(entries) == _MAX_FILES:
                return cls(entries, truncated=True)
            name = path.relative_to(project).as_posix()
            old = previous.entries.get(name) if previous is not None else None
            if old is not None and (old.size, old.mtime_ns) == (info.st_size, info.st_mtime_ns):
                entries[name] = old
                continue
            digest = None
            if info.st_size <= _MAX_HASH_BYTES:
                try:
                    digest = _file_hash(path)
                except OSError:
                    pass  # Retain metadata even when the file became unreadable.
            entries[name] = _File(info.st_size, info.st_mtime_ns, digest)
        return cls(entries)

    def to_json(self) -> str:
        return json.dumps({"truncated": self.truncated, "entries": {k: v.fields() for k, v in self.entries.items()}},
                          sort_keys=True, separators=(",", ":"))

    @classmethod
    def from_json(cls, text: str) -> Manifest:
        """Refuse damaged inventories instead of silently forgetting entries."""
        try:
            data = json.loads(text)
            if (not isinstance(data, dict) or data.keys() != {"truncated", "entries"}
                    or type(data["truncated"]) is not bool or not isinstance(data["entries"], dict)):
                raise ValueError
            entries = {}
            for name, fields in data["entries"].items():
                if not name or _relative(name) != name or not isinstance(fields, list) or len(fields) != 3:
                    raise ValueError
                size, mtime, digest = fields
                if type(size) is not int or size < 0 or type(mtime) is not int:
                    raise ValueError
                if digest is not None and (not isinstance(digest, str) or _SHA256.fullmatch(digest) is None):
                    raise ValueError
                entries[name] = _File(size, mtime, digest)
            return cls(entries, data["truncated"])
        except (TypeError, ValueError, RecursionError) as error:
            raise ValueError("Damaged workspace manifest.") from error

    @staticmethod
    def changed(old: Manifest, new: Manifest) -> list[str]:
        return sorted(name for name in old.entries.keys() | new.entries.keys() if old.entries.get(name) != new.entries.get(name))


class _GitError(Exception):
    pass


def _git(project: Path, *args: str, allow_missing: bool = False) -> str | None:
    executable = shutil.which("git")
    if executable is None:
        raise _GitError
    env = dict(os.environ)
    for key in ("GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE", "GIT_COMMON_DIR", "GIT_OBJECT_DIRECTORY",
                "GIT_ALTERNATE_OBJECT_DIRECTORIES", "GIT_GLOB_PATHSPECS", "GIT_NOGLOB_PATHSPECS", "GIT_ICASE_PATHSPECS"):
        env.pop(key, None)
    env.update(GIT_OPTIONAL_LOCKS="0", GIT_NO_LAZY_FETCH="1")
    argv = [executable, "--no-optional-locks", "--literal-pathspecs", "--no-pager", "-c", "core.fsmonitor=false",
            "-c", "core.hooksPath=" + os.devnull, *args]
    flags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
    try:
        result = subprocess.run(argv, cwd=project, env=env, stdin=subprocess.DEVNULL, capture_output=True,
                                timeout=10, creationflags=flags)
    except (OSError, ValueError, subprocess.TimeoutExpired) as error:
        raise _GitError from error
    if allow_missing and result.returncode == 1:
        return None  # rev-parse --verify --quiet uses 1 for an unresolved revision.
    if result.returncode:
        raise _GitError
    return result.stdout.decode("utf-8", errors="replace")


def _git_prefix(project: Path) -> str:
    output = _git(project, "rev-parse", "--is-inside-work-tree", "--show-prefix")
    inside, _, prefix = (output or "").partition("\n")
    if inside != "true":
        raise _GitError
    return prefix.removesuffix("\n")


def workspace_kind(project: Path) -> str:
    """Recognize a working tree, including a folder below its root."""
    try:
        _git_prefix(project)
    except _GitError:
        return "plain"
    return "git"


def _head(project: Path) -> str:
    revision = _git(project, "rev-parse", "--verify", "--quiet", "HEAD^{commit}", allow_missing=True)
    if revision is None:
        _git(project, "symbolic-ref", "--quiet", "HEAD")  # An unborn branch still has a symbolic HEAD.
    return (revision or "").strip()


def _check_git_config(project: Path) -> None:
    from desktop.core.harness.permissions import git_runs_own_programs

    # Status may invoke a configured clean filter even when index refreshes are disabled.
    if git_runs_own_programs(project.absolute()) is not None:
        raise _GitError


def _status(project: Path, prefix: str, paths: Sequence[str]) -> list[tuple[str, tuple[str, ...]]]:
    output = _git(project, "status", "--porcelain=v1", "-z", "--untracked-files=all", "--", *(paths or (".",)))
    fields = iter((output or "").split("\0")[:-1])
    status = []
    for row in fields:
        if len(row) < 4 or row[2] != " ":
            raise _GitError
        code, raw_names = row[:2], (row[3:],)
        if "R" in code or "C" in code:
            source = next(fields, "")
            if not source:
                raise _GitError
            raw_names += (source,)  # In porcelain -z the destination comes first.
        names = tuple(name[len(prefix):] for name in raw_names if name.startswith(prefix))
        names = tuple(name for name in names if _under(name, paths))
        if code == "??":
            # What the plain inventory skips: caches and build output that running the project leaves behind. A tracked
            # file is the project's work wherever it lies, so only untracked ones are left out.
            names = tuple(name for name in names if not any(part.lower() in _SKIP for part in name.split("/")))
        if names:
            status.append((code, names))
    return status


def _git_file(project: Path, name: str) -> tuple[str, str] | None:
    # Do not turn Git's tracked links into reads outside the project.
    parts = name.split("/")
    if any(part in {"", ".", ".."} for part in parts) or (os.name == "nt" and (":" in name or "\\" in name)):
        return None
    path = project
    try:
        for part in parts[:-1]:
            path /= part
            if _linked(path.lstat()):
                return None
        path /= parts[-1]
        info = path.lstat()
        if stat.S_ISLNK(info.st_mode):
            return "link", _digest(os.readlink(path))
        if not _linked(info) and stat.S_ISREG(info.st_mode):
            return "file", _file_hash(path)
    except (FileNotFoundError, NotADirectoryError):
        pass  # A removed file has a status entry, but no worktree bytes.
    return None


def fingerprint(project: Path, paths: Sequence[str] = (), *, manifest: Manifest | None = None) -> str:
    """Hash the revision and scoped dirty files, or the scoped plain inventory."""
    scope = tuple(_relative(path) for path in paths)
    if "" in scope:
        scope = ()
    try:
        prefix = _git_prefix(project)
        _check_git_config(project)
        head = _head(project)
        dirty = _status(project, prefix, scope)
        index = ""
        if any(code[0] not in " ?!" for code, _ in dirty):
            # The staged blob can change even if the status letters and worktree bytes do not.
            index = _git(project, "diff", "--cached", "--raw", "--no-abbrev", "-z", "--relative", "--no-renames",
                         "--no-ext-diff", "--no-textconv", *([head] if head else []), "--", *(scope or (".",))) or ""
        files = sorted({name for _, names in dirty for name in names})
        return _digest(("git", head, dirty, index, [(name, _git_file(project, name)) for name in files]))
    except (_GitError, OSError):
        if manifest is None:
            manifest = Manifest.scan(project)
        entries = [(name, entry.fields()) for name, entry in sorted(manifest.entries.items()) if _under(name, scope)]
        return _digest(("plain", manifest.truncated, entries))


def changed_since(project: Path, commit: str) -> list[str] | None:
    """List committed and current dirty paths, relative to the project rather than the Git root."""
    try:
        prefix = _git_prefix(project)
        revision = _git(project, "rev-parse", "--verify", "--quiet", "--end-of-options", commit + "^{commit}", allow_missing=True)
        if revision is None:
            return None
        _check_git_config(project)
        head = _head(project)
        names = {name for _, paths in _status(project, prefix, ()) for name in paths}
        if head and head != revision.strip():
            diff = _git(project, "diff", "--name-only", "-z", "--relative", "--no-renames", "--no-ext-diff",
                        "--no-textconv", revision.strip(), head, "--", ".")
            names.update(name for name in (diff or "").split("\0") if name)
        return sorted(names)
    except (_GitError, OSError):
        return None
