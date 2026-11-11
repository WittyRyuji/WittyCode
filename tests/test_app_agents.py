"""Sub-agents through the application core: the page is told about them, can open one, and answers what it asks."""

from __future__ import annotations

import asyncio

import pytest

from desktop.core.app_core import AppError
from desktop.core.session_manager import SessionState
from tests.fakes import until
from tests.test_app_core import make_core_with_real_codex


def run(coro):
    return asyncio.run(coro)


def sessions_of(events):
    merged: dict[str, dict] = {}
    for payload in events.of("session"):
        merged.setdefault(payload["id"], {}).update(payload["patch"])
    return merged


async def codex_session(core, events, tmp_path):
    await core.call("app.init")
    await until(lambda: any(m["id"] == "codex:gpt-6-luna" for p in events.of("models") for m in p["models"]), timeout=10)
    cid = (await core.call("session.new", {"projectId": str((tmp_path / "proj").resolve()), "modelId": "codex:gpt-6-luna"}))["id"]
    await until(lambda: core._by_cid[cid].controller.state is SessionState.READY, timeout=10)
    return cid


def test_a_sub_agent_is_listed_opened_and_its_approval_answered_from_the_page(tmp_path):
    async def scenario():
        core, factory, events, harness = make_core_with_real_codex(tmp_path)
        cid = await codex_session(core, events, tmp_path)
        await until(lambda: "agents" in sessions_of(events).get(cid, {}), timeout=5)
        assert sessions_of(events)[cid]["agents"] == [], "none yet"
        await core.call("session.send", {"id": cid, "text": "[subagent] write the file"})

        await until(lambda: any(a["status"] == "waiting" for a in sessions_of(events).get(cid, {}).get("agents", [])), timeout=10)
        meta = sessions_of(events)[cid]
        (agent,) = meta["agents"]
        assert agent["name"] in ("coder_write_sub", "Dirac") and agent["model"] and agent["effort"] == "max"
        assert meta["mark"] == "attention", "the tab asks for attention: something is waiting for the user"
        assert meta["workingLabel"] == "子任务等待你的确认"
        await until(lambda: any(a["role"] == "coder" and a["task"] for a in sessions_of(events)[cid]["agents"]), timeout=5)
        agent = sessions_of(events)[cid]["agents"][0]
        assert agent["name"] == "Dirac" and agent["task"] == "Write sub.txt with the word hi"

        # nothing about it reaches the page until it is opened ...
        assert not events.of("agentTurns")
        # ... and then the whole run does, with its question
        assert (await core.call("agents.watch", {"id": cid, "agentId": agent["id"]})) == {"ok": True}
        first = events.of("agentTurns")[0]
        assert first["id"] == cid and first["agentId"] == agent["id"] and first["all"] is True
        (turn,) = first["turns"]
        assert turn["blocks"][0]["kind"] == "user" and turn["blocks"][0]["who"] == "主智能体"
        (prompt,) = turn["prompts"]
        assert prompt["kind"] == "permission" and prompt["status"] == "pending"
        assert prompt["actions"][0]["command"] == "Set-Content -Path sub.txt -Value hi"

        assert (await core.call("session.answerPermission", {"id": cid, "agentId": "someone-else", "promptId": prompt["id"], "value": "proceed_once"})) == {"ok": False}
        assert (await core.call("session.answerPermission", {"id": cid, "agentId": agent["id"], "promptId": prompt["id"], "value": "proceed_once"})) == {"ok": True}
        await until(lambda: core._by_cid[cid].controller.state is SessionState.READY, timeout=10)
        await until(lambda: sessions_of(events)[cid]["agents"][0]["status"] == "done", timeout=5)
        await until(lambda: sessions_of(events)[cid]["mark"] == "ready", timeout=5)
        last = events.of("agentTurns")[-1]
        assert last["all"] is False and last["turns"][0]["status"] == "done", "the finished run is pushed as a change"
        decisions = [m["result"]["decision"] for m in harness.answers() if "result" in m and "decision" in (m["result"] or {})]
        assert decisions == ["accept"]

        assert (await core.call("agents.unwatch", {"id": cid})) == {"ok": True}
        before = len(events.of("agentTurns"))
        core._by_cid[cid].dirty = True
        core._schedule_flush()
        await asyncio.sleep(0.2)
        assert len(events.of("agentTurns")) == before, "closed panel, nothing pushed"
        await core.shutdown()

    run(scenario())


def test_opening_a_sub_agent_that_is_not_there_is_refused_and_a_droid_conversation_has_none(tmp_path):
    async def scenario():
        core, factory, events, harness = make_core_with_real_codex(tmp_path)
        await core.call("app.init")
        await until(lambda: events.of("models"), timeout=10)
        cid = (await core.call("session.new", {"projectId": str((tmp_path / "proj").resolve())}))["id"]
        await until(lambda: sessions_of(events).get(cid, {}).get("mark") == "ready", timeout=10)
        assert sessions_of(events)[cid]["agents"] == [], "Droid has no sub-agents of this kind"
        with pytest.raises(AppError, match="子任务已经不在了"):
            await core.call("agents.watch", {"id": cid, "agentId": "nope"})
        await core.shutdown()

    run(scenario())
