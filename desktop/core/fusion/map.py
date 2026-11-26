"""Project map drafts and their portable, human-editable representation."""
from __future__ import annotations

import csv
import json
import os
import re
import stat
import tempfile
import threading
import tomllib
from collections import deque
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, replace
from fnmatch import fnmatchcase
from pathlib import Path
from weakref import WeakValueDictionary

from desktop.core.harness.loop import clip_tokens
from desktop.core.plugin_lock import lock_file

from .drive.filter import scrub
from .inject import ENTRY_TOKENS

STATUSES = ("unverified", "verified", "review")
_FILENAME = ".witty_map.md"
_TRAITS = "工程特点"
_FIELDS = ("路径", "职责", "实现方法")
_SECTION = re.compile(r"^ {0,3}##[ \t]+(.+?)\s*$")
_FIELD = re.compile(r"^ {0,3}-\s*(路径|职责|实现方法)\s*[：:] ?(.*)$")
_DRIVE = re.compile(r"[A-Za-z]:")
_SKIP = frozenset({"node_modules", "__pycache__", "venv", "dist", "build"})
_SOURCE = frozenset(".py .pyi .js .jsx .ts .tsx .mjs .cjs .mts .cts .vue .svelte .go .rs .c .h .cpp .cc .cxx .hpp "
                    ".java .kt .cs .rb .php .swift .scala .sh .ps1 .lua .ex .exs .erl .hs .dart .r .sql .html .css .scss".split())
_SCAN_LIMIT = 2_000
_DEF_LIMIT = 256 * 1024
_FILE_LOCKS: WeakValueDictionary[str, threading.Lock] = WeakValueDictionary()
_FILE_LOCKS_GUARD = threading.Lock()


@dataclass(frozen=True, slots=True)
class MapEntry:
    module: str
    paths: tuple[str, ...]
    role: str = ""
    method: str = ""


@dataclass(frozen=True, slots=True)
class ProjectMap:
    entries: tuple[MapEntry, ...]
    traits: str = ""


def _relative(path: str) -> str:
    parts = path.strip(" \t").replace("\\", "/").split("/")
    if (not parts[0] or any(part.rstrip(" ") == ".." or _DRIVE.match(part) for part in parts)
            or any(char in path for char in "\r\n\x00")):
        raise ValueError("Map paths must be project-relative and cannot traverse parents.")
    return "/".join(part for part in parts if part not in ("", ".")) or "."


def _skipped(name: str) -> bool:
    return name.startswith(".") or name.lower() in _SKIP


def _linked(info: os.stat_result) -> bool:
    return stat.S_ISLNK(info.st_mode) or bool(getattr(info, "st_file_attributes", 0) & stat.FILE_ATTRIBUTE_REPARSE_POINT)


def _mode(path: Path) -> int:
    try:
        info = path.lstat()
        return 0 if _linked(info) else info.st_mode
    except OSError:
        return 0


def _definition(project: Path, name: str) -> str:
    path = project / name
    if path.parent != project and not stat.S_ISDIR(_mode(path.parent)):
        return ""
    if not stat.S_ISREG(_mode(path)):
        return ""
    try:
        with path.open("rb") as file:
            data = file.read(_DEF_LIMIT + 1)
        return data.decode("utf-8-sig") if len(data) <= _DEF_LIMIT else ""
    except (OSError, UnicodeError):
        return ""


class _Scan:
    def __init__(self, project: Path) -> None:
        self.project = project
        self.left = _SCAN_LIMIT
        self.cache: dict[str, tuple[tuple[str, bool], ...]] = {}

    def children(self, directory: str) -> tuple[tuple[str, bool], ...]:
        """Return sorted (name, is_dir) pairs; never keep an order-dependent partial directory."""
        if directory in self.cache:
            return self.cache[directory]
        found: list[tuple[str, bool]] = []
        if self.left:
            try:
                with os.scandir(self.project / directory) as entries:
                    for entry in entries:
                        if not self.left:
                            found.clear()
                            break
                        self.left -= 1
                        if _skipped(entry.name):
                            continue
                        try:
                            info = entry.stat(follow_symlinks=False)
                        except OSError:
                            continue
                        if not _linked(info) and (stat.S_ISDIR(info.st_mode) or stat.S_ISREG(info.st_mode)):
                            found.append((entry.name, stat.S_ISDIR(info.st_mode)))
            except OSError:
                found.clear()
        self.cache[directory] = tuple(sorted(found))
        return self.cache[directory]

    def folders(self) -> tuple[str, ...]:
        return tuple(name for name, is_dir in self.children(".") if is_dir)

    def glob_dirs(self, pattern: str) -> set[str]:
        pending = [(".", tuple(pattern.split("/")))]
        seen: set[tuple[str, tuple[str, ...]]] = set()
        matches: set[str] = set()
        while pending:
            directory, parts = state = pending.pop()
            if state in seen:
                continue
            seen.add(state)
            if not parts:
                if directory != ".":
                    matches.add(directory)
                continue
            head, tail = parts[0], parts[1:]
            if head == "**":
                pending.append((directory, tail))
            for name, is_dir in reversed(self.children(directory)):
                if is_dir and fnmatchcase(name, head):
                    child = name if directory == "." else f"{directory}/{name}"
                    pending.append((child, parts if head == "**" else tail))
        return matches


