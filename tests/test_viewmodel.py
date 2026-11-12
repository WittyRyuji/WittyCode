"""The transcript as JSON for the React page: the shapes the page relies on."""

from __future__ import annotations

from desktop.core.attachments import Thumbnail, image_attachment
from desktop.core.handoff import build_handoff
from desktop.core.transcript import Transcript
from desktop.core.viewmodel import OUTPUT_LIMIT, context_json, turn_json, turns_json, usage_json
from desktop.core.context import ContextState
from types import SimpleNamespace

from tests.support import tiny_png
from tests.transcript_support import ask_permission, ask_question, finish, result, say
from tests.transcript_support import tool as use_tool


def activity_of(turn_data):
    return next(block for block in turn_data["blocks"] if block["kind"] == "activity")


def test_a_finished_turn_has_user_text_activity_and_a_summary():
    transcript = Transcript()
    transcript.start_turn("List the files")
    say(transcript, "Looking.")
    use_tool(transcript, "t1", "Read", file_path="README.md")
    result(transcript, "t1", "hello")
    finish(transcript, seconds=12.34)
    data = turn_json(transcript.turns[0])
    assert data["status"] == "done" and data["seconds"] == 12.3 and data["steps"] == 1
    assert [block["kind"] for block in data["blocks"]] == ["user", "text", "activity"]
    assert data["blocks"][1] == {"kind": "text", "id": data["blocks"][1]["id"], "markdown": "Looking.", "streaming": False}
    tool = activity_of(data)["entries"][0]
    assert tool["kind"] == "tool" and tool["name"] == "Read" and tool["category"] == "read" and tool["status"] == "done"
    assert tool["description"].endswith("README.md") and tool["output"] == "hello"
    assert tool["meta"] == {"file_path": "README.md"}


def test_a_running_turn_streams_and_reports_progress_as_output():
    transcript = Transcript()
    transcript.start_turn("go")
    say(transcript, "Hel", done=False)
    data = turn_json(transcript.turns[0])
    assert data["status"] == "running" and data["blocks"][1]["streaming"] is True and "seconds" not in data
    use_tool(transcript, "t1", "Execute", command="dir")
    item = transcript.turns[0].tools["t1"]
    item.progress = "line 1\nline 2"
    tool = activity_of(turn_json(transcript.turns[0]))["entries"][0]
    assert tool["command"] == "dir" and tool["output"] == "line 1\nline 2" and "meta" not in tool


def test_long_output_is_cut_at_the_end_for_commands_and_the_start_otherwise():
    transcript = Transcript()
    transcript.start_turn("go")
    big = "".join(f"{n}\n" for n in range(40_000))
    use_tool(transcript, "t1", "Execute", command="dir")
    result(transcript, "t1", big)
    use_tool(transcript, "t2", "Read", file_path="a")
    result(transcript, "t2", big)
    entries = activity_of(turn_json(transcript.turns[0]))["entries"]
    command, read = entries
    assert command["output"].startswith("（已省略") and command["output"].rstrip().endswith("39999")
    assert read["output"].startswith("0\n") and read["output"].rstrip().endswith("个字符）")
    assert len(command["output"]) < OUTPUT_LIMIT + 100 and len(read["output"]) < OUTPUT_LIMIT + 100


def test_an_edit_carries_its_diff_and_no_input_dump():
    transcript = Transcript()
    transcript.start_turn("go")
    use_tool(transcript, "t1", "Edit", file_path="a.py", old_str="x = 1\n", new_str="x = 2\n")
    tool = activity_of(turn_json(transcript.turns[0]))["entries"][0]
    assert "meta" not in tool
    (diff,) = tool["diffs"]
    assert diff["path"] == "a.py" and diff["added"] == 1 and diff["removed"] == 1 and diff["kind"] == "edit"
    assert {line["t"] for line in diff["lines"]} <= {"add", "del", "ctx", "gap"}


def test_a_todo_list_becomes_todo_rows():
    transcript = Transcript()
    transcript.start_turn("go")
    todos = "1. [completed] first\n2. [in_progress] second\n3. [pending] third"
    use_tool(transcript, "t1", "TodoWrite", todos=todos)
    tool = activity_of(turn_json(transcript.turns[0]))["entries"][0]
    assert [row["state"] for row in tool["todos"]] == ["completed", "in_progress", "pending"]


def test_prompts_are_listed_while_pending_and_settled_ones_are_dropped():
    transcript = Transcript()
    transcript.start_turn("go")
    use_tool(transcript, "t0", "Execute", command="del x")
    prompt = ask_permission(transcript, "t0")
    data = turn_json(transcript.turns[0])
    (shown,) = data["prompts"]
    assert shown["kind"] == "permission" and shown["status"] == "pending" and shown["id"] == prompt.id
    assert shown["options"] and all({"value", "label", "tone"} <= set(option) for option in shown["options"])
    assert shown["actions"][0]["title"]
    transcript.answer_permission(prompt.id, "cancel")
    assert turn_json(transcript.turns[0])["prompts"] == []


