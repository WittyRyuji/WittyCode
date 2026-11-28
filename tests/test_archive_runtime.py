"""The session archive wired into the production controller (``chatgpt/archive_binding`` + ``harness/recall_tool``):
a new conversation mirrors its durable log into a runtime-bound archive at its checkpoints and moves its output
store there, a verified compaction commits a snapshot generation while every failure path aborts or refuses, a
reopened session rebinds through ``saved.origin`` (old sessions are never migrated), and ``recall_history`` is
declared for sessions that archive and callable only once an archive is usable."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from desktop.core import session_archive
from desktop.core.chatgpt.archive_binding import ArchiveBinding
from desktop.core.chatgpt.controller import ARCHIVE_ORIGIN
from desktop.core.chatgpt.responses import user_message
from desktop.core.harness import compact, recall_tool
from desktop.core.harness.loop import Call
from desktop.core.harness.store import SavedTurn
from desktop.core.session_archive import ArchiveError
from desktop.core.transcript import Turn, TurnStatus
from tests.chatgpt_support import ACCESS_TOKEN, finish_turn, make_engine
from tests.fake_responses import FakeResponses, call, say, usage
from tests.test_chatgpt_compaction import assistant, fill, is_summary_request, roomy, WORDS
from tests.test_chatgpt_controller import opened, project, run


def archive_root(tmp_path: Path) -> Path:
    return tmp_path / "session"


def manifest_of(tmp_path: Path, sid: str) -> dict[str, Any]:
    return session_archive.read_manifest(archive_root(tmp_path), sid)


def declared(controller: Any) -> list[str]:
    assert controller._tools is not None
    return [item["name"] for item in controller._tools.declarations()]


def failed_error(turn: Turn) -> str:
    return " ".join(block.message for block in turn.blocks if block.kind == "error")


# ----------------------------------------------------------------------------------------------- lifecycle


def test_a_new_conversation_archives_its_log_and_moves_its_output_store(tmp_path):
    async def go() -> None:
        with FakeResponses(token=ACCESS_TOKEN) as fake:
            fake.script(say("the answer"))
            engine, controller = await opened(tmp_path, fake)
            assert controller._archive_binding is None and controller._archive_pending, "the archive waits for real history"
            controller.send_prompt("first question")
            await finish_turn(controller)

            binding = controller._archive_binding
            assert binding is not None, "the first durable checkpoint created the archive"
            assert not controller._archive_pending and not controller._archive_broken
            sid = controller.saved.origin[ARCHIVE_ORIGIN]
            assert sid == binding.archive.sid
            manifest = manifest_of(tmp_path, sid)
            (identity,) = manifest["identities"]
            assert identity["backend"] == "gpt" and identity["connection"] == "gpt" and identity["native"] == controller.session_id
            events = (archive_root(tmp_path) / sid / "gpt" / identity["iid"] / "events.jsonl").read_text(encoding="utf-8").strip().splitlines()
            assert len(events) == len(controller.saved.items), "the whole durable log is mirrored"
            assert controller._outputs.root == archive_root(tmp_path) / sid / "output", "the session's output store lives in the archive"
            await engine.close_all()

    run(go())


def test_a_reopened_session_rebinds_and_an_old_one_is_never_migrated(tmp_path):
    async def go() -> None:
        with FakeResponses(token=ACCESS_TOKEN) as fake:
            fake.script(say("the answer"), say("second answer"))
            folder = project(tmp_path)
            engine = make_engine(tmp_path, fake)
            controller = await engine.open_new(folder, model="gpt-6.1-sol", reasoning="medium")
            controller.send_prompt("first question")
            await finish_turn(controller)
            sid = controller.saved.origin[ARCHIVE_ORIGIN]
            session_id = controller.session_id
            await engine.close(controller)

            reopened = await engine.open_saved(folder, session_id)
            assert reopened._archive_binding is not None and reopened._archive_binding.archive.sid == sid
            assert reopened._outputs.root == archive_root(tmp_path) / sid / "output"
            tools = await reopened._ensure_tools()
            assert tools.context.outputs is reopened._outputs, "the bound tools write outputs into the archive"
            await engine.close(reopened)

            # A saved session without the marker must never be migrated onto an archive.
            saved = engine.sessions.load(session_id)
            assert saved is not None and saved.origin.pop(ARCHIVE_ORIGIN, None) == sid
            engine.sessions.save(saved)
            legacy = await engine.open_saved(folder, session_id)
            assert legacy._archive_binding is None and not legacy._archive_pending
            await legacy._ensure_tools()
            assert "recall_history" not in declared(legacy)
            await engine.close_all()

    run(go())


# ----------------------------------------------------------------------------------------------- compaction


def test_a_verified_compaction_commits_a_snapshot_generation(tmp_path):
    async def go() -> None:
        with FakeResponses(token=ACCESS_TOKEN) as fake:
            fake.script(say("SUMMARY OF THE EARLIER PART", used=usage(900, 40)), say("Here is the answer."))
            engine, controller = await opened(tmp_path, fake)
            fill(controller)
            engine._windows["gpt-6.1-sol"] = await roomy(controller)
            logged = len(controller.saved.items)
            controller.send_prompt("next question")
            await finish_turn(controller)
            assert len(fake.requests) == 2 and is_summary_request(fake.requests[0])
            record = controller.saved.compaction
            assert record and 0 < record["upto"] <= logged

            sid = controller.saved.origin[ARCHIVE_ORIGIN]
            manifest = manifest_of(tmp_path, sid)
            assert manifest["latest"] == 1 and len(manifest["generations"]) == 1
            entry = manifest["generations"][0]
            assert entry["evicted"], "the records the summary replaced are the generation's evicted set"
            dump = session_archive.read_generation(archive_root(tmp_path), sid, 1)
            assert len(dump["records"]) == logged + 1, "the staged pre-compaction view (log plus the new question) is committed whole"
            assert dump["summary"]["upto"] == record["upto"] and dump["summary"]["summary"] == record["summary"]
            await engine.close_all()

    run(go())


def test_a_failed_stage_blocks_the_summary_before_it_is_sent(tmp_path, monkeypatch):
    async def go() -> None:
        with FakeResponses(token=ACCESS_TOKEN) as fake:
            fake.script(say("unused"))
            engine, controller = await opened(tmp_path, fake)
            fill(controller)
            engine._windows["gpt-6.1-sol"] = await roomy(controller)

            def boom(self: ArchiveBinding, *args: Any, **kwargs: Any) -> Any:
                raise ArchiveError("injected stage failure")

            monkeypatch.setattr(ArchiveBinding, "prepare", boom)
            controller.send_prompt("next question")
            await finish_turn(controller)
            assert not fake.requests, "a rejected staged snapshot must stop the summary request"
            turn = controller.transcript.turns[-1]
            assert turn.status is TurnStatus.FAILED and "归档" in failed_error(turn)
            assert controller.saved.compaction == {}
            sid = controller.saved.origin[ARCHIVE_ORIGIN]
            assert manifest_of(tmp_path, sid)["latest"] is None, "no generation was committed"
            await engine.close_all()

    run(go())


def test_a_failed_publish_aborts_and_keeps_the_old_state(tmp_path, monkeypatch):
    async def go() -> None:
        with FakeResponses(token=ACCESS_TOKEN) as fake:
            fake.script(say("SUMMARY", used=usage(900, 40)))
            engine, controller = await opened(tmp_path, fake)
            fill(controller)
            engine._windows["gpt-6.1-sol"] = await roomy(controller)

            def boom(self: ArchiveBinding, *args: Any, **kwargs: Any) -> Any:
                raise ArchiveError("injected publish failure")

            monkeypatch.setattr(ArchiveBinding, "publish", boom)
            controller.send_prompt("next question")
            await finish_turn(controller)
            assert len(fake.requests) == 1 and is_summary_request(fake.requests[0]), "the summary ran but its commit failed"
            turn = controller.transcript.turns[-1]
            assert turn.status is TurnStatus.FAILED and "归档" in failed_error(turn)
            assert controller.saved.compaction == {}, "the compaction record was never swapped in"
            sid = controller.saved.origin[ARCHIVE_ORIGIN]
            manifest = manifest_of(tmp_path, sid)
            assert manifest["latest"] is None and not manifest["generations"], "a failed publish commits no generation"
            assert not list((archive_root(tmp_path) / sid / "gpt").glob("*/dumps/*.staging")), "the aborted staging was dropped"
            await engine.close_all()

    run(go())


def test_a_broken_archive_blocks_compaction_but_not_the_conversation(tmp_path, monkeypatch):
    async def go() -> None:
        with FakeResponses(token=ACCESS_TOKEN) as fake:
            fake.script(say("unused"))
            engine, controller = await opened(tmp_path, fake)
            fill(controller)
            engine._windows["gpt-6.1-sol"] = await roomy(controller)

            def boom(self: ArchiveBinding, *args: Any, **kwargs: Any) -> Any:
                raise ArchiveError("injected sync failure")

            monkeypatch.setattr(ArchiveBinding, "sync", boom)
            controller.send_prompt("next question")
            await finish_turn(controller)
            assert not fake.requests, "a broken archive must stop the summary request, not compact as usual"
            assert controller._archive_broken
            turn = controller.transcript.turns[-1]
            assert turn.status is TurnStatus.FAILED and "归档" in failed_error(turn)
            assert controller.saved.compaction == {}
            await engine.close_all()

    run(go())


# ----------------------------------------------------------------------------------------------- the recall tool


def test_recall_declaration_does_not_follow_archive_availability(tmp_path):
    async def go() -> None:
        with FakeResponses(token=ACCESS_TOKEN) as fake:
            fake.script(say("the answer"))
            engine, controller = await opened(tmp_path, fake)
            tools = await controller._ensure_tools()
            offered = tools.declarations()
            assert "recall_history" in declared(controller), "the pending archive declares the tool"
            bound = tools.context.history_recall
            assert bound is not None and not bound.enabled
            controller.send_prompt("first question")
            await finish_turn(controller)
            assert bound.enabled and tools.declarations() == offered
            controller._archive_binding = None
            assert not bound.enabled and tools.declarations() == offered, "a lost binding doesn't change the contract"
            await engine.close_all()

    run(go())


# ----------------------------------------------------------------------------------------------- the fusion handoff


def test_the_acp_handoff_binds_the_real_native_session_and_moves_the_store(tmp_path):
    """The ACP native id only exists after the handshake: the archive identity must map that real id
    (never our logical session id as a stand-in), and the handoff's retained text must land in the
    archive's own session-scoped store so references written mid-turn stay readable."""
    from desktop.core.fusion.registry import Policy
    from tests.app_support import ready_core
    from tests.fakes import until
    from tests.fusion_acp_support import make_guarded_engine

    async def go() -> None:
        harness = make_guarded_engine(tmp_path)
        core, _, _, _, _ = await ready_core(tmp_path, devin_engine=harness.engine)
        try:
            await until(lambda: any(m["source"] == "devin" for m in core.catalog.models))
            cid = (await core.call("session.new", {"projectId": str(tmp_path / "proj")}))["id"]
            main = core._by_cid[cid].controller
            main.enable_fusion(Policy("devin:swe-2-high", ("chatgpt:gpt-6-luna",), "chatgpt:gpt-6-luna", 0))
            assert main.send_prompt("run through the bridge")
            await finish_turn(main, timeout=30)
            assert main.saved.turns[-1].status == "done", main.saved.turns[-1].error

            binding = main._archive_binding
            assert binding is not None and not main._archive_pending and not main._archive_broken
            native = main.saved.origin["fusionAcp"]["session"]
            assert native and native != main.session_id
            assert main._outputs.root == binding.archive.dir / "output"
            manifest = manifest_of(tmp_path, binding.archive.sid)
            identity = next(item for item in manifest["identities"] if item["backend"] == "fusion")
            assert identity["connection"] == "devin" and identity["native"] == native, \
                "the archive maps the real ACP session id, not a stand-in"
        finally:
            await core.shutdown()

    run(go())


