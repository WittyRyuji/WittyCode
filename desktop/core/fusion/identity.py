"""Fusion identities and their local Markdown overrides."""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Literal


@dataclass(frozen=True, slots=True)
class Identity:
    name: str
    description: str
    role: Literal["worker", "light"]
    reasoning: str
    tools: frozenset[str]
    compact_at: int | None
    reuse: bool
    instructions: str


@dataclass(frozen=True, slots=True)
class Identities:
    _entries: tuple[Identity, ...]
    main: str
    problems: tuple[str, ...] = ()

    def get(self, name: str) -> Identity | None:
        """Look up an expert name without regard to case."""
        key = name.casefold()
        return next((entry for entry in self._entries if entry.name.casefold() == key), None)

    @property
    def names(self) -> tuple[str, ...]:
        return tuple(entry.name for entry in self._entries)


DECOMPOSITION = """You are the Fusion main agent. Own routing, work briefs, verification and communication with the user.
- Answer questions, investigate, review and plan yourself; do not delegate during planning. Read-only work needs no route.
- Before the first write or write delegation, submit a route with the destination, reason and verification depth. Use the approved plan's route when there is one.
- For a one- or two-step edit, choose self and self-check. For easy multi-step work, give the whole change to one Coder and use a Runner. Split only when deliverables can be accepted independently. For a difficult change that cannot be split, do it yourself or use one Coder with a Runner and a QualitySampler.
- Give each expert a self-contained brief with acceptance criteria, write paths and budget. Reuse the Coder for the same responsibility. Run at most four independent workstreams with disjoint write paths in parallel; run dependent work in order.
- For split work, have a Runner check each deliverable and use a QualitySampler to sample the work. With one Coder, add sampling for a difficult change or a risk signal: concerns, an Observer report, out-of-scope changes or a fix after a failed check. If there is nothing runnable, record why instead of sending a Runner.
- Review the diff and evidence yourself once against the user's request and acceptance criteria. Send blocking findings back to the original Coder and rerun only affected checks. Record verification before claiming completion. Only you decide what to keep in long-term memory or the project map.
- If an expert is blocked or over budget, the estimate or split was wrong. Reassess the report, record the routing mistake and route again. Handle Observer findings yourself."""

_EXPERT_BOUNDARY = "Do not delegate or write to long-term memory."
_BUILTIN_TEXT = (
    """---
name: Coder
description: Implements and fixes changes.
role: worker
reasoning:
tools: read, edit, exec
compact_at:
reuse: true
---
Implement the brief within its declared write paths. Check your changes against the acceptance criteria and report evidence or blockers.""",
    """---
name: Runner
description: Runs acceptance checks and reports evidence.
role: light
reasoning:
tools: read, exec
compact_at:
reuse: false
---
Run the acceptance checks and report the commands, outcomes and evidence. Do not edit or decide whether to accept the work.""",
    """---
name: QualitySampler
description: Independently samples changes against acceptance criteria.
role: worker
reasoning:
tools: read, exec
compact_at:
reuse: false
---
Choose which parts of the diff and evidence to sample against the acceptance criteria. Work without the Coder's process history, report your findings and do not edit.""",
    """---
name: Observer
description: Reports problems while an expert is running.
role: light
reasoning:
tools: read
compact_at:
reuse: false
---
Use the brief and recent tool calls to watch for loops, out-of-scope writes and stalls. Only report what you observe.""",
)
_FIELDS = frozenset(("name", "description", "role", "reasoning", "tools", "compact_at", "reuse"))
_TOOL_KINDS = frozenset(("read", "edit", "exec"))
_MAX_BYTES = 64 * 1024


