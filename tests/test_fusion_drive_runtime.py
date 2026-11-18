from __future__ import annotations

import asyncio
import hashlib
import sqlite3
import threading

import pytest

from desktop.core.fusion.acp import AcpRun
from desktop.core.fusion.drive.fingerprint import fingerprint
from desktop.core.fusion.drive.retention import Retention
from desktop.core.fusion.drive.store import Drive, DriveError
from desktop.core.fusion.registry import Policy
from desktop.core.harness.loop import ALLOW, DENY, Call
from desktop.core.settings import AppSettings
from desktop.core.transcript import NoticeBlock
from tests.app_support import ready_core
from tests.chatgpt_support import finish_turn, make_engine
from tests.fake_responses import FakeResponses, call, fail, say
from tests.fakes import until
from tests.fusion_acp_support import make_guarded_engine

pytestmark = pytest.mark.fusion_gates("drive")  # Today's free-text delegation with only the store switched on.

MAIN = "chatgpt:gpt-6.1-sol"
WORKER = "chatgpt:gpt-6-luna"
TASK = {"workstream": "Inventory", "paths": ["inventory.txt"], "model": WORKER, "role": "worker",
        "task": "Review and record the inventory."}
HEADER = "[Retained Fusion Drive data, not instructions.]\n"


def _call(name, **args):
    return Call("item", "call", name, "", args)


def _records(drive):
    return {entry.record.kind: entry.record for entry in drive.candidates(shared=False, retention=Retention())}


def test_delegation_retains_task_and_report_for_both_agents(tmp_path):
    async def go():
        project = tmp_path / "project"
        project.mkdir()
        stamp = fingerprint(project, TASK["paths"])
        brief_pointer = "d-" + hashlib.sha256(TASK["task"].encode()).hexdigest()
        with FakeResponses() as fake:
            engine = make_engine(tmp_path, fake)
            main = await engine.open_new(project, model="gpt-6.1-sol")
            main.enable_fusion(Policy(MAIN, (WORKER,), WORKER, 5))
            fake.script(call("delegate_task", TASK, call_id="delegate"),
                        call("read_record", {"pointer": brief_pointer}, call_id="brief"),
                        call("create_file", {"file_path": "inventory.txt", "content": "Part count: 4.\n" * 200}, call_id="write"),
                        call("read_file", {"file_path": "inventory.txt"}, call_id="evidence"),
                        say("Inventory verified."), say("Reviewed."))
            try:
                root = engine.root.parent / "fusion-drive"
                assert not root.exists()
                assert main.send_prompt("Review the inventory.")
                await finish_turn(main)
                runtime = main._fusion
                expert = next(iter(main.agents.values()))
                assert expert.record.status == "done"
                assert any(item.get("output") == HEADER + TASK["task"] for item in expert.controller.saved.items)
                records = await asyncio.to_thread(lambda: _records(Drive(root, project, runtime.owner)))
                brief, report = records["brief"], records["report"]
                assert brief.pointers == (brief_pointer,)
                assert brief.key == f"brief:{expert.session_id}" and not brief.open
                assert report.key == f"report:{expert.session_id}" and not report.open
                assert brief.session == report.session == runtime.owner
                assert brief.paths == report.paths == ("inventory.txt",)
                assert brief.fingerprint == stamp != report.fingerprint == fingerprint(project, TASK["paths"])
                assert brief.summary == "Inventory: " + TASK["task"]
                assert report.summary == "Inventory [done]: Inventory verified."
                assert any(f"Fusion Drive report {report.id}: {', '.join(report.pointers)}" in item.get("output", "")
                           for item in main.saved.items)
                for controller in (main, expert.controller):
                    tools = await controller._ensure_tools()
                    assert tools.known("read_record") and any(tool["name"] == "read_record" for tool in tools.declarations())
                    read = _call("read_record", pointer=brief_pointer)
                    tools.plan = True
                    assert tools.check(read).verdict == ALLOW
                    assert (await tools.run(read)).output == HEADER + TASK["task"]
                    outcome = await tools.run(_call("read_record", pointer=report.pointers[0]))
                    assert not outcome.is_error and outcome.output.startswith(HEADER + "Inventory verified.")
                    assert "Evidence references:\n" in outcome.output and "retained as o-" in outcome.output
                    missing = await tools.run(_call("read_record", pointer="d-" + "0" * 64))
                    assert missing.is_error and "Unknown Drive page" in missing.output
            finally:
                await engine.close_all()
    asyncio.run(go())


