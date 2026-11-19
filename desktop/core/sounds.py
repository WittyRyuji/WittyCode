"""Local PCM WAV and MP3 notification sounds; empty settings use the bundled WAVs, never a system beep."""

from __future__ import annotations

import wave
from pathlib import Path

KINDS = ("question", "complete", "error")
SOUND_DIR = Path(__file__).resolve().parent.parent / "assets" / "sounds"
MAX_BYTES = 10 * 1024 * 1024
_MP3_SCAN_BYTES = 64 * 1024
# Bitrates in kbit/s, indexed by the layer bits (3 = Layer I) and bitrate index minus one.
_MPEG1_BITRATES = {
    3: (32, 64, 96, 128, 160, 192, 224, 256, 288, 320, 352, 384, 416, 448),
    2: (32, 48, 56, 64, 80, 96, 112, 128, 160, 192, 224, 256, 320, 384),
    1: (32, 40, 48, 56, 64, 80, 96, 112, 128, 160, 192, 224, 256, 320),
}
_LOW_RATE_LAYER1 = (32, 48, 56, 64, 80, 96, 112, 128, 144, 160, 176, 192, 224, 256)
_LOW_RATE_LAYER23 = (8, 16, 24, 32, 40, 48, 56, 64, 80, 96, 112, 128, 144, 160)


class SoundProblem(ValueError):
    pass


def sound_path(kind: str, custom: str = "") -> str:
    if kind not in KINDS:
        raise SoundProblem("没有这种提示音。")
    return custom or str(SOUND_DIR / f"{kind}.wav")


def _mpeg_frame(data: bytes, offset: int) -> tuple[int, int, int, int] | None:
    if offset + 4 > len(data) or data[offset] != 0xff or data[offset + 1] & 0xe0 != 0xe0:
        return None
    version, layer = (data[offset + 1] >> 3) & 3, (data[offset + 1] >> 1) & 3
    bitrate, rate, padding = data[offset + 2] >> 4, (data[offset + 2] >> 2) & 3, (data[offset + 2] >> 1) & 1
    if version == 1 or layer == 0 or bitrate in (0, 15) or rate == 3:
        return None
    sample_rate = (44100, 48000, 32000)[rate] // (4, 0, 2, 1)[version]
    rates = _MPEG1_BITRATES[layer] if version == 3 else (_LOW_RATE_LAYER1 if layer == 3 else _LOW_RATE_LAYER23)
    bits_per_second = rates[bitrate - 1] * 1000
    if layer == 3:
        size = (12 * bits_per_second // sample_rate + padding) * 4
    else:
        factor = 72 if layer == 1 and version != 3 else 144
        size = factor * bits_per_second // sample_rate + padding
    return version, layer, rate, size


def _has_mp3_frames(path: Path) -> bool:
    with path.open("rb") as audio:
        tag = audio.read(10)
        if tag.startswith(b"ID3"):
            if len(tag) != 10 or any(byte & 0x80 for byte in tag[6:10]):
                return False
            size = 0
            for byte in tag[6:10]:
                size = (size << 7) | byte
            audio.seek(10 + size + (10 if tag[5] & 0x10 else 0))
        else:
            audio.seek(0)
        data = audio.read(_MP3_SCAN_BYTES)
    offset = data.find(b"\xff")
    while offset != -1:
        frame = _mpeg_frame(data, offset)
        if frame is not None:
            following = _mpeg_frame(data, offset + frame[3])
            # The bitrate and padding can change in a VBR stream, but the audio format cannot.
            if following is not None and following[:3] == frame[:3]:
                return True
        offset = data.find(b"\xff", offset + 1)
    return False


def validate_path(value: str) -> str:
    """Accept only existing local PCM WAV or MP3 files; no URLs or network paths (including linked targets)."""
    path = Path(value.strip()).expanduser()
    if not path.is_absolute() or str(path).startswith(("\\\\", "//")):
        raise SoundProblem("请选择本机上的 WAV 或 MP3 文件，不能使用网络路径。")
    try:
        path = path.resolve()
        if str(path).startswith(("\\\\", "//")):
            raise SoundProblem("请选择本机上的 WAV 或 MP3 文件，不能使用网络路径。")
        suffix = path.suffix.lower()
        if suffix not in (".wav", ".mp3") or not path.is_file() or not (44 if suffix == ".wav" else 0) < path.stat().st_size <= MAX_BYTES:
            raise SoundProblem("请选择有效的 WAV 或 MP3 文件（不超过 10 MB）。")
        if suffix == ".mp3":
            if not _has_mp3_frames(path):
                raise SoundProblem("这个提示音文件无效，请重新选择 WAV 或 MP3 文件。")
        else:
            with wave.open(str(path), "rb") as audio:
                if audio.getcomptype() != "NONE" or audio.getnchannels() not in (1, 2) or audio.getsampwidth() not in (1, 2) or audio.getnframes() == 0:
                    raise SoundProblem("请选择 WAV 或 MP3 文件，其中 WAV 须为单声道或双声道的 8/16 位 PCM。")
                expected = audio.getnframes() * audio.getnchannels() * audio.getsampwidth()
                if expected > MAX_BYTES or len(audio.readframes(audio.getnframes())) != expected:
                    raise SoundProblem("这个提示音文件不完整，请重新选择 WAV 或 MP3 文件。")
    except (OSError, EOFError, wave.Error) as error:
        raise SoundProblem("无法读取这个提示音文件，请重新选择 WAV 或 MP3 文件。") from error
    return str(path)
