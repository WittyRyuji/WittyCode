"""Model selection is durable intent; only a subsequent send may compact or use it."""
from __future__ import annotations

import asyncio
from copy import deepcopy

import pytest

from desktop.core.app_core import AppError
from desktop.core.attachments import text_attachment
from tests.app_support import ready_core
from tests.chatgpt_support import finish_turn
from tests.fake_responses import fail, say
from tests.fakes import until

SOL, LUNA = "chatgpt:gpt-6.1-sol", "chatgpt:gpt-6-luna"


async def select(core, cid, model):
    return await core.call("session.choose", {"id": cid, "field": "model", "value": model})


async def conversation(core, fake, tmp_path):
    cid = (await core.call("session.new", {"projectId": str(tmp_path / "proj")}))["id"]
    main = core._by_cid[cid].controller
    fake.script(say("Original tool evidence and completed work."))
    await core.call("session.send", {"id": cid, "text": "Inspect inventory constraints"})
    await finish_turn(main)
    return cid, main


def test_fusion_selection_waits_for_send_and_does_not_create_a_compact_turn(tmp_path):
    async def go():
        core, fake, events, _, _ = await ready_core(tmp_path)
        try:
            core.settings.fusion_main, core.settings.fusion_workers, core.settings.fusion_light = LUNA, [SOL], SOL
            cid, main = await conversation(core, fake, tmp_path)
            original = deepcopy(main.saved.items)
            fake.script(say("SUMMARY: inspected inventory; verify constraints next."), say("Continued once."))
            assert (await select(core, cid, "fusion"))["ok"]
            assert len(fake.requests) == 1, "selecting a model must not send a summary request"
            assert main._fusion is None and main.model == "gpt-6.1-sol"
            meta = core._session_meta(core._by_cid[cid])
            assert meta["fusion"] and meta["settings"]["model"] == LUNA
            # Changing global defaults must not silently replace a selection already made in this session.
            core.settings.fusion_main = SOL
            assert (await core.call("session.send", {"id": cid, "text": "Continue with inventory"}))["ok"]
            await finish_turn(main)
            assert main._fusion.registry.policy.main == LUNA
            assert [r["model"] for r in fake.requests] == ["gpt-6.1-sol", "gpt-6.1-sol", "gpt-6-luna"]
            assert main.saved.items[:len(original)] == original
            assert [t.user_block.text for t in main.transcript.turns if t.user_block] == ["Inspect inventory constraints", "Continue with inventory"]
            assert "SUMMARY:" in str(fake.requests[-1]["input"])
            assert not core._by_cid[cid].pending_model
            assert all("/compact" not in str(p.get("turns")) for p in events.of("turns"))
        finally:
            await core.shutdown()
    asyncio.run(go())


@pytest.mark.parametrize("outcome", ["failure", "empty", "stop"])
def test_failed_send_keeps_pending_target_draft_attachments_and_history_without_replay(tmp_path, outcome):
    async def go():
        core, fake, _, _, _ = await ready_core(tmp_path)
        try:
            cid, main = await conversation(core, fake, tmp_path)
            before = deepcopy(main.saved.items)
            attachment = text_attachment("notes.txt", "Draft notes")
            core._by_cid[cid].drafts.append(attachment)
            fake.script(fail("invalid_request", "Summary unavailable") if outcome == "failure" else say("") if outcome == "empty" else [("hold", "summary"), *say("Summary")])
            await select(core, cid, LUNA)
            task = asyncio.create_task(core.call("session.send", {"id": cid, "text": "Keep this unsent draft"}))
            if outcome == "stop":
                await until(lambda: len(fake.requests) == 2)
                await core.call("session.stop", {"id": cid})
            with pytest.raises(AppError):
                await task
            conv = core._by_cid[cid]
            assert conv.pending_model["model"] == LUNA
            assert core._session_meta(conv)["settings"]["model"] == LUNA
            assert main.model == "gpt-6.1-sol" and main.saved.items == before
            assert conv.drafts == [attachment] and not conv.queue
            assert [t.user_block.text for t in main.transcript.turns if t.user_block] == ["Inspect inventory constraints"]
            assert len(fake.requests) == 2 and not conv.switching and conv.switch_task is None
            # No background continuation is allowed after the failure/stop.
            await asyncio.sleep(0.05)
            assert len(fake.requests) == 2
        finally:
            fake.release("summary")
            await core.shutdown()
    asyncio.run(go())


