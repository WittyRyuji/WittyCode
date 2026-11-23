from __future__ import annotations

import asyncio
import json

import pytest

from desktop.core.fusion.acp import AcpRun
from desktop.core.fusion.drive.retention import Retention
from desktop.core.fusion.protocol import ROUTE_DECLARATION, route_refusal
from tests.app_support import ready_core
from tests.chatgpt_support import finish_turn
from tests.fake_responses import call, say
from tests.fakes import until
from tests.fusion_acp_support import make_guarded_engine
from tests.test_fusion_identity_runtime import DEVIN, LEGACY, LIGHT, WORKER, _brief, _fusion, _gates, _identity_folder, _manual_steps, _output, _turn

ROUTE_GATES = ("drive", "brief", "report", "route")
pytestmark = pytest.mark.fusion_gates("drive", "brief", "report", "route")


def _route(destination="coder", verification="runner", *, reason="Matches the size of the task.", call_id="route"):
    return call("submit_route", {"destination": destination, "verification": verification, "reason": reason}, call_id=call_id)


def _report(status="done", *, call_id="report"):
    return call("submit_report", {"status": status, "summary": "Inventory checked.", "evidence": [], "notes": [], "ratings": []}, call_id=call_id)


def _edit(call_id="edit"):
    return call("create_file", {"file_path": "marker.txt", "content": "route test"}, call_id=call_id)


def _delegate(call_id="delegate", **kwargs):
    return call("delegate_task", _brief(**kwargs), call_id=call_id)


def test_route_blocks_writes_before_routing_but_not_reads_or_research(tmp_path):
    async def go():
        async with _fusion(tmp_path, active=ROUTE_GATES) as (main, fake):
            (main.project / "stock.txt").write_text("4 parts", encoding="utf-8")
            await _turn(main, fake, _edit(), call("shell", {"command": "New-Item shell-marker.txt"}, call_id="shell"),
                        call("shell", {"command": "Get-Location"}, call_id="location"),
                        call("read_file", {"file_path": "stock.txt"}, call_id="read"), _delegate(),
                        _delegate("malformed-budget", budget=None),
                        _delegate("research", read_only=True, paths=[]), _report(), say("Reviewed."))
            for ident in ("edit", "shell", "delegate", "malformed-budget"):
                assert route_refusal(None, "edit") in _output(main, ident)
            assert "Refused" not in _output(main, "location") and "4 parts" in _output(main, "read")
            assert "Coder-Inventory [done]" in _output(main, "research")
            assert not (main.project / "marker.txt").exists() and not (main.project / "shell-marker.txt").exists()
            assert next(item for item in fake.requests[0]["tools"] if item["name"] == "submit_route")["parameters"] == ROUTE_DECLARATION["parameters"]
            assert "submit_route" not in {item["name"] for item in fake.requests[-2]["tools"]}
    asyncio.run(go())


@pytest.mark.parametrize("destination", ["self", "coder", "split"])
def test_destinations_enforce_edits_commands_and_distinct_workstreams(tmp_path, destination):
    async def go():
        async with _fusion(tmp_path, active=ROUTE_GATES) as (main, fake):
            replies = [_route(destination), _edit(), call("shell", {"command": "New-Item check.txt"}, call_id="check"), _delegate()]
            if destination != "self":
                replies += [_report(), _delegate("again"), _report(), _delegate("other", workstream="Shipping", paths=["shipping.txt"])]
                if destination == "split":
                    replies.append(_report())
            await _turn(main, fake, *replies, say("Reviewed."))
            assert (main.project / "check.txt").exists()
            if destination == "self":
                assert (main.project / "marker.txt").exists() and "assigns writes to you" in _output(main, "delegate")
                assert not main.agents
            else:
                assert not (main.project / "marker.txt").exists() and route_refusal(main._fusion.route_turn.route, "edit") in _output(main, "edit")
                assert "[done]" in _output(main, "again")
                assert len(main.agents) == (1 if destination == "coder" else 2)
                assert ("one write workstream" if destination == "coder" else "Coder-Shipping [done]") in _output(main, "other")
    asyncio.run(go())


def test_route_resets_next_turn_and_plan_refusal_precedes_it(tmp_path):
    async def go():
        async with _fusion(tmp_path, active=ROUTE_GATES) as (main, fake):
            await _turn(main, fake, _route("self"), say("Routed."))
            await _turn(main, fake, _edit(), say("Needs routing."))
            assert "submit a route first" in _output(main, "edit")
            main.mode = "spec"
            await _turn(main, fake, _edit("plan-edit"), _delegate("plan-delegate"), say("Planning."))
            assert all("Plan mode is on" in _output(main, ident) for ident in ("plan-edit", "plan-delegate"))
            assert not (main.project / "marker.txt").exists()
    asyncio.run(go())


def test_approved_plan_is_a_route_and_is_recorded(tmp_path):
    async def go():
        async with _fusion(tmp_path, active=ROUTE_GATES) as (main, fake):
            main.saved.plan = {"id": "plan", "status": "approved", "text": "Check and repair inventory."}
            await _turn(main, fake, _edit(), _delegate(), _report(), say("Reviewed."))
            assert (main.project / "marker.txt").exists() and "[done]" in _output(main, "delegate")
            turn = main._fusion.route_turn
            assert turn.from_plan and turn.route.verification == "runner"
            drive = main._fusion.drive
            route, = [candidate.record for candidate in drive.candidates(shared=False, retention=Retention()) if candidate.record.kind == "route"]
            assert "approved plan" in route.summary and json.loads(drive.read(route.pointers[0]))["source"] == "plan"
    asyncio.run(go())


