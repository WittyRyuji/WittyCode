"""All three Fusion positions accept all three runners, through real transports to local test doubles."""
from __future__ import annotations

import asyncio
from collections import Counter
import json
import urllib.request

import pytest

from desktop.core.chatgpt.http import HttpClient
from desktop.core.llm.backend import ApiBackend
from desktop.core.providers import ProviderStore
from tests.app_support import ready_core
from tests.chatgpt_support import finish_turn, make_engine
from tests.fake_responses import FakeResponses, call, say
from tests.fakes import until
from tests.fusion_acp_support import make_guarded_engine
from tests.llm_support import FakeApi
from tests.test_app_api import chunk, says

RUNNERS = ("devin", "plan", "api")
DEVIN, PLAN = "devin:swe-2-high", "chatgpt:gpt-6-luna"


def api_call(arguments):
    return [chunk({"tool_calls": [{"index": 0, "id": "matrix_delegate", "type": "function",
                                  "function": {"name": "delegate_tasks", "arguments": json.dumps(arguments)}}]},
                  finish="tool_calls"), "[DONE]"]


@pytest.mark.parametrize("main_runner", RUNNERS)
@pytest.mark.parametrize("worker_runner", RUNNERS)
@pytest.mark.parametrize("light_runner", RUNNERS)
def test_fusion_provider_matrix(tmp_path, main_runner, worker_runner, light_runner):
    async def scenario():
        with FakeResponses() as plan, FakeApi() as api:
            profile = tmp_path / "models.json"
            profile.write_text(json.dumps({"customModels": [{"model": "matrix-chat", "displayName": "Matrix API",
                "baseUrl": api.base_url, "apiKey": "${MATRIX_TEST_KEY}", "provider": "generic-chat-completion-api",
                "maxContextLimit": 64_000}]}), encoding="utf-8")
            providers = ProviderStore(models_file=profile, sidecar=tmp_path / "providers.json",
                                      environ={"MATRIX_TEST_KEY": "test-fixture"}, registry_env=lambda: {})
            http = HttpClient(urllib.request.build_opener(urllib.request.ProxyHandler({})))
            engine = make_engine(tmp_path, plan, api=ApiBackend(providers, http=http))
            devin = make_guarded_engine(tmp_path, FAKE_FUSION_BRIDGE_NOTICE="id-only")
            core, _, _, _, _ = await ready_core(tmp_path, chatgpt_engine=engine, devin_engine=devin.engine, providers=providers)
            try:
                await until(lambda: core.catalog.find(DEVIN) and core.catalog.find(PLAN), timeout=20)
                (api_model,) = [entry["id"] for entry in core.catalog.models if entry["source"].startswith("api:")]
                models = {"devin": DEVIN, "plan": PLAN, "api": api_model}
                main_id, worker_id, light_id = (models[runner] for runner in (main_runner, worker_runner, light_runner))
                project = tmp_path / "proj"
                (project / "evidence.txt").write_text("Matrix evidence from the worktree.", encoding="utf-8")
                cid = (await core.call("session.new", {"projectId": str(project), "modelId": "chatgpt:gpt-6.1-sol"}))["id"]
                main = core._by_cid[cid].controller
                plan.script(say("Original result from the source."))
                assert (await core.call("session.send", {"id": cid, "text": "Record a source turn"}))["ok"]
                await finish_turn(main)
                for key, value in {"fusionMain": main_id, "fusionWorkers": [worker_id], "fusionLight": light_id}.items():
                    await core.call("settings.set", {"key": key, "value": value})
                assert (await core.call("session.choose", {"id": cid, "field": "model", "value": "fusion"}))["ok"]

                experts = {"worker": worker_runner, "light": light_runner}
                arguments = {"tasks": [{"workstream": f"{role} check", "paths": [f"{role}.txt"], "model": models[runner],
                    "role": role, "task": "[fusion-read] Confirm the worktree evidence."}
                    for role, runner in experts.items()]}
                plan.script(say("Source summary for the Fusion handoff."))
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
                if main_runner == "devin":
                    prompt = "[fusion-matrix] " + json.dumps(arguments)
                assert (await core.call("session.send", {"id": cid, "text": prompt}))["ok"]
                await finish_turn(main)
                assert len(main.transcript.turns) == 2 and main.saved.turns[-1].status == "done", main.saved.turns[-1].error
                assert main._fusion.registry.policy.main == main_id and main.saved.compaction["summary"].startswith("Source summary")
                assert {view.record.role: view.record.model for view in main.agents.values()} == {"worker": worker_id, "light": light_id}
                output = next(item["output"] for item in reversed(main.saved.items) if item.get("type") == "function_call_output")
                reports = {"devin": "Matrix evidence from the worktree.", "plan": "Plan expert report.", "api": "API expert report."}
                for expert in main.agents.values():
                    controller = expert.controller
                    assert expert.record.status == "done" and controller.saved.turns[-1].status == "done", controller.saved.turns[-1].error
                    assert reports[experts[expert.record.role]] in str(controller.saved.items)
                    assert reports[experts[expert.record.role]] in output
                assert [r["model"] for r in plan.requests] == ["gpt-6.1-sol"] * 2 + ["gpt-6-luna"] * (run_count["plan"] + 2 * (main_runner == "plan"))
                assert [body["model"] for _, _, body in api.posts] == ["matrix-chat"] * (run_count["api"] + 2 * (main_runner == "api"))
                logs = [tuple(json.loads(line) for line in path.read_text(encoding="utf-8").splitlines())
                        for path in (tmp_path / "guarded").rglob("received.jsonl")]
                assert len(logs) == run_count["devin"] + (main_runner == "devin")
                for requests in logs:
                    assert [r["params"]["value"] for r in requests if r.get("method") == "session/set_config_option"
                            and r["params"].get("configId") == "model"] == ["swe-2-high"]
                    assert sum(r.get("method") == "session/prompt" for r in requests) == 1
            finally:
                await core.shutdown()
    asyncio.run(scenario())
