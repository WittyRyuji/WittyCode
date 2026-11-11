"""Attachments of a prompt: their shape, the limits, how they reach the SDK and how saved ones come back.

No Qt here. The UI turns what the user pasted, dropped or picked into ``Attachment`` values
(``desktop.ui.attachments`` decodes and shrinks pictures and reads files); this module defines
what an attachment is, which limits apply, how it becomes an SDK ``Image`` / ``Document`` and how the
image and document blocks of saved messages are read back for the transcript.

Three kinds exist, matching what Droid accepts:

- ``image``: PNG / JPEG / GIF / WebP bytes (the SDK checks the signature);
- ``pdf``: the PDF bytes, plus the text extracted from it so that models without native PDF support
  still get the content (Droid falls back to ``parsedData`` for those);
- ``text``: the decoded text of a source or text file, sent with ``Document.from_text``.
"""

from __future__ import annotations

import base64
import binascii
import codecs
import locale
import struct
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, replace
from pathlib import PurePath
from typing import Any

from droid_sdk import (
    MAX_ATTACHMENT_BYTES,
    MAX_PDF_ATTACHMENT_BYTES,
    Document,
    Image,
    PdfDocumentSource,
)

from desktop.core.context_envelope import strip as strip_context

MIB = 1024 * 1024
SOFT_ATTACHMENTS = 10
SOFT_TOTAL_BYTES = 20 * MIB
"""What one message usually holds. More is the user's call: they are told by how much and confirm (``Overage``); nothing
is refused or dropped for it. Our own figures, not a limit anyone upstream was found to enforce."""
HARD_ATTACHMENTS = 50
HARD_TOTAL_BYTES = 200 * MIB
"""Not asked about: past this a drop or a paste is a slip (a folder dragged in by mistake) and every file would sit in
memory, be base64-encoded by the page and sent through the bridge. Five times what one message usually holds."""
MAX_IMAGE_BYTES = MAX_ATTACHMENT_BYTES
"""The SDK refuses pictures (and text) above this."""
MAX_PDF_BYTES = MAX_PDF_ATTACHMENT_BYTES
MAX_TEXT_BYTES = MIB
"""Much lower than the SDK's 5 MiB on purpose: 1 MiB of text is already ~250k tokens, more than most context windows."""
MAX_SOURCE_BYTES = 64 * MIB
"""Largest file that is even looked at (a picture can be shrunk, anything else must be far smaller)."""
MAX_PARSED_PDF_CHARS = 400_000
DEFAULT_PROMPT = "请查看附件。"
"""What is sent when only attachments were given: Droid needs a non-empty prompt."""

IMAGE = "image"
TEXT = "text"
PDF = "pdf"

TEXT_SUFFIXES = frozenset(
    """
    .txt .text .md .markdown .rst .adoc .org .log .csv .tsv .json .jsonl .ndjson .json5 .jsonc .yaml .yml .toml .ini .cfg
    .conf .config .env .properties .xml .xsd .xsl .xslt .html .htm .xhtml .svg .css .scss .sass .less .js .mjs .cjs .jsx
    .ts .tsx .py .pyi .pyw .ipynb .java .kt .kts .scala .groovy .gradle .c .h .cc .cpp .cxx .hpp .hh .hxx .cs .fs .go
    .rs .rb .php .pl .pm .lua .r .swift .m .mm .sql .sh .bash .zsh .fish .bat .cmd .ps1 .psm1 .psd1 .vb .vbs .dart .ex
    .exs .erl .hs .clj .cljs .lisp .el .vue .svelte .astro .tex .bib .diff .patch .gitignore .gitattributes .editorconfig
    .dockerfile .makefile .mk .cmake .proto .graphql .gql .tf .tfvars .lock .srt .vtt .sln .csproj .vcxproj .props
    .targets .manifest .reg .inf .plist .tmpl .j2 .jinja .cshtml .razor .asm .s .v .sv .vhd .nix .zig .nim .jl .ml .mli
    """.split()
)
TEXT_NAMES = frozenset(
    "makefile dockerfile license licence readme authors changelog notice copying procfile gemfile rakefile vagrantfile "
    "cmakelists.txt .gitignore .gitattributes .editorconfig .env .npmrc .prettierrc .eslintrc .babelrc".split()
)

_NATIVE_IMAGES = {
    b"\x89PNG\r\n\x1a\n": "image/png",
    b"\xff\xd8\xff": "image/jpeg",
    b"GIF87a": "image/gif",
    b"GIF89a": "image/gif",
}
_CONTROL_CHARACTERS = frozenset(range(32)) - {8, 9, 10, 12, 13, 27}
_SAMPLE = 8192


