"""Real Qt WebEngine/QWebChannel regression, with generated media and a loopback fake model."""
from __future__ import annotations

import asyncio
import json
import os
import shutil
from pathlib import Path

import pytest
from PySide6.QtCore import QPoint, Qt
from PySide6.QtGui import QColor, QImage, QPainter
from PySide6.QtMultimedia import QMediaPlayer
from PySide6.QtTest import QTest

from tests.e2e_support import open_rig
from tests.fake_responses import call, say
from tests.test_app_e2e import Model, new_conversation
from tests.test_app_inline_media import gif, png
from tests.test_videoview import make_clip
from tests.ui_support import outside_task, run, wait_for

pytestmark = pytest.mark.ui


def record_file_calls(rig, monkeypatch):
    original = rig.core.call
    calls = []

    async def record(method, params=None):
        if method.startswith("files.") or method == "shell.openExternal":
            calls.append((method, dict(params or {})))
        return await original(method, params)

    monkeypatch.setattr(rig.core, "call", record)
    return calls


async def natural_width(rig, selector, width):
    await wait_for(lambda: rig.js(f"document.querySelector({json.dumps(selector)})?.naturalWidth === {width}"),
                   10, f"{selector} decoded at {width}px")


def test_inline_images_and_missing_refs_use_the_message_project(qapp, loop, tmp_path, monkeypatch):
    async def scenario():
        rig = await open_rig(tmp_path, size=(1450, 900))
        try:
            calls = record_file_calls(rig, monkeypatch)
            root, other = rig.project, tmp_path / "another"
            (root / "outputs").mkdir()
            (other / "outputs").mkdir(parents=True)
            (root / "outputs" / "result one.png").write_bytes(png(width=3))
            (other / "outputs" / "result one.png").write_bytes(png(width=7))
            (root / "outputs" / "loop.gif").write_bytes(gif())
            (tmp_path / "outside.png").write_bytes(png())
            (root / "notes.md").write_text("# Plain file\n\n![Do not load](outputs/result%20one.png)\n\n[Open clip](outputs/sample.mp4)\n", encoding="utf-8")

            model = await new_conversation(rig)
            first = rig.core.order[0].cid
            model.next(say("![PNG](outputs/result%20one.png)\n\n![GIF](outputs/loop.gif)\n\n![Missing](outputs/missing.png)\n\n"
                           "![Outside](../outside.png)\n\n![Remote](https://example.invalid/remote.png)\n\n"
                           "See `notes.md`."), hold=False)
            await rig.send("Show the local pictures")
            await natural_width(rig, ".inline-picture img[alt='PNG']", 3)
            await natural_width(rig, ".inline-picture img[alt='GIF']", 2)
            assert await rig.js("document.querySelector('.inline-picture img[alt=GIF]').src.startsWith('data:image/gif;base64,')")
            await rig.wait_text(".inline-media-error", "找不到")
            assert await rig.texts(".text-block .img-link") == ["[图片：Outside]", "[图片：Remote]"]
            assert sorted((m, p["id"], p["path"]) for m, p in calls) == [
                ("files.inlineImage", first, f"outputs/{name}") for name in ("loop.gif", "missing.png", "result one.png")
            ]

            await rig.click(".text-block code.pathref")
            await rig.wait_text(".fp-doc", "Plain file")
            assert await rig.count(".fp-doc .inline-media, .fp-doc img") == 0
            assert await rig.text(".fp-doc .pathref") == "Open clip"
            assert [m for m, _ in calls].count("files.read") == 1

            await rig.core.call("project.add", {"path": str(other)})
            second = (await rig.core.call("session.new", {"projectId": str(other)}))["id"]
            await rig.wait_for(".tab", 2)
            await rig.wait_text(".statusbar .st-left", "就绪")
            Model(rig).next(say("![PNG](outputs/result%20one.png)"), hold=False)
            await rig.send("Show the other picture")
            await natural_width(rig, ".inline-picture img[alt='PNG']", 7)
            assert [(p["id"], p["path"]) for method, p in calls if method == "files.inlineImage"][-1] == (second, "outputs/result one.png")
            await rig.click(".tab", 0)
            await natural_width(rig, ".inline-picture img[alt='PNG']", 3)
        finally:
            await rig.finish()
    run(loop, scenario())


