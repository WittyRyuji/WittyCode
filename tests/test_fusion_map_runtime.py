from __future__ import annotations

import asyncio
import json

import pytest

from desktop.core.fusion import map as project_map
from desktop.core.fusion.drive.fingerprint import fingerprint
from desktop.core.transcript import NoticeBlock
from tests.chatgpt_support import finish_turn
from tests.fake_responses import call, say
from tests.fakes import until
from tests.test_fusion_identity_runtime import _brief, _fusion, _output, _turn
from tests.test_fusion_inject_runtime import _prompt, _rated_report
from tests.test_fusion_route_runtime import _report, _route

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


def test_draft_entries_are_stored_and_injected_into_overlapping_coder(tmp_path):
    async def go():
        async with _fusion(tmp_path, active=_ACTIVE) as (main, fake):
            (main.project / "stock").mkdir()
            (main.project / "stock" / "counts.py").write_text("COUNT = 1\n", encoding="utf-8")
            await _choose_turn(main, fake, 1, _update("stock", role="Counts items.", method="Totals rows."))
            record = _records(main)["map:stock"]
            first = len(fake.requests)
            await _turn(main, fake, _delegate(paths=["stock/counts.py"]), _rated_report(record), say("Reviewed."))
            prompt = _prompt(fake.requests[first + 1])
            assert "## Map entries" in prompt and record.id in prompt and "Role: Counts items." in prompt
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
            first = len(fake.requests)
            await _turn(main, fake, _delegate(paths=["stock/items.txt"]), say("Checked."), say("Reviewed."))
            prompt = _prompt(fake.requests[first + 1])
            assert "Counts packages." in prompt and "Counts boxes." not in prompt
            assert set(_records(main)) == {"map:counts"} and project_map.read(main.project) == edited
    asyncio.run(go())


@pytest.mark.parametrize("route", [False, True])
def test_only_writing_delegation_is_refused_before_location(tmp_path, route):
    async def go():
        async with _fusion(tmp_path, active=(*_ACTIVE, "route") if route else _ACTIVE) as (main, fake):
            await _turn(main, fake, _delegate(call_id="blocked"), _delegate("Runner", read_only=True), _report(), say("Reviewed."))
            assert "Call choose_map_location" in _output(main, "blocked")
            agent, = main.agents.values()
            assert agent.name == "Runner-Inventory" and agent.record.status == "done"
            assert main._fusion.drive.meta("map_location") is None
    asyncio.run(go())


def _status(main):
    return json.loads(main._fusion.drive.meta("map_status"))


def _write_inventory():
    return call("create_file", {"file_path": "inventory.txt", "content": "Two boxes."}, call_id="write")


def test_new_workstream_starts_unverified_and_joins_missing_check_continuation(tmp_path):
    async def go():
        async with _fusion(tmp_path, active=(*_ACTIVE, "route", "acceptance")) as (main, fake):
            await _choose_turn(main, fake, 0)
            first = len(fake.requests)
            fake.script(_route(), _delegate(), [("hold", "new-map"), *_write_inventory()])
            assert main.send_prompt("Count the inventory.")
            try:
                await until(lambda: len(fake.requests) == first + 3)
                record = _records(main)["map:Inventory"]
                assert record.paths == ("inventory.txt",) and _status(main) == {"Inventory": "unverified"}
                assert f"ID: {record.id}" in _prompt(fake.requests[first + 2])
                fake.script(_rated_report(record), say("Reviewed."), say("No check available."))
            finally:
                fake.release("new-map")
            await finish_turn(main)
            assert _status(main) == {"Inventory": "review"}
            assert len(fake.requests) == first + 6
            continuation = str(fake.requests[-1]["input"])
            assert "Missing checks: runner." in continuation and "Missing checks: runner, sampler." not in continuation
            assert "Describe the involved project map modules with update_map_entry: Inventory." in continuation
            refreshed = _records(main)[record.key]
            assert refreshed.id == record.id and refreshed.fingerprint != record.fingerprint
            assert refreshed.fingerprint == fingerprint(main.project, record.paths)
    asyncio.run(go())


@pytest.mark.parametrize("acceptance", [True, False])
def test_turn_refreshes_fingerprint_and_marks_only_involved_modules(tmp_path, acceptance):
    async def go():
        gates = (*_ACTIVE, "route", "acceptance") if acceptance else _ACTIVE
        async with _fusion(tmp_path, active=gates) as (main, fake):
            await _choose_turn(main, fake, 1, _update("stock", paths=["inventory.txt"], role="Counts items.", method="Totals rows."),
                               _update("other", paths=["other.txt"], call_id="other"))
            before = _records(main)
            route = [_route()] if acceptance else []
            check = [_delegate("Runner", call_id="verify"), _report()] if acceptance else []
            await _turn(main, fake, *route, _delegate(), _write_inventory(), _rated_report(before["map:stock"]), *check, say("Reviewed."))
            after = _records(main)
            changed = after["map:stock"]
            assert _status(main) == {"stock": "verified" if acceptance else "review", "other": "unverified"}
            assert changed.fingerprint != before["map:stock"].fingerprint
            assert changed.fingerprint == fingerprint(main.project, changed.paths)
            assert changed.updated == before["map:stock"].updated and after["map:other"] == before["map:other"]
    asyncio.run(go())


@pytest.mark.parametrize("active", [_ACTIVE[:-1], ("brief", "report", "inject", "map")])
def test_map_needs_both_gates(tmp_path, active):
    async def go():
        async with _fusion(tmp_path, active=active) as (main, fake):
            await _turn(main, fake, _delegate(), _report(), say("Reviewed."))
            assert len(main.agents) == 1 and "choose_map_location" not in _output(main, "delegate")
            names = {tool["name"] for tool in fake.requests[0]["tools"]}
            assert names.isdisjoint({"choose_map_location", "update_map_entry"})
            assert not (main.project / ".witty_map.md").exists()
    asyncio.run(go())


def test_project_file_write_failure_keeps_map_in_drive_and_notices_once(tmp_path):
    async def go():
        async with _fusion(tmp_path, active=_ACTIVE) as (main, fake):
            target = main.project / ".witty_map.md"
            target.mkdir()
            await _choose_turn(main, fake, 0, _update("stock", paths=["inventory.txt"], role="Counts items.", method="Totals rows."))
            record = _records(main)["map:stock"]
            await _turn(main, fake, _delegate(), _rated_report(record), say("Reviewed."))
            assert target.is_dir() and main._fusion.drive.meta("map_location") == "project"
            assert _records(main)["map:stock"].id == record.id and next(iter(main.agents.values())).record.status == "done"
            notices = [block.text for turn in main.transcript.turns for block in turn.blocks if isinstance(block, NoticeBlock)]
            assert sum("Fusion Drive 暂不可用" in text for text in notices) == 1
    asyncio.run(go())
