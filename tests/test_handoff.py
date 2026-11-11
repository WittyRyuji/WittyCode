"""What an engine switch hands over (``core/handoff.py``): the text of the conversation, newest first, within a budget,
and the attachments the user picked, one by one."""

from __future__ import annotations

import json

import pytest

from desktop.core.attachments import SOFT_ATTACHMENTS, SOFT_TOTAL_BYTES, Attachment, Overage, image_attachment, pdf_attachment, text_attachment
from desktop.core.codex.items import split_handoff
from desktop.core.handoff import (
    AS_IMAGE,
    AS_PDF,
    AS_TEXT,
    CEILING_BYTES,
    MAX_BYTES,
    MAX_MESSAGE_BYTES,
    budget_for_window,
    build_handoff,
    carry_options,
    carry_overage,
    clip_message,
)
from desktop.core.transcript import Transcript, TurnStatus

ENTRIES_MARK = "Earlier conversation (JSON):\n"
REQUEST_MARK = "\n\nCurrent request:\n"
PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 24


def conversation(*pairs: tuple[str, str]) -> Transcript:
    transcript = Transcript()
    for question, answer in pairs:
        turn = transcript.start_turn(question)
        transcript.append_text(turn, question, 0, answer)
        transcript.finish_turn(turn, TurnStatus.DONE)
    return transcript


def entries(prefix: str) -> list[dict[str, str]]:
    body = prefix.split(ENTRIES_MARK, 1)[1].rsplit(REQUEST_MARK, 1)[0]
    return json.loads(body)


def test_a_short_conversation_goes_along_whole():
    handoff = build_handoff(conversation(("one?", "1"), ("two?", "2")).turns)
    assert (handoff.carried, handoff.total, handoff.truncated, handoff.clipped) == (4, 4, False, 0)
    assert [e["content"] for e in entries(handoff.prefix)] == ["one?", "1", "two?", "2"]
    assert "bounded excerpt" not in handoff.prefix


def test_a_conversation_without_text_hands_over_nothing():
    assert build_handoff(Transcript().turns).empty


def test_the_newest_message_goes_along_even_when_it_alone_is_bigger_than_the_whole_budget():
    """It used to end the loop at once: nothing at all was handed over, and nothing said so."""
    answer = "A" * (MAX_BYTES + 6000)
    handoff = build_handoff(conversation(("early question", "early answer"), ("the question", answer)).turns)
    assert not handoff.empty and handoff.clipped == 1 and not handoff.truncated
    got = entries(handoff.prefix)
    assert [e["role"] for e in got] == ["user", "assistant", "user", "assistant"], "the earlier messages went along too"
    assert got[3]["content"].startswith("A" * 1000)
    assert "WittyCode cut the rest of this message" in got[3]["content"]
    assert f"of {len(answer)} characters are not shown" in got[3]["content"]
    assert "long messages were cut" in handoff.prefix
    assert handoff.size <= MAX_BYTES


def test_a_message_that_fits_in_its_share_is_left_alone():
    answer = "B" * (MAX_MESSAGE_BYTES - 40)
    handoff = build_handoff(conversation(("q", answer)).turns)
    assert handoff.clipped == 0 and entries(handoff.prefix)[1]["content"] == answer


def test_one_huge_pasted_document_does_not_push_out_the_conversation_after_it():
    handoff = build_handoff(conversation(("D" * 200_000, "got it"), ("and the summary?", "here")).turns)
    got = entries(handoff.prefix)
    assert [e["content"] for e in got][1:] == ["got it", "and the summary?", "here"]
    assert got[0]["content"].startswith("D") and "cut the rest" in got[0]["content"]
    assert handoff.carried == 4 and handoff.clipped == 1


def test_older_messages_are_left_out_when_the_budget_runs_out_and_the_note_says_so():
    handoff = build_handoff(conversation(*[(f"q{i}", "x" * 20_000) for i in range(6)]).turns)
    assert handoff.truncated and handoff.carried < handoff.total == 12
    got = entries(handoff.prefix)
    assert got[-1]["role"] == "assistant" and got[-2]["content"] == "q5", "the newest are kept"
    assert "older messages were left out" in handoff.prefix and handoff.size <= MAX_BYTES


def test_a_cut_never_splits_a_character_and_the_prefix_stays_valid_json():
    handoff = build_handoff(conversation(("问题", "量化" * 40_000)).turns)  # 3 bytes a character
    answer = entries(handoff.prefix)[1]["content"]
    assert answer.startswith("量化量化") and "cut the rest" in answer
    assert "\ufffd" not in answer


