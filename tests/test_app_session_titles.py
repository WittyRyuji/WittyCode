"""Titles are a separate LLM summary of the first question, never another visible turn or a provider crossover."""

from __future__ import annotations

import asyncio

import pytest

from desktop.core.session_title import clean
from tests.app_support import ready_core
from tests.fake_responses import say
from tests.fakes import until
from tests.test_app_core import idle, new_session, talk


def test_first_question_is_summarized_once_without_polluting_the_turn_history(tmp_path):
    async def scenario():
        core, plan, events, _, _ = await ready_core(tmp_path)
        try:
            plan.fake.title_answer = "库存导入校验与去重"
            cid = await new_session(core, events, tmp_path / "proj")
            question = "我今天发现了一个问题。\n批量导入库存时重复行会被计算两次，请检查校验和去重。"
            await talk(core, plan, cid, question, "先检查导入逻辑。")
            await until(lambda: bool(core.settings.session_auto_titles))
            conv = core._by_cid[cid]
            assert core._title_of(conv) == "库存导入校验与去重"
            assert core._title_of(conv) != question.splitlines()[0]
            await talk(core, plan, cid, "再看事务边界")
            assert len(plan.fake.title_requests) == 1
            request = plan.fake.title_requests[0]
            assert request["model"] == "gpt-6.1-sol" and not request.get("tools") and request["store"] is False
            assert "重复行" in str(request["input"]) and "再看事务边界" not in str(request["input"])
            assert len(conv.controller.transcript.turns) == 2
        finally:
            await core.shutdown()

    asyncio.run(scenario())


def test_slow_title_blocks_the_first_turn_and_manual_rename_cannot_bypass_it(tmp_path):
    from desktop.core.app_core import AppError

    async def scenario():
        core, plan, events, _, _ = await ready_core(tmp_path)
        try:
            plan.fake.title_turn = [("hold", "title"), *say("自动摘要")]
            plan.script(say("检查接口。"))
            cid = await new_session(core, events, tmp_path / "proj")
            conv = core._by_cid[cid]
            send = asyncio.create_task(core.call("session.send", {"id": cid, "text": "检查库存接口"}))
            await until(lambda: bool(plan.fake.title_requests))
            assert not send.done() and not plan.requests and not conv.controller.transcript.turns
            assert core._title_of(conv) == "新会话" and not conv.in_history
            with pytest.raises(AppError, match="发出第一条消息之后"):
                await core.call("session.rename", {"id": cid, "title": "手动命名"})
            plan.release("title")
            assert (await send)["ok"]
            await idle(core, cid)
            assert len(plan.requests) == 1 and core._title_of(conv) == "自动摘要"
            assert not core.settings.session_titles
        finally:
            plan.release("title")
            await core.shutdown()

    asyncio.run(scenario())


def test_exhausted_title_retries_do_not_dispatch_and_preserve_the_draft(tmp_path):
    from desktop.core.app_core import AppError
    from tests.test_app_core import png_upload

    async def scenario():
        core, plan, events, _, _ = await ready_core(tmp_path)
        try:
            cid = await new_session(core, events, tmp_path / "proj")
            conv = core._by_cid[cid]
            await core.call("attachments.add", {"id": cid, "files": [png_upload("draft.png")]})
            plan.fake.title_answer = "两行\n无效标题"
            message = {"id": cid, "text": "检查库存接口"}
            with pytest.raises(AppError, match="尝试 3 次后仍未成功，首条消息没有发出"):
                await core.call("session.send", message)
            assert len(plan.fake.title_requests) == 3
            assert events.last("restore")["text"] == message["text"] and len(conv.drafts) == 1
            assert not conv.in_history and not conv.controller.transcript.turns and not plan.requests
            assert not core.settings.session_auto_titles and not core.chatgpt.sessions.load(conv.controller.session_id)
            with pytest.raises(AppError, match="发出第一条消息之后"):
                await core.call("session.rename", {"id": cid, "title": "手动命名"})
            plan.fake.title_answer = "库存校验"
            plan.script(say("收到。"))
            assert (await core.call("session.send", message))["ok"]
            await idle(core, cid)
            assert len(plan.requests) == 1 and len(plan.fake.title_requests) == 4
            assert core._title_of(conv) == "库存校验"
            assert len(conv.controller.transcript.turns[0].user_block.attachments) == 1
        finally:
            await core.shutdown()

    asyncio.run(scenario())


