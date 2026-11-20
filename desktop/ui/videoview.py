"""Local Qt video playback and one-shot posters; the page only sees JPEGs and controls."""
from __future__ import annotations

import asyncio
import base64
import os
import time
from collections.abc import Callable
from typing import Any

from PySide6.QtCore import QFile, QIODevice, QObject, QTimer, QUrl, Qt
from PySide6.QtMultimedia import QAudioOutput, QMediaFormat, QMediaMetaData, QMediaPlayer, QVideoFrame, QVideoSink

from desktop.core.files import FileProblem, InlineVideoSource, transport_stream_header
from desktop.ui.imaging import encode

OPEN_SECONDS = 8
FRAME_INTERVAL = 1 / 20
MAX_SIDE = 1280
MAX_FRAME_PIXELS = 38_000_000
_BAD_VIDEO = "这个视频无法解码，可能是文件损坏或编码不受支持：用默认程序打开。"
Status = QMediaPlayer.MediaStatus
Playback = QMediaPlayer.PlaybackState


def _local_container(header: bytes) -> bool:
    """Allow actual local media containers, not playlists renamed to a video suffix."""
    return (
        (len(header) >= 12 and header[4:8] in {b"ftyp", b"moov", b"mdat", b"free", b"skip", b"wide", b"pnot", b"styp", b"uuid"})
        or (header.startswith(b"RIFF") and header[8:12] in {b"AVI ", b"AVIX"})
        or header.startswith((b"\x1a\x45\xdf\xa3", b"OggS", b"\x30\x26\xb2\x75\x8e\x66\xcf\x11\xa6\xd9\x00\xaa\x00\x62\xce\x6c",
                              b"\x00\x00\x01\xba", b"\x00\x00\x01\xb3", b"\x00\x00\x01\xb0"))
        or transport_stream_header(header)
    )


