from __future__ import annotations

import asyncio
import sys
import threading

import pytest

from desktop.core.fusion import map as project_map
from desktop.core.transcript import NoticeBlock
from tests.chatgpt_support import finish_turn
from tests.fake_responses import call, say
from tests.fakes import until
from tests.test_fusion_identity_runtime import LEGACY, _brief, _fusion, _output, _turn
from tests.test_fusion_inject_runtime import _prompt
from tests.test_fusion_route_runtime import UNIT_GATES, _report, _route, _unit

_ACTIVE = ("drive", "brief", "report", "inject", "map")
pytestmark = pytest.mark.fusion_gates(*_ACTIVE)


def _choose(call_id="choose"):
    return call("choose_map_location", {}, call_id=call_id)


def _delegate(identity="Coder", *, call_id="delegate", **overrides):
    return call("delegate_task", _brief(identity, **overrides), call_id=call_id)


async def _choose_turn(main, fake, selection, *responses):
    fake.script(_choose(), *responses, say("Ready."))
    assert main.send_prompt("Choose how to store the project map.")
    await until(lambda: main.transcript.pending_prompts())
    prompt, = main.transcript.pending_prompts()
    question, = prompt.questions
    assert prompt.tool_call_id == "choose" and len(question.options) == 2 and "项目地图" in question.question
    assert main.answer_question(prompt.id, None if selection is None else [question.options[selection]])
    await finish_turn(main)


@pytest.mark.parametrize(("selection", "location"), [(0, "project"), (1, "local"), (None, "local")])
def test_location_is_asked_once_and_skip_defaults_to_local(tmp_path, selection, location):
    async def go():
        async with _fusion(tmp_path, active=_ACTIVE) as (main, fake):
            await _choose_turn(main, fake, selection, _choose("again"))
            assert main._fusion.drive.meta("map_location") == location
            assert location in _output(main, "again") and "update_map_entry" in _output(main, "choose")
            assert len(main.transcript.turns[-1].prompts) == 1
            path = main.project / ".witty_map.md"
            if location == "project":
                assert path.read_text(encoding="utf-8") == project_map.render(project_map.ProjectMap(()))
            else:
                assert not path.exists()
    asyncio.run(go())


def _records(main):
    fusion = main._fusion
    return {item.record.key: item.record for item in fusion.drive.candidates(shared=False, retention=fusion.retention())
            if item.record.kind == "map"}


def _update(module, *, call_id="update", **fields):
    return call("update_map_entry", {"module": module, **fields}, call_id=call_id)


def test_plan_reads_map_on_demand_but_worker_does_not_receive_it(tmp_path):
    async def go():
        async with _fusion(tmp_path, active=_ACTIVE) as (main, fake):
            await _choose_turn(main, fake, 1, _update("stock", paths=["stock"], role="Counts items.", method="Totals rows."),
                               _update("other", paths=["other"], call_id="other"))
            before = _records(main)
            main.mode = "spec"
            await _turn(main, fake, call("read_project_map", {"paths": ["stock/counts.py"]}, call_id="map"), say("Planned."))
            assert "Counts items." in _output(main, "map") and "other" not in _output(main, "map")
            assert _records(main) == before
            main.mode = "auto"
            first = len(fake.requests)
            await _turn(main, fake, _delegate(paths=["stock/counts.py"]), _report(), say("Reviewed."))
            prompt = _prompt(fake.requests[first + 1])
            assert "## Map entries" not in prompt and "Counts items." not in prompt
            assert not (main.project / ".witty_map.md").exists()
    asyncio.run(go())


