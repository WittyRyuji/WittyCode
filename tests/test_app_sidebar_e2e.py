"""Real Qt page regressions for sidebar dragging and cached history before a connection."""

from __future__ import annotations

import asyncio

import pytest

from desktop.core.cache import Cache
from desktop.core.session_state import SessionState
from tests.devin_support import make_engine as make_devin
from tests.e2e_support import open_rig
from tests.test_app_cache_preview import local_snapshot
from tests.ui_support import run, wait_for

pytestmark = pytest.mark.ui


def test_dragging_the_real_sidebar_saves_order_and_survives_a_page_reload(qapp, loop, tmp_path):
    async def scenario():
        rig = await open_rig(tmp_path, saved=[("new", "Newer conversation", 5), ("old", "Older conversation", 60)])
        try:
            await rig.wait_for(".srow", 2)
            assert await rig.texts(".srow .stitle") == ["Newer conversation", "Older conversation"]
            assert await rig.js("[...document.querySelectorAll('.srow')].every(r => r.draggable)")
            await rig.js("""(() => {
                const [target, source] = document.querySelectorAll('.srow');
                const dt = new DataTransfer();
                source.dispatchEvent(new DragEvent('dragstart', {bubbles: true, dataTransfer: dt}));
                const r = target.getBoundingClientRect();
                for (const type of ['dragover', 'drop']) target.dispatchEvent(new DragEvent(type, {bubbles: true, cancelable: true, dataTransfer: dt, clientY: r.top + 2}));
                source.dispatchEvent(new DragEvent('dragend', {bubbles: true, dataTransfer: dt}));
            })()""")
            await rig.wait_text(".srow:first-child .stitle", "Older conversation")
            assert rig.core.settings.session_order == ["cg:old", "cg:new"]
            assert await rig.count(".tab") == 0, "sorting must not open the dragged conversation"
            await rig.click(".side-head-actions button", 1)
            await rig.wait_text(".srow:first-child .stitle", "Older conversation")
            rig.window.view.reload()
            await rig.wait_for(".boot", 0)
            await rig.wait_for(".srow", 2)
            assert await rig.texts(".srow .stitle") == ["Older conversation", "Newer conversation"]
        finally:
            await rig.finish()

    run(loop, scenario())


def test_local_messages_settings_and_usage_are_visible_before_devin_connects_and_after_failure(qapp, loop, tmp_path):
    async def scenario():
        harness = make_devin(tmp_path, FAKE_DEVIN_OPEN_DELAY="2", FAKE_DEVIN_OPEN_FAIL="1")
        cache = Cache(tmp_path / "cache")
        project = (tmp_path / "proj").resolve()
        snapshot = local_snapshot(project)
        cache.save_session("dv:cached-session", snapshot)
        cache.save_index({"rows": {str(project): [{"id": "dv:cached-session", "native": "cached-session", "engine": "devin", "title": snapshot["title"], "modified": snapshot["activeAt"] + 1800, "created": snapshot["activeAt"] - 100}]}})
        rig = await open_rig(tmp_path, cache=cache, devin_engine=harness.engine)
        try:
            await rig.wait_for(".srow")
            await rig.click(".srow")
            await rig.wait_text(".block.user", "A question from last time")
            await rig.wait_text(".text-block", "An answer available offline")
            conv = rig.core.order[0]
            assert conv.controller.state is SessionState.OPENING
            await rig.wait_text(".statusbar .st-left", "本地缓存")
            assert "1K/262K" in await rig.text(".ctxmeter") and "1.5K" in await rig.text(".usage")
            await wait_for(lambda: conv.controller.state is SessionState.DEAD, 15, "the failed connection")
            await rig.wait_text(".statusbar .st-left", "本地缓存")
            assert await rig.text(".block.user .body") == "A question from last time"
            assert "An answer available offline" in await rig.text(".text-block")
            assert await rig.count(".welcome") == 0
            rig.window.view.reload()
            await rig.wait_for(".boot", 0)
            await rig.wait_text(".text-block", "An answer available offline")
            assert await rig.text(".block.user .body") == "A question from last time", "reloading the page while offline keeps the preview too"
        finally:
            await rig.finish()

    run(loop, scenario())
