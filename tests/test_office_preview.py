"""Bounded Office preview inputs, including ZIP metadata that cannot be trusted by the browser."""
from __future__ import annotations

import base64
import struct
import zipfile
import zlib

import pytest

from desktop.core import files
from tests.test_app_files import conversation, run
from desktop.core.app_core import AppError


def write_package(path, parts=None):
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("[Content_Types].xml", '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types"/>')
        archive.writestr("_rels/.rels", (parts or {}).get("_rels/.rels", '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"/>'))
        for name, value in (parts or {}).items():
            if name != "_rels/.rels":
                archive.writestr(name, value)


@pytest.mark.parametrize(("suffix", "kind"), [
    ("DOCX", "word"), ("docm", "word"), ("dotx", "word"), ("XLSX", "sheet"), ("xlsm", "sheet"), ("xls", "sheet"),
    ("PPTX", "slides"), ("pptm", "slides"), ("ppsx", "slides"),
])
def test_office_suffix(tmp_path, suffix, kind):
    path = tmp_path / f"sample.{suffix}"
    path.touch()
    assert files.preview_kind(path) == kind


def test_office_cap_and_invalid_zip(tmp_path, monkeypatch):
    path = tmp_path / "sample.docx"
    path.write_bytes(b"plain text with the wrong extension")
    with pytest.raises(files.FileProblem, match="不是有效的 Office"):
        files.office_data_url(path, "word")
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("plain.txt", "zip, but not Office")
    with pytest.raises(files.FileProblem, match="不是有效的 Office"):
        files.office_data_url(path, "word")
    monkeypatch.setattr(files, "PREVIEW_OFFICE_BYTES", 10)
    with pytest.raises(files.FileProblem, match="太大"):
        files.preview_size(path, "word")
    with pytest.raises(files.FileProblem, match="太大"):
        files.office_data_url(path, "word")


@pytest.mark.parametrize(("limit", "value"), [("OFFICE_ZIP_ENTRIES", 3), ("OFFICE_ZIP_BYTES", 2048)])
def test_reject_declared_archive_bombs(tmp_path, monkeypatch, limit, value):
    path = tmp_path / "bomb.docm"
    write_package(path, {"word/document.xml": "x" * 4096, "word/a.xml": "x"})
    monkeypatch.setattr(files, limit, value)
    with pytest.raises(files.FileProblem, match="解压后太大或文件过多"):
        files.office_data_url(path, "word")


def test_reject_a_deflate_stream_that_exceeds_its_forged_metadata(tmp_path):
    path = tmp_path / "forged.docx"
    write_package(path, {"word/document.xml": "x" * 600_000})
    assert files.office_data_url(path, "word").startswith("data:application/vnd.openxmlformats-")
    with zipfile.ZipFile(path) as archive:
        local = archive.getinfo("word/document.xml").header_offset
    data = bytearray(path.read_bytes())
    central = data.rindex(b"PK\x01\x02")
    # Even the CRC is made to match the falsely advertised prefix, which a bounded ZipExtFile read may accept.
    length, crc = 16, zlib.crc32(b"x" * 16)
    struct.pack_into("<I", data, local + 14, crc)
    struct.pack_into("<I", data, local + 22, length)
    struct.pack_into("<I", data, central + 16, crc)
    struct.pack_into("<I", data, central + 24, length)
    path.write_bytes(data)
    with pytest.raises(files.FileProblem, match="不是有效的 Office"):
        files.office_data_url(path, "word")


def test_office_bridge_payloads_and_clear_errors(tmp_path):
    async def scenario():
        core, cid, platform = await conversation(tmp_path)
        project = tmp_path / "proj"
        try:
            for suffix, kind, mime in (("docx", "word", "wordprocessingml.document"), ("xlsx", "sheet", "spreadsheetml.sheet"), ("pptx", "slides", "presentationml.presentation")):
                path = project / f"note.{suffix}"
                write_package(path)
                request = {"id": cid, "path": path.name}
                shown = await core.call("files.read", request)
                assert (shown["kind"], shown["name"], shown["size"], shown["text"], shown["pages"]) == (kind, path.name, path.stat().st_size, "", [])
                url = (await core.call("files.page", {**request, "page": 0, "width": 0}))["url"]
                header, data = url.split(",", 1)
                assert header == f"data:application/vnd.openxmlformats-officedocument.{mime};base64"
                assert base64.b64decode(data) == path.read_bytes()
                with pytest.raises(AppError, match="页码不合法"):
                    await core.call("files.page", {**request, "page": 1})
            assert not platform.pdf_requests
            (project / "folder.docx").mkdir()
            (project / "old.DOC").write_bytes(b"legacy")
            (project / "old.ppt").write_bytes(b"legacy")
            (project / "bad.xlsx").write_bytes(b"bad")
            assert files.preview_kind(project / "folder.docx") is None
            for name, reason in [("folder.docx", "这是文件夹"), ("old.DOC", "另存为 .docx"), ("old.ppt", "另存为 .pptx")]:
                with pytest.raises(AppError, match=reason):
                    await core.call("files.read", {"id": cid, "path": name})
            with pytest.raises(AppError, match="不是有效的 Office"):
                await core.call("files.page", {"id": cid, "path": "bad.xlsx"})
        finally:
            await core.shutdown()
    run(scenario())
