"""Attachments without a window: what each kind of file becomes and the limits."""

from __future__ import annotations

import base64
import os
from pathlib import Path

import pytest
from PySide6.QtCore import QMimeData, QUrl
from PySide6.QtGui import QColor, QImage

from desktop.core import attachments as core
from desktop.core.attachments import (
    DEFAULT_PROMPT,
    HARD_ATTACHMENTS,
    HARD_TOTAL_BYTES,
    IMAGE,
    MIB,
    PDF,
    SOFT_ATTACHMENTS,
    SOFT_TOTAL_BYTES,
    TEXT,
    Attachment,
    Overage,
    Rejection,
    admit,
    decode_text,
    describe_rejections,
    format_size,
    image_attachment,
    image_size,
    insertable_path,
    overage,
    pdf_attachment,
    too_many,
    sniff_image,
    text_attachment,
    unique_name,
)
from desktop.devtools.samples import make_pdf, sample_picture
from desktop.ui import attachments as loading
from desktop.ui import imaging
from tests.support import tiny_png

pytestmark = pytest.mark.usefixtures("qapp")

PNG_MAGIC = b"\x89PNG\r\n\x1a\n"
ONE_PIXEL_GIF = (
    b"GIF89a\x01\x00\x01\x00\x80\x00\x00\xff\xff\xff\x00\x00\x00!\xf9\x04\x01\x00\x00\x00\x00,"
    b"\x00\x00\x00\x00\x01\x00\x01\x00\x00\x02\x02D\x01\x00;"
)
SECRET = "Invoice 4417: the secret word is PELICAN-ORCHID."


def picture_file(tmp_path: Path, name: str, fmt: str, width: int = 96, height: int = 64) -> Path:
    path = tmp_path / name
    assert sample_picture(width=width, height=height).save(str(path), fmt)
    return path


def noise(width: int, height: int) -> QImage:
    """Pictures of random pixels do not compress: the way to a file above a size limit without a huge picture."""
    return QImage(os.urandom(width * height * 4), width, height, QImage.Format.Format_RGB32).copy()


def limit_pictures(monkeypatch, limit: int) -> None:
    """A small size limit for pictures, so that tests need no multi-megabyte files."""
    monkeypatch.setattr(loading, "MAX_IMAGE_BYTES", limit)
    monkeypatch.setattr(imaging, "MAX_IMAGE_BYTES", limit)


def loaded(path: Path) -> Attachment:
    outcome = loading.load_path(path)
    assert isinstance(outcome, Attachment), outcome
    return outcome


def refused(path: Path) -> Rejection:
    outcome = loading.load_path(path)
    assert isinstance(outcome, Rejection), outcome
    return outcome


# --------------------------------------------------------------------------- what each file becomes


@pytest.mark.parametrize(
    ("name", "fmt", "media_type"),
    [("a.png", "PNG", "image/png"), ("b.jpg", "JPEG", "image/jpeg"), ("c.webp", "WEBP", "image/webp")],
)
def test_pictures_in_the_formats_models_accept_are_sent_as_they_are(tmp_path, name, fmt, media_type):
    path = picture_file(tmp_path, name, fmt)
    item = loaded(path)
    assert (item.kind, item.media_type, item.name, item.note) == (IMAGE, media_type, name, "")
    assert item.data == path.read_bytes() and item.size == len(item.data)
    assert item.path == str(path.absolute())
    assert core.sniff_image(item.data) == item.media_type  # a picture the models take, as its signature says


def test_a_gif_is_sent_as_it_is(tmp_path):
    path = tmp_path / "dot.gif"
    path.write_bytes(ONE_PIXEL_GIF)
    item = loaded(path)
    assert (item.kind, item.media_type, item.data) == (IMAGE, "image/gif", ONE_PIXEL_GIF)


