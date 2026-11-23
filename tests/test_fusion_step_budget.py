"""Every delegation's step budget is resolved from the shared configured band and the task's size."""
from __future__ import annotations

import asyncio

import pytest

from desktop.core.app_core import AppError
from desktop.core.fusion import step_budget
from desktop.core.fusion.step_budget import Band, Side
from tests.app_support import ready_core
from tests.fake_responses import call, say
from tests.test_fusion_identity_runtime import WORKER, _brief, _fusion, _output, _turn

pytestmark = pytest.mark.fusion_gates


# --------------------------------------------------------------------------- pure helpers


def test_acceptance_items_count_discernible_list_lines():
    assert step_budget.acceptance_items("") == 0 and step_budget.acceptance_items("  ") == 0
    assert step_budget.acceptance_items("Counts match the inventory.") == 1
    assert step_budget.acceptance_items("- a passes\n- b passes") == 2
    assert step_budget.acceptance_items("1. first\n2) second\n3、third") == 3
    assert step_budget.acceptance_items("* one\n• two\n– three") == 3
    wrapped = "One long criterion\nthat keeps going on a second line."
    assert step_budget.acceptance_items(wrapped) == 1, "plain wrapped text stays one item"


def test_task_size_is_bounded_and_monotonic_in_each_signal():
    base = step_budget.task_size(paths=0, acceptance="", text="")
    assert base == 0
    bigger = step_budget.task_size(paths=1, acceptance="- a", text="x" * 1200)
    assert bigger == 1 + 0 + 1
    assert step_budget.task_size(paths=999, acceptance="\n".join(f"{i}. x" for i in range(99)), text="x" * 99999) == 8 + 7 + 4
    for paths, acceptance, text in ((0, "", ""), (2, "- a\n- b\n- c", "y" * 2500)):
        small = step_budget.task_size(paths=paths, acceptance=acceptance, text=text)
        assert small <= step_budget.task_size(paths=paths + 1, acceptance=acceptance, text=text)
        assert small <= step_budget.task_size(paths=paths, acceptance=acceptance + "\n- extra", text=text)
        assert small <= step_budget.task_size(paths=paths, acceptance=acceptance, text=text + "z" * 1000)


def test_the_adaptive_band_grows_from_16_32_and_stays_bounded():
    assert step_budget.adaptive_band(0) == (16, 32), "a minimal task suggests 16-32"
    assert step_budget.adaptive_band(4) == (32, 64)
    assert step_budget.adaptive_band(19) == (92, 184)
    assert step_budget.adaptive_band(100) == (96, 192)
    lows = [step_budget.adaptive_band(size) for size in range(20)]
    assert all(a[0] <= b[0] and a[1] <= b[1] for a, b in zip(lows, lows[1:])), "monotone in size"


@pytest.mark.parametrize(("low", "high", "expected", "source"), [
    (Side(True, 9), Side(True, 9), (16, 32), "adaptive 16-32"),
    (Side(False, 5), Side(False, 15), (5, 15), "manual 5-15"),
    (Side(True, 9), Side(False, 60), (16, 60), "adaptive min 16 + manual max 60"),
    (Side(False, 40), Side(True, 9), (40, 40), "manual min 40 + adaptive max 40"),
])
def test_resolve_band_mixes_manual_and_adaptive_sides(low, high, expected, source):
    band = step_budget.resolve_band(low, high, size=0, cap=1000)
    assert (band.low, band.high) == expected and band.source == source


def test_a_mixed_inversion_keeps_the_manual_side():
    # Adaptive ceiling 32 under a manual floor 50: the manual side wins.
    band = step_budget.resolve_band(Side(False, 50), Side(True, 9), size=0, cap=1000)
    assert (band.low, band.high) == (50, 50) and band.source == "manual min 50 + adaptive max 50"
    band = step_budget.resolve_band(Side(True, 9), Side(False, 10), size=4, cap=1000)
    assert (band.low, band.high) == (10, 10) and band.source == "adaptive min 10 + manual max 10"


def test_the_global_cap_truncates_last_and_is_named_in_the_source():
    band = step_budget.resolve_band(Side(True, 9), Side(True, 9), size=2, cap=10)
    assert (band.low, band.high) == (10, 10) and "global cap 10" in band.source
    band = step_budget.resolve_band(Side(False, 20), Side(False, 40), size=0, cap=5)
    assert (band.low, band.high) == (5, 5), "the cap wins even under a manual floor"


def test_effective_steps_clamps_the_request_and_defaults_to_the_ceiling():
    band = Band(20, 40, "x")
    assert step_budget.effective_steps(8, band) == 20
    assert step_budget.effective_steps(30, band) == 30
    assert step_budget.effective_steps(99, band) == 40
    assert step_budget.effective_steps(None, band) == 40
    assert step_budget.note(band, 8) == "x; requested 8" and step_budget.note(band, None) == "x"


# --------------------------------------------------------------------------- runtime


