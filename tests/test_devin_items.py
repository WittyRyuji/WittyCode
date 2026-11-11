"""Devin's tool calls, prompts, statistics, permission levels and the replay of a saved session: pure, no process."""

from __future__ import annotations

import base64

import pytest

from desktop.core.attachments import image_attachment, pdf_attachment, text_attachment
from desktop.core.devin import items
from desktop.core.devin.history import Replay
from desktop.core.devin.policy import (
    CANCELLED,
    LEVEL_OF_MODE,
    MODE_OF_LEVEL,
    level_of,
    mode_of,
    normalize_level,
    option_views,
    outcome_for,
    reject_option,
)
from desktop.core.settings import DEVIN_PERMISSIONS
from desktop.core.transcript import ActivityBlock, TextBlockView, ToolStatus, Transcript, TurnStatus, UserBlock
from desktop.core.usage import Tokens
from tests.support import tiny_png


def call(native, kind="", raw=None, content=None, **more):
    update = {"sessionUpdate": "tool_call", "toolCallId": f"{native}:0#abc", "kind": kind, "_meta": {items.TOOL_NAME_META: native}, **more}
    if raw is not None:
        update["rawInput"] = raw
    if content is not None:
        update["content"] = content
    return update


# ---------------------------------------------------------------------------------------------- tools


def test_a_command_is_an_execute_with_its_command_and_timeout():
    assert items.tool_spec(call("exec", "execute", {"command": "ping -n 2 x", "timeout": 20000})) == ("Execute", {"command": "ping -n 2 x", "timeout": 20000})
    # no raw input: the command is what the preview shows
    preview = [{"type": "content", "content": {"type": "resource", "resource": {"text": "dir /b", "uri": "tool://preview"}}}]
    assert items.tool_spec(call("exec", "execute", None, preview)) == ("Execute", {"command": "dir /b"})
    assert items.tool_spec({"toolCallId": "exec:9#z", "kind": "execute"}) == ("Execute", {"command": ""})


def test_reading_writing_editing_searching_and_fetching_have_the_names_the_page_knows():
    assert items.tool_spec(call("read", "read", {"file_path": "D:\\p\\a.txt"})) == ("Read", {"file_path": "D:\\p\\a.txt"})
    assert items.tool_spec(call("read", "read", None, locations=[{"path": "D:\\p\\b.txt"}])) == ("Read", {"file_path": "D:\\p\\b.txt"})
    new = [{"type": "diff", "path": "D:\\p\\n.txt", "newText": "x\n"}]
    old = [{"type": "diff", "path": "D:\\p\\n.txt", "oldText": "x\n", "newText": "y\n"}]
    assert items.tool_spec(call("write", "edit", {"file_path": "D:\\p\\n.txt", "content": "x\n"}, new)) == ("Create", {"file_path": "D:\\p\\n.txt"})
    assert items.tool_spec(call("edit", "edit", {"file_path": "D:\\p\\n.txt"}, old)) == ("Edit", {"file_path": "D:\\p\\n.txt"})
    assert items.tool_spec(call("edit", "edit", None, old))[1] == {"file_path": "D:\\p\\n.txt"}, "the path comes from the diff when the input has none"
    two = [*new, {"type": "diff", "path": "D:\\p\\m.txt", "oldText": "a", "newText": "b"}]
    assert items.tool_spec(call("multi_edit", "edit", None, two)) == ("ApplyPatch", {"file_path": "D:\\p\\n.txt", "files": ["D:\\p\\n.txt", "D:\\p\\m.txt"]})
    assert items.tool_spec(call("grep", "search", {"query": "beta", "path": "D:\\p"})) == ("Grep", {"pattern": "beta", "path": "D:\\p"})
    assert items.tool_spec(call("find_file_by_name", "search", {"query": "*.txt", "path": "D:\\p"})) == ("Glob", {"pattern": "*.txt", "path": "D:\\p"})
    assert items.tool_spec(call("code_search", "search", {"pattern": "x"})) == ("Grep", {"pattern": "x", "path": ""})
    assert items.tool_spec(call("read_url", "fetch", {"url": "https://x.y"})) == ("FetchUrl", {"url": "https://x.y"})


