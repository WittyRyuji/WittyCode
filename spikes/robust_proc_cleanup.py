"""Spike (item 4): child-process cleanup when a session ends on Windows.

Each phase starts one Execute running ``ping -n 600 127.0.0.1`` (a ~10 minute
foreground child of the session's PowerShell), records the droid.exe process
tree, then:
  a) closes the session normally (close() while the command is still running)
  b) interrupts the turn, then closes
  c) runs the session in a child Python process that is hard-killed with
     TerminateProcess (no cleanup at all)          -> leak expected
  d) same as (c), but the child assigns droid.exe to a Win32 Job Object with
     JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE            -> OS should reap the tree

Only processes this spike started are inspected or terminated (all PIDs are
recorded from the droid tree of each phase).
Writes fixtures/robust/proc_cleanup_{a,b}.jsonl (event recordings),
fixtures/robust/proc_cleanup_child_{c,d}.log, and proc_cleanup_summary.json.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import subprocess
import sys
import time
from pathlib import Path

from droid_sdk import Autonomy, InteractionHandlers, Session, SessionConfig, ToolProgress

from _common import (
    DEFAULT_MODEL,
    ROOT,
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
from _robust_common import (
    ROBUST_FIXTURES,
    ROBUST_SANDBOX,
    KillOnCloseJob,
    alive,
    descendants_of,
    process_memory,
    session_droid_pid,
    terminate,
    tree_memory,
    mb,
)

PING_PROMPT = (
    "Use the Execute tool to run exactly this command and nothing else: "
    "ping -n 600 127.0.0.1 . The command runs for a while; that is expected, just start it."
)

BASE = ROBUST_SANDBOX / "cleanup"


def tree_brief(root_pid: int) -> dict:
    agg = tree_memory(root_pid)
    return {
        "alive": agg["alive"],
        "members": [
            {"pid": m["pid"], "ppid": m["ppid"], "exe": m["exe"],
             "rss_mb": mb(m["memory"]["rss"]) if m["memory"] else None}
            for m in agg["members"]
        ],
    }


def kill_recorded(tree: dict, also_root: bool = True) -> dict:
    """Terminate every pid recorded in a tree_brief (children first)."""
    killed = {}
    members = list(tree["members"])
    members.sort(key=lambda m: (m["pid"] == tree_root(tree), m["pid"]))  # root last if flagged
    for member in members:
        pid = member["pid"]
        if not also_root and pid == tree_root(tree):
            continue
        if alive(pid):
            killed[pid] = {"exe": member["exe"], "terminated": terminate(pid)}
    return killed


def tree_root(tree: dict) -> int | None:
    return tree["members"][0]["pid"] if tree["members"] else None


async def phase_in_process(tag: str, interrupt_first: bool, timeout: float) -> dict:
    cwd = BASE / tag
    cwd.mkdir(parents=True, exist_ok=True)
    runtime, droid = make_runtime()
    recorder = Recorder(ROBUST_FIXTURES / f"proc_cleanup_{tag}.jsonl",
                        raw_path=ROBUST_FIXTURES / f"proc_cleanup_{tag}.raw.jsonl", echo=False)
    recorder.meta("Environment", **environment_info(droid, model=DEFAULT_MODEL, cwd=str(cwd), phase=tag))
    session = Session(
        cwd=cwd,
        model=DEFAULT_MODEL,
        reasoning_effort=reasoning_effort(),
        config=SessionConfig(autonomy=Autonomy.OFF),
        interactions=InteractionHandlers(on_permission=make_permission_handler(recorder)),
        runtime=runtime,
    )
    out: dict = {"phase": tag}
    started = asyncio.Event()

    def on_event(event: object) -> None:
        if isinstance(event, ToolProgress):
            started.set()

    turn_result = None
    try:
        await open_recorded(session, recorder)
        pid = session_droid_pid(session)
        out["droid_pid"] = pid
        turn_task = asyncio.create_task(
            run_turn(session, recorder, PING_PROMPT, timeout=timeout, on_event=on_event)
        )
        await asyncio.wait_for(started.wait(), timeout=240)
        await asyncio.sleep(2.0)  # let the ping process appear and settle
        out["tree_while_running"] = tree_brief(pid)
        print(f"[{tag}] tree while running: "
              + json.dumps([m["exe"] for m in out["tree_while_running"]["members"]]), flush=True)
        if interrupt_first:
            t0 = time.perf_counter()
            await session.interrupt()
            turn_result = await turn_task
            out["interrupt_to_result_s"] = round(time.perf_counter() - t0, 3)
            out["turn_result"] = type(turn_result).__name__ + "/" + turn_result.subtype
            await asyncio.sleep(1.5)
            out["tree_after_interrupt"] = tree_brief(pid)
        t0 = time.perf_counter()
        try:
            await session.close()
            out["close_s"] = round(time.perf_counter() - t0, 3)
        except Exception as exc:
            out["close_error"] = f"{type(exc).__name__}: {exc}"
        if not interrupt_first:
            try:
                turn_result = await asyncio.wait_for(turn_task, timeout=30)
                out["turn_result"] = type(turn_result).__name__ + "/" + turn_result.subtype
            except Exception as exc:
                out["turn_result"] = f"{type(exc).__name__}: {exc}"
        await asyncio.sleep(2.0)
        out["tree_after_close"] = tree_brief(pid)
        survivors = [m for m in out["tree_after_close"]["members"] if alive(m["pid"])]
        out["survivors_after_close"] = survivors
        out["cleanup_killed"] = kill_recorded(out["tree_after_close"])
        await asyncio.sleep(1.0)
        out["alive_after_cleanup"] = [m["pid"] for m in out["tree_after_close"]["members"] if alive(m["pid"])]
    except Exception as exc:
        out["error"] = f"{type(exc).__name__}: {exc}"
        try:
            await session.close()
        except Exception:
            pass
    recorder.meta("PhaseResult", **{k: v for k, v in out.items()})
    recorder.close()
    print(f"[{tag}] done: {json.dumps({k: v for k, v in out.items() if k not in ('tree_while_running',)}, default=str)[:600]}", flush=True)
    return out


async def child_main(tag: str, state_path: Path) -> None:
    cwd = BASE / f"child_{tag}"
    cwd.mkdir(parents=True, exist_ok=True)
    state_file = state_path

    def write_state(**extra) -> None:
        current = {}
        if state_file.exists():
            try:
                current = json.loads(state_file.read_text(encoding="utf-8"))
            except Exception:
                current = {}
        current.update(extra)
        state_file.write_text(json.dumps(current), encoding="utf-8")

    runtime, droid = make_runtime()
    recorder = Recorder(ROBUST_FIXTURES / f"proc_cleanup_child_{tag}.jsonl", echo=False)
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
    write_state(python_pid=os.getpid(), droid_pid=pid, job=(tag == "d"), opened=True)
    if tag == "d":
        try:
            job = KillOnCloseJob()
            job.assign(pid)
            write_state(job_assigned=True)
            # Intentionally never closed: the OS closes it when this process dies.
            globals()["_job"] = job
        except Exception as exc:
            write_state(job_assigned=False, job_error=f"{type(exc).__name__}: {exc}")

    def on_event(event: object) -> None:
        if isinstance(event, ToolProgress):
            write_state(command_started=True)

    try:
        await run_turn(session, recorder, PING_PROMPT, timeout=900, on_event=on_event)
    except Exception as exc:
        write_state(turn_error=f"{type(exc).__name__}: {exc}")
    write_state(turn_ended=True)
    recorder.close()


def read_state(path: Path) -> dict | None:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None


def phase_via_child(tag: str) -> dict:
    state_path = BASE / f"state_{tag}.json"
    state_path.unlink(missing_ok=True)
    BASE.mkdir(parents=True, exist_ok=True)
    log_path = BASE / f"child_{tag}.log"
    out: dict = {"phase": tag}
    with log_path.open("w", encoding="utf-8") as log:
        proc = subprocess.Popen(
            [sys.executable, str(Path(__file__).resolve()), "--child", tag, "--state", str(state_path)],
            cwd=str(ROOT),
            stdout=log,
            stderr=subprocess.STDOUT,
        )
    # The log handle is closed so the child does not inherit a locked file; the
    # child keeps writing through its own inherited handles.
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
    out["child_python_pid"] = proc.pid
    if not state or not state.get("droid_pid"):
        out["error"] = "child never reported a droid pid"
        if proc.poll() is None:
            terminate(proc.pid)
        return out
    droid_pid = state["droid_pid"]
    time.sleep(2.0)
    out["tree_before_kill"] = tree_brief(droid_pid)
    print(f"[{tag}] tree before kill: "
          + json.dumps([m["exe"] for m in out["tree_before_kill"]["members"]]), flush=True)
    terminate(proc.pid)
    try:
        proc.wait(timeout=15)
    except subprocess.TimeoutExpired:
        out["child_wait"] = "timeout"
    out["child_exit"] = proc.returncode
    time.sleep(3.0)
    out["tree_after_kill"] = tree_brief(droid_pid)
    survivors = [m for m in out["tree_after_kill"]["members"] if alive(m["pid"])]
    out["survivors_after_kill"] = survivors
    out["leaked"] = [m for m in survivors if m["exe"] != "droid.exe" or True]
    out["cleanup_killed"] = kill_recorded(out["tree_after_kill"])
    time.sleep(1.0)
    out["alive_after_cleanup"] = [m["pid"] for m in out["tree_after_kill"]["members"] if alive(m["pid"])]
    print(f"[{tag}] survivors after kill: {json.dumps(survivors)}", flush=True)
    return out


async def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--child", choices=["c", "d"], default=None)
    parser.add_argument("--state", default=None)
    parser.add_argument("--timeout", type=float, default=700.0)
    args = parser.parse_args()
    if args.child:
        await child_main(args.child, Path(args.state))
        return 0
    prepare_sandbox()
    ROBUST_FIXTURES.mkdir(parents=True, exist_ok=True)
    BASE.mkdir(parents=True, exist_ok=True)
    results = []
    results.append(await phase_in_process("a", interrupt_first=False, timeout=args.timeout))
    results.append(await phase_in_process("b", interrupt_first=True, timeout=args.timeout))
    results.append(await asyncio.to_thread(phase_via_child, "c"))
    results.append(await asyncio.to_thread(phase_via_child, "d"))
    summary = ROBUST_FIXTURES / "proc_cleanup_summary.json"
    summary.write_text(json.dumps(results, ensure_ascii=False, indent=2, default=str) + "\n",
                       encoding="utf-8", newline="\n")
    print(f"wrote {summary}", flush=True)
    for result in results:
        print(json.dumps({k: v for k, v in result.items() if k != "state"}, default=str)[:900], flush=True)
    leaked_any = any(r.get("alive_after_cleanup") for r in results)
    return 1 if leaked_any else 0


if __name__ == "__main__":
    setup_console()
    raise SystemExit(asyncio.run(main()))