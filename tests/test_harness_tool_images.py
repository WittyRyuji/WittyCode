"""Picture reads leave text previews, never editable fingerprints."""
import asyncio
import base64

import pytest

from desktop.core.harness.tools import ToolContext, ToolRegistry
from tests.tool_image_support import gif, jpeg, png, webp


def read(path, data, *, vision=True, **args):
    path.write_bytes(data)
    ctx = ToolContext(path.parent, supports_images=vision)
    result = asyncio.run(ToolRegistry.default().run("read_file", {"file_path": path.name, **args}, ctx))
    return result, ctx


@pytest.mark.parametrize("name,media_type,data,animated", [
    ("example.PNG", "image/png", png(), False),
    ("example.jpg", "image/jpeg", jpeg(), False),
    ("example.JPEG", "image/jpeg", jpeg(), False),
    ("example.gif", "image/gif", gif(1).replace(b"GIF89a", b"GIF87a", 1), False),
    ("example.gif", "image/gif", gif(2), True),
    ("lossy.webp", "image/webp", webp(b"VP8 "), False),
    ("lossless.webp", "image/webp", webp(b"VP8L"), False),
    ("extended.webp", "image/webp", webp(b"VP8X"), False),
])
def test_reads_picture_bytes_using_suffix_and_header(tmp_path, name, media_type, data, animated):
    result, ctx = read(tmp_path / name, data, offset=999, limit=1, encoding="cp1252")
    assert not result.is_error
    assert f"{name} ({media_type}, 2 x 3 px, {len(data)} B)" in result.output
    assert ("first frame" in result.output) is animated
    assert result.images == [{"media_type": media_type, "data": base64.b64encode(data).decode("ascii")}]
    assert ctx.read_files == ctx.read_encodings == {}


@pytest.mark.parametrize("name,data,vision,reason", [
    ("bad.png", gif(1), True, "signature does not match"),
    ("bad.jpg", jpeg()[:9], True, "cannot read the picture dimensions"),
    ("big.png", png(height=8001), True, "8000 px on the longer side"),
    ("no-vision.png", png(), False, "current model does not support images"),
    ("other.zip", b"PK\x03\x04\x00", True, "looks like a binary file"),
])
def test_refuses_unsupported_picture_or_binary(tmp_path, name, data, vision, reason):
    result, ctx = read(tmp_path / name, data, vision=vision)
    assert result.is_error and reason in result.output
    assert result.images == [] and ctx.read_files == {}


def test_picture_byte_cap_cannot_be_bypassed_with_a_text_window(tmp_path):
    result, _ = read(tmp_path / "large.png", png() + bytes(5 * 1024 * 1024), offset=1, limit=1)
    assert result.is_error and "5.0 MB picture limit" in result.output and not result.images


def test_text_keeps_pagination_and_editing_fingerprint(tmp_path):
    result, ctx = read(tmp_path / "three.txt", b"one\ntwo\nthree\n", offset=2, limit=1, encoding="utf-8")
    assert not result.is_error and not result.images
    assert result.output.startswith("     2\ttwo\n[Showing lines 2-2 of 3.")
    assert len(ctx.read_files) == 1 and list(ctx.read_encodings.values()) == ["utf-8"]
