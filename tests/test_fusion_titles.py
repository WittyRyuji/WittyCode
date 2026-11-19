"""Only the configured Fusion main can name its conversation, outside the conversation history."""
from __future__ import annotations

import asyncio
import json

import pytest

from desktop.core.fusion.registry import FusionRegistry, Policy
from desktop.core.fusion.title import ATTEMPT
from desktop.core.session_title import SYSTEM, prompt as title_prompt
from desktop.core.transcript import TurnStatus
from desktop.core.usage import Tokens
from tests.app_support import MODEL, OTHER_MODEL, ready_core, save_conversation
from tests.fake_responses import say, usage
from tests.fakes import until
from tests.fusion_acp_support import make_guarded_engine
from tests.test_app_core import idle, new_session, sessions_of, talk


def configure(core, main=MODEL, worker=OTHER_MODEL):
    core.settings.fusion_main = main
    core.settings.fusion_workers = [worker]
    core.settings.fusion_light = worker


async def reopen(core, events, project, cid, row):
    ctl = core._by_cid[cid].controller
    await core.call("session.close", {"id": cid})
    await until(lambda: ctl.state.value == "closed")
    opened = await core.call("session.openSaved", {"id": row, "projectId": str(project)})
    await idle(core, opened["id"])
    return opened["id"]


def test_main_title_is_behind_the_main_budget_and_ahead_of_queued_work(tmp_path):
    async def scenario():
        core, plan, events, _, engine = await ready_core(tmp_path)
        try:
            configure(core)
            project = tmp_path / "proj"
            cid = await new_session(core, events, project, fusion=True)
            ctl = core._by_cid[cid].controller
            row = core._row_of(core._by_cid[cid])
            ctl._fusion.update_budget(1, 200_000, accept_unknown=False)
            plan.script([("hold", "main"), *say("主营结果", used=usage(400, 30))], say("后续结果", used=usage(600, 20)))
            plan.fake.title_turn = [("hold", "title"), *say("库存导入去重", used=usage(40, 5))]
            question = "我发现一个错误。\n批量导入库存时重复条目不该计数两次。"
            await core.call("session.send", {"id": cid, "text": question})
            await until(lambda: len(plan.requests) == 1)
            assert not plan.fake.title_requests
            assert (await core.call("session.send", {"id": cid, "text": "再看事务", "how": "queue"}))["queued"]
            plan.release("main")
            await until(lambda: len(plan.fake.title_requests) == 1)
            assert len(plan.requests) == 1 and ctl.transcript.turns[-1].status is TurnStatus.DONE
            request = plan.fake.title_requests[0]
            assert request["model"] == "gpt-6.1-sol" and request["store"] is False and not request.get("tools")
            assert "重复条目" in str(request["input"]) and "再看事务" not in str(request["input"])
            plan.release("title")
            await until(lambda: len(plan.requests) == 2)
            await idle(core, cid)
            assert ctl._fusion.known_tokens() == 1095
            assert ctl.saved.context_used == 620
            assert [entry for entry in ctl.saved.requests if entry.get("kind") == "title"] == [
                {"kind": "title", "model": "gpt-6.1-sol", "usage": Tokens(40, 5).to_json()}
            ]
            assert [t.user_block.text for t in ctl.transcript.turns] == [question, "再看事务"]
            assert "库存导入去重" not in str(ctl.saved.items) and SYSTEM not in str(plan.requests)
            assert engine.sessions.load(ctl.session_id).title == "库存导入去重"
            await until(lambda: sessions_of(events)[cid].get("title") == "库存导入去重")
            await core.call("session.close", {"id": cid})
            await until(lambda: ctl.state.value == "closed")
            await until(lambda: (core._saved_sessions_json().get(row) or {}).get("title") == "库存导入去重")
            cid = (await core.call("session.openSaved", {"id": row, "projectId": str(project)}))["id"]
            await idle(core, cid)
            assert core._title_of(core._by_cid[cid]) == "库存导入去重"
            await talk(core, plan, cid, "再检查日志")
            assert len(plan.fake.title_requests) == 1
        finally:
            await core.shutdown()

    asyncio.run(scenario())


