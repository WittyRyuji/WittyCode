"""The recall tool contract and local backend transitions of an archived session."""
from __future__ import annotations

import asyncio

import pytest

from desktop.core.harness import recall_tool
from desktop.core.harness.loop import DENY, Call
from desktop.core.session_archive import SessionArchive
from tests.app_support import ready_core
from tests.chatgpt_support import finish_turn
from tests.fake_responses import FakeResponses, say
from tests.test_chatgpt_controller import opened
from tests.test_fusion_switch import SOL, choose, configure


def test_first_three_requests_offer_the_same_tools_including_recall(tmp_path):
    async def scenario():
        with FakeResponses() as fake:
            fake.script(say("First reply."), say("Second reply."), say("Third reply."))
            engine, ctl = await opened(tmp_path, fake)
            try:
                assert ctl._archive_pending and ctl._archive_binding is None
                for prompt in ("First question", "Second question", "Third question"):
                    assert ctl.send_prompt(prompt)
                    await finish_turn(ctl)
                assert ctl._archive_binding is not None and not ctl._archive_broken
                first, second, third = [request["tools"] for request in fake.requests]
                assert first == second == third
                assert recall_tool.NAME in {tool["name"] for tool in first}
            finally:
                await engine.close_all()
    asyncio.run(scenario())


def test_recall_is_offered_but_refused_before_the_archive_is_ready(tmp_path):
    async def scenario():
        with FakeResponses() as fake:
            engine, ctl = await opened(tmp_path, fake)
            try:
                tools = await ctl._ensure_tools()
                assert ctl._archive_pending and ctl._archive_binding is None
                assert tools.known(recall_tool.NAME)
                assert recall_tool.NAME in {tool["name"] for tool in tools.declarations()}
                call = Call("item", "call", recall_tool.NAME, "", {"question": "earlier decision"})
                verdict = tools.check(call)
                assert (verdict.verdict, verdict.reason) == (DENY, recall_tool.UNAVAILABLE)
                outcome = await tools.run(call)
                assert outcome.is_error and recall_tool.UNAVAILABLE in outcome.output
                assert ctl._archive_binding is None, "a refused call must not open the archive"
            finally:
                await engine.close_all()
    asyncio.run(scenario())


@pytest.mark.parametrize("first_fusion", [False, True], ids=["plain-first", "fusion-first"])
def test_a_local_mode_round_trip_archives_each_event_once_and_reuses_the_original_instance(tmp_path, first_fusion):
    async def scenario():
        core, fake, _, _, _ = await ready_core(tmp_path)
        try:
            configure(core)
            first_mode, other_mode = ("fusion", SOL) if first_fusion else (SOL, "fusion")
            cid = (await core.call("session.new", {"projectId": str(tmp_path / "proj"), "modelId": SOL}))["id"]
            ctl = core._by_cid[cid].controller
            if first_fusion:
                assert (await choose(core, cid, "fusion"))["ok"]
                assert ctl._archive_binding is None

            async def turn(i):
                fake.script(say(f"Reply in segment {i}."))
                assert (await core.call("session.send", {"id": cid, "text": f"Question in segment {i}"}))["ok"]
                await finish_turn(ctl)
                assert ctl.saved.turns[-1].status == "done" and not ctl._archive_broken
                return len(ctl.saved.items)

            first_end = await turn(1)
            binding = ctl._archive_binding
            original = binding.instance
            assert (await choose(core, cid, other_mode))["ok"]
            assert not ctl._archive_broken
            other = binding.instance
            assert other.iid != original.iid and other.native_id == original.native_id == ctl.session_id
            second_end = await turn(2)
            assert (await choose(core, cid, first_mode))["ok"]
            assert not ctl._archive_broken and binding.instance == original
            third_end = await turn(3)
            assert 0 < first_end < second_end < third_end

            archive = SessionArchive.open(binding.archive.root, binding.archive.sid)
            backends = ("fusion", "gpt") if first_fusion else ("gpt", "fusion")
            assert [(t["backend"], t["iid"], t["result"]) for t in archive.timeline()] == [
                (backends[0], original.iid, "new"), (backends[1], other.iid, "new"),
                (backends[0], original.iid, "resumed"),
            ]
            first_events, other_events = archive.events(original), archive.events(other)
            assert [e["index"] for e in first_events] == [*range(first_end), *range(second_end, third_end)]
            assert [e["index"] for e in other_events] == list(range(first_end, second_end))
            events = sorted(first_events + other_events, key=lambda e: e["index"])
            assert [e["item"] for e in events] == ctl.saved.items
            assert len({e["id"] for e in events}) == third_end
        finally:
            await core.shutdown()
    asyncio.run(scenario())