def _band(main, low=1, high=1, *, low_adaptive=False, high_adaptive=False):
    settings = main.manager.settings
    settings.fusion_steps_min, settings.fusion_steps_min_adaptive = low, low_adaptive
    settings.fusion_steps_max, settings.fusion_steps_max_adaptive = high, high_adaptive


def test_requested_steps_are_clamped_into_the_adaptive_band_for_every_identity(tmp_path):
    async def go():
        async with _fusion(tmp_path) as (main, fake):
            # The Coder brief declares one write path (size 1 -> 20-40); the read-only identities get size 0 -> 16-32.
            for identity, expected in (("Coder", 20), ("Runner", 16), ("QualitySampler", 16)):
                await _turn(main, fake, call("delegate_task", _brief(identity, budget={"steps": 8}), call_id=identity),
                            say("Done."), say("Reviewed."))
                agent = list(main.agents.values())[-1]
                assert agent.controller._fusion_step_limit == expected, identity
                assert agent.record.status == "done", "a report may be submitted well before the floor"
                # The record shows the effective count and where it came from.
                assert f"Budget: {expected} steps (adaptive {expected}-{'40' if expected == 20 else '32'}; requested 8)" in agent.record.task
    asyncio.run(go())


@pytest.mark.parametrize(("empty", "manual", "global_limit", "expected"), [
    (False, False, 100, 40),
    (True, True, 100, 15),
    (False, True, 10, 10),
], ids=["omitted-adaptive", "empty-manual", "omitted-global-cap"])
def test_default_budget_is_applied_and_reported(tmp_path, empty, manual, global_limit, expected):
    async def go():
        async with _fusion(tmp_path, max_seconds=12) as (main, fake):
            main.manager.settings.max_steps = global_limit
            if manual:
                _band(main, 5, 15)
            brief = _brief(budget={})
            if not empty:
                del brief["budget"]
            await _turn(main, fake, call("delegate_task", brief, call_id="default"), say("Done."), say("Reviewed."))
            agent, = main.agents.values()
            assert main._fusion.registry.calls == 1 and agent.record.status == "done"
            assert agent.controller._fusion_step_limit == expected
            assert f"Budget: {expected} steps, 12 seconds" in agent.record.task
            assert f"Default budget: {expected} steps, 12 seconds" in _output(main, "default")
    asyncio.run(go())


@pytest.mark.parametrize(("unit", "steps", "seconds", "expected"), [
    ("step", 1, 12, "1 step, 12 seconds"),
    ("time", 5, 1, "5 steps, 1 second"),
])
def test_default_budget_is_reported_when_expert_hits_its_limit(tmp_path, unit, steps, seconds, expected):
    async def go():
        async with _fusion(tmp_path, max_seconds=seconds) as (main, fake):
            _band(main, steps, steps)
            worker = (call("list_dir", {"directory_path": "."}) if unit == "step"
                      else [("hold", "expert"), *say("Too late.")])
            try:
                await _turn(main, fake, call("delegate_task", _brief(budget={}), call_id="delegate"),
                            worker, say("Reviewed the interruption."))
            finally:
                if unit == "time":
                    fake.release("expert")
            agent, = main.agents.values()
            assert agent.record.status == "interrupted" and main._fusion.registry.calls == 1
            receipt = _output(main, "delegate")
            assert f"exceeded its {unit} budget" in receipt
            assert f"Default budget: {expected}" in receipt
    asyncio.run(go())


def test_a_larger_brief_gets_a_larger_adaptive_band(tmp_path):
    async def go():
        async with _fusion(tmp_path) as (main, fake):
            brief = _brief("Coder", paths=["a.py", "b.py", "c.py"], acceptance="- a works\n- b works\n- c works")
            await _turn(main, fake, call("delegate_task", brief, call_id="big"), say("Done."), say("Reviewed."))
            agent, = main.agents.values()
            assert agent.controller._fusion_step_limit == 36, "size 5 -> adaptive 36-72, request 8 raised to the floor"
            assert "(adaptive 36-72; requested 8)" in agent.record.task
    asyncio.run(go())


def test_a_seconds_only_budget_gets_the_effective_ceiling(tmp_path):
    async def go():
        async with _fusion(tmp_path) as (main, fake):
            await _turn(main, fake, call("delegate_task", _brief("Runner", budget={"seconds": 60}), call_id="runner"),
                        say("Checked."), say("Reviewed."))
            agent, = main.agents.values()
            assert agent.controller._fusion_step_limit == 32
            assert "Budget: 32 steps, 60 seconds (adaptive 16-32)" in agent.record.task
    asyncio.run(go())


