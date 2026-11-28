"""Real Qt WebEngine/QWebChannel regression, with generated media and a loopback fake model."""
from __future__ import annotations

import asyncio
import json
import os
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
            (root / "outputs" / "result one.png").write_bytes(png(width=1200, height=800))
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
            await natural_width(rig, ".inline-picture img[alt='PNG']", 1200)
            await natural_width(rig, ".inline-picture img[alt='GIF']", 2)
            assert await rig.js("document.querySelector('.inline-picture img[alt=GIF]').src.startsWith('data:image/png;base64,')")
            await rig.wait_for(".inline-gif-toggle", 1)
            await rig.wait_text(".inline-media-error", "找不到")
            assert await rig.texts(".text-block .img-link") == ["[图片：Outside]", "[图片：Remote]"]
            assert sorted((m, p["id"], p["path"]) for m, p in calls) == [
                ("files.inlineImage", first, f"outputs/{name}") for name in ("loop.gif", "missing.png", "result one.png")
            ]

            # Clicking the picture itself opens the shared lightbox on the already decoded bytes, larger than inline.
            inline_box = json.loads(await rig.js("JSON.stringify(document.querySelector(\".inline-picture img[alt='PNG']\").getBoundingClientRect())"))
            before = list(calls)
            await rig.click(".inline-picture img[alt='PNG']")
            await rig.wait_for(".lightbox img", 1)
            shown = json.loads(await rig.js("""(() => {
                const img = document.querySelector('.lightbox img'), box = img.getBoundingClientRect();
                return JSON.stringify([img.src.startsWith('data:image/png;base64,'), img.naturalWidth, box.width, box.height]);
            })()"""))
            assert shown[0] and shown[1] == 1200 and shown[2] > inline_box["width"] and shown[3] > inline_box["height"]
            caption = await rig.text(".lightbox figcaption")
            assert "PNG" in caption and "1200×800" in caption
            if evidence := os.environ.get("WITTYCODE_TEST_EVIDENCE"):
                await rig.shot(".lightbox figure", Path(evidence) / "inline-picture-lightbox.png")
            await rig.click(".lightbox img")
            await rig.wait_for(".lightbox img", 1)  # the picture itself does not dismiss it
            await rig.press("Escape")
            await rig.wait_for(".lightbox", 0)

            # Keyboard activation is the same; both media surfaces zoom (the GIF keeps its own play button).
            assert await rig.count(".inline-zoom") == 2
            await rig.js("document.querySelector('.inline-zoom').focus()")
            await rig.press("Enter")
            await rig.wait_for(".lightbox", 1)
            await rig.click(".lb-close")
            await rig.wait_for(".lightbox", 0)
            assert calls == before

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
            await natural_width(rig, ".inline-picture img[alt='PNG']", 1200)
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


async def painted_media(rig, expected, filename=None):
    selectors = json.dumps(list(expected))
    raw = await rig.js(f"""JSON.stringify({selectors}.map(selector => {{
        const r = document.querySelector(selector).getBoundingClientRect();
        return [r.x + r.width * 0.2, r.y + r.height / 2];
    }}))""")
    points = json.loads(raw)
    zoom = rig.window.view.zoomFactor()
    evidence = os.environ.get("WITTYCODE_TEST_EVIDENCE")

    def painted():
        shot = rig.window.view.grab()
        image = shot.toImage()
        scale = zoom * shot.devicePixelRatio()
        for (x, y), target in zip(points, expected.values(), strict=True):
            x, y = round(x * scale), round(y * scale)
            if not image.valid(x, y):
                return False
            got = image.pixelColor(x, y).getRgb()[:3]
            if max(abs(actual - wanted) for actual, wanted in zip(got, target)) > 24:
                return False
        if filename and evidence:
            assert shot.save(str(Path(evidence) / filename))
        return True

    # QWebEngine can update the DOM before its offscreen compositor presents the frame.
    await wait_for(painted, 5, f"{list(expected)} showing the expected pixels")


