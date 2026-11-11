"""Phase 0b C: long-lived session script.

Plants a canary, then at every checkpoint (minutes after session open):
  * verifies the session's own droid.exe process is alive (PID match)
  * records its working-set memory
  * runs a canary-recall turn (live model call) and checks the canary
  * records turn latency, usage and credits
Every checkpoint line is appended to fixtures/long_session/progress.jsonl and
flushed immediately, so a crash still leaves a trail.

After the last checkpoint the script EXTERNALLY KILLS its own droid.exe PID
(taskkill /PID <own pid> /F -- never any other process) and then resumes the
same session id from the same Python process, runs one more recall turn, and
verifies the canary survived the kill.

Exit code 0 iff every checkpoint passed and the kill/resume recovery passed.

Validate quickly with:   spikes\\ctx_long_session.py --checkpoints 1,2
Full acceptance run:     spikes\\ctx_long_session.py --checkpoints 5,15,30,60,90
"""

from __future__ import annotations

import _pyguard  # noqa: F401  -- require Python 3.11.x

import argparse
import asyncio
import json
import os
import subprocess
import time
from typing import Any

from droid_sdk import Autonomy, InteractionHandlers, Session, SessionConfig

from _common import (
    DEFAULT_MODEL,
    DEFAULT_REASONING,
    Recorder,
    environment_info,
    make_permission_handler,
    make_runtime,
    reasoning_effort,
    run_turn,
    setup_console,
    to_jsonable,
)
from _ctx_common import (
    LONG_FIXTURES,
    LONG_SANDBOX,
    PLANT_PROMPT,
    RECALL_PROMPT,
    UsageTracker,
    droid_pids,
    find_canaries,
    new_canary,
    process_memory_bytes,
    save_json,
    to_jsonable as _tj,  # alias guard (unused if absent)
    transport_pid,
)


def append_progress(line: dict[str, Any]) -> None:
    path = LONG_FIXTURES / "progress.jsonl"
    with path.open("a", encoding="utf-8", newline="\n") as handle:
        handle.write(json.dumps(to_jsonable(line), ensure_ascii=False) + "\n")
        handle.flush()
        os.fsync(handle.fileno())


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


