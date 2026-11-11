"""``AppCore``'s import.* surface: the wizard's calls reach the bridge, and imported conversations get a place in the sidebar."""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from desktop.core.app_core import AppError
from desktop.core.harness.store import SessionStore
from desktop.core.import_bridge import ImportBridge
from desktop.core.importers import Message, SessionInfo, saved_id
from desktop.core.memory.launch import MemoryLaunch
from desktop.core.memory.store import MemoryStore
from tests.app_support import make_core
from tests.fakes import until
from tests.test_import_bridge import FakeSource, conversation


def run(coro):
    return asyncio.run(coro)


class QuietEngine:
    """A stand-in ChatGPT Plan engine: the conversation store the import writes into, and nothing else."""

    def __init__(self, root: Path) -> None:
        self.sessions = SessionStore(root)

    def add_listener(self, _callback) -> None:
        pass

    def add_account_listener(self, _callback) -> None:
        pass

    async def list_sessions(self, project, limit=None):
        return self.sessions.rows(str(project), limit)

    async def close_all(self) -> None:
        pass


def core_with(tmp_path: Path, source: FakeSource):
    memory = MemoryLaunch(tmp_path / "memory", python=sys.executable)
    core, factory, events, platform, _ = make_core(tmp_path, projects=False, chatgpt_engine=QuietEngine(tmp_path / "chatgpt"), memory=memory)
    made = ImportBridge(
        MemoryStore(tmp_path / "memory"), core.chatgpt.sessions, make_sources=lambda: [source],
        row=lambda native: "cg:" + native, defaults=lambda: {"model": "chatgpt:gpt-6.1-sol", "reasoning": "", "autonomy": "code"},
    )
    core.imports = made
    return core, events, made


def test_the_wizard_is_created_with_the_shared_memory(tmp_path):
    memory = MemoryLaunch(tmp_path / "memory", python=sys.executable)
    core, _, _, _, _ = make_core(tmp_path, memory=memory, chatgpt_engine=QuietEngine(tmp_path / "chatgpt"))
    assert isinstance(core.imports, ImportBridge)
    core, _, _, _, _ = make_core(tmp_path, chatgpt_engine=QuietEngine(tmp_path / "chatgpt"))
    assert core.imports is None, "no shared memory, no notes to import"


def test_without_the_shared_memory_the_wizard_is_not_there(tmp_path):
    async def scenario():
        core, _, _, _, _ = make_core(tmp_path, chatgpt_engine=QuietEngine(tmp_path / "chatgpt"))
        with pytest.raises(AppError, match="不能从别的应用导入"):
            await core.call("import.scan")
        await core.shutdown()

    run(scenario())


def test_the_wizard_reads_the_preview_through_the_core(tmp_path):
    async def scenario():
        project = tmp_path / "proj2"
        project.mkdir()
        info = conversation("claude:n1", project)
        source = FakeSource(
            "claude", sessions=[info],
            messages={info.key: [Message("user", "你好"), Message("assistant", "你好，有什么可以帮你的？")]},
        )
        core, _, _ = core_with(tmp_path, source)
        sources = await core.call("import.scan")
        assert sources["sources"][0]["app"] == "claude"
        listed = await core.call("import.sessions", {"apps": ["claude"]})
        assert [item["key"] for item in listed["items"]] == ["claude:n1"]
        await core.shutdown()

    run(scenario())


def test_a_confirmed_conversation_is_added_to_its_project_and_the_sidebar_reloads(tmp_path):
    async def scenario():
        project = tmp_path / "proj2"
        project.mkdir()
        info = conversation("claude:n1", project)
        source = FakeSource(
            "claude", sessions=[info],
            messages={info.key: [Message("user", "你好"), Message("assistant", "你好，有什么可以帮你的？")]},
        )
        core, events, _ = core_with(tmp_path, source)
        result = await core.call("import.commit", {"memories": [], "sessions": ["claude:n1"]})
        assert result["sessions"][0]["id"] == saved_id("claude:n1")
        assert [item["title"] for item in result["sessions"]] == ["修缓存"]
        assert str(project) in core.settings.projects, "the conversation's project is in the sidebar now"
        assert events.of("settings"), "the page hears about the new project"
        assert core.chatgpt.sessions.load(saved_id("claude:n1")) is not None
        await until(lambda: any("cg:" + saved_id("claude:n1") in (payload.get("sessions") or {}) for payload in events.of("sidebar")), timeout=3)
        assert any("cg:" + saved_id("claude:n1") in (payload.get("sessions") or {}) for payload in events.of("sidebar")), "the row is in the sidebar"
        assert result["latest"] == 0 and result["unseen"] == 0, "the memory state comes back with it"
        await core.shutdown()

    run(scenario())


def test_undo_of_an_import_returns_the_memory_state(tmp_path):
    async def scenario():
        source = FakeSource("claude", memories=[])
        core, _, _ = core_with(tmp_path, source)
        result = await core.call("import.undo", {"ids": []})
        assert result["forgotten"] == 0 and "latest" in result and "unseen" in result and "pending" in result
        await core.shutdown()

    run(scenario())
