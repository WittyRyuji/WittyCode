"""The whole application: the real React page in the real Qt window, the real application core, WittyCode's own loop on the
loopback ChatGPT Plan fake (and the fake Devin where a second engine is needed).

Everything the user does is done the way a user does it: Qt mouse and key events into the page, the clipboard, the window.
(Offscreen Qt, no model call.)
"""

from __future__ import annotations

import asyncio
import json
import time

import pytest
from PySide6.QtCore import Qt
from PySide6.QtGui import QGuiApplication, QImage

from desktop.core.session_state import SessionState
from tests.devin_support import make_engine as make_devin
from tests.e2e_support import open_rig
from tests.fake_responses import call, say, usage
from tests.fakes import until
from tests.support import tiny_png
from tests.ui_support import release, run, wait_for

pytestmark = pytest.mark.ui


def user_parts(request):
    messages = [item for item in request["input"] if item.get("type") == "message" and item.get("role") == "user"]
    return messages[-1]["content"] if messages else []


def user_text(request) -> str:
    parts = user_parts(request)
    return parts[0]["text"] if parts else ""


class Model:
    """The model behind the own loop, turn by turn: each answer is scripted before the message goes out and waits
    (``("hold", key)``) until the test lets it go, so the page can be looked at while the turn runs."""

    def __init__(self, rig) -> None:
        self.rig = rig
        self.plan = rig.plan
        self.keys: list[str] = []
        self.released = 0

    def next(self, *events, hold: bool = True) -> None:
        """The answer to the next request the model gets (several requests: several calls)."""
        if hold:
            key = f"t{len(self.keys) + 1}-{id(self)}"
            self.keys.append(key)
            self.plan.script([("hold", key), *[e for group in events for e in group]])
        else:
            self.plan.script([e for group in events for e in group])

    def go(self) -> None:
        """Let the oldest held answer through."""
        self.plan.release(self.keys[self.released])
        self.released += 1

    def go_all(self) -> None:
        while self.released < len(self.keys):
            self.go()

    @property
    def requests(self):
        return self.plan.requests

    @property
    def prompts(self) -> list[str]:
        return [user_text(r) for r in self.plan.requests]

    async def asked(self, count: int = 1, timeout: float = 10.0) -> None:
        await until(lambda: len(self.plan.requests) >= count, timeout=timeout)


async def new_conversation(rig, *, tab_count: int = 1, autonomy: str | None = None) -> Model:
    """A conversation opened the way the page does it once a first message is sent (the page opens none before that)."""
    await rig.core.call("session.new", {"projectId": str(rig.project), **({"autonomy": autonomy} if autonomy else {})})
    await rig.wait_for(".tab", tab_count)
    await rig.wait_text(".statusbar .st-left", "就绪")
    return Model(rig)


async def finish(rig, model: Model | None = None) -> None:
    if model is not None:
        model.go_all()
    await rig.finish()


SAVED = [("s-1", "修复点击闪烁", 5), ("s-2", "附件功能设计", 1500)]


def test_it_starts_with_the_saved_sessions_and_nothing_made_up(qapp, loop, tmp_path):
    async def scenario():
        rig = await open_rig(tmp_path, saved=SAVED)
        await rig.wait_for(".srow", 2)
        titles = await rig.texts(".srow .stitle")
        assert titles == ["修复点击闪烁", "附件功能设计"], "newest first"
        assert not await rig.exists(".reviewbar") and not await rig.exists(".review-show"), "no design-review furniture"
        assert await rig.count(".app-icon") == 1 and await rig.text(".titlebar .tb-title") == "WittyCode"
        assert await rig.count(".tab") == 0
        await rig.finish()

    run(loop, scenario())


def test_chinese_and_english_mixed_can_be_typed_and_arrive_unchanged(qapp, loop, tmp_path):
    from tests.e2e_support import _runs

    assert _runs("你好 world 再见") == [(False, "你好"), (True, " world "), (False, "再见")] and _runs("") == []

    async def scenario():
        rig = await open_rig(tmp_path)
        model = await new_conversation(rig)
        model.next(say("好的"))
        await rig.send("你好 world 再见，WittyCode！")
        await model.asked()
        assert model.prompts[-1] == "你好 world 再见，WittyCode！"
        await rig.wait_for(".block.user")
        assert await rig.text(".block.user .body") == "你好 world 再见，WittyCode！"
        model.go()
        await rig.wait_text(".statusbar .st-left", "就绪")
        await finish(rig, model)

    run(loop, scenario())


def devin_prompts(harness) -> list[str]:
    return [request["prompt"][0]["text"] for request in harness.requests("session/prompt")]


async def devin_listed(rig) -> None:
    """Until Devin's models are in the list (listing them opens a scratch session of its own: not at the same time as the test's)."""
    await until(lambda: any(m.get("source") == "devin" for m in rig.core.catalog.models), timeout=20)


def test_you_can_type_and_send_while_the_session_is_still_connecting(qapp, loop, tmp_path):
    async def scenario():
        harness = make_devin(tmp_path, FAKE_DEVIN_OPEN_DELAY="1.5")
        rig = await open_rig(tmp_path, devin_engine=harness.engine)
        try:
            await devin_listed(rig)
            opening = asyncio.ensure_future(rig.core.call("session.new", {"projectId": str(rig.project), "modelId": "devin:swe-2-high"}))
            await rig.wait_for(".tab", 1)
            assert await rig.js("document.querySelector('.input') && !document.querySelector('.input').disabled"), "the box is not locked"
            placeholder = await rig.js("document.querySelector('.input').placeholder")
            assert placeholder.startswith("输入消息…") and "连接" not in placeholder, "no 'connecting' wording in its place"
            await rig.send("你好 hello")
            await rig.wait_for(".queue-strip .qs-row")
            assert "你好 hello" in await rig.text(".qs-row .qs-text"), "the message is on the page as sent, above the box"
            assert await rig.count(".qs-row .qs-icons button") == 4, "and can be seen, changed, taken back and sent"
            assert await rig.js("document.querySelectorAll(\".qs-row .qs-icons button\")[3].disabled"), "sending it now has to wait for the connection"
            assert await rig.js("document.querySelector('.input').value") == "", "the box is empty again"
            await wait_for(lambda: devin_prompts(harness) == ["你好 hello"], 15, "the message to be sent once ready")
            await rig.wait_for(".qs-row", 0)  # the real turn replaces the waiting one ...
            assert await rig.count(".block.user") == 1 and await rig.text(".block.user .body") == "你好 hello"  # ... with the same words
            await rig.wait_text(".statusbar .st-left", "就绪", timeout=15)
            await opening
        finally:
            await rig.finish()

    run(loop, scenario())


def test_a_queued_message_can_be_taken_back_from_the_page(qapp, loop, tmp_path):
    async def scenario():
        harness = make_devin(tmp_path, FAKE_DEVIN_OPEN_DELAY="2.0")
        rig = await open_rig(tmp_path, devin_engine=harness.engine)
        try:
            await devin_listed(rig)
            opening = asyncio.ensure_future(rig.core.call("session.new", {"projectId": str(rig.project), "modelId": "devin:swe-2-high"}))
            await rig.wait_for(".tab", 1)
            await rig.send("changed my mind")
            await rig.wait_for(".qs-row")
            await rig.click(".qs-row .qs-icons button", 2)  # cancel
            await rig.wait_for(".qs-row", 0)
            assert await rig.js("document.querySelector('.input').value") == "changed my mind", "the text is back in the box"
            await rig.wait_text(".statusbar .st-left", "就绪", timeout=15)
            assert devin_prompts(harness) == [], "nothing was sent"
            await opening
        finally:
            await rig.finish()

    run(loop, scenario())


def test_the_window_opens_on_a_message_box_and_a_conversation_exists_only_once_a_message_is_sent(qapp, loop, tmp_path):
    async def scenario():
        rig = await open_rig(tmp_path)
        model = Model(rig)
        try:
            assert await rig.count(".tab") == 0 and not rig.core.order, "nothing is open and nothing was made"
            assert await rig.js("document.querySelector('.input') && !document.querySelector('.input').disabled"), "the box can be typed in at once"
            assert await rig.texts(".controls .chip-k") == ["模式"], "the chips are not labelled in words, except the mode"
            assert await rig.text(".hint .chip-k") == "项目" and await rig.text(".hint .chip-v") == rig.project.name, "the project it starts in is named, under the box"
            assert "Enter 发送" in await rig.js("document.querySelector('.input').placeholder"), "the keys are in the box"
            model.next(say("好"))
            await rig.send("你好，开始吧")
            await rig.wait_for(".tab", 1)
            await model.asked()
            assert len(rig.core.order) == 1 and model.prompts == ["你好，开始吧"]
            assert await rig.js("document.querySelector('.input').value") == "", "the box is empty again"
            assert rig.core.settings.last_project == str(rig.project.resolve()), "the next window starts in the same project"
            model.go()
            await rig.wait_text(".statusbar .st-left", "就绪")
            await rig.press("n", Qt.KeyboardModifier.ControlModifier)
            await rig.wait_for(".emptypage")
            assert await rig.count(".tab.current") == 0 and await rig.count(".tab") == 1, "the conversation stays open, the box is for the next one"
        finally:
            await finish(rig, model)

    run(loop, scenario())


def test_a_file_dropped_on_the_message_box_opens_the_conversation_to_hold_it(qapp, loop, tmp_path):
    async def scenario():
        rig = await open_rig(tmp_path)
        try:
            note = tmp_path / "notes.txt"
            note.write_text("remember the milk", encoding="utf-8")
            await rig.focus_composer()
            await rig.type("看一下这个文件")
            await rig.drop_files([note])
            await rig.wait_for(".tab", 1)
            await rig.wait_for(".att-row .att", 1, timeout=15)
            assert await rig.text(".att-row .fname") == "notes.txt"
            assert len(rig.core.order) == 1
            assert await rig.js("document.querySelector('.input').value") == "看一下这个文件", "what was typed went along into the new conversation"
        finally:
            await rig.finish()

    run(loop, scenario())


def test_a_session_can_be_renamed_and_archived_from_its_menu_and_brought_back(qapp, loop, tmp_path):
    async def scenario():
        rig = await open_rig(tmp_path, saved=SAVED)
        try:
            await rig.wait_for(".srow", 2)
            await rig.click(".srow", 1, right=True)
            await rig.wait_for(".menu-item")
            labels = await rig.texts(".menu-label")
            assert "重命名…" in labels and "归档" in labels
            await rig.click_text(".menu-item", "重命名…")
            await rig.wait_for(".dialog input")
            await rig.js("document.querySelector('.dialog input').select()")
            await rig.type("新的名字")
            await rig.press("Enter")
            await rig.wait_text(".srow .stitle", "新的名字")
            assert rig.core.settings.session_titles == {"cg:s-2": "新的名字"}, "kept in the settings, the engine's own title is not touched"
            await rig.click(".srow", 1, right=True)
            await rig.click_text(".menu-item", "归档")
            await rig.wait_for(".srow", 1)
            await rig.wait_text(".archived .pname", "已归档 · 1")
            assert "cg:s-2" in rig.core.settings.archived_sessions
            await rig.click(".archived .proj-toggle")
            assert await rig.texts(".archived-row .stitle") == ["新的名字"]
            await rig.click(".archived-row .more")
            await rig.wait_for(".srow", 2)
            await rig.wait_for(".archived", 0)
            assert rig.core.settings.archived_sessions == {}
        finally:
            await rig.finish()

    run(loop, scenario())


