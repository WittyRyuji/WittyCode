"""Decode text from user-attached archives without extracting files to the filesystem."""
from __future__ import annotations

import gzip
import io
import json
import stat
import tarfile
import zipfile
import zlib
from pathlib import PureWindowsPath
from typing import BinaryIO

from desktop.core.attachments import (
    MAX_TEXT_BYTES, MIB, Attachment, Rejection, decode_text, is_pdf, is_text_name, sniff_image, text_attachment,
)

MAX_SOURCE = 8 * MIB
MAX_EXPANDED = 16 * MIB
MAX_MEMBERS = 256


class ArchiveError(ValueError):
    pass


def archive_kind(name: str) -> str:
    name = name.lower()
    if name.endswith(".zip"):
        return "zip"
    if name.endswith((".tar.gz", ".tgz")):
        return "tgz"
    if name.endswith(".tar"):
        return "tar"
    return ""


def member_name(raw: str, *, directory: bool) -> str:
    if raw.startswith("/"):
        raise ArchiveError("压缩包含不安全的文件名，未添加附件。")
    if directory:
        raw = raw.rstrip("/")
    while raw.startswith("./"):
        raw = raw[2:]
    if directory and raw in ("", "."):
        return ""
    parts = raw.split("/")
    if (len(raw.encode("utf-8", "replace")) > 512 or "\\" in raw or ":" in raw
            or any(ord(char) < 32 or ord(char) == 127 for char in raw)
            or any(not part or part.rstrip(". ") != part or PureWindowsPath(part).is_reserved() for part in parts)):
        raise ArchiveError("压缩包含不安全的文件名，未添加附件。")
    return raw


class TextBundle:
    def __init__(self) -> None:
        self.names: set[str] = set()
        self.count = self.expanded = self.text_bytes = 0
        self.sections: list[str] = []
        self.skipped: list[str] = []

    def check(self, raw: str, size: int, *, directory: bool) -> str:
        self.count += 1
        self.expanded += size
        if self.count > MAX_MEMBERS or size < 0 or self.expanded > MAX_EXPANDED:
            raise ArchiveError(f"压缩包超出解包限制（最多 {MAX_MEMBERS} 项，展开不超过 {MAX_EXPANDED // MIB} MB）。")
        name = member_name(raw, directory=directory)
        if name:
            key = name.casefold()
            if key in self.names:
                raise ArchiveError("压缩包含重名条目，未添加附件。")
            self.names.add(key)
        return name

    def read(self, name: str, source: BinaryIO, size: int) -> None:
        data = source.read(size + 1)
        if len(data) != size:
            raise ArchiveError("压缩包文件长度异常，未添加附件。")
        quoted = json.dumps(name, ensure_ascii=False)
        decoded = None
        if not (is_pdf(data) or sniff_image(data) or archive_kind(name)):
            if data:
                decoded = decode_text(data, trusted=is_text_name(name))
            elif is_text_name(name):
                decoded = ("", "UTF-8")
        if decoded is None:
            self.skipped.append(quoted)
            return
        section = f"\n\n--- 文件 {quoted} ---\n{decoded[0]}"
        self.text_bytes += len(section.encode("utf-8"))
        if self.text_bytes > MAX_TEXT_BYTES:
            raise ArchiveError(f"解包后的文本超过 {MAX_TEXT_BYTES // MIB} MB，请拆成几个较小的压缩包。")
        self.sections.append(section)

    def attachment(self, name: str, where: str) -> Attachment:
        if not self.sections:
            raise ArchiveError("压缩包里没有可附加的文本文件。")
        note = f"已解包 · {len(self.sections)} 个文本文件"
        text = "以下是本地解包得到的文件内容，文件名是包内路径。"
        if self.skipped:
            note += f" · 跳过 {len(self.skipped)} 个二进制文件"
            text += "\n未附加的二进制文件：" + "、".join(self.skipped)
        text += "".join(self.sections)
        if len(text.encode("utf-8")) > MAX_TEXT_BYTES:
            raise ArchiveError(f"解包后的文本超过 {MAX_TEXT_BYTES // MIB} MB，请拆成几个较小的压缩包。")
        return text_attachment(name, text, note=note, path=where)


def unpack_text(name: str, where: str, kind: str, data: bytes) -> Attachment | Rejection:
    """Only inspect the first archive level; accept regular files, never links, and enforce a byte/member budget."""
    if len(data) > MAX_SOURCE:
        return Rejection(name, f"压缩包超过 {MAX_SOURCE // MIB} MB。", where)
    bundle = TextBundle()
    try:
        if kind == "zip":
            with zipfile.ZipFile(io.BytesIO(data)) as archive:
                for entry in archive.infolist():
                    directory = entry.is_dir()
                    mode = stat.S_IFMT(entry.external_attr >> 16)
                    if mode not in (0, stat.S_IFDIR if directory else stat.S_IFREG):
                        raise ArchiveError("压缩包含链接或特殊文件，未添加附件。")
                    path = bundle.check(entry.orig_filename, entry.file_size, directory=directory)
                    if directory:
                        continue
                    if entry.flag_bits & 1:
                        raise ArchiveError("暂不支持带密码的压缩包。")
                    if entry.compress_type not in (zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED):
                        raise ArchiveError("暂不支持此 ZIP 压缩算法。")
                    with archive.open(entry) as stream:
                        bundle.read(path, stream, entry.file_size)
        else:
            if kind == "tgz":
                with gzip.GzipFile(fileobj=io.BytesIO(data)) as stream:
                    data = stream.read(MAX_EXPANDED + 1)
                if len(data) > MAX_EXPANDED:
                    raise ArchiveError(f"压缩包展开后超过 {MAX_EXPANDED // MIB} MB。")
            with tarfile.open(fileobj=io.BytesIO(data), mode="r:") as archive:
                for entry in archive:
                    if (not entry.isdir() and not entry.isreg()) or entry.issparse():
                        raise ArchiveError("压缩包含链接或特殊文件，未添加附件。")
                    path = bundle.check(entry.name, entry.size, directory=entry.isdir())
                    if entry.isfile():
                        with archive.extractfile(entry) as stream:
                            bundle.read(path, stream, entry.size)
        return bundle.attachment(name, where)
    except ArchiveError as error:
        return Rejection(name, str(error), where)
    except (zipfile.BadZipFile, tarfile.TarError, zlib.error, OSError, EOFError, UnicodeError, NotImplementedError, ValueError, OverflowError):
        return Rejection(name, "压缩包损坏或格式不受支持，未添加附件。", where)