@pytest.mark.parametrize("activation", ["mouse", "keyboard"])
def test_gif_is_a_centered_still_until_started_then_removes_play_button(qapp, loop, tmp_path, activation):
    async def scenario():
        rig = await open_rig(tmp_path, size=(1320, 900))
        image, button = ".inline-gif img", ".inline-gif-toggle"
        first, second = (36, 156, 146), (224, 112, 55)
        try:
            (rig.project / "motion.gif").write_bytes(gif(180, 96))
            model = await new_conversation(rig)
            if await rig.exists(".sidebar"):
                await rig.click("[aria-label='切换侧栏']")
            model.next(say("### Animation preview\n\n![Two colors](motion.gif)\n\n`motion.gif`"), hold=False)
            await rig.send("Show the animation")
            await natural_width(rig, image, 180)
            await rig.wait_for(f'{button}[aria-label="播放 GIF：Two colors"]', 1)
            assert await rig.js(f"""(() => {{
                const a = document.querySelector('{image}').getBoundingClientRect();
                const b = document.querySelector('{button}').getBoundingClientRect();
                return Math.abs(a.x + a.width / 2 - b.x - b.width / 2) < 1 &&
                       Math.abs(a.y + a.height / 2 - b.y - b.height / 2) < 1;
            }})()""")
            poster = await rig.js(f"document.querySelector('{image}').src")
            assert poster.startswith("data:image/png;base64,")
            await painted_media(rig, {image: first}, f"inline-gif-{activation}-still.png")
            await asyncio.sleep(0.5)
            assert await rig.js(f"document.querySelector('{image}').src") == poster
            await painted_media(rig, {image: first})

            if activation == "mouse":
                await rig.click(button)
            else:
                assert await rig.js(f"""(() => {{
                    const play = document.querySelector('{button}');
                    play.focus();
                    return document.activeElement === play && play.tabIndex === 0;
                }})()""")
                await rig.press("Enter")
            await rig.wait_for(button, 0)
            animation = await rig.js(f"document.querySelector('{image}').src")
            assert animation.startswith("data:image/gif;base64,")
            await painted_media(rig, {image: second}, f"inline-gif-{activation}-playing.png")
            await painted_media(rig, {image: first})

            # The playing picture's surface shares the zoom button: it opens the lightbox, the animation keeps playing.
            await rig.click(image)
            await rig.wait_for(".lightbox img", 1)
            assert await rig.js(f"document.querySelector('.lightbox img').src") == animation
            await rig.press("Escape")
            await rig.wait_for(".lightbox", 0)
            assert await rig.count(button) == 0
            assert await rig.js(f"document.querySelector('{image}').src") == animation
            await painted_media(rig, {image: second})
            await painted_media(rig, {image: first})
            assert await rig.count(button) == 0

            await rig.click(".text-block code.pathref")
            await natural_width(rig, ".filepanel img", 180)
            assert await rig.js("document.querySelector('.filepanel img').src") == animation
        finally:
            await rig.finish()
    run(loop, scenario())


