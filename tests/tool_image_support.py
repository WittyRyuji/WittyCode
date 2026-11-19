"""Synthetic image data, independent of any image library or binary fixture."""
import struct
import zlib


def png(width=2, height=3):
    def chunk(name, data):
        return struct.pack(">I", len(data)) + name + data + struct.pack(">I", zlib.crc32(name + data))

    header = struct.pack(">IIBBBBB", width, height, 8, 0, 0, 0, 0)
    return b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", header) + chunk(b"IDAT", zlib.compress(bytes((width + 1) * height))) + chunk(b"IEND", b"")


def gif(frames):
    header = b"GIF89a" + struct.pack("<HHBBB", 2, 3, 0x80, 0, 0) + b"\x00\x00\x00\xff\xff\xff"
    frame = b"\x2c" + struct.pack("<HHHHB", 0, 0, 1, 1, 0) + b"\x02\x02\x44\x01\x00"
    # A comment with an image separator byte must not be counted as an extra frame.
    return header + b"\x21\xfe\x01\x2c\x00" + frame * frames + b"\x3b"


def jpeg():
    # A length-prefixed APP segment before a progressive start-of-frame segment.
    return b"\xff\xd8\xff\xe0\x00\x05abc\xff\xc2\x00\x0b\x08\x00\x03\x00\x02\x01\x01\x11\x00\xff\xd9"


def webp(kind):
    body = {b"VP8 ": b"\x10\x00\x00\x9d\x01\x2a\x02\x00\x03\x00",
            b"VP8L": b"\x2f" + struct.pack("<I", 1 | (2 << 14)),
            b"VP8X": b"\x00" * 4 + b"\x01\x00\x00\x02\x00\x00"}[kind]
    contents = b"WEBP" + kind + struct.pack("<I", len(body)) + body + b"\x00" * (len(body) % 2)
    return b"RIFF" + struct.pack("<I", len(contents)) + contents