def test_plan_markdown_does_not_auto_load_media(qapp, loop, tmp_path, monkeypatch):
    async def scenario():
        rig = await open_rig(tmp_path)
        try:
            calls = record_file_calls(rig, monkeypatch)
            (rig.project / "result.png").write_bytes(png())
            model = await new_conversation(rig)
            cid = rig.core.order[0].cid
            await rig.core.call("session.choose", {"id": cid, "field": "mode", "value": "spec"})
            model.next(call("submit_plan", {"plan": "# Draft\n\n![Still](result.png)\n\n[Clip](sample.mp4)"}), hold=False)
            await rig.send("Draft the plan")
            await rig.wait_for(".plan-approval-body .img-link", 1)
            assert await rig.count(".plan-approval-body .inline-media, .plan-approval-body img") == 0
            assert await rig.text(".plan-approval-body .pathref") == "Clip"
            assert calls == []
        finally:
            await rig.finish()
    run(loop, scenario())


async def drag_progress(rig, selector, fraction):
    assert await rig._center(selector, 0) is not None  # scroll the input into the real viewport
    raw = await rig.js(f"""(() => {{
        const el = document.querySelector({json.dumps(selector)}), r = el.getBoundingClientRect();
        const track = r.width - 16;
        return JSON.stringify([r.left + 8 + track * (el.valueAsNumber / Number(el.max)),
                               r.left + 8 + track * {fraction}, r.top + r.height / 2]);
    }})()""")
    start, finish, y = json.loads(raw)
    zoom = rig.window.view.zoomFactor()
    first, last = [QPoint(round(x * zoom), round(y * zoom)) for x in (start, finish)]
    widget = rig._target()
    await outside_task(lambda: QTest.mousePress(widget, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier, first))
    await outside_task(lambda: QTest.mouseMove(widget, last))
    await outside_task(lambda: QTest.mouseRelease(widget, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier, last))


def make_preview_chart(path: Path) -> None:
    image = QImage(384, 144, QImage.Format.Format_RGB32)
    image.fill(QColor("#162635"))
    with QPainter(image) as painter:
        for y in (32, 55, 78, 101, 124):
            painter.fillRect(20, y, 344, 1, QColor("#3b5369"))
        for i, height in enumerate((24, 46, 35, 59, 50, 72, 62, 80)):
            painter.fillRect(24 + i * 42, 124 - height, 30, height, QColor("#46d5ba" if i % 2 else "#65a9f8"))
    assert image.save(str(path))