def test_a_conversation_that_is_open_is_closed_when_it_is_archived(qapp, loop, tmp_path):
    async def scenario():
        rig = await open_rig(tmp_path, saved=SAVED[:1])
        try:
            await rig.wait_for(".srow", 1)
            await rig.click(".srow")
            await rig.wait_for(".tab", 1)
            await rig.wait_text(".statusbar .st-left", "就绪")
            await rig.click(".srow", 0, right=True)
            await rig.click_text(".menu-item", "归档")
            await rig.wait_for(".tab", 0)
            await rig.wait_text(".archived .pname", "已归档 · 1")
            assert await rig.count(".srow") == 0
        finally:
            await rig.finish()

    run(loop, scenario())

def test_a_conversation_from_a_new_session_to_the_answer(qapp, loop, tmp_path):
    async def scenario():
        rig = await open_rig(tmp_path)
        model = await new_conversation(rig)
        model.next(say("Hi there"))
        await rig.send("hello")
        await model.asked()
        assert model.prompts[-1] == "hello"
        await rig.wait_for(".block.user")
        assert await rig.text(".block.user .body") == "hello"
        assert await rig.exists(".send.stop"), "the send button turns into stop while the turn runs"
        model.go()
        await rig.wait_text(".text-block", "Hi there")
        await rig.wait_for(".send.stop", 0)
        await rig.wait_text(".turnfoot", "用时")
        assert (await rig.text(".statusbar .st-left")).strip().endswith("就绪")
        await finish(rig, model)

    run(loop, scenario())


def test_tool_calls_fold_into_one_row_and_open_on_click(qapp, loop, tmp_path):
    from tests.fake_responses import calls

    async def scenario():
        rig = await open_rig(tmp_path)
        (rig.project / "README.md").write_text("# readme\n", encoding="utf-8")
        (rig.project / "a.txt").write_text("alpha\n", encoding="utf-8")
        model = await new_conversation(rig, autonomy="full")
        model.next(calls([("read_file", json.dumps({"file_path": "README.md"}), "c1", "fc_1"), ("read_file", json.dumps({"file_path": "a.txt"}), "c2", "fc_2")]), hold=False)
        model.next(say("Done.", item_id="msg_2"), hold=False)
        await rig.send("look around")
        await rig.wait_for(".block.activity", 1, timeout=15)
        await rig.wait_text(".statusbar .st-left", "就绪", timeout=15)
        assert "2 步" in await rig.text(".block.activity .gtext")
        assert not await rig.exists(".block.activity .gbody"), "folded by default"
        await rig.click(".block.activity .gsum")
        await rig.wait_for(".block.activity .item", 2)
        await rig.click(".block.activity .irow", 1)
        await rig.wait_text(".idetail", "alpha")
        await finish(rig, model)

    run(loop, scenario())


def test_an_approval_is_answered_from_the_card(qapp, loop, tmp_path):
    async def scenario():
        rig = await open_rig(tmp_path)
        model = await new_conversation(rig, autonomy="ask")
        model.next(call("shell", {"command": "New-Item marker.txt"}, call_id="c1"), hold=False)
        model.next(say("ok", item_id="msg_2"), hold=False)
        await rig.send("go")
        await rig.wait_for(".prompt.perm", timeout=15)
        assert await rig.exists(".tab.current .dot-attention, .srow.m-attention"), "the session is marked as waiting for you"
        assert not (rig.project / "marker.txt").exists(), "nothing ran before the answer"
        await rig.click(".prompt.perm .btn.primary")
        await rig.wait_for(".prompt.perm", 0)
        await rig.wait_text(".statusbar .st-left", "就绪", timeout=15)
        assert (rig.project / "marker.txt").exists()
        outputs = [item for item in model.requests[-1]["input"] if item.get("type") == "function_call_output"]
        assert [item["call_id"] for item in outputs] == ["c1"], "the model was given the result"
        await finish(rig, model)

    run(loop, scenario())


def test_escape_stops_the_running_turn(qapp, loop, tmp_path):
    async def scenario():
        rig = await open_rig(tmp_path)
        model = await new_conversation(rig)
        model.next(say("late"))
        await rig.send("long job")
        await model.asked()
        await rig.wait_for(".send.stop")
        await rig.focus_composer()
        await rig.press("Escape")
        await rig.wait_text(".turnfoot", "已停止", timeout=15)
        await finish(rig, model)

    run(loop, scenario())


def test_a_pasted_picture_becomes_an_attachment_and_goes_out_with_the_message(qapp, loop, tmp_path):
    async def scenario():
        rig = await open_rig(tmp_path)
        model = await new_conversation(rig)
        model.next(say("a red box"))
        image = QImage(64, 40, QImage.Format.Format_RGB32)
        image.fill(0xE5484D)
        QGuiApplication.clipboard().setImage(image)
        await rig.focus_composer()
        await rig.press("v", Qt.KeyboardModifier.ControlModifier)
        await rig.wait_for(".att-row .att", 1, timeout=15)
        assert await rig.exists(".att-row .att-thumb"), "the card shows a thumbnail made by Python"
        await rig.type("what is this")
        await rig.press("Enter")
        await model.asked()
        parts = user_parts(model.requests[-1])
        assert [p["type"] for p in parts].count("input_image") == 1 and parts[0]["text"] == "what is this"
        await rig.wait_for(".block.user .att-img", 1)
        await finish(rig, model)

    run(loop, scenario())


def test_the_settings_under_the_message_box_change_the_session_and_the_defaults(qapp, loop, tmp_path):
    async def scenario():
        rig = await open_rig(tmp_path)
        await new_conversation(rig)
        controller = rig.core.order[0].controller
        assert await rig.texts(".controls .chip-k") == ["模式"]
        names = await rig.js("[...document.querySelectorAll('.controls .chip')].map(e => e.getAttribute('aria-label').split('：')[0]).join()")
        assert names == "模型,推理,权限,模式", "the others are named for assistive technology and in the tooltip"
        await rig.click(".controls .chip", 1)  # reasoning
        await rig.wait_for(".menu-item")
        await rig.click_text(".menu-item", "High")
        await until(lambda: controller.live_settings().reasoning == "high")
        assert rig.core.settings.reasoning_effort == "high"
        await rig.wait_text(".controls .chip-v", "High")
        await rig.click(".controls .chip", 3)  # mode
        await rig.click_text(".menu-item", "Plan")
        await until(lambda: controller.mode == "spec")
        await rig.finish()

    run(loop, scenario())


def test_the_conversation_can_move_to_devin_and_back(qapp, loop, tmp_path):
    async def scenario():
        harness = make_devin(tmp_path)
        rig = await open_rig(tmp_path, devin_engine=harness.engine)
        await devin_listed(rig)
        model = await new_conversation(rig)
        model.next(say("first answer"), hold=False)
        await rig.send("first question")
        await rig.wait_text(".text-block", "first answer")
        await wait_for(lambda: rig.core.order[0].controller.state is SessionState.READY, message="ready again")
        await rig.click(".controls .chip", 0)
        await rig.wait_text(".menu-head", "DEVIN")
        await rig.click_text(".menu-item", "SWE-2")
        await rig.wait_for(".block.engine-switch", 1, timeout=15)
        assert await rig.count(".tab") == 1, "the same tab"
        assert "Devin" in await rig.text(".block.engine-switch .sw-main")
        assert rig.core.order[0].controller.handoff.carried == 2
        await rig.wait_text(".hint", "下一条消息会带上")
        await rig.click(".controls .chip", 0)
        await rig.click_text(".menu-item", "GPT-6.1-Sol")
        await rig.wait_for(".block.engine-switch", 2, timeout=15)
        assert rig.core.order[0].engine == "chatgpt", "and back on the own loop"
        await rig.finish()

    run(loop, scenario())

def test_the_title_bar_works_as_a_window_frame(qapp, loop, tmp_path, monkeypatch):
    async def scenario():
        rig = await open_rig(tmp_path)
        window = rig.window
        assert window.windowFlags() & Qt.WindowType.FramelessWindowHint
        moves, resizes = [], []
        monkeypatch.setattr(window.platform, "window_start_move", lambda: moves.append(1))
        monkeypatch.setattr(window.platform, "window_start_resize", lambda edge: resizes.append(edge))
        assert await rig.count(".rz[data-edge]") == 8, "invisible handles along the edges and in the corners"

        await rig.click(".titlebar .tb-drag")
        await wait_for(lambda: moves, message="the press on the title bar to ask the system to move the window")
        await rig.click(".rz-se")
        await wait_for(lambda: resizes == ["se"], message="the press on the corner to start a resize")

        await rig.click(".titlebar .tb-btn", 1)  # maximize
        await wait_for(lambda: window.isMaximized(), message="the window to maximize")
        await rig.wait_for(".titlebar .tb-btn[aria-label='还原']")
        await rig.wait_for(".rz[data-edge]", 0)  # nothing to drag at the edges of a maximized window
        await rig.click(".titlebar .tb-btn", 1)
        await wait_for(lambda: not window.isMaximized(), message="the window to restore")
        await rig.wait_for(".titlebar .tb-btn[aria-label='最大化']")
        await rig.wait_for(".rz[data-edge]", 8)
        await rig.finish()

    run(loop, scenario())


def test_the_app_menu_and_shortcuts(qapp, loop, tmp_path):
    async def scenario():
        rig = await open_rig(tmp_path)
        await rig.click(".titlebar .tb-menu")
        await rig.wait_for(".menu-item")
        labels = await rig.texts(".menu-label")
        assert "设置…" in labels and "新会话" in labels
        await rig.press("Escape")
        await rig.focus_composer()
        await rig.press("n", Qt.KeyboardModifier.ControlModifier)
        await rig.wait_for(".emptypage")
        assert await rig.count(".tab") == 0 and not rig.core.order, "Ctrl+N shows the message box; the conversation is made by the first message"
        await rig.press(",", Qt.KeyboardModifier.ControlModifier)
        await rig.wait_for(".settings")
        await rig.press("Escape")
        await rig.wait_for(".settings", 0)
        await rig.finish()

    run(loop, scenario())


def test_a_saved_session_opens_with_its_history_and_is_resumed(qapp, loop, tmp_path):
    async def scenario():
        rig = await open_rig(tmp_path, saved=[("s-1", "File operations", 7)])
        await rig.wait_for(".srow", 1)
        await rig.click(".srow")
        await rig.wait_for(".tab", 1)
        await rig.wait_for(".block.user")
        assert await rig.text(".block.user .body") == "about File operations", "the conversation as it was saved"
        await rig.wait_text(".text-block", "File operations: done.")
        await rig.wait_text(".statusbar .st-left", "就绪")
        assert await rig.count(".srow") == 1, "the open session replaces its saved row"
        assert await rig.exists(".srow.current, .srow.open")
        assert rig.core.order[0].controller.session_id == "s-1", "the same conversation goes on"
        await rig.finish()

    run(loop, scenario())


