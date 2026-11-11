"""Turning files, clipboard pictures and dropped data into ``Attachment`` values.

The rules (what goes where, what is refused and why):

- PNG / JPEG / GIF / WebP (by signature, not by extension) go as they are when they fit (5 MiB, 8000 px);
  oversize ones are scaled down and recompressed (``imaging.fit_image``) and marked "已压缩";
- other pictures Qt can decode (BMP, TIFF, ICO, a clipboard bitmap ...) are re-encoded as PNG;
- PDF up to 3 MiB; its text layer goes along for models that cannot read PDFs themselves;
- text: files with a text or source extension, or anything that is clean UTF-8 / UTF-16 with BOM, up to 1 MiB;
- everything else (archives, executables, Office files, folders ...) and everything too large is refused
  with a reason and, when it is a file, its path, so that the user can have the path typed into the prompt.

Nothing here needs a window; all of it also runs without a ``QApplication``.
"""

from __future__ import annotations

import stat
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from pathlib import Path

from PySide6.QtCore import QEvent, QMimeData, Qt
from PySide6.QtGui import QDropEvent, QImage, QPixmap

from desktop.core.attachments import (
    HARD_ATTACHMENTS,
    MAX_IMAGE_BYTES,
    MAX_PDF_BYTES,
    MAX_SOURCE_BYTES,
    MAX_TEXT_BYTES,
    MIB,
    Attachment,
    Rejection,
    decode_text,
    format_size,
    image_attachment,
    is_pdf,
    is_text_name,
    pdf_attachment,
    sniff_image,
    text_attachment,
)
from desktop.ui import imaging

PASTED_NAME = "粘贴的图片"
COMPRESSED = "已压缩"
FIRST_READ = max(MAX_TEXT_BYTES, MAX_PDF_BYTES) + 1
"""Whole files up to this size are read in one go; of bigger ones only the start is looked at first."""

BINARY = "这是二进制文件，不是图片、PDF 或文本，模型读不了它的内容。"
BROKEN_IMAGE = "图片已损坏，或者格式无法识别。"


@dataclass(slots=True)
class Loaded:
    attachments: list[Attachment] = field(default_factory=list)
    rejections: list[Rejection] = field(default_factory=list)

    def add(self, outcome: Attachment | Rejection) -> None:
        (self.rejections if isinstance(outcome, Rejection) else self.attachments).append(outcome)


# --------------------------------------------------------------------------- pictures

def _fitted(name: str, where: str, image: QImage, *, lossless: bool, converted: bool) -> Attachment | Rejection:
    fitted = imaging.fit_image(image, lossless=lossless)
    if fitted is None:
        return Rejection(name, f"图片太大，压缩之后仍然超过 {MAX_IMAGE_BYTES // MIB} MB。", where)
    note = COMPRESSED if fitted.compressed or not converted else "已转为 PNG"
    return image_attachment(name, fitted.data, fitted.media_type, note=note, path=where)


def _picture(name: str, where: str, data: bytes, media_type: str) -> Attachment | Rejection:
    """A PNG / JPEG / GIF / WebP file: untouched when it fits, otherwise shrunk."""
    size = imaging.probe_size(data)
    image: QImage | None = None
    if size is None:
        image = imaging.decode(data)  # some files do not announce their size in the header
        if image is None:
            return Rejection(name, BROKEN_IMAGE, where)
        longest = max(image.width(), image.height())
    else:
        longest = max(size.width(), size.height())
    if len(data) <= MAX_IMAGE_BYTES and longest <= imaging.PIXEL_LIMIT:
        return image_attachment(name, data, media_type, path=where)
    image = image or imaging.decode(data)
    if image is None:
        return Rejection(name, BROKEN_IMAGE, where)
    return _fitted(name, where, image, lossless=media_type != "image/jpeg", converted=False)


