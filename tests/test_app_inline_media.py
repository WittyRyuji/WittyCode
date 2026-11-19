"""Restricted media RPCs and opened-file semantics, without a window or a decoder."""
from __future__ import annotations

import asyncio
import base64
import os
import struct
import subprocess
import zlib

import pytest

from desktop.core import files
from desktop.core.app_core import AppError
from tests.app_support import ready_core


def png(rgb: bytes = b"\xe2\x14\x38", width: int = 3, height: int = 2) -> bytes:
    def chunk(kind: bytes, data: bytes) -> bytes:
        return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data))
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress((b"\0" + rgb * width) * height)) + chunk(b"IEND", b""))


def gif() -> bytes:
    header = b"GIF89a\x02\x00\x01\x00\x80\x00\x00\x00\x00\x00\xff\x00\x00"
    loop = b"\x21\xff\x0bNETSCAPE2.0\x03\x01\x00\x00\x00"
    frame = b"\x21\xf9\x04\x00\x10\x00\x00\x00\x2c\x00\x00\x00\x00\x02\x00\x01\x00\x00\x02\x02"
    return header + loop + frame + b"\x04\x0a\x00" + frame + b"\x4c\x0a\x00\x3b"


BMFF = b"\x00\x00\x00\x18ftypisom" + b"\0" * 12  # container prefix; not playable media


def payload(url: str) -> bytes:
    return base64.b64decode(url.split(",", 1)[1])


@pytest.mark.parametrize("path", [
    "/outside.png", r"\outside.png", "D:drive-relative.png", r"D:\absolute.png", r"\\server\share\media.png",
    "//server/share/media.png", r"\\?\C:\device.png", "file:///C:/image.png", "https://example.invalid/x.png",
    "../outside.png", "outputs/../image.png", r"..\outside.png", "outputs/%2e%2e/image.png", "././file.png",
    "image.png:2", "allowed.png:private.png", "outputs. /image.png", "NUL.png", "COM¹.png", "image.png\0.png",
])
def test_restricted_paths_rejected_before_open(tmp_path, monkeypatch, path):
    def not_open(*args):
        pytest.fail("unsafe path reached the filesystem")
    monkeypatch.setattr(files, "_open_inline_windows", not_open)
    monkeypatch.setattr(files, "_open_inline_posix", not_open)
    with pytest.raises(files.FileProblem, match="普通本地文件"):
        files.inline_image_url(path, tmp_path)


def test_links_and_junctions_are_not_followed(tmp_path):
    root, outside = tmp_path / "project", tmp_path / "outside"
    root.mkdir()
    outside.mkdir()
    (outside / "image.png").write_bytes(png())
    os.link(outside / "image.png", root / "hard.png")
    with pytest.raises(files.FileProblem, match="普通本地文件"):
        files.inline_image_url("hard.png", root)

    jump = root / "linked"
    if os.name == "nt":
        # Junction creation on the test volume doesn't need symlink privileges.
        subprocess.run([os.environ["COMSPEC"], "/d", "/c", "mklink", "/j", str(jump), str(outside)],
                       capture_output=True, check=True, timeout=10)
    else:
        jump.symlink_to(outside, target_is_directory=True)
    try:
        for project, ref in [(root, "linked/image.png"), (jump, "image.png")]:
            with pytest.raises(files.FileProblem):
                files.inline_image_url(ref, project)
    finally:
        if os.name == "nt":
            jump.rmdir()
        else:
            jump.unlink()


def test_symbolic_link_at_the_leaf_is_refused(tmp_path):
    outside = tmp_path / "image.png"
    outside.write_bytes(png())
    project = tmp_path / "project"
    project.mkdir()
    link = project / "link.png"
    try:
        link.symlink_to(outside)
    except OSError as error:
        pytest.skip(f"Symlink creation unavailable: {error}")
    with pytest.raises(files.FileProblem):
        files.inline_image_url("link.png", project)


