"""Phase 0b B (part 2): prompt cache after resume, settings changes, fork.

Continues the session created by cache_curve.py (id from
fixtures/cache/state.json) in a FRESH process:
  turn 5: plain resume turn (baseline for resume cache behavior)
  turn 6: after update_settings(model='gpt-6-luna')
  turn 7: after update_settings(model='claude-haiku-5-5', reasoning_effort=MEDIUM)
  turn 8: after fork() (fork retires the source session, so it goes last)

--only-fork re-runs just the fork leg under a different fixture tag (used
after a first run completed turns 5-7 but tripped on the fork return shape).

UsageTracker captures raw session_token_usage_changed / agent_turn_completed.
Writes fixtures/cache/<tag>.jsonl (+ .raw.jsonl) and <tag>_summary.json.
"""

from __future__ import annotations

import _pyguard  # noqa: F401  -- require Python 3.11.x

import argparse
import asyncio
import os
import time

from droid_sdk import InteractionHandlers, ReasoningEffort, Session

from _common import (
    Recorder,
    environment_info,
    make_permission_handler,
    make_runtime,
    run_turn,
    setup_console,
    to_jsonable,
)
from _ctx_common import (
    CACHE_FIXTURES,
    CACHE_SANDBOX,
    UsageTracker,
    load_json,
    save_json,
    transport_pid,
)


async def open_meta(session, recorder, **extra):
    started = time.perf_counter()
    await session.open()
    recorder.meta(
        "SessionOpened",
        session_id=session.id,
        cwd=session.cwd,
        settings=session.settings,
        open_seconds=round(time.perf_counter() - started, 3),
        **extra,
    )
    session.on_notification(recorder.raw)


async def close_meta(session, recorder):
    started = time.perf_counter()
    await session.close()
    recorder.meta("SessionClosed", session_id=session.id, close_seconds=round(time.perf_counter() - started, 3))


def as_session(outcome):
    """fork() returns a Session directly; compact()/rewind() return outcomes."""
    return getattr(outcome, "session", outcome)


async def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--alt-model", default="gpt-6-luna")
    parser.add_argument("--timeout", type=float, default=180.0)
    parser.add_argument("--tag", default="resume")
    parser.add_argument("--only-fork", action="store_true")
    args = parser.parse_args()

    state = load_json(CACHE_FIXTURES / "state.json")
    session_id = state["session_id"]
    base_model = state["model"]

    runtime, droid = make_runtime()
    recorder = Recorder(CACHE_FIXTURES / f"{args.tag}.jsonl", raw_path=CACHE_FIXTURES / f"{args.tag}.raw.jsonl")
    recorder.meta(
        "Environment",
        **environment_info(droid, scope=f"B-{args.tag}", resumed_session=session_id, only_fork=args.only_fork),
    )
    tracker = UsageTracker()

    turns = []
    fatal = None
    session = None
    fork_id = None
    try:
        os.chdir(CACHE_SANDBOX)
        session = Session.resume(
            session_id,
            interactions=InteractionHandlers(on_permission=make_permission_handler(recorder)),
            runtime=runtime,
        )
        await open_meta(session, recorder, phase=args.tag)
        session.on_notification(tracker.callback())
        recorder.meta("Process", pid=transport_pid(session))

        if not args.only_fork:
            steps = [
                ("resume_turn", None),
                ("model_change", {"model": args.alt_model}),
                ("model_and_reasoning_change", {"model": base_model, "reasoning_effort": ReasoningEffort.MEDIUM}),
            ]
            for index, (label, settings) in enumerate(steps, start=5):
                if settings is not None:
                    await session.update_settings(**settings)
                    recorder.meta("SettingsUpdated", **{k: str(v) for k, v in settings.items()})
                before = len(tracker.turn_results)
                result = await run_turn(session, recorder, f"Reply with exactly: p{index}", timeout=args.timeout)
                turns.append(
                    {
                        "label": label,
                        "prompt_index": index,
                        "settings": {k: str(v) for k, v in settings.items()} if settings else None,
                        "success": result.success,
                        "text": result.text,
                        "usage": to_jsonable(result.usage),
                        "tracker_turn": tracker.turn_results[before:],
                    }
                )
                print(f"turn {index} ({label}): success={result.success} usage={result.usage}", flush=True)

        fork = as_session(await session.fork(title="cache-fork-probe"))
        fork_id = fork.id
        recorder.meta("Forked", old_id=session_id, new_id=fork_id)
        session = None  # retired by fork
        fork.on_notification(recorder.raw)
        fork.on_notification(tracker.callback())
        before = len(tracker.turn_results)
        result = await run_turn(fork, recorder, "Reply with exactly: p8", timeout=args.timeout)
        turns.append(
            {
                "label": "fork_turn",
                "prompt_index": 8,
                "settings": None,
                "success": result.success,
                "text": result.text,
                "usage": to_jsonable(result.usage),
                "tracker_turn": tracker.turn_results[before:],
            }
        )
        print(f"turn 8 (fork_turn): success={result.success} usage={result.usage}", flush=True)
        await close_meta(fork, recorder)
    except Exception as exc:
        fatal = f"{type(exc).__name__}: {str(exc)[:300]}"
        print(f"FATAL: {fatal}", flush=True)
        if session is not None:
            try:
                await close_meta(session, recorder)
            except Exception as close_exc:
                recorder.meta("CloseError", error=f"{type(close_exc).__name__}: {str(close_exc)[:200]}")
    recorder.meta("UsageTracker", **tracker.summary())
    recorder.close()

    expected = 1 if args.only_fork else 4
    ok = fatal is None and len(turns) == expected and all(t["success"] for t in turns)
    save_json(
        CACHE_FIXTURES / f"{args.tag}_summary.json",
        {
            "resumed_session": session_id,
            "fork_session": fork_id,
            "only_fork": args.only_fork,
            "turns": turns,
            "fatal": fatal,
            "pass": ok,
        },
    )
    print(f"cache {args.tag}: {'PASS' if ok else 'FAIL'}", flush=True)
    return 0 if ok else 1


if __name__ == "__main__":
    setup_console()
    raise SystemExit(asyncio.run(main()))