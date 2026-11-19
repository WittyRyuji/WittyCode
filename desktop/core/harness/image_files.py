"""Read picture metadata without decoding pixels. The source suffix selects the media type."""
from pathlib import Path

MEDIA_TYPES = {".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".gif": "image/gif", ".webp": "image/webp"}
MAX_SIDE = 8000


def _jpeg_size(data: bytes) -> tuple[int, int]:
    pos = 2
    while pos < len(data) and data[pos] == 0xFF:
        while pos < len(data) and data[pos] == 0xFF:
            pos += 1
        if pos + 3 > len(data):
            break
        marker = data[pos]
        pos += 1
        if marker in (0xD9, 0xDA):  # EOI or start of scan: the frame header must precede them.
            break
        if marker == 1 or 0xD0 <= marker <= 0xD7:  # Standalone markers have no length.
            continue
        length = int.from_bytes(data[pos : pos + 2], "big")
        if length < 2 or pos + length > len(data):
            break
        if 0xC0 <= marker <= 0xCF and marker not in (0xC4, 0xC8, 0xCC) and length >= 8:
            return int.from_bytes(data[pos + 5 : pos + 7], "big"), int.from_bytes(data[pos + 3 : pos + 5], "big")
        pos += length
    return 0, 0


def _gif_animated(data: bytes) -> bool:
    # Count image descriptors, skipping colour tables and length-prefixed sub-blocks (which may contain 0x2C).
    pos = 13 + (3 << ((data[10] & 7) + 1) if data[10] & 0x80 else 0)
    frames = 0
    while pos + 1 < len(data):
        tag = data[pos]
        if tag == 0x2C and pos + 10 < len(data):
            frames += 1
            if frames == 2:
                return True
            flags = data[pos + 9]
            pos += 11 + (3 << ((flags & 7) + 1) if flags & 0x80 else 0)
        elif tag == 0x21:  # Extension label, then sub-blocks.
            pos += 2
        else:
            break
        while pos < len(data) and data[pos]:
            pos += 1 + data[pos]
        pos += 1  # Sub-block terminator.
    return False


def _webp_size(data: bytes) -> tuple[int, int]:
    chunk = data[12:16]
    size = int.from_bytes(data[16:20], "little")
    if size > len(data) - 20:
        return 0, 0
    if chunk == b"VP8X" and size >= 10:
        return 1 + int.from_bytes(data[24:27], "little"), 1 + int.from_bytes(data[27:30], "little")
    if chunk == b"VP8L" and size >= 5 and data[20] == 0x2F:
        bits = int.from_bytes(data[21:25], "little")
        return 1 + (bits & 0x3FFF), 1 + ((bits >> 14) & 0x3FFF)
    if chunk == b"VP8 " and size >= 10 and data[23:26] == b"\x9d\x01\x2a":
        return int.from_bytes(data[26:28], "little") & 0x3FFF, int.from_bytes(data[28:30], "little") & 0x3FFF
    return 0, 0


def picture_info(path: Path, media_type: str, data: bytes) -> tuple[int, int, bool]:
    """Check the signature and read header dimensions; this is not a full file validator."""
    if media_type == "image/png":
        signature = data.startswith(b"\x89PNG\r\n\x1a\n")
    elif media_type == "image/jpeg":
        signature = data.startswith(b"\xff\xd8\xff")
    elif media_type == "image/gif":
        signature = data.startswith((b"GIF87a", b"GIF89a"))
    else:
        signature = data.startswith(b"RIFF") and data[8:12] == b"WEBP"
    if not signature:
        raise ValueError(f"{path}: the file signature does not match its picture suffix ({media_type}).")

    width, height = 0, 0
    animated = False
    if media_type == "image/png":
        if len(data) >= 33 and data[8:16] == b"\0\0\0\rIHDR":
            width, height = int.from_bytes(data[16:20], "big"), int.from_bytes(data[20:24], "big")
    elif media_type == "image/jpeg":
        width, height = _jpeg_size(data)
    elif media_type == "image/gif":
        if len(data) >= 13:
            width, height = int.from_bytes(data[6:8], "little"), int.from_bytes(data[8:10], "little")
            animated = _gif_animated(data)
    else:
        width, height = _webp_size(data)
    if not width or not height:
        raise ValueError(f"{path}: cannot read the picture dimensions; its header is incomplete or invalid.")
    if max(width, height) > MAX_SIDE:
        raise ValueError(f"{path} is {width} x {height} px; resize it to at most {MAX_SIDE} px on the longer side.")
    return width, height, animated
