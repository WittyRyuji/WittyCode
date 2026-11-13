"""The real Qt page keeps square frames while sharing the newer reading layout."""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from tests.e2e_support import open_rig
from tests.fake_responses import call, say
from tests.test_app_e2e import new_conversation
from tests.ui_support import run, wait_for

pytestmark = pytest.mark.ui


@pytest.mark.parametrize("theme,width", [("light", 1500), ("dark", 900)])
def test_square_frames_sidebar_gutters_and_question_answers_in_the_real_page(qapp, loop, tmp_path, theme, width):
    async def scenario():
        rig = await open_rig(tmp_path, size=(width, 960), saved=[("saved", "Review inventory changes", 30)])
        try:
            await rig.core.call("settings.set", {"key": "theme", "value": theme})
            model = await new_conversation(rig)
            model.next(call("ask_user", {"questions": [{"question": "Which storage?", "header": "Storage", "options": ["SQLite", "Postgres"]}]}), hold=False)
            model.next(say("Continue with the selected storage."), hold=False)
            await rig.send("Choose storage for the inventory service")
            await rig.wait_for(".prompt.ask")
            assert await rig.js("""(() => {
                const selectors = ['.newbtn', '.srow.current', '.composer', '.composer .chip', '.send', '.block.user', '.block.activity', '.prompt.ask'];
                return selectors.every(selector => {
                    const s = getComputedStyle(document.querySelector(selector));
                    return [s.borderTopLeftRadius, s.borderTopRightRadius, s.borderBottomLeftRadius, s.borderBottomRightRadius].every(r => parseFloat(r) === 0);
                });
            })()"""), "the shared reading layout does not introduce rounded cards or controls"
            assert await rig.js("""(() => {
                const header = document.querySelector('.phead-row').getBoundingClientRect();
                const left = document.querySelector('.newbtn').getBoundingClientRect().left;
                return Math.abs(header.left - left) < 1 && [...document.querySelectorAll('.srow')].every(row => {
                    const r = row.getBoundingClientRect();
                    return Math.abs(r.left - header.left) < 1 && Math.abs(r.right - header.right) < 1;
                });
            })()"""), "selected and saved rows share the project header's outer edges"
            assert await rig.js("""(() => {
                const t = document.querySelector('.transcript').getBoundingClientRect(), c = document.querySelector('.composer').getBoundingClientRect();
                return Math.abs(t.left - c.left) < 1 && Math.abs(t.right - c.right) < 1;
            })()"""), "restoring the frame language keeps the newer reading grid"
            await rig.focus_composer()
            await wait_for(lambda: rig.js("getComputedStyle(document.querySelector('.composer')).boxShadow.includes('4px 4px 0px')"), 3, "the original hard focus shadow")
            before = await rig.js("document.querySelector('.scroller').scrollTop")
            await rig.type("A draft without layout changes")
            assert abs(await rig.js("document.querySelector('.scroller').scrollTop") - before) < 2
            await rig.click(".prompt.ask .btn.primary")
            await rig.wait_for(".q.missing")
            await rig.click_text(".prompt.ask .opt", "Postgres")
            screenshot = Path(__file__).resolve().parents[1] / "sandbox" / f"frame-style-qt-{theme}.png"
            screenshot.parent.mkdir(parents=True, exist_ok=True)
            await asyncio.sleep(0.15)
            assert rig.window.grab().save(str(screenshot))
            await rig.click(".prompt.ask .btn.primary")
            await rig.wait_for(".prompt.ask", 0)
            await rig.wait_text(".text-block", "Continue with the selected storage.")
            assert "Postgres" in await rig.text(".block.qa")
        finally:
            await rig.finish()

    run(loop, scenario())
