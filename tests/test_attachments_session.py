"""Attachments between the composer and the SDK: sending, failing, the transcript, saved sessions and the replay."""

from __future__ import annotations

import asyncio
import base64
from datetime import datetime, timezone
from typing import Any

import pytest
from droid_sdk import (
    Document,
    DocumentBlock,
    DroidError,
    Image,
    ImageBlock,
    InvalidAttachmentError,
    ModelInfo,
    ModelProvider,
    PdfDocumentSource,
    ReasoningEffort,
    TextBlock,
    TextDocumentSource,
    UserMessage,
)

from desktop.core import session_manager as sm
from desktop.core.attachments import (
    DEFAULT_PROMPT,
    IMAGE,
    PDF,
    TEXT,
    Attachment,
    attachments_from_content,
    image_attachment,
    is_system_reminder,
    pdf_attachment,
    text_attachment,
    typed_text,
)
from desktop.core.session_manager import HistorySnapshot, SessionManager, SessionState
from desktop.core.settings import AppSettings
from desktop.core.transcript import ErrorBlock, Transcript, TurnStatus
from desktop.devtools.replay import ReplayFactory
from desktop.devtools.samples import make_pdf
from tests.fakes import FakeFactory, until
from tests.support import (
    NOTES_BLOCK,
    PICTURE_BLOCK,
    QUESTION_BLOCK,
    REMINDER_BLOCK,
    attachment_history,
    complete,
    delta,
    failure,
    fixture_path,
    success,
    tiny_png,
    wire_message,
)


def run(coro):
    return asyncio.run(coro)


def make(**options):
    factory = FakeFactory()
    factory.default_options.update(options)
    return factory, SessionManager(AppSettings(), factory)


def picture(name: str = "shot.png") -> Attachment:
    data = tiny_png()
    return image_attachment(name, data, "image/png")


def pdf(name: str = "invoice.pdf") -> Attachment:
    data = make_pdf(["Invoice 4417"])
    return pdf_attachment(name, data, parsed_text="Invoice 4417")


def notes(name: str = "notes.md") -> Attachment:
    return text_attachment(name, "# 笔记\nremember the milk")


def model(model_id: str, *, no_images: bool | None) -> ModelInfo:
    return ModelInfo(
        id=model_id,
        display_name="Text Only" if no_images else "Seer",
        short_display_name=model_id,
        model_provider=ModelProvider.ANTHROPIC,
        supported_reasoning_efforts=(ReasoningEffort.LOW,),
        default_reasoning_effort=ReasoningEffort.LOW,
        no_image_support=no_images,
    )


async def finish_turn(controller, stream, text: str = "ok") -> None:
    stream.push(delta(text), complete(), success(text))
    stream.end()
    await until(lambda: controller.state is SessionState.READY)


# --------------------------------------------------------------------------- sending


def test_the_stream_gets_the_pictures_and_files_under_the_sdks_option_names(tmp_path):
    async def scenario():
        factory, manager = make()
        controller = await manager.open_new(tmp_path)
        session = factory.sessions[-1]
        assert controller.send_prompt("what do these say?", [picture(), pdf(), notes()])
        await until(lambda: session.active is not None)
        stream = session.active
        assert stream.prompt == "what do these say?"
        assert set(stream.options) == {"include_partial_messages", "images", "files"}
        (image,) = stream.images
        assert isinstance(image, Image) and image.source.media_type == "image/png"
        assert base64.b64decode(image.source.data) == tiny_png()
        pdf_file, text_file = stream.files
        assert isinstance(pdf_file, Document) and isinstance(pdf_file.source, PdfDocumentSource)
        assert base64.b64decode(pdf_file.source.data).startswith(b"%PDF-")
        assert pdf_file.source.name == "invoice.pdf" and pdf_file.source.parsed_data == "Invoice 4417"
        assert isinstance(text_file.source, TextDocumentSource) and text_file.source.name == "notes.md"
        assert "remember the milk" in text_file.source.data
        await finish_turn(controller, stream)

    run(scenario())


def test_a_message_without_attachments_passes_no_attachment_options(tmp_path):
    async def scenario():
        factory, manager = make()
        controller = await manager.open_new(tmp_path)
        session = factory.sessions[-1]
        assert controller.send_prompt("just words")
        await until(lambda: session.active is not None)
        assert set(session.active.options) == {"include_partial_messages"}
        assert session.active.images == () and session.active.files == ()
        await finish_turn(controller, session.active)

    run(scenario())


