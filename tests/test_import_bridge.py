"""The import wizard's application side (``core/import_bridge``): preview, confirm, undo, over fake sources only."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from desktop.core.harness.store import SessionStore
from desktop.core.import_bridge import ImportBridge
from desktop.core.importers import MemoryCandidate, Message, Report, SessionInfo, saved_id, session_key, text_digest
from desktop.core.memory.store import MemoryStore


class FakeSource:
    """One app as the wizard sees it: what a real ``core/importers`` source would say, without any file."""

    def __init__(
        self,
        app: str,
        *,
        label: str = "",
        memories: list[MemoryCandidate] | None = None,
        sessions: list[SessionInfo] | None = None,
        messages: dict[str, list[Message]] | None = None,
        available: bool = True,
        problem: str = "",
    ) -> None:
        self.app = app
        self.label = label or app
        self._memories = list(memories or ())
        self._sessions = list(sessions or ())
        self._messages = dict(messages or {})
        self._available = available
        self.report = Report(problems=[problem] if problem else [])

    def available(self) -> bool:
        return self._available

    def memories(self) -> list[MemoryCandidate]:
        return list(self._memories)

    def sessions(self) -> list[SessionInfo]:
        return list(self._sessions)

    def read(self, info: SessionInfo) -> list[Message]:
        return list(self._messages[info.key])


def candidate(key: str, title: str, text: str, *, app: str = "claude", scope: str = "global", project: str = "") -> MemoryCandidate:
    return MemoryCandidate(
        key=key, app=app, title=title, text=text, scope=scope, project=project, kind="fact", tags=(app, "导入"), origin="MEMORY.md › 命令",
        digest=text_digest(text),
    )


def conversation(key: str, project: Path, *, title: str = "修缓存", messages: int = 2) -> SessionInfo:
    return SessionInfo(
        key=key, app="claude", native_id=key.partition(":")[2], title=title, project=str(project), started=1_790_000_000.0,
        updated=1_790_000_100.0, messages=messages, size=0, path="",
    )


def bridge(tmp_path: Path, source: FakeSource, **options: Any) -> tuple[ImportBridge, MemoryStore, SessionStore]:
    store = MemoryStore(tmp_path / "memory")
    conversations = SessionStore(tmp_path / "chatgpt")
    options.setdefault("defaults", lambda: {"model": "chatgpt:gpt-6.1-sol", "reasoning": "", "autonomy": "code"})
    options.setdefault("row", lambda native: "cg:" + native)
    made = ImportBridge(store, conversations, make_sources=lambda: [source], **options)
    return made, store, conversations


def test_scan_says_what_each_app_has_and_why_it_cannot_be_read(tmp_path):
    source = FakeSource(
        "claude",
        memories=[candidate("a", "命令", "用 make 构建。"), candidate("b", "发布", "发布用 make ship。")],
        sessions=[conversation("claude:n1", tmp_path)],
    )
    locked = FakeSource("devin", available=True, problem="Devin 的会话库正被另一个程序占用。")
    missing = FakeSource("codex", available=False)
    made = ImportBridge(MemoryStore(tmp_path / "memory"), None, make_sources=lambda: [source, locked, missing])

    sources = {item["app"]: item for item in made.scan({})["sources"]}
    assert sources["claude"] == {"app": "claude", "label": "claude", "available": True, "memories": 2, "sessions": 1}
    assert sources["devin"]["error"] == "Devin 的会话库正被另一个程序占用。" and sources["devin"]["memories"] == 0
    assert sources["codex"]["available"] is False and "error" not in sources["codex"]


def test_a_source_that_fails_on_one_app_does_not_take_the_others_with_it(tmp_path):
    class Broken(FakeSource):
        def memories(self):
            raise OSError("gone")

    good = FakeSource("claude", memories=[candidate("a", "命令", "用 make 构建。")])
    made = ImportBridge(MemoryStore(tmp_path / "memory"), None, make_sources=lambda: [Broken("codex"), good])
    assert [item["key"] for item in made.memories({})["items"]] == ["a"]


def test_the_preview_gives_each_note_a_name_the_memory_would_not_refuse(tmp_path):
    source = FakeSource(
        "claude",
        memories=[
            candidate("a", "命令", "构建用 make。", app="claude"),
            candidate("b", "命令", "发布用 make ship。", app="codex"),
        ],
    )
    made, _, _ = bridge(tmp_path, source)
    items = made.memories({"apps": ["claude", "codex"]})["items"]
    assert [item["title"] for item in items] == ["命令", "MEMORY › 命令"]
    assert all(item["exists"] is False for item in items)


def test_the_preview_marks_what_the_memory_already_holds_and_drops_a_repeated_text(tmp_path):
    made, store, _ = bridge(tmp_path, FakeSource("claude"))
    store.create(title="构建", text="构建用 make。", scope="global", kind="fact", tags=(), by="user")
    source = FakeSource(
        "claude",
        memories=[
            candidate("a", "构建", "构建用  make。"),  # (the same words, spaced differently)
            candidate("b", "发布", "发布用 make ship。"),
            candidate("c", "发布（另一处）", "发布用 make ship。"),
        ],
    )
    made.make_sources = lambda: [source]
    items = made.memories({"apps": ["claude"]})["items"]
    assert [(item["key"], item["exists"]) for item in items] == [("a", True), ("b", False)]


def test_a_note_the_memory_holds_is_shown_once_however_many_files_say_it(tmp_path):
    made, store, _ = bridge(tmp_path, FakeSource("claude"))
    store.create(title="构建", text="构建用 make。", scope="global", kind="fact", tags=(), by="user")
    source = FakeSource(
        "claude",
        memories=[candidate("a", "构建", "构建用 make。", app="claude"), candidate("b", "Make", "构建用 make。", app="codex")],
    )
    made.make_sources = lambda: [source]
    items = made.memories({"apps": ["claude", "codex"]})["items"]
    assert [(item["key"], item["exists"]) for item in items] == [("a", True)]


def test_a_confirmed_note_is_written_as_the_app_it_came_from(tmp_path):
    source = FakeSource("claude", memories=[candidate("a", "命令", "构建用 make。")])
    made, store, _ = bridge(tmp_path, source)
    result = made.commit({"memories": ["a"], "sessions": []})
    assert result["notes"] == [{"key": "a", "id": store.entries()[0].id, "title": "命令"}]
    entry = store.entries()[0]
    assert entry.text == "构建用 make。" and entry.source == {"engine": "claude", "run": ""} and "导入" in entry.tags


def test_the_preview_only_reads_the_apps_that_were_ticked(tmp_path):
    claude = FakeSource("claude", memories=[candidate("a", "命令", "构建用 make。")])
    codex = FakeSource("codex", memories=[candidate("b", "发布", "发布用 make ship。", app="codex")])
    made = ImportBridge(MemoryStore(tmp_path / "memory"), None, make_sources=lambda: [claude, codex])
    assert [item["key"] for item in made.memories({"apps": ["codex"]})["items"]] == ["b"]


def test_two_notes_of_the_same_name_are_written_under_names_of_their_own(tmp_path):
    source = FakeSource(
        "claude",
        memories=[candidate("a", "命令", "构建用 make。"), candidate("b", "命令", "发布用 make ship。")],
    )
    made, store, _ = bridge(tmp_path, source)
    notes = made.commit({"memories": ["a", "b"]})["notes"]
    assert len(notes) == 2 and notes[0]["title"] != notes[1]["title"]
    assert sorted(entry.title for entry in store.entries()) == sorted(note["title"] for note in notes)


def test_what_is_already_in_the_memory_is_skipped_with_a_sentence(tmp_path):
    source = FakeSource("claude", memories=[candidate("a", "命令", "构建用 make。")])
    made, store, _ = bridge(tmp_path, source)
    store.create(title="构建", text="构建用 make。", scope="global", kind="fact", tags=(), by="user")
    result = made.commit({"memories": ["a"]})
    assert result["notes"] == [] and result["skipped"][0]["reason"] == "记忆里已经有同样的内容了。"
    assert len(store.entries()) == 1


def test_a_confirmed_conversation_is_saved_as_a_chatgpt_plan_one(tmp_path):
    project = tmp_path / "proj"
    project.mkdir()
    info = conversation("claude:n1", project)
    source = FakeSource(
        "claude", sessions=[info],
        messages={info.key: [Message("user", "这个项目怎么发布？", 1_790_000_000.0), Message("assistant", "用 make ship。", 1_790_000_005.0)]},
    )
    made, _, conversations = bridge(tmp_path, source)
    listed = made.sessions_list({"apps": ["claude"]})["items"]
    assert listed == [{
        "key": "claude:n1", "app": "claude", "title": "修缓存", "project": str(project), "projectName": "proj",
        "started": 1_790_000_000_000, "updated": 1_790_000_100_000, "messages": 2, "imported": False,
    }]

    result = made.commit({"sessions": ["claude:n1"]})
    assert result["sessions"] == [{"key": "claude:n1", "id": saved_id("claude:n1"), "title": "修缓存", "row": "cg:" + saved_id("claude:n1")}]
    assert result["projects"] == [str(project)]
    saved = conversations.load(saved_id("claude:n1"))
    assert saved is not None and saved.project == str(project) and saved.origin["app"] == "claude"
    assert saved.model == "chatgpt:gpt-6.1-sol" and saved.autonomy == "code"
    assert [item["role"] for item in saved.items] == ["user", "assistant"]
    assert made.sessions_list({"apps": ["claude"]})["items"][0]["imported"] is True


def test_a_conversation_with_no_project_is_skipped_rather_than_saved_out_of_sight(tmp_path):
    info = conversation("claude:n2", tmp_path)
    info = SessionInfo(
        key=info.key, app=info.app, native_id=info.native_id, title=info.title, project="", started=info.started,
        updated=info.updated, messages=info.messages, size=0, path="",
    )
    source = FakeSource("claude", sessions=[info], messages={info.key: [Message("user", "你好")]})
    made, _, conversations = bridge(tmp_path, source)
    result = made.commit({"sessions": ["claude:n2"]})
    assert result["sessions"] == [] and "不知道它在哪个项目里" in result["skipped"][0]["reason"]
    assert conversations.load(saved_id("claude:n2")) is None


def test_without_a_chatgpt_plan_engine_conversations_are_skipped_with_a_sentence(tmp_path):
    info = conversation("claude:n1", tmp_path)
    source = FakeSource("claude", sessions=[info], messages={info.key: [Message("user", "你好")]})
    made = ImportBridge(MemoryStore(tmp_path / "memory"), None, make_sources=lambda: [source])
    result = made.commit({"sessions": ["claude:n1"]})
    assert result["sessions"] == [] and "没有 ChatGPT Plan 引擎" in result["skipped"][0]["reason"]


def test_undo_forgets_what_the_import_made_and_it_can_be_restored(tmp_path):
    source = FakeSource("claude", memories=[candidate("a", "命令", "构建用 make。"), candidate("b", "发布", "发布用 make ship。")])
    made, store, _ = bridge(tmp_path, source)
    notes = made.commit({"memories": ["a", "b"]})["notes"]
    assert made.undo({"ids": [note["id"] for note in notes]}) == {"forgotten": 2}
    assert store.entries() == [] and len(store.entries(status="forgotten")) == 2
    assert store.restore(notes[0]["id"], by="user").status == "active"


def test_a_key_whose_file_changed_is_skipped(tmp_path):
    source = FakeSource("claude", memories=[])
    made, store, _ = bridge(tmp_path, source)
    result = made.commit({"memories": ["gone"]})
    assert result["notes"] == [] and "找不到这条了" in result["skipped"][0]["reason"] and store.entries() == []