def test_a_tool_nobody_taught_the_page_keeps_devins_name_and_input():
    assert items.tool_spec(call("get_output", "other", {"shell_id": "cc03", "timeout": 30000})) == ("get_output", {"shell_id": "cc03", "timeout": 30000})
    assert items.tool_spec({"toolCallId": "x", "title": "Think hard", "rawInput": {"a": 1}}) == ("Think hard", {"a": 1})
    assert items.tool_spec({"toolCallId": "x"}) == ("tool", {})
    assert items.native_name({"toolCallId": "grep:3#q"}) == "grep" and items.native_name({"toolCallId": "plain"}) == ""


def test_the_diffs_of_a_call_say_which_files_are_new():
    diffs = items.call_diffs(
        [
            {"type": "diff", "path": "a.txt", "newText": "one\ntwo\n"},
            {"type": "diff", "path": "b.txt", "oldText": "x\n", "newText": "y\n"},
            {"type": "diff", "newText": "no path"},
            {"type": "content", "content": {"type": "text", "text": "not a diff"}},
            "garbage",
        ]
    )
    assert [(d.path, d.is_new_file, d.added, d.removed) for d in diffs] == [("a.txt", True, 2, 0), ("b.txt", False, 1, 1)]
    assert items.call_diffs(None) == ()


def test_what_a_call_says_in_words_is_its_text_its_files_and_its_diffs():
    contents = [
        {"type": "content", "content": {"type": "text", "text": "line 1"}},
        {"type": "content", "content": {"type": "resource_link", "name": "a.txt", "uri": "file:///a.txt"}},
        {"type": "content", "content": {"type": "resource_link", "uri": "file:///b.txt"}},
        {"type": "diff", "path": "n.txt", "newText": "x"},
        {"type": "diff", "path": "m.txt", "oldText": "a", "newText": "b"},
        {"type": "content", "content": {"type": "resource", "resource": {"text": "preview"}}},
        {"type": "content", "content": "odd"},
        3,
    ]
    assert items.content_text(contents) == "line 1\na.txt\nfile:///b.txt\nA n.txt\nM m.txt"
    assert items.content_text(None) == ""


def test_a_tool_ends_when_devin_says_completed_or_failed():
    assert items.call_status("completed") is ToolStatus.DONE and items.call_status("failed") is ToolStatus.ERROR
    assert items.call_status("in_progress") is None and items.call_status(None) is None and items.call_status("pending") is None


# ---------------------------------------------------------------------------------------------- statistics


def dimension(uid, value):
    return {"uid": uid, "kind": {"type": "cumulativeMetric", "value": value}}


def test_a_prompts_cost_is_read_from_its_turn_stats():
    stats = {"responseDimensions": [dimension("agent_messages", 2.0), dimension("input_tokens", 13912.0), dimension("output_tokens", 34.0), dimension("cached_input_tokens", 512.0)]}
    assert items.stats_tokens(stats) == Tokens(13912, 34, None, 512)
    assert items.stats_tokens({"responseDimensions": [dimension("output_tokens", 5.0)]}) == Tokens(0, 5, None, 0)
    assert items.stats_tokens({"turnClientMessageId": "x"}) is None, "a stopped turn reports no figures"
    assert items.stats_tokens({"responseDimensions": [{"uid": "model", "kind": {"type": "metric", "value": "SWE-2"}}]}) is None
    assert items.stats_tokens({"responseDimensions": ["junk", {"uid": "input_tokens", "kind": "odd"}]}) is None