def test_only_the_kinds_that_are_present_are_passed(tmp_path):
    async def scenario():
        factory, manager = make()
        controller = await manager.open_new(tmp_path)
        session = factory.sessions[-1]
        assert controller.send_prompt("read it", [notes()])
        await until(lambda: session.active is not None)
        assert set(session.active.options) == {"include_partial_messages", "files"}
        await finish_turn(controller, session.active)
        assert controller.send_prompt("look", [picture()])
        await until(lambda: session.active is not None)
        assert set(session.active.options) == {"include_partial_messages", "images"}

    run(scenario())


def test_attachments_alone_send_the_default_prompt(tmp_path):
    async def scenario():
        factory, manager = make()
        controller = await manager.open_new(tmp_path)
        session = factory.sessions[-1]
        assert controller.send_prompt("   ") is False and session.active is None
        assert controller.send_prompt("  ", [picture()])
        await until(lambda: session.active is not None)
        assert session.active.prompt == DEFAULT_PROMPT == "请查看附件。"
        assert controller.transcript.turns[-1].user_block.text == DEFAULT_PROMPT

    run(scenario())


def test_a_turn_that_is_running_takes_no_second_message_with_attachments(tmp_path):
    async def scenario():
        factory, manager = make()
        controller = await manager.open_new(tmp_path)
        session = factory.sessions[-1]
        assert controller.send_prompt("first")
        await until(lambda: session.active is not None)
        assert controller.send_prompt("second", [picture()]) is False
        assert len(controller.transcript.turns) == 1 and len(session.streams) == 1
        await finish_turn(controller, session.active)

    run(scenario())


def test_the_user_block_keeps_what_was_attached(tmp_path):
    async def scenario():
        factory, manager = make()
        controller = await manager.open_new(tmp_path)
        session = factory.sessions[-1]
        assert controller.send_prompt("look", [picture("a.png"), pdf(), notes()])
        await until(lambda: session.active is not None)
        block = controller.transcript.turns[-1].user_block
        assert [(item.kind, item.name) for item in block.attachments] == [
            (IMAGE, "a.png"), (PDF, "invoice.pdf"), (TEXT, "notes.md")
        ]
        assert all(item.id for item in block.attachments) and len({item.id for item in block.attachments}) == 3
        kept = controller.transcript.attachment(block.attachments[0].id)
        assert kept is block.attachments[0] and kept.data == tiny_png(), "pictures stay: preview and thumbnail need them"
        slim = controller.transcript.attachment(block.attachments[2].id)
        assert slim.text == notes().text and slim.size == notes().size, "a text file keeps its words: an engine change can hand them over"
        pdf_kept = controller.transcript.attachment(block.attachments[1].id)
        assert pdf_kept.data == b"" and pdf_kept.parsed_text == "Invoice 4417", "a PDF loses its bytes and keeps the text taken out of it"
        assert controller.transcript.attachment("nope") is None
        await finish_turn(controller, session.active)

    run(scenario())


# --------------------------------------------------------------------------- handed over from the other engine


def earlier_conversation() -> tuple[Transcript, list[Attachment]]:
    transcript = Transcript()
    scan = pdf_attachment("scan.pdf", b"%PDF-1.4 scanned pages", parsed_text="")
    turn = transcript.start_turn("look at these", [picture(), scan])
    transcript.append_text(turn, "m1", 0, "Two things.")
    transcript.finish_turn(turn, TurnStatus.DONE)
    return transcript, list(turn.user_block.attachments)