def _converted(name: str, where: str, data: bytes) -> Attachment | Rejection | None:
    """Another picture format (BMP, TIFF ...) as PNG; ``None`` when Qt cannot decode it after all."""
    image = imaging.decode(data)
    if image is None:
        return None
    return _fitted(name, where, image, lossless=True, converted=True)


def load_image(image: QImage, name: str = PASTED_NAME) -> Attachment | Rejection:
    """A picture that has no file (clipboard)."""
    if image.isNull():
        return Rejection(name, BROKEN_IMAGE)
    fitted = imaging.fit_image(image)
    if fitted is None:
        return Rejection(name, f"图片太大，压缩之后仍然超过 {MAX_IMAGE_BYTES // MIB} MB。")
    extension = ".png" if fitted.media_type == "image/png" else ".jpg"
    return image_attachment(name + extension, fitted.data, fitted.media_type, note=COMPRESSED if fitted.compressed else "")


# --------------------------------------------------------------------------- files

def _text(name: str, where: str, size: int, whole: Callable[[], bytes], *, trusted: bool) -> Attachment | Rejection:
    if size > MAX_TEXT_BYTES:
        return Rejection(name, f"文本文件有 {format_size(size)}，超过 {MAX_TEXT_BYTES // MIB} MB 的上限。", where)
    decoded = decode_text(whole(), trusted=trusted)
    if decoded is None:
        return Rejection(name, BINARY, where)
    text, encoding = decoded
    note = "" if encoding in ("UTF-8", "UTF-16") else f"按 {encoding} 读取"
    return text_attachment(name, text, note=note, path=where)


def _classify(name: str, where: str, size: int, head: bytes, rest: Callable[[], bytes]) -> Attachment | Rejection:
    def whole() -> bytes:
        return head if len(head) >= size else head + rest()

    partial = len(head) < size
    if is_pdf(head):
        if size > MAX_PDF_BYTES:
            return Rejection(name, f"PDF 有 {format_size(size)}，超过 {MAX_PDF_BYTES // MIB} MB 的上限。", where)
        data = whole()
        return pdf_attachment(name, data, parsed_text=imaging.pdf_text(data), path=where)
    media_type = sniff_image(head)
    if media_type:
        return _picture(name, where, whole(), media_type)
    trusted = is_text_name(name)
    if trusted and decode_text(head, trusted=True, partial=partial) is not None:
        return _text(name, where, size, whole, trusted=True)
    if imaging.can_read(head):
        converted = _converted(name, where, whole())
        if converted is not None:
            return converted
    if not trusted and decode_text(head, partial=partial) is not None:
        return _text(name, where, size, whole, trusted=False)
    return Rejection(name, BINARY, where)


def load_path(path: str | Path) -> Attachment | Rejection:
    path = Path(path)
    where = str(path.absolute())
    name = path.name or where
    try:
        info = path.stat()
    except OSError:
        return Rejection(name, "找不到这个文件，或者没有权限读取它。")
    if stat.S_ISDIR(info.st_mode):
        return Rejection(name, "这是文件夹，附件只能是文件。", where)
    if not stat.S_ISREG(info.st_mode):
        return Rejection(name, "这不是普通文件。", where)
    size = info.st_size
    if size == 0:
        return Rejection(name, "文件是空的。")
    if size > MAX_SOURCE_BYTES:
        return Rejection(name, f"文件太大（{format_size(size)}，上限 {MAX_SOURCE_BYTES // MIB} MB）。", where)
    try:
        with path.open("rb") as handle:
            head = handle.read(min(size, FIRST_READ))
            return _classify(name, where, size, head, handle.read)
    except OSError:
        return Rejection(name, "读取文件失败（可能被其他程序占用，或没有权限）。", where)