class VideoView(QObject):
    """A fresh player/sink per source: late Qt signals from the previous file cannot enter the new preview."""

    def __init__(self, parent: QObject, width: int, publish: Callable[[dict[str, Any]], None] | None) -> None:
        super().__init__(parent)
        self._publish = publish
        self._width = min(MAX_SIDE, max(240, width))
        self._ready: asyncio.Future[dict[str, Any]] = asyncio.get_running_loop().create_future()
        self._opened = False
        self._closed = False
        self._priming = False
        self._last_frame = float("-inf")
        self._last_state: tuple[int, bool, bool] | None = None
        self.source = QFile(self)
        self._source_fd: int | None = None
        self.player = QMediaPlayer(self)
        # A poster has no audio output at all. Interactive players also prime while muted.
        self.audio = QAudioOutput(self) if publish is not None else None
        if self.audio is not None:
            self.audio.setVolume(0.7)
            self.audio.setMuted(True)
            self.player.setAudioOutput(self.audio)
        self.sink = QVideoSink(self)
        self.player.setVideoSink(self.sink)
        self.timer = QTimer(self)
        self.timer.setInterval(200)
        self.timer.timeout.connect(self._state)
        self.player.mediaStatusChanged.connect(self._status)
        self.player.playbackStateChanged.connect(self._playback)
        self.player.errorOccurred.connect(self._error)
        self.sink.videoFrameChanged.connect(self._frame)

    async def load(self, path: str) -> dict[str, Any]:
        mode = QIODevice.OpenModeFlag.ReadOnly
        if isinstance(path, InlineVideoSource):
            # QMediaPlayer keeps reading after load() returns. Own a duplicate until the source is cleared.
            # Do not give the demuxer a pathname or a base URL for resolving other locations.
            self._source_fd = os.dup(path.file.fileno())
            opened = self.source.open(self._source_fd, mode, QFile.FileHandleFlag.DontCloseHandle)
            url = QUrl()
        else:
            self.source.setFileName(path)
            opened = self.source.open(mode)
            url = QUrl.fromLocalFile(path)
        if not opened or not _local_container(bytes(self.source.peek(1024))):
            self._ready.cancel()
            raise FileProblem(_BAD_VIDEO)
        # Use a seekable, local device. Qt reads from disk on demand rather than buffering the clip.
        self.player.setSourceDevice(self.source, url)
        try:
            return await asyncio.wait_for(self._ready, OPEN_SECONDS)
        except TimeoutError as error:
            raise FileProblem("读取视频超时，不能在侧栏里预览：用默认程序打开。") from error

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        self.timer.stop()
        if not self._ready.done():
            self._ready.set_exception(FileProblem("视频预览已关闭。"))
        # stop() can hang on the FFmpeg backend during a quick replay of the same source.
        self.player.setSource(QUrl())
        self.source.close()
        if self._source_fd is not None:
            os.close(self._source_fd)
            self._source_fd = None
        self.deleteLater()

    def control(self, action: str, args: dict[str, Any]) -> None:
        if self._closed or not self._opened:
            return
        if action == "play":
            if self.player.mediaStatus() == Status.EndOfMedia:
                self.player.setPosition(0)
            self.player.play()
        elif action == "pause":
            self.player.pause()
        elif action == "seek" and self.player.isSeekable():
            self.player.setPosition(min(args["position"], self.player.duration()))
            self._state()
        elif action == "volume" and self.audio is not None:
            self.audio.setVolume(args["volume"])
            self.audio.setMuted(args["muted"])

    def _status(self, status: Status) -> None:
        if self._closed:
            return
        if status == Status.InvalidMedia:
            self._failed(_BAD_VIDEO)
        elif not self._opened and not self._priming and status in (Status.LoadedMedia, Status.BufferedMedia):
            if not self.player.hasVideo():
                self._failed("这个文件没有视频画面，不能在侧栏里预览：用默认程序打开。")
                return
            self._priming = True
            # Decode the first frame while muted. No audio is enabled until after the player has paused.
            QTimer.singleShot(0, self, self._prime)
        elif status == Status.EndOfMedia:
            if self._opened:
                self._state(ended=True)
            else:
                self._failed(_BAD_VIDEO)

    def _prime(self) -> None:
        if not self._closed and not self._ready.done():
            self.player.play()

    def _playback(self, state: Playback) -> None:
        if not self._opened or self._closed:
            return
        if state == Playback.PlayingState:
            self.timer.start()
        else:
            self.timer.stop()
        self._state()

    def _error(self, _kind: QMediaPlayer.Error, _text: str) -> None:
        self._failed(_BAD_VIDEO)

    def _failed(self, message: str) -> None:
        if self._closed:
            return
        if self._opened and self._publish is not None:
            self._opened = False
            self.timer.stop()
            self._publish({"position": self.player.position(), "playing": False, "ended": False, "error": message})
            QTimer.singleShot(0, self, self.close)
        elif not self._ready.done():
            self._ready.set_exception(FileProblem(message))

    def _state(self, *, ended: bool = False, url: str | None = None) -> None:
        if not self._opened or self._closed or self._publish is None:
            return
        ended = ended or self.player.mediaStatus() == Status.EndOfMedia
        position = max(0, self.player.duration() if ended else self.player.position())
        playing = not ended and self.player.playbackState() == Playback.PlayingState
        # Notify state changes even when the position has not moved (e.g. play immediately after opening).
        state = position, playing, ended
        if url is None and state == self._last_state:
            return
        self._last_state = state
        payload: dict[str, Any] = {"position": position, "playing": playing, "ended": ended}
        if url is not None:
            payload["url"] = url
        self._publish(payload)

    def _frame(self, frame: QVideoFrame) -> None:
        if self._closed or not frame.isValid() or (not self._opened and self._ready.done()):
            return
        now = time.monotonic()
        playing = self._opened and self.player.playbackState() == Playback.PlayingState
        if playing and now - self._last_frame < FRAME_INTERVAL:
            return  # no queue, no conversion for a dropped frame
        if frame.width() * frame.height() > MAX_FRAME_PIXELS:
            self._failed("这个视频的画面太大，不能在侧栏里预览：用默认程序打开。")
            return
        image = frame.toImage()
        if image.isNull():
            return
        width, height = image.width(), image.height()
        if width > self._width or height > MAX_SIDE:
            image = image.scaled(self._width, MAX_SIDE, Qt.AspectRatioMode.KeepAspectRatio, Qt.TransformationMode.SmoothTransformation)
        jpeg = encode(image, "JPEG", 75)
        if not jpeg:
            return
        self._last_frame = now
        url = f"data:image/jpeg;base64,{base64.b64encode(jpeg).decode('ascii')}"
        if self._opened:
            self._state(url=url)
        else:
            if self.audio is not None:
                self.player.pause()
                self.audio.setMuted(False)
                self._opened = True
            # For posters the caller closes the source as soon as this one JPEG is ready.
            meta = self.player.metaData()
            keys = QMediaMetaData.Key
            video = meta.value(keys.VideoCodec)
            audio = meta.value(keys.AudioCodec)
            self._ready.set_result({
                "duration": max(0, self.player.duration()), "width": width, "height": height,
                "videoCodec": QMediaFormat.videoCodecName(video) if isinstance(video, QMediaFormat.VideoCodec) else "",
                "audioCodec": QMediaFormat.audioCodecName(audio) if isinstance(audio, QMediaFormat.AudioCodec) else "",
                "hasAudio": self.player.hasAudio(), "seekable": self.player.isSeekable(), "frame": url,
            })