@pytest.mark.parametrize("over_budget", [False, True])
def test_next_reroute_counts_once_and_survives_reopening(tmp_path, over_budget):
    async def go():
        async with _fusion(tmp_path, active=ROUTE_GATES) as (main, fake):
            if over_budget:
                _manual_steps(main)
            delegate = _delegate(budget={"steps": 1}) if over_budget else _delegate()
            worker = call("list_dir", {"directory_path": "."}) if over_budget else _report("blocked")
            await _turn(main, fake, _route(), delegate, worker,
                        _route("split", reason="The expert needed a different split.", call_id="reroute"),
                        _route("split", reason="The final route.", call_id="same-turn"), say("Reassessed."))
            assert main._fusion.registry.misroutes == 1
            assert main.manager.sessions.load(main.session_id).fusion["misroutes"] == 1
            drive = main._fusion.drive
            route, = [candidate.record for candidate in drive.candidates(shared=False, retention=Retention()) if candidate.record.kind == "route"]
            assert route.summary == "split [runner]: The final route."
            assert json.loads(drive.read(route.pointers[0]))["source"] == "submitted"
            await main.close()
            reopened = await main.manager.open_saved(main.project, main.session_id)
            assert reopened._fusion.registry.misroutes == 1
    asyncio.run(go())


@pytest.mark.parametrize("enabled", [False, True])
def test_gate_off_and_legacy_writing_delegation(tmp_path, enabled):
    async def go():
        active = ("route",) if enabled else ()
        async with _fusion(tmp_path, active=active) as (main, fake):
            legacy = call("delegate_task", LEGACY, call_id="legacy")
            await _turn(main, fake, _edit(), legacy, *([] if enabled else [say("Legacy report.")]), say("Reviewed."))
            assert ("submit_route" in {item["name"] for item in fake.requests[0]["tools"]}) == enabled
            assert (main.project / "marker.txt").exists() != enabled
            assert ("submit a route first" if enabled else "Legacy report.") in _output(main, "legacy")
    asyncio.run(go())


def test_devin_main_receives_route_schema_and_cannot_edit_before_routing(tmp_path, monkeypatch):
    original = AcpRun.dispatch
    manifests = []

    async def observe(self, method, params):
        result = await original(self, method, params)
        if method == "tools/list":
            manifests.append(result["tools"])
        return result

    monkeypatch.setattr(AcpRun, "dispatch", observe)

    async def go():
        _identity_folder()
        harness = make_guarded_engine(tmp_path)
        core, _, _, _, _ = await ready_core(tmp_path, devin_engine=harness.engine)
        try:
            await until(lambda: any(item["source"] == "devin" for item in core.catalog.models), timeout=20)
            core.settings.fusion_gates = _gates(*ROUTE_GATES)
            core.settings.fusion_main, core.settings.fusion_workers, core.settings.fusion_light = DEVIN, [WORKER], LIGHT
            project = tmp_path / "proj"
            cid = (await core.call("session.new", {"projectId": str(project), "fusion": True, "autonomy": "full"}))["id"]
            main = core._by_cid[cid].controller
            assert main.send_prompt("[fusion-write] Check inventory.")
            await finish_turn(main)
            assert any(item["name"] == "submit_route" and item["inputSchema"] == ROUTE_DECLARATION["parameters"]
                       for manifest in manifests for item in manifest)
            assert not (project / "marker.txt").exists()
            assert any("submit a route first" in item.get("output", "") for item in main.saved.items)
        finally:
            await core.shutdown()
    asyncio.run(go())


def _write(name, call_id):
    return call("create_file", {"file_path": name, "content": "route test"}, call_id=call_id)


def test_more_than_two_edits_of_its_own_must_go_to_a_coder(tmp_path):
    # A hard rule from the user after a real model kept a multi-step change for itself by calling it small.
    async def go():
        async with _fusion(tmp_path, active=ROUTE_GATES) as (main, fake):
            missed = call("edit_file", {"file_path": "a.txt", "old_str": "absent text", "new_str": "never written"}, call_id="failed")
            await _turn(main, fake, _route("self", "self_check"), _write("a.txt", "one"), missed, _write("b.txt", "two"),
                        _write("c.txt", "three"), _route("self", "self_check", call_id="again"), _write("c.txt", "still"),
                        _route(call_id="coder"), _delegate(), _report(), say("Handed over."))
            assert "Refused" not in _output(main, "one") and "Refused" not in _output(main, "two")
            assert "must go to a Coder" not in _output(main, "failed")  # A failed edit is not one of the two.
            assert "must go to a Coder" in _output(main, "three") and "must go to a Coder" in _output(main, "still")
            assert sorted(path.name for path in main.project.iterdir()) == ["a.txt", "b.txt"]
            assert "Coder-Inventory [done]" in _output(main, "delegate")
            # The count belongs to one user turn.
            await _turn(main, fake, _route("self", "self_check"), _write("d.txt", "next"), say("Done."))
            assert "Refused" not in _output(main, "next") and (main.project / "d.txt").is_file()
    asyncio.run(go())


def test_an_approved_plan_is_not_held_to_the_two_edit_limit(tmp_path):
    async def go():
        async with _fusion(tmp_path, active=ROUTE_GATES) as (main, fake):
            main.saved.plan = {"id": "plan", "status": "approved", "text": "Write three notes."}
            await _turn(main, fake, _write("a.txt", "one"), _write("b.txt", "two"), _write("c.txt", "three"), say("Done."))
            assert len(list(main.project.iterdir())) == 3
    asyncio.run(go())