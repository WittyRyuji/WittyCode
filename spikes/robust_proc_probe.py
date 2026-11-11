"""Spike (item 4 follow-up, phase e): does a SILENT long-lived command survive
a hard kill of the Python host?

Same shape as proc_cleanup phase (c), but the Execute runs
``Start-Sleep -Seconds 600`` (no stdout writes: a broken-pipe exit cannot
explain the outcome), and the child additionally records IsProcessInJob() for
droid.exe and for each PowerShell in its tree.

Parent: waits for the sleep to start, hard-kills the child Python with
TerminateProcess, then checks whether the sleeping PowerShell survives.
"""

from __future__ import annotations

import argparse
import asyncio
import ctypes
import json
import os
import subprocess
import sys
import time
from ctypes import wintypes
from pathlib import Path

from droid_sdk import Autonomy, InteractionHandlers, Session, SessionConfig, ToolProgress

from _common import (
    DEFAULT_MODEL,
    ROOT,
    Recorder,
    environment_info,
    make_permission_handler,
    make_runtime,
    reasoning_effort,
    run_turn,
    setup_console,
)
from _robust_common import (
    ROBUST_FIXTURES,
    ROBUST_SANDBOX,
    alive,
    descendants_of,
    kernel32,
    session_droid_pid,
    terminate,
)

SLEEP_PROMPT = (
    "Use the Execute tool to run exactly this command and nothing else: "
    "Start-Sleep -Seconds 600 . The command runs for a while; that is expected, just start it."
)

BASE = ROBUST_SANDBOX / "cleanup"


def in_job(pid: int) -> bool | None:
    handle = kernel32.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
    if not handle:
        return None
    result = wintypes.BOOL(False)
    ok = kernel32.IsProcessInJob(handle, None, ctypes.byref(result))
    kernel32.CloseHandle(handle)
    return bool(result.value) if ok else None


async def child_main(state_path: Path) -> None:
    cwd = BASE / "child_e"
    cwd.mkdir(parents=True, exist_ok=True)

    def write_state(**extra) -> None:
        current = {}
        if state_path.exists():
            try:
                current = json.loads(state_path.read_text(encoding="utf-8"))
            except Exception:
                current = {}
        current.update(extra)
        state_path.write_text(json.dumps(current), encoding="utf-8")

    runtime, droid = make_runtime()
    recorder = Recorder(ROBUST_FIXTURES / "proc_cleanup_child_e.jsonl", echo=False)
    session = Session(
        cwd=cwd,
        model=DEFAULT_MODEL,
        reasoning_effort=reasoning_effort(),
        config=SessionConfig(autonomy=Autonomy.OFF),
        interactions=InteractionHandlers(on_permission=make_permission_handler(recorder)),
        runtime=runtime,
    )
    await session.open()
    pid = session_droid_pid(session)
    write_state(python_pid=os.getpid(), droid_pid=pid, opened=True, droid_in_job=in_job(pid))

    def on_event(event: object) -> None:
        if isinstance(event, ToolProgress):
            time.sleep(2.0)  # let the shell tree settle
            tree = descendants_of(pid)
            shells = {
                str(p): {"exe": i["exe"], "in_job": in_job(p)}
                for p, i in tree.items()
                if p != pid
            }
            write_state(command_started=True, tree=shells, droid_in_job=in_job(pid))

    try:
        await run_turn(session, recorder, SLEEP_PROMPT, timeout=900, on_event=on_event)
    except Exception as exc:
        write_state(turn_error=f"{type(exc).__name__}: {exc}")
    write_state(turn_ended=True)
    recorder.close()


def read_state(path: Path) -> dict | None:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None


def parent_main() -> dict:
    state_path = BASE / "state_e.json"
    state_path.unlink(missing_ok=True)
    BASE.mkdir(parents=True, exist_ok=True)
    log_path = BASE / "child_e.log"
    out: dict = {"phase": "e"}
    with log_path.open("w", encoding="utf-8") as log:
        proc = subprocess.Popen(
            [sys.executable, str(Path(__file__).resolve()), "--child", "--state", str(state_path)],
            cwd=str(ROOT),
            stdout=log,
            stderr=subprocess.STDOUT,
        )
    deadline = time.perf_counter() + 300
    state = None
    while time.perf_counter() < deadline:
        state = read_state(state_path)
        if state and state.get("command_started"):
            break
        if proc.poll() is not None and not state:
            break
        time.sleep(1.0)
    out["state"] = state
    out["child_launcher_pid"] = proc.pid
    if not state or not state.get("command_started"):
        out["error"] = "sleep never started"
        if proc.poll() is None:
            terminate(proc.pid)
        return out
    child_python = state.get("python_pid")
    shell_pids = [int(p) for p, i in (state.get("tree") or {}).items()]
    out["shells_before_kill"] = state.get("tree")
    out["droid_in_job"] = state.get("droid_in_job")
    # Hard-kill the real python (and the launcher for good measure).
    terminate(child_python)
    terminate(proc.pid)
    try:
        proc.wait(timeout=15)
    except subprocess.TimeoutExpired:
        pass
    out["child_exit"] = proc.returncode
    time.sleep(4.0)
    out["shells_alive_after_kill"] = {
        str(p): alive(p) for p in shell_pids
    }
    out["droid_alive_after_kill"] = alive(state["droid_pid"])
    out["python_alive_after_kill"] = alive(child_python)
    for p in shell_pids + [state["droid_pid"]]:
        if alive(p):
            terminate(p)
    time.sleep(1.0)
    out["alive_after_cleanup"] = [str(p) for p in shell_pids + [state["droid_pid"]] if alive(p)]
    print(json.dumps(out, ensure_ascii=False, indent=2, default=str), flush=True)
    return out


async def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--child", action="store_true")
    parser.add_argument("--state", default=None)
    args = parser.parse_args()
    if args.child:
        await child_main(Path(args.state))
        return 0
    result = parent_main()
    summary = ROBUST_FIXTURES / "proc_probe_summary.json"
    summary.write_text(json.dumps(result, ensure_ascii=False, indent=2, default=str) + "\n",
                       encoding="utf-8", newline="\n")
    leaked = any(result.get("shells_alive_after_kill", {}).values())
    print(f"silent shell leaked: {leaked}; wrote {summary}", flush=True)
    return 0 if not result.get("alive_after_cleanup") else 1


if __name__ == "__main__":
    setup_console()
    raise SystemExit(asyncio.run(main()))