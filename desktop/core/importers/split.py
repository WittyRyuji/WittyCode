"""Markdown memory files as candidate notes: split by headings, small neighbours merged, big sections cut.

A file is read as a tree of sections (headings ``#`` to ``####``; deeper ones are text). A section whose whole subtree fits
in ``MERGE_LIMIT`` characters becomes one note (its sub-headings stay in the text); a bigger one gives its own text as a note
and its children are looked at the same way, where neighbours that are small on their own are packed together up to
``MERGE_LIMIT``, so a list of one-line sections does not become forty notes. A note longer than the memory takes
(``MAX_TEXT``) is cut between paragraphs or list items, never inside a code block (a code block too long by itself is cut
between its lines and each part closed and reopened); the parts are titled "<heading> (2)", "(3)".

Before that, front matter, HTML comments and lines that only import other files (``@path``) are taken out, and lines that
look like a secret are dropped (``base.scrub``). A file that is only a table of contents or only imports gives nothing.
Everything is deterministic: the same text gives the same notes with the same keys.
"""

from __future__ import annotations

import hashlib
import re
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field, replace
from pathlib import PureWindowsPath

from desktop.core.importers.base import KINDS, MAX_TAG, MAX_TAGS, MAX_TEXT, MAX_TITLE, MemoryCandidate, Report, norm_key, scrub, text_digest

# (the memory's own rule for "the same title", so that what is offered is what it will take)
from desktop.core.memory.store import _normal_title, _same_subject

MERGE_LIMIT = 1200
"""Characters up to which a section with its subsections, or a run of small neighbours, is one note."""

_HEADING = re.compile(r"^(#{1,4})[ \t]+(.+?)(?:[ \t]+#+)?[ \t]*$")
_FENCE = re.compile(r"^[ \t]{0,3}(`{3,}|~{3,})(.*)$")
_COMMENT = re.compile(r"<!--.*?-->", re.DOTALL)
_IMPORT_ONLY = re.compile(r"^\s*(?:[-*+]\s+)?(?:@[^\s@]+\s*)+$")
_TOC_LINE = re.compile(r"^\s*(?:[-*+]|\d+[.)])\s+\[[^\]]*\]\([^)]*\)\s*$")
_LIST_ITEM = re.compile(r"^(?:[-*+]|\d+[.)])\s")
_SENTENCE_END = re.compile(r"(?<=[。！？!?；;])|(?<=[.:])(?=\s)")
_WORD_SPLIT = re.compile(r"[\s,，。;；:：!！?？()（）\[\]{}<>\"'“”‘’/\\|`*_~、·•—–=+#&@^$%]+")
_STOP_WORDS = frozenset(
    "a an and are as at be by for from how in is it of on or the to with what when where why which your you 的 和 与 及 或 在 是".split()
)

_KIND_HEADING = (
    ("pitfall", re.compile(r"坑|陷阱|踩|注意|警告|pitfall|gotcha|caveat|trap|warning|known issue|workaround|\bbugs?\b|troubleshoot", re.IGNORECASE)),
    ("preference", re.compile(r"偏好|喜好|喜欢|习惯|风格|口味|prefer|preference|\bstyle\b|\btaste\b|\bfeedback\b", re.IGNORECASE)),
    ("convention", re.compile(r"规则|约定|规范|惯例|准则|守则|纪律|禁止|必须|convention|\brules?\b|guideline|\bstandards?\b|\bmust\b|\bnever\b|\balways\b|policy|policies", re.IGNORECASE)),
)
_KIND_TEXT = (
    ("pitfall", re.compile(r"踩过的坑|这个坑|陷阱|pitfall|gotcha|known issue", re.IGNORECASE)),
    ("preference", re.compile(r"偏好|我喜欢|I prefer|\bprefers?\b", re.IGNORECASE)),
    ("convention", re.compile(r"必须|禁止|不要|务必|一律|不得|\bmust\b|\bnever\b|\balways\b|\bdo not\b|\bdon't\b", re.IGNORECASE)),
)


@dataclass(slots=True)
class _Section:
    level: int
    title: str
    lines: list[str] = field(default_factory=list)
    children: list[_Section] = field(default_factory=list)
    heading: str = ""
    """The heading line as written (empty for the text before the first heading)."""

    def own(self) -> str:
        return "\n".join(self.lines).strip()

    def render(self, *, with_heading: bool) -> str:
        parts = [self.heading] if with_heading and self.heading else []
        if self.own():
            parts.append(self.own())
        parts += [child.render(with_heading=True) for child in self.children if child.has_text()]
        return "\n\n".join(parts)

    def has_text(self) -> bool:
        return bool(self.own()) or any(child.has_text() for child in self.children)


