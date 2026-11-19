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
import stat
import struct
import zipfile
import zlib
from contextlib import ExitStack
from dataclasses import dataclass
from pathlib import Path, PureWindowsPath
from typing import BinaryIO
from urllib.parse import unquote, urlsplit

from desktop.core.attachments import image_size, sniff_image

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
    ".cur": "image/x-icon",
    ".apng": "image/apng",
    ".avif": "image/avif",
    ".jfif": "image/jpeg",
    ".jpe": "image/jpeg",
}
"""Pictures the page draws directly (an SVG in an ``<img>`` runs no script)."""
CONVERTED_IMAGES = frozenset(".tif .tiff .tga .pbm .pgm .ppm .xbm .xpm .icns .wbmp .svgz".split())
_UNSUPPORTED_IMAGES = frozenset(".heic .heif .heics .heifs .hif .dng .cr2 .cr3 .nef .arw .raw .psd .eps .jxl .jp2 .j2k .dds .exr".split())
PREVIEW_VIDEOS = frozenset(".mp4 .m4v .mov .webm .mkv .avi .wmv .mpg .mpeg .ogv .3gp .ts .m2ts".split())
# The assistant body opts into a narrower set than an explicitly opened file inspector.
INLINE_IMAGES = frozenset(".png .apng .jpg .jpeg .jfif .jpe .gif .webp".split())
INLINE_VIDEOS = frozenset(".mp4 .m4v .mov .3gp .webm .mkv".split())
INLINE_IMAGE_BYTES = 6 * 1024**2
INLINE_IMAGE_PIXELS = 16_000_000
INLINE_VIDEO_BYTES = 512 * 1024**2
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


_INLINE_PATH = "正文媒体必须是项目内的普通本地文件，不支持绝对路径、链接或网络位置。"
_INLINE_NAME = re.compile(r'[^\x00-\x1f<>:"|?*#%]+\Z')


def _inline_parts(raw: str, project: str | Path, suffixes: frozenset[str]) -> tuple[Path, tuple[str, ...]]:
    # Do not use parse_ref/resolve: they intentionally accept paths suitable only for an explicit file-panel click.
    if not isinstance(raw, str) or not 1 <= len(raw) <= 512 or raw != raw.strip():
        raise FileProblem(_INLINE_PATH)
    text = raw.replace("\\", "/").removeprefix("./")
    parts = tuple(text.split("/"))
    if len(parts) > 32 or any(
        not _INLINE_NAME.fullmatch(part) or part in (".", "..") or part.endswith((" ", "."))
        or part.startswith("~") or PureWindowsPath(part).is_reserved()
        for part in parts
    ):
        raise FileProblem(_INLINE_PATH)
    if Path(parts[-1]).suffix.lower() not in suffixes:
        raise FileProblem("不支持这种正文媒体格式。")
    root = Path(project)
    if not root.is_absolute() or ".." in root.parts or str(root).startswith(("\\\\", "//")):
        raise FileProblem(_INLINE_PATH)
    return root, parts


