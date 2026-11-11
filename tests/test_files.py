"""``core.files``: a path the way the model wrote it, found and read for the side panel."""

from __future__ import annotations

import pytest

from desktop.core import files
from desktop.core.files import FileProblem, Ref, is_runnable, parse_ref, read_for_panel, resolve


def test_a_reference_may_end_in_a_line_or_a_range_and_a_drive_letter_is_not_one():
    assert parse_ref("docs/a.md") == Ref("docs/a.md")
    assert parse_ref("src/a.py:42") == Ref("src/a.py", 42, None)
    assert parse_ref("src/a.ts:42-51") == Ref("src/a.ts", 42, 51)
    assert parse_ref("src/a.ts:51-42") == Ref("src/a.ts", 42, 51), "a range the wrong way round"
    assert parse_ref("src/a.py:42:7") == Ref("src/a.py", 42, None), "a column is dropped"
    assert parse_ref("D:\\work\\b.txt") == Ref("D:\\work\\b.txt")
    assert parse_ref("D:\\work\\b.txt:7") == Ref("D:\\work\\b.txt", 7, None)
    assert parse_ref("C:12") == Ref("C:12"), "a drive letter and digits are not a file and a line"
    assert parse_ref("  `a/b.py`  ") == Ref("a/b.py"), "backticks and quotes around it"
    assert parse_ref('"a b.py:3"') == Ref("a b.py", 3, None)


def test_a_relative_path_is_looked_for_in_the_project_and_an_absolute_one_where_it_is(tmp_path):
    project = tmp_path / "proj"
    (project / "docs").mkdir(parents=True)
    note = project / "docs" / "note.md"
    note.write_text("hi", encoding="utf-8")
    elsewhere = tmp_path / "elsewhere.txt"
    elsewhere.write_text("x", encoding="utf-8")
    assert resolve("docs/note.md:3", project) == (note, Ref("docs/note.md", 3, None))
    assert resolve(".\\docs\\note.md", project)[0] == note
    assert resolve("docs/../docs/note.md", project)[0] == note, "dots are folded away"
    assert resolve(str(elsewhere), project)[0] == elsewhere
    assert resolve(elsewhere.as_uri(), project)[0] == elsewhere, "a file: address"
    assert resolve("docs", project)[0] == project / "docs", "a folder is found too"


def test_a_file_that_is_not_there_says_where_it_was_looked_for(tmp_path):
    with pytest.raises(FileProblem) as caught:
        resolve("nope/missing.md", tmp_path)
    assert "nope/missing.md" in str(caught.value) and str(tmp_path) in str(caught.value)
    with pytest.raises(FileProblem):
        resolve("   ", tmp_path)


def test_a_file_on_another_computer_is_never_looked_for(tmp_path, monkeypatch):
    looked = []
    real = files.Path.exists
    monkeypatch.setattr(files.Path, "exists", lambda self: looked.append(str(self)) or real(self))
    for raw in ("\\\\server\\share\\a.txt", "//server/share/a.txt", "file://server/share/a.txt", "\\\\?\\UNC\\server\\share\\a.txt", "\\\\.\\C:\\a.txt"):
        with pytest.raises(FileProblem) as caught:
            resolve(raw, tmp_path)
        assert "网络" in str(caught.value), raw
    assert looked == [], "nothing was even asked of the system"
    here = tmp_path / "x.txt"
    here.write_text("x", encoding="utf-8")
    assert resolve("file://localhost/" + here.as_posix().lstrip("/"), tmp_path)[0] == here, "localhost is this computer"


def test_the_home_folder_is_understood(tmp_path, monkeypatch):
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    monkeypatch.setenv("HOME", str(tmp_path))
    (tmp_path / "n.txt").write_text("x", encoding="utf-8")
    assert resolve("~/n.txt", tmp_path / "elsewhere")[0] == tmp_path / "n.txt"


def test_text_files_come_back_in_the_encoding_they_were_written_in(tmp_path):
    cases = {
        "utf8.txt": ("中文 and text\n".encode("utf-8"), "UTF-8"),
        "bom.txt": (b"\xef\xbb\xbf" + "中文\n".encode("utf-8"), "UTF-8 (BOM)"),
        "gbk.txt": ("中文内容\n".encode("gb18030"), "GB18030"),
        "wide.txt": ("中文\n".encode("utf-16"), "UTF-16"),
    }
    for name, (data, label) in cases.items():
        path = tmp_path / name
        path.write_bytes(data)
        panel = read_for_panel(path)
        assert panel.text.strip().endswith(("中文 and text", "中文", "中文内容")) and panel.encoding == label, name
        assert "\ufffd" not in panel.text and "\ufeff" not in panel.text and not panel.truncated


def test_lines_and_markdown_are_counted_and_named(tmp_path):
    path = tmp_path / "a.md"
    path.write_bytes(b"one\ntwo\nthree\n")
    panel = read_for_panel(path)
    assert (panel.lines, panel.markdown, panel.size) == (3, True, 14)
    path.write_bytes(b"one\ntwo")
    assert read_for_panel(path).lines == 2
    plain = tmp_path / "a.py"
    plain.write_text("x", encoding="utf-8")
    assert read_for_panel(plain).markdown is False
    empty = tmp_path / "empty.txt"
    empty.write_bytes(b"")
    assert (read_for_panel(empty).text, read_for_panel(empty).lines) == ("", 0)


def test_a_binary_file_a_folder_and_a_missing_one_are_refused_with_a_sentence(tmp_path):
    binary = tmp_path / "pic.bin"
    binary.write_bytes(b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR")
    with pytest.raises(FileProblem) as caught:
        read_for_panel(binary)
    assert "不是文本文件" in str(caught.value)
    with pytest.raises(FileProblem) as caught:
        read_for_panel(tmp_path)
    assert "文件夹" in str(caught.value)
    with pytest.raises(FileProblem):
        read_for_panel(tmp_path / "gone.txt")


def test_a_big_file_is_read_only_as_far_as_the_limit_and_not_in_the_middle_of_a_character(tmp_path):
    path = tmp_path / "big.txt"
    path.write_bytes(("中" * 1_000).encode("utf-8"))  # three bytes each
    for limit in (2_999, 2_998, 1_001):
        panel = read_for_panel(path, limit=limit)
        assert "\ufffd" not in panel.text, limit
        assert set(panel.text) == {"中"} and len(panel.text) == limit // 3
        assert panel.truncated and panel.size == 3_000
    whole = read_for_panel(path, limit=3_000)
    assert not whole.truncated and len(whole.text) == 1_000


def test_only_programs_and_scripts_count_as_runnable():
    for name in ("a.exe", "run.BAT", "x.ps1", "y.py", "z.js", "w.lnk", "q.sh", "setup.msi"):
        assert is_runnable(files.Path(name)), name
    for name in ("a.md", "a.txt", "a.json", "pic.png", "doc.pdf", "sheet.xlsx", "noext"):
        assert not is_runnable(files.Path(name)), name