@pytest.mark.parametrize("sizes", [[5], [70_000], [30_000, 30_000, 30_000], [100, 90_000, 100], [40_000] * 8])
def test_what_goes_along_never_exceeds_the_budget_and_always_includes_the_newest_message(sizes):
    pairs = [(f"q{i}", chr(97 + i) * n) for i, n in enumerate(sizes)]
    handoff = build_handoff(conversation(*pairs).turns)
    assert handoff.size <= MAX_BYTES and not handoff.empty
    newest = entries(handoff.prefix)[-1]["content"]
    assert newest.startswith(chr(97 + len(sizes) - 1) * min(sizes[-1], 100))


def test_split_handoff_undoes_a_hand_over_that_cut_a_message():
    handoff = build_handoff(conversation(("q", "Z" * (MAX_BYTES * 2))).turns)
    assert handoff.clipped == 1
    assert split_handoff(handoff.prefix + "typed") == ("typed", (2, True)), "a cut message makes it an excerpt too"


@pytest.mark.parametrize(
    ("window", "budget"),
    [
        (None, MAX_BYTES),
        (0, MAX_BYTES),
        (-5, MAX_BYTES),
        (32_000, MAX_BYTES),
        (196_608, MAX_BYTES),  # 10 % of it is 59 KB: not more than the least a model gets
        (400_000, 120_000),  # 10 % of the window, 3 bytes a token
        (872_000, 261_600),  # Claude 5.5's window: just under the most
        (1_000_000, CEILING_BYTES),
        (10_000_000, CEILING_BYTES),
    ],
)
def test_the_budget_is_a_tenth_of_the_window_between_the_least_and_the_most(window, budget):
    assert budget_for_window(window) == budget


def test_a_bigger_window_carries_a_conversation_the_default_budget_cuts():
    transcript = conversation(*[(f"q{i}", "x" * 30_000) for i in range(5)])
    small = build_handoff(transcript.turns)
    big = build_handoff(transcript.turns, window_tokens=872_000)
    assert small.truncated and small.carried == 4
    assert not big.truncated and big.carried == big.total == 10 and big.size <= CEILING_BYTES
    assert build_handoff(transcript.turns, window_tokens=872_000, max_bytes=MAX_BYTES).truncated, "an explicit budget wins"


# ---------------------------------------------------------------------------------------------- attachments


def attached(transcript: Transcript, text: str, *items: Attachment) -> list[Attachment]:
    """A finished turn with these attachments; returns the transcript's own copies (they have ids)."""
    turn = transcript.start_turn(text, items)
    transcript.append_text(turn, text, 0, "answer to " + (text or "the files"))
    transcript.finish_turn(turn, TurnStatus.DONE)
    return list(turn.user_block.attachments)


def scan(name: str = "scan.pdf", size: int = 800) -> Attachment:
    return pdf_attachment(name, b"%PDF-1.4" + b"\x00" * size, parsed_text="")


def test_every_attachment_is_offered_with_how_it_would_travel():
    transcript = Transcript()
    attached(
        transcript,
        "please check these",
        text_attachment("notes.md", "# Notes\nremember the milk"),
        pdf_attachment("invoice.pdf", b"%PDF-1.4", parsed_text="Invoice 4417"),
        scan(),
        image_attachment("shot.png", PNG, "image/png"),
    )
    options = carry_options(transcript.turns)
    assert [(o.attachment.name, o.mode, o.available) for o in options] == [
        ("notes.md", AS_TEXT, True),
        ("invoice.pdf", AS_TEXT, True),
        ("scan.pdf", AS_PDF, True),
        ("shot.png", AS_IMAGE, True),
    ], "nothing is left out of the list: the picture and the scan are offered like the text files"
    assert all(o.attachment.id for o in options)