def _inside(project: Path, path: str, *, existing_dir: bool = False) -> bool:
    """Reject linked ancestors; a CODEOWNERS partition may name a not-yet-created path."""
    parts = path.split("/")
    if any(map(_skipped, parts)):
        return False
    for i, part in enumerate(parts):
        project /= part
        try:
            info = project.lstat()
        except FileNotFoundError:
            return not existing_dir
        except OSError:
            return False
        if _linked(info) or ((existing_dir or i < len(parts) - 1) and not stat.S_ISDIR(info.st_mode)):
            return False
    return True


def _owners(project: Path) -> set[str]:
    for file in ("CODEOWNERS", ".github/CODEOWNERS", "docs/CODEOWNERS"):
        paths: set[str] = set()
        for line in _definition(project, file).splitlines():
            fields = line.split(maxsplit=1)
            if len(fields) != 2 or fields[0].startswith(("#", "!")):
                continue
            try:
                pattern = _relative(fields[0].removeprefix("/"))
            except ValueError:
                continue
            fixed = []
            for part in pattern.split("/"):
                if any(char in part for char in "*?["):
                    break
                fixed.append(part)
            path = "/".join(fixed)
            if path and _inside(project, path):
                paths.add(path)
        if paths:
            return paths
    return set()


def _workspaces(project: Path, scan: _Scan) -> set[str]:
    try:
        manifest = json.loads(_definition(project, "package.json"))
    except (ValueError, RecursionError):
        return set()
    workspaces = manifest.get("workspaces") if isinstance(manifest, dict) else None
    if isinstance(workspaces, dict):
        workspaces = workspaces.get("packages")
    included: set[str] = set()
    if isinstance(workspaces, list):
        for value in workspaces:
            if not isinstance(value, str):
                continue
            try:
                pattern = _relative(value.removeprefix("!"))
            except ValueError:
                continue
            matches = scan.glob_dirs(pattern)
            if value.startswith("!"):
                included.difference_update(matches)
            else:
                included.update(matches)
    return included


def _python_packages(project: Path, scan: _Scan) -> set[str]:
    paths = {name for name in scan.folders() if stat.S_ISREG(_mode(project / name / "__init__.py"))}
    try:
        manifest = tomllib.loads(_definition(project, "pyproject.toml"))
    except (ValueError, RecursionError):
        return paths
    tool = manifest.get("tool")
    tool = tool if isinstance(tool, dict) else {}
    setuptools, poetry = tool.get("setuptools"), tool.get("poetry")
    candidates = []
    if isinstance(setuptools, dict) and isinstance(packages := setuptools.get("packages"), list):
        roots = setuptools.get("package-dir")
        roots = roots if isinstance(roots, dict) else {}
        for package in packages:
            if isinstance(package, str):
                prefix = next((name for name in sorted(roots, key=len, reverse=True)
                               if not name or package == name or package.startswith(name + ".")), "")
                root = roots.get(prefix, ".")
                if isinstance(root, str):
                    suffix = package[len(prefix):].lstrip(".").replace(".", "/")
                    candidates.append(f"{root}/{suffix}" if suffix else root)
    if isinstance(poetry, dict) and isinstance(packages := poetry.get("packages"), list):
        for package in packages:
            if isinstance(package, dict) and isinstance(name := package.get("include"), str):
                root = package.get("from", ".")
                if isinstance(root, str):
                    candidates.append(f"{root}/{name}")
    if isinstance(metadata := manifest.get("project"), dict) and isinstance(name := metadata.get("name"), str):
        package = name.replace("-", "_")
        if package.isidentifier():
            candidates.extend((package, f"src/{package}"))
    for candidate in candidates:
        try:
            path = _relative(candidate)
        except ValueError:
            continue
        if _inside(project, path, existing_dir=True):
            paths.add(path)
    return paths