# ----------------------------------------------------------------------------------------------- commit and recall


def fill_marked(controller: Any, turns: int = 8) -> None:
    """A long conversation whose user messages each carry one unique marker (about 430 tokens a message)."""
    for number in range(turns):
        start = len(controller.saved.items)
        controller.saved.items += [user_message(f"question {number} zebra{number} {WORDS}"),
                                   assistant(f"answer {number} {WORDS}")]
        controller.saved.turns.append(SavedTurn(start, 100.0 + number, 101.0 + number, "done", "", controller.model))


def test_a_compacted_conversation_recalls_the_evicted_original_text(tmp_path):
    """The production vertical slice: a compaction commits a generation, then a scripted ``recall_history``
    reads the evicted original message from it and the tool result rides the next request to the model."""
    async def go() -> None:
        with FakeResponses(token=ACCESS_TOKEN) as fake:
            fake.script(
                say("SUMMARY OF THE EARLIER PART", used=usage(900, 40)),
                call("recall_history", {"question": "zebra0", "reason": "the archived original zebra0 message"}),
                say("the answer"),
            )
            engine, controller = await opened(tmp_path, fake)
            fill_marked(controller)
            engine._windows["gpt-6.1-sol"] = await roomy(controller)
            controller.send_prompt("please continue")
            await finish_turn(controller)

            record = controller.saved.compaction
            assert record and record["upto"] >= 2, "the first messages left the active view"
            sid = controller.saved.origin[ARCHIVE_ORIGIN]
            assert manifest_of(tmp_path, sid)["latest"] == 1
            assert len(fake.requests) == 3 and is_summary_request(fake.requests[0])
            sent = json.dumps(fake.requests[2], ensure_ascii=False)
            assert "zebra0" in sent, "the recall's tool result reaches the model's next request"
            await engine.close_all()

    run(go())


