"""How the application hands the memory to an engine: the command of the MCP server, and the note put before a new conversation.

Nothing is written to an engine's own configuration: each engine is given the server as a parameter of the conversation
it opens (Droid's ``mcp_servers``, Codex's ``config``, Devin's ``mcpServers``), so turning the memory off, or removing
WittyCode, leaves all three exactly as they were.
"""

from __future__ import annotations

import logging
import os
import secrets
import sys
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from desktop.core.hooks import PromptContext, PromptEvent
from desktop.core.memory.capture import detect
from desktop.core.memory.learn import Learner
from desktop.core.memory.recall import pick, render
from desktop.core.memory.review import ReviewStore, remembered
from desktop.core.memory.server import BRIEF_NOTE
from desktop.core.memory.store import MemoryRejected, MemoryStore, project_path

log = logging.getLogger(__name__)

SERVER_NAME = "wittycode-memory"
BRIEF_GLOBAL = 8
BRIEF_PROJECT = 12
BRIEF_CHARS = 4000
"""About 1200 tokens: the note before a new conversation never grows beyond this."""
PACKAGE_ROOT = Path(__file__).resolve().parents[3]
"""The folder that holds the ``desktop`` package: the server must be able to import it from any working folder."""


@dataclass(frozen=True, slots=True)
class McpCommand:
    """A stdio MCP server as an engine is told to start it."""

    name: str
    command: str
    args: tuple[str, ...]
    env: dict[str, str]


def console_python() -> str:
    """This program's Python with a console (the server talks over stdin / stdout, which ``pythonw`` does not have)."""
    executable = Path(sys.executable)
    if executable.name.lower() == "pythonw.exe":
        sibling = executable.with_name("python.exe")
        if sibling.is_file():
            return str(sibling)
    return str(executable)


def new_run() -> str:
    """A token for one conversation: the notes it writes carry it, and the application knows whose it is."""
    return secrets.token_hex(6)


