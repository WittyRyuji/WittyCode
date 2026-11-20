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


def gif(width: int = 2, height: int = 1) -> bytes:
    size = struct.pack("<HH", width, height)
    header = b"GIF89a" + size + b"\x80\x00\x00\x24\x9c\x92\xe0\x70\x37"
    loop = b"\x21\xff\x0bNETSCAPE2.0\x03\x01\x00\x00\x00"
    descriptor = b"\x21\xf9\x04\x00\x14\x00\x00\x00\x2c\x00\x00\x00\x00" + size + b"\x00\x02"

    def frame(color: int) -> bytes:
        # Each pixel uses a clear code (4) followed by the palette index, keeping LZW codes at 3 bits.
        packed = bytearray()
        bits = count = 0
        for i in range(width * height + 1):
            code, n = (5, 3) if i == width * height else (4 | color << 3, 6)
            bits |= code << count
            count += n
            if count >= 8:
                packed.append(bits & 255)
                bits >>= 8
                count -= 8
        if count:
            packed.append(bits)
        blocks = b"".join(bytes([len(chunk)]) + chunk for chunk in (packed[i : i + 255] for i in range(0, len(packed), 255)))
        return descriptor + blocks + b"\x00"

    return header + loop + frame(0) + frame(1) + b"\x3b"


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
        files.inline_image(path, tmp_path)


def test_links_and_junctions_are_not_followed(tmp_path):
    root, outside = tmp_path / "project", tmp_path / "outside"
    root.mkdir()
    outside.mkdir()
    (outside / "image.png").write_bytes(png())
    os.link(outside / "image.png", root / "hard.png")
    with pytest.raises(files.FileProblem, match="普通本地文件"):
        files.inline_image("hard.png", root)

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
                files.inline_image(ref, project)
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
        files.inline_image("link.png", project)


def test_image_size_format_and_original_animated_bytes(tmp_path, monkeypatch):
    (tmp_path / "图 像.PNG").write_bytes(png())
    (tmp_path / "motion.gif").write_bytes(gif())
    (tmp_path / "wrong.png").write_bytes(gif())
    from PySide6.QtGui import QImage

    assert files.inline_image("./图 像.PNG", tmp_path) == {"url": "data:image/png;base64," + base64.b64encode(png()).decode()}
    motion = files.inline_image("motion.gif", tmp_path)
    assert motion["url"].startswith("data:image/png;base64,")
    poster = QImage.fromData(payload(motion["url"]))
    assert (poster.width(), poster.height()) == (2, 1)
    assert poster.pixelColor(0, 0).getRgb()[:3] == (36, 156, 146)
    assert payload(motion["animation"]) == gif()
    assert payload(files.image_data_url(tmp_path / "motion.gif")) == gif(), "sidebar still receives the original GIF"
    with pytest.raises(files.FileProblem, match="格式不符"):
        files.inline_image("wrong.png", tmp_path)
    with pytest.raises(files.FileProblem, match="找不到"):
        files.inline_image("missing.png", tmp_path)
    monkeypatch.setattr(files, "INLINE_IMAGE_PIXELS", 5)
    with pytest.raises(files.FileProblem, match="像素太多"):
        files.inline_image("图 像.PNG", tmp_path)
    monkeypatch.setattr(files, "INLINE_IMAGE_BYTES", 20)
    with pytest.raises(files.FileProblem, match="文件超过"):
        files.inline_image("motion.gif", tmp_path)


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


def test_video_posters_are_serial_and_do_not_take_the_playback_lease(tmp_path, monkeypatch):
    async def scenario():
        core, _, events, platform, _ = await ready_core(tmp_path)
        project = tmp_path / "proj"
        cid = (await core.call("session.new", {"projectId": str(project)}))["id"]
        for name in ("one.mp4", "two.mp4", "sidebar.mp4"):
            (project / name).write_bytes(BMFF)
        entered, finish = asyncio.Event(), asyncio.Event()
        sources = []

        async def poster(source, width):
            assert isinstance(source, files.InlineVideoSource) and source.file.read(12) == BMFF[:12]
            assert width == 1280
            assert all(previous.file.closed for previous in sources)
            sources.append(source)
            if len(sources) == 1:
                entered.set()
                await finish.wait()
            return {"frame": "one shot"}

        monkeypatch.setattr(platform, "video_poster", poster, raising=False)
        lease = {"id": cid, "token": "sidebar"}
        params = {"id": cid, "width": 1600}
        tasks = []
        try:
            await core.call("files.video", {**lease, "action": "open", "path": "sidebar.mp4", "width": 640})
            await core.call("files.video", {**lease, "action": "play"})
            with pytest.raises(AppError, match="普通本地文件"):
                await core.call("files.inlineVideoPoster", {**params, "path": "../one.mp4"})
            for name in ("one.mp4", "two.mp4"):
                tasks.append(asyncio.create_task(core.call("files.inlineVideoPoster", {**params, "path": name})))
            await asyncio.wait_for(entered.wait(), 3)
            assert len(sources) == 1
            await core.call("files.video", {**lease, "action": "seek", "position": 200})
            finish.set()
            assert await asyncio.gather(*tasks) == [{"frame": "one shot"}] * 2
            assert len(sources) == 2 and all(source.file.closed for source in sources)
            assert core._video_preview == (cid, "sidebar") and platform.video_closes == 0
            assert [action for action, _ in platform.video_controls] == ["play", "seek"]
            assert events.of("files.video") == []
        finally:
            finish.set()
            await asyncio.gather(*tasks, return_exceptions=True)
            await core.shutdown()
    asyncio.run(scenario())