def test_traits_given_beside_a_module_are_accepted_as_the_projects(tmp_path):
    # Found with a real model: it fills traits in the same call as the module's fields. The call was refused, and the model
    # repeated it twenty times before writing the map file by hand.
    async def go():
        async with _fusion(tmp_path, active=_ACTIVE) as (main, fake):
            await _choose_turn(
                main, fake, 0,
                _update("stats", paths=["stats.py"], role="Computes statistics.", method="Sorts a copy.", traits="Small Python library."),
                _update("stats", paths=[], role="Computes the mean and the median.", method="", traits="", call_id="empty"),
                _update("stats", traits="Standard library only.", call_id="only"),
                _update("traits", traits="Tested with unittest.", call_id="named"),
                _update("stats", paths=[], role="", method="", traits="", call_id="nothing"))
            assert "Updated project map entry: stats." in _output(main, "update")
            assert "replaced the project-wide traits" in _output(main, "update") and "not one per module" in _output(main, "update")
            assert _output(main, "empty") == "Updated project map entry: stats."
            assert _output(main, "only") == _output(main, "named") == "Updated the project-wide traits."
            assert "at least one of" in _output(main, "nothing")
            stored = project_map.read(main.project)
            assert stored.entries == (project_map.MapEntry("stats", ("stats.py",), "Computes the mean and the median.", "Sorts a copy."),)
            assert stored.traits == "Tested with unittest."
            records = _records(main)
            assert set(records) == {"map:stats", "map:traits"} and "Tested with unittest." in records["map:traits"].summary
    asyncio.run(go())


def test_existing_file_wins_then_updates_one_entry_and_imports_hand_edit(tmp_path):
    async def go():
        async with _fusion(tmp_path, active=_ACTIVE) as (main, fake):
            original = project_map.ProjectMap((project_map.MapEntry("counts", ("stock",), "Counts rows.", "Totals rows."),
                                               project_map.MapEntry("other", ("other.txt",))), "Portable layout.")
            path = project_map.write(main.project, original)
            await _choose_turn(main, fake, 0)
            assert project_map.read(main.project) == original
            before = _records(main)
            assert set(before) == {"map:counts", "map:other", "map:traits"}
            # (a model fills every field: an empty traits beside a module's fields means "not given", not an error)
            await _turn(main, fake, _update("counts", role="Counts boxes.", traits="", paths=[]),
                        _update("counts", paths=["C:/outside.txt"], call_id="bad-path"), say("Updated."))
            updated = project_map.read(main.project)
            assert updated.entries[0].role == "Counts boxes." and updated.entries[1] == original.entries[1]
            assert "project-relative" in _output(main, "bad-path") and str(main.project) not in path.read_text(encoding="utf-8")
            stored = _records(main)
            assert "Counts boxes." in stored["map:counts"].summary and stored["map:other"].id == before["map:other"].id
            edited = project_map.ProjectMap((project_map.MapEntry("counts", ("stock",), "Counts packages.", "Totals batches."),))
            project_map.write(main.project, edited)
            await _turn(main, fake, call("read_project_map", {}, call_id="read_map"), say("Read."))
            assert "Counts packages." in _output(main, "read_map") and "Counts boxes." not in _output(main, "read_map")
            assert set(_records(main)) == {"map:counts", "map:other", "map:traits"}  # an explicit read does not sync writes
            assert project_map.read(main.project) == edited
    asyncio.run(go())


@pytest.mark.parametrize("route", [False, True])
def test_read_only_delegation_does_not_choose_location(tmp_path, route):
    async def go():
        async with _fusion(tmp_path, active=(*_ACTIVE, "route") if route else _ACTIVE) as (main, fake):
            await _turn(main, fake, _delegate("Runner", read_only=True), _report(), say("Reviewed."))
            agent, = main.agents.values()
            assert agent.name == "Runner-Inventory" and agent.record.status == "done"
            assert main._fusion.drive.meta("map_location") is None and not main.transcript.turns[-1].prompts
    asyncio.run(go())