@dataclass(slots=True)
class _Note:
    title: str
    path: tuple[str, ...]
    text: str


# ---------------------------------------------------------------------------------------------- cleaning


def normalize(text: str) -> str:
    return text.lstrip("\ufeff").replace("\r\n", "\n").replace("\r", "\n")


def front_matter(text: str) -> tuple[dict[str, str], str]:
    """(the ``key: value`` lines of a YAML front matter, the text after it); no front matter: ``({}, text)``."""
    text = normalize(text)
    if not text.startswith("---\n"):
        return {}, text
    end = re.search(r"^(?:---|\.\.\.)[ \t]*$", text[4:], re.MULTILINE)
    if end is None:
        return {}, text
    head = text[4 : 4 + end.start()]
    body = text[4 + end.end() :].lstrip("\n")
    meta: dict[str, str] = {}
    for line in head.split("\n"):
        key, colon, value = line.partition(":")
        if colon and re.fullmatch(r"[A-Za-z_][\w-]*", key.strip()):
            value = value.strip()
            if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
                value = value[1:-1]
            meta[key.strip()] = value
    return meta, body


def _outside_fences(text: str) -> list[tuple[bool, str]]:
    """The text in runs: (inside a code block, the run's lines)."""
    runs: list[tuple[bool, list[str]]] = []
    fence = ""
    for line in text.split("\n"):
        found = _FENCE.match(line)
        inside = bool(fence)
        if fence and found and found.group(1)[0] == fence[0] and len(found.group(1)) >= len(fence) and not found.group(2).strip():
            fence = ""
        elif not fence and found:
            fence = found.group(1)
            inside = True
        if runs and runs[-1][0] == inside:
            runs[-1][1].append(line)
        else:
            runs.append((inside, [line]))
    return [(inside, "\n".join(lines)) for inside, lines in runs]


def strip_comments(text: str) -> str:
    """HTML comments taken out, except in code blocks (as Claude Code does before a CLAUDE.md reaches the model)."""
    if "<!--" not in text:
        return text
    return "\n".join(run if inside else _COMMENT.sub("", run) for inside, run in _outside_fences(text))


def drop_imports(text: str) -> str:
    """Lines that only import other files (``@path``) taken out; an import in a sentence stays as written."""
    if "@" not in text:
        return text
    out = []
    for inside, run in _outside_fences(text):
        if inside:
            out.append(run)
        else:
            out.append("\n".join(line for line in run.split("\n") if not _IMPORT_ONLY.match(line)))
    return "\n".join(out)


def _only_contents(text: str) -> bool:
    """Nothing but headings and lines that are a link each (a table of contents)."""
    content = [line for line in text.split("\n") if line.strip() and not _HEADING.match(line)]
    return bool(content) and all(_TOC_LINE.match(line) for line in content)


def clean_title(text: str) -> str:
    text = re.sub(r"\[([^\]]*)\]\([^)]*\)", r"\1", text)
    text = re.sub(r"[*_`]{1,3}([^*_`]+)[*_`]{1,3}", r"\1", text)
    text = re.sub(r"^\s*(?:[-*+>]|\d+[.)])\s+", "", text)
    text = re.sub(r"\s+", " ", text).strip().strip("#").strip().rstrip(":：").strip()
    return text


def _fit_title(title: str, suffix: str = "") -> str:
    room = MAX_TITLE - len(suffix)
    if len(title) > room:
        title = title[: room - 1].rstrip() + "…"
    return title + suffix


# ---------------------------------------------------------------------------------------------- the tree


def _tree(text: str) -> _Section:
    root = _Section(0, "")
    stack = [root]
    for inside, run in _outside_fences(text):
        for line in run.split("\n"):
            found = None if inside else _HEADING.match(line)
            if found is None:
                stack[-1].lines.append(line)
                continue
            level = len(found.group(1))
            while stack[-1].level >= level:
                stack.pop()
            section = _Section(level, clean_title(found.group(2)), heading=line.strip())
            stack[-1].children.append(section)
            stack.append(section)
    return root


def _first_line(text: str) -> str:
    for line in text.split("\n"):
        cleaned = clean_title(line)
        if cleaned and not _FENCE.match(line):
            return cleaned
    return ""


