"""Right click through the actual QtWebEngine page, not a synthetic DOM contextmenu event."""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest
from PySide6.QtCore import QPoint, Qt
from PySide6.QtTest import QTest

from tests import e2e_support
from tests.e2e_support import Rig, open_rig
from tests.fake_responses import say
from tests.ui_support import outside_task, run, wait_for

pytestmark = pytest.mark.ui


@pytest.fixture
def offscreen_clipboard(qapp, monkeypatch):
    # Check the running Qt platform, not just the environment (an existing app may use another plugin).
    if qapp.platformName() != "offscreen":
        pytest.skip("chat copy tests require Qt's offscreen clipboard")
    if work := os.environ.get("WITTYCODE_TEST_WORK"):
        built = Path(work) / "dist" / "index.html"
        if built.is_file():
            monkeypatch.setattr(e2e_support, "PAGE_FILE", built)
    return qapp.clipboard()


async def _answer(rig: Rig, prompt: str, answer: str) -> None:
    await rig.core.call("session.new", {"projectId": str(rig.project)})
    await rig.wait_for(".tab")
    await rig.wait_text(".statusbar .st-left", "就绪")
    rig.plan.script(say(answer))
    await rig.send(prompt)
    await rig.wait_for(".text-block .text:not(.streaming)")


async def _select_by_drag(rig: Rig, selector: str, text: str) -> QPoint:
    """DOM ranges locate the glyphs; all selection input goes through Qt."""
    raw = await rig.js(f"""(() => {{
        const el = document.querySelector({json.dumps(selector)});
        el.scrollIntoView({{block: 'nearest'}});
        const n = el.firstChild, needle = {json.dumps(text)};
        const at = n.textContent.indexOf(needle);
        const r = document.createRange();
        r.setStart(n, at); r.setEnd(n, at + needle.length);
        const b = r.getBoundingClientRect();
        return JSON.stringify([b.left, b.right, b.top + b.height / 2]);
    }})()""")
    left, right, y = json.loads(raw)
    zoom = rig.window.view.zoomFactor()
    start, middle, end = (QPoint(round(x * zoom), round(y * zoom)) for x in (left, (left + right) / 2, right))
    target = rig._target()
    await outside_task(lambda: QTest.mouseMove(target, start))
    await outside_task(lambda: QTest.mousePress(target, Qt.MouseButton.LeftButton, pos=start))
    await outside_task(lambda: QTest.mouseMove(target, middle))
    await outside_task(lambda: QTest.mouseMove(target, end))
    await outside_task(lambda: QTest.mouseRelease(target, Qt.MouseButton.LeftButton, pos=end))

    async def selected() -> bool:
        return await rig.js("getSelection().toString()") == text

    await wait_for(selected, 3, f"{text!r} selected by dragging")
    raw = await rig.js("""(() => {
        const b = getSelection().getRangeAt(0).getBoundingClientRect();
        return JSON.stringify([b.left + b.width / 2, b.top + b.height / 2]);
    })()""")
    x, y = json.loads(raw)
    return QPoint(round(x * zoom), round(y * zoom))


async def _right_click_at(rig: Rig, point: QPoint) -> None:
    target = rig._target()
    await outside_task(lambda: QTest.mouseMove(target, point))
    await outside_task(lambda: QTest.mouseClick(target, Qt.MouseButton.RightButton, pos=point))


async def _assert_chat_menu(rig: Rig) -> None:
    await rig.wait_for(".menu-item", 2)
    assert await rig.texts(".menu-label") == ["复制", "Ask it"]


async def _copy_from_menu(rig: Rig, clipboard, label: str, expected: str) -> None:
    await rig.wait_text(".menu-label", label)
    clipboard.setText("before copy")
    await rig.click_text(".menu-item", label)
    # Windows may use native CRLF line endings in the clipboard's plain-text format.
    await wait_for(lambda: clipboard.text().replace("\r\n", "\n") == expected, 5, f"{label} to copy {expected!r}")
    await rig.wait_for(".menu", 0)


