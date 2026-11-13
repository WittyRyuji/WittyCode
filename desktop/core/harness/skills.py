"""Skills and custom slash commands: workflows kept in files, for every engine.

A **skill** is a folder with a ``SKILL.md`` (front matter ``name`` and ``description``, then the instructions) and, beside it,
whatever the workflow needs. A **custom command** is a Markdown file in a ``commands`` folder; ``/name arguments`` runs it with
``$ARGUMENTS`` replaced. Both are found in the project and in the user's home folder, in WittyCode's own ``.wittycode`` first
and then, read only, in the folders other agent applications use (``SKILL_FOLDERS`` / ``COMMAND_FOLDERS``), so skills written
for those work here too; plugins of that layout (a folder holding ``.factory-plugin/plugin.json`` or ``.claude-plugin/plugin.json``,
with its own ``skills/`` and ``commands/``) are read as well. The project wins over the user's, which wins over a plugin's, and
the first of two skills of one name is the one that counts. A skill turned off (``enabled: false``, or ``disabledSkills`` in a
``settings.json`` of that layout) is left out; reading those settings is a leftover the Roadmap removes.

How they reach the model:

* **Any engine, by name**: a message that names a skill (``/summarize-diff focus on the migration``, or ``/summarize-diff`` as
  one word anywhere in the text) or starts with a command goes out with the instructions after it, in the block every hook uses
  (``core/context_envelope``): the page shows what was typed, saved conversations hold the block and readers strip it. This is
  ``prompt_hook``.
* **WittyCode's own loop, by the model's choice**: the instructions list the skills (name and description) and the ``skill``
  tool loads one; the folders of the skills may be read without asking (``ToolContext.readable``).

Not carried over from plugins: subagents, hooks, output styles and MCP servers.
"""

from __future__ import annotations

import json
import logging
import os
import re
from collections.abc import Iterator, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from desktop.core.hooks import PromptContext, PromptEvent

log = logging.getLogger("desktop.skills")

SKILL_FILE = "SKILL.md"
DEPTH = 4
"""How deep below a ``skills`` / ``commands`` folder a skill or a command is looked for."""
PLUGIN_DEPTH = 7
BODY_LIMIT = 60_000
"""Characters of a skill's instructions handed to the model."""
LISTED = 60
"""Skills the instructions of the own loop name; the rest are still found by their ``/name``."""
DESCRIPTION_LIMIT = 300
FILES_LISTED = 40
_SKIP = frozenset({"node_modules", ".git", "__pycache__"})

_NAME = re.compile(r"[^a-z0-9]+")
_TOKEN = re.compile(r"(?:(?<=\s)|^)/([a-z0-9][a-z0-9-]*)(?=$|\s|[,;:!?)\]}\"']|[.](?:\s|$))")
_CALL = re.compile(r"\A/([a-z0-9][a-z0-9-]*)(?:\s+(.*))?\Z", re.DOTALL)


# ---------------------------------------------------------------------------------------------------------- front matter


def _unquote(value: str) -> str:
    value = value.strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
        return value[1:-1]
    return value


def _indent(line: str) -> int:
    return len(line) - len(line.lstrip(" \t"))


def _front_matter_values(lines: Sequence[str]) -> dict[str, Any]:
    """The top-level keys of a small YAML block: plain, quoted and block (``>`` / ``|``) text, ``[a, b]`` and ``- a`` lists. A
    nested mapping (``metadata:``) is skipped: nothing here reads it."""
    data: dict[str, Any] = {}
    index = 0
    while index < len(lines):
        line = lines[index]
        index += 1
        found = re.match(r"^([A-Za-z0-9_-]+):[ \t]*(.*)$", line)
        if found is None:  # (also every indented line: the pattern starts at the left edge)
            continue
        key, rest = found.group(1), found.group(2).strip()
        block: list[str] = []
        while index < len(lines) and (not lines[index].strip() or _indent(lines[index]) or (not rest and lines[index].startswith("- "))):
            block.append(lines[index])
            index += 1
        while block and not block[-1].strip():
            block.pop()
        if rest[:1] in (">", "|"):
            text = [part.strip() for part in block]
            data[key] = (" " if rest[0] == ">" else "\n").join(part for part in text if part or rest[0] == "|").strip()
        elif rest.startswith("["):
            data[key] = [_unquote(part) for part in rest.strip("[]").split(",") if part.strip()]
        elif rest:
            continuation = [part.strip() for part in block if part.strip() and not part.strip().startswith("- ")]
            data[key] = _unquote(" ".join([rest, *continuation]) if continuation else rest)
        else:
            items = [_unquote(part.strip()[2:]) for part in block if part.strip().startswith("- ")]
            if items:
                data[key] = items
    return data


