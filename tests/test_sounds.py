"""Format and locality checks for notification sounds, independent of Qt playback."""

from __future__ import annotations

import struct
import wave
from pathlib import Path

import pytest

from desktop.core import sounds


@pytest.mark.parametrize("prefix", [
    b"",
    b"ID3\x03\x00\x00\x00\x04\x00\x00" + bytes(65536),  # A syncsafe tag size can exceed the frame scan window.
    b"ID3\x04\x00\x10\x00\x00\x01\x02" + bytes(130) + b"3DI\x04\x00\x10\x00\x00\x01\x02",
], ids=["bare", "id3v2", "id3v2-footer"])
def test_mp3_accepts_consecutive_frames_with_or_without_id3v2(tmp_path, mp3_frame, prefix):
    path = tmp_path / "sound.MP3"
    path.write_bytes(prefix + mp3_frame * 10)
    assert sounds.validate_path(str(path)) == str(path.resolve())


@pytest.mark.parametrize("first_header,size", [
    (b"\xff\xfb\xa2\x00", 523),  # MPEG-1 Layer III, 160 kbit/s with padding.
    (b"\xff\xf3\x82\x00", 209),  # MPEG-2 Layer III, 64 kbit/s with padding.
    (b"\xff\xe3\x80\x00", 417),  # MPEG-2.5 Layer III, 64 kbit/s.
    (b"\xff\xff\x42\x00", 140),  # MPEG-1 Layer I, 128 kbit/s with four-byte slot padding.
])
def test_mp3_frame_lengths_account_for_version_layer_and_padding(tmp_path, first_header, size):
    path = tmp_path / "sound.mp3"
    path.write_bytes((first_header + bytes(size - 4)) * 2)
    assert sounds.validate_path(str(path)) == str(path.resolve())


def test_mp3_accepts_variable_bitrate_and_leading_junk(tmp_path, mp3_frame):
    path = tmp_path / "sound.mp3"
    path.write_bytes(b"junk" + mp3_frame + b"\xff\xfb\xa2\x00" + bytes(519))
    assert sounds.validate_path(str(path)) == str(path.resolve())


@pytest.mark.parametrize("case", ["junk", "truncated", "unpaired", "empty", "too-large", "bad-tag", "tag-only", "ogg"])
def test_invalid_sound_files_are_rejected(tmp_path, mp3_frame, case):
    path = tmp_path / ("sound.ogg" if case == "ogg" else "sound.mp3")
    contents = {
        "junk": b"not audio" * 100,
        "truncated": mp3_frame[:200],
        "unpaired": mp3_frame + b"\x00" + mp3_frame,
        "empty": b"",
        "too-large": mp3_frame * 10,
        "bad-tag": b"ID3\x04\x00\x00\x80\x00\x00\x00" + mp3_frame * 2,
        "tag-only": b"ID3\x04\x00\x00\x00\x00\x06\x42" + mp3_frame * 2,
        "ogg": mp3_frame * 2,
    }[case]
    with path.open("wb") as audio:
        audio.write(contents)
        if case == "too-large":
            audio.truncate(sounds.MAX_BYTES + 1)
    with pytest.raises(sounds.SoundProblem, match="WAV 或 MP3"):
        sounds.validate_path(str(path))


@pytest.mark.parametrize("header", [
    b"\xff\xeb\x90\x00",  # Reserved version.
    b"\xff\xf9\x90\x00",  # Reserved layer.
    b"\xff\xfb\x00\x00",  # Free bitrate.
    b"\xff\xfb\xf0\x00",  # Invalid bitrate.
    b"\xff\xfb\x9c\x00",  # Reserved sample rate.
])
def test_reserved_mpeg_header_values_are_rejected(tmp_path, header):
    path = tmp_path / "sound.mp3"
    path.write_bytes((header + bytes(413)) * 2)
    with pytest.raises(sounds.SoundProblem):
        sounds.validate_path(str(path))


@pytest.mark.parametrize("path", [r"\\server\share\sound.mp3", "//server/share/sound.mp3", "https://example.com/sound.mp3", "sound.mp3"])
def test_mp3_must_be_local_and_absolute(path):
    with pytest.raises(sounds.SoundProblem, match="不能使用网络路径"):
        sounds.validate_path(path)


def test_bundled_sounds_are_distinct_short_pcm_wavs_without_metadata():
    contents = []
    for kind in sounds.KINDS:
        path = Path(sounds.validate_path(sounds.sound_path(kind)))
        assert path.name == f"{kind}.wav"
        with wave.open(str(path), "rb") as audio:
            assert audio.getcomptype() == "NONE" and audio.getsampwidth() == 2
            assert 0 < audio.getnframes() / audio.getframerate() <= 2
            data = audio.readframes(audio.getnframes())
            assert any(data)
            contents.append(data)
        raw = path.read_bytes()
        assert raw[:4] == b"RIFF" and raw[8:12] == b"WAVE"
        assert struct.unpack_from("<I", raw, 4)[0] == len(raw) - 8
        chunks, offset = [], 12
        while offset < len(raw):
            chunk, size = struct.unpack_from("<4sI", raw, offset)
            chunks.append(chunk)
            offset += 8 + size + size % 2
        assert chunks == [b"fmt ", b"data"] and offset == len(raw)
    assert len(set(contents)) == 3


def test_wav_accepts_8_bit_pcm_and_rejects_truncated_audio(tmp_path):
    path = tmp_path / "sound.wav"
    with wave.open(str(path), "wb") as audio:
        audio.setparams((1, 1, 24000, 0, "NONE", "not compressed"))
        audio.writeframes(b"\x80" * 100)
    assert sounds.validate_path(str(path)) == str(path.resolve())
    path.write_bytes(path.read_bytes()[:-20])
    with pytest.raises(sounds.SoundProblem, match="不完整"):
        sounds.validate_path(str(path))
