"""All three Fusion positions accept every registered backend, through real transports to local test doubles."""
from __future__ import annotations

import asyncio
from collections import Counter
import json

import pytest

from tests.chatgpt_support import finish_turn
from tests.fake_responses import call, say
from tests.fusion_backend_support import PROFILES, RUNNERS, api_call, configure, factory_turns, peers
from tests.test_app_api import says


MATRIX_GOAL = "Matrix resolver goal: confirm the worktree evidence."


def _unit_facts():
    return {"units": [{"id": "matrix", "task": "Confirm the worktree evidence without changing files.",
                       "basis": "The project fixture contains evidence.txt with known contents.",
                       "acceptance": "The evidence text is reported exactly.", "paths": [],
                       "J": 4, "I": 4, "C": 4, "review_required": True}]}


def _activity(tmp_path, plan, api):
    devin_lines = sum(len(path.read_text(encoding="utf-8").splitlines())
                      for path in (tmp_path / "guarded").rglob("received.jsonl"))
    return len(plan.requests), len(api.posts), len(factory_turns(tmp_path)), devin_lines


def _assert_unit_binding(key, frozen, models, worker_runner, light_runner, review_runner):
    assert len(key) == 64 and not frozen["activated"]
    unit = frozen["spec"]["units"]["matrix"]
    assert frozen["spec"]["goal"] == MATRIX_GOAL
    assert unit["facts"] == {"N": 50, "J": 4, "I": 4, "C": 4,
                             "review_required": True, "risk": False, "force_lead": False}
    assert unit["derived"]["worker_seat"] == "L2"
    worker, runner, review = (unit["roster"][stage] for stage in ("worker", "runner", "review"))
    assert (worker["seat"], worker["identity"], worker["expert"], worker["role"]) == ("L2", "Coder", "Coder", "worker")
    assert (runner["seat"], runner["identity"], runner["expert"], runner["role"]) == ("L3", "Runner", "Runner", "light")
    assert (review["identity"], review["expert"], review["role"]) == ("QualitySampler", "QualitySampler", "worker")
    assert review["seat"] == ("L3" if review_runner == light_runner else "L1")
    assert worker["model"] == models[worker_runner] and worker["actual_model"] == PROFILES[worker_runner][0]
    assert runner["model"] == models[light_runner] and runner["actual_model"] == PROFILES[light_runner][0]
    assert review["model"] == models[review_runner] and review["actual_model"] == PROFILES[review_runner][0]
    assert review["actual_model"].casefold() != worker["actual_model"].casefold()
    assert unit["author"]["actual_model"] == worker["actual_model"]