@pytest.mark.parametrize("has_paths", [True, False], ids=["paths-ignored", "no-paths"])
@pytest.mark.parametrize("identity", ["Coder", "Runner", "QualitySampler"])
def test_read_only_brief_with_paths_needs_no_route_or_map_choice(tmp_path, identity, has_paths):
    async def go():
        async with _fusion(tmp_path, active=(*_ACTIVE, "route", "acceptance", "v3")) as (main, fake):
            declare = call("declare_topic", {"kind": "simple", "intent": "execute", "topic": "Check stock."})
            paths = ["inventory.txt", "packing.txt"] if has_paths else []
            if identity == "Coder":
                command = "New-Item packing.txt"
            else:
                (main.project / "test_inventory_check.py").write_text(
                    "from pathlib import Path\nfrom unittest import TestCase\n\n"
                    "class InventoryCheck(TestCase):\n"
                    "    def test_no_edit(self):\n"
                    "        self.assertFalse(Path('inventory.txt').exists())\n", encoding="utf-8")
                python = sys.executable.replace("'", "''")
                command = f"& '{python}' -B -m unittest test_inventory_check"
            await _turn(main, fake, declare, _delegate(identity, read_only=True, paths=paths),
                        call("create_file", {"file_path": "inventory.txt", "content": "Not written."}, call_id="edit"),
                        call("shell", {"command": command}, call_id="command"), _report(), say("Reviewed."))
            runtime = main._fusion
            agent, = main.agents.values()
            assert agent.record.status == "done" and runtime.registry.calls == 1
            assert ("Ignored 2 write paths for this read-only brief." in _output(main, "delegate")) is has_paths
            edit, shell = (_output(agent.controller, call_id) for call_id in ("edit", "command"))
            if identity == "Coder":
                assert "read-only" in edit and "read-only" in shell
            else:
                assert "Refused" in edit and "Ran 1 test" in shell and "OK" in shell and "Refused" not in shell
            assert not (main.project / "inventory.txt").exists() and not (main.project / "packing.txt").exists()
            assert runtime.registry.write_paths(agent.record) == () and not runtime.registry.allocations
            assert runtime.drive.meta("map_location") is None and not main.transcript.turns[-1].prompts
            turn = runtime.route_turn
            assert turn.route is None and not turn.workstreams and not turn.unfrozen_write and not turn.frozen_l3_paths
            assert all(not result.paths for result in turn.delegations) and runtime.acceptance_verdict() is None
    asyncio.run(go())


@pytest.mark.parametrize("structured", [True, False], ids=["structured", "legacy"])
def test_first_write_delegation_does_not_ask_where_to_store_the_map(tmp_path, structured):
    async def go():
        gates = tuple(gate for gate in (*_ACTIVE, "route") if structured or gate != "brief")
        async with _fusion(tmp_path, active=gates) as (main, fake):
            args = (_brief() if structured else
                    {key: LEGACY[key] for key in ("model", "role", "task")} | {"scope": "inventory.txt"})
            await _turn(main, fake, _route(), call("delegate_task", args, call_id="first"),
                        _report(evidence=["Updated inventory.txt."]) if structured else say("First run done."), say("Reviewed."))
            agent, = main.agents.values()
            assert agent.record.status == "done" and not main.transcript.turns[-1].prompts
            assert main._fusion.drive.meta("map_location") is None and not (main.project / ".witty_map.md").exists()
            assert bool(_records(main)) == structured
            if structured:
                # A silent local write never takes away the later explicit choice of a project file.
                await _choose_turn(main, fake, 0)
                assert project_map.read(main.project).entries[0].paths == ("inventory.txt",)
    asyncio.run(go())


def test_missing_route_still_refuses_without_asking_for_map_location(tmp_path):
    async def go():
        async with _fusion(tmp_path, active=(*_ACTIVE, "route")) as (main, fake):
            await _turn(main, fake, _delegate(), say("Reviewed."))
            assert "submit a route first" in _output(main, "delegate")
            assert not main.agents and not main.transcript.turns[-1].prompts
            assert main._fusion.drive.meta("map_location") is None
    asyncio.run(go())


def test_frozen_unit_writes_map_after_acceptance_using_author_receipt_not_runner_or_reviewer(tmp_path):
    from tests.test_fusion_debate_runtime import _declare

    async def go():
        async with _fusion(tmp_path, active=_ACTIVE) as (main, fake):
            await _choose_turn(main, fake, 0, _update("stock", paths=["stock.txt"], role="Old role.", method="Old method."))
            before = project_map.read(main.project)
            main.manager.settings.fusion_gates.update(dict.fromkeys(UNIT_GATES, True))
            main.mode = "spec"
            await _turn(main, fake, _declare("simple", "plan"),
                        call("submit_work_plan", {"data": {"units": [_unit()]}}, call_id="facts"),
                        call("submit_plan", {"plan": "Repair stock count."}, call_id="plan"))
            author = "Counts only available stock."
            first = len(fake.requests)
            fake.script(call("submit_route", {}, call_id="route"),
                        call("delegate_task", {"unit": "stock"}, call_id="worker"),
                        call("create_file", {"file_path": "stock.txt", "content": "4"}, call_id="write"),
                        call("submit_report", {"status": "done", "summary": author, "evidence": ["Wrote stock.txt."]}),
                        [("hold", "before-checks"), *call("delegate_task", {"unit": "stock"}, call_id="runner")],
                        _report(evidence=["Count is four."]), call("delegate_task", {"unit": "stock"}, call_id="review"),
                        _report(evidence=["Reviewed stock.txt."]), say("Verified."))
            assert main.approve_plan(main.saved.plan["id"])
            try:
                await until(lambda: len(fake.requests) >= first + 5)
                assert "next=runner" in _output(main, "worker")
                assert project_map.read(main.project) == before
                assert "Old method." not in _prompt(fake.requests[first + 2])
            finally:
                fake.release("before-checks")
            await finish_turn(main)
            assert "next=done" in _output(main, "review")
            assert project_map.read(main.project).entries == (
                project_map.MapEntry("stock", ("stock.txt",), "Repair the stock count.", author),)
            assert "Describe the involved project map" not in str(fake.requests[-1]["input"])
            assert len(fake.requests) == first + 9  # No extra model call to describe the map.
    asyncio.run(go())


