"""Local WAV notification sounds; empty settings use the bundled originals, never a system beep."""

from __future__ import annotations

import wave
from pathlib import Path

KINDS = ("question", "complete", "error")
SOUND_DIR = Path(__file__).resolve().parent.parent / "assets" / "sounds"
MAX_BYTES = 10 * 1024 * 1024


class SoundProblem(ValueError):
    pass


def sound_path(kind: str, custom: str = "") -> str:
    if kind not in KINDS:
        raise SoundProblem("没有这种提示音。")
    return custom or str(SOUND_DIR / f"{kind}.wav")


def validate_path(value: str) -> str:
    """Accept only an existing local PCM WAV; no URLs or network paths (including linked targets)."""
    path = Path(value.strip()).expanduser()
    if not path.is_absolute() or str(path).startswith(("\\\\", "//")):
        raise SoundProblem("请选择本机上的 WAV 文件，不能使用网络路径。")
    try:
        path = path.resolve()
        if str(path).startswith(("\\\\", "//")):
            raise SoundProblem("请选择本机上的 WAV 文件，不能使用网络路径。")
        if path.suffix.lower() != ".wav" or not path.is_file() or not 44 < path.stat().st_size <= MAX_BYTES:
            raise SoundProblem("请选择有效的 WAV 文件（不超过 10 MB）。")
        with wave.open(str(path), "rb") as audio:
            if audio.getcomptype() != "NONE" or audio.getnchannels() not in (1, 2) or audio.getsampwidth() not in (1, 2) or audio.getnframes() == 0:
                raise SoundProblem("提示音需要是单声道或双声道的 8/16 位 PCM WAV 文件。")
            expected = audio.getnframes() * audio.getnchannels() * audio.getsampwidth()
            if expected > MAX_BYTES or len(audio.readframes(audio.getnframes())) != expected:
                raise SoundProblem("这个 WAV 文件不完整。")
    except (OSError, EOFError, wave.Error) as error:
        raise SoundProblem("无法读取这个 WAV 文件，请重新选择。") from error
    return str(path)