@pytest.mark.parametrize("main_runner", RUNNERS)
@pytest.mark.parametrize("worker_runner", RUNNERS)
@pytest.mark.parametrize("light_runner", RUNNERS)
def test_fusion_provider_matrix(tmp_path, monkeypatch, main_runner, worker_runner, light_runner):
    async def scenario():
        async with peers(tmp_path, monkeypatch) as (core, models, plan, api):
            main_id, worker_id, light_id = (models[runner] for runner in (main_runner, worker_runner, light_runner))
            project = tmp_path / "proj"
            (project / "evidence.txt").write_text("Matrix evidence from the worktree.", encoding="utf-8")
            source = "api" if main_runner == "plan" else "plan"
            source_reply = (lambda text: api.script(says(text))) if source == "api" else (lambda text: plan.script(say(text)))
            cid = (await core.call("session.new", {"projectId": str(project), "modelId": models[source],
                                                   "settings": {"reasoning": PROFILES[source][1]}}))["id"]
            main = core._by_cid[cid].controller
            source_reply("Original result from the source.")
            assert (await core.call("session.send", {"id": cid, "text": "Record a source turn"}))["ok"]
            await finish_turn(main)
            await configure(core, models, main_runner, worker_runner, light_runner)
            assert (await core.call("session.choose", {"id": cid, "field": "model", "value": "fusion"}))["ok"]

            experts = {"worker": worker_runner, "light": light_runner}
            arguments = {"tasks": [{"workstream": f"{role} check", "paths": [f"{role}.txt"], "model": models[runner],
                "role": role, "task": "[fusion-read] Confirm the worktree evidence."}
                for role, runner in experts.items()]}
            source_reply("Source summary for the Fusion handoff.")
            run_count = Counter(experts.values())
            if main_runner == "plan":
                plan.script(call("delegate_tasks", arguments))
            if main_runner == "api":
                api.script(api_call(arguments))
            plan.script(*(say("Plan expert report.") for _ in range(run_count["plan"])))
            api.script(*(says("API expert report.") for _ in range(run_count["api"])))
            if main_runner == "plan":
                plan.script(say("Reviewed both expert reports."))
            if main_runner == "api":
                api.script(says("Reviewed both expert reports."))

            prompt = "Review both expert reports"
            if main_runner in ("devin", "factory"):
                prompt = "[fusion-matrix] " + json.dumps(arguments)
            assert (await core.call("session.send", {"id": cid, "text": prompt}))["ok"]
            await finish_turn(main)
            assert len(main.transcript.turns) == 2 and [t.status for t in main.saved.turns] == ["done", "done"]
            assert main._fusion.registry.policy.main == main_id and main.saved.compaction["summary"].startswith("Source summary")
            assert {view.record.role: view.record.model for view in main.agents.values()} == {"worker": worker_id, "light": light_id}
            output = next(item["output"] for item in reversed(main.saved.items) if item.get("type") == "function_call_output")
            reports = {"devin": "Matrix evidence from the worktree.", "plan": "Plan expert report.", "api": "API expert report.", "factory": "Factory report: "}
            for expert in main.agents.values():
                controller = expert.controller
                assert expert.record.status == "done" and [t.status for t in controller.saved.turns] == ["done"], output
                assert reports[experts[expert.record.role]] in str(controller.saved.items)
                assert reports[experts[expert.record.role]] in output
                if experts[expert.record.role] == "factory":
                    assert "Matrix evidence from the worktree." in str(controller.saved.items) and "Matrix evidence from the worktree." in output
            assert [r["model"] for r in plan.requests] == [PROFILES["plan"][0]] * (2 * (source == "plan") + run_count["plan"] + 2 * (main_runner == "plan"))
            assert [body["model"] for _, _, body in api.posts] == [PROFILES["api"][0]] * (2 * (source == "api") + run_count["api"] + 2 * (main_runner == "api"))
            assert all(r["reasoning"]["effort"] == PROFILES["plan"][1] for r in plan.requests)
            assert all(body["reasoning_effort"] == PROFILES["api"][1] for _, _, body in api.posts)
            turns = factory_turns(tmp_path)
            assert Counter(t["action"] for t in turns) == Counter({"delegate": int(main_runner == "factory"), "expert": run_count["factory"]})
            for t in turns:
                assert (t["model"], t["effort"]) == PROFILES["factory"]
                if t["action"] == "delegate":
                    assert "Source summary" in t["prompt"]
            logs = [tuple(json.loads(line) for line in path.read_text(encoding="utf-8").splitlines())
                    for path in (tmp_path / "guarded").rglob("received.jsonl")]
            assert len(logs) == run_count["devin"] + (main_runner == "devin")
            for requests in logs:
                assert [r["params"]["value"] for r in requests if r.get("method") == "session/set_config_option"
                        and r["params"].get("configId") == "model"] == [PROFILES["devin"][0]]
                assert [r["params"]["value"] for r in requests if r.get("method") == "session/set_config_option"
                        and r["params"].get("configId") == "thought_level"] == [PROFILES["devin"][1]]
                assert sum(r.get("method") == "session/prompt" for r in requests) == 1

            before = _activity(tmp_path, plan, api)
            main.saved.goal = {"text": MATRIX_GOAL}
            reviewable = worker_runner != main_runner or worker_runner != light_runner
            if reviewable:
                key, frozen = main._fusion._resolve_units(_unit_facts())
                expected_review = light_runner if light_runner != worker_runner else main_runner
                _assert_unit_binding(key, frozen, models, worker_runner, light_runner, expected_review)
                assert not main._fusion.registry.plans
            else:
                with pytest.raises(ValueError, match="no independent L1, L2 or L3 model"):
                    main._fusion._resolve_units(_unit_facts())
            assert _activity(tmp_path, plan, api) == before
    asyncio.run(scenario())