def test_a_failed_compaction_save_rolls_back_the_generation_and_keeps_the_view(tmp_path, monkeypatch):
    """The companion save runs inside ``publish``'s checkpoint: when it fails, the manifest's ``latest`` on
    disk, the staged file and ``saved.compaction`` all stay as before, and a reopened session shows the
    whole uncompacted log. The save's own error message — not an archive wrapper — reaches the turn."""
    async def go() -> None:
        with FakeResponses(token=ACCESS_TOKEN) as fake:
            fake.script(say("SUMMARY OF THE EARLIER PART", used=usage(900, 40)), say("unreached"))
            engine, controller = await opened(tmp_path, fake)
            fill(controller)
            engine._windows["gpt-6.1-sol"] = await roomy(controller)
            before = dict(controller.saved.compaction)
            used_before = controller.saved.context_used
            real_save = controller.manager.sessions.save

            def boom(snapshot: Any) -> Any:
                if getattr(snapshot, "compaction", None):
                    raise OSError("disk full")
                return real_save(snapshot)

            monkeypatch.setattr(controller.manager.sessions, "save", boom)
            controller.send_prompt("next question")
            await finish_turn(controller)

            assert len(fake.requests) == 1 and is_summary_request(fake.requests[0]), \
                "the summary request ran; the commit failed before the next request"
            turn = controller.transcript.turns[-1]
            assert turn.status is TurnStatus.FAILED
            assert "保存" in failed_error(turn) and "归档提交失败" not in failed_error(turn)
            assert controller.saved.compaction == before and controller.saved.context_used == used_before
            sid = controller.saved.origin[ARCHIVE_ORIGIN]
            manifest = manifest_of(tmp_path, sid)
            assert manifest["latest"] is None and manifest["generations"] == []
            identity = manifest["identities"][0]
            dumps = archive_root(tmp_path) / sid / "gpt" / identity["iid"] / "dumps"
            assert not list(dumps.glob("*.jsonl.gz")), "no staged or committed snapshot survives the failed save"
            await engine.close(controller)

            reopened = await engine.open_saved(tmp_path / "project", controller.session_id)
            assert reopened.saved.compaction == {}
            view = compact.view_of(reopened.saved.items, reopened.saved.compaction)
            assert len(view) == len(reopened.saved.items) and not any(compact.is_summary(item) for item in view)
            await engine.close_all()

    run(go())