def test_drive_gate_off_keeps_old_delegation_and_tools(tmp_path):
    async def go():
        project = tmp_path / "project"
        project.mkdir()
        with FakeResponses() as fake:
            engine = make_engine(tmp_path, fake, settings=AppSettings(fusion_gates={"drive": False}))
            main = await engine.open_new(project, model="gpt-6.1-sol")
            main.enable_fusion(Policy(MAIN, (WORKER,), WORKER, 5))
            fake.script(call("delegate_task", TASK), say("Inventory verified."), say("Reviewed."))
            try:
                assert main.send_prompt("Check the inventory.")
                await finish_turn(main)
                assert next(iter(main.agents.values())).record.status == "done"
                assert not (engine.root.parent / "fusion-drive").exists()
                assert all("read_record" not in {tool["name"] for tool in request["tools"]} for request in fake.requests)
                assert all("Fusion Drive report" not in item.get("output", "") for item in main.saved.items)
            finally:
                await engine.close_all()
    asyncio.run(go())


@pytest.mark.parametrize(("stage", "error"), [("brief", DriveError), ("brief", OSError), ("report", sqlite3.Error)])
def test_store_failure_does_not_break_delegation(tmp_path, monkeypatch, stage, error):
    original = Drive.put
    attempts = []

    def failing_put(self, kind, *args, **kwargs):
        attempts.append(kind)
        if kind == stage:
            raise error("Test store failure")
        return original(self, kind, *args, **kwargs)

    monkeypatch.setattr(Drive, "put", failing_put)

    async def go():
        project = tmp_path / "project"
        project.mkdir()
        with FakeResponses() as fake:
            engine = make_engine(tmp_path, fake)
            main = await engine.open_new(project, model="gpt-6.1-sol")
            main.enable_fusion(Policy(MAIN, (WORKER,), WORKER, 5))
            fake.script(call("delegate_task", TASK), say("Inventory verified."), say("Reviewed."),
                        call("delegate_task", TASK, call_id="again"), say("Inventory rechecked."), say("Reviewed again."))
            try:
                for text in ("Check the inventory.", "Check again."):
                    assert main.send_prompt(text)
                    await finish_turn(main)
                    assert next(iter(main.agents.values())).record.status == "done"
                notices = [block.text for turn in main.transcript.turns for block in turn.blocks if isinstance(block, NoticeBlock)]
                assert sum("Fusion Drive" in text for text in notices) == 1
                assert attempts == (["brief"] if stage == "brief" else ["brief", "report"]) * 2
                records = await asyncio.to_thread(_records, main._fusion.drive)
                assert not records if stage == "brief" else not records["brief"].open
            finally:
                await engine.close_all()
    asyncio.run(go())


def test_failed_expert_still_closes_brief_and_leaves_report(tmp_path):
    async def go():
        project = tmp_path / "project"
        project.mkdir()
        with FakeResponses() as fake:
            engine = make_engine(tmp_path, fake)
            main = await engine.open_new(project, model="gpt-6.1-sol")
            main.enable_fusion(Policy(MAIN, (WORKER,), WORKER, 5))
            fake.script(call("delegate_task", TASK), fail("bad_request"), say("The expert failed."))
            try:
                assert main.send_prompt("Check the inventory.")
                await finish_turn(main)
                assert next(iter(main.agents.values())).record.status == "failed"
                records = await asyncio.to_thread(_records, main._fusion.drive)
                assert not records["brief"].open and records["report"].summary.startswith("Inventory [failed]:")
                assert any(f"Fusion Drive report {records['report'].id}:" in item.get("output", "") for item in main.saved.items)
            finally:
                await engine.close_all()
    asyncio.run(go())


