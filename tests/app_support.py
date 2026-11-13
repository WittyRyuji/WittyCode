"""Fakes for driving ``AppCore`` without a window: a platform, an event collector, and the application's own loop signed in
to a ChatGPT Plan service on this machine (``tests/fake_responses``)."""

from __future__ import annotations

import asyncio
import re
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from desktop.core.app_core import AppCore
from desktop.core.attachments import Attachment, Rejection, Thumbnail, image_attachment, pdf_attachment, sniff_image, text_attachment
from desktop.core.files import FileProblem
from desktop.core.settings import AppSettings
from tests.chatgpt_support import make_engine
from tests.fake_responses import FakeResponses
from tests.fakes import until

MODEL = "chatgpt:gpt-6.1-sol"
"""The page id of the model a new test session starts with (the first of ``fake_responses.DEFAULT_MODELS``)."""
OTHER_MODEL = "chatgpt:gpt-6-luna"


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


@dataclass
class Plan:
    """The ChatGPT Plan service of a test core: script its answers (``fake.script(say(...))``, ``("hold", name)``)."""

    fake: FakeResponses | None
    engine: Any

    def script(self, *turns: Any) -> None:
        assert self.fake is not None, "this core has no ChatGPT Plan service of its own"
        self.fake.script(*turns)

    def release(self, name: str) -> None:
        assert self.fake is not None
        self.fake.release(name)

    @property
    def requests(self) -> list[dict[str, Any]]:
        return self.fake.requests if self.fake is not None else []

    def stop(self) -> None:
        if self.fake is not None and self.fake._server is not None:
            self.fake.__exit__(None, None, None)
            self.fake._server.shutdown()
            self.fake._server.server_close()
            self.fake._server = None


def make_core(
    tmp_path: Path,
    *,
    providers: Any = None,
    max_open: int = 4,
    projects: bool = True,
    cache: Any = None,
    devin_engine: Any = None,
    factory_engine: Any = None,
    chatgpt_engine: Any = None,
    plan: bool = True,
    signed_in: bool = True,
    models: list[dict[str, Any]] | None = None,
    memory: Any = None,
    settings: AppSettings | None = None,
) -> tuple[AppCore, Plan, Events, FakePlatform, Any]:
    """``plan``: the own loop, signed in to a ChatGPT Plan service of its own (unless ``chatgpt_engine`` is given). Returns
    (core, plan, events, platform, the own loop's engine)."""
    # (``think_chinese`` off: its sentence would ride along with every message these tests compare word for word; the tests
    # of the language turn it on themselves)
    if settings is None:
        settings = AppSettings(max_open_sessions=max_open, default_model=MODEL if plan else "", reasoning_effort="medium", think_chinese=False)
    if projects:
        (tmp_path / "proj").mkdir(exist_ok=True)
        settings.add_project(tmp_path / "proj")
    fake = None
    engine = chatgpt_engine
    if engine is None and plan:
        fake = FakeResponses(models=models).__enter__()
        engine = make_engine(tmp_path, fake, signed_in=signed_in, settings=settings)
    elif engine is not None:
        engine.settings = settings
    service = Plan(fake, engine)
    platform = FakePlatform()
    events = Events()
    if factory_engine is not None:
        settings.factory_enabled = True
        factory_engine.settings = settings
    core = AppCore(
        settings, platform, factory=factory_engine, devin=devin_engine, chatgpt=engine, providers=providers, persist=False, emit=events, cache=cache, memory=memory
    )
    shutdown = core.shutdown

    async def shutdown_and_stop() -> None:
        try:
            await shutdown()
        finally:
            service.stop()

    core.shutdown = shutdown_and_stop  # type: ignore[method-assign]
    return core, service, events, platform, engine


def save_conversation(engine: Any, project: Path, session_id: str, title: str, *, minutes_ago: float = 5, model: str = "gpt-6.1-sol") -> str:
    """A conversation of the own loop saved ``minutes_ago`` (one question, one answer), as the engine lists it; returns its
    sidebar row id."""
    import time

    from desktop.core.harness.store import SavedSession, SavedTurn
    from desktop.core.settings import normalize_folder

    stamp = time.time() - minutes_ago * 60
    items = [
        {"type": "message", "role": "user", "content": [{"type": "input_text", "text": f"about {title}"}]},
        {"type": "message", "role": "assistant", "content": [{"type": "output_text", "text": f"{title}: done."}]},
    ]
    engine.sessions.save(
        SavedSession(id=session_id, project=normalize_folder(project), title=title, model=model, created_at=stamp, modified_at=stamp,
                     items=items, turns=[SavedTurn(start=0, at=stamp, finished=stamp)])
    )
    return "cg:" + session_id


async def ready_core(tmp_path: Path, **options: Any) -> tuple[AppCore, Plan, Events, FakePlatform, Any]:
    """A core after ``app.init`` with the model list loaded."""
    core, service, events, platform, engine = make_core(tmp_path, **options)
    await core.call("app.init")
    await until(lambda: bool(events.of("models")))
    return core, service, events, platform, engine


async def settled(events: Events, predicate: Any, timeout: float = 5.0) -> None:
    await until(predicate, timeout)
    await asyncio.sleep(0)
