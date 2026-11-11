"""Phase 0b B (part 1): prompt-cache curve, turns 1-4.

Turns 1-3 run back-to-back (warm cache expected from turn 2 on); turn 4 runs
after a configurable idle (default 360 s, beyond a typical 5-minute cache
TTL). Per-turn usage including cache read/creation tokens and factory_credits
is taken from the raw agent_turn_completed / session_token_usage_changed
notifications (UsageTracker).

Live turns: 4. Writes fixtures/cache/curve.jsonl (+ .raw.jsonl),
fixtures/cache/curve_summary.json and fixtures/cache/state.json (session id
for spikes/cache_resume.py). Wall time ~= idle_seconds + 4 turns; run it as a
background job.
"""

from __future__ import annotations

import _pyguard  # noqa: F401  -- require Python 3.11.x

import argparse
import asyncio
import time

from droid_sdk import Autonomy, InteractionHandlers, Session, SessionConfig

from _common import (
    DEFAULT_MODEL,
    DEFAULT_REASONING,
    Recorder,
    close_recorded,
    environment_info,
    make_permission_handler,
    make_runtime,
    open_recorded,
    reasoning_effort,
    run_turn,
    setup_console,
    to_jsonable,
)
from _ctx_common import (
    CACHE_FIXTURES,
    CACHE_SANDBOX,
    UsageTracker,
    save_json,
    transport_pid,
)

PROMPTS = [
    "Reply with exactly: p1",
    "Reply with exactly: p2",
    "Reply with exactly: p3",
    "Reply with exactly: p4",
]


async def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--model", default=DEFAULT_MODEL)
    parser.add_argument("--reasoning", default=DEFAULT_REASONING)
    parser.add_argument("--timeout", type=float, default=120.0)
    parser.add_argument("--idle-seconds", type=float, default=360.0)
    args = parser.parse_args()

    runtime, droid = make_runtime()
    recorder = Recorder(
        CACHE_FIXTURES / "curve.jsonl", raw_path=CACHE_FIXTURES / "curve.raw.jsonl"
    )
    recorder.meta(
        "Environment",
        **environment_info(
            droid,
            model=args.model,
            reasoning=args.reasoning,
            autonomy="off",
            idle_seconds=args.idle_seconds,
        ),
    )
    tracker = UsageTracker()
    session = Session(
        cwd=CACHE_SANDBOX,
        model=args.model,
        reasoning_effort=reasoning_effort(args.reasoning),
        config=SessionConfig(autonomy=Autonomy.OFF),
        interactions=InteractionHandlers(on_permission=make_permission_handler(recorder)),
        runtime=runtime,
    )
    turns = []
    try:
        await open_recorded(session, recorder)
        session.on_notification(tracker.callback())
        for prompt in PROMPTS[:3]:
            turns.append(await run_turn(session, recorder, prompt, timeout=args.timeout))
        recorder.meta("IdleBegin", seconds=args.idle_seconds)
        print(f"idle {args.idle_seconds:.0f}s before turn 4 ...", flush=True)
        deadline = time.perf_counter() + args.idle_seconds
        while True:
            remaining = deadline - time.perf_counter()
            if remaining <= 0:
                break
            await asyncio.sleep(min(remaining, 30.0))
        recorder.meta("IdleEnd")
        turns.append(await run_turn(session, recorder, PROMPTS[3], timeout=args.timeout))
        recorder.meta("ContextUsage", context=await session.context())
        recorder.meta("UsageTracker", **tracker.summary())
    finally:
        await close_recorded(session, recorder)
        recorder.close()

    summary = {
        "session_id": session.id,
        "model": args.model,
        "reasoning": args.reasoning,
        "pid": transport_pid(session),
        "idle_seconds": args.idle_seconds,
        "turns": [
            {
                "n": i + 1,
                "prompt": PROMPTS[i],
                "subtype": r.subtype,
                "success": r.success,
                "reply": r.text,
                "duration_s": r.duration.total_seconds(),
                "usage": to_jsonable(r.usage),
            }
            for i, r in enumerate(turns)
        ],
        "tracker": tracker.summary(),
    }
    save_json(CACHE_FIXTURES / "curve_summary.json", summary)
    save_json(
        CACHE_FIXTURES / "state.json",
        {"session_id": session.id, "model": args.model, "reasoning": args.reasoning},
    )
    ok = len(turns) == 4 and all(r.success for r in turns)
    print("cache curve:", "PASS" if ok else "FAIL", flush=True)
    return 0 if ok else 1


if __name__ == "__main__":
    setup_console()
    raise SystemExit(asyncio.run(main()))