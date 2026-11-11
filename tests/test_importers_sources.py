"""``core.importers.sources`` and what every source must stand: missing folders, damaged and foreign lines, and files of
tens of megabytes read as a stream."""

from __future__ import annotations

import json
import random
import time
import tracemalloc
from pathlib import Path

import pytest

from desktop.core.importers import LABELS, ClaudeSource, CodexSource, DevinSource, FactorySource, sources
from desktop.core.importers.base import parse_time, records
from tests.importer_support import ClaudeTranscript, DevinDb, DroidSession, Rollout, chain, devin_chat, text_block, tool_use, write_jsonl

THREAD = "019a5555-0000-7000-8000-900000000005"


def test_four_sources_in_order_with_their_places_taken_from_the_environment(tmp_path):
    env = {"USERPROFILE": str(tmp_path / "me"), "APPDATA": str(tmp_path / "Roaming")}
    found = sources(env=env)
    assert [s.app for s in found] == ["claude", "codex", "devin", "factory"]
    assert [s.label for s in found] == [LABELS[s.app] for s in found]
    claude, codex, devin, factory = found
    assert (claude.root, codex.root, devin.root, factory.root) == (
        tmp_path / "me" / ".claude", tmp_path / "me" / ".codex", tmp_path / "Roaming" / "devin", tmp_path / "me" / ".factory",
    )
    assert next(s for s in sources(env={"HOME": str(tmp_path / "h")}) if s.app == "factory").root == tmp_path / "h" / ".factory"


def test_on_an_empty_machine_nothing_is_available_and_nothing_raises(tmp_path):
    for source in sources(home=tmp_path / "me", appdata=tmp_path / "Roaming", env={}):
        assert not source.available()
        assert source.memories() == [] and source.sessions() == []
        assert source.report.problems == []


GARBAGE = [
    "",
    "   ",
    "{",
    "[1, 2, 3]",
    "null",
    "42",
    '"just a string"',
    "{\"type\": " + "[" * 5000 + "]" * 5000 + "}",
    "\x00\x01\x02 binary",
    '{"timestamp": NaN, "type": "user"}',
    '{"timestamp": "9999-12-31T23:59:59-14:00", "type": "user", "message": {"role": "user", "content": "late"}}',
]


def test_claude_lines_of_unknown_shapes_are_skipped(tmp_path):
    claude = tmp_path / ".claude"
    weird = [
        {"type": "user", "message": {"role": "user", "content": 5}},
        {"type": "user", "message": "not an object"},
        {"type": "assistant", "message": {"content": [1, "x", None, {"type": "tool_use", "input": "not an object", "name": 7}]}},
        {"type": "assistant", "message": {"content": "plain string answer"}},
        {"type": "custom-title", "customTitle": 5},
        {"type": "ai-title", "aiTitle": ["x"]},
        {"cwd": 5, "timestamp": {"no": 1}},
        {"type": "summary", "summary": None},
    ]
    transcript = ClaudeTranscript("D:\\w").user("real question")
    for line in GARBAGE + [json.dumps(w) for w in weird]:
        transcript.raw(line)
    transcript.write(claude)
    source = ClaudeSource(claude)
    (info,) = source.sessions()
    texts = [m.text for m in source.read(info)]
    assert texts[0] == "real question" and "plain string answer" in texts
    assert source.report.bad_records >= 6


def test_codex_lines_of_unknown_shapes_are_skipped(tmp_path):
    home = tmp_path / "codex"
    weird = [
        {"type": "response_item", "payload": "x"},
        {"type": "response_item", "payload": {"type": "message", "role": "user", "content": 5}},
        {"type": "response_item", "payload": {"type": "function_call", "name": 5, "arguments": 5}},
        {"type": "response_item", "payload": {"type": "local_shell_call", "action": "x"}},
        {"type": "response_item", "payload": {"type": "web_search_call", "action": None}},
        {"type": "session_meta", "payload": {"cwd": 5, "source": [1]}},
        {"type": "event_msg", "payload": None},
        {"type": "compacted", "payload": {"replacement_history": "x"}},
    ]
    rollout = Rollout(THREAD, "D:\\w").user("real question")
    for line in GARBAGE + [json.dumps(w) for w in weird]:
        rollout.raw(line)
    rollout.assistant("answer")
    rollout.write(home)
    source = CodexSource(home)
    (info,) = source.sessions()
    messages = source.read(info)
    assert messages[0].text == "real question" and messages[-1].text == "answer"
    assert source.report.bad_records >= 6


def test_factory_lines_of_unknown_shapes_are_skipped(tmp_path):
    factory = tmp_path / ".factory"
    session = DroidSession("ffffffff-1111-2222-3333-444444444444", "D:\\w").message("user", [text_block("real question")])
    for line in GARBAGE + [json.dumps(w) for w in [{"type": "message", "message": 5}, {"type": "message", "message": {"role": "assistant", "content": "string answer"}}, {"type": "session_start", "title": 5, "cwd": [1]}, {"type": "message", "message": {"role": "user", "content": [5, {"type": "text", "text": 7}]}}]]:
        session.raw(line)
    session.write(factory)
    source = FactorySource(factory)
    (info,) = source.sessions()
    assert [m.text for m in source.read(info)] == ["real question", "string answer"]


