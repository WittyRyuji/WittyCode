"""The PDF pages of the side panel, drawn by the real QtPdf (a PDF written by Qt itself, offscreen)."""

from __future__ import annotations

import base64
import os

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
pytest.importorskip("PySide6.QtPdf")

from PySide6.QtCore import QMarginsF
from PySide6.QtGui import QFont, QImage, QPageLayout, QPageSize, QPainter, QPdfWriter

from desktop.core.files import FileProblem
from desktop.ui.pdfview import MAX_PIXELS, MAX_WIDTH, MIN_WIDTH, PdfReader


def write_pdf(path, pages: int, *, size: QPageSize.PageSizeId = QPageSize.PageSizeId.A4) -> None:
    writer = QPdfWriter(str(path))
    writer.setPageSize(QPageSize(size))
    writer.setPageMargins(QMarginsF(0, 0, 0, 0), QPageLayout.Unit.Millimeter)
    painter = QPainter(writer)
    painter.setFont(QFont("Arial", 40))
    for number in range(pages):
        if number:
            writer.newPage()
        painter.drawText(400, 1200, f"PAGE {number + 1}")
    painter.end()


def decode(url: str) -> QImage:
    assert url.startswith("data:image/png;base64,")
    image = QImage.fromData(base64.b64decode(url.split(",", 1)[1]), "PNG")
    assert not image.isNull()
    return image


def dark_pixels(image: QImage) -> int:
    return sum(1 for y in range(0, image.height(), 3) for x in range(0, image.width(), 3) if image.pixelColor(x, y).lightness() < 128)


@pytest.fixture
def reader(qapp):
    found = PdfReader()
    yield found
    found.close()


def test_the_pages_have_the_sizes_of_the_document(tmp_path, reader):
    write_pdf(tmp_path / "a.pdf", 3)
    sizes = reader.pages(str(tmp_path / "a.pdf"))
    assert len(sizes) == 3
    for width, height in sizes:
        assert (round(width), round(height)) == (595, 842), "A4 in points"


def test_a_page_is_drawn_on_white_at_the_width_asked_for(tmp_path, reader):
    write_pdf(tmp_path / "a.pdf", 2)
    image = decode(reader.page_image(str(tmp_path / "a.pdf"), 0, 600))
    assert image.width() == 600 and abs(image.height() - round(600 * 842 / 595)) <= 1
    assert image.pixelColor(2, 2).name() == "#ffffff" and image.pixelColor(2, 2).alpha() == 255, "paper is white and opaque"
    assert dark_pixels(image) > 20, "the text of the page is drawn"
    other = decode(reader.page_image(str(tmp_path / "a.pdf"), 1, 600))
    assert other.size() == image.size()


def test_the_width_is_kept_between_the_limits_and_the_pixels_under_the_ceiling(tmp_path, reader):
    write_pdf(tmp_path / "a.pdf", 1)
    path = str(tmp_path / "a.pdf")
    assert decode(reader.page_image(path, 0, 10)).width() == MIN_WIDTH
    assert decode(reader.page_image(path, 0, 100_000)).width() <= MAX_WIDTH
    big = decode(reader.page_image(path, 0, 100_000))
    assert big.width() * big.height() <= MAX_PIXELS


def test_a_page_that_is_not_there_and_a_file_that_is_not_a_pdf_are_sentences(tmp_path, reader):
    write_pdf(tmp_path / "a.pdf", 2)
    (tmp_path / "bad.pdf").write_bytes(b"%PDF-1.4 this is not really a document")
    (tmp_path / "empty.pdf").write_bytes(b"")
    with pytest.raises(FileProblem, match="没有第 3 页（共 2 页）"):
        reader.page_image(str(tmp_path / "a.pdf"), 2, 600)
    with pytest.raises(FileProblem, match="没有第 0 页"):
        reader.page_image(str(tmp_path / "a.pdf"), -1, 600)
    for name in ("bad.pdf", "empty.pdf"):
        with pytest.raises(FileProblem):
            reader.pages(str(tmp_path / name))
    with pytest.raises(FileProblem, match="读不了"):
        reader.pages(str(tmp_path / "gone.pdf"))


def test_a_changed_file_is_read_again_and_an_open_one_can_still_be_replaced(tmp_path, reader):
    path = tmp_path / "a.pdf"
    write_pdf(path, 1)
    assert len(reader.pages(str(path))) == 1
    write_pdf(path, 4)  # the document is read from memory: nothing holds the file on disk
    os.utime(path, ns=(1, path.stat().st_mtime_ns + 5_000_000_000))
    assert len(reader.pages(str(path))) == 4, "the cached document is not used after the file changed"
    path.unlink()
    assert not path.exists()


def test_a_pdf_over_the_size_limit_is_refused_before_it_is_read(tmp_path, reader, monkeypatch):
    from desktop.core import files

    write_pdf(tmp_path / "a.pdf", 1)
    monkeypatch.setattr(files, "PREVIEW_PDF_BYTES", 100)
    with pytest.raises(FileProblem, match="太大"):
        reader.pages(str(tmp_path / "a.pdf"))