def test_the_archive_segment_uses_only_real_native_ids(tmp_path):
    """No fake bindings: before the ACP handshake the devin stretch is not archivable at all (``None`` —
    nothing is written under our session id or a ``str(None)`` stand-in); once the real native id exists it
    is bound as ``fusion/devin``; a Fusion session on a local driver is still ``fusion`` — under its own
    driver's connection name with the session's own id, never ``devin``."""
    from types import SimpleNamespace

    async def go() -> None:
        with FakeResponses(token=ACCESS_TOKEN) as fake:
            fake.script(say("the answer"))
            engine, controller = await opened(tmp_path, fake)
            controller._archive_begin()
            binding = controller._archive_binding
            assert binding is not None

            controller.requested_model = "devin:swe-2-high"
            assert controller._archive_segment() is None, "no handshake: the ACP stretch is not archivable yet"
            controller._archive_sync()
            manifest = manifest_of(tmp_path, binding.archive.sid)
            assert manifest["identities"] == [], "nothing may be written under a stand-in native id"

            controller._acp = SimpleNamespace(session="acp-native-1")
            assert controller._archive_segment() == ("fusion", "devin", "acp-native-1")
            controller._archive_sync()
            manifest = manifest_of(tmp_path, binding.archive.sid)
            (identity,) = manifest["identities"]
            assert identity["backend"] == "fusion" and identity["connection"] == "devin"
            assert identity["native"] == "acp-native-1" and identity["native"] != controller.session_id

            controller._acp, controller._fusion = None, object()
            controller.requested_model = "gpt-6.1-sol"
            assert controller._archive_segment() == ("fusion", "gpt", controller.session_id)
            controller._archive_sync()
            manifest = manifest_of(tmp_path, binding.archive.sid)
            local = next(item for item in manifest["identities"] if item["connection"] == "gpt")
            assert local["backend"] == "fusion" and local["native"] == controller.session_id

            controller._fusion = None
            assert controller._archive_segment() == ("gpt", "gpt:gpt", controller.session_id), \
                "the plain backend must not reuse the driver key already bound to the Fusion backend"
            await engine.close_all()

    run(go())