def split_front_matter(text: str) -> tuple[dict[str, Any], str]:
    """``(front matter, the rest)`` of a Markdown file; no front matter is ``({}, text)``."""
    text = text.lstrip("\ufeff")
    lines = text.splitlines()
    if not lines or lines[0].strip() != "---":
        return {}, text
    end = next((i for i in range(1, len(lines)) if lines[i].strip() in ("---", "...")), None)
    if end is None:
        return {}, text
    return _front_matter_values(lines[1:end]), "\n".join(lines[end + 1 :]).lstrip("\n")


def _flag(value: Any, default: bool) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str) and value.strip().lower() in ("true", "false"):
        return value.strip().lower() == "true"
    return default


def slug(text: str) -> str:
    """A name as it is typed after the slash: lower case, letters and digits, single hyphens."""
    return _NAME.sub("-", text.lower()).strip("-")


def _read(path: Path, limit: int = 400_000) -> str | None:
    try:
        with path.open("r", encoding="utf-8", errors="replace") as handle:
            return handle.read(limit)
    except OSError:
        return None


# ---------------------------------------------------------------------------------------------------------- where they live


@dataclass(frozen=True, slots=True)
class Source:
    label: str
    """``project`` | ``personal`` | ``plugin`` (shown to the user)."""
    folder: Path
    plugin: Path | None = None
    """The root of the plugin the folder belongs to (``${DROID_PLUGIN_ROOT}`` in its files)."""


SKILL_FOLDERS = (".wittycode", ".agents", ".agent", ".codex", ".claude", ".factory")
"""The folders a project or the user keeps skills in: WittyCode's own first, then the ones other agent applications use, so
skills written for those work here too (they are only read)."""
COMMAND_FOLDERS = (".wittycode", ".factory")
"""The same for custom commands (``commands/*.md``)."""

USER_HOME: Path | None = None
"""Whose folders count as the user's (``~/.factory`` …): the real home folder unless a test says otherwise."""


def _home(home: Path | None) -> Path:
    return home if home is not None else (USER_HOME or Path.home())


def _repository_folders(project: Path, home: Path) -> list[Path]:
    """The project folder and its parents up to the repository's root (the folder with a ``.git``), else up to the home folder."""
    found: list[Path] = []
    folder = project
    real = Path.home()
    for _ in range(12):
        if folder not in (home, real):  # (a home folder's own ``.factory`` is the user's, listed as such)
            found.append(folder)
        if (folder / ".git").exists() or folder in (home, real) or folder.parent == folder:
            break
        folder = folder.parent
    return found


def _settings(path: Path) -> Mapping[str, Any]:
    text = _read(path, 2_000_000)
    if text is None:
        return {}
    try:
        data = json.loads(text)
    except ValueError:
        return {}
    return data if isinstance(data, Mapping) else {}


def _disabled(project: Path, home: Path) -> set[str]:
    names: set[str] = set()
    for path in (home / ".factory" / "settings.json", project / ".factory" / "settings.json"):
        listed = _settings(path).get("disabledSkills")
        if isinstance(listed, list):
            names |= {slug(str(item)) for item in listed}
    return names