def test_a_usage_update_is_one_model_call_with_its_cached_part_taken_out_of_the_input():
    update = {"used": 14788, "size": 262000, "_meta": {"cognition.ai/inputTokens": 14684, "cognition.ai/outputTokens": 104, "cognition.ai/cachedReadTokens": 512}}
    assert items.usage_call(update) == (Tokens(14172, 104, None, 512), 14788, 262000)
    assert items.usage_call({"used": 0, "size": -1, "_meta": {}}) == (None, None, None)
    assert items.usage_call({"_meta": {"cognition.ai/inputTokens": 10, "cognition.ai/cachedReadTokens": 50}})[0] == Tokens(0, 0, None, 50), "never below zero"
    assert items.usage_call({"used": True, "_meta": "junk"}) == (None, None, None)


def test_times_come_with_nanoseconds_and_zones():
    assert items.parse_time("2026-11-01T14:14:55.301589500+00:00") == pytest.approx(1793542495.301589)
    assert items.parse_time("2026-10-31T21:25:03.000Z") == items.parse_time("2026-10-31T21:25:03+00:00")
    assert items.parse_time("2026-11-01T14:14:55+08:00") == items.parse_time("2026-11-01T06:14:55+00:00")
    assert items.parse_time("2026-11-01T14:14:55") == items.parse_time("2026-11-01T14:14:55+00:00"), "no zone: UTC"
    assert items.parse_time("2026-11-01T14:14:55.5-05:00") == items.parse_time("2026-11-01T19:14:55.5+00:00")
    assert items.parse_time("yesterday") is None and items.parse_time("") is None and items.parse_time(None) is None


# ---------------------------------------------------------------------------------------------- what the user sent


def test_a_prompt_is_the_typed_text_then_the_pictures_then_the_files():
    picture = image_attachment("shot.png", tiny_png(), "image/png")
    note = text_attachment("notes.txt", "milk")
    paper = pdf_attachment("paper.pdf", b"%PDF-1.4", parsed_text="Invoice 4417")
    blocks = items.prompt_blocks("look", [picture, note, paper])
    assert [b["type"] for b in blocks] == ["text", "image", "text", "text"]
    assert blocks[0] == {"type": "text", "text": "look"}
    assert blocks[1]["mimeType"] == "image/png" and base64.b64decode(blocks[1]["data"]) == tiny_png()
    assert blocks[2]["text"] == '<attachment name="notes.txt" type="text/plain">\nmilk\n</attachment>'
    assert blocks[3]["text"].startswith('<attachment name="paper.pdf" type="application/pdf">') and "Invoice 4417" in blocks[3]["text"]
    with pytest.raises(ValueError):
        items.prompt_blocks("x", [pdf_attachment("scan.pdf", b"%PDF-1.4", parsed_text="")])


def test_a_saved_message_gives_back_the_typed_text_and_its_attachments():
    picture = base64.b64encode(tiny_png()).decode()
    text, attachments, handoff = items.user_text_and_attachments(
        [
            {"type": "text", "text": "look"},
            {"type": "image", "data": picture, "mimeType": "image/png"},
            {"type": "text", "text": '<attachment name="notes.txt" type="text/plain">\nmilk\n</attachment>'},
            {"type": "audio"},
            "junk",
        ]
    )
    assert text == "look" and handoff is None
    assert sorted((a.kind, a.name) for a in attachments) == [("image", "图片 1"), ("text", "notes.txt")]
    assert items.user_text_and_attachments([]) == ("", [], None)


# ---------------------------------------------------------------------------------------------- permission levels and buttons


def test_the_levels_are_devins_modes_and_nothing_else_is_a_level():
    assert set(DEVIN_PERMISSIONS) == set(MODE_OF_LEVEL) and LEVEL_OF_MODE["accept-edits"] == "code" and LEVEL_OF_MODE["bypass"] == "full"
    assert [normalize_level(v) for v in ("code", "accept-edits", " bypass ", "full", "readonly", "", None, 3)] == ["code", "code", "full", "full", None, None, None, None]
    assert mode_of("smart") == "smart" and mode_of("nonsense") == "accept-edits" and level_of("plan") == "plan" and level_of("x") is None and level_of(None) is None


