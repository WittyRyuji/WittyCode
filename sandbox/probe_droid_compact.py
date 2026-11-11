"""The real droid.exe through the app's SessionManager: one tiny message on a cheap model, then /compact (Session.compact).
One real model call for the message and one for the summary. Prints what the page would show."""
from __future__ import annotations

import asyncio
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from desktop import _pyguard  # noqa: E402,F401
from desktop.core.session_manager import SessionManager, SessionState  # noqa: E402
from desktop.core.settings import AppSettings  # noqa: E402

WORK = ROOT / "sandbox" / "window-probe"


async def wait_state(controller, wanted, timeout=180.0):
    end = time.time() + timeout
    while controller.state is not wanted:
        if time.time() > end:
            raise TimeoutError(f"state stayed {controller.state}")
        await asyncio.sleep(0.2)


def show(controller):
    for turn in controller.transcript.turns:
        for block in turn.blocks:
            body = getattr(block, "text", None) or getattr(block, "message", None) or ""
            print(f"    [{turn.status.value}] {block.kind}: {str(body)[:200]!r}")


async def main():
    WORK.mkdir(parents=True, exist_ok=True)
    manager = SessionManager(AppSettings())
    controller = await manager.open_new(WORK, model=sys.argv[1] if len(sys.argv) > 1 else "claude-haiku-5-5", reasoning=None, autonomy="off")
    try:
        await wait_state(controller, SessionState.READY, 60)
        first_id = controller.session_id
        assert controller.send_prompt("Reply with the single word ok.")
        await wait_state(controller, SessionState.RUNNING, 10)
        await wait_state(controller, SessionState.READY)
        print("first turn done; session", first_id, "context", controller.context, flush=True)
        print("start_compaction ->", controller.start_compaction(), flush=True)
        started = time.time()
        await wait_state(controller, SessionState.RUNNING, 10)
        try:
            await wait_state(controller, SessionState.READY, 180)
            print(f"compaction over after {time.time() - started:.1f}s; session now {controller.session_id} (was {first_id})", flush=True)
        except TimeoutError as error:
            print("STUCK:", error, flush=True)
        await asyncio.sleep(1.5)
        print("context after:", controller.context, flush=True)
        show(controller)
        assert controller.send_prompt("Reply with the single word again.")
        await wait_state(controller, SessionState.RUNNING, 10)
        await wait_state(controller, SessionState.READY)
        print("a turn after the compaction works; session", controller.session_id, flush=True)
        show(controller)
    finally:
        await manager.close_all()


asyncio.run(main())