def _open_inline_windows(root: Path, parts: tuple[str, ...]) -> int:
    import ctypes as c
    import msvcrt
    from ctypes import wintypes as w

    kernel = c.WinDLL("kernel32", use_last_error=True)
    drive_type, create, attrs, close = (kernel.GetDriveTypeW, kernel.CreateFileW,
                                       kernel.GetFileInformationByHandleEx, kernel.CloseHandle)
    drive_type.argtypes, drive_type.restype = [w.LPCWSTR], w.UINT
    create.argtypes = [w.LPCWSTR, w.DWORD, w.DWORD, w.LPVOID, w.DWORD, w.DWORD, w.HANDLE]
    create.restype = w.HANDLE
    attrs.argtypes, attrs.restype = [w.HANDLE, c.c_int, w.LPVOID, w.DWORD], w.BOOL
    close.argtypes, close.restype = [w.HANDLE], w.BOOL
    # Including mapped drives: only removable, fixed, optical and RAM drives may be touched.
    if not re.fullmatch(r"[A-Za-z]:", root.drive) or drive_type(root.anchor) not in (2, 3, 5, 6):
        raise FileProblem(_INLINE_PATH)

    def open_one(path: Path, directory: bool) -> int:
        # OPEN_REPARSE_POINT opens each object itself, not its target (including junctions/cloud placeholders).
        # Share read only while descending, so an ancestor cannot be swapped or retagged before the final open.
        access, flags = (0x81, 0x02200000) if directory else (0x80000000, 0x00200000)
        handle = create(str(path), access, 1, None, 3, flags, None)
        if handle == c.c_void_p(-1).value:
            raise c.WinError(c.get_last_error())
        info = (w.DWORD * 2)()  # FILE_ATTRIBUTE_TAG_INFO
        if not attrs(handle, 9, c.byref(info), c.sizeof(info)):
            error = c.WinError(c.get_last_error())
            close(handle)
            raise error
        if info[0] & stat.FILE_ATTRIBUTE_REPARSE_POINT or bool(info[0] & stat.FILE_ATTRIBUTE_DIRECTORY) != directory:
            close(handle)
            raise FileProblem(_INLINE_PATH)
        return handle

    path = Path(root.anchor)
    with ExitStack() as ancestors:
        for part in (*root.parts[1:], *parts[:-1]):
            path /= part
            ancestors.callback(close, open_one(path, True))
        handle = open_one(path / parts[-1], False)
        try:
            return msvcrt.open_osfhandle(handle, os.O_RDONLY | os.O_BINARY)
        except BaseException:
            close(handle)
            raise


def _open_inline_posix(root: Path, parts: tuple[str, ...]) -> int:
    # The desktop target is Windows; keep descriptor-relative traversal for non-Windows core/test users too.
    flags = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK
    directory = os.open(root.anchor, flags | os.O_DIRECTORY)
    try:
        for part in (*root.parts[1:], *parts[:-1]):
            child = os.open(part, flags | os.O_DIRECTORY, dir_fd=directory)
            os.close(directory)
            directory = child
        return os.open(parts[-1], flags, dir_fd=directory)
    finally:
        os.close(directory)


def _open_inline(raw: str, project: str | Path, suffixes: frozenset[str], limit: int) -> tuple[Path, BinaryIO]:
    root, parts = _inline_parts(raw, project, suffixes)
    path = root.joinpath(*parts)
    try:
        fd = (_open_inline_windows if os.name == "nt" else _open_inline_posix)(root, parts)
        try:
            info = os.fstat(fd)
            if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
                raise FileProblem(_INLINE_PATH)
            if info.st_size > limit:
                raise FileProblem(f"文件超过 {limit // 1024**2} MiB，不能在正文中预览。")
            return path, os.fdopen(fd, "rb")
        except BaseException:
            os.close(fd)
            raise
    except FileNotFoundError as error:
        raise FileProblem("在此会话的项目中找不到这个媒体文件。") from error
    except OSError as error:
        raise FileProblem("读不了此会话项目中的媒体文件。") from error


def inline_image_url(raw: str, project: str | Path) -> str:
    path, source = _open_inline(raw, project, INLINE_IMAGES, INLINE_IMAGE_BYTES)
    try:
        with source:
            data = source.read(INLINE_IMAGE_BYTES + 1)
    except OSError as error:
        raise FileProblem("读不了此会话项目中的图片。") from error
    if len(data) > INLINE_IMAGE_BYTES:
        raise FileProblem("图片太大，不能在正文中预览。")
    mime = PREVIEW_IMAGES[path.suffix.lower()]
    actual = sniff_image(data)
    dimensions = image_size(data) if actual == ("image/png" if mime == "image/apng" else mime) else None
    if dimensions is None or min(dimensions) <= 0:
        raise FileProblem("图片格式不符或文件已损坏，不能在正文中预览。")
    if dimensions[0] * dimensions[1] > INLINE_IMAGE_PIXELS:
        raise FileProblem("图片像素太多，不能在正文中预览。")
    return _image_url(data, mime)


