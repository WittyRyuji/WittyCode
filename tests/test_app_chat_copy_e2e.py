"""Right click through the actual QtWebEngine page, not a synthetic DOM contextmenu event."""

from __future__ import annotations

import json

import pytest
from PySide6.QtCore import QPoint, Qt
from PySide6.QtTest import QTest

from tests.e2e_support import Rig, open_rig
from tests.fake_responses import say
from tests.ui_support import outside_task, run, wait_for

pytestmark = pytest.mark.ui


@pytest.fixture
def offscreen_clipboard(qapp):
    # Check the running Qt platform, not just the environment (an existing app may use another plugin).
    if qapp.platformName() != "offscreen":
        pytest.skip("chat copy tests require Qt's offscreen clipboard")
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
            await rig.wait_for(".menu-item", 1)
            assert await rig.text(".menu-label") == "复制选中文字"
            # The clicked command must use the fragment at menu-open time, not the later selection state.
            await rig.js("getSelection().removeAllRanges()")
            await _copy_from_menu(rig, clipboard, "复制选中文字", "制片")

            await rig.focus_composer()
            await rig.press("v", Qt.KeyboardModifier.ControlModifier)
            assert await rig.js("document.querySelector('.input').value") == "制片"

            user_body = ".block.user .body"
            point = await _select_by_drag(rig, user_body, "part of")
            await _right_click_at(rig, point)
            await _copy_from_menu(rig, clipboard, "复制选中文字", "part of")
            await rig.js("getSelection().removeAllRanges()")

            await rig.click(user_body, right=True)
            await _copy_from_menu(rig, clipboard, "复制正文", prompt)
            await rig.click(".text-block p", right=True)
            await _copy_from_menu(rig, clipboard, "复制正文（Markdown）", markdown)
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
            assert "在右侧打开" in await rig.texts(".menu-label")
            await _copy_from_menu(rig, clipboard, "复制路径", str(file))

            point = await _select_by_drag(rig, path_link, "sample")
            await _right_click_at(rig, point)
            await rig.wait_for(".menu-item", 1)
            await _copy_from_menu(rig, clipboard, "复制选中文字", "sample")

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