def test_the_pictures_and_scans_handed_over_go_with_the_first_message_once_it_succeeds(tmp_path):
    from desktop.core.handoff import build_handoff

    async def scenario():
        factory, manager = make()
        transcript, chosen = earlier_conversation()
        handoff = build_handoff(transcript.turns, carry=chosen)
        assert (handoff.pictures, handoff.pdfs) == (1, 1)
        controller = await manager.open_new(tmp_path, transcript=transcript, handoff=handoff)
        session = factory.sessions[-1]

        mine = picture("mine.png")
        assert controller.send_prompt("and now?", [mine])
        await until(lambda: session.active is not None)
        stream = session.active
        assert stream.prompt == handoff.prefix + "and now?"
        assert len(stream.images) == 2, "the picture handed over, then the one attached now"
        (scan_file,) = stream.files
        assert isinstance(scan_file.source, PdfDocumentSource) and scan_file.source.name == "scan.pdf"
        assert base64.b64decode(scan_file.source.data) == b"%PDF-1.4 scanned pages"
        block = controller.transcript.turns[-1].user_block
        assert [a.name for a in block.attachments] == ["mine.png"], "the page shows only what was attached now"
        await finish_turn(controller, stream)
        assert controller.handoff is None

        assert controller.send_prompt("again")
        await until(lambda: len(session.streams) == 2 and session.active is not None)
        assert session.active.images == () and session.active.files == () and session.active.prompt == "again"
        await finish_turn(controller, session.active)

    run(scenario())


def test_the_pictures_handed_over_are_still_there_when_the_first_turn_fails(tmp_path):
    from desktop.core.handoff import build_handoff

    async def scenario():
        factory, manager = make()
        transcript, chosen = earlier_conversation()
        handoff = build_handoff(transcript.turns, carry=chosen)
        controller = await manager.open_new(tmp_path, transcript=transcript, handoff=handoff)
        session = factory.sessions[-1]
        assert controller.send_prompt("and now?")
        await until(lambda: session.active is not None)
        session.active.push(failure("overloaded"))
        session.active.end()
        await until(lambda: controller.state is SessionState.READY)
        assert controller.handoff is handoff, "a failed turn keeps the hand-over for the retry"

        assert controller.send_prompt("and now?")
        await until(lambda: len(session.streams) == 2 and session.active is not None)
        assert len(session.active.images) == 1 and len(session.active.files) == 1
        await finish_turn(controller, session.active)
        assert controller.handoff is None

    run(scenario())


# --------------------------------------------------------------------------- failing


def test_an_attachment_the_sdk_refuses_fails_the_turn_with_a_readable_error(tmp_path):
    async def scenario():
        factory, manager = make()
        controller = await manager.open_new(tmp_path)
        session = factory.sessions[-1]
        broken = Attachment(kind=IMAGE, name="broken.png", data=b"this is not a picture", media_type="image/png", size=21)
        assert controller.send_prompt("what is this?", [broken])
        await until(lambda: controller.state is SessionState.READY)
        turn = controller.transcript.turns[-1]
        assert turn.status is TurnStatus.FAILED
        (error,) = [block for block in turn.blocks if isinstance(block, ErrorBlock)]
        assert error.message == "附件无法发送：图片数据无效，或格式不受支持。"
        assert session.streams == [], "nothing reached Droid"
        assert turn.user_block.attachments[0].name == "broken.png", "the message stays visible so the user sees what failed"
        assert controller.send_prompt("and now?"), "the session is usable afterwards"

    run(scenario())


def test_a_stream_that_raises_invalid_attachment_error_fails_the_turn(tmp_path):
    async def scenario():
        factory, manager = make()
        controller = await manager.open_new(tmp_path)
        session = factory.sessions[-1]

        def refuse(prompt: str, **options: Any):
            raise InvalidAttachmentError("PDF attachment exceeds the 3145728-byte limit")

        session.stream = refuse
        assert controller.send_prompt("read", [pdf()])
        await until(lambda: controller.state is SessionState.READY)
        turn = controller.transcript.turns[-1]
        assert turn.status is TurnStatus.FAILED
        assert [block.message for block in turn.blocks if isinstance(block, ErrorBlock)] == [
            "附件无法发送：PDF 超过 3 MB 的上限。"
        ]

    run(scenario())


def test_a_model_that_rejects_the_pictures_is_an_error_in_the_turn(tmp_path):
    async def scenario():
        factory, manager = make()
        controller = await manager.open_new(tmp_path)
        session = factory.sessions[-1]
        assert controller.send_prompt("look", [picture()])
        await until(lambda: session.active is not None)
        session.active.push(failure("The model does not accept image input"))
        session.active.end()
        await until(lambda: controller.state is SessionState.READY)
        turn = controller.transcript.turns[-1]
        assert turn.status is TurnStatus.FAILED
        assert [block.message for block in turn.blocks if isinstance(block, ErrorBlock)] == [
            "The model does not accept image input"
        ]

    run(scenario())