def test_devin_rows_of_unknown_shapes_are_skipped(tmp_path):
    devin = tmp_path / "devin"
    weird = [
        devin_chat("user", "real question", user_input=True),
        {"role": "assistant", "content": 5, "tool_calls": "x", "metadata": 7},
        {"role": "assistant", "content": "answer", "tool_calls": [5, {"name": 3, "arguments": "not json"}, {"name": "exec", "arguments": {"command": ["a", "b"]}}]},
        {"role": None},
    ]
    db = DevinDb(devin / "cli" / "sessions.db").session("s", "D:\\w", chain(1, weird), head=4)
    db.connection.execute("update message_nodes set metadata = 'not json' where node_id = 2")
    db.connection.commit()
    db.close()
    source = DevinSource(devin)
    (info,) = source.sessions()
    assert [m.text for m in source.read(info)] == ["real question", "answer\n[工具] ? not json\n[工具] exec a b"]


def test_a_file_that_cannot_be_opened_is_skipped(tmp_path):
    claude = tmp_path / ".claude"
    (claude / "projects" / "D--w" / "folder.jsonl").mkdir(parents=True)
    ClaudeTranscript("D:\\w", session="ok").user("fine").write(claude, "D--w")
    assert [info.native_id for info in ClaudeSource(claude).sessions()] == ["ok"]


def test_times_in_every_form_the_apps_write(tmp_path):
    assert parse_time("2026-11-07T10:00:00Z") == parse_time("2026-11-07T18:00:00+08:00") == parse_time("2026-11-07 10:00:00")
    assert parse_time("2026-11-07T10:00:00.123456789Z") == pytest.approx(parse_time("2026-11-07T10:00:00Z") + 0.123456)
    assert parse_time(1_790_000_000) == parse_time(1_790_000_000_000) == parse_time(1_790_000_000_000_000) == 1_790_000_000
    for bad in (None, "", "yesterday", "2026-13-40T00:00:00Z", -5, 0, True, float("nan"), float("inf"), "9999-12-31T23:59:59-14:00", [1]):
        assert parse_time(bad) is None
    assert list(records([b"{}", b"[]", b"x", b"{\"a\": 1}"], None)) == [{}, {"a": 1}]


def _big_claude(claude: Path, megabytes: int) -> Path:
    rng = random.Random(7)
    transcript = ClaudeTranscript("D:\\w", session="big").user("first question")
    output = "".join(rng.choice("abcdefghij \n") for _ in range(100_000))
    number = 0
    while len(transcript.lines) * 100_000 < megabytes * 1024 * 1024:
        number += 1
        transcript.assistant([text_block(f"step {number}"), tool_use("Bash", f"t{number}", command=f"build {number}")], f"m{number}")
        transcript.tool_result(f"t{number}", output)
    transcript.meta("custom-title", customTitle="The big one")
    return transcript.write(claude)


def test_a_transcript_of_twenty_megabytes_is_listed_quickly_and_read_as_a_stream(tmp_path):
    claude = tmp_path / ".claude"
    path = _big_claude(claude, 20)
    assert path.stat().st_size > 20 * 1024 * 1024
    source = ClaudeSource(claude)
    tracemalloc.start()
    started = time.perf_counter()
    (info,) = source.sessions()
    listed = time.perf_counter() - started
    _, peak = tracemalloc.get_traced_memory()
    tracemalloc.stop()
    assert listed < 2.0 and peak < 8 * 1024 * 1024
    assert not info.exact and info.title == "The big one"
    tracemalloc.start()
    started = time.perf_counter()
    messages = source.read(info)
    took = time.perf_counter() - started
    _, peak = tracemalloc.get_traced_memory()
    tracemalloc.stop()
    assert took < 10.0 and peak < 12 * 1024 * 1024
    assert messages[0].text == "first question" and len(messages) > 100
    assert all("abcdefghij" not in m.text for m in messages)


def test_a_rollout_of_twenty_megabytes_is_read_as_a_stream(tmp_path):
    home = tmp_path / "codex"
    rollout = Rollout(THREAD, "D:\\w").user("go")
    blob = "o" * 200_000
    while len(rollout.lines) * 200_000 < 20 * 1024 * 1024:
        rollout.call("shell", {"command": ["ls"]}, f"c{len(rollout.lines)}", blob)
    rollout.assistant("done")
    rollout.write(home)
    source = CodexSource(home)
    tracemalloc.start()
    (info,) = source.sessions()
    messages = source.read(info)
    _, peak = tracemalloc.get_traced_memory()
    tracemalloc.stop()
    assert peak < 12 * 1024 * 1024
    assert messages[0].text == "go" and messages[-1].text == "done"
