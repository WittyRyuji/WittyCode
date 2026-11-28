"""Only a submitted conversation (or recoverable in-flight work) belongs in project history."""

from __future__ import annotations

import asyncio

import pytest

from desktop.core.app_core import AppError
from desktop.core.cache import Cache
from tests.app_support import ready_core, save_conversation
from tests.fake_responses import fail, say
from tests.fakes import until
from tests.test_app_core import idle, new_session, png_upload


def project_ids(core):
    return core.project_rows()[0]["sessionIds"]


@pytest.mark.parametrize("fusion", [False, True])
def test_draft_has_a_tab_but_never_enters_saved_history(tmp_path, fusion):
    async def scenario():
        cache = Cache(tmp_path / "cache")
        core, plan, events, _, _ = await ready_core(tmp_path, cache=cache)
        project = tmp_path / "proj"
        try:
            core.settings.fusion_main = "chatgpt:gpt-6.1-sol"
            core.settings.fusion_light = "chatgpt:gpt-6-luna"
            core.settings.fusion_workers = [core.settings.fusion_light]
            cid = (await core.call("session.new", {"projectId": str(project), "fusion": fusion}))["id"]
            controller = core._by_cid[cid].controller
            native = controller.session_id
            assert events.last("tabs")["tabs"] == [cid]
            assert (await core.call("attachments.add", {"id": cid, "files": [png_upload("draft.png")]}))["added"]
            with pytest.raises(AppError, match="没有内容"):
                await core.call("session.send", {"id": cid, "text": " ", "attachmentIds": []})
            await core.call("project.refresh", {"id": str(project)})
            assert project_ids(core) == [] and core.chatgpt.sessions.load(native) is None
            assert not core.settings.session_created and not core.settings.session_order and not plan.requests
            await core.call("session.close", {"id": cid})
        finally:
            await core.shutdown()
        again, _, _, _, _ = await ready_core(tmp_path, cache=cache)
        try:
            await again.call("project.refresh", {"id": str(project)})
            assert project_ids(again) == [] and again.chatgpt.sessions.load(native) is None
            assert cache.load_session("cg:" + native) is None
        finally:
            await again.shutdown()

    asyncio.run(scenario())


def test_first_refusal_does_not_list_or_name_and_can_be_retried(tmp_path):
    async def scenario():
        core, plan, events, _, _ = await ready_core(tmp_path)
        project = tmp_path / "proj"
        try:
            # An existing row with no title must not be mistaken for a new draft.
            save_conversation(core.chatgpt, project, "known", "", minutes_ago=5)
            await core.call("project.refresh", {"id": str(project)})
            cid = await new_session(core, events, project)
            native = core._by_cid[cid].controller.session_id
            row = "cg:" + native
            plan.script([("hold", "refuse"), *fail("subscription_sharing_usage_limit_exceeded")], say("accepted"))
            assert (await core.call("session.send", {"id": cid, "text": "first attempt"}))["ok"]
            await until(lambda: len(plan.requests) == 1)
            await core.call("project.refresh", {"id": str(project)})
            assert project_ids(core) == [cid, "cg:known"], "the named draft is a sidebar row before the first dispatch"
            plan.release("refuse")
            await idle(core, cid)
            await until(lambda: events.of("restore"))
            assert events.last("restore")["text"] == "first attempt"
            assert core.chatgpt.sessions.load(native) is None and not core.settings.session_auto_titles
            assert len(plan.fake.title_requests) == 1
            assert row not in core.settings.session_created
            await core.call("project.refresh", {"id": str(project)})
            assert project_ids(core) == ["cg:known"]
            await core.call("session.send", {"id": cid, "text": "retry instead"})
            await idle(core, cid)
            assert len(plan.fake.title_requests) == 2
            assert project_ids(core) == [cid, "cg:known"]
            saved = core.chatgpt.sessions.load(native)
            assert saved.messages == 1 and "first attempt" not in str(saved.items)
            assert "retry instead" in str(plan.fake.title_requests[-1]["input"])
            await core.call("session.close", {"id": cid})
            await core.call("project.refresh", {"id": str(project)})
            assert project_ids(core) == [row, "cg:known"]
        finally:
            plan.release("refuse")
            await core.shutdown()

    asyncio.run(scenario())