def test_two_writers_in_separate_profiles_serialize_the_portable_map(tmp_path, monkeypatch):
    from copy import copy
    from types import SimpleNamespace

    async def go():
        async with _fusion(tmp_path, active=_ACTIVE) as (main, fake):
            await _choose_turn(main, fake, 0)
            first = main._fusion
            second = copy(first)
            second.__dict__.pop("drive", None)
            second.parent = SimpleNamespace(project=main.project, manager=SimpleNamespace(
                root=tmp_path / "second-profile" / "chatgpt", settings=main.manager.settings))
            assert second.drive.map_lock_target != first.drive.map_lock_target
            await asyncio.to_thread(second._edit_map, lambda current: current, selection="project")
            writing, attempted, finish = threading.Event(), threading.Event(), threading.Event()
            write, file_lock = project_map.write, project_map.portable_lock

            def hold(project, data):
                if not writing.is_set():
                    writing.set()
                    assert finish.wait(15)
                return write(project, data)

            def track(project):
                if writing.is_set():
                    attempted.set()
                return file_lock(project)

            monkeypatch.setattr(project_map, "write", hold)
            monkeypatch.setattr(project_map, "portable_lock", track)
            tasks = [asyncio.create_task(first._map_delivery(("first.py",), "First job", "Writes the first result."))]
            try:
                await until(writing.is_set)
                tasks.append(asyncio.create_task(second._map_delivery(("second.py",), "Second job", "Writes the second result.")))
                await until(attempted.is_set)
                assert not project_map.read(main.project).entries
            finally:
                finish.set()
                await asyncio.gather(*tasks)
            assert {entry.paths for entry in project_map.read(main.project).entries} == {("first.py",), ("second.py",)}
    asyncio.run(go())


@pytest.mark.parametrize("active", [_ACTIVE[:-1], ("brief", "report", "inject", "map")])
def test_map_needs_both_gates(tmp_path, active):
    async def go():
        async with _fusion(tmp_path, active=active) as (main, fake):
            await _turn(main, fake, _delegate(), _report(), say("Reviewed."))
            assert len(main.agents) == 1 and "choose_map_location" not in _output(main, "delegate")
            names = {tool["name"] for tool in fake.requests[0]["tools"]}
            assert names.isdisjoint({"read_project_map", "choose_map_location", "update_map_entry"})
            assert not (main.project / ".witty_map.md").exists()
    asyncio.run(go())


def test_portable_map_write_failure_does_not_lose_delivery_or_update_only_the_mirror(tmp_path, monkeypatch):
    async def go():
        async with _fusion(tmp_path, active=_ACTIVE) as (main, fake):
            await _choose_turn(main, fake, 0, _update("stock", paths=["inventory.txt"], role="Counts items.", method="Totals rows."))
            before, file = _records(main), project_map.read(main.project)

            def unavailable(*args):
                raise PermissionError("The test file is locked.")

            monkeypatch.setattr(project_map, "write", unavailable)
            await _turn(main, fake, _delegate(), _report(evidence=["Updated inventory.txt."]), say("Reviewed."))
            assert _records(main) == before and project_map.read(main.project) == file
            assert next(iter(main.agents.values())).record.status == "done"
            notices = [block.text for turn in main.transcript.turns for block in turn.blocks if isinstance(block, NoticeBlock)]
            assert sum("项目地图更新失败" in text for text in notices) == 1
    asyncio.run(go())
