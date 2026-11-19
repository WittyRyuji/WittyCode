"""Generate a tiny local MP4 in the test, then exercise the real Qt decoder and JPEG stream."""
from __future__ import annotations

import asyncio
import base64
from pathlib import Path

import pytest
from PySide6.QtCore import QObject, QUrl
from PySide6.QtGui import QImage
from PySide6.QtMultimedia import QMediaCaptureSession, QMediaFormat, QMediaRecorder, QVideoFrame, QVideoFrameInput

from desktop.core.files import FileProblem
from desktop.ui.videoview import VideoView
from tests.fakes import until
from tests.ui_support import run


async def make_clip(path: Path) -> Path:
    parent = QObject()
    capture = QMediaCaptureSession(parent)
    input_ = QVideoFrameInput(parent)
    recorder = QMediaRecorder(parent)
    capture.setVideoFrameInput(input_)
    capture.setRecorder(recorder)
    fmt = QMediaFormat(QMediaFormat.FileFormat.MPEG4)
    codecs = fmt.supportedVideoCodecs(QMediaFormat.ConversionMode.Encode)
    if QMediaFormat.VideoCodec.H264 in codecs:
        codec = QMediaFormat.VideoCodec.H264
    elif QMediaFormat.VideoCodec.MPEG4 in codecs:
        codec = QMediaFormat.VideoCodec.MPEG4
    else:
        pytest.skip("Qt has no MP4 encoder")
    fmt.setVideoCodec(codec)
    recorder.setMediaFormat(fmt)
    recorder.setVideoFrameRate(25)
    recorder.setOutputLocation(QUrl.fromLocalFile(str(path)))
    recorder.setAutoStop(True)
    frame_index = 0

    def send() -> None:
        nonlocal frame_index
        if frame_index == 25:
            if input_.sendVideoFrame(QVideoFrame()):
                frame_index += 1
            return
        if frame_index > 25:
            return
        image = QImage(64, 48, QImage.Format.Format_RGB32)
        image.fill(0xFF2D709B if frame_index < 12 else 0xFFB97A39)
        frame = QVideoFrame(image)
        frame.setStartTime(frame_index * 40_000)
        frame.setEndTime((frame_index + 1) * 40_000)
        if input_.sendVideoFrame(frame):
            frame_index += 1

    # Queued ready notifications supply one frame at a time as the encoder makes room.
    input_.readyToSendVideoFrame.connect(send)
    recorder.record()
    try:
        await until(lambda: recorder.error() != QMediaRecorder.Error.NoError or (frame_index == 26 and recorder.recorderState() == QMediaRecorder.RecorderState.StoppedState), timeout=12)
        assert recorder.error() == QMediaRecorder.Error.NoError, recorder.errorString()
        clip = Path(recorder.actualLocation().toLocalFile())
        assert clip.exists() and clip.stat().st_size > 0
        return clip
    finally:
        if recorder.recorderState() != QMediaRecorder.RecorderState.StoppedState:
            recorder.stop()
        parent.deleteLater()


def test_real_video_decodes_paused_then_streams_and_seeks(qapp, loop, tmp_path):
    async def scenario():
        clip = await make_clip(tmp_path / "sample.mp4")
        parent = QObject()
        events = []
        player = VideoView(parent, 550, events.append)
        try:
            info = await player.load(str(clip))
            assert 880 <= info["duration"] <= 1120 and (info["width"], info["height"]) == (64, 48)
            assert info["seekable"] and not info["hasAudio"] and info["videoCodec"]
            assert info["frame"].startswith("data:image/jpeg;base64,")
            image = QImage.fromData(base64.b64decode(info["frame"].split(",", 1)[1]))
            assert (image.width(), image.height()) == (64, 48)
            assert player.player.playbackState() == Playback.PausedState
            await asyncio.sleep(0.13)
            assert len(events) <= 1, "the paused video does not continuously publish"
            player.control("seek", {"position": 520})
            await until(lambda: any(event["position"] >= 500 and "url" in event for event in events))
            player.control("play", {})
            await until(lambda: any(event["playing"] and "url" in event for event in events))
            await until(lambda: any(event["ended"] for event in events))
            assert events[-1]["position"] == info["duration"] and not events[-1]["playing"]
        finally:
            player.close()
            assert player.player.source().isEmpty()
            parent.deleteLater()
    run(loop, scenario())


def test_invalid_video_is_a_friendly_failure(qapp, loop, tmp_path):
    async def scenario():
        path = tmp_path / "broken.mp4"
        path.write_bytes(b"not a video")
        parent = QObject()
        player = VideoView(parent, 640, lambda _: None)
        try:
            with pytest.raises(FileProblem, match="用默认程序打开"):
                await player.load(str(path))
        finally:
            player.close()
            parent.deleteLater()
    run(loop, scenario())


from PySide6.QtMultimedia import QMediaPlayer
Playback = QMediaPlayer.PlaybackState
