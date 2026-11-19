from __future__ import annotations

import asyncio
import threading
import time

import pytest

from desktop.core.fusion.drive.store import Drive
from desktop.core.transcript import NoticeBlock
from tests.chatgpt_support import finish_turn
from tests.fake_responses import call, say
from tests.fakes import until
from tests.test_fusion_identity_runtime import _brief, _fusion, _output, _turn
from tests.test_fusion_route_runtime import _report

_ACTIVE = ("drive", "brief", "report")
pytestmark = pytest.mark.fusion_gates(*_ACTIVE)


def _peer(main, session="other-session", **kwargs):
    return Drive(main.manager.root.parent / "fusion-drive", main.project, session, **kwargs)


def _read(record, call_id):
    return call("read_record", {"pointer": record.pointers[0]}, call_id=call_id)


@pytest.mark.parametrize("shared", [False, True])
def test_pointer_sharing_only_allows_facts_and_always_allows_own_and_map(tmp_path, shared):
    async def go():
        async with _fusion(tmp_path, active=_ACTIVE) as (main, fake):
            main.manager.settings.fusion_drive_shared = shared
            peer = _peer(main)
            own = main._fusion.drive.put("report", "own", "Own report.", body="Own stock report.")
            run = peer.put("run", "run", "Stored check.", body="Historical stock check.", fingerprint="outdated")
            report = peer.put("report", "report", "Peer report.", body="Private stock report.")
            map_entry = peer.put("map", "map:stock", "Stock module.", body="Stock map details.")
            await _turn(main, fake, _read(own, "own"), _read(run, "run"), _read(report, "report"),
                        _read(map_entry, "map"), say("Reviewed."))
            assert "Own stock report." in _output(main, "own") and "Stock map details." in _output(main, "map")
            result = _output(main, "run")
            assert ("Historical stock check." in result) == shared
            if not shared:
                assert "belongs to another conversation" in result and "sharing is off" in result
            assert "belongs to another conversation" in _output(main, "report")
            assert "Private stock report." not in _output(main, "report")
    asyncio.run(go())


def _delegate(call_id="delegate", **kwargs):
    return call("delegate_task", _brief(**kwargs), call_id=call_id)


def _full_notices(main):
    return [block.text for turn in main.transcript.turns for block in turn.blocks
            if isinstance(block, NoticeBlock) and "Fusion Drive 已满" in block.text]


async def _quota_prompt(main):
    await until(lambda: main.transcript.pending_prompts())
    prompt, = main.transcript.pending_prompts()
    question, = prompt.questions
    assert prompt.tool_call_id == "delegate" and "已达到容量上限" in question.question
    assert question.options == ("清理列出的会话", "保留全部记录")
    return prompt, question


def test_quota_asks_before_brief_and_cleans_only_listed_sessions(tmp_path):
    async def go():
        async with _fusion(tmp_path, active=_ACTIVE) as (main, fake):
            fusion = main._fusion
            drive = fusion.drive
            main.manager.settings.fusion_drive_shared = True
            now = time.time()
            old = _peer(main, clock=lambda: now - 20 * 86400).put("run", "old", "Older check.", body="Older log.\n" * 3000)
            newer = _peer(main, "newer-session", clock=lambda: now - 10 * 86400).put("run", "newer", "Newer check.", body="Newer log.\n" * 3000)
            opened = _peer(main, "active-session").put("brief", "open", "Work in progress.", body="Open brief.", open=True)
            own = drive.put("run", "mine", "Current result.", body="Current check.")
            before = drive.usage()
            main.manager.settings.fusion_drive_max_gb = (before - 1) / 1024**3
            fake.script(_delegate(), _report(), _read(old, "cleaned"), say("Reviewed."))
            assert main.send_prompt("Count items.")
            prompt, question = await _quota_prompt(main)
            assert question.question.index("other-session") < question.question.index("newer-session")
            assert fusion.owner not in question.question and "active-session" not in question.question and "字节" in question.question
            assert time.strftime("%Y-%m-%d", time.localtime(old.updated)) in question.question
            assert len(fake.requests) == 1 and drive.read(old.pointers[0]).startswith("Older log.")
            assert all(item.record.kind != "brief" or item.record.session != fusion.owner
                       for item in drive.candidates(shared=False, retention=fusion.retention()))
            assert main.answer_question(prompt.id, [question.options[0]])
            await finish_turn(main)
            assert "was cleaned" in _output(main, "cleaned") and "Fusion Drive report" in _output(main, "delegate")
            assert drive.get(old.id).status == drive.get(newer.id).status == "cleaned" and drive.get(old.id).summary == old.summary
            assert drive.get(opened.id) == opened and drive.get(own.id) == own and drive.usage() < before
            assert not _full_notices(main) and len(main.transcript.turns[-1].prompts) == 1
    asyncio.run(go())


