"""Spike (e): interrupt a multi-step turn right after its first tool event, then
check that the same session still runs a follow-up turn.

Writes fixtures/NAME.jsonl (+ .raw.jsonl); NAME defaults to "interrupt".
Example that interrupts while a shell command is running:
  python spikes/interrupt.py --trigger ToolProgress,ToolResult --name interrupt_running_tool --no-follow-up
"""

from __future__ import annotations

import argparse
import asyncio
import time

from droid_sdk import (
    Autonomy,
    InteractionHandlers,
    RunInterrupted,
    Session,
    SessionConfig,
    ToolCall,
    ToolCallDelta,
    ToolProgress,
    ToolResult,
)

from _common import (
    DEFAULT_MODEL,
    DEFAULT_REASONING,
    FIXTURES,
    SANDBOX,
    Recorder,
    close_recorded,
    environment_info,
    make_permission_handler,
    make_runtime,
    open_recorded,
    prepare_sandbox,
    reasoning_effort,
    run_turn,
    setup_console,
)

PROMPT = (
    "Count slowly: run 5 separate shell commands, one tool call at a time and one after another, "
    "each printing a single number from 1 to 5 and then pausing 3 seconds."
)
FOLLOW_UP = "Reply with just the word ok."
TOOL_EVENTS = {"ToolCallDelta": ToolCallDelta, "ToolCall": ToolCall, "ToolProgress": ToolProgress, "ToolResult": ToolResult}


async def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--model", default=DEFAULT_MODEL)
    parser.add_argument("--reasoning", default=DEFAULT_REASONING)
    parser.add_argument("--timeout", type=float, default=180.0)
    parser.add_argument(
        "--trigger",
        default="any",
        help="comma-separated event types that trigger session.interrupt(): "
        f"any or {','.join(TOOL_EVENTS)} (default: first tool event of any kind)",
    )
    parser.add_argument("--name", default="interrupt", help="fixture base name")
    parser.add_argument("--no-follow-up", action="store_true")
    args = parser.parse_args()
    names = [n.strip() for n in args.trigger.split(",") if n.strip()]
    unknown = [n for n in names if n != "any" and n not in TOOL_EVENTS]
    if unknown or not names:
        parser.error(f"unknown trigger(s): {unknown}")
    trigger_types = tuple(TOOL_EVENTS.values()) if "any" in names else tuple(TOOL_EVENTS[n] for n in names)

    prepare_sandbox()
    runtime, droid = make_runtime()
    recorder = Recorder(FIXTURES / f"{args.name}.jsonl", raw_path=FIXTURES / f"{args.name}.raw.jsonl")
    recorder.meta(
        "Environment",
        **environment_info(droid, model=args.model, reasoning=args.reasoning, autonomy="off", cwd=str(SANDBOX),
                           trigger=args.trigger),
    )
    session = Session(
        cwd=SANDBOX,
        model=args.model,
        reasoning_effort=reasoning_effort(args.reasoning),
        config=SessionConfig(autonomy=Autonomy.OFF),
        interactions=InteractionHandlers(on_permission=make_permission_handler(recorder)),
        runtime=runtime,
    )
    state: dict[str, object] = {"task": None, "requested_at": None, "events_after": []}

    def on_event(event: object) -> None:
        if state["task"] is None and isinstance(event, trigger_types):
            state["requested_at"] = time.perf_counter()
            recorder.meta("InterruptRequested", trigger=type(event).__name__, turn_t=round(recorder.turn_now(), 4))
            recorder.timeline.note(recorder.turn_now(), f"[spike] session.interrupt() after {type(event).__name__}")
            # A separate task, like a UI stop button, so the consumer keeps draining the stream.
            state["task"] = asyncio.create_task(session.interrupt())
        elif state["task"] is not None:
            state["events_after"].append(type(event).__name__)

    first = follow = None
    try:
        await open_recorded(session, recorder)
        first = await run_turn(session, recorder, PROMPT, timeout=args.timeout, on_event=on_event)
        latency = None
        if state["task"] is not None:
            await state["task"]
            latency = time.perf_counter() - state["requested_at"]
        recorder.meta(
            "InterruptOutcome",
            result_type=type(first).__name__,
            subtype=first.subtype,
            seconds_from_interrupt_to_result=None if latency is None else round(latency, 3),
            events_after_interrupt=state["events_after"],
        )
        print(f"turn 1 -> {type(first).__name__} ({first.subtype}); interrupt->result "
              f"{'n/a' if latency is None else f'{latency:.2f}s'}; events after interrupt: {state['events_after']}")
        if not args.no_follow_up:
            follow = await run_turn(session, recorder, FOLLOW_UP, timeout=args.timeout)
            print(f"turn 2 -> {type(follow).__name__} ({follow.subtype}) reply={follow.text!r}")
    finally:
        await close_recorded(session, recorder)
        recorder.close()
    recorder.print_summary()
    ok = isinstance(first, RunInterrupted) and (args.no_follow_up or (follow is not None and follow.success))
    return 0 if ok else 1


if __name__ == "__main__":
    setup_console()
    raise SystemExit(asyncio.run(main()))