def test_first_title_uses_raw_text_and_retries_invalid_answers_without_sending_main(tmp_path):
    from desktop.core.session_title import FIRST_SYSTEM, prompt

    async def scenario():
        core, plan, events, _, _ = await ready_core(tmp_path)
        try:
            raw = "  检查库存导入\n并核对事务边界  "
            name = "库存数据导入事务检查"
            plan.fake.title_turns = [say("超长" * 6), say("新会话"), [("hold", "last-title"), *say(name)]]
            plan.script(say("已检查。"))
            cid = await new_session(core, events, tmp_path / "proj")
            send = asyncio.create_task(core.call("session.send", {"id": cid, "text": raw}))
            await until(lambda: len(plan.fake.title_requests) == 3)
            assert not send.done() and not plan.requests
            for request in plan.fake.title_requests:
                assert request["instructions"] == FIRST_SYSTEM and not request.get("tools")
                assert request["input"][0]["content"][0]["text"] == prompt(raw, first=True)
            plan.release("last-title")
            assert (await send)["ok"]
            await idle(core, cid)
            assert len(plan.requests) == 1 and core._title_of(core._by_cid[cid]) == name
        finally:
            plan.release("last-title")
            await core.shutdown()

    asyncio.run(scenario())


def test_stopping_first_title_does_not_start_the_turn(tmp_path):
    from desktop.core.app_core import AppError

    async def scenario():
        core, plan, events, _, _ = await ready_core(tmp_path)
        try:
            plan.fake.title_turn = [("hold", "title"), *say("库存接口")]
            cid = await new_session(core, events, tmp_path / "proj")
            send = asyncio.create_task(core.call("session.send", {"id": cid, "text": "核对库存接口"}))
            await until(lambda: bool(plan.fake.title_requests))
            await core.call("session.stop", {"id": cid})
            with pytest.raises(AppError, match="已放回输入框"):
                await send
            conv = core._by_cid[cid]
            assert events.last("restore")["text"] == "核对库存接口"
            assert not plan.requests and not conv.in_history and not conv.controller.transcript.turns
            assert conv.preparing is None and conv.switch_task is None
        finally:
            plan.release("title")
            await core.shutdown()

    asyncio.run(scenario())


def test_api_titles_use_the_selected_provider_and_a_30_second_deadline(tmp_path, monkeypatch):
    from types import SimpleNamespace
    from unittest.mock import Mock

    from desktop.core.fusion import title
    from tests.llm_support import FakeApi
    from tests.test_app_api import api_core, api_model_id, new_session as api_session, says

    deadline = Mock(wraps=asyncio.timeout)
    monkeypatch.setattr(title, "asyncio", SimpleNamespace(timeout=deadline))

    async def scenario():
        with FakeApi() as api:
            api.title_turn = says("库存事务校验")
            api.script(says("检查事务边界。"))
            core, _, events, engine, _ = await api_core(tmp_path, api)
            try:
                assert engine.cached_account()["auth"] == "signed-out"
                cid = await api_session(core, events, tmp_path, api_model_id(events))
                await core.call("session.send", {"id": cid, "text": "检查导入库存的事务和重复行"})
                await until(lambda: bool(core.settings.session_auto_titles))
                assert core._title_of(core._by_cid[cid]) == "库存事务校验"
                deadline.assert_called_once_with(30.0)
                assert len(api.title_posts) == 1
                path, _, body = api.title_posts[0]
                assert path == "/v1/chat/completions" and body["model"] == "deep-chat"
                assert not body.get("tools")
            finally:
                await core.shutdown()

    asyncio.run(scenario())


@pytest.mark.parametrize("answer", [None, "", "\n", "两行\n不是标题", "# Markdown", "x" * 81])
def test_invalid_titles_are_not_replaced_with_the_first_sentence(answer):
    assert clean(answer) is None