def test_a_question_prompt_and_its_answer_block():
    transcript = Transcript()
    transcript.start_turn("go")
    prompt = ask_question(transcript)
    (shown,) = turn_json(transcript.turns[0])["prompts"]
    assert shown["kind"] == "question" and shown["questions"] and {"topic", "question", "options", "multiSelect"} <= set(shown["questions"][0])
    answers = ["a"] * len(prompt.questions)
    transcript.answer_question(prompt.id, answers)
    blocks = turn_json(transcript.turns[0])["blocks"]
    qa = next(block for block in blocks if block["kind"] == "qa")
    assert qa["cancelled"] is False and qa["pairs"][0][1] == "a"


def test_attachments_carry_a_thumbnail_only_for_pictures():
    transcript = Transcript()
    picture = image_attachment("shot.png", tiny_png(40, 30), "image/png")
    transcript.start_turn("look", [picture])
    thumb = lambda item: Thumbnail("data:image/png;base64,AAAA", 4, 3)  # noqa: E731
    (user,) = turn_json(transcript.turns[0], thumb)["blocks"]
    (card,) = user["attachments"]
    assert card["kind"] == "image" and card["url"].startswith("data:image/png")
    assert (card["width"], card["height"]) == (40, 30), "the original picture's size, not the thumbnail's 4x3"
    assert card["id"] and card["name"] == "shot.png" and card["size"] == len(tiny_png(40, 30))
    (bare,) = turn_json(transcript.turns[0])["blocks"][0]["attachments"]
    assert "url" not in bare and (bare["width"], bare["height"]) == (40, 30)


def test_an_image_whose_size_cannot_be_read_sends_no_dimensions():
    transcript = Transcript()
    picture = image_attachment("broken.png", b"\x89PNG\r\n\x1a\n" + b"\x00" * 4, "image/png")
    transcript.start_turn("look", [picture])
    thumb = lambda item: Thumbnail("data:image/png;base64,AAAA", 4, 3)  # noqa: E731
    (card,) = turn_json(transcript.turns[0], thumb)["blocks"][0]["attachments"]
    assert card["url"].startswith("data:image/png") and "width" not in card and "height" not in card


def test_a_truncated_diff_says_so_in_a_gap_line_the_page_shows():
    from desktop.core.diffs import diff_for_new_file
    from desktop.core.viewmodel import diff_json

    diff = diff_for_new_file("big.txt", "\n".join(f"line {n}" for n in range(10)), limit=4)
    assert diff.truncated == 6
    data = diff_json(diff)
    assert "truncated" not in data, "the page reads no such flag"
    assert data["lines"][-1] == {"t": "gap", "text": "（已省略 6 行）"}
    whole = diff_json(diff_for_new_file("small.txt", "one\ntwo", limit=4))
    assert whole["lines"][-1]["t"] == "add", "no extra row when nothing was cut"


def test_a_change_of_engine_is_a_turn_of_its_own_and_marks_the_next_message():
    transcript = Transcript()
    transcript.start_turn("hello")
    say(transcript, "Hi there.")
    finish(transcript)
    handoff = build_handoff(transcript.turns)
    transcript.add_switch(
        from_model="GPT-6.1-Sol", from_engine="ChatGPT Plan", to_model="SWE-2", to_engine="Devin",
        carried=handoff.carried, total=handoff.total, size=handoff.size, truncated=handoff.truncated,
    )
    transcript.start_turn("continue", handoff=(handoff.carried, handoff.truncated))
    data = turns_json(transcript.turns)
    switch = data[1]["blocks"][0]
    assert switch["kind"] == "switch" and switch["from"] == {"model": "GPT-6.1-Sol", "engine": "ChatGPT Plan"} and switch["to"]["engine"] == "Devin"
    assert switch["carried"] == 2 and switch["total"] == 2 and switch["truncated"] is False and switch["bytes"] > 0
    assert data[2]["blocks"][0]["handoff"] == {"carried": 2, "truncated": False}
    assert data[1]["status"] == "done"


def test_usage_and_context_figures():
    assert usage_json(None) is None
    figures = usage_json(SimpleNamespace(input_tokens=394, output_tokens=498, cache_creation_tokens=0, cache_read_tokens=68_300))
    assert figures["input"] == 394 and figures["output"] == 498 and figures["cacheRead"] == 68_300
    assert isinstance(figures["cacheWrite"], int) or figures["cacheWrite"] is None

    class NoCacheWrites:
        input_tokens, output_tokens, cache_creation_tokens, cache_read_tokens = 10, 5, None, 3

    assert usage_json(NoCacheWrites())["cacheWrite"] is None
    assert context_json(ContextState(used=22_518, window=872_000, model="m")) == {"used": 22_518, "window": 872_000, "model": "m", "compacted": False}
    assert context_json(ContextState(), window_hint=250_000)["window"] == 250_000
