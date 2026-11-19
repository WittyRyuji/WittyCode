"""Archive attachments become bounded text; nothing is extracted into the workspace."""
import io
import stat
import tarfile
import zipfile

import pytest

from desktop.core import archive_attachments as archives
from desktop.core.attachments import TEXT, Attachment, Rejection
from desktop.ui import attachments as loading


def zip_bytes(entries):
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, 'w', compression=zipfile.ZIP_DEFLATED) as archive:
        for name, data in entries:
            archive.writestr(name, data)
    return buffer.getvalue()


@pytest.mark.parametrize('from_disk', [False, True])
def test_zip_becomes_one_named_text_attachment_with_inner_paths(tmp_path, from_disk):
    entries = [('jobs/', b''), ('jobs/test.log', b'Pytest failed: expected 1'),
               ('summary.txt', '回归失败'.encode()), ('scan.pdf', b'%PDF-1.7\nnot text')]
    payload = zip_bytes(entries)
    path = tmp_path / 'runner.zip'
    if from_disk:
        path.write_bytes(payload)
        result = loading.load_path(path)
        assert path.read_bytes() == payload
        assert set(tmp_path.iterdir()) == {path}
    else:
        result = loading.load_bytes(path.name, payload)
        assert not list(tmp_path.iterdir())
    assert isinstance(result, Attachment) and result.kind == TEXT and result.name == path.name
    assert result.data == b'' and '已解包 · 2 个文本文件' in result.note
    assert 'jobs/test.log' in result.text and 'Pytest failed: expected 1' in result.text and '回归失败' in result.text
    assert '未附加的二进制文件："scan.pdf"' in result.text and '%PDF' not in result.text


@pytest.mark.parametrize(('name', 'mode'), [('src.tar', 'w'), ('src.tar.gz', 'w:gz'), ('src.TGZ', 'w:gz')])
def test_tar_supports_standard_relative_members(name, mode):
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode=mode) as archive:
        directory = tarfile.TarInfo('.')
        directory.type = tarfile.DIRTYPE
        archive.addfile(directory)
        text = b'def sample(): return 1'
        item = tarfile.TarInfo('./pkg/example.py')
        item.size = len(text)
        archive.addfile(item, io.BytesIO(text))
    result = loading.load_bytes(name, buffer.getvalue())
    assert isinstance(result, Attachment) and 'pkg/example.py' in result.text and 'def sample' in result.text


@pytest.mark.parametrize('name', ['../outside.txt', '/absolute.txt', 'D:/elsewhere.txt', 'safe/data:stream.txt',
                                  'dir/../outside.txt', 'dir\\file.txt', 'NUL.txt'])
def test_unsafe_archive_paths_are_rejected(name):
    unsafe = zipfile.ZipInfo(name)
    # The writer normalizes Windows path separators; keep the authored entry name for this parser test.
    unsafe.filename = name
    result = loading.load_bytes('paths.zip', zip_bytes([('valid.txt', b'example'), (unsafe, b'information')]))
    assert isinstance(result, Rejection) and '不安全' in result.reason


def test_archive_links_and_ambiguous_names_are_rejected():
    link = zipfile.ZipInfo('link.txt')
    link.create_system = 3
    link.external_attr = (stat.S_IFLNK | 0o777) << 16
    for entries, message in [([(link, b'outside.txt')], '链接'),
                             ([('notes.txt', b'a'), ('NOTES.TXT', b'b')], '重名')]:
        result = loading.load_bytes('fixture.zip', zip_bytes(entries))
        assert isinstance(result, Rejection) and message in result.reason


def test_archive_expansion_and_member_limits_are_checked(monkeypatch):
    monkeypatch.setattr(archives, 'MAX_EXPANDED', 1024)
    oversized = loading.load_bytes('oversized.zip', zip_bytes([('large.log', b'x' * 1025)]))
    assert isinstance(oversized, Rejection) and '解包限制' in oversized.reason
    monkeypatch.setattr(archives, 'MAX_MEMBERS', 1)
    many = loading.load_bytes('many.zip', zip_bytes([('a.log', b'A'), ('b.log', b'B')]))
    assert isinstance(many, Rejection) and '解包限制' in many.reason


def test_archive_source_and_text_limits_are_visible(monkeypatch):
    over_text = zip_bytes([('one.log', b'a' * 200), ('two.log', b'b' * 200)])
    monkeypatch.setattr(archives, 'MAX_TEXT_BYTES', 300)
    full = loading.load_bytes('logs.zip', over_text)
    assert isinstance(full, Rejection) and '拆成几个较小' in full.reason
    monkeypatch.setattr(archives, 'MAX_SOURCE', 50)
    huge = loading.load_bytes('logs.zip', over_text)
    assert isinstance(huge, Rejection) and '压缩包超过' in huge.reason


@pytest.mark.parametrize(('data', 'message'), [(b'PK\x03\x04 bad', '损坏'),
                                                (zip_bytes([('sample.bin', b'\x00\x00\x00')]), '没有可附加的文本')])
def test_an_unreadable_archive_is_not_an_empty_attachment(data, message):
    result = loading.load_bytes('broken.zip', data)
    assert isinstance(result, Rejection) and message in result.reason
