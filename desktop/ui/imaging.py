"""Picture and PDF work for attachments: decode, shrink, thumbnails, PDF text.

``QImage``, ``QImageReader`` and ``QBuffer`` are QtGui classes that need neither a ``QApplication`` nor a
window, so everything here runs (and is tested) without a GUI. QtPdf is imported on first use because it
pulls in the whole PDF engine.
"""

from __future__ import annotations

import base64
import logging
from dataclasses import dataclass

from PySide6.QtCore import QBuffer, QByteArray, QIODevice, QSize, Qt
from PySide6.QtGui import QColor, QImage, QImageReader, QPainter

from desktop.core.attachments import MAX_IMAGE_BYTES, MAX_PARSED_PDF_CHARS, Thumbnail

log = logging.getLogger("desktop.attachments")

MAX_SIDE = 2048
"""A picture that has to shrink is scaled to at most this many pixels on its longest side first."""
PIXEL_LIMIT = 8000
"""Models refuse pictures with a longer side than this, however small the file is."""
THUMB_SIDE = 320
THUMB_QUALITY = 80
JPEG_QUALITIES = (90, 80, 70, 60, 50, 40)
FALLBACK_SIDES = (1536, 1024, 768, 512)
TRUNCATED = "\n[…已截断]"


@dataclass(frozen=True, slots=True)
class Fitted:
    data: bytes
    media_type: str
    compressed: bool
    """True when pixels were thrown away (scaled down or saved as JPEG), not just re-wrapped."""


def _open(data: bytes) -> QBuffer:
    buffer = QBuffer()
    buffer.setData(QByteArray(data))
    buffer.open(QIODevice.OpenModeFlag.ReadOnly)
    return buffer


def can_read(data: bytes) -> bool:
    """Whether the signature at the start of ``data`` is one of the picture formats Qt reads."""
    buffer = _open(data)  # the reader only holds a pointer: the buffer must stay alive
    return QImageReader(buffer).canRead()


def probe_size(data: bytes) -> QSize | None:
    """Pixel size from the header, without decoding (``None``: not a picture Qt can read)."""
    buffer = _open(data)
    reader = QImageReader(buffer)
    if not reader.canRead():
        return None
    size = reader.size()
    return size if size.isValid() else None


def scaled(image: QImage, longest: int) -> QImage:
    if max(image.width(), image.height()) <= longest:
        return image
    return image.scaled(longest, longest, Qt.AspectRatioMode.KeepAspectRatio, Qt.TransformationMode.SmoothTransformation)


def decode(data: bytes, *, at_most: int | None = None) -> QImage | None:
    """The picture (EXIF rotation applied); with ``at_most`` it is decoded at reduced size where the format allows."""
    buffer = _open(data)
    reader = QImageReader(buffer)
    reader.setAutoTransform(True)
    if at_most:
        size = reader.size()
        if size.isValid() and max(size.width(), size.height()) > at_most:
            reader.setScaledSize(size.scaled(at_most, at_most, Qt.AspectRatioMode.KeepAspectRatio))
    image = reader.read()
    if image.isNull():
        return None
    return scaled(image, at_most) if at_most else image


def encode(image: QImage, fmt: str, quality: int = -1) -> bytes:
    buffer = QBuffer()
    buffer.open(QIODevice.OpenModeFlag.WriteOnly)
    if not image.save(buffer, fmt, quality):
        return b""
    return bytes(buffer.data())


def _flatten(image: QImage) -> QImage:
    """JPEG has no transparency: put the picture on white."""
    if not image.hasAlphaChannel():
        return image.convertToFormat(QImage.Format.Format_RGB32)
    flat = QImage(image.size(), QImage.Format.Format_RGB32)
    flat.fill(QColor("white"))
    painter = QPainter(flat)
    painter.drawImage(0, 0, image)
    painter.end()
    return flat


