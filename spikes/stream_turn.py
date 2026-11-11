"""Spike (b): one streamed turn with partial events, Autonomy.OFF and an auto-approving
(PROCEED_ONCE, sandbox-checked) permission handler.

The prompt makes Droid use LS, Read, Create and Edit so the fixture covers the
tool-call life cycle and the permission/diff payloads of file edits.

Writes (NAME defaults to "stream_turn"):
  fixtures/NAME.jsonl      every SDK stream event + handler calls + meta records
  fixtures/NAME.raw.jsonl  raw wire notifications (session.on_notification)
  fixtures/tools.json      session.list_tools(include_schemas=True)
"""

from __future__ import annotations

import argparse
import asyncio
import json
import time

from droid_sdk import Autonomy, DroidError, InteractionHandlers, Session, SessionConfig

from _common import (
    DEFAULT_MODEL,
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
    to_jsonable,
)

PROMPT = (
    "List the files here, read README.txt, then create hello.txt containing 'hi', "
    "then edit hello.txt so it says 'hi there'. Then reply with one sentence."
)
EXPECTED_CONTENT = "hi there"


async def dump_tools(session: Session) -> None:
    started = time.perf_counter()
    try:
        tools = await session.list_tools(include_schemas=True)
        with_schemas = True
    except DroidError as exc:
        print(f"list_tools(include_schemas=True) failed ({exc}); retrying without schemas")
        tools = await session.list_tools()
        with_schemas = False
    elapsed = time.perf_counter() - started
    (FIXTURES / "tools.json").write_text(
        json.dumps(
            {"elapsed_s": round(elapsed, 3), "with_schemas": with_schemas, "tools": to_jsonable(tools)},
            ensure_ascii=False,
            indent=2,
        )
        + "\n",
        encoding="utf-8",
        newline="\n",
    )
    print(f"list_tools: {len(tools)} tools in {elapsed:.2f}s -> fixtures/tools.json")


async def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--model", default=DEFAULT_MODEL)
    # Higher effort than the other spikes so the fixture contains streamed thinking.
    parser.add_argument("--reasoning", default="high")
    parser.add_argument("--timeout", type=float, default=240.0)
    parser.add_argument("--name", default="stream_turn", help="fixture base name")
    parser.add_argument("--skip-tools", action="store_true", help="skip the list_tools() dump")
    args = parser.parse_args()

    prepare_sandbox(remove=("hello.txt",))
    runtime, droid = make_runtime()
    recorder = Recorder(FIXTURES / f"{args.name}.jsonl", raw_path=FIXTURES / f"{args.name}.raw.jsonl")
    recorder.meta(
        "Environment",
        **environment_info(droid, model=args.model, reasoning=args.reasoning, autonomy="off", cwd=str(SANDBOX)),
    )
    session = Session(
        cwd=SANDBOX,
        model=args.model,
        reasoning_effort=reasoning_effort(args.reasoning),
        config=SessionConfig(autonomy=Autonomy.OFF),
        interactions=InteractionHandlers(on_permission=make_permission_handler(recorder)),
        runtime=runtime,
    )
    result = None
    try:
        await open_recorded(session, recorder)
        if not args.skip_tools:
            await dump_tools(session)
        result = await run_turn(session, recorder, PROMPT, timeout=args.timeout)
        recorder.meta("ContextUsage", context=await session.context())
    finally:
        await close_recorded(session, recorder)
        recorder.close()
    recorder.print_summary()
    hello = SANDBOX / "hello.txt"
    content = hello.read_text(encoding="utf-8") if hello.exists() else None
    print(f"result: {result.subtype if result else None}; session_id={session.id}; hello.txt={content!r}")
    ok = result is not None and result.success and content is not None and content.strip() == EXPECTED_CONTENT
    return 0 if ok else 1


if __name__ == "__main__":
    setup_console()
    raise SystemExit(asyncio.run(main()))