def _plugins(home: Path) -> list[Path]:
    """The installed plugins' roots: a folder that holds ``.factory-plugin/plugin.json`` or ``.claude-plugin/plugin.json``, below
    ``~/.factory/plugins``. A plugin ``enabledPlugins`` turns off (any ``name@marketplace`` of its name set to false) is left out."""
    base = home / ".factory" / "plugins"
    enabled = _settings(home / ".factory" / "settings.json").get("enabledPlugins")
    switched = enabled if isinstance(enabled, Mapping) else {}
    roots: list[Path] = []

    def walk(folder: Path, depth: int) -> None:
        try:
            entries = [entry for entry in os.scandir(folder) if entry.is_dir(follow_symlinks=False)]
        except OSError:
            return
        names = {entry.name for entry in entries}
        if ".factory-plugin" in names or ".claude-plugin" in names:
            roots.append(folder)
            return
        if depth >= PLUGIN_DEPTH:
            return
        for entry in sorted(entries, key=lambda e: e.name):
            if entry.name not in _SKIP:
                walk(Path(entry.path), depth + 1)

    if base.is_dir():
        walk(base, 0)
    kept = []
    for root in roots:
        name = ""
        for meta in (root / ".factory-plugin" / "plugin.json", root / ".claude-plugin" / "plugin.json"):
            data = _settings(meta)
            if isinstance(data.get("name"), str):
                name = data["name"]
                break
        name = name or root.name
        if any(key.split("@")[0] == name and value is False for key, value in switched.items() if isinstance(key, str)):
            continue
        kept.append(root)
    return kept


def skill_sources(project: Path, home: Path | None = None) -> list[Source]:
    """Where skills are looked for, the one that wins first."""
    home = _home(home)
    out: list[Source] = []
    for folder in _repository_folders(project, home):
        out += [Source("project", folder / part / "skills") for part in SKILL_FOLDERS]
    for part in SKILL_FOLDERS:
        out.append(Source("personal", home / part / "skills"))
    from desktop.core.connections import ConnectionStore
    out += ConnectionStore(home=home).skill_sources(project)
    out += [Source("plugin", root / "skills", root) for root in _plugins(home)]
    return out


def command_sources(project: Path, home: Path | None = None) -> list[Source]:
    home = _home(home)
    out = [Source("project", folder / part / "commands") for folder in _repository_folders(project, home) for part in COMMAND_FOLDERS]
    out += [Source("personal", home / part / "commands") for part in COMMAND_FOLDERS]
    out += [Source("plugin", root / "commands", root) for root in _plugins(home)]
    return out


def _expand_root(text: str, plugin: Path | None) -> str:
    if plugin is None:
        return text
    for variable in ("DROID_PLUGIN_ROOT", "CLAUDE_PLUGIN_ROOT"):
        text = text.replace("${" + variable + "}", str(plugin)).replace("$" + variable, str(plugin))
    return text


# ---------------------------------------------------------------------------------------------------------- skills


@dataclass(frozen=True, slots=True)
class Skill:
    name: str
    description: str
    path: Path
    """The ``SKILL.md``."""
    source: str
    user_invocable: bool = True
    model_invocable: bool = True
    plugin: Path | None = None

    @property
    def folder(self) -> Path:
        return self.path.parent

    def body(self) -> str:
        text = _read(self.path)
        if text is None:
            return ""
        return _expand_root(split_front_matter(text)[1].strip()[:BODY_LIMIT], self.plugin)

    def files(self) -> list[str]:
        """The files beside ``SKILL.md`` (relative, forward slashes), a few."""
        found: list[str] = []
        for current, folders, names in os.walk(self.folder):
            folders[:] = sorted(name for name in folders if name not in _SKIP)
            for name in sorted(names):
                relative = (Path(current) / name).relative_to(self.folder).as_posix()
                if relative != SKILL_FILE:
                    found.append(relative)
                if len(found) >= FILES_LISTED:
                    return found
        return found