async def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--checkpoints", default="5,15,30,60,90", help="comma-separated minutes")
    parser.add_argument("--model", default=DEFAULT_MODEL)
    parser.add_argument("--reasoning", default=DEFAULT_REASONING)
    parser.add_argument("--timeout", type=float, default=180.0)
    args = parser.parse_args()
    checkpoints = sorted({float(x) for x in args.checkpoints.split(",") if x.strip()})
    if not checkpoints:
        print("FATAL: no checkpoints", flush=True)
        return 1

    runtime, droid = make_runtime()
    recorder = Recorder(LONG_FIXTURES / "long_session.jsonl", raw_path=LONG_FIXTURES / "long_session.raw.jsonl")
    recorder.meta(
        "Environment",
        **environment_info(droid, model=args.model, reasoning=args.reasoning, autonomy="off", scope="C", checkpoints=checkpoints),
    )
    tracker = UsageTracker()
    canary = new_canary(set())

    results: dict[str, Any] = {"canary": canary, "checkpoints": [], "kill": None, "fatal": None}
    t0 = time.perf_counter()
    session = None
    resumed = None
    try:
        session = Session(
            cwd=LONG_SANDBOX,
            model=args.model,
            reasoning_effort=reasoning_effort(args.reasoning),
            config=SessionConfig(autonomy=Autonomy.OFF),
            interactions=InteractionHandlers(on_permission=make_permission_handler(recorder)),
            runtime=runtime,
        )
        await open_meta(session, recorder)
        session.on_notification(tracker.callback())
        pid = transport_pid(session)
        results["session_id"] = session.id
        results["pid"] = pid
        results["memory_at_open"] = process_memory_bytes(pid) if pid else None
        plant = await run_turn(session, recorder, PLANT_PROMPT.format(canary=canary), timeout=args.timeout)
        results["plant"] = {"success": plant.success, "usage": to_jsonable(plant.usage)}
        append_progress({"kind": "plant", "t_min": 0.0, "success": plant.success, "pid": pid})

        for minute in checkpoints:
            wait = minute * 60.0 - (time.perf_counter() - t0)
            while wait > 0:
                await asyncio.sleep(min(wait, 30.0))
                wait = minute * 60.0 - (time.perf_counter() - t0)
            alive = pid in droid_pids() if pid else False
            memory = process_memory_bytes(pid) if alive else None
            before = len(tracker.turn_results)
            started = time.perf_counter()
            recall = await run_turn(session, recorder, RECALL_PROMPT, timeout=args.timeout)
            latency = round(time.perf_counter() - started, 3)
            found = find_canaries(recall.text or "", {"canary": canary})["canary"]
            entry = {
                "kind": "checkpoint",
                "minute": minute,
                "t_min": round((time.perf_counter() - t0) / 60.0, 3),
                "pid": pid,
                "alive": alive,
                "memory_bytes": memory,
                "recall_success": recall.success,
                "canary_recalled": found,
                "latency_s": latency,
                "usage": to_jsonable(recall.usage),
                "tracker_turn": tracker.turn_results[before:],
            }
            entry["pass"] = bool(alive and recall.success and found)
            results["checkpoints"].append(entry)
            append_progress(entry)
            print(
                f"checkpoint {minute}m: alive={alive} mem={memory} recalled={found} "
                f"latency={latency}s -> {'PASS' if entry['pass'] else 'FAIL'}",
                flush=True,
            )

        # ---- external kill of OUR OWN droid pid, then resume recovery ----
        kill: dict[str, Any] = {"pid": pid}
        if pid and pid != os.getpid() and pid in droid_pids():
            proc = subprocess.run(
                ["taskkill", "/PID", str(pid), "/F"],
                capture_output=True,
                text=True,
                timeout=30,
            )
            kill["taskkill_exit"] = proc.returncode
            await asyncio.sleep(2.0)
            kill["alive_after_kill"] = pid in droid_pids()
        else:
            kill["skipped"] = f"pid={pid} not ours or already gone"
        results["kill"] = kill
        append_progress({"kind": "kill", **kill})
        print(f"kill: {kill}", flush=True)

        os.chdir(LONG_SANDBOX)
        resumed = Session.resume(
            session.id,
            interactions=InteractionHandlers(on_permission=make_permission_handler(recorder)),
            runtime=runtime,
        )
        await open_meta(resumed, recorder, phase="post-kill-resume")
        resumed.on_notification(tracker.callback())
        new_pid = transport_pid(resumed)
        started = time.perf_counter()
        recall2 = await run_turn(resumed, recorder, RECALL_PROMPT, timeout=args.timeout)
        latency2 = round(time.perf_counter() - started, 3)
        found2 = find_canaries(recall2.text or "", {"canary": canary})["canary"]
        recovery = {
            "kind": "resume_after_kill",
            "new_pid": new_pid,
            "recall_success": recall2.success,
            "canary_recalled": found2,
            "latency_s": latency2,
            "usage": to_jsonable(recall2.usage),
        }
        recovery["pass"] = bool(recall2.success and found2 and kill.get("alive_after_kill") is False)
        results["recovery"] = recovery
        append_progress(recovery)
        print(f"resume after kill: recalled={found2} new_pid={new_pid} -> {'PASS' if recovery['pass'] else 'FAIL'}", flush=True)
        session = None  # old session object is dead with its process
    except Exception as exc:
        results["fatal"] = f"{type(exc).__name__}: {str(exc)[:300]}"
        append_progress({"kind": "fatal", "error": results["fatal"]})
        print(f"FATAL: {results['fatal']}", flush=True)
    finally:
        for target in (resumed, session):
            if target is not None:
                try:
                    await target.close()
                except Exception:
                    pass
        recorder.meta("UsageTracker", **tracker.summary())
        recorder.close()

    results["wall_minutes"] = round((time.perf_counter() - t0) / 60.0, 3)
    ok = (
        results["fatal"] is None
        and results.get("plant", {}).get("success")
        and all(c["pass"] for c in results["checkpoints"])
        and results.get("recovery", {}).get("pass")
    )
    results["pass"] = bool(ok)
    save_json(LONG_FIXTURES / "summary.json", results)
    print(f"long session: {'PASS' if ok else 'FAIL'} wall={results['wall_minutes']}min", flush=True)
    return 0 if ok else 1


if __name__ == "__main__":
    setup_console()
    raise SystemExit(asyncio.run(main()))