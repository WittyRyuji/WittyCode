"""Video RPC ownership, bounded controls, and lifetime without a multimedia device."""
import asyncio

import pytest

from desktop.core import files
from desktop.core.app_core import AppError
from tests.app_support import ready_core


@pytest.mark.parametrize("suffix", sorted(files.PREVIEW_VIDEOS))
def test_video_suffixes_and_no_preview_byte_cap(tmp_path, suffix, monkeypatch):
    path = tmp_path / f"sample{suffix.upper()}"
    content = (b"\x47" + b"\0" * 187) * 3 if suffix == ".ts" else b"0123456789"
    path.write_bytes(content)
    monkeypatch.setattr(files, "PREVIEW_OFFICE_BYTES", 4)
    assert files.preview_kind(path) == "video"
    assert files.preview_size(path, "video") == len(content)
    folder = tmp_path / f"folder{suffix}"
    folder.mkdir()
    assert files.preview_kind(folder) is None


def test_typescript_is_still_a_text_preview(tmp_path):
    path = tmp_path / "sample.TS"
    path.write_bytes(b"export const name: string = 'demo';\n")
    assert files.preview_kind(path) is None
    assert files.read_for_panel(path).text.startswith("export const name")


def test_video_bridge_lease_and_lifetime(tmp_path):
    async def scenario():
        core, _, events, platform, _ = await ready_core(tmp_path)
        project = tmp_path / "proj"
        cid = (await core.call("session.new", {"projectId": str(project)}))["id"]
        for name in ("sample.mp4", "other.webm", "broken.mov", "notes.txt"):
            (project / name).touch()

        async def video(action, token="t1", **params):
            return await core.call("files.video", {"id": cid, "token": token, "action": action, **params})

        try:
            request = {"id": cid, "path": "sample.mp4"}
            shown = await core.call("files.read", request)
            assert (shown["kind"], shown["size"], shown["text"], shown["pages"]) == ("video", 0, "", [])
            with pytest.raises(AppError):
                await core.call("files.page", request)
            opened = await video("open", path="sample.mp4", width=2000)
            assert opened["duration"] == 21000 and opened["hasAudio"] and opened["frame"].startswith("data:image/jpeg")
            assert platform.video_requests == [("sample.mp4", 1280)]
            old_callback = platform.video_publish
            old_callback({"position": 0, "playing": False, "ended": False})
            assert events.of("files.video")[-1]["token"] == "t1"

            await video("play")
            await video("pause")
            await video("seek", position=532)
            await video("volume", volume=0.4, muted=True)
            assert [action for action, _ in platform.video_controls] == ["play", "pause", "seek", "volume"]
            with pytest.raises(AppError, match="位置不合法"):
                await video("seek", position=-1)
            with pytest.raises(AppError, match="音量不合法"):
                await video("volume", volume=True, muted=False)

            await video("open", "t2", path="other.webm", width=650)
            assert platform.video_closes == 1
            assert events.of("files.video")[-1] == {"token": "t1", "released": True}
            await video("close")  # the old panel's asynchronous cleanup is harmless
            await video("play")
            old_callback({"position": 100})
            assert platform.video_closes == 1 and len(events.of("files.video")) == 2
            assert len(platform.video_controls) == 4
            await core.call("files.read", {"id": cid, "path": "notes.txt"})
            assert platform.video_closes == 1 and core._video_preview == (cid, "t2")

            with pytest.raises(AppError, match="无法解码"):
                await video("open", "t3", path="broken.mov", width=500)
            assert events.of("files.video")[-1] == {"token": "t2", "released": True}
            assert platform.video_closes == 3 and core._video_preview is None
            await video("open", "t4", path="other.webm", width=500)
            await core.call("session.close", {"id": cid})
            assert platform.video_closes == 4
            cid = (await core.call("session.new", {"projectId": str(project)}))["id"]
            await video("open", "t5", path="other.webm", width=500)
        finally:
            await core.shutdown()
        assert platform.video_closes == 5 and platform.video_publish is None
    asyncio.run(scenario())


def test_closing_an_open_in_flight_cannot_publish(tmp_path, monkeypatch):
    async def scenario():
        core, _, events, platform, _ = await ready_core(tmp_path)
        path = tmp_path / "proj"
        (path / "sample.mp4").touch()
        cid = (await core.call("session.new", {"projectId": str(path)}))["id"]
        started, finish = asyncio.Event(), asyncio.Event()
        original = platform.video_open

        async def delayed(name, width, publish):
            started.set()
            await finish.wait()
            publish({"position": 1, "playing": True, "ended": False})
            # The real Qt platform cancels its own open immediately; model an already-sent late reply here.
            return {"duration": 1234}

        monkeypatch.setattr(platform, "video_open", delayed)
        try:
            command = {"id": cid, "token": "old", "action": "open", "path": "sample.mp4", "width": 800}
            pending = asyncio.create_task(core.call("files.video", command))
            await asyncio.wait_for(started.wait(), 5)
            await core.call("files.video", {**command, "action": "close"})
            finish.set()
            with pytest.raises(AppError, match="已关闭"):
                await pending
            monkeypatch.setattr(platform, "video_open", original)
            await core.call("files.video", {**command, "token": "new"})
            assert events.of("files.video") == [] and platform.video_closes == 1
        finally:
            await core.shutdown()
    asyncio.run(scenario())