def test_offscreen_video_loads_on_scroll_without_interrupting_sidebar(qapp, loop, tmp_path, monkeypatch):
    async def scenario():
        rig = await open_rig(tmp_path, size=(1350, 900))
        try:
            calls = record_file_calls(rig, monkeypatch)
            await make_clip(rig.project / "on-scroll.mp4")
            model = await new_conversation(rig)
            # Keep the middle embed outside the viewport whether the transcript starts at the top or the bottom.
            spacer = "\n\n".join(f"Spacer paragraph {i}." for i in range(40))
            model.next(say(f"{spacer}\n\n![On scroll](on-scroll.mp4)\n\n{spacer}\n\n`on-scroll.mp4`"), hold=False)
            await rig.send("Show the clip below the fold")
            await rig.wait_for(".inline-video", 1)
            assert not await rig.exists(".inline-video img")
            assert not any(m == "files.inlineVideoPoster" for m, _ in calls)

            await rig.click(".text-block code.pathref")
            await natural_width(rig, ".filepanel .fp-video-stage img", 64)
            playing = rig.window.platform._video
            lease = rig.core._video_preview
            playing.player.setPlaybackRate(0.25)
            await rig.click(".filepanel .fp-video-play")
            await rig.wait_for(".filepanel .fp-video-play[aria-label='暂停']", 1)
            await rig.js("document.querySelector('.inline-video').scrollIntoView({block: 'center'})")
            await natural_width(rig, ".inline-video .fp-video-stage img", 64)
            await rig.wait_for(".inline-video .fp-video-play[aria-label='播放']", 1)
            assert rig.core._video_preview == lease and rig.window.platform._video is playing
            assert playing.player.playbackState() == QMediaPlayer.PlaybackState.PlayingState
            assert [p["path"] for m, p in calls if m == "files.inlineVideoPoster"] == ["on-scroll.mp4"]
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
            await make_clip(directory / "first.mp4", size=(192, 96))
            await make_clip(directory / "second.mp4", (0xFF8452A8, 0xFF489064), size=(192, 96))
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
            chart = ".inline-picture img[alt='Synthetic signal']"
            first_box, second_box = (f".inline-video[title=\"outputs/{name}.mp4\"]" for name in ("first", "second"))
            pictures = [f"{box} .fp-video-stage img" for box in (first_box, second_box)]
            picture = pictures[0]
            play, play_second = (f"{box} .fp-video-play" for box in (first_box, second_box))
            slider = f"{first_box} .fp-video-time"
            await natural_width(rig, chart, 384)
            for img in pictures:
                await natural_width(rig, img, 192)
            await rig.wait_text(".inline-video .inline-media-error", "找不到")
            assert await rig.text(".text-block .img-link") == "Outside clip"
            assert rig.core._video_preview is None and rig.window.platform._video is None
            assert not any(m == "files.video" for m, _ in calls), "passive posters never acquire the playback lease"
            assert sorted(p["path"] for m, p in calls if m == "files.inlineVideoPoster") == [
                "outputs/first.mp4", "outputs/missing.mp4", "outputs/second.mp4",
            ]
            poster = await rig.js(f"document.querySelector({json.dumps(picture)}).src")
            assert poster.startswith("data:image/jpeg;base64,")
            assert await rig.js(f"!document.querySelector({json.dumps(slider)}).disabled")
            await rig.wait_for(".inline-video .fp-video-play[aria-label='播放']", 2)
            assert not await rig.exists(".sidebar, .filepanel, .conversation-rail")
            assert await rig.js(f"""(() => {{
                const reply = document.querySelector('.conversation-column .text-block');
                const viewport = reply.closest('.scroller').getBoundingClientRect();
                return {json.dumps([chart, *pictures, play, play_second, slider, f"{second_box} .fp-video-time"])}.every(selector => {{
                    const box = reply.querySelector(selector)?.getBoundingClientRect();
                    return box && box.width > 0 && box.height > 0 && box.top >= viewport.top && box.bottom <= viewport.bottom;
                }});
            }})()"""), "the chart and both posters with controls are visible together"
            await painted_media(rig, {pictures[0]: (45, 112, 155), pictures[1]: (132, 82, 168)}, "inline-video-posters.png")

            await rig.click(play)
            await rig.wait_for(f"{play}[aria-label='暂停']", 1)
            first_lease = rig.core._video_preview
            first = rig.window.platform._video
            assert first_lease[0] == cid and first is not None
            await wait_for(lambda: first.player.playbackState() == QMediaPlayer.PlaybackState.PlayingState
                           and first.player.position() > 100, 3, "first click plays and advances the MP4")
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
            await rig.click(play_second)
            await rig.wait_for(f"{play_second}[aria-label='暂停']", 1)
            assert rig.core._video_preview != first_lease and first._closed and first._source_fd is None
            await rig.wait_for(f"{play}[aria-label='播放']", 1)
            assert await rig.js(f"document.querySelector('{picture}').src") == poster
            assert await rig.count(".inline-video .fp-video-stage img") == 2

            # The same interactive decoder is shared with the pre-existing, explicitly opened file panel.
            await rig.click_text(".text-block code.pathref", "outputs/second.mp4")
            await natural_width(rig, ".filepanel .fp-video-stage img", 192)
            assert await rig.count(".inline-video .fp-video-stage img") == 2
            assert await rig.count(".inline-video .fp-video-play[aria-label='播放']") == 2
            await rig.click(".filepanel [aria-label='关闭文件面板']")
            await wait_for(lambda: rig.window.platform._video is None)

            await rig.click(play)
            await rig.wait_for(f"{play}[aria-label='暂停']", 1)
            await new_conversation(rig, tab_count=2)
            await wait_for(lambda: rig.core._video_preview is None and rig.window.platform._video is None)
            await rig.click(".tab", 0)
            await rig.wait_for(".inline-video .fp-video-play[aria-label='播放']", 2)
            assert rig.core._video_preview is None and rig.window.platform._video is None

            opens = [p for m, p in calls if m == "files.video" and p["action"] == "open"]
            assert [p["path"] for p in opens if p.get("inline")] == [
                "outputs/first.mp4", "outputs/second.mp4", "outputs/first.mp4",
            ]
            assert len([p for m, p in calls if m == "files.video" and p["action"] == "play"]) == 3
            assert all(p["id"] == cid for p in opens)
            assert not any(m == "files.page" for m, _ in calls), "videos are never sent as file bytes"
        finally:
            await rig.finish()
    run(loop, scenario())