def test_the_kind_of_picture_comes_from_the_bytes_not_from_the_name(tmp_path):
    png = picture_file(tmp_path, "real.png", "PNG")
    disguised = tmp_path / "photo.txt"
    disguised.write_bytes(png.read_bytes())
    jpeg = picture_file(tmp_path, "real.jpg", "JPEG")
    wrong = tmp_path / "wrong.png"
    wrong.write_bytes(jpeg.read_bytes())
    assert loaded(disguised).media_type == "image/png" and loaded(disguised).kind == IMAGE
    assert loaded(wrong).media_type == "image/jpeg"
    assert sniff_image(b"RIFF\x00\x00\x00\x00WEBPVP8 ") == "image/webp" and sniff_image(b"plain text") is None


def test_image_size_reads_the_header_of_every_format_models_accept(tmp_path):
    assert image_size(tiny_png(40, 30)) == (40, 30)
    assert image_size(ONE_PIXEL_GIF) == (1, 1)
    for name, fmt in (("a.jpg", "JPEG"), ("b.webp", "WEBP"), ("c.png", "PNG")):
        assert image_size(picture_file(tmp_path, name, fmt, 96, 64).read_bytes()) == (96, 64), name
    assert image_size(b"plain text") is None and image_size(b"") is None
    assert image_size(PNG_MAGIC + b"\x00" * 4) is None, "a header cut short is not a size"


@pytest.mark.parametrize("name", ["shot.bmp", "scan.tif"])
def test_other_pictures_are_converted_to_png(tmp_path, name):
    path = picture_file(tmp_path, name, "BMP" if name.endswith("bmp") else "TIFF")
    item = loaded(path)
    assert (item.kind, item.media_type, item.note) == (IMAGE, "image/png", "已转为 PNG")
    assert item.data.startswith(PNG_MAGIC) and item.name == name
    assert core.sniff_image(item.data) == item.media_type  # a picture the models take, as its signature says


def test_a_pdf_goes_with_the_text_it_contains(tmp_path):
    path = tmp_path / "invoice.pdf"
    path.write_bytes(make_pdf([SECRET, "Second line (with parens) and total 1234.50"]))
    item = loaded(path)
    assert (item.kind, item.media_type, item.size) == (PDF, "application/pdf", path.stat().st_size)
    assert item.data == path.read_bytes()
    assert SECRET in (item.parsed_text or "") and "Second line (with parens)" in (item.parsed_text or "")
    assert item.badge == "PDF"


def test_a_pdf_without_a_text_layer_is_still_attached(tmp_path):
    path = tmp_path / "scan.pdf"
    path.write_bytes(make_pdf([]))
    item = loaded(path)
    assert item.kind == PDF and item.parsed_text is None


@pytest.mark.parametrize("name", ["tool.py", "notes.md", "data.csv", "Makefile", ".gitignore", "page.svg"])
def test_text_and_source_files_are_read_as_text(tmp_path, name):
    path = tmp_path / name
    path.write_bytes("第一行\nprint('héllo')\n".encode("utf-8"))
    item = loaded(path)
    assert (item.kind, item.media_type, item.text) == (TEXT, "text/plain", "第一行\nprint('héllo')\n")
    assert item.size == len(item.text.encode()) and item.note == ""


def test_a_file_without_a_known_extension_is_text_when_it_is_clean_utf8(tmp_path):
    path = tmp_path / "mystery.xyz"
    path.write_text("just words, 中文也可以", encoding="utf-8")
    assert loaded(path).kind == TEXT
    bom = tmp_path / "bom.dat"
    bom.write_bytes(b"\xef\xbb\xbfwith bom")
    assert loaded(bom).text == "with bom"
    wide = tmp_path / "wide.dat"
    wide.write_bytes("宽字符 text".encode("utf-16"))
    assert loaded(wide).text == "宽字符 text"