def offered():
    return [
        {"optionId": "allow_once", "name": "Allow", "kind": "allow_once"},
        {"optionId": "allow_session", "name": "Yes, allow `Get-Content` commands (this session)", "kind": "allow_always"},
        {"optionId": "allow_always_global", "name": "Yes, always allow `Get-Content` commands in all projects", "kind": "allow_always"},
        {"optionId": "switch_bypass", "name": "Yes, switch to bypass mode", "kind": "allow_always"},
        {"optionId": "something_new", "name": "Do something new", "kind": "other"},
        {"optionId": "reject_once", "name": "Reject", "kind": "reject_once"},
        {"optionId": "reject_always", "name": "Reject always", "kind": "reject_always"},
        {"name": "no id"},
        "junk",
    ]


def test_the_buttons_keep_every_choice_devin_gave_with_a_chinese_label_and_its_scope():
    views = option_views(offered())
    assert [(v.value, v.tone) for v in views] == [
        ("allow_once", "primary"),
        ("allow_session", "normal"),
        ("allow_always_global", "normal"),
        ("switch_bypass", "normal"),
        ("something_new", "normal"),
        ("cancel", "danger"),
        ("reject_always", "danger"),
    ]
    assert views[0].label == "允许（仅此一次）"
    assert views[1].label == "允许，本会话内同类操作不再询问：allow `Get-Content` commands (this session)"
    assert views[2].label == "允许，所有项目里以后一律允许同类操作：always allow `Get-Content` commands in all projects"
    assert views[3].label == "允许，并切换到完全访问" and views[4].label == "Do something new"
    assert (views[5].label, views[5].original) == ("拒绝", "Reject") and views[6].label == "拒绝，以后一律拒绝"
    assert option_views(None) == ()


def test_the_answer_is_the_option_chosen_and_a_refusal_is_the_option_that_refuses():
    options = offered()
    assert outcome_for("allow_once", options) == {"outcome": {"outcome": "selected", "optionId": "allow_once"}}
    assert outcome_for("cancel", options) == {"outcome": {"outcome": "selected", "optionId": "reject_once"}}
    assert outcome_for("cancel", options[:3]) == CANCELLED, "nothing to refuse with: cancelled"
    assert outcome_for(None, options) == CANCELLED
    assert reject_option(options) == "reject_once" and reject_option([]) is None and reject_option(None) is None


# ---------------------------------------------------------------------------------------------- the replay


def user(text, message_id, at="2026-11-01T10:00:00.000000000+00:00", **more):
    return ("session/update", {"update": {"sessionUpdate": "user_message_chunk", "content": {"type": "text", "text": text}, "_meta": {items.CLIENT_MESSAGE_META: message_id, items.TIMESTAMP_META: at}, **more}})


def agent(kind, text, at=None):
    update = {"sessionUpdate": kind, "content": {"type": "text", "text": text}}
    if at:
        update["_meta"] = {items.TIMESTAMP_META: at}
    return ("session/update", {"update": update})


def tool(call_id, native, kind, raw=None, **more):
    return ("session/update", {"update": {"sessionUpdate": "tool_call", "toolCallId": call_id, "kind": kind, "rawInput": raw or {}, "_meta": {items.TOOL_NAME_META: native}, **more}})


def tool_update(call_id, status=None, text=None, exit_code=None):
    update = {"sessionUpdate": "tool_call_update", "toolCallId": call_id}
    if status:
        update["status"] = status
    if text is not None:
        update["content"] = [{"type": "content", "content": {"type": "text", "text": text}}]
    if exit_code is not None:
        update["_meta"] = {"terminal_exit": {"exit_code": exit_code}}
    return ("session/update", {"update": update})


def stats(message_id, read=None, write=0, cached=0):
    params = {"turnClientMessageId": message_id}
    if read is not None:
        params["responseDimensions"] = [dimension("input_tokens", read), dimension("output_tokens", write), dimension("cached_input_tokens", cached)]
    return ("_cognition.ai/turn_stats", params)