def test_chat_right_click_copies_selection_or_body_to_the_clipboard(offscreen_clipboard, loop, tmp_path):
    clipboard = offscreen_clipboard

    async def scenario():
        rig = await open_rig(tmp_path)
        try:
            prompt = "Please copy part of this request"
            markdown = "Prefix **复制片段** suffix.\n\n```text\nplain code\n```"
            await _answer(rig, prompt, markdown)
            point = await _select_by_drag(rig, ".text-block strong", "制片")
            await _right_click_at(rig, point)
            await _assert_chat_menu(rig)
            # The clicked command must use the fragment at menu-open time, not the later selection state.
            await rig.js("getSelection().removeAllRanges()")
            await _copy_from_menu(rig, clipboard, "复制", "制片")

            await rig.focus_composer()
            await rig.press("v", Qt.KeyboardModifier.ControlModifier)
            assert await rig.js("document.querySelector('.input').value") == "制片"

            user_body = ".block.user .body"
            point = await _select_by_drag(rig, user_body, "part of")
            await _right_click_at(rig, point)
            await _assert_chat_menu(rig)
            await _copy_from_menu(rig, clipboard, "复制", "part of")
            await rig.js("getSelection().removeAllRanges()")

            await rig.click(user_body, right=True)
            await _assert_chat_menu(rig)
            await _copy_from_menu(rig, clipboard, "复制", prompt)
            await rig.click(".text-block p", right=True)
            await _assert_chat_menu(rig)
            await _copy_from_menu(rig, clipboard, "复制", markdown)
        finally:
            await rig.finish()

    run(loop, scenario())


async def _assert_focused_draft(rig: Rig, expected: str) -> None:
    async def matches() -> bool:
        return await rig.js(f"""(() => {{
            const input = document.querySelector('.input');
            return input.value === {json.dumps(expected)} && document.activeElement === input &&
                input.selectionStart === input.value.length && input.selectionEnd === input.value.length;
        }})()""")

    await rig.wait_for(".menu", 0)
    await wait_for(matches, 5, f"focused draft with the caret at the end: {expected!r}")


def test_chat_ask_it_appends_the_snapshot_to_the_current_draft_without_sending(offscreen_clipboard, loop, tmp_path, monkeypatch):
    clipboard = offscreen_clipboard

    async def scenario():
        rig = await open_rig(tmp_path, saved=[("other-chat", "Other chat", 5)])
        try:
            prompt = "Explain this request"
            markdown = "Prefix **selected words** suffix.\n\n```text\nplain code\n```"
            await _answer(rig, prompt, markdown)
            await rig.wait_text(".statusbar .st-left", "就绪")

            await rig.click_text(".srow", "Other chat")
            await rig.wait_text(".block.user .body", "about Other chat")
            await rig.wait_for(".input:enabled")
            await rig.focus_composer()
            await rig.type("Other draft")
            await rig.click(".tab", 0)
            user_body = ".block.user .body"
            await rig.wait_text(user_body, prompt)
            await rig.wait_for(".input:enabled")

            note = tmp_path / "notes.txt"
            note.write_text("leave this attachment in the composer", encoding="utf-8")
            await rig.drop_files([note])
            await rig.wait_text(".att-row .fname", "notes.txt")

            send_calls = []
            call = rig.core.call

            async def observe(method, params):
                if method in {"session.send", "session.sendQueued"}:
                    send_calls.append(method)
                return await call(method, params)

            monkeypatch.setattr(rig.core, "call", observe)
            clipboard.setText("leave this clipboard text alone")
            await rig.js("""window.clipboardCalls = [];
                for (const method of ['read', 'readText', 'write', 'writeText']) {
                    navigator.clipboard[method] = (...args) => {
                        window.clipboardCalls.push(method);
                        return Clipboard.prototype[method].apply(navigator.clipboard, args);
                    };
                }""")

            ask = "这是什么意思，解释一下："
            point = await _select_by_drag(rig, ".text-block strong", "selected")
            await _right_click_at(rig, point)
            await _assert_chat_menu(rig)
            await rig.js("getSelection().removeAllRanges()")
            await rig.click_text(".menu-item", "Ask it")
            expected = ask + "selected"
            await _assert_focused_draft(rig, expected)

            await rig.click(user_body, right=True)
            await _assert_chat_menu(rig)
            # Edit after the menu has opened and leave a selection away from the end of the latest draft.
            await rig.press("l", Qt.KeyboardModifier.ControlModifier)
            await rig.press("End", Qt.KeyboardModifier.ControlModifier)
            await rig.type(" plus a late edit")
            await rig.js("document.querySelector('.input').setSelectionRange(1, 4)")
            await rig.click_text(".menu-item", "Ask it")
            expected += " plus a late edit\n" + ask + prompt
            await _assert_focused_draft(rig, expected)

            await rig.press("Enter", Qt.KeyboardModifier.ShiftModifier)
            await rig.click(".text-block p", right=True)
            await _assert_chat_menu(rig)
            await rig.click_text(".menu-item", "Ask it")
            expected += "\n" + ask + markdown
            await _assert_focused_draft(rig, expected)

            # A keyboard selection in the same menu must consume Enter even if the composer still has focus.
            await rig.click(user_body, right=True)
            await _assert_chat_menu(rig)
            await rig.press("l", Qt.KeyboardModifier.ControlModifier)
            for label in ("复制", "Ask it"):
                await outside_task(lambda: QTest.keyClick(rig._target(), Qt.Key.Key_Down))
                await rig.wait_text(".menu-item.active .menu-label", label)
            await rig.press("Enter")
            expected += "\n" + ask + prompt
            await _assert_focused_draft(rig, expected)

            assert await rig.texts(".att-row .fname") == ["notes.txt"]
            assert await rig.count(".turn") == 1 and not await rig.exists(".qs-row")
            await rig.click(".tab", 1)
            await rig.wait_text(".block.user .body", "about Other chat")
            assert await rig.js("document.querySelector('.input').value") == "Other draft"
            assert clipboard.text() == "leave this clipboard text alone"
            assert await rig.js("JSON.stringify(window.clipboardCalls)") == "[]"
            assert not send_calls, "Ask it must not issue a send or queue RPC"
        finally:
            await rig.finish()

    run(loop, scenario())


