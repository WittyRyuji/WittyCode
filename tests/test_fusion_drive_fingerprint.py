"""Fusion Drive fingerprints distinguish content, not just Git status letters."""
import hashlib
import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest

from desktop.core.fusion.drive import fingerprint as stamps
from desktop.core.fusion.drive.fingerprint import Manifest, changed_since, fingerprint, workspace_kind

GIT = shutil.which("git")
needs_git = pytest.mark.skipif(GIT is None, reason="Git is not installed")


@pytest.fixture(autouse=True)
def isolated_git(tmp_path, monkeypatch):
    for name in ("GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE", "GIT_COMMON_DIR", "GIT_OBJECT_DIRECTORY",
                 "GIT_ALTERNATE_OBJECT_DIRECTORIES", "GIT_TEMPLATE_DIR", "GIT_CONFIG_COUNT", "GIT_CONFIG_PARAMETERS"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", os.devnull)
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    monkeypatch.setenv("GIT_CEILING_DIRECTORIES", str(tmp_path.parent))


def _git(project: Path, *args: str) -> str:
    flags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
    result = subprocess.run([GIT, "-c", "user.name=Test", "-c", "user.email=test@example.com",
                             "-c", "core.autocrlf=false", *args],
                            cwd=project, stdin=subprocess.DEVNULL, capture_output=True, encoding="utf-8",
                            errors="replace", timeout=10, creationflags=flags)
    assert result.returncode == 0, result.stderr
    return result.stdout.strip()


def _put(project: Path, name: str, data: bytes = b"initial\n") -> Path:
    path = project / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    return path


@needs_git
def test_git_fingerprint_tracks_edits_commits_and_only_requested_paths(tmp_path):
    _git(tmp_path, "init", "-q")
    tracked = _put(tmp_path, "src/main.txt")
    outside = _put(tmp_path, "src-old/main.txt")
    _put(tmp_path, ".gitignore", b"*.ignored\n")
    _git(tmp_path, "add", ".")
    _git(tmp_path, "commit", "-qm", "Initial")
    assert workspace_kind(tmp_path) == workspace_kind(tracked.parent) == "git"

    initial = fingerprint(tmp_path)
    scoped = fingerprint(tmp_path, ("src",))
    assert re.fullmatch(r"[0-9a-f]{64}", initial)
    assert fingerprint(tmp_path) == initial
    assert fingerprint(tmp_path, ("src/",)) == scoped
    assert fingerprint(tmp_path, (".",)) == initial
    tracked.write_bytes(b"first\n")
    first = fingerprint(tmp_path, ("src",))
    tracked.write_bytes(b"other\n")
    assert len({first, scoped, fingerprint(tmp_path, ("src",))}) == 3
    tracked.write_bytes(b"initial\n")
    assert fingerprint(tmp_path) == initial
    assert fingerprint(tmp_path, ("src",)) == scoped

    _put(tmp_path, "src/new.ignored")
    assert fingerprint(tmp_path) == initial
    outside.write_bytes(b"outside\n")
    _put(tmp_path, "new.txt")
    assert fingerprint(tmp_path) != initial
    assert fingerprint(tmp_path, ("src",)) == scoped
    _git(tmp_path, "add", "new.txt")
    _git(tmp_path, "commit", "-qm", "Add a file outside the scope")
    assert fingerprint(tmp_path, ("src",)) != scoped  # HEAD is global.


@needs_git
def test_unborn_repo_and_literal_pathspec(tmp_path):
    _git(tmp_path, "init", "-q")
    assert workspace_kind(tmp_path) == "git"
    empty = fingerprint(tmp_path)
    assert changed_since(tmp_path, "HEAD") is None
    _put(tmp_path, "x.txt")
    assert fingerprint(tmp_path, ("[x].txt",)) == empty
    named = _put(tmp_path, "[x].txt")
    untracked = fingerprint(tmp_path, ("[x].txt",))
    assert untracked != empty
    named.write_bytes(b"a different untracked edit\n")
    edited = fingerprint(tmp_path, ("[x].txt",))
    assert edited != untracked
    _git(tmp_path, "add", "--", "[x].txt")
    staged = fingerprint(tmp_path, ("[x].txt",))
    assert staged != edited
    assert fingerprint(tmp_path, ("[x].txt",)) == staged


@needs_git
def test_git_fingerprint_includes_index_content_without_locking(tmp_path):
    _git(tmp_path, "init", "-q")
    file = _put(tmp_path, "tracked.txt")
    _git(tmp_path, "add", ".")
    _git(tmp_path, "commit", "-qm", "Initial")
    file.write_bytes(b"staged version 1\n")
    _git(tmp_path, "add", "tracked.txt")
    file.write_bytes(b"same worktree bytes\n")
    first = fingerprint(tmp_path)
    file.write_bytes(b"staged version 2\n")
    _git(tmp_path, "add", "tracked.txt")
    file.write_bytes(b"same worktree bytes\n")
    index = tmp_path / ".git/index"
    before = (index.read_bytes(), index.stat().st_mtime_ns)
    lock = _put(tmp_path, ".git/index.lock", b"held by another reader\n")
    second = fingerprint(tmp_path)
    assert second != first
    assert second == fingerprint(tmp_path)
    assert workspace_kind(tmp_path) == "git"
    assert (index.read_bytes(), index.stat().st_mtime_ns) == before
    assert lock.read_bytes() == b"held by another reader\n"


@needs_git
def test_changed_since_includes_renames_and_uses_project_relative_names(tmp_path):
    _git(tmp_path, "init", "-q")
    committed = _put(tmp_path, "sub/committed.txt")
    dirty = _put(tmp_path, "sub/dirty.txt")
    renamed = _put(tmp_path, "sub/old name.txt")
    removed = _put(tmp_path, "sub/removed.txt")
    _put(tmp_path, ".gitignore", b"*.ignored\n")
    _git(tmp_path, "add", ".")
    _git(tmp_path, "commit", "-qm", "Initial")
    base = _git(tmp_path, "rev-parse", "HEAD")
    assert changed_since(tmp_path, base) == []
    project = committed.parent
    committed.write_bytes(b"now committed\n")
    _put(tmp_path, "root.txt")
    _git(tmp_path, "add", ".")
    _git(tmp_path, "commit", "-qm", "Change committed files")
    clean = fingerprint(project)
    _put(tmp_path, "root-untracked.txt")
    assert fingerprint(project) == clean

    dirty.write_bytes(b"not yet committed\n")
    removed.unlink()
    _git(tmp_path, "mv", renamed.relative_to(tmp_path).as_posix(), "sub/new name.txt")
    _put(project, "新.txt")
    _put(project, "file.ignored")
    assert changed_since(project, base) == sorted(["committed.txt", "dirty.txt", "new name.txt", "old name.txt",
                                                  "removed.txt", "新.txt"])
    assert changed_since(project, "HEAD") == sorted(["dirty.txt", "new name.txt", "old name.txt", "removed.txt", "新.txt"])
    assert changed_since(project, "unknown-commit") is None
    assert changed_since(project, "--all") is None
    assert fingerprint(project) != clean


@needs_git
def test_untracked_caches_and_build_output_are_not_changes_in_a_git_project(tmp_path):
    # Found with a real model: the caches a test run left counted as changes outside an expert's declared paths.
    _git(tmp_path, "init", "-q")
    _put(tmp_path, "src/main.py")
    tracked = _put(tmp_path, "build/release.py")
    _git(tmp_path, "add", ".")
    _git(tmp_path, "commit", "-qm", "Initial")
    base = _git(tmp_path, "rev-parse", "HEAD")
    clean, scoped = fingerprint(tmp_path), fingerprint(tmp_path, ("src",))
    for name in ("node_modules", "__pycache__", ".venv", "venv", "dist", "build", ".mypy_cache", ".pytest_cache"):
        _put(tmp_path, f"src/{name}/made.bin")
    _put(tmp_path, "__PyCache__/main.cpython-311.pyc")
    assert changed_since(tmp_path, base) == []
    assert fingerprint(tmp_path) == clean and fingerprint(tmp_path, ("src",)) == scoped
    # A tracked file is the project's work wherever it lies, and so is a new file anywhere else.
    tracked.write_bytes(b"changed\n")
    _put(tmp_path, "src/new.py")
    assert changed_since(tmp_path, base) == ["build/release.py", "src/new.py"]
    assert fingerprint(tmp_path) != clean and fingerprint(tmp_path, ("src",)) != scoped


def test_plain_manifest_fields_and_skipped_folders(tmp_path):
    kept = _put(tmp_path, "sub/kept.txt")
    for name in (".git", "node_modules", "__pycache__", ".venv", "venv", "dist", "build", ".mypy_cache", ".pytest_cache"):
        _put(tmp_path, f"sub/{name}/ignored.txt")
    assert workspace_kind(tmp_path) == "plain"
    assert changed_since(tmp_path, "HEAD") is None
    manifest = Manifest.scan(tmp_path)
    assert not manifest.truncated
    assert list(manifest.entries) == ["sub/kept.txt"]
    entry = manifest.entries["sub/kept.txt"]
    assert entry.size == kept.stat().st_size
    assert entry.mtime_ns == kept.stat().st_mtime_ns
    assert entry.sha256 == hashlib.sha256(kept.read_bytes()).hexdigest()
    assert Manifest.from_json(manifest.to_json()) == manifest


def test_manifest_does_not_follow_symlinks(tmp_path):
    parent = tmp_path / "project"
    parent.mkdir()
    target = _put(tmp_path, "elsewhere/file.txt")
    try:
        (parent / "file-link").symlink_to(target)
        (parent / "dir-link").symlink_to(target.parent, target_is_directory=True)
    except OSError as error:
        pytest.skip(f"Symlinks unavailable: {error.strerror}")
    assert Manifest.scan(parent).entries == {}


def test_manifest_skips_windows_reparse_point_entries(tmp_path, monkeypatch):
    marked = _put(tmp_path, "junction/file.txt").parent
    real_lstat = Path.lstat

    def lstat(path):
        info = real_lstat(path)
        if path != marked:
            return info
        return type("ReparsePoint", (), {"st_mode": info.st_mode, "st_file_attributes": 0x400})()

    monkeypatch.setattr(Path, "lstat", lstat)
    assert Manifest.scan(tmp_path).entries == {}


def test_manifest_reuses_unchanged_metadata_without_reading(tmp_path):
    path = _put(tmp_path, "file.txt", b"abc")
    old = Manifest.scan(tmp_path)
    info = path.stat()
    path.write_bytes(b"xyz")
    os.utime(path, ns=(info.st_atime_ns, info.st_mtime_ns))
    assert path.stat().st_mtime_ns == info.st_mtime_ns
    assert Manifest.scan(tmp_path, previous=old) == old
    assert Manifest.scan(tmp_path) != old


def test_plain_fingerprint_scope_and_changed_paths(tmp_path):
    _put(tmp_path, "src/a.txt")
    kept = _put(tmp_path, "src/b.txt")
    deleted = _put(tmp_path, "src-old/deleted.txt")
    old = Manifest.scan(tmp_path)
    scoped = fingerprint(tmp_path, ("./src/",), manifest=old)
    deleted.unlink()
    _put(tmp_path, "src-old/added.txt")
    outside = Manifest.scan(tmp_path, previous=old)
    assert fingerprint(tmp_path, ("src",), manifest=outside) == scoped
    assert fingerprint(tmp_path, manifest=outside) != fingerprint(tmp_path, manifest=old)
    kept.write_bytes(b"longer replacement\n")
    new = Manifest.scan(tmp_path, previous=outside)
    assert Manifest.changed(old, new) == ["src-old/added.txt", "src-old/deleted.txt", "src/b.txt"]
    assert fingerprint(tmp_path, ("src",)) != scoped
    assert fingerprint(tmp_path, ("src",), manifest=old) == scoped


def test_manifest_caps_file_count_and_large_files_use_metadata(tmp_path, monkeypatch):
    monkeypatch.setattr(stamps, "_MAX_FILES", 2)
    path = _put(tmp_path, "large.bin", b"")
    with path.open("r+b") as file:
        file.truncate(8 * 1024 * 1024 + 1)
    _put(tmp_path, "small.txt")
    full = Manifest.scan(tmp_path)
    assert not full.truncated
    assert full.entries["large.bin"].size == 8 * 1024 * 1024 + 1
    assert full.entries["large.bin"].sha256 is None
    assert full.entries["small.txt"].sha256 is not None
    _put(tmp_path, "z.txt")
    capped = Manifest.scan(tmp_path, previous=full)
    assert capped.truncated and capped.entries == full.entries
    assert Manifest.from_json(capped.to_json()) == capped


@pytest.mark.parametrize("path", ["/src", "\\root", "C:/src", "C:src", "src/C:file", "../src", "src\\..\\dst", "a/../b", "\0"])
def test_invalid_workspace_paths_are_rejected(tmp_path, path):
    with pytest.raises(ValueError):
        fingerprint(tmp_path, (path,))


@pytest.mark.parametrize("text", ["{", "[]", '{"entries":{},"truncated":1}', '{"entries":{"../file":[1,0,null]},"truncated":false}',
                                  '{"entries":{"file":[-1,0,null]},"truncated":false}',
                                  '{"entries":{"file":[1,0,"no hash"]},"truncated":false}'])
def test_damaged_manifest_is_rejected(text):
    with pytest.raises(ValueError):
        Manifest.from_json(text)


def test_git_missing_and_timeout_use_plain_manifest(tmp_path, monkeypatch):
    _put(tmp_path, "file.txt")
    manifest = Manifest.scan(tmp_path)
    monkeypatch.setattr(shutil, "which", lambda name: None)
    plain = fingerprint(tmp_path, manifest=manifest)
    assert workspace_kind(tmp_path) == "plain"
    assert changed_since(tmp_path, "HEAD") is None

    def timeout(argv, **kwargs):
        assert isinstance(argv, list)
        assert kwargs["cwd"] == tmp_path
        assert kwargs["stdin"] == subprocess.DEVNULL
        assert kwargs["timeout"] == 10
        assert kwargs["env"]["GIT_OPTIONAL_LOCKS"] == "0"
        if os.name == "nt":
            assert kwargs["creationflags"] == subprocess.CREATE_NO_WINDOW
        raise subprocess.TimeoutExpired(argv, kwargs["timeout"])

    monkeypatch.setattr(shutil, "which", lambda name: "git")
    monkeypatch.setattr(subprocess, "run", timeout)
    assert workspace_kind(tmp_path) == "plain"
    assert changed_since(tmp_path, "HEAD") is None
    assert fingerprint(tmp_path, manifest=manifest) == plain


@needs_git
def test_git_status_failure_falls_back_after_kind_detection(tmp_path, monkeypatch):
    _git(tmp_path, "init", "-q")
    _put(tmp_path, "file.txt")
    _git(tmp_path, "add", ".")
    _git(tmp_path, "commit", "-qm", "Initial")
    manifest = Manifest.scan(tmp_path)
    with monkeypatch.context() as missing_git:
        missing_git.setattr(shutil, "which", lambda name: None)
        plain = fingerprint(tmp_path, manifest=manifest)
    assert fingerprint(tmp_path) != plain

    real_run = subprocess.run

    def fail_status(argv, **kwargs):
        if "status" in argv:
            return subprocess.CompletedProcess(argv, 128, stdout=b"", stderr=b"fatal: invalid index")
        return real_run(argv, **kwargs)

    monkeypatch.setattr(subprocess, "run", fail_status)
    assert workspace_kind(tmp_path) == "git"
    assert fingerprint(tmp_path, manifest=manifest) == plain
    assert changed_since(tmp_path, "HEAD") is None