def _notes(section: _Section, path: tuple[str, ...], fallback: str) -> list[_Note]:
    whole = section.render(with_heading=False)
    title = section.title or fallback or _first_line(whole)
    if not whole.strip():
        return []
    if len(whole) <= MERGE_LIMIT:
        return [_Note(title, path, whole)]
    notes: list[_Note] = []
    own = section.own()
    if own:
        notes.append(_Note(title or _first_line(own), path, own))
    pack: list[_Section] = []
    size = 0

    def flush() -> None:
        nonlocal pack, size
        if len(pack) == 1:
            child = pack[0]
            notes.append(_Note(child.title, path + (child.title,), child.render(with_heading=False)))
        elif pack:
            names = [child.title for child in pack]
            joined = " / ".join(names)
            label = joined if len(joined) <= MAX_TITLE else f"{names[0]} 等 {len(names)} 节"
            notes.append(_Note(label, path + ("+".join(names),), "\n\n".join(child.render(with_heading=True) for child in pack)))
        pack, size = [], 0

    for child in section.children:
        if not child.has_text():
            continue
        body = child.render(with_heading=True)
        if len(body) > MERGE_LIMIT:
            flush()
            notes += _notes(child, path + (child.title,), "")
            continue
        if pack and size + 2 + len(body) > MERGE_LIMIT:
            flush()
        pack.append(child)
        size += len(body) + (2 if size else 0)
    flush()
    return notes


# ---------------------------------------------------------------------------------------------- cutting


def _blocks(text: str) -> list[tuple[str, str]]:
    """Paragraphs, list items and code blocks, in order, each with what separated it from the one before (a blank line,
    or a line break between the items of a list)."""
    blocks: list[tuple[str, str]] = []
    current: list[str] = []
    gap = "\n\n"

    def end(next_gap: str) -> None:
        nonlocal gap
        if current and "\n".join(current).strip():
            blocks.append(("\n".join(current).strip("\n"), gap))
            gap = next_gap
        current.clear()

    for inside, run in _outside_fences(text):
        if inside:
            end("\n\n")
            blocks.append((run.strip("\n"), gap))
            gap = "\n\n"
            continue
        for line in run.split("\n"):
            if not line.strip():
                end("\n\n")
                gap = "\n\n"
            elif _LIST_ITEM.match(line) and current:
                end("\n")
                current.append(line)
            else:
                current.append(line)
        end("\n\n")
    return blocks


