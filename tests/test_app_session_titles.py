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


def test_slow_title_does_not_block_a_turn_or_override_a_manual_name(tmp_path):
    async def scenario():
        core, plan, events, _, _ = await ready_core(tmp_path)
        try:
            plan.fake.title_turn = [("hold", "title"), *say("自动摘要")]
            cid = await new_session(core, events, tmp_path / "proj")
            await talk(core, plan, cid, "检查库存接口")
            await until(lambda: bool(plan.fake.title_requests))
            await idle(core, cid)
            assert core._by_cid[cid].controller.title == "新会话"
            await core.call("session.rename", {"id": cid, "title": "用户自己的名字"})
            plan.release("title")
            await until(lambda: bool(core.settings.session_auto_titles))
            assert core._title_of(core._by_cid[cid]) == "用户自己的名字"
        finally:
            await core.shutdown()

    asyncio.run(scenario())


def test_api_titles_use_the_selected_provider_without_a_chatgpt_login(tmp_path):
    from tests.llm_support import FakeApi
    from tests.test_app_api import api_core, api_model_id, new_session as api_session, says

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
