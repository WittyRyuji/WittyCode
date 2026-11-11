"""Files named in the conversation: shown in the side panel, opened outside the window (``files.read`` / ``open`` / ``resolve``)."""

from __future__ import annotations

import asyncio
import base64

import pytest

from desktop.core import files as filetools
from desktop.core.app_core import AppError
from tests.app_support import ready_core
from tests.fakes import until


def run(coro):
    return asyncio.run(coro)


async def conversation(tmp_path):
    core, factory, events, platform, _ = await ready_core(tmp_path)
    cid = (await core.call("session.new", {"projectId": str(tmp_path / "proj")}))["id"]
    await until(lambda: any(p["id"] == cid and p["patch"].get("mark") == "ready" for p in events.of("session")))
    return core, cid, platform


def test_a_file_is_read_from_the_project_with_its_line(tmp_path):
    async def scenario():
        core, cid, _ = await conversation(tmp_path)
        (tmp_path / "proj" / "docs").mkdir()
        (tmp_path / "proj" / "docs" / "a.md").write_bytes("# 标题\n正文\n".encode("utf-8"))
        shown = await core.call("files.read", {"id": cid, "path": "docs/a.md:2-3"})
        assert shown["text"] == "# 标题\n正文\n" and shown["name"] == "a.md" and shown["markdown"] is True
        assert (shown["line"], shown["lineEnd"], shown["lines"], shown["truncated"]) == (2, 3, 2, False)
        assert shown["path"] == str(tmp_path / "proj" / "docs" / "a.md") and shown["dir"] == str(tmp_path / "proj" / "docs")
        assert shown["encoding"] == "UTF-8"
        await core.shutdown()

    run(scenario())


def test_a_file_that_cannot_be_shown_is_a_sentence_for_the_user(tmp_path):
    async def scenario():
        core, cid, _ = await conversation(tmp_path)
        (tmp_path / "proj" / "pic.bin").write_bytes(b"\x00\x01\x02")
        for path, fragment in (("nope.md", "找不到文件"), ("pic.bin", "不是文本文件"), (".", "文件夹")):
            with pytest.raises(AppError) as caught:
                await core.call("files.read", {"id": cid, "path": path})
            assert fragment in str(caught.value), path
        for bad in ("", "   ", 7, None):
            with pytest.raises(AppError):
                await core.call("files.read", {"id": cid, "path": bad})
        await core.shutdown()

    run(scenario())


def test_a_long_file_is_cut_at_the_lines_the_panel_shows(tmp_path, monkeypatch):
    async def scenario():
        monkeypatch.setattr(filetools, "PANEL_LINES", 5)
        core, cid, _ = await conversation(tmp_path)
        (tmp_path / "proj" / "long.txt").write_bytes("\n".join(f"line {n}" for n in range(1, 21)).encode())
        shown = await core.call("files.read", {"id": cid, "path": "long.txt"})
        assert shown["text"].split("\n") == [f"line {n}" for n in range(1, 6)]
        assert shown["lines"] == 20 and shown["truncated"] is True
        await core.shutdown()

    run(scenario())


def test_the_default_program_opens_documents_but_a_program_or_a_script_is_only_shown(tmp_path):
    async def scenario():
        core, cid, platform = await conversation(tmp_path)
        project = tmp_path / "proj"
        for name in ("note.md", "run.bat", "tool.exe", "script.py", "profile.ps1"):
            (project / name).write_bytes(b"x")
        (project / "folder").mkdir()
        assert await core.call("files.open", {"id": cid, "path": "note.md", "how": "default"}) == {"ok": True}
        assert platform.opened_paths == [str(project / "note.md")]
        assert await core.call("files.open", {"id": cid, "path": "folder", "how": "default"}) == {"ok": True}
        assert platform.opened_paths[-1] == str(project / "folder"), "a folder opens in the explorer"
        (project / "package.py").mkdir()
        assert await core.call("files.open", {"id": cid, "path": "package.py", "how": "default"}) == {"ok": True}
        assert platform.opened_paths.pop() == str(project / "package.py"), "a folder named like a script is still only a folder"
        for name in ("run.bat", "tool.exe", "script.py", "profile.ps1"):
            answer = await core.call("files.open", {"id": cid, "path": name, "how": "default"})
            assert answer["shown"] is True and name in answer["message"] and "不会直接运行" in answer["message"], name
        assert platform.opened_paths == [str(project / "note.md"), str(project / "folder")], "none of them was run"
        assert platform.shown_paths == [str(project / name) for name in ("run.bat", "tool.exe", "script.py", "profile.ps1")]
        await core.shutdown()

    run(scenario())


def test_choosing_the_program_and_showing_in_the_folder_work_for_any_file(tmp_path):
    async def scenario():
        core, cid, platform = await conversation(tmp_path)
        project = tmp_path / "proj"
        (project / "tool.exe").write_bytes(b"x")
        assert await core.call("files.open", {"id": cid, "path": "tool.exe", "how": "choose"}) == {"ok": True}
        assert await core.call("files.open", {"id": cid, "path": "tool.exe:3", "how": "reveal"}) == {"ok": True}
        assert platform.chosen_paths == [str(project / "tool.exe")] and platform.shown_paths == [str(project / "tool.exe")]
        assert platform.opened_paths == []
        with pytest.raises(AppError):
            await core.call("files.open", {"id": cid, "path": "tool.exe", "how": "run"})
        with pytest.raises(AppError):
            await core.call("files.open", {"id": cid, "path": "gone.exe", "how": "choose"})
        await core.shutdown()

    run(scenario())