def test_the_sidebar_is_ordered_by_the_last_message_not_by_what_was_clicked(qapp, loop, tmp_path):
    async def scenario():
        rig = await open_rig(tmp_path, saved=SAVED)
        model = Model(rig)
        try:
            await rig.wait_for(".srow", 2)
            order = ["修复点击闪烁", "附件功能设计"]
            old_age = (await rig.texts(".srow .ssub"))[1].split(" · ")[0]
            assert "刚刚" not in old_age

            async def where() -> tuple[list[str], list[str]]:
                return await rig.texts(".srow .stitle"), await rig.texts(".srow .ssub")

            await rig.click(".srow", 1)  # the older one: opening it is not a message
            await rig.wait_for(".tab", 1)
            await rig.wait_text(".statusbar .st-left", "就绪")
            await asyncio.sleep(0.3)  # the sidebar is refreshed a moment after a session changes
            titles, subs = await where()
            assert titles == order, "the one that was opened stays where it was"
            assert old_age in subs[1] and "刚刚" not in subs[1] and subs[1].startswith("已打开")

            await rig.click(".srow", 0)
            await rig.wait_for(".tab", 2)
            await rig.wait_text(".statusbar .st-left", "就绪")
            await rig.click(".srow", 1)  # an open row: only the tab changes
            await asyncio.sleep(0.3)
            titles, subs = await where()
            assert titles == order and old_age in subs[1] and "刚刚" not in subs[0], "five minutes old stays five minutes old"

            model.next(say("好"))
            await rig.send("继续 go on")
            await model.asked()

            async def first_is_the_one_talked_to() -> bool:
                return "刚刚" in (await rig.texts(".srow .ssub"))[0]

            await wait_for(first_is_the_one_talked_to, 10, "the session talked to moves to the top")
            titles, subs = await where()
            assert "刚刚" not in subs[1] and titles[1] == "修复点击闪烁", "the one not talked to is now second"
            model.go()
            await rig.wait_text(".statusbar .st-left", "就绪")
        finally:
            await finish(rig, model)

    run(loop, scenario())


def test_a_devin_session_whose_process_died_can_be_reconnected_from_the_page(qapp, loop, tmp_path):
    async def scenario():
        harness = make_devin(tmp_path)
        rig = await open_rig(tmp_path, devin_engine=harness.engine)
        try:
            await devin_listed(rig)
            await rig.core.call("session.new", {"projectId": str(rig.project), "modelId": "devin:swe-2-high"})
            await rig.wait_for(".tab", 1)
            await rig.wait_text(".statusbar .st-left", "就绪", timeout=15)
            await rig.send("[crash] go")
            await rig.wait_for(".block.error .btn", timeout=15)
            await rig.wait_text(".statusbar .st-left", "断开")
            await rig.click(".block.error .btn")
            await rig.wait_text(".statusbar .st-left", "就绪", timeout=15)
            assert rig.core.order[0].controller.state is SessionState.READY
        finally:
            await rig.finish()

    run(loop, scenario())


def test_files_dropped_from_explorer_are_attached(qapp, loop, tmp_path):
    async def scenario():
        rig = await open_rig(tmp_path)
        model = await new_conversation(rig)
        model.next(say("ok"))
        notes = tmp_path / "notes.txt"
        notes.write_text("remember the milk", encoding="utf-8")
        action = await rig.drop_files([notes])
        assert action == Qt.DropAction.CopyAction, "a drop is accepted as a copy: Explorer must not delete the original"
        await rig.wait_for(".att-row .att", 1, timeout=15)
        assert await rig.text(".att-row .fname") == "notes.txt"
        await rig.send("read it")
        await model.asked()
        texts = [p.get("text", "") for p in user_parts(model.requests[-1])]
        assert texts[0] == "read it" and any("notes.txt" in t and "remember the milk" in t for t in texts[1:]) and notes.exists()
        await finish(rig, model)

    run(loop, scenario())


def test_the_copy_buttons_reach_the_system_clipboard(qapp, loop, tmp_path):
    async def scenario():
        rig = await open_rig(tmp_path)
        model = await new_conversation(rig)
        model.next(say("好"), hold=False)
        await rig.send("hello")
        await rig.wait_text(".statusbar .st-left", "就绪")
        sid = rig.core.order[0].controller.session_id
        QGuiApplication.clipboard().setText("before")
        await rig.click(".srow", 0, right=True)
        await rig.wait_for(".menu-item")
        await rig.click_text(".menu-item", "复制会话 ID")
        await wait_for(lambda: QGuiApplication.clipboard().text() == sid, message="the session id on the clipboard")
        await rig.finish()

    run(loop, scenario())


def test_a_project_is_added_through_the_folder_dialog(qapp, loop, tmp_path, monkeypatch):
    async def scenario():
        rig = await open_rig(tmp_path, projects=False)
        folder = tmp_path / "newproject"
        folder.mkdir()

        async def pick(start):
            return str(folder)

        monkeypatch.setattr(rig.window.platform, "pick_folder", pick)
        await rig.click("[aria-label='添加项目文件夹']")
        await rig.wait_for(".proj", 1)
        assert await rig.text(".proj .pname") == "newproject" or "newproject" in " ".join(await rig.texts(".proj"))
        assert str(folder.resolve()) in rig.core.settings.projects
        await rig.finish()

    run(loop, scenario())


def test_the_status_bar_shows_context_and_token_figures(qapp, loop, tmp_path):
    async def scenario():
        rig = await open_rig(tmp_path)
        model = await new_conversation(rig)
        model.next(say("x", used=usage(22_518, 498, cached=20_000)), hold=False)
        await rig.send("hello")
        await rig.wait_text(".statusbar .st-right", "Out")  # "Token —" says nothing yet: wait for the figures
        text = (await rig.texts(".statusbar .st-right"))[0]
        assert "Context" in text and "400K" in text.replace(" ", ""), text
        assert "In" in text and "Out" in text
        await rig.finish()

    run(loop, scenario())


def test_the_theme_chosen_in_the_settings_is_applied_and_remembered(qapp, loop, tmp_path):
    async def scenario():
        rig = await open_rig(tmp_path)
        await rig.press(",", Qt.KeyboardModifier.ControlModifier)
        await rig.wait_for(".settings")
        await rig.click_text(".segc button", "浅色")
        await wait_for(lambda: rig.core.settings.theme == "light", message="the theme to reach the settings")
        assert await rig.js("document.documentElement.dataset.theme") == "light"
        await rig.finish()

    run(loop, scenario())

def steers_of(rig) -> list[str]:
    return [steer.ref.text for steer in rig.core.order[0].controller._steers]


def test_while_a_turn_runs_enter_queues_the_message_and_ctrl_enter_gives_it_to_the_turn(qapp, loop, tmp_path):
    async def scenario():
        rig = await open_rig(tmp_path)
        model = await new_conversation(rig)
        model.next(say("ok"))  # the first turn's answer, held until the test lets it go
        model.next(say("noted", item_id="msg_s"), hold=False)  # the request that carries the steer
        model.next(say("done", item_id="msg_2"))  # the queued message's turn
        await rig.send("long job")
        await model.asked()
        await rig.wait_for(".send.stop")
        hint = await rig.js("document.querySelector('.input').placeholder")
        assert "Enter 排队" in hint and "Ctrl+Enter 引导" in hint and "Esc 停止" in hint, hint
        under = await rig.text(".hint") if await rig.exists(".hint") else ""
        assert "Enter" not in under and "Using ChatGPT Plan" in under, "the keys are in the box; the line under it only says the plan is in use"
        assert await rig.count(".send.how") == 0, "nothing typed: only the stop button"

        # Enter: queued behind the turn, shown as sent, the box empty again
        await rig.focus_composer()
        await rig.type("then do this")
        assert await rig.count(".send.how") == 2, "排队 and 引导 are there once there is something to send"
        await rig.press("Enter")
        await rig.wait_for(".qs-row", 1)
        assert "then do this" in await rig.text(".qs-row .qs-text")
        assert await rig.text(".qs-row .qs-tag") == "排队" and await rig.js("document.querySelector(\".qs-row .qs-tag\").title") == "本轮结束后发出"
        assert await rig.js("document.querySelector('.input').value") == ""
        assert model.prompts == ["long job"] and steers_of(rig) == []

        # Ctrl+Enter: given to the running turn now
        await rig.type("actually, stop at the docs")
        await rig.press("Enter", Qt.KeyboardModifier.ControlModifier)
        await rig.wait_for(".block.user.steer", 1)
        assert "actually, stop at the docs" in await rig.text(".block.user.steer .body")
        assert "引导" in await rig.text(".block.user.steer .who") and await rig.exists(".block.user.steer.pending"), "waiting for the loop to take it in"
        assert steers_of(rig) == ["actually, stop at the docs"]
        assert await rig.count(".qs-row") == 1, "the queued one is still waiting"

        # the loop takes it into its next request: no longer waiting
        model.go()
        await model.asked(2)
        assert model.prompts[1] == "actually, stop at the docs"
        await rig.wait_for(".block.user.steer.pending", 0)

        # the turn ends: the queued message goes out by itself
        await model.asked(3)
        assert model.prompts[2] == "then do this"
        await rig.wait_for(".qs-row", 0)
        model.go()
        await rig.wait_text(".statusbar .st-left", "就绪")
        await finish(rig, model)

    run(loop, scenario())


def test_a_waiting_message_can_be_taken_back_and_the_setting_swaps_the_two_keys(qapp, loop, tmp_path):
    async def scenario():
        rig = await open_rig(tmp_path)
        model = await new_conversation(rig)
        model.next(say("x"))
        model.next(say("y", item_id="msg_s"), hold=False)
        model.next(say("z", item_id="msg_2"), hold=False)
        model.next(say("w", item_id="msg_3"), hold=False)
        await rig.send("long job")
        await model.asked()
        await rig.wait_for(".send.stop")
        await rig.focus_composer()
        await rig.type("one")
        await rig.press("Enter")
        await rig.type("two")
        await rig.press("Enter")
        await rig.wait_for(".qs-row", 2)
        assert await rig.texts(".qs-row .qs-tag") == ["排队 1/2", "排队 2/2"]
        await rig.click(".qs-row .qs-icons button", 6)  # the cancel icon of the second line
        await rig.wait_for(".qs-row", 1)
        assert await rig.js("document.querySelector('.input').value") == "two", "the withdrawn message is back in the box"

        # the setting: Enter steers, Ctrl+Enter queues
        await rig.press(",", Qt.KeyboardModifier.ControlModifier)
        await rig.wait_for(".settings")
        await rig.click(".snav", 3)  # 外观 / 账号与模型 / 新会话默认值 / 会话
        await rig.click_text(".segc button", "立即引导")
        await wait_for(lambda: rig.core.settings.follow_up == "steer", message="the setting to reach the core")
        await rig.press("Escape")
        await rig.wait_for(".settings", 0)
        placeholder = await rig.js("document.querySelector('.input').placeholder")
        assert "Enter 引导" in placeholder and "Ctrl+Enter 排队" in placeholder, placeholder
        await rig.focus_composer()
        await rig.press("Enter")  # the box holds "two"
        await rig.wait_for(".block.user.steer", 1)
        assert steers_of(rig) == ["two"]
        await rig.type("three")
        await rig.press("Enter", Qt.KeyboardModifier.ControlModifier)
        await rig.wait_for(".qs-row", 2)
        await finish(rig, model)

    run(loop, scenario())