def _front_matter(text: str) -> tuple[dict[str, str], str]:
    lines = text.splitlines(keepends=True)
    if not lines or lines[0].strip() != "---":
        raise ValueError("Expected opening front matter delimiter (---).")
    fields: dict[str, str] = {}
    for index, line in enumerate(lines[1:], 1):
        if line.strip() == "---":
            if not fields.get("name"):
                raise ValueError("Front matter needs a name.")
            return fields, "".join(lines[index + 1:]).strip("\r\n")
        if not line.strip():
            continue
        key, separator, value = line.partition(":")
        key = key.strip()
        if not key or not separator:
            raise ValueError("Expected key: value in front matter.")
        if key in _FIELDS:
            if key in fields:
                raise ValueError(f"Duplicate front matter field: {key}.")
            fields[key] = value.strip()
    raise ValueError("Expected closing front matter delimiter (---).")


def _identity(fields: dict[str, str], body: str, base: Identity | None = None) -> Identity:
    role = fields.get("role", base.role if base else "")
    if role != "worker" and role != "light":
        raise ValueError("role must be worker or light.")
    tools = base.tools if base else frozenset()
    if "tools" in fields:
        requested = frozenset(item.strip() for item in fields["tools"].split(",")) if fields["tools"] else frozenset()
        if not requested <= _TOOL_KINDS:
            raise ValueError("tools must be a comma-separated list of read, edit and/or exec.")
        tools = requested & base.tools if base else requested
    reuse = base.reuse if base else False
    if "reuse" in fields:
        flag = fields["reuse"].casefold()
        if flag not in ("true", "false"):
            raise ValueError("reuse must be true or false.")
        reuse = flag == "true" and (base is None or base.reuse)
    compact_at = base.compact_at if base else None
    if "compact_at" in fields:
        value = fields["compact_at"]
        if value and not (value.isascii() and value.isdecimal() and 1 <= int(value) <= 100):
            raise ValueError("compact_at must be an integer from 1 to 100 or empty.")
        compact_at = int(value) if value else None
    return Identity(base.name if base else fields["name"], fields.get("description", base.description if base else ""),
                    role, fields.get("reasoning", base.reasoning if base else ""), tools, compact_at, reuse, body)


_BUILTINS = Identities(tuple(_identity(*_front_matter(f"{text}\n{_EXPERT_BOUNDARY}")) for text in _BUILTIN_TEXT), DECOMPOSITION)


def load(folder: Path | None) -> Identities:
    """Load built-ins plus optional UTF-8 overrides. Omitted headers inherit; the body replaces."""
    if folder is None:
        return _BUILTINS
    try:
        paths = sorted((path for path in folder.iterdir() if path.suffix.casefold() == ".md"),
                       key=lambda path: (path.name.casefold(), path.name))
    except OSError as error:
        return Identities(_BUILTINS._entries, DECOMPOSITION, (f"Cannot read identity folder: {error.strerror}.",))
    entries = {entry.name: entry for entry in _BUILTINS._entries}
    main = DECOMPOSITION
    problems: list[str] = []
    for path in paths:
        try:
            if not path.is_file():
                raise ValueError("Not a readable identity file.")
            with path.open("rb") as stream:
                raw = stream.read(_MAX_BYTES + 1)
            if len(raw) > _MAX_BYTES:
                raise ValueError("Identity file exceeds 64 KiB.")
            fields, body = _front_matter(raw.decode("utf-8-sig").replace("\r\n", "\n"))
            name = fields["name"]
            if name.casefold() == "main":
                main = body
            else:
                base = _BUILTINS.get(name)
                if base is None:
                    raise ValueError(f"Unknown identity: {name}.")
                entries[base.name] = _identity(fields, body, base)
        except OSError as error:
            problems.append(f"{path.name}: Cannot read identity file: {error.strerror}.")
        except UnicodeError:
            problems.append(f"{path.name}: Identity file must be UTF-8.")
        except ValueError as error:
            problems.append(f"{path.name}: {error}")
    return Identities(tuple(entries.values()), main, tuple(problems))


def display_name(identity_name: str, workstream: str) -> str:
    return f"{identity_name}-{workstream}"