def test_what_cannot_go_is_still_listed_with_the_reason():
    transcript = Transcript()
    attached(
        transcript,
        "look",
        image_attachment("shot.png", PNG, "image/png"),
        scan(),
        Attachment(kind="image", name="from-history.png"),  # a saved session that kept no picture
        Attachment(kind="pdf", name="from-history.pdf"),
        Attachment(kind="text", name="from-history.txt"),
        text_attachment("blank.txt", "\n  \n"),
    )
    options = carry_options(transcript.turns, images_problem="「X」不支持图片输入。", scans_problem="Codex 只能读 PDF 里的文字。")
    got = {o.attachment.name: o for o in options}
    assert not got["shot.png"].available and got["shot.png"].reason == "「X」不支持图片输入。"
    assert not got["scan.pdf"].available and got["scan.pdf"].reason == "Codex 只能读 PDF 里的文字。"
    assert "没有保留" in got["from-history.png"].reason and not got["from-history.png"].available
    assert "没有保留" in got["from-history.pdf"].reason and "没有保留" in got["from-history.txt"].reason
    assert "只有空白" in got["blank.txt"].reason and not got["blank.txt"].available
    assert len(options) == 6, "all six are listed, available or not"


def test_nothing_goes_along_unless_it_is_picked():
    transcript = Transcript()
    attached(transcript, "check", text_attachment("notes.md", "remember the milk"), scan(), image_attachment("shot.png", PNG, "image/png"))
    handoff = build_handoff(transcript.turns)
    assert handoff.files == 0 and handoff.attachments == () and "remember the milk" not in handoff.prefix and "Attached" not in handoff.prefix
    assert (handoff.pictures, handoff.pdfs) == (0, 0)


def test_each_attachment_goes_along_by_its_own_pick():
    transcript = Transcript()
    notes, invoice, scanned, shot = attached(
        transcript,
        "please check these",
        text_attachment("notes.md", "# Notes\nremember the milk"),
        pdf_attachment("invoice.pdf", b"%PDF-1.4", parsed_text="Invoice 4417"),
        scan(),
        image_attachment("shot.png", PNG, "image/png"),
    )
    handoff = build_handoff(transcript.turns, carry=[notes, scanned, shot])
    assert handoff.files == 1 and (handoff.pictures, handoff.pdfs) == (1, 1) and handoff.carried == handoff.total == 2
    assert handoff.attachments == (scanned, shot), "the picture and the scan go out as attachments of the first message"
    user = entries(handoff.prefix)[0]["content"]
    assert user.startswith("please check these\n\n[Attached file \"notes.md\"]\n# Notes\nremember the milk")
    assert "Invoice 4417" not in handoff.prefix, "the invoice was not picked"
    assert 'as [Attached file "name"]' in handoff.prefix and "2 earlier attachment(s)" in handoff.prefix

    only_invoice = build_handoff(transcript.turns, carry=[invoice])
    assert only_invoice.files == 1 and only_invoice.attachments == () and '[Attached file "invoice.pdf"]\nInvoice 4417' in entries(only_invoice.prefix)[0]["content"]


def test_a_picked_picture_goes_along_even_when_the_conversation_has_no_text_to_hand_over():
    transcript = Transcript()
    shot = Attachment(kind="image", name="shot.png", data=PNG, media_type="image/png", size=len(PNG))
    turn = transcript.start_turn("", [shot])  # a message of nothing but a picture, no answer yet
    handoff = build_handoff(transcript.turns, carry=list(turn.user_block.attachments))
    assert handoff.carried == 0 and not handoff.empty and handoff.prefix == ""
    assert [a.name for a in handoff.attachments] == ["shot.png"]


def test_a_message_that_is_only_files_goes_along_when_the_files_do():
    transcript = Transcript()
    (spec,) = attached(transcript, "", text_attachment("spec.md", "the specification"))
    assert build_handoff(transcript.turns).carried == 1, "only the answer"
    with_files = build_handoff(transcript.turns, carry=[spec])
    assert with_files.carried == 2 and entries(with_files.prefix)[0] == {"role": "user", "content": '[Attached file "spec.md"]\nthe specification'}


def test_a_huge_file_is_cut_but_the_typed_words_stay_in_front():
    transcript = Transcript()
    (big,) = attached(transcript, "what is in here?", text_attachment("big.txt", "W" * 300_000))
    handoff = build_handoff(transcript.turns, carry=[big])
    user = entries(handoff.prefix)[0]["content"]
    assert user.startswith("what is in here?\n\n[Attached file \"big.txt\"]\nWWW") and "cut the rest" in user
    assert handoff.clipped == 1 and handoff.files == 1 and handoff.size <= MAX_BYTES