class MemoryLaunch:
    def __init__(
        self,
        root: str | os.PathLike[str],
        *,
        enabled: Callable[[str], bool] = lambda engine: True,
        recall: Callable[[], bool] = lambda: True,
        capture: Callable[[], bool] = lambda: True,
        python: str | None = None,
        learn: Callable[[], bool] = lambda: False,
    ) -> None:
        self.root = Path(root)
        self.enabled = enabled
        self.recall = recall
        """Whether the notes that bear on the first message of a new conversation are given with it (``prompt_hook``). Later
        messages bring none: the model searches the memory itself when it needs to."""
        self.capture = capture
        """Whether a message that says plainly "remember this" is written down by the application (``prompt_hook``)."""
        self.python = python or console_python()
        self.learn = learn
        """Whether a note a message asks for in so many words is also a lesson for ``learner``: the user keeps notes like it
        (when the application writes notes on its own and learns from the user's word, ``core/memory/review``)."""
        self.learner = Learner(self.root)
        """What the user's verdicts on the application's own notes taught (``core/memory/learn``; nothing is read before
        it is asked)."""
        self.review = ReviewStore(self.root)
        """The notes the application wrote on its own, waiting for the user's look (``core/memory/review``)."""
        self._store: MemoryStore | None = None

    @property
    def store(self) -> MemoryStore:
        if self._store is None:
            self._store = MemoryStore(self.root)
        return self._store

    def server_for(self, engine: str, project: str | os.PathLike[str], run: str) -> McpCommand | None:
        """The server to start for a conversation of ``engine`` (``None``: the memory is off for that engine)."""
        if not self.enabled(engine):
            return None
        env = {"PYTHONPATH": str(PACKAGE_ROOT), "PYTHONIOENCODING": "utf-8", "PYTHONUTF8": "1"}
        if os.environ.get("WITTYCODE_MEMORY_LOG"):  # (for looking at what an engine says to the server)
            env["WITTYCODE_MEMORY_LOG"] = os.environ["WITTYCODE_MEMORY_LOG"]
        return McpCommand(
            SERVER_NAME,
            self.python,
            ("-m", "desktop.core.memory", "--store", str(self.root), "--engine", engine, "--project", project_path(project), "--run", run),
            env,
        )

    def brief_for(self, engine: str, project: str | os.PathLike[str]) -> str:
        """The note put before the first message of a new conversation: which notes there are (titles only) and how to use
        the rest. Empty when the memory is off for the engine."""
        if not self.enabled(engine):
            return ""
        return build_brief(self.store, project)

    def _remember(self, event: PromptEvent) -> tuple[str, str]:
        """Write the note a message asks for in so many words (``core/memory/capture``): the line for the user, and the id."""
        found = detect(event.text, has_project=bool(event.project))
        if found is None:
            return "", ""
        try:
            entry = self.store.create(
                title=found.title, text=found.text, scope=found.scope, project=str(event.project) if found.scope == "project" else "",
                kind=found.kind, tags=found.tags, by="user",
            )
        except MemoryRejected as rejected:
            if rejected.code == "secret":
                return "这条里有像密钥的内容，没有记进共享记忆。", ""
            if rejected.code == "limit":
                return "共享记忆已满，这条没有记下；在设置 › 共享记忆 里忘掉一些再试。", ""
            return "", ""  # (already there, or nothing to keep)
        if self.learn():
            try:
                remembered(self.learner, entry, event.engine)
            except Exception:
                log.warning("the memory could not learn from a note the user asked for", exc_info=True)
        return f"已记下「{entry.title}」（设置 › 共享记忆 里可以改或忘记）", entry.id

    def prompt_hook(self, event: PromptEvent) -> PromptContext | None:
        """The hook before a message is sent (``core/hooks``): the note the message asks for, if it does, and, for the first
        message of a new conversation only, the notes that bear on it, with the title of each for the user. Later messages
        bring no notes: they cost tokens on every turn, and the model has ``memory_search`` for what it needs. Whatever goes
        wrong is the hooks' to log: the message is sent without."""
        if not self.enabled(event.engine):
            return None
        said, made = self._remember(event) if self.capture() else ("", "")
        picked = []
        if event.first and self.recall():
            picked = [n for n in pick(self.store.candidates(str(event.project) if event.project else None), event.text, seen=event.seen) if n.id != made]
        if not picked:
            return PromptContext("", said, frozenset()) if said else None
        text, shown = render(picked)
        titles = "、".join(f"「{' '.join(note.title.split())}」" for note in shown)
        line = f"带上了 {len(shown)} 条相关记忆：{titles}"
        return PromptContext(text, f"{said}；{line}" if said else line, frozenset(note.id for note in shown))


def build_brief(store: MemoryStore, project: str | os.PathLike[str]) -> str:
    here = project_path(project) if project else None
    lines = [
        "[Shared memory of the user's coding agents is available: tools memory_search, memory_read, memory_write, "
        "memory_update, memory_forget. " + BRIEF_NOTE + " Notes are handed over only here, with the first message: later, call "
        "memory_search yourself whenever the work may depend on something remembered. Save what is worth keeping for later "
        "conversations with memory_write.]"
    ]
    sections = (
        ("Global notes", store.search("", scope="global", limit=BRIEF_GLOBAL)),
        ("Notes for this project", store.search("", project=here, scope="project", limit=BRIEF_PROJECT) if here else []),
    )
    for title, hits in sections:
        if hits:
            lines.append(f"{title}:")
            lines += [f"- {hit.entry.id} {hit.entry.title} ({hit.entry.kind})" for hit in hits]
    text = "\n".join(lines)
    if len(text) > BRIEF_CHARS:
        text = text[: BRIEF_CHARS - 1].rsplit("\n", 1)[0] + "\n…"
    return text + "\n\n"
