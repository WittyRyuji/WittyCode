"""Fakes for driving ``AppCore`` without a window: a platform, an event collector and a second 'engine'."""

from __future__ import annotations

import asyncio
import re
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from droid_sdk import ModelInfo, ModelProvider, ReasoningEffort

from desktop.core.app_core import AppCore
from desktop.core.attachments import Attachment, Rejection, Thumbnail, image_attachment, pdf_attachment, sniff_image, text_attachment
from desktop.core.files import FileProblem
from desktop.core.handoff import Handoff
from desktop.core.session_manager import SessionController, SessionManager, SessionState
from desktop.core.settings import AppSettings
from desktop.core.transcript import Transcript
from tests.fakes import FakeFactory, until


class FakePlatform:
    def __init__(self) -> None:
        self.folder: str | None = None
        self.files: list[str] = []
        self.opened: list[str] = []
        self.revealed: list[str] = []
        self.opened_paths: list[str] = []
        self.chosen_paths: list[str] = []
        self.shown_paths: list[str] = []
        self.calls: list[str] = []
        self.pdf_requests: list[tuple[str, int, int]] = []
        self.maximized = False

    async def pick_folder(self, start: str) -> str | None:
        return self.folder

    async def pick_files(self, start: str) -> list[str]:
        return list(self.files)

    def open_url(self, url: str) -> None:
        self.opened.append(url)

    def reveal(self, path: str) -> None:
        self.revealed.append(path)

    def open_path(self, path: str) -> None:
        self.opened_paths.append(path)

    def open_with(self, path: str) -> None:
        self.chosen_paths.append(path)

    def show_in_folder(self, path: str) -> None:
        self.shown_paths.append(path)

    def thumbnail(self, attachment: Attachment) -> Thumbnail | None:
        return Thumbnail("data:image/png;base64,AAAA", 4, 3) if attachment.kind == "image" else None

    def load_bytes(self, name: str, data: bytes) -> Attachment | Rejection:
        if not data:
            return Rejection(name, "文件是空的。")
        media = sniff_image(data)
        if media:
            return image_attachment(name, data, media)
        if data.startswith(b"%PDF"):  # the words of a fake PDF follow "TEXT:"; none: a scan
            return pdf_attachment(name, data, parsed_text=data.split(b"TEXT:", 1)[1].decode() if b"TEXT:" in data else "")
        try:
            return text_attachment(name, data.decode("utf-8"))
        except UnicodeDecodeError:
            return Rejection(name, "这是二进制文件。")

    def load_path(self, path: str) -> Attachment | Rejection:
        return self.load_bytes(Path(path).name, Path(path).read_bytes())

    def full_picture(self, attachment: Attachment) -> str | None:
        return "data:image/png;base64,FULL" if attachment.kind == "image" else None

    def pdf_pages(self, path: str) -> list[tuple[float, float]]:
        """A fake PDF is ``%PDF`` and ``pages=N``; anything else is a damaged file."""
        data = Path(path).read_bytes()
        found = re.search(rb"pages=(\d+)", data)
        if not data.startswith(b"%PDF") or found is None:
            raise FileProblem("这个文件不是有效的 PDF，或者已经损坏。")
        return [(595.0, 842.0 if n % 2 == 0 else 595.0) for n in range(int(found.group(1)))]

    def pdf_page(self, path: str, page: int, width: int) -> str:
        self.pdf_requests.append((Path(path).name, page, width))
        return f"data:image/png;base64,PAGE{page}W{width}"

    def window_minimize(self) -> None:
        self.calls.append("minimize")

    def window_toggle_maximize(self) -> bool:
        self.maximized = not self.maximized
        self.calls.append("maximize")
        return self.maximized

    def window_close(self) -> None:
        self.calls.append("close")

    def window_start_move(self) -> None:
        self.calls.append("move")

    def window_start_resize(self, edge: str) -> None:
        self.calls.append(f"resize:{edge}")

    def window_is_maximized(self) -> bool:
        return self.maximized


class Events:
    def __init__(self) -> None:
        self.items: list[tuple[str, dict[str, Any]]] = []

    def __call__(self, name: str, payload: dict[str, Any]) -> None:
        self.items.append((name, payload))

    def of(self, name: str) -> list[dict[str, Any]]:
        return [payload for event, payload in self.items if event == name]

    def last(self, name: str) -> dict[str, Any]:
        return self.of(name)[-1]

    def clear(self) -> None:
        self.items.clear()


class CodexLikeController(SessionController):
    """A controller of the 'other engine': the real controller logic on a fake session, labelled ``codex``."""

    engine = "codex"  # type: ignore[assignment]