@dataclass(frozen=True, slots=True, eq=False, weakref_slot=True)
class Attachment:
    """By identity (``eq=False``), and weakly referable: ``AppCore`` keeps a thumbnail per attachment *object* and lets
    it go with the attachment (ids are only unique within one conversation's transcript)."""

    kind: str
    name: str
    data: bytes = b""
    """Pictures: the encoded picture that is sent. PDFs: the file. Text files keep ``text`` instead."""
    media_type: str = ""
    size: int | None = None
    """Bytes that are sent (``None``: unknown, e.g. a PDF of a saved session, which Droid keeps without its content)."""
    text: str = ""
    parsed_text: str | None = None
    note: str = ""
    """Short remark for the user, e.g. that a picture was compressed."""
    path: str = ""
    id: str = ""
    """Set by the transcript so the page can ask for a preview."""

    @property
    def badge(self) -> str:
        if self.kind == PDF:
            return "PDF"
        if self.kind == IMAGE:
            return "IMG"
        return (PurePath(self.name).suffix.lstrip(".") or "TXT").upper()[:4]

    @property
    def meta(self) -> str:
        """The line under the name: size and note."""
        return " · ".join(part for part in (format_size(self.size), self.note) if part)

    def slim(self) -> Attachment:
        """What the transcript keeps, so that an engine change can offer to hand it over (``core/handoff``): pictures stay
        (preview, thumbnail, and the picture itself); text files keep their text; a PDF keeps the text taken out of it
        and loses its bytes, unless there is no text (a scan): then the file is all there is to hand over.
        The page never gets any of it (``viewmodel.attachment_json`` sends the chip only)."""
        if self.kind == IMAGE:
            return self
        if self.kind == PDF and not (self.parsed_text or "").strip():
            return self
        return replace(self, data=b"")


@dataclass(frozen=True, slots=True)
class Rejection:
    name: str
    reason: str
    path: str = ""
    """The file's location; when set the user can have it inserted into the prompt instead."""


@dataclass(frozen=True, slots=True)
class Thumbnail:
    """A picture shrunk for the transcript page: a ``data:`` URI and the size of the shrunken picture in pixels."""

    uri: str
    width: int
    height: int


def format_size(size: int | None) -> str:
    if size is None:
        return ""
    if size < 1024:
        return f"{size} B"
    if size < MIB:
        return f"{size / 1024:.1f} KB"
    return f"{size / MIB:.1f} MB"


# --------------------------------------------------------------------------- building

def image_attachment(name: str, data: bytes, media_type: str, *, note: str = "", path: str = "") -> Attachment:
    return Attachment(kind=IMAGE, name=name, data=data, media_type=media_type, size=len(data), note=note, path=path)


def text_attachment(name: str, text: str, *, note: str = "", path: str = "") -> Attachment:
    return Attachment(
        kind=TEXT, name=name, text=text, media_type="text/plain", size=len(text.encode("utf-8", "replace")), note=note, path=path
    )


def pdf_attachment(
    name: str, data: bytes, *, parsed_text: str | None = None, note: str = "", path: str = ""
) -> Attachment:
    return Attachment(
        kind=PDF,
        name=name,
        data=data,
        media_type="application/pdf",
        size=len(data),
        parsed_text=parsed_text,
        note=note,
        path=path,
    )


def sniff_image(data: bytes) -> str | None:
    """Media type of the four picture formats Droid takes, by signature."""
    for magic, media_type in _NATIVE_IMAGES.items():
        if data.startswith(magic):
            return media_type
    if len(data) >= 12 and data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    return None


def is_pdf(data: bytes) -> bool:
    return data.startswith(b"%PDF-")


def image_size(data: bytes) -> tuple[int, int] | None:
    """Pixel size of an encoded picture, read from its header alone (``None``: not recognized or damaged)."""
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        if len(data) >= 24 and data[12:16] == b"IHDR":
            return struct.unpack(">I", data[16:20])[0], struct.unpack(">I", data[20:24])[0]
        return None
    if data[:6] in (b"GIF87a", b"GIF89a"):
        if len(data) >= 10:
            return struct.unpack("<H", data[6:8])[0], struct.unpack("<H", data[8:10])[0]
        return None
    if data.startswith(b"\xff\xd8"):
        return _jpeg_size(data)
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return _webp_size(data)
    return None


