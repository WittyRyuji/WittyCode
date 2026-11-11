"""Small files with known contents, for the tests and the live check of attachments.

Nothing here is imported by the application itself.
"""

from __future__ import annotations

from collections.abc import Sequence

from PySide6.QtCore import QRect, Qt
from PySide6.QtGui import QColor, QFont, QGuiApplication, QImage, QPainter


def make_pdf(lines: Sequence[str]) -> bytes:
    """A one-page PDF whose text layer is exactly ``lines`` (Latin-1 text only, Helvetica, no embedded fonts).

    Written by hand so that no PDF library is needed; Qt's PDF module and Droid both read it.
    """

    def escape(line: str) -> str:
        return line.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")

    operations = ["BT", "/F1 14 Tf", "72 720 Td", "18 TL"]
    for index, line in enumerate(lines):
        operations.append(f"({escape(line)}) Tj" if index == 0 else f"T* ({escape(line)}) Tj")
    operations.append("ET")
    stream = "\n".join(operations).encode("latin-1")
    objects = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Contents 4 0 R /Resources << /Font << /F1 5 0 R >> >> >>",
        b"<< /Length %d >>\nstream\n" % len(stream) + stream + b"\nendstream",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
    ]
    out = bytearray(b"%PDF-1.4\n")
    offsets: list[int] = []
    for number, body in enumerate(objects, 1):
        offsets.append(len(out))
        out += b"%d 0 obj\n" % number + body + b"\nendobj\n"
    table = len(out)
    out += b"xref\n0 %d\n" % (len(objects) + 1) + b"0000000000 65535 f \n"
    for offset in offsets:
        out += b"%010d 00000 n \n" % offset
    out += b"trailer\n<< /Size %d /Root 1 0 R >>\nstartxref\n%d\n%%%%EOF\n" % (len(objects) + 1, table)
    return bytes(out)


def sample_picture(label: str = "TANGERINE-42", width: int = 640, height: int = 360) -> QImage:
    """A picture a model can describe without guessing: a red square, a blue circle and a big word on yellow."""
    image = QImage(width, height, QImage.Format.Format_RGB32)
    image.fill(QColor("#fff3a0"))
    painter = QPainter(image)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    painter.setPen(Qt.PenStyle.NoPen)
    painter.setBrush(QColor("#d62828"))
    painter.drawRect(QRect(int(width * 0.06), int(height * 0.1), int(width * 0.3), int(height * 0.45)))
    painter.setBrush(QColor("#1d4ed8"))
    painter.drawEllipse(QRect(int(width * 0.62), int(height * 0.08), int(width * 0.3), int(height * 0.48)))
    if QGuiApplication.instance() is not None:  # Qt aborts when text is drawn before the font database exists
        font = QFont("Arial")
        font.setBold(True)
        font.setPixelSize(max(12, height // 7))
        painter.setFont(font)
        painter.setPen(QColor("#111111"))
        painter.drawText(QRect(0, int(height * 0.66), width, int(height * 0.3)), Qt.AlignmentFlag.AlignCenter, label)
    painter.end()
    return image
