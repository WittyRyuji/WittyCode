"""The Codex engine on the app's event loop (qasync over Qt, as in WittyCode), not only under ``asyncio.run``."""

from __future__ import annotations

import asyncio

from desktop.core.session_manager import SessionState
from tests.codex_support import _ENGINES, finish_turn, make_engine


def test_the_engine_runs_on_a_qasync_loop(tmp_path, loop):
    async def scenario():
        harness = make_engine(tmp_path)
        try:
            account = await harness.engine.account()
            assert account["auth"] == "chatgpt"
            controller = await harness.engine.open_new(tmp_path)
            assert controller.state is SessionState.READY
            turn = await finish_turn(controller, "[full] on qt")
            assert turn.blocks[-1].text == "Done: fixed."
            assert controller.send_prompt("[approve-command] go")
            await asyncio.sleep(0)
            while not controller.transcript.pending_prompts():
                await asyncio.sleep(0.01)
            (prompt,) = controller.transcript.pending_prompts()
            assert controller.answer_permission(prompt.id, "proceed_once")
            while controller.busy:
                await asyncio.sleep(0.01)
            assert controller.transcript.turns[-1].blocks[-1].text == "ran it (accept)"
        finally:
            await harness.engine.close_all()
            _ENGINES.clear()
        assert not harness.clients[0].running and harness.clients[0].exit_code == 0

    loop.run_until_complete(asyncio.wait_for(scenario(), 60))