def test_a_legacy_encoded_text_file_is_read_with_a_note(tmp_path, monkeypatch):
    monkeypatch.setattr(core.locale, "getpreferredencoding", lambda _do_setlocale=False: "cp936")
    path = tmp_path / "notes.txt"
    path.write_bytes("你好，世界".encode("gbk"))
    item = loaded(path)
    assert item.kind == TEXT and item.text == "你好，世界" and "GBK" in item.note.upper()
    # The same bytes under an unknown extension are not trusted to be text.
    assert isinstance(loading.load_path(_copy(path, tmp_path / "notes.zzz")), Rejection)


def _copy(source: Path, target: Path) -> Path:
    target.write_bytes(source.read_bytes())
    return target


@pytest.mark.parametrize(
    ("name", "content"),
    [
        ("archive.zip", b"PK\x03\x04" + bytes(range(256)) * 4),
        ("tool.exe", b"MZ\x90\x00\x03\x00\x00\x00" + b"\x00" * 200),
        ("blob.bin", os.urandom(512) + b"\x00\x01\x02"),
        ("lying.txt", b"\x00\x01\x02\x03" * 100),
    ],
)
def test_other_binary_files_are_refused_with_a_reason_and_their_path(tmp_path, name, content):
    path = tmp_path / name
    path.write_bytes(content)
    rejection = refused(path)
    assert rejection.name == name and rejection.path == str(path.absolute())
    assert rejection.reason == loading.BINARY and "二进制" in rejection.reason


def test_folders_missing_and_empty_files_are_refused(tmp_path):
    folder = tmp_path / "sub"
    folder.mkdir()
    empty = tmp_path / "empty.txt"
    empty.write_bytes(b"")
    assert "文件夹" in refused(folder).reason and refused(folder).path == str(folder.absolute())
    assert "空" in refused(empty).reason
    assert "找不到" in refused(tmp_path / "gone.txt").reason


def test_oversize_text_is_refused_but_its_path_is_kept(tmp_path):
    path = tmp_path / "big.log"
    path.write_text("x" * (core.MAX_TEXT_BYTES + 10), encoding="utf-8")
    rejection = refused(path)
    assert "超过 1 MB" in rejection.reason and rejection.path == str(path.absolute())
    assert insertable_path(rejection.path) == rejection.path  # no spaces: typed as it is


def test_an_oversize_pdf_is_refused_but_its_path_is_kept(tmp_path):
    path = tmp_path / "big.pdf"
    path.write_bytes(make_pdf(["x"]) + b"\x00" * (core.MAX_PDF_BYTES + 1))
    rejection = refused(path)
    assert "PDF" in rejection.reason and "超过 3 MB" in rejection.reason and rejection.path == str(path.absolute())


def test_a_corrupt_picture_is_refused(tmp_path):
    path = tmp_path / "broken.png"
    path.write_bytes(PNG_MAGIC + b"this is not a png body")
    assert refused(path).reason == loading.BROKEN_IMAGE


# --------------------------------------------------------------------------- shrinking pictures


def test_a_picture_with_a_side_beyond_the_model_limit_is_scaled_down(tmp_path):
    path = tmp_path / "banner.png"
    wide = QImage(9000, 300, QImage.Format.Format_RGB32)
    wide.fill(QColor("#3a7bd5"))
    assert wide.save(str(path), "PNG")
    assert path.stat().st_size < core.MAX_IMAGE_BYTES, "small in bytes, too long in pixels"
    item = loaded(path)
    size = imaging.probe_size(item.data)
    assert max(size.width(), size.height()) <= imaging.MAX_SIDE
    assert item.note == "已压缩" and item.media_type == "image/png"
    assert core.sniff_image(item.data) == item.media_type  # a picture the models take, as its signature says


@pytest.mark.parametrize(
    ("limit", "keeps_its_size"),
    [(150_000, True), (60_000, False)],
    ids=["lower-jpeg-quality", "smaller-picture"],
)
def test_a_picture_above_the_size_limit_is_shrunk_until_it_fits(tmp_path, monkeypatch, limit, keeps_its_size):
    limit_pictures(monkeypatch, limit)
    path = tmp_path / "grainy.png"
    assert noise(600, 450).save(str(path), "PNG") and path.stat().st_size > limit
    item = loaded(path)
    assert item.size <= limit and len(item.data) == item.size
    assert item.note == "已压缩" and item.media_type == "image/jpeg"
    size = imaging.probe_size(item.data)
    assert (size.width() == 600) is keeps_its_size
    assert core.sniff_image(item.data) == item.media_type  # a picture the models take, as its signature says