def test_a_waiting_message_can_be_looked_at_changed_and_sent_now_from_its_line(qapp, loop, tmp_path):
    async def scenario():
        rig = await open_rig(tmp_path)
        model = await new_conversation(rig)
        model.next(say("x"))
        model.next(say("noted", item_id="msg_s"), hold=False)
        model.next(say("y", item_id="msg_2"), hold=False)
        await rig.send("long job")
        await model.asked()
        await rig.wait_for(".send.stop")
        await rig.focus_composer()
        await rig.type("then do this")
        await rig.press("Enter")
        await rig.type("and that")
        await rig.press("Enter")
        await rig.wait_for(".qs-row", 2)
        assert await rig.count(".qs-row .qs-icons button") == 8, "four icons on each line"

        await rig.click(".qs-row .qs-icons button", 0)  # see all of the first
        await rig.wait_for(".qs-row.open", 1)
        await rig.click(".qs-row .qs-icons button", 0)
        await rig.wait_for(".qs-row.open", 0)

        await rig.click(".qs-row .qs-icons button", 5)  # change the second
        await rig.wait_for(".qs-row textarea.qs-edit", 1)
        assert await rig.js("document.querySelector('.qs-edit').value") == "and that"
        await rig.js("(() => { const t = document.querySelector('.qs-edit'); t.setSelectionRange(t.value.length, t.value.length); return 1; })()")
        await rig.type(" please")
        await rig.press("Enter")
        await rig.wait_for(".qs-row textarea", 0)
        await wait_for(lambda: [m.text for m in rig.core.order[0].queue] == ["then do this", "and that please"], 10, "the new words to reach the queue")
        assert await rig.texts(".qs-row .qs-text") == ["then do this", "and that please"]

        await rig.click(".qs-row .qs-icons button", 7)  # send the second one now, to the turn
        await rig.wait_for(".qs-row", 1)
        await rig.wait_for(".block.user.steer", 1)
        assert "and that please" in await rig.text(".block.user.steer .body")
        assert steers_of(rig) == ["and that please"]
        assert await rig.text(".qs-row .qs-text") == "then do this" and await rig.text(".qs-row .qs-tag") == "排队"

        model.go()
        await model.asked(3)
        assert model.prompts[1:] == ["and that please", "then do this"], "the steer went with the turn, the one left in line after it"
        await rig.wait_text(".statusbar .st-left", "就绪")
        await finish(rig, model)

    run(loop, scenario())


def test_a_pdf_and_a_picture_the_answer_names_are_drawn_in_the_side_panel(qapp, loop, tmp_path):
    """The real window: Python draws the pages (QtPdf), the real page shows them as images and asks for the ones it scrolls to."""

    async def scenario():
        from tests.test_pdfview import write_pdf

        rig = await open_rig(tmp_path)
        (rig.project / "docs").mkdir(parents=True, exist_ok=True)
        write_pdf(rig.project / "docs" / "r.pdf", 3)
        picture = QImage(64, 48, QImage.Format.Format_RGB32)
        picture.fill(0xFFCC3333)
        assert picture.save(str(rig.project / "shot.png"), "PNG")
        model = await new_conversation(rig)
        model.next(say("报告在 `docs/r.pdf`，截图是 `shot.png`。"), hold=False)
        await rig.send("show me")
        await rig.wait_for(".text-block code.pathref", 2)

        await rig.click(".text-block code.pathref", 0)
        await rig.wait_for(".filepanel .fp-sheet", 3)
        assert await rig.text(".fp-name") == "r.pdf" and not await rig.exists(".fp-rows"), "a PDF is pages, not text"
        assert "3 页" in await rig.text(".fp-facts")
        await wait_for_js(rig, "document.querySelectorAll('.fp-sheet img').length >= 1", "the first page to be drawn")
        await wait_for_js(rig, "[...document.querySelectorAll('.fp-sheet img')].every((i) => i.complete && i.naturalWidth > 0)", "the page images to load")
        first = json.loads(await rig.js("JSON.stringify((() => { const i = document.querySelector('.fp-sheet img'); return [i.naturalWidth, i.naturalHeight]; })())"))
        assert first[0] >= 700 and abs(first[1] / first[0] - 842 / 595) < 0.02, f"an A4 page drawn at the panel's width: {first}"
        sheet = json.loads(await rig.js("JSON.stringify((() => { const r = document.querySelector('.fp-sheet').getBoundingClientRect(); return [r.width, r.height]; })())"))
        assert abs(sheet[1] / sheet[0] - 842 / 595) < 0.02, "the sheet has the page's shape"
        await rig.js("document.querySelector('.fp-pages').scrollTop = document.querySelector('.fp-pages').scrollHeight")
        await wait_for_js(rig, "document.querySelector('.fp-pagepill')?.textContent.trim() === '3 / 3'", "the page number to follow the scroll")
        await wait_for_js(rig, "document.querySelectorAll('.fp-sheet img').length === 3", "the last page to be drawn")

        await rig.click(".text-block code.pathref", 1)
        await rig.wait_for(".filepanel .fp-picture img", 1)
        await wait_for_js(rig, "(() => { const i = document.querySelector('.fp-picture img'); return i.complete && i.naturalWidth === 64 && i.naturalHeight === 48; })()", "the picture to load")
        assert not await rig.exists(".fp-sheet")
        await rig.finish()

    async def wait_for_js(rig, expression: str, message: str) -> None:
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            if await rig.js(expression):
                return
            await asyncio.sleep(0.05)
        raise AssertionError(f"timed out waiting for {message}")

    run(loop, scenario())


def test_a_file_the_answer_names_opens_in_the_side_panel_with_the_default_program_or_by_the_menu(qapp, loop, tmp_path):
    async def scenario():
        rig = await open_rig(tmp_path)
        (rig.project / "docs").mkdir(parents=True, exist_ok=True)
        (rig.project / "docs" / "a.md").write_text("# 标题\n\n第二段 `src/m.py`\n", encoding="utf-8")
        (rig.project / "src").mkdir(exist_ok=True)
        (rig.project / "src" / "m.py").write_text("\n".join(f"x{i} = {i}" for i in range(1, 401)) + "\n", encoding="utf-8")
        (rig.project / "run.bat").write_text("echo hi\n", encoding="utf-8")
        default, chosen, shown = [], [], []
        platform = rig.core.platform  # (the window's own: nothing of the test may reach the desktop)
        platform.open_path = default.append
        platform.open_with = chosen.append
        platform.show_in_folder = shown.append
        model = await new_conversation(rig)
        model.next(say("改了 `docs/a.md` 和 `src/m.py:300-302`，别运行 `run.bat`；`self.state` 只是代码，`docs/none.md` 还没有。"), hold=False)
        await rig.send("what did you change")
        await rig.wait_for(".text-block code.pathref", 4)
        assert await rig.texts(".text-block code.pathref") == ["docs/a.md", "src/m.py:300-302", "run.bat", "docs/none.md"], "`self.state` stays plain code"
        assert not await rig.exists(".filepanel")

        await rig.click(".text-block code.pathref", 0)  # a click: the side panel, a Markdown file as a document
        await rig.wait_for(".filepanel .fp-doc h1", 1)
        assert await rig.text(".fp-name") == "a.md" and await rig.text(".fp-doc h1") == "标题"
        assert default == [] and chosen == [] and shown == []
        assert await rig.exists(".fp-doc code.pathref"), "a path inside the document is a link too"
        assert await rig.js("(() => { const p = document.querySelector('.filepanel').getBoundingClientRect(), m = document.querySelector('.main').getBoundingClientRect(); return p.left >= m.right - 1 && m.width > 300; })()")

        await rig.click(".text-block code.pathref", 1)  # with lines: the source, the lines marked and scrolled to
        await rig.wait_text(".fp-name", "m.py")
        await rig.wait_for(".filepanel .fp-rows", 1)
        assert await rig.js("document.querySelector('.fp-mark').style.top") == f"{8 + 299 * 20}px"
        assert await rig.js("document.querySelector('.fp-mark').style.height") == "60px"
        await wait_for_scroll(rig)
        assert "x300 = 300" in await rig.text(".fp-text")

        await rig.click(".text-block code.pathref", 0, ctrl=True)  # Ctrl+click: the default program, the panel stays
        await wait_for(lambda: default == [str(rig.project / "docs" / "a.md")], 10, "the file to go to the default program")
        assert await rig.text(".fp-name") == "m.py"

        await rig.click(".text-block code.pathref", 2, ctrl=True)  # a script is shown in its folder, never run
        await wait_for(lambda: shown == [str(rig.project / "run.bat")], 10, "the script to be shown in its folder")
        assert len(default) == 1, "a script never goes to the default program"
        await rig.wait_text(".notice-text", "不会直接运行")

        await rig.click(".text-block code.pathref", 0, right=True)  # right click: the ways to open it
        await rig.wait_for(".menu-item", 5)
        assert [t.replace("Ctrl+点击", "").strip() for t in await rig.texts(".menu-item")] == ["在右侧打开", "用默认程序打开", "选择打开方式…", "在文件夹中显示", "复制路径"]
        await rig.click_text(".menu-item", "选择打开方式")
        await wait_for(lambda: chosen == [str(rig.project / "docs" / "a.md")], 10, "the 'open with' choice for the file")
        await rig.click(".text-block code.pathref", 0, right=True)
        await rig.wait_for(".menu-item", 5)
        await rig.click_text(".menu-item", "复制路径")
        await wait_for(lambda: QGuiApplication.clipboard().text() == str(rig.project / "docs" / "a.md"), message="the path on the clipboard")
        await rig.click(".text-block code.pathref", 0, right=True)
        await rig.wait_for(".menu-item", 5)
        await rig.click_text(".menu-item", "在右侧打开")
        await rig.wait_text(".fp-name", "a.md")

        await rig.click(".text-block code.pathref", 3)  # a file that is not there: a sentence and the other ways
        await rig.wait_for(".filepanel .fp-error", 1)
        assert "找不到" in await rig.text(".fp-error") or "不存在" in await rig.text(".fp-error")
        await rig.press("Escape")
        await rig.wait_for(".filepanel", 0)

        await rig.click(".text-block code.pathref", 0)
        await rig.wait_for(".filepanel", 1)
        await rig.click(".tab-x")  # the panel goes with its conversation
        await rig.wait_for(".tab", 0)
        await rig.wait_for(".filepanel", 0)
        await rig.finish()

    async def wait_for_scroll(rig) -> None:
        await wait_for_js(rig, "document.querySelector('.fp-scroll').scrollTop > 5000", "the panel to scroll to the lines")

    async def wait_for_js(rig, expression: str, message: str) -> None:
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            if await rig.js(expression):
                return
            await asyncio.sleep(0.05)
        raise AssertionError(f"timed out waiting for {message}")

    run(loop, scenario())