def test_local_preparation_failure_does_not_create_an_archive_after_close_or_restart(tmp_path, monkeypatch):
    async def scenario():
        cache = Cache(tmp_path / "cache")
        core, plan, events, _, _ = await ready_core(tmp_path, cache=cache)
        project = tmp_path / "proj"
        try:
            cid = await new_session(core, events, project)
            controller = core._by_cid[cid].controller
            native = controller.session_id

            async def unavailable(_):
                raise ValueError("test preflight failure")

            with monkeypatch.context() as patch:
                patch.setattr(type(controller), "send_prompt", lambda *args: False)
                with pytest.raises(AppError, match="没有就绪"):
                    await core.call("session.send", {"id": cid, "text": "not queued"})
            monkeypatch.setattr(type(controller), "_ensure_tools", unavailable)
            await core.call("session.send", {"id": cid, "text": "cannot start"})
            await idle(core, cid)
            await until(lambda: events.last("restore")["text"] == "cannot start")
            assert not plan.requests
            await core.call("session.close", {"id": cid})
        finally:
            await core.shutdown()
        again, _, _, _, _ = await ready_core(tmp_path, cache=cache)
        try:
            await again.call("project.refresh", {"id": str(project)})
            assert project_ids(again) == []
            assert again.chatgpt.sessions.load(native) is None and cache.load_session("cg:" + native) is None
        finally:
            await again.shutdown()

    asyncio.run(scenario())


def test_a_dispatched_first_message_is_recoverable_on_close_and_restart(tmp_path):
    async def scenario():
        core, plan, events, _, _ = await ready_core(tmp_path)
        project = tmp_path / "proj"
        try:
            cid = await new_session(core, events, project)
            native = core._by_cid[cid].controller.session_id
            plan.script([("hold", "response"), *say("not received before closing")])
            await core.call("session.send", {"id": cid, "text": "possibly delivered"})
            await until(lambda: len(plan.requests) == 1)
            checkpoint = core.chatgpt.sessions.load(native)
            assert checkpoint.turns[0].status == "running" and "possibly delivered" in str(checkpoint.items)
            assert checkpoint.title == plan.fake.title_answer
        finally:
            await core.shutdown()
        again, _, _, _, _ = await ready_core(tmp_path)
        try:
            await again.call("project.refresh", {"id": str(project)})
            assert project_ids(again) == ["cg:" + native]
            saved = again.chatgpt.sessions.load(native)
            assert saved.messages == 1 and saved.turns[0].status == "interrupted"
        finally:
            await again.shutdown()

    asyncio.run(scenario())


def test_failure_after_first_output_keeps_the_conversation(tmp_path):
    async def scenario():
        core, plan, events, _, _ = await ready_core(tmp_path)
        project = tmp_path / "proj"
        try:
            cid = await new_session(core, events, project)
            native = core._by_cid[cid].controller.session_id
            plan.script([*say("partial response")[:-1], *fail("invalid_request_error")])
            await core.call("session.send", {"id": cid, "text": "retain this"})
            await idle(core, cid)
            saved = core.chatgpt.sessions.load(native)
            assert saved.turns[0].status == "failed" and "partial response" in str(saved.items)
            assert project_ids(core) == [cid]
            assert not events.of("restore"), "an actual first answer is not returned as an unsent draft"
            await core.call("session.close", {"id": cid})
            await core.call("project.refresh", {"id": str(project)})
            assert project_ids(core) == ["cg:" + native]
        finally:
            await core.shutdown()

    asyncio.run(scenario())


def test_api_missing_key_is_a_draft_until_a_request_is_actually_sent(tmp_path):
    from tests.llm_support import FakeApi
    from tests.test_app_api import api_core, api_model_id, new_session as api_session, says

    async def scenario():
        with FakeApi() as api:
            core, _, events, engine, store = await api_core(tmp_path, api, key=None)
            project = tmp_path / "proj"
            try:
                cid = await api_session(core, events, tmp_path, api_model_id(events))
                native = core._by_cid[cid].controller.session_id
                with pytest.raises(AppError, match="首条消息没有发出"):
                    await core.call("session.send", {"id": cid, "text": "without a key"})
                await idle(core, cid)
                await until(lambda: events.of("restore"))
                assert project_ids(core) == [] and engine.sessions.load(native) is None and api.posts == []
                store._environ["TEST_API_KEY"] = "sk-" + "demo-retry"
                api.script(says("received"))
                await core.call("session.send", {"id": cid, "text": "with a key"})
                await idle(core, cid)
                assert project_ids(core) == [cid] and engine.sessions.load(native).messages == 1
                assert len(api.posts) == 1
            finally:
                await core.shutdown()

    asyncio.run(scenario())