def test_a_picture_that_cannot_be_made_small_enough_is_refused(tmp_path, monkeypatch):
    limit_pictures(monkeypatch, 100)
    path = tmp_path / "grainy.png"
    assert noise(200, 200).save(str(path), "PNG")
    rejection = refused(path)
    assert "图片太大" in rejection.reason and rejection.path == str(path.absolute())


def test_fit_image_prefers_lossless_and_only_then_jpeg():
    small = sample_picture(width=80, height=60)
    fitted = imaging.fit_image(small)
    assert (fitted.media_type, fitted.compressed) == ("image/png", False)
    grainy = noise(300, 300)
    squeezed = imaging.fit_image(grainy, limit=60_000)
    assert squeezed is not None and len(squeezed.data) <= 60_000 and squeezed.compressed
    assert imaging.fit_image(grainy, limit=60_000, lossless=False).media_type == "image/jpeg"
    assert imaging.fit_image(grainy, limit=10) is None


def test_a_clipboard_picture_becomes_a_named_png():
    item = loading.load_image(sample_picture(width=120, height=80))
    assert (item.kind, item.media_type, item.name, item.note) == (IMAGE, "image/png", "粘贴的图片.png", "")
    assert item.data.startswith(PNG_MAGIC) and item.path == ""
    assert isinstance(loading.load_image(QImage()), Rejection)


def test_a_clipboard_picture_that_is_too_large_is_compressed(monkeypatch):
    limit_pictures(monkeypatch, 150_000)
    item = loading.load_image(noise(500, 400))
    assert isinstance(item, Attachment), item
    assert item.size <= 150_000 and item.note == "已压缩" and item.name == "粘贴的图片.jpg"
    assert item.media_type == "image/jpeg"


# --------------------------------------------------------------------------- limits and names


def make(name: str, size: int) -> Attachment:
    return Attachment(kind=TEXT, name=name, text="x", size=size)


def test_more_than_a_message_usually_holds_is_not_refused():
    """Ten files and 20 MB are what one message usually holds: past that is the user's call (see ``overage``)."""
    existing = [make(f"f{number}.txt", 10) for number in range(SOFT_ATTACHMENTS + 5)]
    accepted, rejected = admit(existing, [make("more.txt", 10)])
    assert [item.name for item in accepted] == ["more.txt"] and rejected == []
    heavy = [make("a.txt", SOFT_TOTAL_BYTES + 5 * MIB)]
    accepted, rejected = admit(heavy, [make("b.txt", MIB)])
    assert [item.name for item in accepted] == ["b.txt"] and rejected == []


def test_only_the_safety_ceiling_refuses_files():
    existing = [make(f"f{number}.txt", 10) for number in range(HARD_ATTACHMENTS - 1)]
    accepted, rejected = admit(existing, [make("last.txt", 10), make("over.txt", 10)])
    assert [item.name for item in accepted] == ["last.txt"]
    assert [item.name for item in rejected] == ["over.txt"] and f"最多附加 {HARD_ATTACHMENTS}" in rejected[0].reason
    assert HARD_ATTACHMENTS == 5 * SOFT_ATTACHMENTS and HARD_TOTAL_BYTES == 10 * SOFT_TOTAL_BYTES


def test_the_total_size_has_a_safety_ceiling_too():
    existing = [make("a.txt", HARD_TOTAL_BYTES - 100)]
    accepted, rejected = admit(existing, [make("small.txt", 50), make("big.txt", 200)])
    assert [item.name for item in accepted] == ["small.txt"]
    assert "总大小不能超过 200 MB" in rejected[0].reason