# The messages are the ones droid-sdk 0.5.0 raises (checked against the installed package).
@pytest.mark.parametrize(
    ("message", "reason"),
    [
        ("Unsupported or invalid image data", "图片数据无效，或格式不受支持"),
        ("Image data is image/png, not image/bmp", "图片数据无效，或格式不受支持"),
        ("Attachment exceeds the 5242880-byte limit", "超过 5 MB 的上限"),
        ("PDF attachment exceeds the 3145728-byte limit", "PDF 超过 3 MB 的上限"),
        ("Image data is not valid base64", "数据已损坏"),
        ("PDF data is not valid base64", "数据已损坏"),
        ("Document data is not a valid PDF", "不是有效的 PDF"),
        ("Document bytes are not a valid PDF", "不是有效的 PDF"),
    ],
)
def test_invalid_attachment_errors_read_as_a_reason_in_chinese(message, reason):
    assert sm.friendly_failure(InvalidAttachmentError(message)) == f"附件无法发送：{reason}。"


def test_an_unknown_attachment_error_keeps_its_own_words():
    text = sm.friendly_failure(InvalidAttachmentError("Something new went wrong"))
    assert text.startswith("附件无法发送：") and "Something new went wrong" in text
    assert sm.friendly_failure(DroidError("plain")).startswith("DroidError")


def test_the_real_sdk_errors_are_the_ones_the_messages_above_come_from():
    with pytest.raises(InvalidAttachmentError) as raised:
        Image.from_bytes(b"not a picture", media_type="image/png")
    assert sm.friendly_failure(raised.value) == "附件无法发送：图片数据无效，或格式不受支持。"
    with pytest.raises(InvalidAttachmentError) as raised:
        Document.from_bytes(b"not a pdf")
    assert sm.friendly_failure(raised.value) == "附件无法发送：不是有效的 PDF。"


# --------------------------------------------------------------------------- models without pictures


def test_pictures_are_refused_up_front_for_a_model_known_to_lack_image_input(tmp_path):
    async def scenario():
        factory, manager = make(model="text-only")
        factory.models = [model("text-only", no_images=True), model("seer", no_images=False)]
        await manager.list_models(tmp_path)
        controller = await manager.open_new(tmp_path)
        problem = controller.attachment_problem([picture()])
        assert problem and "「Text Only」" in problem and "不支持图片输入" in problem
        assert controller.attachment_problem([picture(), notes()]) == problem
        assert controller.attachment_problem([notes(), pdf()]) is None, "files go as text: only pictures need the model's eyes"
        assert controller.attachment_problem([]) is None

    run(scenario())


@pytest.mark.parametrize("known", [False, None])
def test_pictures_are_not_refused_when_the_model_may_see_them_or_is_unknown(tmp_path, known):
    async def scenario():
        factory, manager = make(model="seer")
        factory.models = [model("seer", no_images=known)]
        await manager.list_models(tmp_path)
        controller = await manager.open_new(tmp_path)
        assert controller.attachment_problem([picture()]) is None
        factory.next_session = {"model": "a-model-nobody-listed"}
        stranger = await manager.open_new(tmp_path / "other")
        assert stranger.model == "a-model-nobody-listed" and stranger.attachment_problem([picture()]) is None

    run(scenario())


def test_the_check_needs_no_model_list_yet(tmp_path):
    async def scenario():
        _, manager = make()
        controller = await manager.open_new(tmp_path)
        assert manager.model_info(controller.model) is None
        assert controller.attachment_problem([picture()]) is None

    run(scenario())


# --------------------------------------------------------------------------- saved sessions

HISTORY_PNG = tiny_png(60, 40)