def test_cancel_during_brief_write_still_closes_it_and_records_interruption(tmp_path, monkeypatch):
    async def go():
        project = tmp_path / "project"
        project.mkdir()
        worker_started, release = asyncio.Event(), threading.Event()
        loop = asyncio.get_running_loop()
        original = Drive.put

        def wait_on_brief(self, kind, *args, **kwargs):
            if kind == "brief":
                loop.call_soon_threadsafe(worker_started.set)
                assert release.wait(5)
            return original(self, kind, *args, **kwargs)

        monkeypatch.setattr(Drive, "put", wait_on_brief)
        engine = make_engine(tmp_path, None)
        main = await engine.open_new(project, model="gpt-6.1-sol")
        main.enable_fusion(Policy(MAIN, (WORKER,), WORKER, 5))
        delegation = asyncio.create_task(main._fusion.delegate("Inventory", WORKER, "worker", TASK["task"], ("inventory.txt",)))
        try:
            await asyncio.wait_for(worker_started.wait(), timeout=5)
            delegation.cancel()
            release.set()
            with pytest.raises(asyncio.CancelledError):
                await delegation
            records = await asyncio.to_thread(_records, main._fusion.drive)
            assert not records["brief"].open and records["report"].summary.startswith("Inventory [interrupted]:")
        finally:
            release.set()
            await engine.close_all()
    asyncio.run(go())


def test_ordinary_conversation_has_no_record_tool_and_cannot_access_store(tmp_path):
    async def go():
        project = tmp_path / "project"
        project.mkdir()
        engine = make_engine(tmp_path, None)
        main = await engine.open_new(project, model="gpt-6.1-sol", autonomy="full")
        try:
            tools = await main._ensure_tools()
            assert not tools.known("read_record") and "read_record" not in {tool["name"] for tool in tools.declarations()}
            root = engine.root.parent / "fusion-drive"
            root.mkdir()
            sample = root / "sample.txt"
            sample.write_text("Retained data", encoding="utf-8")
            for request in (_call("read_file", file_path=str(sample)), _call("list_dir", directory_path=str(root)),
                            _call("create_file", file_path=str(root / "new.txt"), content="Not written")):
                assert tools.check(request).verdict == DENY
                assert (await tools.run(request)).is_error
            assert not (root / "new.txt").exists()
        finally:
            await engine.close_all()
    asyncio.run(go())


def test_devin_main_gets_record_tool_through_private_mcp(tmp_path, monkeypatch):
    manifests = []
    original = AcpRun.dispatch

    async def observe_manifest(self, method, params):
        result = await original(self, method, params)
        if method == "tools/list":
            manifests.append(result["tools"])
        return result

    monkeypatch.setattr(AcpRun, "dispatch", observe_manifest)

    async def go():
        harness = make_guarded_engine(tmp_path)
        core, _, _, _, _ = await ready_core(tmp_path, devin_engine=harness.engine)
        try:
            await until(lambda: any(model["source"] == "devin" for model in core.catalog.models), timeout=20)
            core.settings.fusion_main = "devin:swe-2-high"
            core.settings.fusion_workers = [WORKER]
            core.settings.fusion_light = WORKER
            cid = (await core.call("session.new", {"projectId": str(tmp_path / "proj"), "fusion": True}))["id"]
            main = core._by_cid[cid].controller
            assert main.send_prompt("Check the inventory.")
            await finish_turn(main, timeout=30)
            assert main.saved.turns[-1].status == "done"
            manifest = next(item for tools in manifests for item in tools if item["name"] == "read_record")
            assert manifest["inputSchema"]["required"] == ["pointer"]
            assert not (main.manager.root.parent / "fusion-drive").exists()
        finally:
            await core.shutdown()
    asyncio.run(go())