def replayed(*messages):
    transcript = Transcript()
    replay = Replay(transcript)
    for method, params in messages:
        replay.feed(method, params)
    return transcript, replay, replay.finish()


def test_two_prompts_are_two_turns_and_each_keeps_its_cost_under_its_message_id():
    transcript, replay, turns = replayed(
        user("one", "m1", "2026-11-01T10:00:00+00:00"),
        agent("agent_thought_chunk", "hmm ", "2026-11-01T10:00:01+00:00"),
        agent("agent_thought_chunk", "ok"),
        agent("agent_message_chunk", "fir", "2026-11-01T10:00:03+00:00"),
        agent("agent_message_chunk", "st"),
        user("two", "m2", "2026-11-01T10:01:00+00:00"),
        agent("agent_message_chunk", "second", "2026-11-01T10:01:02+00:00"),
        stats("m1", 100, 10, 5),  # (the statistics of every prompt come at the end of the replay)
        stats("m2", 200, 20, 6),
    )
    assert len(turns) == 2 and all(t.from_history and t.status is TurnStatus.DONE for t in turns)
    first, second = turns
    assert first.user_block.text == "one" and first.user_block.message_id == "m1"
    assert [type(b) for b in first.blocks] == [UserBlock, ActivityBlock, TextBlockView]
    assert first.blocks[1].entries[0].text == "hmm ok" and first.blocks[2].text == "first"
    assert first.duration == pytest.approx(3.0) and second.duration == pytest.approx(2.0)
    assert replay.usage == {"m1": Tokens(100, 10, None, 5), "m2": Tokens(200, 20, None, 6)}


def test_a_prompt_listed_right_after_another_is_a_steer_in_the_same_turn():
    # (the real program lists a prompt sent to a running turn right after the turn's own, before the turn's first reply)
    _, replay, turns = replayed(
        user("run it", "m1"),
        user("correction", "m2"),
        tool("exec:0#a", "exec", "execute", {"command": "ping"}),
        tool_update("exec:0#a", "completed", "pong", 0),
        agent("agent_message_chunk", "done"),
        user("next", "m3"),
        agent("agent_message_chunk", "ok"),
        stats("m1", 100, 10, 5),
        stats("m2", 50, 5, 1),
        stats("m3", 20, 2, 0),
    )
    first, second = turns
    blocks = [b for b in first.blocks if isinstance(b, UserBlock)]
    assert [(b.text, b.steer, b.pending) for b in blocks] == [("run it", False, False), ("correction", True, False)]
    assert second.user_block.text == "next" and not [b for b in second.blocks if isinstance(b, UserBlock) and b.steer]
    assert set(replay.usage) == {"m1", "m2", "m3"}


def test_a_prompt_sent_after_the_turn_had_answered_comes_back_as_a_turn_of_its_own():
    _, _, turns = replayed(
        user("run it", "m1"),
        tool("exec:0#a", "exec", "execute", {"command": "ping"}),
        user("correction", "m2"),
        agent("agent_message_chunk", "done"),
        stats("m1", 1, 1, 0),
        stats("m2", 1, 1, 0),
    )
    assert [t.user_block.text for t in turns] == ["run it", "correction"]


def test_a_message_in_several_pieces_is_one_message():
    _, _, turns = replayed(
        ("session/update", {"update": {"sessionUpdate": "user_message_chunk", "content": {"type": "text", "text": "look"}, "_meta": {items.CLIENT_MESSAGE_META: "m1"}}}),
        ("session/update", {"update": {"sessionUpdate": "user_message_chunk", "content": {"type": "image", "data": base64.b64encode(tiny_png()).decode(), "mimeType": "image/png"}, "_meta": {items.CLIENT_MESSAGE_META: "m1"}}}),
        agent("agent_message_chunk", "I see."),
        stats("m1", 10, 1, 0),
    )
    (turn,) = turns
    assert turn.user_block.text == "look" and [a.kind for a in turn.user_block.attachments] == ["image"]
    assert not [b for b in turn.blocks if isinstance(b, UserBlock) and b.steer], "pieces of one message are not messages"