def is_transparent(image: QImage) -> bool:
    """Whether any pixel is not fully opaque (an RGBA screenshot usually is opaque)."""
    if not image.hasAlphaChannel():
        return False
    alpha = image.convertToFormat(QImage.Format.Format_Alpha8)
    rows, width, stride = alpha.height(), alpha.width(), alpha.bytesPerLine()
    view = memoryview(alpha.constBits())
    return any(min(view[row * stride : row * stride + width]) < 255 for row in range(rows))


def fit_image(
    image: QImage, *, limit: int | None = None, lossless: bool = True, max_side: int = MAX_SIDE
) -> Fitted | None:
    """Encode ``image`` so that it is at most ``limit`` bytes and no longer than ``PIXEL_LIMIT`` pixels.

    Order of attempts: a lossless PNG at full size; a PNG scaled down to ``max_side``; JPEG of the scaled
    picture at falling quality; JPEG at ever smaller sizes. Photos (``lossless=False``) go straight to JPEG.
    Returns ``None`` only for data that cannot be made small enough. ``limit`` defaults to ``MAX_IMAGE_BYTES``.
    """
    limit = MAX_IMAGE_BYTES if limit is None else limit
    if lossless and max(image.width(), image.height()) <= PIXEL_LIMIT:
        data = encode(image, "PNG")
        if data and len(data) <= limit:
            return Fitted(data, "image/png", False)
    work = scaled(image, max_side)
    if lossless and work is not image:
        data = encode(work, "PNG")
        if data and len(data) <= limit:
            return Fitted(data, "image/png", True)
    flat = _flatten(work)
    for quality in JPEG_QUALITIES:
        data = encode(flat, "JPEG", quality)
        if data and len(data) <= limit:
            return Fitted(data, "image/jpeg", True)
    for side in FALLBACK_SIDES:
        data = encode(scaled(flat, side), "JPEG", JPEG_QUALITIES[-1])
        if data and len(data) <= limit:
            return Fitted(data, "image/jpeg", True)
    return None


def thumbnail_image(data: bytes, side: int = THUMB_SIDE) -> QImage | None:
    return decode(data, at_most=side)


def thumbnail(data: bytes, side: int = THUMB_SIDE) -> Thumbnail | None:
    """A small picture for the transcript page as a ``data:`` URI (``None`` when ``data`` cannot be decoded).

    JPEG, except for pictures with transparency, which stay PNG so that they do not turn black or white.
    """
    image = thumbnail_image(data, side)
    if image is None:
        return None
    if is_transparent(image):
        media_type, payload = "image/png", encode(image, "PNG")
    else:
        media_type, payload = "image/jpeg", encode(_flatten(image), "JPEG", THUMB_QUALITY)
    if not payload:
        return None
    uri = f"data:{media_type};base64,{base64.b64encode(payload).decode('ascii')}"
    return Thumbnail(uri, image.width(), image.height())


def pdf_text(data: bytes, *, limit: int = MAX_PARSED_PDF_CHARS) -> str | None:
    """The text layer of a PDF (``None`` for scans, encrypted or damaged files)."""
    try:
        from PySide6.QtPdf import QPdfDocument
    except ImportError:
        return None
    # The document reads from the buffer lazily: both must outlive it.
    raw = QByteArray(data)
    buffer = QBuffer(raw)
    buffer.open(QIODevice.OpenModeFlag.ReadOnly)
    document = QPdfDocument()
    try:
        document.load(buffer)
        if document.status() != QPdfDocument.Status.Ready:
            return None
        parts: list[str] = []
        total = 0
        for page in range(document.pageCount()):
            text = document.getAllText(page).text().replace("\r\n", "\n").replace("\r", "\n").strip()
            if text:
                parts.append(text)
                total += len(text)
            if total > limit:
                break
        joined = "\n\n".join(parts)
    except Exception:
        log.exception("could not read the text of a PDF")
        return None
    finally:
        document.close()
    if not joined.strip():
        return None
    return joined[:limit] + TRUNCATED if len(joined) > limit else joined
