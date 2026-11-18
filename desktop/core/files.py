"""Files the conversation talks about: finding one from the way the model wrote it, reading it for the side panel, and what may
be opened with the system's default program.

The model writes a path the way it was told to (`docs/x.md`, `src/a.py:42`, `D:\\work\\b.txt`); a click on it is the user's own
act, so any path on the machine may be shown. Opening with the default program is another matter: for a program or a
script that means running it, and a path in a model's text must never run anything by one click.
"""

from __future__ import annotations

import base64
import codecs
import io
import os
import re
import struct
import zipfile
import zlib
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import unquote, urlsplit

PANEL_BYTES = 1_500_000
"""How much of a file the side panel reads."""
PANEL_LINES = 6_000
"""How many lines it shows."""

MARKDOWN = {".md", ".markdown", ".mdx"}
RUNNABLE = {
    ".exe", ".com", ".bat", ".cmd", ".msi", ".msp", ".scr", ".pif", ".lnk", ".url", ".reg", ".hta", ".cpl", ".dll", ".sys",
    ".ps1", ".psm1", ".psd1", ".vbs", ".vbe", ".js", ".jse", ".wsf", ".wsh", ".jar", ".appx", ".msix", ".application",
    ".py", ".pyw", ".pyz", ".sh", ".bash", ".pl", ".rb", ".php",
}
"""Opening one of these with the default program usually runs it."""

PREVIEW_IMAGES = {
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".gif": "image/gif",
    ".webp": "image/webp",
    ".bmp": "image/bmp",
    ".svg": "image/svg+xml",
    ".ico": "image/x-icon",
}
"""The pictures the side panel shows, with the type the page needs to draw them (an SVG in an ``<img>`` runs no script)."""
PREVIEW_IMAGE_BYTES = 25_000_000
PREVIEW_PDF_BYTES = 80_000_000
PREVIEW_OFFICE_BYTES = 30_000_000
OFFICE_ZIP_ENTRIES = 5_000
OFFICE_ZIP_BYTES = 300_000_000

_OFFICE_TYPES = {
    **dict.fromkeys((".docx", ".docm", ".dotx"), "word"),
    **dict.fromkeys((".xlsx", ".xlsm", ".xls"), "sheet"),
    **dict.fromkeys((".pptx", ".pptm", ".ppsx"), "slides"),
}
_OFFICE_MIME = {
    "word": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    "sheet": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    "slides": "application/vnd.openxmlformats-officedocument.presentationml.presentation",
}
_LEGACY_OFFICE = {".doc": ".docx", ".ppt": ".pptx"}

_SUFFIX = re.compile(r":(\d+)(?:-(\d+))?(?::\d+)?$")


class FileProblem(Exception):
    """Why a file cannot be shown: one Chinese sentence for the user."""


@dataclass(frozen=True, slots=True)
class Ref:
    path: str
    line: int | None = None
    end: int | None = None


@dataclass(frozen=True, slots=True)
class PanelFile:
    path: Path
    text: str
    encoding: str
    size: int
    truncated: bool
    """Only the first part of the file is in ``text``."""
    lines: int
    """Lines in ``text``."""
    markdown: bool


def parse_ref(raw: str) -> Ref:
    """``a/b.py:12-20`` -> path, first and last line. The colon of a drive letter is not a line number."""
    text = raw.strip().strip("`\"'").strip()
    line = end = None
    found = _SUFFIX.search(text)
    if found is not None and found.start() > 1:
        text = text[: found.start()]
        line = int(found.group(1))
        end = int(found.group(2)) if found.group(2) else None
        if end is not None and end < line:
            line, end = end, line
    return Ref(text, line, end)


def _from_uri(text: str) -> str:
    parts = urlsplit(text)
    path = unquote(parts.path)
    if parts.netloc and parts.netloc != "localhost":
        return "\\\\" + parts.netloc + path.replace("/", "\\")
    if re.match(r"^/[A-Za-z]:", path):
        path = path[1:]
    return path