def test_chat_copy_keeps_path_and_sidebar_menus_and_input_editing(offscreen_clipboard, loop, tmp_path):
    clipboard = offscreen_clipboard

    async def scenario():
        rig = await open_rig(tmp_path, saved=[("sidebar-copy", "Saved example", 5)])
        try:
            docs = rig.project / "docs"
            docs.mkdir()
            file = docs / "sample.md"
            file.write_text("Example", encoding="utf-8")
            await _answer(rig, "Show the link", "Open the [sample file](docs/sample.md) for details.")
            path_link = ".text-block .pathref"
            await rig.click(path_link, right=True)
            await rig.wait_for(".menu-item", 5)
            labels = await rig.texts(".menu-label")
            assert "在右侧打开" in labels and "Ask it" not in labels and "复制" not in labels
            await _copy_from_menu(rig, clipboard, "复制路径", str(file))

            point = await _select_by_drag(rig, path_link, "sample")
            await _right_click_at(rig, point)
            await _assert_chat_menu(rig)
            await _copy_from_menu(rig, clipboard, "复制", "sample")

            await rig.wait_text(".srow .stitle", "Saved example")
            await rig.click_text(".srow", "Saved example")
            await rig.wait_text(".block.user .body", "about Saved example")
            titles = await rig.texts(".srow .stitle")
            await rig.click(".srow", titles.index("Saved example"), right=True)
            await _copy_from_menu(rig, clipboard, "复制会话 ID", "sidebar-copy")

            await rig.focus_composer()
            await rig.type("draft text")
            await rig.press("a", Qt.KeyboardModifier.ControlModifier)
            await rig.click(".input", right=True)
            assert not await rig.exists(".menu"), "the composer must not get the transcript menu"
            await rig.press("a", Qt.KeyboardModifier.ControlModifier)
            await rig.press("c", Qt.KeyboardModifier.ControlModifier)
            await wait_for(lambda: clipboard.text() == "draft text", message="the composer's normal Ctrl+C")
        finally:
            await rig.finish()

    run(loop, scenario())
