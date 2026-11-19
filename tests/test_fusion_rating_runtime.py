"""The main agent rates the Fusion Drive records it read before its turn ends (design 8.2, the Q rules)."""
from __future__ import annotations

import asyncio

import pytest

from desktop.core.fusion.drive.retention import Retention
from desktop.core.fusion.protocol import RATE_DECLARATION, parse_ratings
from tests.fake_responses import call, say
from tests.test_fusion_identity_runtime import _brief, _fusion, _output, _turn
from tests.test_fusion_route_runtime import _report, _route

GATES = ("drive", "brief", "report", "inject")
pytestmark = pytest.mark.fusion_gates(*GATES)


async def _record(main):
    # No paths and another name: experts of the Inventory workstream are not given it.
    return await asyncio.to_thread(main._fusion.drive.put, "artifact", "notes", "Unrelated sample", body="There are 4 parts.")


def _read(source, call_id="read"):
    return call("read_record", {"pointer": source.pointers[0]}, call_id=call_id)


def _rate(source, score=1, reason="It answered the question.", call_id="rate"):
    return call("rate_records", {"ratings": [{"record": source.id, "score": score, "reason": reason}]}, call_id=call_id)


def _asks(main):
    return [block["text"] for item in main.saved.items if item.get("role") == "developer" and isinstance(item.get("content"), list)
            for block in item["content"] if "Rate the Fusion Drive records" in block.get("text", "")]


async def _stored(main, source):
    drive = main._fusion.drive
    return await asyncio.to_thread(drive.get, source.id), await asyncio.to_thread(drive.stats, Retention())


def test_a_rating_in_the_turn_moves_the_quality_and_needs_no_reminder(tmp_path):
    async def go():
        async with _fusion(tmp_path, active=GATES) as (main, fake):
            source = await _record(main)
            await _turn(main, fake, _read(source), _rate(source), say("Answered."))
            manifest = next(item for item in fake.requests[0]["tools"] if item["name"] == "rate_records")
            assert all(manifest[key] == value for key, value in RATE_DECLARATION.items())
            assert f"Fusion Drive record ID (rate it with rate_records before this turn ends): {source.id}" in _output(main, "read")
            assert "Recorded 1 rating(s)." in _output(main, "rate")
            record, stats = await _stored(main, source)
            assert record.q == pytest.approx(0.1) and record.updated == source.updated
            assert stats["ratings"] == {-1: 0, 0: 0, 1: 1} and stats["unrated"] == 0
            assert not _asks(main) and len(fake.requests) == 3
    asyncio.run(go())


def test_reading_alone_never_moves_the_quality_and_is_asked_for_once(tmp_path):
    async def go():
        async with _fusion(tmp_path, active=GATES) as (main, fake):
            source = await _record(main)
            await _turn(main, fake, _read(source), say("First answer."), _rate(source, -1, "The count was wrong."), say("Final answer."))
            ask, = _asks(main)
            assert source.id in ask and ask in str(fake.requests[2]["input"]) and len(main.saved.turns) == 1
            record, stats = await _stored(main, source)
            assert record.q == pytest.approx(-0.3) and stats["unrated"] == 0
            # The next turn starts clean: nothing is asked for a record that was already rated.
            await _turn(main, fake, say("Nothing read."))
            assert len(_asks(main)) == 1
    asyncio.run(go())


def test_still_unrated_after_the_reminder_counts_as_neutral(tmp_path):
    async def go():
        async with _fusion(tmp_path, active=GATES) as (main, fake):
            source = await _record(main)
            await _turn(main, fake, _read(source), say("First answer."), say("Final answer."))
            assert len(_asks(main)) == 1 and len(fake.requests) == 3  # Never a second reminder.
            record, stats = await _stored(main, source)
            assert record.q == 0 and stats["unrated"] == 1 and stats["ratings"] == {-1: 0, 0: 1, 1: 0}
    asyncio.run(go())


def test_ratings_follow_the_rules_of_a_report(tmp_path):
    async def go():
        async with _fusion(tmp_path, active=GATES) as (main, fake):
            source = await _record(main)
            other = await asyncio.to_thread(main._fusion.drive.put, "artifact", "other", "Never read", body="Other text.")
            await _turn(main, fake, _read(source), _rate(source, 1, "", call_id="no-reason"), _rate(other, 1, call_id="not-read"),
                        _rate(source, 0, ""), _rate(source, 1, call_id="twice"), say("Answered."))
            assert "needs a reason" in _output(main, "no-reason")
            assert "Missing rating for record" in _output(main, "not-read")  # Only a record that was read can be rated.
            assert "Recorded 1 rating(s)." in _output(main, "rate")
            assert "no record left to rate" in _output(main, "twice")
            record, stats = await _stored(main, source)
            unread, _ = await _stored(main, other)
            assert record.q == 0 and unread.q == 0 and stats["ratings"] == {-1: 0, 0: 1, 1: 0} and not _asks(main)
    assert parse_ratings(None, ["r-1"]) == ({}, ("Provide the ratings as an object.",))
    asyncio.run(go())


def test_missing_verification_and_missing_ratings_share_one_continuation(tmp_path):
    async def go():
        async with _fusion(tmp_path, active=(*GATES, "route", "acceptance")) as (main, fake):
            source = await _record(main)
            await _turn(main, fake, _route(), _read(source), call("delegate_task", _brief(), call_id="delegate"), _report(),
                        say("First answer."), say("Final answer."))
            asks = [block["text"] for item in main.saved.items if item.get("role") == "developer" and isinstance(item.get("content"), list)
                    for block in item["content"] if block.get("text", "").startswith("Fusion verification is incomplete.")]
            assert len(asks) == 1 and "Missing checks: runner" in asks[0] and _asks(main) == asks
            _, stats = await _stored(main, source)
            assert stats["unrated"] == 1
    asyncio.run(go())


def test_without_the_inject_gate_the_main_agent_is_not_asked_to_rate(tmp_path):
    async def go():
        async with _fusion(tmp_path, active=("drive", "brief", "report")) as (main, fake):
            source = await _record(main)
            await _turn(main, fake, _read(source), say("Answered."))
            assert "rate_records" not in {item["name"] for item in fake.requests[0]["tools"]}
            assert "Fusion Drive record ID" not in _output(main, "read") and "There are 4 parts." in _output(main, "read")
            assert not _asks(main) and len(fake.requests) == 2
            _, stats = await _stored(main, source)
            assert stats["unrated"] == 0 and stats["ratings"] == {-1: 0, 0: 0, 1: 0}
    asyncio.run(go())


def test_experts_never_get_the_main_agents_rating_tool(tmp_path):
    async def go():
        async with _fusion(tmp_path, active=GATES) as (main, fake):
            await _turn(main, fake, call("delegate_task", _brief(read_only=True, paths=[]), call_id="delegate"), _report(), say("Reviewed."))
            expert_tools = {item["name"] for item in fake.requests[1]["tools"]}
            assert "submit_report" in expert_tools and "rate_records" not in expert_tools
    asyncio.run(go())
