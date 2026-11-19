"""Real Qt window, QWebChannel, and MP4 decoder with a generated clip and fake conversation."""
from __future__ import annotations

import pytest

from tests.e2e_support import open_rig
from tests.fake_responses import say
from tests.fakes import until
from tests.test_app_e2e import new_conversation
from tests.test_videoview import make_clip
from tests.ui_support import run

pytestmark = pytest.mark.ui


def test_mp4_draws_and_unloads_when_switching_conversations(qapp, loop, tmp_path):
    async def scenario():
        rig = await open_rig(tmp_path, size=(1450, 900))
        try:
            directory = rig.project / "media"
            directory.mkdir()
            clip = await make_clip(directory / "sample.mp4")
            model = await new_conversation(rig)
            model.next(say(f"示例视频在 `media/{clip.name}`。"), hold=False)
            await rig.send("Show the sample clip")
            await rig.wait_for(".text-block code.pathref", 1)
            await rig.click(".text-block code.pathref")
            await rig.wait_for(".fp-video-stage img", 1)
            assert await rig.js("document.querySelector('.fp-video-stage img').src.startsWith('data:image/jpeg;base64,')")
            assert await rig.js("document.querySelector('.fp-video-play').getAttribute('aria-label')") == "播放"
            assert "64 × 48" in await rig.text(".fp-facts")
            first = rig.core._video_preview
            assert first is not None

            await rig.click(".fp-video-play")
            await rig.wait_for(".fp-video-play[aria-label='暂停']", 1)
            await new_conversation(rig, tab_count=2)
            await until(lambda: rig.core._video_preview is None)
            assert not await rig.exists(".fp-video-stage")
            await rig.click(".tab", 0)
            await rig.wait_for(".fp-video-stage img", 1)
            assert rig.core._video_preview is not None and rig.core._video_preview != first
            await rig.click(".filepanel [aria-label='关闭文件面板']")
            await until(lambda: rig.core._video_preview is None)
        finally:
            await rig.finish()
    run(loop, scenario())
