"""Spike (d): list saved sessions for the sandbox folder, resume the stream_turn
session and send a short follow-up.

The session id defaults to the SessionOpened record in fixtures/stream_turn.jsonl.
Writes fixtures/resume.jsonl (+ .raw.jsonl).
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import time
from pathlib import Path

from droid_sdk import InteractionHandlers, Session, list_sessions

from _common import (
    FIXTURES,
    ROOT,
    SANDBOX,
    Recorder,
    close_recorded,
    environment_info,
    make_permission_handler,
    make_runtime,
    open_recorded,
    prepare_sandbox,
    run_turn,
    setup_console,
)

PROMPT = "What file did you create?"


def session_id_from_fixture(path: Path) -> str | None:
    if not path.exists():
        return None
    with path.open(encoding="utf-8") as handle:
        for line in handle:
            record = json.loads(line)
            if record.get("source") == "meta" and record.get("type") == "SessionOpened":
                return record["data"]["session_id"]
    return None


async def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--session-id", default=None)
    parser.add_argument("--timeout", type=float, default=180.0)
    args = parser.parse_args()
    target = args.session_id or session_id_from_fixture(FIXTURES / "stream_turn.jsonl")
    if not target:
        print("no session id: run spikes/stream_turn.py first or pass --session-id")
        return 2

    prepare_sandbox()
    # Session.resume() takes no cwd; the droid subprocess inherits this process's cwd.
    os.chdir(SANDBOX)
    runtime, droid = make_runtime()
    recorder = Recorder(FIXTURES / "resume.jsonl", raw_path=FIXTURES / "resume.raw.jsonl")
    recorder.meta("Environment", **environment_info(droid, cwd=str(SANDBOX), resume_session_id=target))

    started = time.perf_counter()
    saved = await list_sessions(cwd=SANDBOX)
    elapsed = time.perf_counter() - started
    parent = await list_sessions(cwd=ROOT)
    recorder.meta(
        "ListSessions",
        cwd=SANDBOX,
        elapsed_s=round(elapsed, 4),
        sessions=saved,
        parent_folder_count=len(parent),
    )
    print(f"list_sessions(cwd=sandbox): {len(saved)} sessions in {elapsed * 1000:.1f} ms "
          f"(list_sessions(cwd=WittyCode) -> {len(parent)})")
    for item in saved:
        print(
            f"   {item.id} title={item.title!r} messages={item.message_count} "
            f"modified={item.modified_at.isoformat(timespec='seconds')} cwd={item.cwd} favorite={item.is_favorite}"
        )
    listed = any(item.id == target for item in saved)
    print(f"target {target} listed: {listed}")

    session = Session.resume(
        target,
        interactions=InteractionHandlers(on_permission=make_permission_handler(recorder)),
        runtime=runtime,
    )
    result = None
    try:
        await open_recorded(session, recorder)
        result = await run_turn(session, recorder, PROMPT, timeout=args.timeout)
    finally:
        await close_recorded(session, recorder)
        recorder.close()
    recorder.print_summary()
    remembered = result is not None and "hello.txt" in result.text.lower()
    print(f"result: {result.subtype if result else None}; same id: {result.session_id == target if result else None}; "
          f"mentions hello.txt: {remembered}; reply={result.text if result else None!r}")
    return 0 if result is not None and result.success and listed and remembered else 1


if __name__ == "__main__":
    setup_console()
    raise SystemExit(asyncio.run(main()))