def test_reselection_replaces_or_cancels_pending_target_without_requests(tmp_path):
    async def go():
        core, fake, _, _, _ = await ready_core(tmp_path)
        try:
            core.settings.fusion_main, core.settings.fusion_workers, core.settings.fusion_light = LUNA, [SOL], SOL
            cid, main = await conversation(core, fake, tmp_path)
            await select(core, cid, "fusion")
            await select(core, cid, LUNA)
            assert not core._session_meta(core._by_cid[cid])["fusion"]
            await select(core, cid, SOL)
            assert not core._by_cid[cid].pending_model and len(fake.requests) == 1
            fake.script(say("No handoff required."))
            await core.call("session.send", {"id": cid, "text": "Stay with the original model"})
            await finish_turn(main)
            assert len(fake.requests) == 2 and main.model == "gpt-6.1-sol"
        finally:
            await core.shutdown()
    asyncio.run(go())


def test_pending_selection_survives_close_and_reopen_without_automatic_requests(tmp_path):
    async def go():
        core, fake, _, _, _ = await ready_core(tmp_path)
        try:
            cid, main = await conversation(core, fake, tmp_path)
            row = core._row_of(core._by_cid[cid])
            await select(core, cid, LUNA)
            await core.call("session.close", {"id": cid})
            await until(lambda: main.state.value == "closed")
            cid = (await core.call("session.openSaved", {"projectId": str(tmp_path / "proj"), "id": row}))["id"]
            conv = core._by_cid[cid]
            await until(lambda: conv.controller.state.value == "ready")
            assert conv.pending_model["model"] == LUNA and len(fake.requests) == 1
            assert core._session_meta(conv)["settings"]["model"] == LUNA
            fake.script(say("SUMMARY: original work completed."), say("Continued after reopen."))
            await core.call("session.send", {"id": cid, "text": "Continue after reopen"})
            await finish_turn(conv.controller)
            assert len(fake.requests) == 3 and not conv.pending_model
        finally:
            await core.shutdown()
    asyncio.run(go())


def test_switch_compaction_can_reuse_an_existing_complete_summary_without_another_request(tmp_path):
    async def go():
        core, fake, _, _, _ = await ready_core(tmp_path)
        try:
            cid, main = await conversation(core, fake, tmp_path)
            main.saved.compaction = {"upto": len(main.saved.items), "summary": "Existing complete summary.", "at": 1, "after": 20}
            await select(core, cid, LUNA)
            fake.script(say("Use the complete summary."))
            await core.call("session.send", {"id": cid, "text": "Continue from existing summary"})
            await finish_turn(main)
            assert len(fake.requests) == 2 and fake.requests[-1]["model"] == "gpt-6-luna"
        finally:
            await core.shutdown()
    asyncio.run(go())


def test_pending_state_write_failure_never_accepts_a_message_before_reporting_failure(tmp_path, monkeypatch):
    async def go():
        core, fake, _, _, _ = await ready_core(tmp_path)
        try:
            cid, main = await conversation(core, fake, tmp_path)
            await select(core, cid, LUNA)
            save = core.history.save
            def fail_clear(row, entry):
                if row.startswith("pending-switch:") and entry.get("target") is None:
                    raise OSError("test disk failure")
                return save(row, entry)
            monkeypatch.setattr(core.history, "save", fail_clear)
            fake.script(say("Original constraints summarized."))
            with pytest.raises(AppError, match="消息没有发出"):
                await core.call("session.send", {"id": cid, "text": "Send exactly once"})
            assert core._by_cid[cid].pending_model is not None and len(fake.requests) == 2
            assert [t.user_block.text for t in main.transcript.turns if t.user_block] == ["Inspect inventory constraints"]
            monkeypatch.setattr(core.history, "save", save)
            fake.script(say("Sent once after the storage recovered."))
            await core.call("session.send", {"id": cid, "text": "Send exactly once"})
            await finish_turn(main)
            assert len(fake.requests) == 3
            assert [t.user_block.text for t in main.transcript.turns if t.user_block].count("Send exactly once") == 1
        finally:
            await core.shutdown()
    asyncio.run(go())