def test_closing_the_window_during_a_turn_asks_first(qapp, loop, tmp_path):
    async def scenario():
        rig = await open_rig(tmp_path)
        model = await new_conversation(rig)
        model.next(say("late"))
        await rig.send("long job")
        await model.asked()
        await rig.wait_for(".send.stop")
        controller = rig.core.order[0].controller
        rig.window.close()  # Alt+F4
        await rig.wait_for(".dialog")
        assert "退出" in await rig.text(".dialog")
        await rig.press("Escape")
        await rig.wait_for(".dialog", 0)
        assert not rig.window.closed.is_set() and controller.is_open, "cancelling keeps everything running"
        rig.window.close()
        await rig.wait_for(".dialog")
        await rig.click(".dialog .danger-solid")
        await asyncio.wait_for(rig.window.closed.wait(), 15)
        assert not controller.is_open
        model.go_all()
        await release(rig.window)

    run(loop, scenario())

def test_a_provider_is_added_through_the_settings_editor(qapp, loop, tmp_path):
    from desktop.core.providers import ProviderStore

    async def scenario():
        store = ProviderStore(
            models_file=tmp_path / "app" / "models.json",
            sidecar=tmp_path / "app" / "providers.json",
            environ={},
            registry_env=lambda: {},
        )
        rig = await open_rig(tmp_path, providers=store)
        await rig.press(",", Qt.KeyboardModifier.ControlModifier)
        await rig.wait_for(".settings")
        await rig.click_text(".snav", "账号与模型")
        await rig.wait_for(".providers")
        assert await rig.exists(".prov-empty"), "no providers yet"
        await rig.click_text(".pcard .btn", "添加 Provider")
        await rig.wait_for(".pe")
        await rig.click("input[aria-label='名称']")
        await rig.type("Test GW")
        await rig.click("input[aria-label='Base URL']")
        await rig.type("https://example.com/v1")
        await rig.click_text(".pe .segc button", "不需要")
        await rig.click("input[aria-label='模型 ID']")
        await rig.type("my-model")
        await rig.click_text(".pe-foot .btn", "保存")
        assert "上下文窗口未知" in await rig.text(".stack-err")
        await rig.click("input[aria-label='上下文窗口']")
        await rig.type("64000")
        await rig.click(".mrow:not(.mhead) .selectbox")
        assert "XHigh" in await rig.text(".menu") and "Max" in await rig.text(".menu")
        await rig.click_text(".menu-item", "Max")
        await rig.click_text(".pe-foot .btn", "保存")
        await rig.wait_for(".pe", 0)
        await rig.wait_for(".prov", 1)
        assert await rig.text(".prov .prov-name") == "Test GW"
        saved = json.loads((tmp_path / "app" / "models.json").read_text(encoding="utf-8"))
        assert [(m["model"], m["baseUrl"]) for m in saved["customModels"]] == [("my-model", "https://example.com/v1")]
        assert saved["customModels"][0]["maxContextLimit"] == 64_000
        assert saved["customModels"][0]["reasoningEffort"] == "max"
        await rig.finish()

    run(loop, scenario())


def test_what_an_agent_wrote_to_the_shared_memory_shows_as_a_badge_and_can_be_undone(qapp, loop, tmp_path):
    from desktop.core.memory.launch import MemoryLaunch
    from desktop.core.memory.store import MemoryStore, agent

    async def scenario():
        launch = MemoryLaunch(tmp_path / "memory", python="py.exe")
        rig = await open_rig(tmp_path, memory=launch)
        assert not await rig.exists(".foot-badge"), "nothing written yet: no number on the sidebar's memory button"
        # another process (an agent's memory server) writes to the same folder
        store = MemoryStore(launch.root)
        entry = store.create(title="PowerShell 5.1 has no &&", text="Chain commands with a semicolon.", scope="global", kind="pitfall", by=agent("devin", "run-1"))
        await rig.wait_for(".foot-badge", 1, timeout=10)
        assert await rig.text(".foot-badge") == "1"
        await rig.click(".foot-btn", 0)
        await rig.wait_for(".settings")
        await rig.wait_for(".mem-change", 1)
        assert "Devin" in await rig.text(".mem-change-line") and "记住了" in await rig.text(".mem-change-line")
        assert await rig.text(".snav.on") == "共享记忆1", "the settings tab carries the number too"
        await rig.wait_for(".mem-note", 1)
        assert await rig.texts(".mem-note .mem-title") == ["PowerShell 5.1 has no &&"]

        await rig.click_text(".mem-head .btn", "全部标为已看过")
        await rig.wait_for(".foot-badge", 0)
        assert not await rig.exists(".mem-change.new")

        await rig.click_text(".mem-change .btn", "撤销")
        await rig.wait_for(".mem-note", 0)
        assert store.read(entry.id).status == "forgotten", "the undo reached the store: the note is forgotten, not deleted"
        await rig.finish()

    run(loop, scenario())


def test_the_weekly_review_lists_what_the_application_wrote_and_one_confirmation_settles_it(qapp, loop, tmp_path):
    import sys

    from desktop.core.memory.launch import MemoryLaunch
    from desktop.core.memory.store import APP

    async def scenario():
        launch = MemoryLaunch(tmp_path / "memory", python=sys.executable)
        rig = await open_rig(tmp_path, memory=launch)
        assert not await rig.exists(".foot-badge")
        week_ago = time.time() - 8 * 86400
        scores = [0.2, 0.9, 0.8, 0.7, 0.6, 0.4]
        for n, score in enumerate(scores):
            entry = launch.store.create(title=f"第 {n} 条笔记", text=f"第 {n} 条笔记的内容，足够长，读得出来。", scope="global", kind="fact", by=APP)
            launch.review.add(entry, engine="chatgpt", conversation="测试对话", why="用户明确说了", score=score, llm=score, learned=0.5, now=week_ago)
        rig.core._suggestions_changed()  # (what the curator calls once it has written something)
        await rig.wait_for(".foot-badge", 1, timeout=10)
        assert await rig.text(".foot-badge") == "6", "the week is up: the sidebar's memory button counts the notes waiting for the review"
        await rig.click(".foot-btn", 0)
        await rig.wait_for(".mem-rev", 6)
        assert await rig.texts(".mem-rev .mem-title") == ["第 0 条笔记", "第 5 条笔记", "第 4 条笔记", "第 3 条笔记", "第 2 条笔记", "第 1 条笔记"], "the least sure first"
        assert len(launch.store.entries()) == 6, "the notes are in the memory already, in use"
        assert "把握 20%" in await rig.text(".mem-rev .mem-meta", 0) and "ChatGPT Plan" in await rig.text(".mem-rev .mem-meta", 0)
        assert await rig.js("[...document.querySelectorAll('.mem-rev .switch')].map(e => e.getAttribute('aria-checked')).join()") == "false,false,true,true,true,true"

        await rig.click_text(".mem-rev-foot .btn", "确认")
        await rig.wait_for(".mem-rev", 0)
        assert {e.title for e in launch.store.entries()} == {f"第 {n} 条笔记" for n in (1, 2, 3, 4)}, "the ticked ones stay"
        assert {e.title for e in launch.store.entries(status="forgotten")} == {"第 0 条笔记", "第 5 条笔记"}, "the others are forgotten, not deleted"
        await rig.wait_for(".foot-badge", 0)
        assert launch.learner.labels == 6 and launch.review.count_pending() == 0, "six verdicts taught the filter"
        await rig.finish()

    run(loop, scenario())


def test_importing_from_another_application_previews_what_it_has_before_writing_anything(qapp, loop, tmp_path):
    import sys

    from desktop.core.import_bridge import ImportBridge
    from desktop.core.memory.launch import MemoryLaunch
    from desktop.core.memory.store import MemoryStore
    from tests.test_import_bridge import FakeSource, candidate

    async def scenario():
        launch = MemoryLaunch(tmp_path / "memory", python=sys.executable)
        rig = await open_rig(tmp_path, memory=launch)
        source = FakeSource(
            "claude",
            label="Claude Code",
            memories=[
                candidate("a", "回答用中文", "和我说话用中文；命令和报错原文保持英文。", app="claude"),
                candidate("b", "提交前跑测试", "提交之前必须跑完整的测试。", app="claude"),
            ],
        )
        rig.core.imports = ImportBridge(MemoryStore(launch.root), None, make_sources=lambda: [source])
        await rig.wait_for(".foot-btn", timeout=10)  # (the sidebar is drawn once app.init has answered: a click at once could find nothing)
        await rig.click(".foot-btn", 0)
        await rig.wait_for(".settings")
        await rig.click_text(".mem-head .btn", "从其他应用导入…")
        await rig.wait_for(".imp", 1, timeout=10)
        await rig.wait_for(".imp-item", 2)
        assert await rig.texts(".imp-item .mem-title") == ["回答用中文", "提交前跑测试"]
        assert await rig.exists(".imp-source input:disabled") is False, "the only app is there"
        assert MemoryStore(launch.root).entries() == [], "a preview writes nothing"
        await rig.click_text(".imp-foot .btn.primary", "导入所选")
        await rig.wait_for(".imp-done", 1, timeout=10)
        entries = MemoryStore(launch.root).entries()
        assert [(entry.title, entry.source["engine"]) for entry in entries] == [("提交前跑测试", "claude"), ("回答用中文", "claude")]
        await rig.wait_for(".mem-note", 2, timeout=10)
        await rig.click(".mem-note-head", 0)
        await rig.wait_for(".mem-note.open .mem-note-body", 1)
        assert "Claude Code 写入" in await rig.text(".mem-note.open .mem-note-body"), "the note says where it came from"
        await rig.finish()

    run(loop, scenario())


def test_a_note_is_written_and_the_memory_switched_off_from_the_settings(qapp, loop, tmp_path):
    from desktop.core.memory.launch import MemoryLaunch
    from desktop.core.memory.store import MemoryStore

    async def scenario():
        launch = MemoryLaunch(tmp_path / "memory", python="py.exe")
        rig = await open_rig(tmp_path, memory=launch)
        await rig.press(",", Qt.KeyboardModifier.ControlModifier)
        await rig.wait_for(".settings")
        await rig.click_text(".snav", "共享记忆")
        await rig.wait_for(".mem-empty", 2)
        await rig.click_text(".mem-head .btn", "新建笔记")
        await rig.wait_for(".mem-create")
        await rig.type("Use pytest")  # the title box has the focus
        await rig.click(".mem-create textarea")
        await rig.type("Run python -m pytest tests -q.")
        await rig.click_text(".mem-create .segc button", "所有项目")
        await rig.click_text(".mem-create .btn", "记下来")
        await rig.wait_for(".mem-note", 1)
        (note,) = MemoryStore(launch.root).entries()
        assert (note.title, note.text, note.scope, note.source) == ("Use pytest", "Run python -m pytest tests -q.", "global", "user")

        await rig.click_text(".mem-note-body .btn", "忘记")
        await rig.wait_for(".mem-note", 0)
        assert MemoryStore(launch.root).read(note.id).status == "forgotten"

        assert rig.core.settings.memory_enabled
        await rig.click(".settings-body .field .switch", 0)
        await wait_for(lambda: rig.core.settings.memory_enabled is False, message="the master switch to reach the settings")
        await rig.wait_for(".mem-engine", 0)
        await rig.click(".settings-body .field .switch", 0)
        await rig.wait_for(".mem-engine", 2)
        await rig.click(".mem-engine .switch", 1)
        await wait_for(lambda: rig.core.settings.memory_engines == ["chatgpt"], message="the engine switch to reach the settings")
        await rig.finish()

    run(loop, scenario())