def test_image_size_format_and_original_animated_bytes(tmp_path, monkeypatch):
    (tmp_path / "图 像.PNG").write_bytes(png())
    (tmp_path / "motion.gif").write_bytes(gif())
    (tmp_path / "wrong.png").write_bytes(gif())
    assert payload(files.inline_image_url("./图 像.PNG", tmp_path)) == png()
    assert payload(files.inline_image_url("motion.gif", tmp_path)) == gif()
    with pytest.raises(files.FileProblem, match="格式不符"):
        files.inline_image_url("wrong.png", tmp_path)
    with pytest.raises(files.FileProblem, match="找不到"):
        files.inline_image_url("missing.png", tmp_path)
    monkeypatch.setattr(files, "INLINE_IMAGE_PIXELS", 5)
    with pytest.raises(files.FileProblem, match="像素太多"):
        files.inline_image_url("图 像.PNG", tmp_path)
    monkeypatch.setattr(files, "INLINE_IMAGE_BYTES", 20)
    with pytest.raises(files.FileProblem, match="文件超过"):
        files.inline_image_url("motion.gif", tmp_path)


def test_opened_inline_video_is_usable_by_qt_without_its_path(tmp_path, monkeypatch):
    from PySide6.QtCore import QFile, QIODevice

    clip = tmp_path / "clip.mp4"
    clip.write_bytes(BMFF)
    # Exercise the exact fd bridge (read and seek after the core closes its original). No Qt application is created.
    source = files.open_inline_video("clip.mp4", tmp_path)
    with source.file:
        fd = os.dup(source.file.fileno())
    device = QFile()
    try:
        assert device.open(fd, QIODevice.OpenModeFlag.ReadOnly, QFile.FileHandleFlag.DontCloseHandle)
        assert bytes(device.peek(12)) == BMFF[:12]
        assert device.seek(4)
        assert bytes(device.read(4)) == b"ftyp"
    finally:
        device.close()
        os.close(fd)
    clip.write_bytes(b"#EXTM3U\nhttps://example.invalid/remote.ts\n")
    with pytest.raises(files.FileProblem, match="本地视频容器"):
        files.open_inline_video("clip.mp4", tmp_path)
    monkeypatch.setattr(files, "INLINE_VIDEO_BYTES", 4)
    with pytest.raises(files.FileProblem, match="文件超过"):
        files.open_inline_video("clip.mp4", tmp_path)


def test_inline_media_uses_the_named_conversation_and_not_the_file_panel(tmp_path, monkeypatch):
    async def scenario():
        core, _, _, platform, _ = await ready_core(tmp_path)
        root, other = tmp_path / "proj", tmp_path / "second"
        try:
            other.mkdir()
            for directory, rgb in [(root, b"\xf0\x00\x00"), (other, b"\x00\xf0\x00")]:
                (directory / "outputs").mkdir()
                (directory / "outputs" / "image.png").write_bytes(png(rgb))
            core.settings.add_project(other)
            first = (await core.call("session.new", {"projectId": str(root)}))["id"]
            second = (await core.call("session.new", {"projectId": str(other)}))["id"]
            (root / "outputs" / "clip.mp4").write_bytes(BMFF)
            (root / "notes.txt").write_text("line 1\nline 2", encoding="utf-8")
            for cid, project in [(first, root), (second, other)]:
                reply = await core.call("files.inlineImage", {"id": cid, "path": r"outputs\image.png"})
                assert payload(reply["url"]) == (project / "outputs" / "image.png").read_bytes()
            with pytest.raises(AppError, match="会话已经关闭"):
                await core.call("files.inlineImage", {"id": "stale", "path": "outputs/image.png"})

            original = platform.video_open
            sources = []

            async def opened(name, width, publish):
                assert isinstance(name, files.InlineVideoSource) and name.file.read(12) == BMFF[:12]
                sources.append(name)
                return await original(name, width, publish)

            monkeypatch.setattr(platform, "video_open", opened)
            video = dict(action="open", path="outputs/clip.mp4", token="test", width=500, inline=True)
            with pytest.raises(AppError, match="找不到"):
                await core.call("files.video", {**video, "id": second})
            assert platform.video_requests == []
            await core.call("files.video", {**video, "id": first})
            assert sources[0].file.closed and core._video_preview == (first, "test")
            read = await core.call("files.read", {"id": first, "path": "notes.txt:2"})
            assert read["line"] == 2 and "line 2" in read["text"]
            await core.call("files.inlineImage", {"id": second, "path": "outputs/image.png"})
            assert core._video_preview == (first, "test")
            await core.call("session.close", {"id": first})
            assert core._video_preview is None and platform.video_publish is None
        finally:
            await core.shutdown()
    asyncio.run(scenario())