def _walk_for(folder: Path, marker: str) -> Iterator[Path]:
    """Folders below ``folder`` that hold a file named ``marker``; the walk does not go below one."""
    stack = [(folder, 0)]
    while stack:
        current, depth = stack.pop()
        try:
            entries = sorted(os.scandir(current), key=lambda e: e.name)
        except OSError:
            continue
        if any(entry.name == marker and entry.is_file() for entry in entries):
            yield current
            continue
        if depth < DEPTH:
            stack.extend((Path(entry.path), depth + 1) for entry in reversed(entries) if entry.is_dir(follow_symlinks=False) and entry.name not in _SKIP)


def _skill_of(path: Path, source: Source) -> Skill | None:
    text = _read(path)
    if text is None:
        return None
    data, _ = split_front_matter(text)
    if not _flag(data.get("enabled"), True):
        return None
    description = " ".join(str(data.get("description") or "").split())
    if not description:
        return None
    name = slug(str(data.get("name") or path.parent.name))
    if not name:
        return None
    return Skill(
        name,
        description,
        path,
        source.label,
        user_invocable=_flag(data.get("user-invocable"), True),
        model_invocable=not _flag(data.get("disable-model-invocation"), False),
        plugin=source.plugin,
    )


def discover(project: Path | str, home: Path | None = None) -> list[Skill]:
    """The skills the conversation in ``project`` can use, the winner of each name, in the order the sources are searched."""
    project = Path(project)
    home = _home(home)
    off = _disabled(project, home)
    from desktop.core.connections import ConnectionStore
    connections = ConnectionStore(home=home)
    found: dict[str, Skill] = {}
    for source in skill_sources(project, home):
        if not source.folder.is_dir():
            continue
        for folder in _walk_for(source.folder, SKILL_FILE):
            skill = _skill_of(folder / SKILL_FILE, source)
            if skill is not None and skill.name not in found and skill.name not in off and connections.skill_allowed(skill.path, project):
                found[skill.name] = skill
    return list(found.values())


def read_roots(found: Sequence[Skill]) -> tuple[Path, ...]:
    """The folders the model may read without asking: those of the skills found, and the roots of the plugins they come from."""
    roots: list[Path] = []
    for skill in found:
        for root in (skill.folder, skill.plugin):
            if root is not None and root not in roots:
                roots.append(root)
    return tuple(roots)


def skills_prompt(skills: Sequence[Skill]) -> str:
    """The part of the own loop's instructions that tells the model which skills it has."""
    usable = [skill for skill in skills if skill.model_invocable]
    if not usable:
        return ""
    lines = [
        "Skills are reusable workflows kept in files. When the user's request matches the description of one of these, call the "
        "`skill` tool with its name and follow what it returns; then do the work. A message that names a skill as /name already "
        "carries that skill's instructions, so do not load it again. Available skills:"
    ]
    for skill in usable[:LISTED]:
        description = skill.description if len(skill.description) <= DESCRIPTION_LIMIT else skill.description[: DESCRIPTION_LIMIT - 1] + "…"
        lines.append(f"- {skill.name}: {description}")
    if len(usable) > LISTED:
        lines.append(f"({len(usable) - LISTED} more; the user can name them as /name.)")
    return "\n".join(lines)


def load_text(skill: Skill) -> str:
    """What the ``skill`` tool answers: where the skill is, what is beside it, and its instructions."""
    files = skill.files()
    head = f'Skill "{skill.name}" ({skill.source}); its folder is {skill.folder}.'
    if files:
        head += " Files beside the instructions (read them with read_file when the instructions point to them): " + ", ".join(files) + "."
    return f"{head}\n\n<instructions>\n{skill.body()}\n</instructions>"


def named_in(text: str, skills: Sequence[Skill]) -> list[Skill]:
    """The skills a message names as whole words ``/name`` (each once, in the order named); a path such as ``/tmp/x`` names none."""
    by_name = {skill.name: skill for skill in skills if skill.user_invocable}
    out: list[Skill] = []
    for match in _TOKEN.finditer(text):
        skill = by_name.get(match.group(1))
        if skill is not None and skill not in out:
            out.append(skill)
    return out