def test_the_light_theme_carries_a_whole_conversation(qapp, loop, tmp_path):
    async def scenario():
        rig = await open_rig(tmp_path)
        await rig.press(",", Qt.KeyboardModifier.ControlModifier)
        await rig.wait_for(".settings")
        await rig.click_text(".segc button", "浅色")
        await wait_for(lambda: rig.core.settings.theme == "light", message="the theme to reach the settings")
        assert await rig.js("document.documentElement.dataset.theme") == "light"
        await rig.press("Escape")
        await rig.wait_for(".settings", 0)
        model = await new_conversation(rig)
        model.next(say("Hi"), hold=False)
        await rig.send("hello")
        await rig.wait_text(".text-block", "Hi")
        assert await rig.js("document.documentElement.dataset.theme") == "light", "the theme survived a whole conversation"
        await rig.finish()

    run(loop, scenario())


def test_changing_engine_asks_which_of_the_attachments_go_along(qapp, loop, tmp_path):
    async def scenario():
        harness = make_devin(tmp_path)
        rig = await open_rig(tmp_path, devin_engine=harness.engine)
        await devin_listed(rig)
        model = await new_conversation(rig)
        model.next(say("noted"), hold=False)
        notes = tmp_path / "notes.txt"
        notes.write_text("remember the milk", encoding="utf-8")
        picture = tmp_path / "shot.png"
        picture.write_bytes(tiny_png(40, 30))
        await rig.drop_files([notes, picture])
        await rig.wait_for(".att-row .att", 2, timeout=15)
        await rig.send("read it")
        await rig.wait_text(".text-block", "noted")
        await wait_for(lambda: rig.core.order[0].controller.state is SessionState.READY, message="ready again")

        async def pick_devin() -> None:
            await rig.click(".controls .chip", 0)
            await rig.click_text(".menu-item", "SWE-2")
            await rig.wait_for(".dialog")

        await pick_devin()
        assert "SWE-2" in await rig.text(".dialog")
        names = await rig.texts(".dialog .carry-name")
        assert sorted(names) == ["notes.txt", "shot.png"], "every attachment is listed, the picture too"
        assert await rig.js("[...document.querySelectorAll('.dialog .carry-list input')].every(e => !e.checked && !e.disabled)")
        assert await rig.texts(".dialog .dialog-btns .btn") == ["取消", "都不带，换过去"]
        await rig.press("Escape")
        await rig.wait_for(".dialog", 0)
        assert rig.core.order[0].engine == "chatgpt", "cancelling changes nothing"

        await pick_devin()
        await rig.click(".dialog .carry-list input", names.index("shot.png"))
        await rig.wait_text(".dialog .dialog-btns .btn.primary", "带上选中的 1 个，换过去")
        await rig.click_text(".dialog .btn", "带上选中的 1 个")
        await wait_for(lambda: rig.core.order[0].engine == "devin", message="the conversation to move to Devin")
        handoff = rig.core.order[0].controller.handoff
        assert (handoff.files, handoff.pictures) == (0, 1) and "remember the milk" not in handoff.prefix, "only the picture was ticked"
        await rig.wait_for(".block.engine-switch", 1)
        await rig.wait_text(".statusbar .st-left", "1 张图片一起带过去")
        await rig.wait_text(".hint", "和 1 个附件")
        await rig.finish()

    run(loop, scenario())


def test_models_hidden_in_the_settings_leave_the_conversations_model_menu(qapp, loop, tmp_path):
    source = ".mvis[data-source='{}']"

    async def menu_labels(rig) -> list[str]:
        await rig.click(".controls .chip", 0)
        await rig.wait_for(".menu-item")
        labels = await rig.texts(".menu-item .menu-label")
        await rig.press("Escape")
        await rig.wait_for(".menu-item", 0)
        return labels

    async def scenario():
        harness = make_devin(tmp_path)
        rig = await open_rig(tmp_path, devin_engine=harness.engine)
        await devin_listed(rig)
        await new_conversation(rig)

        async def lists_every_model() -> bool:
            return {"GPT-6.1-Sol", "GPT-6-Luna", "SWE-2"} <= set(await menu_labels(rig))

        await wait_for(lists_every_model, 10, "the model menu to list the plan's and Devin's models")

        await rig.press(",", Qt.KeyboardModifier.ControlModifier)
        await rig.wait_for(".settings")
        await rig.click_text(".snav", "账号与模型")
        await rig.wait_text(f"{source.format('chatgpt')} .mvis-count", "2 / 2")
        await rig.click(f"{source.format('chatgpt')} .disclose")
        row = await rig.js(f"[...document.querySelectorAll(\"{source.format('chatgpt')} .mvis-row\")].findIndex(e => e.textContent.includes('GPT-6-Luna'))")
        await rig.click(f"{source.format('chatgpt')} .mvis-row .switch", row)
        await wait_for(lambda: rig.core.settings.hidden_models == ["chatgpt:gpt-6-luna"], message="the switch to reach the settings")
        await rig.wait_text(f"{source.format('chatgpt')} .mvis-count", "1 / 2")
        await rig.click(f"{source.format('devin')} .disclose")
        await rig.click_text(f"{source.format('devin')} .btn", "全部隐藏")
        await wait_for(
            lambda: rig.core.settings.hidden_models[:1] == ["chatgpt:gpt-6-luna"] and len(rig.core.settings.hidden_models) > 1 and all(m.startswith("devin:") for m in rig.core.settings.hidden_models[1:]),
            message="hide all to reach the settings",
        )
        await rig.press("Escape")
        await rig.wait_for(".settings", 0)

        labels = await menu_labels(rig)
        assert "GPT-6-Luna" not in labels and "SWE-2" not in labels and "GPT-6.1-Sol" in labels
        assert "没有显示的模型" in labels, "the emptied Devin group says why it is empty"

        await rig.press(",", Qt.KeyboardModifier.ControlModifier)
        await rig.wait_for(".settings")
        await rig.click_text(".snav", "账号与模型")
        await rig.click(f"{source.format('devin')} .disclose")
        await rig.click_text(f"{source.format('devin')} .btn", "全部显示")
        await wait_for(lambda: rig.core.settings.hidden_models == ["chatgpt:gpt-6-luna"], message="show all to reach the settings")
        await rig.press("Escape")
        await rig.wait_for(".settings", 0)
        labels = await menu_labels(rig)
        assert "SWE-2" in labels and "GPT-6-Luna" not in labels and "没有显示的模型" not in labels
        await rig.finish()

    run(loop, scenario())


def test_the_window_outline_is_drawn_when_python_asks_for_it_and_not_while_maximized(qapp, loop, tmp_path, monkeypatch):
    from desktop.ui import shell

    shadowed: list[int] = []
    monkeypatch.setattr(shell.winframe, "apply", lambda hwnd, **_: shadowed.append(hwnd) or True)

    async def scenario():
        rig = await open_rig(tmp_path, frame="round")
        assert rig.window.frame == "round"
        assert shadowed == [int(rig.window.winId())], "the window got its drop shadow once, when it was first shown"
        assert "frame=round" in await rig.js("location.search"), "and the page was told which corners to draw"
        rig.window.hide()
        rig.window.show()
        assert len(shadowed) == 1, "not again when it is shown again"
        await rig.wait_for(".frame-line.round")
        style = json.loads(await rig.js("JSON.stringify((() => { const s = getComputedStyle(document.querySelector('.frame-line')); return [s.borderTopWidth, s.borderTopLeftRadius, s.pointerEvents]; })())"))
        assert style == ["1px", "8px", "none"], "a 1 px line with the corners Windows 11 gives the window, taking no clicks"
        await rig.click(".titlebar .tb-btn", 1)  # maximize
        await rig.wait_for(".frame-line", 0)
        await rig.click(".titlebar .tb-btn", 1)  # restore
        await rig.wait_for(".frame-line.round")
        await rig.finish()

    run(loop, scenario())


def test_there_is_no_outline_without_a_system_shadow_to_go_with_it(qapp, loop, tmp_path):
    async def scenario():
        rig = await open_rig(tmp_path)  # the offscreen platform of the tests: no Windows window, so none was asked for
        assert rig.window.frame == ""
        assert not await rig.exists(".frame-line")
        await rig.finish()

    run(loop, scenario())

async def devin_rig(tmp_path, **options):
    """The real page on the real core and the real Devin engine, which talks to the fake `devin acp`."""
    from tests.devin_support import make_engine

    harness = make_engine(tmp_path, **options.pop("env", {}), account=options.pop("account", "signed-in"))
    rig = await open_rig(tmp_path, devin_engine=harness.engine, **options)
    return rig, harness


def test_a_devin_conversation_goes_from_the_model_menu_to_the_answer_and_an_approval(qapp, loop, tmp_path):
    async def scenario():
        rig, harness = await devin_rig(tmp_path)
        try:
            await wait_for(lambda: any(m["id"] == "devin:swe-2-high" for m in rig.core.catalog.models), 15, "the Devin models")
            await new_conversation(rig)
            await rig.click(".controls .chip", 0)
            await rig.wait_text(".menu-head", "DEVIN")
            assert "Claude Opus 5.5" in " ".join(await rig.texts(".menu-item")), "the models are in the menu"
            await rig.click_text(".menu-item", "SWE-2")
            await wait_for(lambda: rig.core.order[0].controller.engine == "devin", 15, "the conversation to move to Devin")
            await rig.wait_text(".statusbar .st-left", "就绪")
            assert await rig.text(".controls .chip-v", 0) == "SWE-2" and await rig.text(".controls .src-tag") == "订阅"
            assert "Devin" in await rig.text(".srow.open .ssub"), "the sidebar names the engine"

            await rig.send("hello devin")
            await rig.wait_text(".transcript .text-block", "echo: hello devin")
            await rig.wait_text(".statusbar .st-left", "就绪")
            assert "Token 合计" in await rig.text(".statusbar .st-right")

            await rig.send("[approve] clean the build folder")
            await rig.wait_for(".prompt.perm", 1, timeout=10)
            card = await rig.text(".prompt.perm")
            assert "rm -rf build" in card and "允许（仅此一次）" in card and "拒绝" in card
            assert await rig.exists(".tab .dot-attention")
            await rig.click_text(".prompt.perm .pbtns .btn", "允许（仅此一次）")
            await rig.wait_text(".statusbar .st-left", "就绪", timeout=15)
            await rig.wait_text(".transcript .text-block", "approved: allow_once")
            assert [m["result"]["outcome"] for m in harness.answers() if "result" in m] == [{"outcome": "selected", "optionId": "allow_once"}]
            await rig.click(".block.activity .gsum")
            await rig.click(".block.activity .irow")
            await rig.wait_text(".idetail", "审批：允许（仅此一次）")  # (the answer given is written on the tool)
            # closed, it is a saved session in the list, named as Devin's
            await rig.click(".tab-x")
            await rig.wait_for(".tab", 0)
            await rig.wait_text(".srow", "hello devin", timeout=15)
            assert "· Devin" in await rig.text(".srow .ssub")
        finally:
            await rig.finish()

    run(loop, scenario())