def test_a_tool_in_a_saved_turn_has_its_input_diffs_output_and_exit_code():
    transcript, _, turns = replayed(
        user("go", "m1"),
        tool("exec:0#a", "exec", "execute", {"command": "pytest"}),
        tool_update("exec:0#a", "in_progress", "1 failed"),
        tool_update("exec:0#a", "in_progress", "1 failed, 2 passed", 1),
        tool_update("exec:0#a", "completed"),
        tool("write:0#b", "write", "edit", {"file_path": "n.txt"}, content=[{"type": "diff", "path": "n.txt", "newText": "x\n"}]),
        tool_update("write:0#b", "completed"),
        tool("exec:0#c", "exec", "execute", {"command": "never ends"}),
        tool("grep:0#d", "grep", "search", {"query": "x", "path": "."}),
        tool_update("grep:0#d", "failed", "boom"),
        tool_update("unknown:0#z", "completed"),
        stats("m1", 1, 1, 0),
    )
    (turn,) = turns
    run, write, hung, grep = [e for b in turn.blocks if isinstance(b, ActivityBlock) for e in b.entries]
    assert (run.name, run.status, run.result) == ("Execute", ToolStatus.DONE, "1 failed, 2 passed\n\n[Process exited with code 1]")
    assert (write.name, write.status, write.diffs[0].is_new_file) == ("Create", ToolStatus.DONE, True)
    assert (hung.name, hung.status) == ("Execute", ToolStatus.CANCELLED), "a tool that never ended was cut off"
    assert (grep.name, grep.status, grep.result) == ("Grep", ToolStatus.ERROR, "boom")


def test_words_and_thoughts_between_tools_are_blocks_of_their_own():
    _, _, turns = replayed(
        user("go", "m1"),
        agent("agent_message_chunk", "A"),
        agent("agent_thought_chunk", "t1"),
        agent("agent_message_chunk", "B"),
        tool("exec:0#a", "exec", "execute", {"command": "x"}),
        agent("agent_message_chunk", "C"),
        stats("m1", 1, 1, 0),
    )
    (turn,) = turns
    assert [type(b).__name__ for b in turn.blocks] == ["UserBlock", "TextBlockView", "ActivityBlock", "TextBlockView", "ActivityBlock", "TextBlockView"]
    assert [b.text for b in turn.blocks if isinstance(b, TextBlockView)] == ["A", "B", "C"]


def test_the_title_the_context_and_noise_of_a_replay():
    transcript, replay, turns = replayed(
        agent("agent_message_chunk", "orphan words"),  # (no user message before: the turn is still kept)
        ("session/update", {"update": {"sessionUpdate": "session_info_update", "title": "A title"}}),
        ("session/update", {"update": {"sessionUpdate": "session_info_update", "title": None}}),
        ("session/update", {"update": {"sessionUpdate": "usage_update", "used": 1234, "size": 262000, "_meta": {}}}),
        ("session/update", {"update": {"sessionUpdate": "usage_update", "used": 99, "size": 1, "_meta": {items.SUBAGENT_META: {"parentAgentId": "root"}}}}),
        ("session/update", {"update": {"sessionUpdate": "available_commands_update"}}),
        ("session/update", {"update": "junk"}),
        ("session/update", "junk"),
        ("_cognition.ai/mcp/serversChanged", {}),
        ("_cognition.ai/turn_stats", {"turnClientMessageId": ""}),
        ("somewhere/else", {"x": 1}),
    )
    assert (replay.title, replay.context_used, replay.context_window) == ("A title", 1234, 262000)
    (turn,) = turns
    assert turn.user_block is None and turn.blocks[0].text == "orphan words"
    assert replay.usage == {}


def test_nothing_replayed_is_no_turns():
    assert replayed()[2] == []