# ---------------------------------------------------------------------------------------------------------- commands


@dataclass(frozen=True, slots=True)
class Command:
    name: str
    description: str
    hint: str
    path: Path
    source: str
    plugin: Path | None = None

    def prompt(self, arguments: str) -> str:
        text = _read(self.path)
        body = split_front_matter(text)[1].strip()[:BODY_LIMIT] if text is not None else ""
        return _expand_root(body.replace("$ARGUMENTS", arguments.strip()), self.plugin)


def _command_of(path: Path, source: Source) -> Command | None:
    text = _read(path)
    if text is None:
        return None
    data, body = split_front_matter(text)
    name = slug(path.stem)
    if not name:
        return None
    first = next((line.strip().lstrip("#").strip() for line in body.splitlines() if line.strip()), "")
    description = " ".join(str(data.get("description") or first).split())
    return Command(name, description, str(data.get("argument-hint") or ""), path, source.label, source.plugin)


def discover_commands(project: Path | str, home: Path | None = None) -> list[Command]:
    project = Path(project)
    home = _home(home)
    found: dict[str, Command] = {}
    for source in command_sources(project, home):
        if not source.folder.is_dir():
            continue
        for current, folders, names in os.walk(source.folder):
            depth = len(Path(current).relative_to(source.folder).parts)
            folders[:] = sorted(name for name in folders if name not in _SKIP) if depth < DEPTH else []
            for name in sorted(names):
                if name.lower().endswith(".md"):
                    command = _command_of(Path(current) / name, source)
                    if command is not None and command.name not in found:
                        found[command.name] = command
    return list(found.values())


def called_command(text: str, commands: Sequence[Command]) -> tuple[Command, str] | None:
    """The command a message starts with and the arguments after it (a message starting with a command is that command's, whole)."""
    match = _CALL.match(text.strip())
    if match is None:
        return None
    command = next((c for c in commands if c.name == match.group(1)), None)
    return (command, (match.group(2) or "").strip()) if command is not None else None


# ---------------------------------------------------------------------------------------------------------- the hook


def listing(project: Path | str, home: Path | None = None) -> list[dict[str, Any]]:
    """What the page's command menu is given: ``{name, description, hint, kind, source}`` of the skills a user may name and the
    custom commands."""
    items: list[dict[str, Any]] = []
    for command in discover_commands(project, home):
        items.append({"name": command.name, "description": command.description, "hint": command.hint, "kind": "command", "source": command.source})
    names = {item["name"] for item in items}
    for skill in discover(project, home):
        if skill.user_invocable and skill.name not in names:
            items.append({"name": skill.name, "description": skill.description, "hint": "", "kind": "skill", "source": skill.source})
    return items


def prompt_hook(event: PromptEvent) -> PromptContext | None:
    """Before a message goes to any engine: the instructions of the command it starts with, or of the skills it names."""
    try:
        called = called_command(event.text, discover_commands(event.project))
        if called is not None:
            command, arguments = called
            return PromptContext(
                text=f"The user ran the custom command /{command.name}. Do what it asks.\n<command name=\"{command.name}\">\n{command.prompt(arguments)}\n</command>",
                notice=f"已载入命令 /{command.name}",
            )
        used = named_in(event.text, discover(event.project))
        if not used:
            return None
        names = ", ".join(skill.name for skill in used)
        parts = [f"The user named the skill{'s' if len(used) > 1 else ''} {names}. Follow {'their' if len(used) > 1 else 'its'} instructions for this request."]
        for skill in used:
            parts.append(f'<skill name="{skill.name}" folder="{skill.folder}">\n{skill.body()}\n</skill>')
        return PromptContext(text="\n".join(parts), notice="已载入技能 " + "、".join(skill.name for skill in used))
    except Exception:
        log.exception("reading skills for a message failed")
        return None
