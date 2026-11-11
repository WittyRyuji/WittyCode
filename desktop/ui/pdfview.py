"""PDF pages for the side panel, drawn by QtPdf.

The page cannot show a PDF itself: plug-ins are off in its web view and it may not open a file as a frame. So Python draws
the pages and the page asks for them one at a time as they scroll into view. ``QPdfDocument`` reads from memory, never
from the path: a PDF that is open in the panel can still be replaced or deleted on disk.
"""

from __future__ import annotations

import base64
import logging
from pathlib import Path

from PySide6.QtCore import QBuffer, QByteArray, QIODevice, QSize
from PySide6.QtGui import QColor, QImage, QPainter
from PySide6.QtPdf import QPdfDocument

from desktop.core.files import PREVIEW_PDF_BYTES, FileProblem, preview_size
from desktop.ui import imaging

log = logging.getLogger("desktop.pdfview")

MIN_WIDTH = 240
MAX_WIDTH = 2400
MAX_PIXELS = 16_000_000
"""A page drawn at a width the panel asked for is kept under this many pixels (a poster-sized page stays drawable)."""

_PROBLEMS = {
    "IncorrectPassword": "这个 PDF 有密码，侧栏打不开：用默认程序打开。",
    "UnsupportedSecurityScheme": "这个 PDF 用了不支持的加密方式，侧栏打不开：用默认程序打开。",
    "InvalidFileFormat": "这个文件不是有效的 PDF，或者已经损坏。",
}


class PdfReader:
    """Keeps the last PDF open: the pages of one document are asked for one after the other."""

    def __init__(self) -> None:
        self._key: tuple[str, int, int] | None = None
        self._document: QPdfDocument | None = None
        self._keep: tuple[QByteArray, QBuffer] | None = None
        """The bytes and the buffer the document reads from: both must outlive it."""

    def close(self) -> None:
        document, self._document, self._keep, self._key = self._document, None, None, None
        if document is not None:
            document.close()

    def pages(self, path: str) -> list[tuple[float, float]]:
        """The size of every page in points. Raises ``FileProblem`` for a file that cannot be drawn."""
        document = self._open(Path(path))
        return [(size.width(), size.height()) for size in (document.pagePointSize(n) for n in range(document.pageCount()))]

    def page_image(self, path: str, page: int, width: int) -> str:
        """Page ``page`` (from 0) drawn ``width`` pixels wide, as a ``data:`` URI of a PNG. Raises ``FileProblem``."""
        document = self._open(Path(path))
        count = document.pageCount()
        if not 0 <= page < count:
            raise FileProblem(f"这个 PDF 没有第 {page + 1} 页（共 {count} 页）。")
        points = document.pagePointSize(page)
        if points.width() <= 0 or points.height() <= 0:
            raise FileProblem("这一页的大小不对，画不出来。")
        wide = min(max(int(width), MIN_WIDTH), MAX_WIDTH)
        high = max(1, round(wide * points.height() / points.width()))
        if wide * high > MAX_PIXELS:
            wide = max(1, int((MAX_PIXELS * points.width() / points.height()) ** 0.5))
            high = max(1, round(wide * points.height() / points.width()))
        try:
            image = document.render(page, QSize(wide, high))
        except Exception as error:
            log.exception("could not draw page %s of %s", page, path)
            raise FileProblem("这一页画不出来。") from error
        if image.isNull():
            raise FileProblem("这一页画不出来。")
        data = imaging.encode(_on_white(image), "PNG")
        if not data:
            raise FileProblem("这一页画不出来。")
        return f"data:image/png;base64,{base64.b64encode(data).decode('ascii')}"

    def _open(self, path: Path) -> QPdfDocument:
        size = preview_size(path, "pdf")
        try:
            key = (str(path), path.stat().st_mtime_ns, size)
        except OSError as error:
            raise FileProblem(f"读不了这个文件：{error.strerror or error}。") from error
        if key == self._key and self._document is not None:
            return self._document
        self.close()
        try:
            data = path.read_bytes()
        except OSError as error:
            raise FileProblem(f"读不了这个文件：{error.strerror or error}。") from error
        if len(data) > PREVIEW_PDF_BYTES:
            raise FileProblem("PDF 太大，不在侧栏里预览：用默认程序打开。")
        raw = QByteArray(data)
        buffer = QBuffer(raw)
        buffer.open(QIODevice.OpenModeFlag.ReadOnly)
        document = QPdfDocument()
        document.load(buffer)
        if document.status() != QPdfDocument.Status.Ready:
            name = getattr(document.error(), "name", "")
            document.close()
            raise FileProblem(_PROBLEMS.get(name, "这个 PDF 打不开：用默认程序打开。"))
        if document.pageCount() < 1:
            document.close()
            raise FileProblem("这个 PDF 没有页面。")
        self._key, self._document, self._keep = key, document, (raw, buffer)
        return document


def _on_white(image: QImage) -> QImage:
    """A page is paper: whatever the renderer leaves transparent is white."""
    if not image.hasAlphaChannel():
        return image
    flat = QImage(image.size(), QImage.Format.Format_RGB32)
    flat.fill(QColor("white"))
    painter = QPainter(flat)
    painter.drawImage(0, 0, image)
    painter.end()
    return flat