def resolve(raw: str, project: str | Path) -> tuple[Path, Ref]:
    """The file a reference names, looked for in the project folder when it is not absolute. Raises ``FileProblem``."""
    ref = parse_ref(raw)
    text = ref.path
    if not text:
        raise FileProblem("没有指定文件。")
    if text.lower().startswith("file:"):
        text = _from_uri(text)
    text = os.path.expandvars(os.path.expanduser(text)) if text.startswith("~") else text
    if text.startswith(("\\\\", "//")):
        # Looking for a file on another computer makes Windows sign in to it (and hand over the user's hash): the text is
        # the model's, and a click on it is not a decision to go there.
        raise FileProblem("不打开网络位置或设备路径上的文件。需要的话，请在资源管理器里自己打开。")
    path = Path(text)
    if not path.is_absolute():
        path = Path(project) / path
    path = Path(os.path.normpath(path))
    if not path.exists():
        raise FileProblem(f"找不到文件「{ref.path}」（按项目文件夹 {project} 找过）。")
    return path, ref


def is_runnable(path: Path) -> bool:
    """Whether opening ``path`` with its default program may run it."""
    return path.suffix.lower() in RUNNABLE


def preview_kind(path: Path) -> str | None:
    """The kind the panel draws instead of reading as text (by suffix; never a folder)."""
    suffix = path.suffix.lower()
    if path.is_dir():
        return None
    if suffix == ".pdf":
        return "pdf"
    if suffix in PREVIEW_IMAGES:
        return "image"
    return _OFFICE_TYPES.get(suffix)


def _too_large(size: int) -> FileProblem:
    return FileProblem(f"文件有 {size / 1_000_000:.0f} MB，太大了，不在侧栏里预览：用默认程序打开。")


def preview_size(path: Path, kind: str) -> int:
    """The size of a file to preview. Raises ``FileProblem`` when it cannot be read or is too big to be drawn here."""
    try:
        size = path.stat().st_size
    except OSError as error:
        raise FileProblem(f"读不了这个文件：{error.strerror or error}。") from error
    limit = {"pdf": PREVIEW_PDF_BYTES, "image": PREVIEW_IMAGE_BYTES}.get(kind, PREVIEW_OFFICE_BYTES)
    if size > limit:
        raise _too_large(size)
    return size


def image_data_url(path: Path) -> str:
    """The picture as a ``data:`` URI for an ``<img>`` of the page. Raises ``FileProblem``."""
    preview_size(path, "image")
    try:
        data = path.read_bytes()
    except OSError as error:
        raise FileProblem(f"读不了这个文件：{error.strerror or error}。") from error
    return f"data:{PREVIEW_IMAGES[path.suffix.lower()]};base64,{base64.b64encode(data).decode('ascii')}"


def _check_office_entry(data: bytes, archive: zipfile.ZipFile, entry: zipfile.ZipInfo) -> None:
    # ZipExtFile checks the local header, but its read() stops at the declared size. The page's ZIP readers may inflate
    # beyond it, so check the real size and CRC in bounded chunks before sharing the same bytes with the page.
    with archive.open(entry):
        offset = entry.header_offset
        name_length, extra_length = struct.unpack_from("<HH", data, offset + 26)
        start = offset + 30 + name_length + extra_length
        end = start + entry.compress_size
        remaining, crc = entry.file_size, 0
        inflater = zlib.decompressobj(-15) if entry.compress_type == zipfile.ZIP_DEFLATED else None
        for at in range(start, end, 65_536):
            raw = data[at:min(at + 65_536, end)]
            while raw:
                chunk = inflater.decompress(raw, min(remaining + 1, 256_000)) if inflater else raw
                remaining -= len(chunk)
                if remaining < 0:
                    raise zipfile.BadZipFile("entry exceeds declared size")
                crc = zlib.crc32(chunk, crc)
                raw = inflater.unconsumed_tail if inflater else b""
        if remaining or crc != entry.CRC or (inflater and (not inflater.eof or inflater.unused_data)):
            raise zipfile.BadZipFile("incomplete entry or incorrect checksum")


