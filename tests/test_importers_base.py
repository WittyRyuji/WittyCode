"""``core.importers.base``: the Builder every source fills (secret scrubbing, streamed answers, tool lines) and the
file helpers the sources share."""

from __future__ import annotations

from desktop.core.importers.base import Builder, Report, tail_lines
from tests.importer_support import FAKE_KEY


def test_streamed_parts_of_one_answer_stay_one_message():
    builder = Builder(Report())
    builder.assistant("前半", 1.0, key="msg-1")
    builder.assistant("后半", 2.0, key="msg-1")
    builder.assistant("另一条", 3.0, key="msg-2")
    assert [(m.role, m.text, m.at) for m in builder.messages] == [("assistant", "前半\n\n后半", 1.0), ("assistant", "另一条", 3.0)]


def test_a_secret_line_in_the_users_words_is_dropped_and_counted():
    report = Report()
    builder = Builder(report)
    builder.user(f"keep me\napi key: {FAKE_KEY}\nand me", 1.0)
    assert [m.text for m in builder.messages] == ["keep me\nand me"]
    assert report.secret_lines == 1


def test_the_first_line_of_a_tail_is_dropped_when_cut_in_two(tmp_path):
    path = tmp_path / "rollout.jsonl"
    path.write_bytes(b'{"a": 1}\n{"b": 2}\n{"c": 3}\n')
    assert tail_lines(path, 1000) == [b'{"a": 1}', b'{"b": 2}', b'{"c": 3}']
    assert tail_lines(path, 21) == [b'{"b": 2}', b'{"c": 3}']