def test_the_devin_program_path_is_set_from_the_settings(qapp, loop, tmp_path):
    async def scenario():
        rig, harness = await devin_rig(tmp_path)
        try:
            await wait_for(lambda: any(m["id"] == "devin:swe-2-high" for m in rig.core.catalog.models), 15, "the Devin models")
            await rig.press(",", Qt.KeyboardModifier.ControlModifier)
            await rig.wait_for(".settings")
            await rig.click_text(".snav", "外部程序")
            await rig.wait_text(".settings-body", "Devin 命令行")
            assert rig.core.settings.devin_path == ""
            buttons = await rig.texts(".settings-body .segc button")
            assert buttons == ["自动检测", "指定路径"], "only Devin has a path: the own loop needs no program"
            await rig.click(".settings-body .segc button", 1)
            await wait_for(lambda: rig.core.settings.devin_path != "", 10, "the path to reach the settings")
            assert rig.core.settings.devin_path.endswith("fake_devin.py"), "the program in use becomes the path to use"
        finally:
            await rig.finish()

    run(loop, scenario())


def test_the_permission_of_a_devin_conversation_is_one_of_its_modes(qapp, loop, tmp_path):
    async def scenario():
        rig, harness = await devin_rig(tmp_path)
        try:
            await wait_for(lambda: any(m["id"] == "devin:swe-2-high" for m in rig.core.catalog.models), 15, "the Devin models")
            cid = (await rig.core.call("session.new", {"projectId": str(rig.project), "modelId": "devin:swe-2-high"}))["id"]
            await rig.wait_for(".tab", 1)
            await rig.wait_text(".statusbar .st-left", "就绪")
            assert await rig.text(".controls .chip-v", 2) == "写代码"
            await rig.click(".controls .chip", 2)
            assert await rig.texts(".menu-label") == ["问答", "计划", "写代码", "智能", "完全访问"]
            await rig.click_text(".menu-item", "智能")
            await wait_for(lambda: rig.core._by_cid[cid].controller.autonomy == "smart", 10, "the mode to change")
            assert harness.state()["sessions"][rig.core._by_cid[cid].controller.session_id]["mode"] == "smart"
            assert await rig.text(".controls .chip-v", 2) == "智能"
            await rig.click(".controls .chip", 1)
            assert await rig.texts(".menu-label") == ["Medium", "High", "Max"], "the model's own thought levels"
            await rig.press("Escape")
        finally:
            await rig.finish()

    run(loop, scenario())


def test_while_a_devin_turn_runs_ctrl_enter_gives_the_message_to_the_turn(qapp, loop, tmp_path):
    async def scenario():
        rig, harness = await devin_rig(tmp_path)
        try:
            await wait_for(lambda: any(m["id"] == "devin:swe-2-high" for m in rig.core.catalog.models), 15, "the Devin models")
            cid = (await rig.core.call("session.new", {"projectId": str(rig.project), "modelId": "devin:swe-2-high"}))["id"]
            await rig.wait_for(".tab", 1)
            await rig.wait_text(".statusbar .st-left", "就绪")
            await rig.send("[slow] long job")
            await rig.wait_for(".send.stop")
            await wait_for(lambda: rig.core._by_cid[cid].controller.transcript.turns[-1].tools, 10, "the command to start")
            await rig.focus_composer()
            await rig.type("actually, stop at the docs")
            await rig.press("Enter", Qt.KeyboardModifier.ControlModifier)
            await rig.wait_for(".block.user.steer", 1)
            assert not await rig.exists(".block.user.steer.pending"), "Devin takes it in at once: nothing to wait for"
            await rig.wait_text(".transcript", "got: actually, stop at the docs", timeout=15)
            await rig.wait_text(".statusbar .st-left", "就绪", timeout=15)
            assert await rig.count(".turn") == 1, "one turn, with the message in it"
        finally:
            await rig.finish()

    run(loop, scenario())


def test_devin_sign_in_and_out_from_the_settings(qapp, loop, tmp_path):
    async def scenario():
        rig, harness = await devin_rig(tmp_path, account="signed-out", env={"FAKE_DEVIN_LOGIN_DELAY": 0.1})
        try:
            await rig.press(",", Qt.KeyboardModifier.ControlModifier)
            await rig.wait_for(".settings")
            await rig.click_text(".snav", "账号与模型")
            await rig.wait_for(".providers")
            card = ".providers section.pcard:nth-of-type(2)"
            await rig.wait_text(f"{card} .pstate", "未登录", timeout=15)
            await rig.click_text(f"{card} .btn", "登录 Devin")
            await rig.wait_text(f"{card} .pstate", "me@example.com", timeout=15)
            assert "Devin Pro" in await rig.text(f"{card} .pstate")
            await wait_for(lambda: any(m["source"] == "devin" for m in rig.core.catalog.models), 15, "the Devin models after signing in")
            await rig.click_text(f"{card} .btn", "退出登录")
            await rig.wait_text(f"{card} .pstate", "未登录")
            await wait_for(lambda: not any(m["source"] == "devin" for m in rig.core.catalog.models), 15, "the Devin models to go")
        finally:
            await rig.finish()

    run(loop, scenario())


def test_a_devin_question_is_shown_once_and_names_devin_not_the_app(qapp, loop, tmp_path):
    async def scenario():
        harness = make_devin(tmp_path)
        rig = await open_rig(tmp_path, devin_engine=harness.engine, plan=False)
        try:
            await wait_for(lambda: any(m["source"] == "devin" for m in rig.core.catalog.models), 15, "the Devin models")
            cid = (await rig.core.call("session.new", {"projectId": str(rig.project), "modelId": "devin:swe-2-high"}))["id"]
            await rig.wait_text(".statusbar .st-left", "就绪")
            await rig.send("[slow] 先确认方案")
            controller = rig.core._by_cid[cid].controller
            await until(lambda: controller.state is SessionState.RUNNING)
            question = "如何处理这份演示配置？"
            request = {
                "sessionId": controller.session_id, "mode": "form", "message": question,
                "requestedSchema": {"type": "object", "properties": {
                    "config": {"type": "string", "title": "配置文件", "description": question, "enum": ["保留", "移除"]},
                }, "required": ["config"]},
            }
            waiting = asyncio.ensure_future(harness.engine._route_request(harness.clients[0], "elicitation/create", request, "form1"))
            await rig.wait_for(".prompt.ask", 1)
            assert (await rig.text(".prompt.ask .phead")).strip() == "Devin 想先问你几个问题"
            assert (await rig.text(".prompt.ask .qtext")).strip() == question
            assert (await rig.text(".prompt.ask")).count(question) == 1
            assert await rig.text(".prompt.ask .qtopic") == "配置文件"
            await rig.click_text(".prompt.ask .opt", "保留")
            await rig.click_text(".prompt.ask .btn", "提交回答")
            assert await waiting == {"action": "accept", "content": {"config": "保留"}}
            await rig.wait_for(".prompt.ask", 0)
            await rig.core.call("session.stop", {"id": cid})
        finally:
            await rig.finish()

    run(loop, scenario())


async def chatgpt_rig(tmp_path, fake, *, welcomed: bool = True, **options):
    """The real page on the real core and the real ChatGPT Plan engine (its own agent loop and tools), which talks to the
    loopback fake of OpenAI's Responses API."""
    from tests.chatgpt_support import make_engine

    engine = make_engine(tmp_path, fake, **options.pop("engine", {}))
    return await open_rig(tmp_path, chatgpt_engine=engine, chatgpt_welcomed=welcomed, plan=False, **options)


def test_a_chatgpt_plan_model_can_ask_a_question_mid_turn_and_goes_on_with_the_answer(qapp, loop, tmp_path):
    async def scenario():
        from tests.chatgpt_support import ACCESS_TOKEN
        from tests.fake_responses import FakeResponses, call, say

        with FakeResponses(token=ACCESS_TOKEN) as fake:
            rig = await chatgpt_rig(tmp_path, fake)
            try:
                await wait_for(lambda: any(m["id"] == "chatgpt:gpt-6.1-sol" for m in rig.core.catalog.models), 15, "the ChatGPT Plan models")
                await new_conversation(rig)
                await rig.click(".controls .chip", 0)
                await rig.click_text(".menu-item", "GPT-6.1-Sol")
                await wait_for(lambda: rig.core.order[0].controller.engine == "chatgpt", 15, "the conversation to move to ChatGPT Plan")
                await rig.wait_text(".statusbar .st-left", "就绪")

                ask = {"questions": [{"question": "Which database?", "header": "Storage", "options": ["SQLite", "Postgres"]}]}
                fake.script(call("ask_user", ask, call_id="c1", text="Before I start:"), say("Going on with the one you picked."))
                await rig.send("set up the storage")
                await rig.wait_for(".prompt.ask", 1, timeout=20)
                assert "ChatGPT Plan 想先问你几个问题" in await rig.text(".prompt.ask .phead"), "the card names the engine that asks"
                assert "Which database?" in await rig.text(".prompt.ask") and await rig.exists(".tab .dot-attention")
                await rig.click_text(".prompt.ask .opt", "Postgres")
                await rig.click_text(".prompt.ask .pbtns .btn", "提交回答")
                await rig.wait_text(".transcript", "Going on with the one you picked.", timeout=30)
                await rig.wait_text(".statusbar .st-left", "就绪", timeout=15)
                assert "Postgres" in await rig.text(".block.qa"), "the page keeps what was asked and answered"
                output = [item for item in fake.requests[-1]["input"] if item.get("type") == "function_call_output"][-1]["output"]
                assert output.startswith("The user answered:") and "Answer: Postgres" in output, "the model was given the answer"
            finally:
                await rig.finish()

    run(loop, scenario())