class FakeCodexEngine:
    """What ``AppCore`` needs from the Codex engine, built on a second fake factory (so both engines can be driven)."""

    def __init__(self, settings: AppSettings, *, signed_in: bool = True) -> None:
        self.factory = FakeFactory()
        self.manager = SessionManager(settings, self.factory)
        self.signed_in = signed_in
        self.fail_open: BaseException | None = None
        self.opened: list[dict[str, Any]] = []
        self.threads: list[Any] = []
        self.peeks: dict[str, Any] = {}
        self.peeked: list[str] = []
        self.logins = 0
        self.models = [
            SimpleNamespace(id="gpt-5.3-codex", display_name="GPT-5.3 Codex", efforts=("low", "medium", "high"), default_effort="medium", context_window=400_000)
        ]
        self.supports_images = True
        self.path_changes = 0
        self.keep_server = False
        """A session is using the app-server of the old path: it is not stopped."""
        self._listeners: list[Any] = []

    # the same surface as ``CodexEngine``
    def add_listener(self, callback: Any) -> None:
        self.manager.add_listener(callback)

    def remove_listener(self, callback: Any) -> None:
        self.manager.remove_listener(callback)

    @property
    def controllers(self) -> list[Any]:
        return self.manager.controllers

    async def account(self) -> dict[str, Any]:
        if not self.signed_in:
            return {"cli": "found", "auth": "signed-out", "limits": []}
        return {"cli": "found", "auth": "chatgpt", "email": "a@b.c", "plan": "Pro", "limits": [{"label": "5 小时", "short": "5h", "usedPercent": 17, "resetsIn": "x"}]}

    async def login(self) -> dict[str, Any]:
        self.logins += 1
        self.signed_in = True
        return await self.account()

    async def cancel_login(self) -> dict[str, Any]:
        return await self.account()

    async def logout(self) -> dict[str, Any]:
        self.signed_in = False
        return await self.account()

    async def list_models(self) -> list[Any]:
        return list(self.models)

    async def path_changed(self) -> bool:
        self.path_changes += 1
        return not self.keep_server

    def model_info(self, model_id: str) -> Any:
        found = next((m for m in self.models if m.id == model_id), None)
        return None if found is None else SimpleNamespace(display_name=found.display_name, supports_images=self.supports_images)

    async def list_sessions(self, project: Any, limit: int = 50) -> list[Any]:
        return list(self.threads)

    async def peek_saved(self, thread_id: str) -> Any:
        """A thread read ahead for the cache: whatever the test put in ``peeks`` (nothing: the thread cannot be read)."""
        self.peeked.append(thread_id)
        return self.peeks.get(thread_id)

    async def open_new(
        self, project: Any, *, model: str, reasoning: str, autonomy: str, transcript: Transcript | None = None, handoff: Handoff | None = None
    ) -> SessionController:
        self.opened.append({"model": model, "reasoning": reasoning, "autonomy": autonomy, "handoff": handoff})
        self.factory.next_session = {"model": model, **({"fail_open": self.fail_open} if self.fail_open is not None else {})}
        controller = CodexLikeController(manager=self.manager, project=Path(project), handoff=handoff)
        if transcript is not None:
            controller.transcript = transcript
        await self.manager._register(controller)
        await self.manager._opened(controller, controller.open_new(model=model, reasoning=reasoning, autonomy="off", mode="auto"))
        if controller.state is SessionState.CLOSED:
            self.manager._forget(controller)
        return controller

    async def open_saved(
        self, project: Any, thread_id: str, *, title: str | None = None, modified: float | None = None
    ) -> SessionController:
        return await self.manager.open_saved(project, thread_id, title=title, modified=modified)

    async def close_all(self) -> None:
        await self.manager.close_all()


def make_core(
    tmp_path: Path,
    *,
    codex: bool = False,
    codex_signed_in: bool = True,
    providers: Any = None,
    max_open: int = 4,
    projects: bool = True,
    cache: Any = None,
    factory: FakeFactory | None = None,
    codex_engine: Any = None,
    devin_engine: Any = None,
    chatgpt_engine: Any = None,
    read_ahead: bool = False,
    memory: Any = None,
) -> tuple[AppCore, FakeFactory, Events, FakePlatform, Any]:
    factory = factory if factory is not None else FakeFactory()
    factory.models = [
        ModelInfo(
            id="claude-haiku-5-5",
            display_name="Haiku 5.5",
            short_display_name="Haiku 5.5",
            model_provider=ModelProvider.ANTHROPIC,
            supported_reasoning_efforts=(ReasoningEffort.LOW, ReasoningEffort.HIGH),
            default_reasoning_effort=ReasoningEffort.LOW,
        ),
        ModelInfo(
            id="gpt-5.4",
            display_name="GPT-5.4",
            short_display_name="GPT-5.4",
            model_provider=ModelProvider.OPENAI,
            supported_reasoning_efforts=(ReasoningEffort.MEDIUM,),
            default_reasoning_effort=ReasoningEffort.MEDIUM,
        ),
    ]
    # (``think_chinese`` off: its sentence would ride along with every message these tests compare word for word; the tests
    # of the language turn it on themselves)
    settings = AppSettings(max_open_sessions=max_open, default_model="claude-haiku-5-5", reasoning_effort="low", think_chinese=False)
    if projects:
        (tmp_path / "proj").mkdir(exist_ok=True)
        settings.add_project(tmp_path / "proj")
    manager = SessionManager(settings, factory)
    platform = FakePlatform()
    events = Events()
    engine = codex_engine if codex_engine is not None else (FakeCodexEngine(settings, signed_in=codex_signed_in) if codex else None)
    core = AppCore(
        manager, platform, codex=engine, devin=devin_engine, chatgpt=chatgpt_engine, providers=providers, persist=False, emit=events, cache=cache,
        read_ahead=read_ahead, memory=memory,
    )
    return core, factory, events, platform, engine


async def ready_core(tmp_path: Path, **options: Any) -> tuple[AppCore, FakeFactory, Events, FakePlatform, Any]:
    """A core after ``app.init`` with the model list loaded."""
    core, factory, events, platform, engine = make_core(tmp_path, **options)
    await core.call("app.init")
    await until(lambda: bool(events.of("models")))
    return core, factory, events, platform, engine


async def settled(events: Events, predicate: Any, timeout: float = 2.0) -> None:
    await until(predicate, timeout)
    await asyncio.sleep(0)
