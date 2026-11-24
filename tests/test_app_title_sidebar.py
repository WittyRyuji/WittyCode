"""A named first draft is one sidebar row at once: before the first output, never doubled, gone when it fails."""

from __future__ import annotations

import asyncio

import pytest

from desktop.core import app_core
from desktop.core.app_core import AppError
from tests.app_support import ready_core
from tests.fake_responses import http_error, say
from tests.fakes import until
from tests.test_app_core import idle, listed, new_session


def row_keys(core, conv) -> list[str]:
    return [row["key"] for row in core._rows_for(conv.project)]


def test_titled_draft_is_one_sidebar_row_before_the_first_output(tmp_path):
    async def scenario():
        core, plan, events, _, _ = await ready_core(tmp_path)
        try:
            plan.fake.title_turn = [("hold", "title"), *say("库存导入校验")]
            plan.script([("hold", "main"), *say("先检查导入逻辑。")])
            cid = await new_session(core, events, tmp_path / "proj")
            conv = core._by_cid[cid]
            send = asyncio.create_task(core.call("session.send", {"id": cid, "text": "批量导入库存时重复行会被计算两次"}))
            await until(lambda: bool(plan.fake.title_requests))
            assert not conv.draft_title and row_keys(core, conv) == []  # a draft being named has no row yet
            plan.release("title")
            await until(lambda: bool(plan.requests))  # dispatched and held before the first output event
            assert (await send)["ok"]
            assert not conv.in_history and conv.draft_title == "库存导入校验"
            (row,) = core._rows_for(conv.project)
            assert row["id"] == cid and row["key"] == f"cg:{conv.controller.session_id}"
            plan.release("main")
            await idle(core, cid)
            assert conv.in_history and not conv.draft_title
            (stayed,) = core._rows_for(conv.project)
            assert stayed == row  # the same single row, promoted in place: nothing added or duplicated
        finally:
            plan.release("title")
            plan.release("main")
            await core.shutdown()

    asyncio.run(scenario())


def test_the_titled_row_is_emitted_without_waiting_for_the_sidebar_timer(tmp_path, monkeypatch):
    monkeypatch.setattr(app_core, "SIDEBAR_SECONDS", 3600)

    async def scenario():
        core, plan, events, _, _ = await ready_core(tmp_path)
        try:
            plan.fake.title_answer = "库存导入校验"
            plan.script([("hold", "main"), *say("先检查导入逻辑。")])
            cid = await new_session(core, events, tmp_path / "proj")
            conv = core._by_cid[cid]
            send = asyncio.create_task(core.call("session.send", {"id": cid, "text": "批量导入库存时重复行会被计算两次"}))
            await until(lambda: bool(plan.requests))  # dispatched and held; the emit had to precede this point
            assert cid in listed(events) and not conv.in_history  # the row came out with the title, not on the timer
            assert (await send)["ok"]
        finally:
            plan.release("main")
            await core.shutdown()

    asyncio.run(scenario())


def test_failed_title_never_enters_the_sidebar(tmp_path):
    async def scenario():
        core, plan, events, _, _ = await ready_core(tmp_path)
        try:
            plan.fake.title_answer = "两行\n无效标题"
            cid = await new_session(core, events, tmp_path / "proj")
            conv = core._by_cid[cid]
            with pytest.raises(AppError, match="尝试 3 次后仍未成功"):
                await core.call("session.send", {"id": cid, "text": "检查库存接口"})
            assert len(plan.fake.title_requests) == 3 and not plan.requests
            assert not conv.in_history and row_keys(core, conv) == []
        finally:
            await core.shutdown()

    asyncio.run(scenario())


def test_failed_first_dispatch_removes_the_title_row(tmp_path):
    async def scenario():
        core, plan, events, _, _ = await ready_core(tmp_path)
        try:
            plan.fake.title_answer = "库存导入校验"
            plan.script(http_error(400, {"error": {"message": "refused before the stream"}}))
            cid = await new_session(core, events, tmp_path / "proj")
            conv = core._by_cid[cid]
            assert (await core.call("session.send", {"id": cid, "text": "批量导入库存时重复行会被计算两次"}))["ok"]
            await until(lambda: not conv.draft_title)
            assert not conv.in_history and row_keys(core, conv) == []  # an undelivered first turn is not a row either
            await until(lambda: bool(events.of("restore")))
            assert events.last("restore")["text"] == "批量导入库存时重复行会被计算两次"
        finally:
            await core.shutdown()

    asyncio.run(scenario())