def load_bytes(name: str, data: bytes) -> Attachment | Rejection:
    """A file the page has read (pasted or dropped): judged like a file on disk, only without a path."""
    name = name.strip() or PASTED_NAME
    size = len(data)
    if size == 0:
        return Rejection(name, "文件是空的。")
    if size > MAX_SOURCE_BYTES:
        return Rejection(name, f"文件太大（{format_size(size)}，上限 {MAX_SOURCE_BYTES // MIB} MB）。")
    return _classify(name, "", size, data[:FIRST_READ], lambda: data[FIRST_READ:])


def load_paths(paths: Sequence[str | Path], *, room: int = HARD_ATTACHMENTS) -> Loaded:
    """Load up to ``room`` files; the rest are refused without being opened (a dropped folder tree can be huge)."""
    result = Loaded()
    for index, path in enumerate(paths):
        if index >= max(room, 0):
            where = str(Path(path).absolute())
            result.rejections.append(Rejection(Path(path).name or where, f"一次最多附加 {HARD_ATTACHMENTS} 个文件。", where))
            continue
        result.add(load_path(path))
    return result


# --------------------------------------------------------------------------- clipboard and drag-and-drop data

DROP_EVENTS = (QEvent.Type.DragEnter, QEvent.Type.DragMove, QEvent.Type.Drop)


def copy_action(event: QDropEvent) -> Qt.DropAction:
    """How to answer a drag of files or a picture: copy, never move.

    Explorer deletes the original files when a drop is accepted as a move; attaching must leave them alone.
    ``IgnoreAction`` means the drag offers nothing else, and so cannot be accepted.
    """
    offered = event.possibleActions()
    for action in (Qt.DropAction.CopyAction, Qt.DropAction.LinkAction):
        if offered & action:
            return action
    return Qt.DropAction.IgnoreAction


def accept_drag(event: QDropEvent) -> bool:
    """Accept a drag that carries files or a picture (as a copy); ``False`` for anything else, which is left alone."""
    action = copy_action(event)
    if action is Qt.DropAction.IgnoreAction or not wants_attachments(event.mimeData()):
        return False
    event.setDropAction(action)
    event.accept()
    return True


def claim_drag(event: QDropEvent) -> bool:
    """Deal with a drag of files or a picture: accept it as a copy or, when it offers nothing but a move, refuse it.

    ``False`` means the drag is none of our business and the widget may handle it as it would otherwise. A refused
    drag must not reach a widget that accepts files as text or navigates to them: that one would take it as a move,
    and Explorer then deletes the originals. Check ``event.isAccepted()`` to tell the two outcomes apart.
    """
    if accept_drag(event):
        return True
    if wants_attachments(event.mimeData()):
        event.ignore()
        return True
    return False


def mime_paths(mime: QMimeData) -> list[str]:
    """Local files in a clipboard or drag payload (Explorer puts them there as ``file:`` URLs)."""
    return [url.toLocalFile() for url in mime.urls() if url.isLocalFile() and url.toLocalFile()]


def mime_image(mime: QMimeData) -> QImage | None:
    if not mime.hasImage():
        return None
    data = mime.imageData()
    if isinstance(data, QPixmap):
        data = data.toImage()
    return data if isinstance(data, QImage) and not data.isNull() else None


def _has_plain_text(mime: QMimeData) -> bool:
    return mime.hasFormat("text/plain") and bool(mime.text().strip())


def wants_attachments(mime: QMimeData) -> bool:
    """Whether pasting or dropping this should attach something instead of inserting text.

    Local files always attach. A picture attaches unless the data also carries text (cells copied from a
    spreadsheet come with a picture of themselves, and the user wants the text then).
    """
    if mime_paths(mime):
        return True
    return mime.hasImage() and not _has_plain_text(mime)


def load_mime(mime: QMimeData, *, room: int = HARD_ATTACHMENTS) -> Loaded:
    paths = mime_paths(mime)
    if paths:
        return load_paths(paths, room=room)
    result = Loaded()
    image = mime_image(mime)
    if image is not None:
        result.add(load_image(image))
    return result