def test_a_reopened_session_whose_archive_cannot_be_restored_refuses_to_compact(tmp_path, monkeypatch):
    """The marker exists but ``ArchiveBinding.restore`` fails: the session opens anyway with its old view,
    marked broken — ``_compact`` refuses instead of degrading to a legacy, unarchived compaction, the
    archive output store is never adopted, and the declared ``recall_history`` refuses calls."""
    async def go() -> None:
        with FakeResponses(token=ACCESS_TOKEN) as fake:
            fake.script(say("the answer"), say("unreached"))
            folder = project(tmp_path)
            engine = make_engine(tmp_path, fake)
            controller = await engine.open_new(folder, model="gpt-6.1-sol", reasoning="medium")
            controller.send_prompt("first question")
            await finish_turn(controller)
            sid = controller.saved.origin[ARCHIVE_ORIGIN]
            session_id = controller.session_id
            await engine.close(controller)

            def boom(*args: Any, **kwargs: Any) -> Any:
                raise ArchiveError("injected restore failure")

            monkeypatch.setattr(ArchiveBinding, "restore", boom)
            reopened = await engine.open_saved(folder, session_id)
            assert reopened._archive_binding is None and reopened._archive_broken
            fill(reopened)
            engine._windows["gpt-6.1-sol"] = await roomy(reopened)
            reopened.send_prompt("next question")
            await finish_turn(reopened)

            assert len(fake.requests) == 1, "a broken archive refuses compaction; no request is made"
            turn = reopened.transcript.turns[-1]
            assert turn.status is TurnStatus.FAILED and "归档" in failed_error(turn)
            assert reopened.saved.compaction == {}, "the old view is untouched"
            assert "recall_history" in declared(reopened), "a failed restore does not remove the declared tool"
            recall = Call("i1", "c1", "recall_history", "", {"question": "zebra0"})
            outcome = await reopened._tools.run(recall)
            assert outcome.is_error and recall_tool.UNAVAILABLE in outcome.output
            assert reopened._outputs.root != archive_root(tmp_path) / sid / "output", \
                "the archive's store was never adopted"
            await engine.close_all()

    run(go())