def test_saved_user_messages_bring_their_pictures_and_documents_back():
    transcript = Transcript()
    assert transcript.load_history(attachment_history()) == 2
    first, second = transcript.turns
    block = first.user_block
    assert block.text == "what is in these?"
    assert [(item.kind, item.name) for item in block.attachments] == [
        (IMAGE, "图片 1"), (PDF, "invoice.pdf"), (TEXT, "notes.md")
    ]
    picture_, pdf_, text_ = block.attachments
    assert picture_.data == HISTORY_PNG and picture_.media_type == "image/png" and picture_.size == len(HISTORY_PNG)
    assert pdf_.size is None and pdf_.meta == "", "a saved PDF has no size to show, only its name"
    assert pdf_.path.endswith("invoice.pdf") and pdf_.badge == "PDF"
    assert text_.size == len("# 笔记\nbuy milk".encode()) and text_.badge == "MD"
    assert all(item.id and transcript.attachment(item.id) is item for item in block.attachments)


def test_a_saved_message_that_is_only_a_picture_is_a_turn_of_its_own():
    transcript = Transcript()
    transcript.load_history(attachment_history())
    second = transcript.turns[1]
    assert second.user_block.text == "" and [item.kind for item in second.user_block.attachments] == [IMAGE]
    assert second.from_history and second.status is TurnStatus.DONE


def test_the_reminder_droid_saves_next_to_a_picture_is_not_what_the_user_typed():
    """A live session showed `<system-reminder>Attached image local file paths: ...` as the start of the message."""
    transcript = Transcript()
    transcript.load_history(attachment_history())
    first, second = transcript.turns
    assert first.user_block.text == "what is in these?"
    assert second.user_block.text == ""
    assert "system-reminder" not in repr([turn.user_block for turn in transcript.turns])


def test_a_saved_session_is_titled_by_what_the_user_typed_not_by_the_reminder(tmp_path):
    async def scenario():
        factory, manager = make()
        factory.history["saved-1"] = HistorySnapshot(messages=attachment_history(), title=None)
        controller = await manager.open_saved(tmp_path, "saved-1")
        assert controller.title == "what is in these?"

    run(scenario())


def test_a_message_that_is_only_reminders_is_not_a_turn():
    messages = [
        wire_message("u1", "user", REMINDER_BLOCK),
        wire_message("u2", "user", QUESTION_BLOCK),
        wire_message("a2", "assistant", {"type": "text", "text": "Hi."}),
    ]
    transcript = Transcript()
    assert transcript.load_history(messages) == 1
    assert transcript.turns[0].user_block.text == "what is in these?"


def test_the_live_echo_of_a_message_with_a_reminder_keeps_the_typed_text():
    transcript = Transcript()
    turn = transcript.start_turn("look at this")
    reminder = TextBlock(text=REMINDER_BLOCK["text"])
    image = ImageBlock(source=Image.from_bytes(tiny_png(), media_type="image/png").source)
    now = datetime.now(timezone.utc)
    transcript.apply(
        UserMessage(
            id="echo-1",
            content=(image, reminder, TextBlock(text="look at this")),
            text=reminder.text + "look at this",
            created_at=now,
            updated_at=now,
        )
    )
    assert turn.user_block.text == "look at this" and turn.user_block.message_id == "echo-1"
    assert [item.kind for item in turn.user_block.attachments] == [IMAGE]


def test_typed_text_drops_whole_reminder_blocks_and_nothing_else():
    reminder, words = REMINDER_BLOCK["text"], "keep <system-reminder> as a word"
    blocks = lambda *texts: [TextBlock(text=text) for text in texts]  # noqa: E731
    assert typed_text(blocks(reminder, "hello")) == "hello"
    assert typed_text(blocks("hello ", "world")) == "hello world", "typed text is joined as it always was"
    assert typed_text(blocks(words)) == words, "only a block that is a reminder from start to end is dropped"
    assert typed_text(blocks(reminder)) == ""
    assert typed_text([], fallback="the event's own text") == "the event's own text"
    assert typed_text([ImageBlock(source=Image.from_bytes(tiny_png(), media_type="image/png").source)], "x") == "x"
    assert is_system_reminder("  <system-reminder>a</system-reminder>\n") and not is_system_reminder("a <system-reminder>")


def test_pictures_returned_by_a_tool_are_not_the_users_attachments():
    messages = [
        wire_message("u1", "user", {"type": "text", "text": "screenshot the page"}),
        wire_message("a1", "assistant", {"type": "tool_use", "id": "t1", "name": "Screenshot", "input": {}}),
        wire_message("u2", "user", {"type": "tool_result", "toolUseId": "t1", "content": [PICTURE_BLOCK]}, PICTURE_BLOCK),
        wire_message("a2", "assistant", {"type": "text", "text": "Done."}),
    ]
    transcript = Transcript()
    assert transcript.load_history(messages) == 1
    assert transcript.turns[0].user_block.attachments == ()