@pytest.mark.parametrize("skip", [False, True])
def test_quota_declined_or_skipped_pauses_writes_without_asking_again(tmp_path, skip):
    async def go():
        async with _fusion(tmp_path, active=_ACTIVE) as (main, fake):
            old = _peer(main).put("run", "old", "Do not delete.", body="Keep this stock check.")
            fusion = main._fusion
            main.manager.settings.fusion_drive_max_gb = (fusion.drive.usage() - 1) / 1024**3
            fake.script(_delegate(), _report(), _delegate("second"), _report(call_id="report-2"), say("Reviewed."))
            assert main.send_prompt("Count items twice.")
            prompt, question = await _quota_prompt(main)
            assert main.answer_question(prompt.id, None if skip else [question.options[1]])
            await finish_turn(main)
            await _turn(main, fake, _delegate("third"), _report(call_id="report-3"), say("Reviewed."))
            assert len(_full_notices(main)) == 1 and sum(len(turn.prompts) for turn in main.transcript.turns) == 1
            for ident in ("delegate", "second", "third"):
                assert "Inventory checked." in _output(main, ident) and "Fusion Drive report" not in _output(main, ident)
            assert fusion.drive.get(old.id) == old and fusion.drive.read(old.pointers[0]) == "Keep this stock check."
            assert not fusion.drive.candidates(shared=False, retention=fusion.retention())
    asyncio.run(go())


def test_under_quota_checks_off_loop_once_per_turn(tmp_path, monkeypatch):
    async def go():
        async with _fusion(tmp_path, active=_ACTIVE) as (main, fake):
            on_loop = threading.get_ident()
            threads = []

            def usage(drive):
                threads.append(threading.get_ident())
                return 0

            monkeypatch.setattr(Drive, "usage", usage)
            await _turn(main, fake, _delegate(), _report(), _delegate("second"), _report(call_id="report-2"), say("Reviewed."))
            assert len(threads) == 1 and threads[0] != on_loop
            await _turn(main, fake, _delegate("third"), _report(call_id="report-3"), say("Reviewed."))
            assert len(threads) == 2 and not _full_notices(main)
            assert all(not turn.prompts for turn in main.transcript.turns)
    asyncio.run(go())


@pytest.mark.parametrize("enabled", [False, True])
def test_auto_delete_runs_once_off_loop_before_quota_and_protects_open_and_map(tmp_path, monkeypatch, enabled):
    async def go():
        async with _fusion(tmp_path, active=_ACTIVE) as (main, fake):
            now = time.time()
            peer = _peer(main, clock=lambda: now - 90 * 86400)
            expired = peer.put("run", "old", "Old stock check.", body="Expired log.")
            opened = peer.put("brief", "open", "Ongoing task.", body="Open task details.", open=True)
            map_entry = peer.put("map", "map:stock", "Stock module.", body="Stock map details.")
            main.manager.settings.fusion_drive_auto_delete = enabled
            on_loop = threading.get_ident()
            seen = []
            auto_delete, usage = Drive.auto_delete, Drive.usage

            def tracked_delete(drive, retention):
                seen.append(("auto", threading.get_ident()))
                return auto_delete(drive, retention)

            def tracked_usage(drive):
                seen.append(("quota", threading.get_ident()))
                return usage(drive)

            monkeypatch.setattr(Drive, "auto_delete", tracked_delete)
            monkeypatch.setattr(Drive, "usage", tracked_usage)
            await _turn(main, fake, _delegate(), _report(), _delegate("second"), _report(call_id="report-2"), say("Reviewed."))
            assert [event for event, _ in seen] == (["auto", "quota"] if enabled else ["quota"])
            assert all(thread != on_loop for _, thread in seen)
            assert peer.get(expired.id) == (None if enabled else expired)
            assert peer.get(opened.id) == opened and peer.get(map_entry.id) == map_entry
    asyncio.run(go())


@pytest.mark.parametrize("cleanable", [False, True])
def test_full_drive_stays_paused_when_nothing_can_free_enough_space(tmp_path, cleanable):
    async def go():
        async with _fusion(tmp_path, active=_ACTIVE) as (main, fake):
            fusion = main._fusion
            old = _peer(main).put("run", "old", "Old stock check.", body="Old log.") if cleanable else None
            main.manager.settings.fusion_drive_max_gb = 10 / 1024**3  # Less than the empty SQLite index.
            if cleanable:
                fake.script(_delegate(), _report(), say("Reviewed."))
                assert main.send_prompt("Count items.")
                prompt, question = await _quota_prompt(main)
                assert main.answer_question(prompt.id, [question.options[0]])
                await finish_turn(main)
                assert fusion.drive.get(old.id).status == "cleaned"
            else:
                await _turn(main, fake, _delegate(), _report(), say("Reviewed."))
                assert not main.transcript.turns[-1].prompts
            assert fusion.drive_writable is False and len(_full_notices(main)) == 1
            assert "Inventory checked." in _output(main, "delegate") and "Fusion Drive report" not in _output(main, "delegate")
    asyncio.run(go())