def test_files_of_messages_that_were_left_out_are_not_counted():
    transcript = Transcript()
    picked = attached(transcript, "old", text_attachment("old.txt", "o" * 1000))
    for i in range(3):
        picked += attached(transcript, f"q{i}", text_attachment(f"f{i}.txt", "n" * 25_000))
    handoff = build_handoff(transcript.turns, carry=picked)
    assert handoff.truncated and handoff.files < 4
    assert "old.txt" not in handoff.prefix and handoff.files == sum(1 for e in entries(handoff.prefix) if "[Attached file" in e["content"])


def test_the_pictures_and_pdf_files_of_one_message_are_counted_against_what_a_message_usually_holds():
    many = [image_attachment(f"{i}.png", PNG, "image/png") for i in range(SOFT_ATTACHMENTS + 1)]
    held = carry_overage(many)
    assert held.over and held.files == 1 and held.count == SOFT_ATTACHMENTS + 1 and not held.beyond_ceiling, "told, not refused"
    assert not carry_overage(many[:SOFT_ATTACHMENTS]).over
    assert carry_overage(many[:3], other_count=SOFT_ATTACHMENTS - 2).files == 1, "what else the message has counts too"
    big = [pdf_attachment("a.pdf", b"x" * 1000, parsed_text=""), pdf_attachment("b.pdf", b"x" * 1000, parsed_text="")]
    assert carry_overage(big, other_bytes=SOFT_TOTAL_BYTES).bytes == 2000
    assert not carry_overage([text_attachment(f"{i}.txt", "x") for i in range(30)]).over, "words inside the text are not files of the message"


def test_a_file_that_comes_after_the_cut_is_not_counted_as_carried():
    transcript = Transcript()
    first, second = attached(transcript, "two files", text_attachment("first.txt", "A" * 40_000), text_attachment("second.txt", "B" * 40_000))
    handoff = build_handoff(transcript.turns, carry=[first, second])
    user = entries(handoff.prefix)[0]["content"]
    assert handoff.clipped == 1 and "first.txt" in user and "second.txt" not in user
    assert handoff.files == 1, "the second file did not go along, whatever was attached"


def test_files_before_the_cut_count_and_so_does_one_that_is_cut_in_the_middle():
    transcript = Transcript()
    small = attached(transcript, "q1", text_attachment("a.txt", "a" * 1000), text_attachment("b.txt", "b" * 1000))
    assert build_handoff(transcript.turns, carry=small).files == 2
    middle = attached(transcript, "q2", text_attachment("c.txt", "c" * 20_000), text_attachment("d.txt", "d" * 30_000), text_attachment("e.txt", "e" * 30_000))
    handoff = build_handoff(transcript.turns, carry=middle)
    user = entries(handoff.prefix)[-2]["content"]
    assert handoff.clipped == 1 and "d.txt" in user and "e.txt" not in user
    assert handoff.files == 2, "c in full, d with its beginning, e not at all"


def test_the_pick_counts_what_the_message_box_already_holds_and_says_by_how_much_it_is_over():
    many = [image_attachment(f"{i}.png", PNG, "image/png") for i in range(3)]
    held = carry_overage(many, other_count=SOFT_ATTACHMENTS - 2)
    assert (held.count, held.files) == (11, 1) and "多 1 个文件" in held.describe() and "这条消息有 11 个附件" in held.describe()
    assert not carry_overage(many, other_count=SOFT_ATTACHMENTS - 3).over
    big = [pdf_attachment("a.pdf", b"x" * 1000, parsed_text="")]
    assert carry_overage(big, other_bytes=SOFT_TOTAL_BYTES).bytes == 1000
    assert carry_overage([text_attachment("n.txt", "words")], other_count=99) == Overage(0, 0), "nothing of this pick is a file: not its matter"


def test_split_handoff_still_reads_a_hand_over_that_carries_attachments():
    transcript = Transcript()
    notes, shot = attached(transcript, "q", text_attachment("n.txt", "words"), image_attachment("s.png", PNG, "image/png"))
    handoff = build_handoff(transcript.turns, carry=[notes, shot])
    assert split_handoff(handoff.prefix + "typed") == ("typed", (2, False))


def test_clip_message_keeps_the_beginning_and_counts_what_is_missing():
    assert clip_message("short", 100) == ("short", False)
    text, clipped = clip_message("abcdefghij" * 100, 300)
    assert clipped and text.startswith("abcdefghij") and len(text.encode()) <= 300 + 80
    assert f"{1000 - text.index(chr(10) + '[') } of 1000 characters are not shown" in text