def _source_folders(scan: _Scan) -> set[str]:
    found: set[str] = set()
    pending = deque((name, name) for name in scan.folders())
    while pending:
        root, folder = pending.popleft()
        if root in found:
            continue
        for name, is_dir in scan.children(folder):
            if is_dir:
                pending.append((root, f"{folder}/{name}"))
            elif Path(name).suffix.lower() in _SOURCE:
                found.add(root)
                break
    return found


def initial(project: Path) -> ProjectMap:
    """Draft from ownership, then packages, then source folders; do not read source bodies."""
    if not stat.S_ISDIR(_mode(project)):
        return ProjectMap(())
    scan = _Scan(project)
    paths = _owners(project) or (_workspaces(project, scan) | _python_packages(project, scan)) or _source_folders(scan)
    return ProjectMap(tuple(MapEntry(path, (path,)) for path in sorted(paths)))


def digest(project_map: ProjectMap) -> str:
    """Summarize the partitions for naming and adjustment by the main agent."""
    if not project_map.entries:
        return "Project map draft: no modules found."
    return "Project map draft (module: paths):\n" + "\n".join(
        f"- {entry.module}: {', '.join(entry.paths) or '(no paths)'}" for entry in project_map.entries
    )


def _path_field(path: str) -> str:
    path = _relative(path)
    if any(char in path for char in ',"'):
        return '"' + path.replace('"', '""') + '"'
    return path


def render(project_map: ProjectMap) -> str:
    """Write the fixed Markdown format without machine fields or local paths."""
    sections = ["# 项目地图\n由 WittyCode 维护，也可手动编辑。"]
    for entry in project_map.entries:
        if not entry.module.strip() or entry.module.strip() == _TRAITS or any(c in entry.module for c in "\r\n"):
            raise ValueError("Invalid map module name.")
        fields = (", ".join(map(_path_field, entry.paths)), entry.role, entry.method)
        sections.append(f"## {entry.module}\n" + "\n".join(
            f"- {label}：" + value.replace("\n", "\n    ") for label, value in zip(_FIELDS, fields)
        ))
    return "\n\n".join([*sections, f"## {_TRAITS}\n{project_map.traits}"]) + "\n"


def _parse_paths(value: str) -> tuple[str, ...]:
    paths = []
    try:
        for path in next(csv.reader([value], skipinitialspace=True)):
            try:
                paths.append(_relative(path.strip()))
            except ValueError:
                continue
    except csv.Error:
        pass
    return tuple(paths)


def parse(text: str) -> ProjectMap:
    """Read recognizable sections, ignoring unknown lines and non-portable path items."""
    entries: list[MapEntry] = []
    traits = module = active = ""
    body: dict[str, str] = {}
    lines = text.splitlines()
    for i, line in enumerate(lines):
        if heading := _SECTION.match(line):
            if module:
                entries.append(MapEntry(module, _parse_paths(body.get("路径", "")), body.get("职责", ""), body.get("实现方法", "")))
            module, active, body = heading[1].strip(), "", {}
            if module == _TRAITS:
                traits = "\n".join(lines[i + 1:]).strip("\n")
                return ProjectMap(tuple(entries), traits)
        elif module:
            if field := _FIELD.match(line):
                active, body[field[1]] = field[1], field[2]
            elif active in ("职责", "实现方法") and line.startswith("    "):
                body[active] += "\n" + line[4:]
            else:
                active = ""
    if module:
        entries.append(MapEntry(module, _parse_paths(body.get("路径", "")), body.get("职责", ""), body.get("实现方法", "")))
    if not entries:
        raise ValueError("No project map section found.")
    return ProjectMap(tuple(entries), traits)


def write(project: Path, project_map: ProjectMap) -> Path:
    """Replace the project file only after a complete UTF-8 write to a closed sibling temp file."""
    text = render(project_map)
    target = project / _FILENAME
    temp: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", newline="\n", dir=project, delete=False) as file:
            temp = Path(file.name)
            file.write(text)
        os.replace(temp, target)
        temp = None
    finally:
        if temp is not None:
            temp.unlink(missing_ok=True)
    return target


def read(project: Path, *, strict: bool = False) -> ProjectMap | None:
    """Return no map for a missing file; optionally distinguish invalid or unreadable files."""
    path = project / _FILENAME
    try:
        info = path.lstat()
        if _linked(info) or not stat.S_ISREG(info.st_mode):
            raise OSError("The project map is not a regular file.")
        return parse(path.read_text(encoding="utf-8-sig"))
    except FileNotFoundError:
        return None
    except (OSError, ValueError):
        if strict:
            raise
        return None