@pytest.mark.parametrize("outcome", ["manual", "invalid", "missing usage", "insufficient", "unknown"])
def test_optional_title_respects_names_and_budget_without_failing_the_main_turn(tmp_path, outcome):
    async def scenario():
        core, plan, events, _, engine = await ready_core(tmp_path)
        try:
            configure(core)
            project = tmp_path / "proj"
            cid = await new_session(core, events, project, fusion=True)
            conv = core._by_cid[cid]
            ctl, row = conv.controller, core._row_of(conv)
            ctl._fusion.update_budget(1, 200_000, accept_unknown=False)
            main = say("Main task completed", used=usage(199_000 if outcome == "insufficient" else 400, 30))
            if outcome == "unknown":
                main[-1]["response"].pop("usage")
            plan.script(main)
            if outcome == "manual":
                plan.fake.title_turn = [("hold", "title"), *say("自动摘要")]
            elif outcome == "invalid":
                plan.fake.title_answer = "无效标题\n两行"
            elif outcome == "missing usage":
                plan.fake.title_turn = say("回答没有完整返回")
                plan.fake.title_turn.pop()  # No terminal event or reliable usage.
            await core.call("session.send", {"id": cid, "text": "核对重复入库明细"})
            if outcome == "manual":
                await until(lambda: bool(plan.fake.title_requests))
                await core.call("session.rename", {"id": cid, "title": "用户指定的名字"})
                plan.release("title")
            await idle(core, cid)
            saved = engine.sessions.load(ctl.session_id)
            assert saved.turns[-1].status == "done" and len(ctl.transcript.turns) == 1
            attempted = outcome in ("manual", "invalid", "missing usage")
            assert len(plan.fake.title_requests) == int(attempted)
            assert (ATTEMPT in saved.origin) == attempted and not saved.title
            assert saved.fusion["unmetered"] == (outcome in ("unknown", "missing usage"))
            expected = "用户指定的名字" if outcome == "manual" else "新会话"
            assert core._title_of(conv) == expected
            if outcome in ("invalid", "manual"):
                cid = await reopen(core, events, project, cid, row)
                assert core._title_of(core._by_cid[cid]) == expected
                await talk(core, plan, cid, "之后再看缓存")
                assert len(plan.fake.title_requests) == 1
        finally:
            await core.shutdown()

    asyncio.run(scenario())


def test_legacy_untitled_fusion_is_only_named_when_a_user_sends_again(tmp_path):
    async def scenario():
        core, plan, events, _, engine = await ready_core(tmp_path)
        try:
            configure(core)
            project = tmp_path / "proj"
            old_id = "legacy"
            save_conversation(engine, project, old_id, "")
            old = engine.sessions.load(old_id)
            first = "修复入库时重复计数"
            old.items[0]["content"][0]["text"] = first
            old.fusion = FusionRegistry(project, Policy(MODEL, (OTHER_MODEL,), OTHER_MODEL, max_calls=0)).to_json()
            engine.sessions.save(old)
            cid = (await core.call("session.openSaved", {"id": "cg:" + old_id, "projectId": str(project), "title": "（无标题）"}))["id"]
            await idle(core, cid)
            assert not plan.fake.title_requests
            ctl = core._by_cid[cid].controller
            assert ctl.transcript.turns[0].user_block.text == first
            current = "核对导入事务边界"
            await talk(core, plan, cid, current)
            (request,) = plan.fake.title_requests
            assert request["input"][0]["content"][0]["text"] == title_prompt(current)
            assert first not in str(request["input"])
            saved = engine.sessions.load(old_id)
            assert saved.title == plan.fake.title_answer and saved.items[:len(old.items)] == old.items
        finally:
            await core.shutdown()

    asyncio.run(scenario())


def test_legacy_devin_question_is_not_resent_to_the_new_main_for_naming(tmp_path):
    async def scenario():
        harness = make_guarded_engine(tmp_path)
        core, plan, events, _, engine = await ready_core(tmp_path, devin_engine=harness.engine)
        try:
            devin = "devin:swe-2-high"
            await until(lambda: core.catalog.find(devin), timeout=20)
            configure(core, devin)
            cid = await new_session(core, events, tmp_path / "proj", fusion=True)
            ctl = core._by_cid[cid].controller
            original = "只交给旧主模型的入库问题"
            assert ctl.send_prompt(original)  # Simulate a legacy turn that did not request a title.
            await idle(core, cid)
            assert ctl.saved.turns[-1].status == "done" and not ctl.fusion_title_attempted

            assert (await core.call("fusion.changeMain", {"id": cid, "model": MODEL}))["ok"]
            assert not plan.fake.title_requests
            current = "检查事务回滚"
            await talk(core, plan, cid, current, "The new main completed the requested check.")
            (main,) = plan.requests
            (title,) = plan.fake.title_requests
            assert main["model"] == title["model"] == "gpt-6.1-sol"
            assert "ACP summary:" in str(main["input"]) and original not in str(main["input"])
            assert title["input"][0]["content"][0]["text"] == title_prompt(current)
            saved = engine.sessions.load(ctl.session_id)
            assert original in str(saved.items) and saved.title == plan.fake.title_answer
            assert len(saved.turns) == 2 and all(turn.status == "done" for turn in saved.turns)
        finally:
            await core.shutdown()

    asyncio.run(scenario())


