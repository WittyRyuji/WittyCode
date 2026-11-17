"""Closing a Fusion conversation must reap active guarded ACP runners, without real model requests."""
from __future__ import annotations

import asyncio

import pytest

from desktop.core.fusion.registry import Policy
from tests.app_support import ready_core
from tests.fake_responses import call
from tests.fakes import until
from tests.fusion_acp_support import make_guarded_engine


@pytest.mark.parametrize("main_model", ["chatgpt:gpt-6.1-sol", "devin:swe-2-high"])
def test_shutdown_closes_an_active_acp_expert_and_its_main(tmp_path, main_model):
    async def go():
        harness = make_guarded_engine(tmp_path)
        core, model, _, _, _ = await ready_core(tmp_path, devin_engine=harness.engine)
        try:
            await until(lambda: any(item["source"] == "devin" for item in core.catalog.models), timeout=20)
            cid = (await core.call("session.new", {"projectId": str(tmp_path / "proj")}))["id"]
            main = core._by_cid[cid].controller
            main.enable_fusion(Policy(main_model, ("devin:swe-2-high",), "devin:swe-2-high", 0))
            if not main_model.startswith("devin:"):
                model.script(call("delegate_task", {"scope": "src", "model": "devin:swe-2-high", "role": "worker",
                                                   "task": "[fusion-slow] Wait for cancellation."}))
            assert main.send_prompt("[fusion-delegate-acp-slow] Start the expert.")
            def expert_running():
                return any(view.controller is not None and view.controller._acp is not None and view.controller._acp.active
                           for view in main.agents.values())
            await until(expert_running, timeout=30)
            child = next(iter(main.agents.values())).controller
            processes = [client._process for client in harness.clients if client.running]
            assert processes
            await asyncio.wait_for(core.shutdown(), timeout=30)
            assert main.state.value == child.state.value == "closed"
            assert child._acp.client is None and child._acp.server is None and not child._acp._connections
            if main._acp is not None:
                assert main._acp.client is None and main._acp.server is None and not main._acp._connections
            assert all(process.returncode is not None for process in processes)
            assert all(not client.running for client in harness.clients)
        finally:
            await core.shutdown()
    asyncio.run(go())