def test_the_page_can_ask_where_a_path_is_to_copy_it(tmp_path):
    async def scenario():
        core, cid, _ = await conversation(tmp_path)
        (tmp_path / "proj" / "a.py").write_bytes(b"x")
        assert await core.call("files.resolve", {"id": cid, "path": "a.py:9"}) == {"path": str(tmp_path / "proj" / "a.py"), "isDir": False, "line": 9}
        with pytest.raises(AppError):
            await core.call("files.resolve", {"id": cid, "path": "b.py"})
        await core.shutdown()

    run(scenario())


def test_a_pdf_is_shown_page_by_page_not_read_as_text(tmp_path):
    async def scenario():
        core, cid, platform = await conversation(tmp_path)
        (tmp_path / "proj" / "report.pdf").write_bytes(b"%PDF-1.7 pages=3 \x00\x01\x02")
        shown = await core.call("files.read", {"id": cid, "path": "report.pdf"})
        assert shown["kind"] == "pdf" and shown["name"] == "report.pdf" and shown["text"] == "" and shown["lines"] == 0
        assert [(p["w"], p["h"]) for p in shown["pages"]] == [(595.0, 842.0), (595.0, 595.0), (595.0, 842.0)]
        assert platform.pdf_requests == [], "reading the PDF draws nothing: the page asks for the pages it scrolls to"
        page = await core.call("files.page", {"id": cid, "path": "report.pdf", "page": 2, "width": 800})
        assert page == {"url": "data:image/png;base64,PAGE2W800"} and platform.pdf_requests == [("report.pdf", 2, 800)]
        await core.shutdown()

    run(scenario())


def test_a_picture_is_sent_whole_with_its_type(tmp_path):
    async def scenario():
        core, cid, platform = await conversation(tmp_path)
        (tmp_path / "proj" / "shot.PNG").write_bytes(b"\x89PNG-bytes")
        (tmp_path / "proj" / "logo.svg").write_bytes(b"<svg/>")
        shown = await core.call("files.read", {"id": cid, "path": "shot.PNG"})
        assert shown["kind"] == "image" and shown["pages"] == [] and shown["size"] == len(b"\x89PNG-bytes")
        page = await core.call("files.page", {"id": cid, "path": "shot.PNG"})
        assert page["url"] == "data:image/png;base64," + base64.b64encode(b"\x89PNG-bytes").decode()
        svg = await core.call("files.page", {"id": cid, "path": "logo.svg"})
        assert svg["url"].startswith("data:image/svg+xml;base64,")
        assert platform.pdf_requests == []
        await core.shutdown()

    run(scenario())


def test_a_text_file_still_comes_back_as_text(tmp_path):
    async def scenario():
        core, cid, _ = await conversation(tmp_path)
        (tmp_path / "proj" / "a.txt").write_bytes(b"hello\n")
        shown = await core.call("files.read", {"id": cid, "path": "a.txt"})
        assert shown["kind"] == "text" and shown["pages"] == [] and shown["text"] == "hello\n"
        with pytest.raises(AppError):
            await core.call("files.page", {"id": cid, "path": "a.txt"})
        await core.shutdown()

    run(scenario())


def test_a_preview_that_cannot_be_drawn_is_a_sentence_for_the_user(tmp_path, monkeypatch):
    async def scenario():
        monkeypatch.setattr(filetools, "PREVIEW_IMAGE_BYTES", 10)
        monkeypatch.setattr(filetools, "PREVIEW_PDF_BYTES", 30)
        core, cid, _ = await conversation(tmp_path)
        project = tmp_path / "proj"
        (project / "broken.pdf").write_bytes(b"not a pdf at all")
        (project / "huge.pdf").write_bytes(b"%PDF pages=1 " + b"x" * 100)
        (project / "huge.png").write_bytes(b"x" * 100)
        (project / "folder.pdf").mkdir()
        for path, fragment in (("broken.pdf", "不是有效的 PDF"), ("huge.pdf", "太大"), ("huge.png", "太大")):
            with pytest.raises(AppError) as caught:
                await core.call("files.read", {"id": cid, "path": path})
            assert fragment in str(caught.value), path
        with pytest.raises(AppError) as caught:
            await core.call("files.read", {"id": cid, "path": "folder.pdf"})
        assert "文件夹" in str(caught.value), "a folder named like a PDF is still a folder"
        with pytest.raises(AppError):
            await core.call("files.page", {"id": cid, "path": "huge.png"})
        for bad in ({"page": -1}, {"page": "1"}, {"page": True}, {"width": "800"}, {"width": 1.5}):
            with pytest.raises(AppError):
                await core.call("files.page", {"id": cid, "path": "broken.pdf", **bad})
        await core.shutdown()

    run(scenario())