def test_guarded_devin_main_titles_with_the_same_native_model(tmp_path):
    async def scenario():
        harness = make_guarded_engine(tmp_path)
        core, plan, events, _, engine = await ready_core(tmp_path, devin_engine=harness.engine)
        try:
            model = "devin:swe-2-high"
            await until(lambda: core.catalog.find(model), timeout=20)
            configure(core, model)
            cid = await new_session(core, events, tmp_path / "proj", fusion=True)
            await core.call("session.send", {"id": cid, "text": "请检查库存重复行和事务"})
            await idle(core, cid)
            ctl = core._by_cid[cid].controller
            saved = engine.sessions.load(ctl.session_id)
            assert saved.title == "库存事务与去重" and len(saved.turns) == len(ctl.transcript.turns) == 1
            assert saved.turns[0].status == "done" and "ACP report." in str(saved.items) and saved.title not in str(saved.items)
            assert not plan.requests and not plan.fake.title_requests
            assert len([entry for entry in saved.requests if entry.get("kind") == "title" and entry["model"] == model and entry["usage"]]) == 1
            logs = {path.parent.name: [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
                    for path in (tmp_path / "guarded").glob("*/received.jsonl")}
            assert len(logs) == 2
            (name_log,) = [log for key, log in logs.items() if key.startswith("summary-")]
            for log in logs.values():
                assert [r["params"]["value"] for r in log if r.get("method") == "session/set_config_option"
                        and r["params"]["configId"] == "model"] == ["swe-2-high"]
            (prompt,) = [r["params"]["prompt"] for r in name_log if r.get("method") == "session/prompt"]
            assert len(prompt) == 1 and prompt[0]["text"].startswith(SYSTEM)
            assert "请检查库存重复行和事务" in prompt[0]["text"] and "ACP report." not in prompt[0]["text"]
            assert any(r.get("method") == "session/close" for r in name_log)
        finally:
            await core.shutdown()

    asyncio.run(scenario())


def test_api_fusion_main_uses_its_provider_without_a_plan_account(tmp_path):
    from tests.llm_support import FakeApi
    from tests.test_app_api import api_core, api_model_id, says

    async def scenario():
        with FakeApi() as api:
            core, _, events, engine, _ = await api_core(tmp_path, api)
            try:
                assert engine.cached_account()["auth"] == "signed-out"
                model = api_model_id(events)
                configure(core, model, model)
                api.script(says("Check import transactions."))
                api.title_turn = says("Inventory import validation")
                cid = await new_session(core, events, tmp_path / "proj", fusion=True)
                await core.call("session.send", {"id": cid, "text": "Check duplicate inventory imports"})
                await idle(core, cid)
                ctl = core._by_cid[cid].controller
                assert ctl.saved.title == "Inventory import validation" and len(ctl.saved.turns) == 1
                (title,) = api.title_posts
                assert title[2]["model"] == api.posts[0][2]["model"] == "deep-chat" and not title[2].get("tools")
                assert ctl.saved.usage_models[model] == Tokens(400, 40).to_json()
            finally:
                await core.shutdown()

    asyncio.run(scenario())


@pytest.mark.parametrize("sent", [False, True], ids=["missing-local-key", "posted-no-usage"])
def test_api_title_failure_only_locks_a_finite_budget_if_it_could_have_spent_tokens(tmp_path, monkeypatch, sent):
    from tests.llm_support import FakeApi
    from tests.test_app_api import api_core, api_model_id, says

    async def scenario():
        with FakeApi() as api:
            core, _, events, engine, store = await api_core(tmp_path, api)
            try:
                model = api_model_id(events)
                configure(core, model, model)
                cid = await new_session(core, events, tmp_path / "proj", fusion=True)
                ctl = core._by_cid[cid].controller
                ctl._fusion.update_budget(1, 200_000, accept_unknown=False)
                api.script(says("First main answer."), says("Second main answer."))
                # The fake's default empty title stream records the POST but reports no usage or start event.
                key = store.api_key
                key_lookups = 0

                def lookup(spec):
                    nonlocal key_lookups
                    key_lookups += 1
                    if key_lookups == 2 and not sent:
                        # Only the title's lookup finds the key missing, via the real ProviderStore error path.
                        with monkeypatch.context() as missing:
                            missing.setattr(store, "_environ", {})
                            return key(spec)
                    return key(spec)

                monkeypatch.setattr(store, "api_key", lookup)
                assert (await core.call("session.send", {"id": cid, "text": "Check inventory imports"}))["ok"]
                await idle(core, cid)
                saved = engine.sessions.load(ctl.session_id)
                assert saved.turns[0].status == "done" and "First main answer." in str(saved.items)
                assert not saved.title and len(saved.turns) == len(ctl.transcript.turns) == 1
                assert key_lookups == 2 and len(api.posts) == 1 and len(api.title_posts) == int(sent)
                assert saved.fusion["unmetered"] == sent
                assert len([entry for entry in saved.requests if entry.get("kind") == "title"]) == int(sent)

                assert (await core.call("session.send", {"id": cid, "text": "Check inventory exports"}))["ok"]
                await idle(core, cid)
                assert len(api.title_posts) == int(sent)
                if sent:
                    assert len(api.posts) == 1 and key_lookups == 2
                    assert ctl.transcript.turns[-1].status is TurnStatus.FAILED
                else:
                    assert len(api.posts) == 2 and key_lookups == 3
                    assert ctl.saved.turns[-1].status == "done" and "Second main answer." in str(ctl.saved.items)
            finally:
                await core.shutdown()

    asyncio.run(scenario())