def _jpeg_size(data: bytes) -> tuple[int, int] | None:
    at = 2  # after the FFD8 opening marker
    while at + 4 <= len(data):
        if data[at] != 0xFF:
            return None
        marker = data[at + 1]
        if marker == 0xFF:  # fill bytes precede the real marker
            at += 1
            continue
        if marker in (0x01, 0xD8, 0xD9) or 0xD0 <= marker <= 0xD7:  # markers without a payload
            at += 2
            continue
        length = struct.unpack(">H", data[at + 2 : at + 4])[0]
        if length < 2 or at + 2 + length > len(data):
            return None
        if 0xC0 <= marker <= 0xCF and marker not in (0xC4, 0xC8, 0xCC):  # a start-of-frame segment
            if length >= 7:
                return struct.unpack(">H", data[at + 7 : at + 9])[0], struct.unpack(">H", data[at + 5 : at + 7])[0]
            return None
        at += 2 + length
    return None


def _webp_size(data: bytes) -> tuple[int, int] | None:
    if len(data) < 30:
        return None
    chunk, payload = data[12:16], data[20:]
    if chunk == b"VP8X" and len(payload) >= 10:  # extended: canvas size minus one, 24-bit each
        return int.from_bytes(payload[4:7], "little") + 1, int.from_bytes(payload[7:10], "little") + 1
    if chunk == b"VP8L" and len(payload) >= 5 and payload[0] == 0x2F:  # lossless: 14-bit sizes packed after the 0x2F byte
        bits = int.from_bytes(payload[1:5], "little")
        return (bits & 0x3FFF) + 1, ((bits >> 14) & 0x3FFF) + 1
    if chunk == b"VP8 " and len(payload) >= 10 and payload[3:6] == b"\x9d\x01\x2a":  # lossy: after the start code
        return struct.unpack("<H", payload[6:8])[0] & 0x3FFF, struct.unpack("<H", payload[8:10])[0] & 0x3FFF
    return None


def is_text_name(name: str) -> bool:
    lowered = name.lower()
    return lowered in TEXT_NAMES or PurePath(lowered).suffix in TEXT_SUFFIXES or lowered in TEXT_SUFFIXES


def _decode_with(data: bytes, encoding: str, partial: bool) -> str:
    if not partial:
        return data.decode(encoding)
    return codecs.getincrementaldecoder(encoding)().decode(data, final=False)


def _legacy_decode(data: bytes, partial: bool) -> tuple[str, str]:
    """The file is a text file but not UTF-8: try the machine's ANSI code page, then GB18030, then give up gracefully."""
    for encoding in dict.fromkeys((locale.getpreferredencoding(False), "gb18030")):
        try:
            return _decode_with(data, encoding, partial), codecs.lookup(encoding).name.upper()
        except (UnicodeDecodeError, LookupError):
            continue
    return data.decode("utf-8", errors="replace"), "UTF-8"


def decode_text(data: bytes, *, trusted: bool = False, partial: bool = False) -> tuple[str, str] | None:
    """Text and the name of the encoding it was read with, or ``None`` when ``data`` is not text.

    UTF-8 (with or without BOM) and UTF-16 with BOM are always accepted. ``trusted`` (the file name says
    it is a text or source file) also takes the machine's legacy code page (GBK on Chinese Windows) and
    GB18030, and as a last resort UTF-8 with replacement characters; untrusted data must decode cleanly.
    ``partial`` means ``data`` is only the beginning of a file, so a character cut off at the end is fine.
    """
    if data.startswith((codecs.BOM_UTF16_LE, codecs.BOM_UTF16_BE)):
        try:
            text, encoding = _decode_with(data, "utf-16", partial), "UTF-16"
        except UnicodeDecodeError:
            return None
    elif b"\x00" in data[:_SAMPLE]:
        return None
    else:
        try:
            text, encoding = _decode_with(data, "utf-8-sig", partial), "UTF-8"
        except UnicodeDecodeError:
            if not trusted:
                return None
            text, encoding = _legacy_decode(data, partial)
    sample = text[:_SAMPLE]
    if sample and sum(1 for char in sample if ord(char) in _CONTROL_CHARACTERS) / len(sample) > 0.02:
        return None
    return text, encoding


def unique_name(name: str, taken: Iterable[str]) -> str:
    """``name`` or, when it is in use, ``name (2)``, ``name (3)`` ... (the extension stays last)."""
    used = set(taken)
    if name not in used:
        return name
    path = PurePath(name)
    stem, suffix = (path.stem, path.suffix) if path.suffix else (name, "")
    number = 2
    while f"{stem} ({number}){suffix}" in used:
        number += 1
    return f"{stem} ({number}){suffix}"


