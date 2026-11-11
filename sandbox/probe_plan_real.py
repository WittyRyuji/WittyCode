"""Real requests through WittyCode's own loop with the user's signed-in ChatGPT Plan (a few small ones): /compact on a made-up conversation,
the model's ask_user and Plan mode (/goal runs in AppCore now: sandbox/probe_skill_goal_real.py). Sessions are written to a temp folder, not to the application's list; the account is the real
one (its tokens are read and, when they expire, refreshed exactly as the application does). Never prints a token."""
from __future__ import annotations

import asyncio
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from desktop.core.chatgpt.engine import ChatGPTEngine  # noqa: E402
from desktop.core.chatgpt.responses import user_message  # noqa: E402
from desktop.core.harness import compact  # noqa: E402
from desktop.core.harness.store import SavedTurn, SessionStore  # noqa: E402
from desktop.core.session_manager import SessionState  # noqa: E402
from desktop.core.settings import AppSettings, default_settings_path  # noqa: E402

ONLY = set(a for a in sys.argv[1:] if not a.startswith("-"))
WORK = Path(tempfile.mkdtemp(prefix="wc-plan-probe-"))
PROJECT = WORK / "project"
PROJECT.mkdir()


def assistant(text: str) -> dict:
    return {"type": "message", "role": "assistant", "content": [{"type": "output_text", "text": text, "annotations": []}]}


async def done(controller, timeout: float = 120.0, on_prompt=None) -> None:
    end = time.monotonic() + timeout
    while not (controller.state is SessionState.READY and controller._turn is None):
        if time.monotonic() > end:
            raise TimeoutError("the turn did not end")
        if on_prompt is not None:
            on_prompt(controller)
        await asyncio.sleep(0.1)


def texts(controller) -> list[str]:
    return [b.text for t in controller.transcript.turns for b in t.blocks if b.kind == "text"]


def notices(controller) -> list[str]:
    return [b.text for t in controller.transcript.turns for b in t.blocks if b.kind == "notice"]


def show(controller, since: int = 0) -> None:
    for turn in controller.transcript.turns[since:]:
        for block in turn.blocks:
            body = getattr(block, "text", None) or getattr(block, "message", None) or ""
            print(f"    [{turn.status.value}] {block.kind}: {str(body)[:220]!r}")


async def main() -> None:
    engine = ChatGPTEngine(AppSettings(), default_settings_path().parent / "chatgpt")
    engine.sessions = SessionStore(WORK / "sessions")
    await engine.ensure_ready()
    listed = await engine.list_models()
    models = [(m.id, m.display_name, m.efforts, engine.model_window(m.id)) for m in listed]
    print("models:", models)
    cheap = next((m for m in listed if "luna" in m.id.lower()), None) or listed[-1]
    model = cheap.id
    effort = "low" if "low" in cheap.efforts else (cheap.efforts[0] if cheap.efforts else "")
    print("using", model, "effort", effort or "-", "window", engine.model_window(model))

    async def fresh(**more):
        return await engine.open_new(PROJECT, model=model, reasoning=effort, autonomy="full", **more)

    if not ONLY or "compact" in ONLY:
        print("== /compact")
        c = await fresh()
        for n in range(6):
            start = len(c.saved.items)
            first = "Remember this: my project's codename is KESTREL-42. " if n == 0 else ""
            c.saved.items += [user_message(first + f"Question {n}: " + "tell me about caching strategies in detail. " * 40), assistant(f"Answer {n}: " + "caches hold copies of data close to where it is used. " * 40)]
            c.saved.turns.append(SavedTurn(start, 1.0 + n, 2.0 + n, "done", "", model))
        before = compact.estimate_items(compact.view_of(c.saved.items, c.saved.compaction))
        print("  log items:", len(c.saved.items), "~tokens:", before)
        problem = c.start_compaction()
        print("  start:", problem)
        await done(c)
        record = c.saved.compaction
        print("  record:", {k: record.get(k) for k in ("upto", "before", "after", "model", "mechanical")})
        print("  summary head:", record.get("summary", "")[:300].replace("\n", " | "))
        print("  usage:", c.usage)
        show(c)
        c.send_prompt("What is my project's codename? Answer with the codename only.")
        await done(c)
        print("  answer after compaction:", texts(c)[-1:])
        await c.close()

    if not ONLY or "ask" in ONLY:
        print("== ask_user")
        c = await fresh()
        answered = []

        def answer(controller) -> None:
            for prompt_id in list(controller._pending):
                prompt = controller.transcript.prompt(prompt_id)
                if prompt is not None and prompt.status.value == "pending" and prompt_id not in answered:
                    answered.append(prompt_id)
                    print("  question shown:", type(prompt).__name__)
                    controller.answer_question(prompt_id, ["blue"])

        c.send_prompt("Use the ask_user tool to ask me which colour I prefer, with the options red and blue. Then tell me the colour I chose in one word.")
        await done(c, on_prompt=answer)
        print("  answered prompts:", len(answered))
        show(c)
        await c.close()

    if not ONLY or "plan" in ONLY:
        print("== Plan mode")
        c = await fresh()
        await c.update_settings(mode="spec")
        target = PROJECT / "plan_test.txt"
        c.send_prompt("Create a file named plan_test.txt containing the word hi.")
        await done(c)
        print("  file exists in Plan mode (should be False):", target.exists())
        show(c)
        await c.update_settings(mode="auto")
        c.send_prompt("Good plan. Go ahead.")
        await done(c)
        print("  file exists after Auto (should be True):", target.exists())
        show(c, 1)
        await c.close()

    await engine.close_all()
    print("work folder:", WORK)


asyncio.run(main())