def test_overage_says_by_how_much_a_message_is_over_what_it_usually_holds():
    usual = overage([make(f"f{n}.txt", MIB) for n in range(SOFT_ATTACHMENTS)])
    assert not usual.over and usual.files == 0 and usual.bytes == 0 and usual.describe() == ""
    held = overage([make(f"f{n}.txt", 2 * MIB) for n in range(12)])
    assert (held.count, held.total, held.files, held.bytes) == (12, 24 * MIB, 2, 4 * MIB) and held.over and not held.beyond_ceiling
    assert held.describe() == "比一条消息通常带的量多 2 个文件、多 4.0 MB（这条消息有 12 个附件、24.0 MB；通常最多 10 个、20 MB）"
    only_files = overage([make(f"f{n}.txt", 10) for n in range(11)])
    assert only_files.describe() == "比一条消息通常带的量多 1 个文件（这条消息有 11 个附件、110 B；通常最多 10 个、20 MB）"
    only_size = overage([make("big.txt", 21 * MIB)])
    assert only_size.describe() == "比一条消息通常带的量多 1.0 MB（这条消息有 1 个附件、21.0 MB；通常最多 10 个、20 MB）"


def test_a_picture_without_a_recorded_size_counts_by_its_bytes_and_past_the_ceiling_it_is_refused_in_words():
    picture = Attachment(kind=IMAGE, name="p.png", data=b"x" * 1000, media_type="image/png")
    assert overage([picture]).total == 1000
    many = Overage(HARD_ATTACHMENTS + 1, 10)
    assert many.beyond_ceiling and Overage(1, HARD_TOTAL_BYTES + 1).beyond_ceiling and not Overage(HARD_ATTACHMENTS, HARD_TOTAL_BYTES).beyond_ceiling
    assert f"最多 {HARD_ATTACHMENTS} 个、{HARD_TOTAL_BYTES // MIB} MB" in too_many(many) and f"现在有 {HARD_ATTACHMENTS + 1} 个" in too_many(many)


def test_load_paths_does_not_even_open_files_beyond_the_room(tmp_path):
    paths = []
    for number in range(5):
        path = tmp_path / f"f{number}.txt"
        path.write_text(f"file {number}")
        paths.append(path)
    result = loading.load_paths(paths, room=3)
    assert [item.name for item in result.attachments] == ["f0.txt", "f1.txt", "f2.txt"]
    assert [item.name for item in result.rejections] == ["f3.txt", "f4.txt"]
    assert all("最多附加" in item.reason and item.path for item in result.rejections)


def test_names_that_clash_get_a_number():
    assert unique_name("a.png", ["b.png"]) == "a.png"
    assert unique_name("a.png", ["a.png"]) == "a (2).png"
    assert unique_name("a.png", ["a.png", "a (2).png"]) == "a (3).png"
    assert unique_name("Makefile", ["Makefile"]) == "Makefile (2)"


def test_sizes_read_like_a_file_manager():
    assert [format_size(value) for value in (None, 0, 1023, 1024, 1536, MIB, int(2.5 * MIB))] == [
        "", "0 B", "1023 B", "1.0 KB", "1.5 KB", "1.0 MB", "2.5 MB",
    ]


def test_rejections_are_listed_one_per_line_and_truncated():
    items = [Rejection(f"f{number}.zip", "不行") for number in range(9)]
    text = describe_rejections(items, limit=3)
    assert text.splitlines()[:3] == ["• f0.zip：不行", "• f1.zip：不行", "• f2.zip：不行"]
    assert text.splitlines()[-1] == "• 还有 6 个文件……"


def test_a_path_with_spaces_is_quoted_when_typed_into_the_prompt():
    assert insertable_path(r"D:\My Files\a b.zip") == r'"D:\My Files\a b.zip"'
    assert insertable_path(r"D:\data\a.zip") == r"D:\data\a.zip"