def test_wire_blocks_and_live_sdk_blocks_read_the_same():
    wire_content = wire_message("u1", "user", QUESTION_BLOCK, PICTURE_BLOCK, NOTES_BLOCK).content
    live = Image.from_bytes(HISTORY_PNG, media_type="image/png"), Document.from_text("# 笔记\nbuy milk", name="notes.md")
    live_content = (ImageBlock(source=live[0].source), DocumentBlock(source=live[1].source))
    from_wire = [(item.kind, item.data, item.text) for item in attachments_from_content(wire_content)]
    from_live = [(item.kind, item.data, item.text) for item in attachments_from_content(live_content)]
    assert from_wire == from_live == [(IMAGE, HISTORY_PNG, ""), (TEXT, b"", "# 笔记\nbuy milk")]


def test_an_image_block_without_data_is_a_chip_not_a_picture():
    block = {"type": "image", "source": {"type": "base64", "data": "", "mediaType": "image/png"}}
    (item,) = attachments_from_content(wire_message("u1", "user", QUESTION_BLOCK, block).content)
    assert item.kind == IMAGE and item.data == b"" and item.size is None


# --------------------------------------------------------------------------- the replay


def test_the_replay_session_accepts_pictures_and_files_and_echoes_them(tmp_path):
    async def scenario():
        factory = ReplayFactory(fixture_path("stream_turn.jsonl"), speed=1000.0, max_delay=0.01)
        manager = SessionManager(AppSettings(), factory)
        controller = await manager.open_saved(tmp_path, "saved-1")
        session = factory.sessions[-1]
        assert controller.send_prompt("what is this?", [picture(), notes()])
        await until(lambda: controller.state is SessionState.READY, timeout=5)
        ((prompt, images, files),) = session.sent
        assert prompt == "what is this?" and len(images) == 1 and len(files) == 1
        assert isinstance(images[0], Image) and isinstance(files[0], Document)
        turn = controller.transcript.turns[-1]
        assert turn.status is TurnStatus.DONE
        assert [item.name for item in turn.user_block.attachments] == ["shot.png", "notes.md"]

    run(scenario())


def test_the_canned_replay_turn_echoes_the_blocks_it_was_given(tmp_path):
    factory = ReplayFactory(fixture_path("stream_turn.jsonl"))
    session = factory.resume("saved-1", interactions=None)
    image = Image.from_bytes(tiny_png(), media_type="image/png")
    document = Document.from_text("hello", name="notes.md")
    events = session.canned_turn("look", [image], [document])
    (echo,) = [event for event in events if isinstance(event, UserMessage)]
    assert [type(block).__name__ for block in echo.content] == ["TextBlock", "ImageBlock", "DocumentBlock"]
    assert [(item.kind, item.name) for item in attachments_from_content(echo.content)] == [
        (IMAGE, "图片 1"), (TEXT, "notes.md")
    ]
    assert session.canned_turn("plain")[1].content[0].text == "plain"


def test_a_replayed_echo_does_not_replace_what_the_user_attached(tmp_path):
    transcript = Transcript()
    turn = transcript.start_turn("look", [picture("mine.png")])
    session = ReplayFactory(fixture_path("stream_turn.jsonl")).resume("saved-1", interactions=None)
    image = Image.from_bytes(tiny_png(), media_type="image/png")
    for event in session.canned_turn("look", [image], []):
        transcript.apply(event)
    assert [item.name for item in turn.user_block.attachments] == ["mine.png"]
    assert turn.user_block.message_id is not None


def test_an_echo_gives_a_turn_without_attachments_the_ones_droid_reports():
    transcript = Transcript()
    turn = transcript.start_turn("look")
    image = Image.from_bytes(tiny_png(), media_type="image/png")
    session = ReplayFactory(fixture_path("stream_turn.jsonl")).resume("saved-1", interactions=None)
    for event in session.canned_turn("look", [image], []):
        transcript.apply(event)
    assert [item.kind for item in turn.user_block.attachments] == [IMAGE]