def office_data_url(path: Path, kind: str) -> str:
    """Read a bounded snapshot, validate the archive, and pass only those exact bytes to the page."""
    preview_size(path, kind)
    try:
        with path.open("rb") as handle:
            data = handle.read(PREVIEW_OFFICE_BYTES + 1)
    except OSError as error:
        raise FileProblem(f"读不了这个文件：{error.strerror or error}。") from error
    if len(data) > PREVIEW_OFFICE_BYTES:
        raise _too_large(len(data))
    mime = _OFFICE_MIME[kind]
    if path.suffix.lower() == ".xls":
        # Excel 97–2003 compound binary files and earlier raw BIFF workbooks, not arbitrary SheetJS inputs.
        if not data.startswith((bytes.fromhex("D0 CF 11 E0 A1 B1 1A E1"), b"\x09\x00", b"\x09\x02", b"\x09\x04", b"\x09\x08")):
            raise FileProblem("这不是有效的 Excel 工作簿：用默认程序打开。")
        mime = "application/vnd.ms-excel"
    else:
        try:
            with zipfile.ZipFile(io.BytesIO(data)) as archive:
                entries = archive.infolist()
                if len(entries) > OFFICE_ZIP_ENTRIES or sum(e.file_size for e in entries) > OFFICE_ZIP_BYTES:
                    raise FileProblem("Office 文件解压后太大或文件过多，不能在侧栏里预览：用默认程序打开。")
                names = {e.filename for e in entries}
                if len(names) != len(entries) or not {"[Content_Types].xml", "_rels/.rels"} <= names:
                    raise zipfile.BadZipFile("missing or duplicate package entries")
                for entry in entries:
                    if entry.compress_type not in (zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED):
                        raise zipfile.BadZipFile("unsupported compression")
                    _check_office_entry(data, archive, entry)
        except (zipfile.BadZipFile, RuntimeError, NotImplementedError, struct.error, UnicodeError, zlib.error) as error:
            raise FileProblem("这不是有效的 Office 文件，或文件已损坏、加密：用默认程序打开。") from error
    return f"data:{mime};base64,{base64.b64encode(data).decode('ascii')}"


def _decode(data: bytes, cut: bool) -> tuple[str, str]:
    if data.startswith(codecs.BOM_UTF8):
        return data[len(codecs.BOM_UTF8) :].decode("utf-8", errors="replace"), "UTF-8 (BOM)"
    if data.startswith((codecs.BOM_UTF16_LE, codecs.BOM_UTF16_BE)):
        body = data if len(data) % 2 == 0 else data[:-1]
        return body.decode("utf-16", errors="replace"), "UTF-16"
    tail = 0
    while True:
        try:
            return data[: len(data) - tail].decode("utf-8"), "UTF-8"
        except UnicodeDecodeError as error:
            if cut and tail < 3 and error.start >= len(data) - 4:
                tail += 1  # (the cut fell inside a character)
                continue
            break
    try:
        return data.decode("gb18030"), "GB18030"
    except UnicodeDecodeError:
        return data.decode("utf-8", errors="replace"), "UTF-8 (有无法识别的字符)"


def read_for_panel(path: Path, *, limit: int = PANEL_BYTES) -> PanelFile:
    """The text of ``path``, for the side panel. Raises ``FileProblem`` for a folder, an unreadable file or a binary one."""
    if path.is_dir():
        raise FileProblem("这是文件夹，不是文件：可以在文件夹中显示它。")
    suffix = path.suffix.lower()
    if suffix in _LEGACY_OFFICE:
        raise FileProblem(f"旧版 Office 格式（{suffix}）不能在侧栏里预览：用默认程序打开，或另存为 {_LEGACY_OFFICE[suffix]}。")
    try:
        size = path.stat().st_size
        with path.open("rb") as handle:
            data = handle.read(limit + 1)
    except OSError as error:
        raise FileProblem(f"读不了这个文件：{error.strerror or error}。") from error
    truncated = len(data) > limit
    data = data[:limit]
    head = data[:8192]
    if b"\x00" in head and not head.startswith((codecs.BOM_UTF16_LE, codecs.BOM_UTF16_BE)):
        raise FileProblem("这不是文本文件，不能在侧栏里看：用默认程序打开。")
    text, encoding = _decode(data, truncated)
    lines = text.count("\n") + (0 if text.endswith("\n") or not text else 1)
    return PanelFile(path, text, encoding, size, truncated, lines, path.suffix.lower() in MARKDOWN)