def test_text_decoding_is_strict_unless_the_name_says_it_is_text():
    assert decode_text(b"\xff\xfe\x00\x01 binary \x00") is None  # a UTF-16 BOM over an odd number of bytes
    assert decode_text("héllo".encode("latin-1")) is None
    assert decode_text(b"abc\x00def") is None
    assert decode_text("héllo".encode("latin-1"), trusted=True) is not None
    assert decode_text("宽字符 text".encode("utf-16")) == ("宽字符 text", "UTF-16")
    cut = "多字节".encode("utf-8")[:-1]
    assert decode_text(cut) is None, "a character cut in half is not valid text for a whole file"
    assert decode_text(cut, partial=True) == ("多字", "UTF-8"), "but it is for the head of a bigger file"


# --------------------------------------------------------------------------- what a message gets


def test_a_message_with_only_attachments_gets_the_default_prompt():
    assert DEFAULT_PROMPT == "请查看附件。"


# --------------------------------------------------------------------------- clipboard and drag data


def mime_with(**parts) -> QMimeData:
    mime = QMimeData()
    if "urls" in parts:
        mime.setUrls([QUrl(url) for url in parts["urls"]])
    if "text" in parts:
        mime.setText(parts["text"])
    if "image" in parts:
        mime.setImageData(parts["image"])
    return mime


def test_pasted_or_dropped_data_attaches_files_and_pictures_but_not_text(tmp_path):
    path = tmp_path / "a.txt"
    path.write_text("hi")
    local = QUrl.fromLocalFile(str(path)).toString()
    picture = sample_picture(width=40, height=30)
    assert loading.wants_attachments(mime_with(urls=[local]))
    assert loading.wants_attachments(mime_with(image=picture))
    assert not loading.wants_attachments(mime_with(text="plain words"))
    assert not loading.wants_attachments(mime_with(urls=["https://example.com/page"])), "web links are text"
    assert not loading.wants_attachments(mime_with(image=picture, text="1\t2\t3")), "spreadsheet cells come with a picture"
    assert not loading.wants_attachments(QMimeData())


def test_load_mime_reads_files_or_the_picture(tmp_path):
    path = tmp_path / "a.txt"
    path.write_text("hi")
    result = loading.load_mime(mime_with(urls=[QUrl.fromLocalFile(str(path)).toString()]))
    assert [item.name for item in result.attachments] == ["a.txt"] and not result.rejections
    result = loading.load_mime(mime_with(image=sample_picture(width=40, height=30)))
    assert [item.name for item in result.attachments] == ["粘贴的图片.png"]


# --------------------------------------------------------------------------- thumbnails and PDF text


def test_a_thumbnail_is_a_small_data_uri_with_its_size():
    big = sample_picture(width=1600, height=900)
    buffer = imaging.encode(big, "PNG")
    thumb = imaging.thumbnail(buffer)
    assert thumb.uri.startswith("data:image/jpeg;base64,")
    assert (thumb.width, thumb.height) == (320, 180) and len(thumb.uri) < 60_000
    assert base64.b64decode(thumb.uri.split(",", 1)[1])[:3] == b"\xff\xd8\xff"


def test_a_thumbnail_keeps_transparency_by_staying_png():
    clear = QImage(100, 100, QImage.Format.Format_ARGB32)
    clear.fill(QColor(0, 0, 0, 0))
    thumb = imaging.thumbnail(imaging.encode(clear, "PNG"))
    assert thumb.uri.startswith("data:image/png;base64,")


def test_a_thumbnail_of_something_that_is_not_a_picture_is_none():
    assert imaging.thumbnail(b"not a picture") is None


def test_pdf_text_is_limited_and_unreadable_pdfs_give_none():
    long_pdf = make_pdf([f"line {number} " + "word " * 10 for number in range(40)])
    text = imaging.pdf_text(long_pdf, limit=200)
    assert text.endswith(imaging.TRUNCATED) and len(text) < 260
    assert imaging.pdf_text(b"%PDF-1.4 garbage") is None
    assert imaging.pdf_text(make_pdf([])) is None