# --------------------------------------------------------------------------- limits

def admit(existing: Sequence[Attachment], candidates: Iterable[Attachment]) -> tuple[list[Attachment], list[Rejection]]:
    """Split ``candidates`` into those that fit next to ``existing`` under the safety ceiling (``HARD_*``) and the rest.
    Going past what a message usually holds (``SOFT_*``) is no reason to refuse: see ``overage``."""
    accepted: list[Attachment] = []
    rejected: list[Rejection] = []
    count = len(existing)
    total = sum(item.size or 0 for item in existing)
    for item in candidates:
        size = item.size or 0
        if count >= HARD_ATTACHMENTS:
            rejected.append(Rejection(item.name, f"一次最多附加 {HARD_ATTACHMENTS} 个文件（再多多半是误操作）。", item.path))
        elif total + size > HARD_TOTAL_BYTES:
            rejected.append(
                Rejection(item.name, f"附件总大小不能超过 {HARD_TOTAL_BYTES // MIB} MB（已有 {format_size(total)}）。", item.path)
            )
        else:
            accepted.append(item)
            count += 1
            total += size
    return accepted, rejected


@dataclass(frozen=True, slots=True)
class Overage:
    """What a message holds beyond what one usually does (``SOFT_*``): the user decides whether to send it anyway."""

    count: int
    """How many attachments the message would hold."""
    total: int
    """Their size together, in bytes."""

    @property
    def files(self) -> int:
        return max(0, self.count - SOFT_ATTACHMENTS)

    @property
    def bytes(self) -> int:
        return max(0, self.total - SOFT_TOTAL_BYTES)

    @property
    def over(self) -> bool:
        return self.files > 0 or self.bytes > 0

    @property
    def beyond_ceiling(self) -> bool:
        """Past the safety ceiling (``HARD_*``): not a question, it is refused."""
        return self.count > HARD_ATTACHMENTS or self.total > HARD_TOTAL_BYTES

    def describe(self) -> str:
        """By how much, in the words the page uses as well (``frontend/src/lib/carry.ts``): ``""`` when not over."""
        if not self.over:
            return ""
        parts = []
        if self.files:
            parts.append(f"多 {self.files} 个文件")
        if self.bytes:
            parts.append(f"多 {format_size(self.bytes)}")
        return (
            f"比一条消息通常带的量{'、'.join(parts)}"
            f"（这条消息有 {self.count} 个附件、{format_size(self.total)}；通常最多 {SOFT_ATTACHMENTS} 个、{SOFT_TOTAL_BYTES // MIB} MB）"
        )


def overage(attachments: Iterable[Attachment]) -> Overage:
    """What a message with these attachments holds against what one usually does."""
    items = list(attachments)
    return Overage(len(items), sum(item.size or len(item.data) for item in items))


def too_many(o: Overage) -> str:
    """The sentence for a message past the safety ceiling (``HARD_*``)."""
    return (
        f"附件太多了：一条消息最多 {HARD_ATTACHMENTS} 个、{HARD_TOTAL_BYTES // MIB} MB，"
        f"现在有 {o.count} 个、{format_size(o.total)}：再多多半是误操作，请分几条消息发。"
    )


def describe_rejections(rejections: Sequence[Rejection], *, limit: int = 6) -> str:
    lines = [f"• {item.name}：{item.reason}" for item in rejections[:limit]]
    if len(rejections) > limit:
        lines.append(f"• 还有 {len(rejections) - limit} 个文件……")
    return "\n".join(lines)


def insertable_path(path: str) -> str:
    """A path as it is typed into the prompt (quoted when it contains spaces)."""
    return f'"{path}"' if any(char.isspace() for char in path) else path


# --------------------------------------------------------------------------- SDK

def _pdf_document(item: Attachment) -> Document:
    source = PdfDocumentSource(
        data=base64.b64encode(item.data).decode("ascii"),
        parsed_data=item.parsed_text,
        name=item.name,
        path=item.path or None,
    )
    return Document(source)


def to_sdk(attachments: Iterable[Attachment]) -> tuple[list[Image], list[Document]]:
    """SDK values for ``Session.stream(images=..., files=...)``; raises ``InvalidAttachmentError`` for bad data."""
    images: list[Image] = []
    documents: list[Document] = []
    for item in attachments:
        if item.kind == IMAGE:
            images.append(Image.from_bytes(item.data, media_type=item.media_type))  # type: ignore[arg-type]
        elif item.kind == PDF:
            documents.append(_pdf_document(item))
        elif item.kind == TEXT:
            documents.append(Document.from_text(item.text, name=item.name, mime="text/plain"))
    return images, documents


