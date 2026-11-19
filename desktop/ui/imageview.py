"""Qt-only picture formats are decoded once and sent to the side panel as PNG."""
from __future__ import annotations

import base64
import gzip
import io
import zlib
from pathlib import Path

from PySide6.QtCore import QBuffer, QByteArray, QIODevice
from PySide6.QtGui import QImageReader

from desktop.core.files import PREVIEW_IMAGE_BYTES, FileProblem, image_bytes
from desktop.ui.imaging import encode

MAX_PIXELS = 16_000_000
_UNREADABLE = "这张图片不能在侧栏里预览：用默认程序打开。"


def picture_png(path: str) -> str:
    source = Path(path)
    data = image_bytes(source)
    fmt = source.suffix.lower().lstrip(".")
    if fmt == "svgz":
        # Bound the expanded SVG before handing it to Qt's parser.
        try:
            with gzip.GzipFile(fileobj=io.BytesIO(data)) as packed:
                data = packed.read(PREVIEW_IMAGE_BYTES + 1)
        except (OSError, EOFError, zlib.error) as error:
            raise FileProblem(_UNREADABLE) from error
        if len(data) > PREVIEW_IMAGE_BYTES:
            raise FileProblem("这张图片解压后太大，不能在侧栏里预览：用默认程序打开。")
        fmt = "svg"
    elif fmt == "tif":
        fmt = "tiff"
    elif fmt == "tga" and not data.endswith(b"TRUEVISION-XFILE.\0"):
        # Qt's TGA reader recognizes the v2 footer; old images can have identical pixels without it.
        data += b"\0" * 8 + b"TRUEVISION-XFILE.\0"
    buffer = QBuffer()
    buffer.setData(QByteArray(data))
    buffer.open(QIODevice.OpenModeFlag.ReadOnly)
    reader = QImageReader(buffer, fmt.encode("ascii"))
    reader.setAutoTransform(True)
    size = reader.size()
    # Qt's ICNS plugin cannot report even a valid icon's dimensions ahead of read. Keep Qt's allocation
    # guard for this format and check the decoded dimensions below; all other formats must report a size.
    if not size.isValid() and not (fmt == "icns" and 0 < reader.allocationLimit() <= 256):
        raise FileProblem(_UNREADABLE)
    if size.width() * size.height() > MAX_PIXELS:
        raise FileProblem("这张图片的像素太多，不能在侧栏里预览：用默认程序打开。")
    # Calling read once decodes only the first image in a multi-page file.
    image = reader.read()
    if image.width() * image.height() > MAX_PIXELS:
        raise FileProblem("这张图片的像素太多，不能在侧栏里预览：用默认程序打开。")
    png = b"" if image.isNull() else encode(image, "PNG")
    if not png:
        raise FileProblem(_UNREADABLE)
    return f"data:image/png;base64,{base64.b64encode(png).decode('ascii')}"
