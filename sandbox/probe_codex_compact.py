"""The user's real signed-in Codex (the app's own CodexEngine): one tiny message, then /compact (``thread/compact/start``).
Two real model calls on the Codex plan; the thread is made in sandbox/codex-live-cwd. Prints what the page would show."""
from __future__ import annotations

import asyncio
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from desktop.core.codex.engine import CodexEngine  # noqa: E402
from desktop.core.session_manager import SessionState  # noqa: E402
from desktop.core.settings import AppSettings  # noqa: E402

WORK = ROOT / "sandbox" / "codex-live-cwd"


async def wait_state(controller, wanted: SessionState, timeout: float = 120.0) -> None:
    end = time.time() + timeout
    while controller.state is not wanted:
        if time.time() > end:
            raise TimeoutError(f"state stayed {controller.state}")
        await asyncio.sleep(0.2)


def show(controller) -> None:
    for turn in controller.transcript.turns:
        for block in turn.blocks:
            body = getattr(block, "text", None) or getattr(block, "message", None) or ""
            print(f"    [{turn.status.value}] {block.kind}: {str(body)[:200]!r}")


async def main() -> None:
    WORK.mkdir(exist_ok=True)
    engine = CodexEngine(AppSettings())
    try:
        models = await engine.list_models()
        model = sys.argv[1] if len(sys.argv) > 1 else models[0].id
        print("model:", model, flush=True)
        controller = await engine.open_new(WORK, model=model, reasoning="low")
        await wait_state(controller, SessionState.READY, 60)
        assert controller.send_prompt("Reply with the single word ok.")
        await wait_state(controller, SessionState.RUNNING, 10)
        await wait_state(controller, SessionState.READY)
        print("first turn done; context:", controller.context, flush=True)
        print("start_compaction ->", controller.start_compaction(), flush=True)
        started = time.time()
        await wait_state(controller, SessionState.RUNNING, 10)
        try:
            await wait_state(controller, SessionState.READY, 150)
            print(f"compaction turn over after {time.time() - started:.1f}s", flush=True)
        except TimeoutError as error:
            print("STUCK:", error, flush=True)
        print("context after:", controller.context, flush=True)
        show(controller)
        await controller.close()
    finally:
        await engine.close_all()


asyncio.run(main())
