"""Real Qt page regressions for sidebar dragging and cached history before a connection."""

from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path

import pytest

from desktop.core.cache import Cache
from desktop.core.session_state import SessionState
from tests.app_support import save_conversation
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


def test_concurrent_thinking_does_not_move_real_rows_but_each_final_answer_does(qapp, loop, tmp_path):
    from tests.fake_responses import say

    async def scenario():
        rig = await open_rig(tmp_path, saved=[("new", "Newest", 5), ("mid", "Middle", 30), ("old", "Oldest", 60)])
        try:
            rig.core.settings.sound_enabled = False
            await rig.wait_for(".srow", 3)
            conversations = []
            for native, title in (("old", "Oldest"), ("mid", "Middle"), ("new", "Newest")):
                cid = (await rig.core.call("session.openSaved", {"id": "cg:" + native, "projectId": str(rig.project)}))["id"]
                controller = rig.core._by_cid[cid].controller
                await wait_for(lambda: controller.state is SessionState.READY)
                rig.plan.script([("hold", native), *say("Final " + title)])
                requests = len(rig.plan.requests)
                await rig.core.call("session.send", {"id": cid, "text": "Continue"})
                await wait_for(lambda: len(rig.plan.requests) > requests)
                await rig.wait_text(".srow:first-child .stitle", title)
                conversations.append((native, title, controller))
            for _, _, controller in conversations:
                turn = controller.transcript.turns[-1]
                controller.active_at = 1
                controller.transcript.append_thinking(turn, "thought", 0, "Thinking")
                controller._changed()
                rig.core._emit_sidebar()
                await rig.wait_text(".srow:first-child .stitle", "Newest")
                assert await rig.texts(".srow .stitle") == ["Newest", "Middle", "Oldest"]
            for native, title, controller in conversations:
                rig.plan.release(native)
                await wait_for(lambda: controller.state is SessionState.READY)
                await rig.wait_text(".srow:first-child .stitle", title)
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


async def session_titles(rig, project_id: str) -> list[str]:
    """The ordinary sidebar rows of one project, in shown order (``.proj`` is found by id: paths do not
    survive a CSS attribute selector)."""
    raw = await rig.js(
        "JSON.stringify((() => {const p = [...document.querySelectorAll('.proj')].find("
        f"p => p.dataset.projectId === {json.dumps(project_id)});"
        "return p ? [...p.querySelectorAll('.slist .stitle')].map(e => e.textContent.trim()) : [];})())"
    )
    return json.loads(raw)


def test_two_projects_share_one_pinned_strip_above_them(qapp, loop, tmp_path):
    async def scenario():
        rig = await open_rig(tmp_path, saved=[("new", "Newer", 5), ("mid", "Middle", 20), ("old", "Older", 60)])
        try:
            await rig.wait_for(".srow", 3)
            proj = str(rig.project)
            other_dir = tmp_path / "other"
            other_dir.mkdir()
            save_conversation(rig.engine, other_dir, "x1", "X1", minutes_ago=30)
            save_conversation(rig.engine, other_dir, "x2", "X2", minutes_ago=10)
            await rig.core.call("project.add", {"path": str(other_dir)})
            await rig.core.call("project.refresh")
            other = str(other_dir.resolve())
            await rig.wait_for('.srow[data-session-id="cg:x2"]')
            assert await rig.texts(".side-scroll .proj .pname") == ["other", "proj"], "a newly added project opens the list"

            await rig.click('.srow[data-session-id="cg:mid"]', right=True)
            await rig.wait_for(".menu-item")
            await rig.click_text(".menu-item", "置顶")
            await rig.wait_for('.side-pinned .srow[data-session-id="cg:mid"]')
            await rig.click('.srow[data-session-id="cg:x1"] .more')
            await rig.wait_for(".menu-item")
            await rig.click_text(".menu-item", "置顶")
            await rig.wait_for('.side-pinned .srow[data-session-id="cg:x1"]')
            assert await rig.texts(".side-pinned .srow .stitle") == ["X1", "Middle"], "the newest pin leads one strip above the projects"
            assert await session_titles(rig, proj) == ["Newer", "Older"]
            assert await session_titles(rig, other) == ["X2"]
            for row in ("cg:mid", "cg:x1"):
                assert await rig.count(f'.srow[data-session-id="{row}"]') == 1, "a pinned row shows only at the top"

            toggle = await rig.js(
                "[...document.querySelectorAll('.side-scroll .proj')].findIndex(p => p.dataset.projectId === " + json.dumps(other) + ")"
            )
            await rig.click(".proj-toggle", toggle)
            await rig.wait_for(".proj .slist", 1)
            assert await rig.texts(".side-pinned .srow .stitle") == ["X1", "Middle"], "a folded project keeps its pinned rows"
            await rig.click(".proj-toggle", toggle)
            await rig.wait_for(".proj .slist", 2)

            await rig.js(
                "(() => {"
                "const projs = [...document.querySelectorAll('.proj')];"
                f"const source = projs.find(p => p.dataset.projectId === {json.dumps(other)}).querySelector('.phead-row');"
                f"const target = projs.find(p => p.dataset.projectId === {json.dumps(proj)});"
                "const dt = new DataTransfer();"
                "source.dispatchEvent(new DragEvent('dragstart', {bubbles: true, dataTransfer: dt}));"
                "const r = target.getBoundingClientRect();"
                "for (const type of ['dragover', 'drop']) target.dispatchEvent(new DragEvent(type, {bubbles: true, cancelable: true, dataTransfer: dt, clientY: r.bottom - 2}));"
                "source.dispatchEvent(new DragEvent('dragend', {bubbles: true, dataTransfer: dt}));"
                "})()"
            )
            await wait_for(lambda: rig.js("document.querySelector('.side-scroll .proj').dataset.projectId === " + json.dumps(proj)), 10, "the dragged project")
            assert await rig.texts(".side-scroll .proj .pname") == ["proj", "other"]
            assert rig.core.settings.projects == [proj, other]
            assert await rig.texts(".side-pinned .srow .stitle") == ["X1", "Middle"], "a project drag leaves the pinned strip alone"

            save_conversation(rig.engine, other_dir, "x3", "X3", minutes_ago=1)
            await rig.click('.side-head-actions [aria-label="刷新会话列表"]')
            await rig.wait_for('.srow[data-session-id="cg:x3"]')
            assert await rig.texts(".side-scroll .proj .pname") == ["proj", "other"]
            assert await rig.texts(".side-pinned .srow .stitle") == ["X1", "Middle"]
            assert await session_titles(rig, other) == ["X3", "X2"]

            if evidence := os.environ.get("WITTYCODE_TEST_EVIDENCE"):
                await rig.js("[...document.querySelectorAll('.ppath')].forEach(e => e.textContent = 'C:\\\\Projects\\\\demo')")
                await rig.shot(".sidebar", Path(evidence) / "two-projects-pins-qt.png")

            await rig.click('.side-pinned .srow[data-session-id="cg:mid"]', right=True)
            await rig.wait_for(".menu-item")
            await rig.click_text(".menu-item", "取消置顶")
            await wait_for(lambda: rig.js("document.querySelectorAll('.side-pinned .srow').length === 1"), 10, "one pin left")
            assert await rig.texts(".side-pinned .srow .stitle") == ["X1"]
            assert await session_titles(rig, proj) == ["Middle", "Newer", "Older"], "unpinning returns the row to the head of its own project"
            assert rig.core.settings.pinned_sessions == ["cg:x1"]
        finally:
            await rig.finish()

    run(loop, scenario())