class InlineVideoSource(str):
    """A display path compatible with the platform bridge, plus the already verified open file for Qt.

    The decoder must use ``file`` and never reopen the path (which could have been swapped since validation).
    """
    file: BinaryIO

    def __new__(cls, path: Path, file: BinaryIO) -> InlineVideoSource:
        result = super().__new__(cls, str(path))
        result.file = file
        return result


def open_inline_video(raw: str, project: str | Path) -> InlineVideoSource:
    path, source = _open_inline(raw, project, INLINE_VIDEOS, INLINE_VIDEO_BYTES)
    try:
        # Keep the Python buffer untouched: Qt will use a duplicate of this descriptor and its offset.
        fd = source.fileno()
        header = os.read(fd, 12)
        if path.suffix.lower() in (".webm", ".mkv"):
            valid = header.startswith(b"\x1a\x45\xdf\xa3")  # EBML
        else:
            valid = len(header) == 12 and header[4:8] == b"ftyp"  # require an ISO BMFF file-type box, not a playlist
        if not valid:
            raise FileProblem("这不是支持的本地视频容器，不能在正文中预览。")
        os.lseek(fd, 0, os.SEEK_SET)
        return InlineVideoSource(path, source)
    except OSError as error:
        source.close()
        raise FileProblem("读不了此会话项目中的视频。") from error
    except BaseException:
        source.close()
        raise


def is_runnable(path: Path) -> bool:
    """Whether opening ``path`` with its default program may run it."""
    return path.suffix.lower() in RUNNABLE


def transport_stream_header(header: bytes) -> bool:
    """MPEG transport packets: sync bytes at consecutive 188/192/204-byte packet boundaries."""
    return any(
        header[offset] == header[offset + stride] == header[offset + 2 * stride] == 0x47
        for stride in (188, 192, 204)
        for offset in range(min(stride, len(header) - 2 * stride))
    )


def preview_kind(path: Path) -> str | None:
    """The kind the panel draws instead of reading as text (never a folder)."""
    suffix = path.suffix.lower()
    if path.is_dir():
        return None
    if suffix == ".pdf":
        return "pdf"
    if suffix in PREVIEW_IMAGES or suffix in CONVERTED_IMAGES:
        return "image"
    if suffix == ".ts":
        # .ts also names TypeScript source. Only treat a transport-stream signature as video.
        try:
            with path.open("rb") as handle:
                return "video" if transport_stream_header(handle.read(1024)) else None
        except OSError:
            return None
    if suffix in PREVIEW_VIDEOS:
        return "video"
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
    if kind != "video" and size > limit:
        raise _too_large(size)
    return size


def image_bytes(path: Path) -> bytes:
    """Read a bounded picture even if the file grows after the size check."""
    preview_size(path, "image")
    try:
        with path.open("rb") as handle:
            data = handle.read(PREVIEW_IMAGE_BYTES + 1)
    except OSError as error:
        raise FileProblem(f"读不了这个文件：{error.strerror or error}。") from error
    if len(data) > PREVIEW_IMAGE_BYTES:
        raise _too_large(len(data))
    return data


def _image_url(data: bytes, mime: str) -> str:
    return f"data:{mime};base64,{base64.b64encode(data).decode('ascii')}"


def image_data_url(path: Path) -> str:
    """Send native pictures unchanged so animated images keep all of their frames."""
    return _image_url(image_bytes(path), PREVIEW_IMAGES[path.suffix.lower()])


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
    if suffix in _UNSUPPORTED_IMAGES:
        raise FileProblem(f"{suffix.upper()} 图片不能在侧栏里预览：用默认程序打开。")
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