def _cut_block(block: str, limit: int) -> list[str]:
    """A block longer than ``limit`` in pieces: a code block between its lines (each piece fenced again), other text
    after a sentence, else after a line, else where it must."""
    found = _FENCE.match(block.split("\n", 1)[0])
    if found:
        lines = block.split("\n")
        opener = lines[0]
        closer = lines[-1] if len(lines) > 1 and _FENCE.match(lines[-1]) else found.group(1)
        body = lines[1:-1] if len(lines) > 1 and _FENCE.match(lines[-1]) else lines[1:]
        room = limit - len(opener) - len(closer) - 2
        pieces: list[str] = []
        current: list[str] = []
        used = 0
        for line in body:
            for part in [line[i : i + room] for i in range(0, len(line), room)] or [""]:
                if current and used + len(part) + 1 > room:
                    pieces.append("\n".join([opener, *current, closer]))
                    current, used = [], 0
                current.append(part)
                used += len(part) + 1
        if current:
            pieces.append("\n".join([opener, *current, closer]))
        return pieces
    pieces = []
    rest = block
    while len(rest) > limit:
        window = rest[:limit]
        cut = max((m.end() for m in _SENTENCE_END.finditer(window) if m.end() > limit // 3), default=0)
        if not cut:
            cut = window.rfind("\n") + 1 if window.rfind("\n") > limit // 3 else 0
        if not cut:
            cut = window.rfind(" ") + 1 if window.rfind(" ") > limit // 3 else limit
        pieces.append(rest[:cut].strip())
        rest = rest[cut:].strip()
    if rest:
        pieces.append(rest)
    return pieces


def cut(text: str, limit: int = MAX_TEXT) -> list[str]:
    """``text`` in parts of at most ``limit`` characters, cut between blocks."""
    text = text.strip()
    if len(text) <= limit:
        return [text] if text else []
    parts: list[str] = []
    current = ""
    for block, gap in _blocks(text):
        for piece in _cut_block(block, limit) if len(block) > limit else [block]:
            if current and len(current) + len(gap) + len(piece) > limit:
                parts.append(current)
                current = ""
            current = f"{current}{gap}{piece}" if current else piece
            gap = "\n\n"
    if current:
        parts.append(current)
    return parts


# ---------------------------------------------------------------------------------------------- kind, tags, keys


def guess_kind(titles: Sequence[str], text: str) -> str:
    heading = " ".join(titles)
    for kind, pattern in _KIND_HEADING:
        if pattern.search(heading):
            return kind
    for kind, pattern in _KIND_TEXT:
        if pattern.search(text):
            return kind
    return "fact"


def _words(title: str) -> list[str]:
    out = []
    for word in _WORD_SPLIT.split(title):
        word = word.strip(".-")
        if not word or word.lower() in _STOP_WORDS or word.isdigit():
            continue
        if word.isascii() and len(word) < 2:
            continue
        out.append(word[:MAX_TAG])
    return out


def make_tags(app: str, base: Sequence[str], titles: Sequence[str]) -> tuple[str, ...]:
    tags: list[str] = []
    seen: set[str] = set()
    for tag in [app, *base, *(word for title in reversed(titles) for word in _words(title))]:
        tag = re.sub(r"\s+", " ", str(tag)).strip()[:MAX_TAG]
        if tag and tag.lower() not in seen:
            seen.add(tag.lower())
            tags.append(tag)
        if len(tags) >= MAX_TAGS:
            break
    return tuple(tags)


def candidate_key(app: str, origin_file: str, path: Sequence[str], part: int, occurrence: int) -> str:
    raw = "\n".join([app, norm_key(origin_file), " › ".join(path), str(part), str(occurrence)])
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()


# ---------------------------------------------------------------------------------------------- the whole


def tidy(candidates: Sequence[MemoryCandidate], existing: Iterable[tuple[str, str, str]] = ()) -> list[MemoryCandidate]:
    """The candidates as the shared memory will take them side by side: a text that came before is left out, and a title it
    would call the same as an earlier one of the same scope and project (``memory.store`` refuses those as duplicates)
    gets the heading above it, or the file's name, and a number if that is not enough. ``existing``: (scope, project,
    title) of notes already in the memory."""
    taken: dict[tuple[str, str], list[str]] = {}
    for scope, project, title in existing:
        taken.setdefault((scope, norm_key(project) if project else ""), []).append(_normal_title(title))
    seen: set[str] = set()
    out: list[MemoryCandidate] = []
    for candidate in candidates:
        if candidate.digest in seen:
            continue
        seen.add(candidate.digest)
        names = taken.setdefault((candidate.scope, norm_key(candidate.project) if candidate.project else ""), [])
        title = candidate.title
        if _clashes(title, names):
            places = candidate.origin.split(" › ")
            above = next((p for p in reversed(places[1:-1]) if _normal_title(p) != _normal_title(title)), "") or PureWindowsPath(places[0]).stem
            title = _fit_title(f"{above} › {candidate.title}")
            number = 2
            while _clashes(title, names):
                title = _fit_title(f"{above} › {candidate.title}", f" ({number})")
                number += 1
        names.append(_normal_title(title))
        out.append(candidate if title == candidate.title else replace(candidate, title=title))
    return out


def _clashes(title: str, names: Sequence[str]) -> bool:
    normal = _normal_title(title)
    return any(_same_subject(normal, other) for other in names)


def split_markdown(
    text: str,
    *,
    app: str,
    origin_file: str,
    scope: str,
    project: str = "",
    base_tags: Sequence[str] = (),
    title: str = "",
    kind: str | None = None,
    report: Report | None = None,
) -> list[MemoryCandidate]:
    """Candidate notes of one Markdown file. ``title``: the title of the text before the first heading (else its first
    line); ``kind``: the kind of every note (else guessed from the headings and the text); ``report`` counts the lines
    dropped as secrets."""
    _, body = front_matter(text)
    body = scrub(drop_imports(strip_comments(body)), report)
    if not body.strip() or _only_contents(body):
        return []
    section, path = _tree(body), ()
    while not title.strip() and not section.own() and len(shown := [child for child in section.children if child.has_text()]) == 1:
        section = shown[0]
        path = path + (section.title,)
    notes = _notes(section, path, title.strip())
    out: list[MemoryCandidate] = []
    seen: dict[tuple[tuple[str, ...], int], int] = {}
    for note in notes:
        parts = cut(note.text)
        for number, part in enumerate(parts, 1):
            heading = clean_title(note.title) or _first_line(part) or "笔记"
            name = _fit_title(heading, f" ({number})" if number > 1 else "")
            slot = (note.path, number)
            occurrence = seen.get(slot, 0)
            seen[slot] = occurrence + 1
            titles = [t for t in note.path if t] or [heading]
            out.append(
                MemoryCandidate(
                    key=candidate_key(app, origin_file, note.path, number, occurrence),
                    app=app,
                    title=name,
                    text=part,
                    scope=scope,
                    project=project if scope == "project" else "",
                    kind=kind if kind in KINDS else guess_kind(titles, part),
                    tags=make_tags(app, base_tags, titles),
                    origin=" › ".join([origin_file, *[t for t in note.path if t]]),
                    digest=text_digest(part),
                )
            )
    return out