def test_inline_mp4_plays_pauses_seeks_and_releases_its_decoder(qapp, loop, tmp_path, monkeypatch):
    async def scenario():
        rig = await open_rig(tmp_path, size=(1450, 1000))
        try:
            calls = record_file_calls(rig, monkeypatch)
            directory = rig.project / "outputs"
            directory.mkdir()
            clip = await make_clip(directory / "first.mp4")
            shutil.copyfile(clip, directory / "second.mp4")
            make_preview_chart(directory / "signal.png")
            (rig.project / "notes.txt").write_text("This is still a text file.\n", encoding="utf-8")
            model = await new_conversation(rig)
            cid = rig.core.order[0].cid
            if await rig.exists(".sidebar"):
                await rig.click("[aria-label='切换侧栏']")
            model.next(say("### Local media preview\n\n![Synthetic signal](outputs/signal.png)\n\n"
                           "![First clip](outputs/first.mp4)\n\n[Second](outputs/second.mp4)\n\n![Missing](outputs/missing.mp4)\n\n"
                           "[Outside clip](../outside.mp4)\n\n`notes.txt` and `outputs/second.mp4`"), hold=False)
            await rig.send("Show the image and the video together")
            button = ".inline-video-activate"
            chart = ".inline-picture img[alt='Synthetic signal']"
            picture = ".inline-video .fp-video-stage img"
            play = ".inline-video .fp-video-play"
            slider = ".inline-video .fp-video-time"
            await rig.wait_for(button, 3)
            await natural_width(rig, chart, 384)
            assert await rig.text(".text-block .img-link") == "Outside clip"
            assert rig.core._video_preview is None
            assert calls == [("files.inlineImage", {"id": cid, "path": "outputs/signal.png"})]
            assert not await rig.exists(".fp-video-stage")

            await rig.click(button, 0)
            await natural_width(rig, picture, 64)
            first_lease = rig.core._video_preview
            first = rig.window.platform._video
            assert first_lease[0] == cid and first is not None
            assert first.player.playbackState() == QMediaPlayer.PlaybackState.PausedState
            poster = await rig.js(f"document.querySelector('{picture}').src")
            assert poster.startswith("data:image/jpeg;base64,")
            assert await rig.js(f"!document.querySelector('{slider}').disabled")
            await rig.wait_for(f"{play}[aria-label='播放']", 1)
            assert not await rig.exists(".sidebar, .filepanel, .conversation-rail")
            assert await rig.js(f"""(() => {{
                const reply = document.querySelector('.conversation-column .text-block');
                const viewport = reply.closest('.scroller').getBoundingClientRect();
                return {json.dumps([chart, picture, play, slider])}.every(selector => {{
                    const box = reply.querySelector(selector)?.getBoundingClientRect();
                    return box && box.width > 0 && box.height > 0 && box.top >= viewport.top && box.bottom <= viewport.bottom;
                }});
            }})()"""), "the chart, decoded video and controls are visible in one assistant reply"
            if evidence := os.environ.get("WITTYCODE_TEST_EVIDENCE"):
                raw = await rig.js(f"""(() => {{
                    const r = document.querySelector('{picture}').getBoundingClientRect();
                    return JSON.stringify([r.x + r.width / 2, r.y + r.height / 2]);
                }})()""")
                x, y = json.loads(raw)
                zoom = rig.window.view.zoomFactor()

                def save_painted_frame():
                    # The DOM can be ready before the offscreen compositor has painted the blue first frame.
                    shot = rig.window.view.grab()
                    scale = zoom * shot.devicePixelRatio()
                    pixel = shot.toImage().pixelColor(round(x * scale), round(y * scale))
                    if max(abs(got - target) for got, target in zip(pixel.getRgb()[:3], (45, 112, 155))) > 24:
                        return False
                    assert shot.save(str(Path(evidence) / "inline-media-paused.png"))
                    return True

                await wait_for(save_painted_frame, 5, "the paused frame to appear in the offscreen screenshot")

            await rig.click(play)
            await wait_for(lambda: first.player.playbackState() == QMediaPlayer.PlaybackState.PlayingState
                           and first.player.position() > 100, 3, "MP4 playing and advancing")
            await rig.wait_for(f"{play}[aria-label='暂停']", 1)
            await rig.click(play)
            await rig.wait_for(f"{play}[aria-label='播放']", 1)
            assert first.player.playbackState() == QMediaPlayer.PlaybackState.PausedState
            paused = first.player.position()
            await asyncio.sleep(0.15)
            assert first.player.position() == paused

            await drag_progress(rig, slider, 0.78)
            await wait_for(lambda: first.player.position() >= 650, 4, "Qt position after dragging the progress thumb")
            await wait_for(lambda: rig.js(f"document.querySelector('{picture}').src !== {json.dumps(poster)}"),
                           4, "a frame from the second half of the video")
            assert first.player.playbackState() == QMediaPlayer.PlaybackState.PausedState
            assert any(m == "files.video" and p["action"] == "seek" and p["position"] >= 650 for m, p in calls)

            await rig.click_text(".text-block code.pathref", "notes.txt")
            await rig.wait_text(".fp-text", "still a text file")
            assert rig.core._video_preview == first_lease and rig.window.platform._video is first
            await rig.click(".filepanel [aria-label='关闭文件面板']")
            await rig.click(button, 1)
            await natural_width(rig, ".inline-video[title='outputs/second.mp4'] .fp-video-stage img", 64)
            assert await rig.count(picture) == 1
            assert rig.core._video_preview != first_lease and first._closed and first._source_fd is None
            assert not await rig.exists(".inline-video[title='outputs/first.mp4'] .fp-video-stage")

            # The same decoder is shared with the pre-existing, explicitly opened file panel.
            await rig.click_text(".text-block code.pathref", "outputs/second.mp4")
            await natural_width(rig, ".filepanel .fp-video-stage img", 64)
            assert await rig.count(picture) == 0
            await rig.click(".filepanel [aria-label='关闭文件面板']")
            await wait_for(lambda: rig.window.platform._video is None)

            await rig.click(button, 2)
            await rig.wait_text(".inline-video .inline-media-error", "找不到")
            assert rig.core._video_preview is None and rig.window.platform._video is None
            await rig.click(button, 2)
            await rig.click(button, 0)
            await natural_width(rig, picture, 64)
            await rig.click(button, 0)
            await wait_for(lambda: rig.window.platform._video is None)
            await rig.click(button, 1)
            await natural_width(rig, picture, 64)
            await new_conversation(rig, tab_count=2)
            await wait_for(lambda: rig.core._video_preview is None and rig.window.platform._video is None)
            await rig.click(".tab", 0)
            await rig.wait_for(button, 3)
            assert rig.core._video_preview is None and not await rig.exists(".fp-video-stage")

            opens = [p for m, p in calls if m == "files.video" and p["action"] == "open"]
            assert [p["path"] for p in opens if p.get("inline")] == [
                "outputs/first.mp4", "outputs/second.mp4", "outputs/missing.mp4", "outputs/first.mp4", "outputs/second.mp4",
            ]
            assert all(p["id"] == cid for p in opens)
            assert not any(m == "files.page" for m, _ in calls), "videos are never sent as file bytes"
        finally:
            await rig.finish()
    run(loop, scenario())