def stream_options(attachments: Iterable[Attachment]) -> dict[str, list[Any]]:
    """Keyword arguments for ``Session.stream``: only the ones in use, so plain prompts call it as before."""
    images, documents = to_sdk(attachments)
    options: dict[str, list[Any]] = {}
    if images:
        options["images"] = images
    if documents:
        options["files"] = documents
    return options


def steer_options(attachments: Iterable[Attachment]) -> dict[str, list[dict[str, Any]]]:
    """Keyword arguments for ``DroidClient.add_user_message``, which takes the wire form (``Session.stream`` converts
    ``Image`` / ``Document`` into it before it calls that; a message to a running turn has to bypass ``stream``)."""
    from droid_sdk._high_level._convert import document_to_wire  # the SDK's own conversion, not worth copying

    images, documents = to_sdk(attachments)
    options: dict[str, list[dict[str, Any]]] = {}
    if images:
        options["images"] = [
            {"type": "base64", "data": image.source.data, "mediaType": image.source.media_type} for image in images
        ]
    if documents:
        options["files"] = [document_to_wire(document) for document in documents]
    return options


# --------------------------------------------------------------------------- saved messages

def _value(value: Any) -> Any:
    return getattr(value, "value", value)


def _block_kind(block: Any) -> str:
    kind = str(_value(getattr(block, "type", "")))
    if kind:
        return kind
    # Live SDK blocks are plain dataclasses without a ``type``.
    return {"TextBlock": "text", "ImageBlock": "image", "DocumentBlock": "document"}.get(type(block).__name__, "")


def is_system_reminder(text: str) -> bool:
    """A text block droid added to a message by itself, for example where the temporary copy of a picture was put."""
    stripped = text.strip()
    return stripped.startswith("<system-reminder>") and stripped.endswith("</system-reminder>")


def typed_text(content: Iterable[Any], fallback: str = "") -> str:
    """What the user typed in a message: its text blocks without droid's system reminders.

    Droid saves a message with pictures as the picture, a ``<system-reminder>`` block with the paths of the
    pictures' temporary copies, then the typed text. ``fallback`` is returned when there are no text blocks at all.
    """
    blocks = [block for block in content if _block_kind(block) == "text"]
    if not blocks:
        return fallback
    texts = (str(getattr(block, "text", "") or "") for block in blocks)
    return strip_context("".join(text for text in texts if not is_system_reminder(text)))


def _decode(data: Any) -> bytes:
    if not isinstance(data, str) or not data:
        return b""
    try:
        return base64.b64decode(data)
    except (binascii.Error, ValueError):
        return b""


def _image_from_block(block: Any, number: int) -> Attachment:
    source = getattr(block, "source", None)
    media_type = str(_value(getattr(source, "media_type", "")) or "")
    data = _decode(getattr(source, "data", None))
    name = f"图片 {number}"
    if not data:
        return Attachment(kind=IMAGE, name=name, media_type=media_type)
    return image_attachment(name, data, sniff_image(data) or media_type)


def _document_from_block(block: Any) -> Attachment | None:
    source = getattr(block, "source", None)
    if source is None:
        return None
    name = getattr(source, "name", None) or "文档"
    if isinstance(source, PdfDocumentSource) or _value(getattr(source, "media_type", "")) == "application/pdf":
        data = _decode(getattr(source, "data", None))
        return Attachment(
            kind=PDF,
            name=name,
            data=data,
            media_type="application/pdf",
            size=len(data) or None,
            parsed_text=getattr(source, "parsed_data", None),
            path=getattr(source, "path", None) or "",
        )
    text = getattr(source, "data", "")
    return text_attachment(name, text if isinstance(text, str) else "")


def attachments_from_content(content: Iterable[Any]) -> list[Attachment]:
    """The pictures and documents among the content blocks of a user message (saved wire models or live SDK blocks).

    Droid keeps a saved PDF without its bytes (``data`` is empty): such an attachment has no size and no preview.
    """
    found: list[Attachment] = []
    pictures = 0
    for block in content:
        kind = _block_kind(block)
        if kind == "image":
            pictures += 1
            found.append(_image_from_block(block, pictures))
        elif kind == "document":
            item = _document_from_block(block)
            if item is not None:
                found.append(item)
    return found