def test_a_chatgpt_plan_conversation_goes_from_the_model_menu_to_the_answer_and_an_approval(qapp, loop, tmp_path):
    async def scenario():
        from tests.chatgpt_support import ACCESS_TOKEN
        from tests.fake_responses import FakeResponses, call, say

        with FakeResponses(token=ACCESS_TOKEN) as fake:
            rig = await chatgpt_rig(tmp_path, fake)
            try:
                await wait_for(lambda: any(m["id"] == "chatgpt:gpt-6.1-sol" for m in rig.core.catalog.models), 15, "the ChatGPT Plan models")
                await new_conversation(rig)
                assert await rig.exists(".plan-badge"), "a new conversation starts on the plan, and says so"
                await rig.click(".controls .chip", 0)
                await rig.wait_text(".menu-head", "CHATGPT PLAN")
                assert "GPT-6.1-Sol" in " ".join(await rig.texts(".menu-item")), "the models are in the menu"
                await rig.click_text(".menu-item", "GPT-6.1-Sol")
                await wait_for(lambda: rig.core.order[0].controller.engine == "chatgpt", 15, "the conversation to move to ChatGPT Plan")
                await rig.wait_text(".statusbar .st-left", "就绪")
                assert await rig.text(".controls .chip-v", 0) == "GPT-6.1-Sol"
                badge = await rig.text(".plan-badge")
                assert "Using ChatGPT Plan" in badge and "Manage usage" in badge, "OpenAI's guideline: say the plan is in use and where to manage it"
                assert "ChatGPT Plan" in await rig.text(".srow.open .ssub"), "the sidebar names the engine"
                assert await rig.js("document.querySelector('.plan-badge .plan-ring .usage-ring')?.classList.contains('unknown')"), "the plan gives this sign-in no figures: the ring after Manage usage is grey"

                fake.script(say("Hello from the plan."))
                await rig.send("hello plan")
                await rig.wait_text(".transcript .text-block", "Hello from the plan.")
                await rig.wait_text(".statusbar .st-left", "就绪")
                assert "Token 合计" in await rig.text(".statusbar .st-right")
                assert fake.requests[-1]["store"] is False and fake.requests[-1]["stream"] is True

                fake.script(call("shell", {"command": "New-Item marker.txt"}, call_id="c1"), say("marker made"))
                await rig.send("make a marker")
                await rig.wait_for(".prompt.perm", 1, timeout=20)
                card = await rig.text(".prompt.perm")
                assert "New-Item marker.txt" in card and "允许（仅此一次）" in card and "拒绝" in card
                assert not (rig.project / "marker.txt").exists(), "nothing ran before the answer"
                assert await rig.exists(".tab .dot-attention")
                await rig.click_text(".prompt.perm .pbtns .btn", "允许（仅此一次）")
                await rig.wait_text(".transcript", "marker made", timeout=30)
                await rig.wait_text(".statusbar .st-left", "就绪", timeout=15)
                assert (rig.project / "marker.txt").exists()
                outputs = [item for item in fake.requests[-1]["input"] if item.get("type") == "function_call_output"]
                assert [item["call_id"] for item in outputs] == ["c1"], "the model was given the result"

                # closed, it is a saved session in the list, named as the plan's
                await rig.click(".tab-x")
                await rig.wait_for(".tab", 0)
                await rig.wait_text(".srow", "hello plan", timeout=15)
                assert "· ChatGPT Plan" in await rig.text(".srow .ssub")
            finally:
                await rig.finish()

    run(loop, scenario())


def test_the_status_bar_shows_the_speed_of_a_turn_between_context_and_token(qapp, loop, tmp_path):
    async def scenario():
        import re

        from tests.chatgpt_support import ACCESS_TOKEN
        from tests.fake_responses import FakeResponses, say, usage

        with FakeResponses(token=ACCESS_TOKEN) as fake:
            fake.script([("sleep", 0.8), *say("An answer that is long enough to measure.", used=usage(500, 120))])
            rig = await chatgpt_rig(tmp_path, fake)
            try:
                await wait_for(lambda: any(m["id"] == "chatgpt:gpt-6.1-sol" for m in rig.core.catalog.models), 15, "the ChatGPT Plan models")
                await rig.core.call("session.new", {"projectId": str(rig.project), "modelId": "chatgpt:gpt-6.1-sol"})
                await rig.wait_for(".tab", 1)
                await rig.wait_text(".statusbar .st-left", "就绪")
                bar = await rig.text(".statusbar .st-right")
                assert re.search(r"TPS\s*—", bar), "nothing measured before the first turn"
                await rig.send("write something")

                async def shown() -> bool:
                    return re.search(r"TPS\s*≈\s*\d", await rig.text(".statusbar .st-right")) is not None

                await wait_for(shown, 20, "the speed in the status bar")
                bar = await rig.text(".statusbar .st-right")
                assert bar.index("Context") < bar.index("TPS") < bar.index("Token"), bar
                value = float(re.search(r"TPS\s*≈\s*([\d.]+)", bar).group(1))
                assert 10 < value < 160, "120 tokens over a little more than the 0.8 s it took to begin"
            finally:
                await rig.finish()

    run(loop, scenario())


def test_the_one_time_chatgpt_plan_note_is_shown_until_it_is_dismissed(qapp, loop, tmp_path):
    async def scenario():
        from tests.chatgpt_support import ACCESS_TOKEN
        from tests.fake_responses import FakeResponses

        with FakeResponses(token=ACCESS_TOKEN) as fake:
            rig = await chatgpt_rig(tmp_path, fake, welcomed=False)
            try:
                await rig.wait_for(".chatgpt-welcome", timeout=15)
                text = await rig.text(".chatgpt-welcome")
                assert "You're using your ChatGPT Plan" in text and "Manage usage" in text
                assert rig.core.settings.chatgpt_welcomed is False
                await rig.click_text(".chatgpt-welcome .btn", "Got it")
                await rig.wait_for(".chatgpt-welcome", 0)
                await wait_for(lambda: rig.core.settings.chatgpt_welcomed is True, 10, "the note to be remembered")
            finally:
                await rig.finish()

    run(loop, scenario())


def test_the_chatgpt_plan_card_shows_the_account_and_its_switch_takes_the_models_out_of_the_menu(qapp, loop, tmp_path):
    async def scenario():
        from tests.chatgpt_support import ACCESS_TOKEN
        from tests.fake_responses import FakeResponses

        with FakeResponses(token=ACCESS_TOKEN) as fake:
            rig = await chatgpt_rig(tmp_path, fake)
            try:
                await wait_for(lambda: any(m["source"] == "chatgpt" for m in rig.core.catalog.models), 15, "the ChatGPT Plan models")
                await rig.press(",", Qt.KeyboardModifier.ControlModifier)
                await rig.wait_for(".settings")
                await rig.click_text(".snav", "账号与模型")
                await rig.wait_for(".providers")
                card = ".providers section.pcard:nth-of-type(1)"
                await rig.wait_text(f"{card} .pstate", "me@example.com", timeout=15)
                assert "ChatGPT Plan" in await rig.text(card) and "使用你的 ChatGPT 套餐" in await rig.text(f"{card} .pstate")
                assert "Manage usage" in await rig.text(card), "the way to the user's usage settings"
                await rig.click(f'{card} button[role="switch"]')
                await wait_for(lambda: rig.core.settings.chatgpt_enabled is False, 10, "the switch to reach the settings")
                await wait_for(lambda: not any(m["source"] == "chatgpt" for m in rig.core.catalog.models), 15, "the models to go")
                assert "me@example.com" in await rig.text(f"{card} .pstate"), "hidden, not signed out"
            finally:
                await rig.finish()

    run(loop, scenario())


def test_without_a_sign_in_chatgpt_plan_asks_for_it_with_openais_wording(qapp, loop, tmp_path):
    async def scenario():
        from tests.fake_responses import FakeResponses

        with FakeResponses() as fake:
            rig = await chatgpt_rig(tmp_path, fake, engine={"signed_in": False})
            try:
                assert await rig.count(".tab") == 0, "no model at all: nothing can be opened, the box for the first message is there"
                await rig.click(".controls .chip", 0)
                await rig.wait_text(".menu-head", "CHATGPT PLAN")
                assert "Continue with ChatGPT" in await rig.texts(".menu-label"), "the menu offers the sign-in"
                assert not any(m["source"] == "chatgpt" for m in rig.core.catalog.models), "no models without a sign-in"
                await rig.press("Escape")
                await rig.press(",", Qt.KeyboardModifier.ControlModifier)
                await rig.wait_for(".settings")
                await rig.click_text(".snav", "账号与模型")
                await rig.wait_for(".providers")
                card = ".providers section.pcard:nth-of-type(1)"
                await rig.wait_text(f"{card} .pstate", "未登录", timeout=15)
                assert await rig.text(f"{card} .btn.chatgpt-btn") == "Continue with ChatGPT"
            finally:
                await rig.finish()

    run(loop, scenario())


def test_the_slash_menu_offers_the_users_skills_and_a_skill_named_in_a_message_reaches_the_model(qapp, loop, tmp_path, monkeypatch):
    from desktop.core.harness import skills

    home = tmp_path / "home"
    monkeypatch.setattr(skills, "USER_HOME", home)
    folder = home / ".wittycode" / "skills" / "summarize-diff"
    folder.mkdir(parents=True)
    (folder / "SKILL.md").write_text("---\nname: summarize-diff\ndescription: Summarizes the diff.\n---\n\nRun git diff and list the risks.\n", encoding="utf-8")

    async def scenario():
        rig = await open_rig(tmp_path)
        model = await new_conversation(rig)
        model.next(say("Done."))
        await rig.focus_composer()
        await rig.type("/")
        await rig.wait_for(".slash-menu")
        await rig.wait_text(".slash-item .sl-name", "/summarize-diff")
        names = await rig.texts(".slash-item .sl-name")
        assert names == ["/compact", "/plan", "/goal", "/summarize-diff"], "the page's own commands (no /new) and the user's skill in one list"
        assert await rig.texts(".slash-item.skill .sl-kind") == ["技能"]
        order = await rig.js("JSON.stringify([...document.querySelector('.slash-menu').children].map(e => e.classList.contains('slash-sep') ? '---' : e.textContent.slice(0, 4)))")
        assert json.loads(order) == ["/com", "/pla", "/goa", "---", "/sum"], "a line sets the user's skills apart from the page's own commands"
        await rig.type("summ")
        await rig.wait_for(".slash-item", 1)
        await rig.press("Tab")
        assert await rig.js("document.querySelector('.input').value") == "/summarize-diff ", "picking a skill writes its name and waits for the rest"
        await rig.type("the migration")
        await rig.press("Enter")
        await model.asked()
        sent = "\n".join(part.get("text", "") for part in user_parts(model.requests[-1]))
        assert "/summarize-diff the migration" in sent and "Run git diff and list the risks." in sent
        await rig.wait_for(".block.user")
        assert await rig.text(".block.user .body") == "/summarize-diff the migration", "the page shows what was typed, not the instructions"
        await rig.wait_text(".block.notice", "已载入技能 summarize-diff")
        model.go()
        await rig.wait_text(".statusbar .st-left", "就绪")
        await finish(rig, model)

    run(loop, scenario())


def test_typing_slash_compact_in_the_message_box_compacts_the_conversation(qapp, loop, tmp_path):
    async def scenario():
        rig = await open_rig(tmp_path)
        model = await new_conversation(rig)
        model.next(say("Hi."), hold=False)
        model.next(say("SUMMARY: the user said hello.", item_id="msg_s"), hold=False)
        await rig.send("hello " + "background " * 300)
        await rig.wait_text(".statusbar .st-left", "就绪")
        await rig.focus_composer()
        await rig.type("/compact")
        await rig.wait_for(".slash-item", 1)
        await rig.press("Enter")
        await model.asked(2)
        await rig.wait_text(".block.notice", "已按你的要求压缩", timeout=15)
        assert await rig.js("document.querySelector('.input').value") == "", "the command is taken out of the box once it runs"
        await rig.wait_text(".statusbar .st-left", "就绪")
        await rig.finish()

    run(loop, scenario())