@contextmanager
def file_lock(target: Path) -> Iterator[None]:
    """Hold the same map lock through one worker's transaction, including on Windows across profiles."""
    sidecar = target.with_suffix(".lock")
    with _FILE_LOCKS_GUARD:
        local = _FILE_LOCKS.setdefault(os.path.normcase(str(sidecar.parent.resolve() / sidecar.name)), threading.Lock())
    with local:
        try:
            info = sidecar.lstat()
        except FileNotFoundError:
            pass
        else:
            if _linked(info) or not stat.S_ISREG(info.st_mode):
                raise OSError("The project map lock is not a regular file.")
        with lock_file(target, busy_message="项目地图正在写入，稍后再试。"):
            yield


def portable_lock(project: Path):
    return file_lock(project / _FILENAME)


def _paths(paths: Sequence[str]) -> tuple[str, ...]:
    normalized = []
    for path in paths:
        try:
            normalized.append(_relative(path))
        except ValueError:
            continue
    return tuple(dict.fromkeys(normalized))


def _under(path: str, parent: str) -> bool:
    return parent == "." or path == parent or path.startswith(parent + "/")


def entries_for(project_map: ProjectMap, paths: Sequence[str]) -> tuple[MapEntry, ...]:
    """Return overlapping modules in map order, matching whole path components."""
    wanted = _paths(paths)
    return tuple(entry for entry in project_map.entries if any(
        _under(path, base) or _under(base, path) for base in _paths(entry.paths) for path in wanted
    ))


def apply_result(current: ProjectMap, paths: Sequence[str], goal: str, summary: str) -> ProjectMap:
    """Describe the finished scope without replacing descriptions for wider or unrelated modules."""
    wanted = _paths(paths)
    if not wanted or "." in wanted:
        return current
    role_data, method_data = map(scrub, (goal, summary))
    role, method = (clip_tokens(" ".join(data.text.split()), ENTRY_TOKENS) for data in (role_data, method_data))
    if role_data.suspect or method_data.suspect or not method:
        return current
    scope = {os.path.normcase(path) for path in wanted}
    entries = list(current.entries)
    for i, entry in enumerate(entries):
        if {os.path.normcase(path) for path in entry.paths} == scope:
            entries[i] = replace(entry, role=role or entry.role, method=method)
            return replace(current, entries=tuple(entries))
    # Plan unit IDs and workstream names can repeat across plans. Key a new entry by its actual scope.
    base = ", ".join(wanted)
    module, suffix = base, 1
    existing = {entry.module for entry in entries}
    while module in existing:
        suffix += 1
        module = f"{base} ({suffix})"
    return replace(current, entries=(*entries, MapEntry(module, wanted, role, method)))


def update(project_map: ProjectMap, module: str, *, paths: Sequence[str] | None = None, role: str | None = None,
           method: str | None = None, traits: str | None = None) -> ProjectMap:
    """Replace just the named module. ``traits`` belong to the project, not to a module: given beside a module's fields
    they replace the project-wide traits in the same call, because that is how a model fills the tool in."""
    if module in ("traits", _TRAITS):
        if traits is None or any(value is not None for value in (paths, role, method)):
            raise ValueError("Provide only traits for the project-wide traits entry.")
        return replace(project_map, traits=traits)
    if traits is not None:
        project_map = replace(project_map, traits=traits)
    if paths is None and role is None and method is None:
        return project_map  # (traits alone under a module's name: nothing of the module changes, and none is added)
    old = next((entry for entry in project_map.entries if entry.module == module), None)
    base = old or MapEntry(module, ())
    entry = replace(base, paths=tuple(dict.fromkeys(map(_relative, paths))) if paths is not None else base.paths,
                    role=base.role if role is None else role, method=base.method if method is None else method)
    entries = (tuple(entry if item is old else item for item in project_map.entries) if old is not None
               else (*project_map.entries, entry))
    return replace(project_map, entries=entries)


def needs_review(entry: MapEntry, changed: Sequence[str]) -> bool:
    """A change affects an entry when it lies inside one of the entry's paths."""
    bases = _paths(entry.paths)
    return any(_under(path, base) for path in _paths(changed) for base in bases)


def entry_text(entry: MapEntry) -> str:
    """Project facts only, sized for one L0 entry."""
    facts = [f"Module: {entry.module}", f"Paths: {', '.join(entry.paths)}"]
    if entry.role:
        facts.append(f"Role: {entry.role}")
    if entry.method:
        facts.append(f"Method: {entry.method}")
    return clip_tokens("\n".join(facts), ENTRY_TOKENS)


def traits_text(project_map: ProjectMap) -> str:
    """Project-wide engineering traits, sized for one L0 entry."""
    return clip_tokens(f"Engineering traits: {project_map.traits}", ENTRY_TOKENS) if project_map.traits else ""