def test_a_manual_pair_and_a_mixed_pair_drive_the_limit(tmp_path):
    async def go():
        async with _fusion(tmp_path) as (main, fake):
            _band(main, 5, 15)
            await _turn(main, fake, call("delegate_task", _brief("Coder", budget={"steps": 30}), call_id="manual"),
                        say("Done."), say("Reviewed."))
            agent = list(main.agents.values())[-1]
            assert agent.controller._fusion_step_limit == 15
            assert "(manual 5-15; requested 30)" in agent.record.task
            # A manual floor above the adaptive ceiling keeps the manual side; the adaptive side moves to it.
            _band(main, 50, 9, high_adaptive=True)
            await _turn(main, fake, call("delegate_task", _brief("Runner", budget={"steps": 8}), call_id="mixed"),
                        say("Done."), say("Reviewed."))
            agent = list(main.agents.values())[-1]
            assert agent.controller._fusion_step_limit == 50
            assert "(manual min 50 + adaptive max 50; requested 8)" in agent.record.task
    asyncio.run(go())


def test_the_global_per_turn_cap_still_wins_below_the_floor(tmp_path):
    async def go():
        async with _fusion(tmp_path) as (main, fake):
            main.manager.settings.max_steps = 10
            await _turn(main, fake, call("delegate_task", _brief("Coder", budget={"steps": 8}), call_id="low"),
                        say("Done."), say("Reviewed."))
            agent, = main.agents.values()
            assert agent.controller._fusion_step_limit == 10
            assert "global cap 10" in agent.record.task and "requested 8" in agent.record.task
    asyncio.run(go())


def test_the_legacy_delegation_entry_gets_the_adaptive_ceiling(tmp_path):
    async def go():
        async with _fusion(tmp_path) as (main, fake):
            fake.script(say("Old expert text."))
            result = await main._fusion.delegate("Inventory", WORKER, "worker", "Read totals.", ("inventory.txt",))
            assert not result.is_error
            agent, = main.agents.values()
            assert agent.controller._fusion_step_limit == 40, "no request: the adaptive ceiling applies"
            assert "Budget: 40 steps (adaptive 20-40)" in agent.record.task
    asyncio.run(go())


def test_the_pair_is_read_live_at_delegation_time(tmp_path):
    async def go():
        async with _fusion(tmp_path) as (main, fake):
            _band(main, 5, 15)
            await _turn(main, fake, call("delegate_task", _brief("Coder", budget={"steps": 30}), call_id="one"),
                        say("Done."), say("Reviewed."))
            assert list(main.agents.values())[-1].controller._fusion_step_limit == 15
            _band(main, 60, 90)
            await _turn(main, fake, call("delegate_task", _brief("Coder", workstream="Ledger", paths=["ledger.txt"],
                                                                 budget={"steps": 30}), call_id="two"),
                        say("Done."), say("Reviewed."))
            assert list(main.agents.values())[-1].controller._fusion_step_limit == 60
    asyncio.run(go())


def test_the_band_setting_is_saved_and_rejected_atomically(tmp_path):
    async def go():
        core, _, events, _, _ = await ready_core(tmp_path)
        view = core.settings_view()
        assert (view["fusionStepsMin"], view["fusionStepsMinAdaptive"], view["fusionStepsMax"], view["fusionStepsMaxAdaptive"]) == (16, True, 32, True)
        view = await core.call("settings.set", {"key": "fusionSteps",
                                                "value": {"min": 5, "minAdaptive": False, "max": 15, "maxAdaptive": True}})
        assert (view["fusionStepsMin"], view["fusionStepsMinAdaptive"], view["fusionStepsMax"], view["fusionStepsMaxAdaptive"]) == (5, False, 15, True)
        assert (core.settings.fusion_steps_min, core.settings.fusion_steps_max_adaptive) == (5, True)
        saved = events.last("settings")["settings"]
        assert (saved["fusionStepsMin"], saved["fusionStepsMaxAdaptive"]) == (5, True)
        # A manual floor above an adaptive ceiling is allowed; only a both-manual inversion is refused.
        await core.call("settings.set", {"key": "fusionSteps",
                                         "value": {"min": 90, "minAdaptive": False, "max": 10, "maxAdaptive": True}})
        assert (core.settings.fusion_steps_min, core.settings.fusion_steps_max_adaptive) == (90, True)
        for value in ({"min": 91, "minAdaptive": False, "max": 90, "maxAdaptive": False},
                      {"min": 5, "minAdaptive": False, "max": 15},
                      {"min": 5, "minAdaptive": False, "max": 15, "maxAdaptive": True, "extra": 1},
                      {"min": 0, "minAdaptive": False, "max": 15, "maxAdaptive": False},
                      {"min": 5, "minAdaptive": "yes", "max": 15, "maxAdaptive": False},
                      {"min": 5.5, "minAdaptive": False, "max": 15, "maxAdaptive": False},
                      "5-15", [5, False, 15, False]):
            with pytest.raises(AppError):
                await core.call("settings.set", {"key": "fusionSteps", "value": value})
        assert (core.settings.fusion_steps_min, core.settings.fusion_steps_max_adaptive) == (90, True), "refused pairs change nothing"
        await core.shutdown()
    asyncio.run(go())
