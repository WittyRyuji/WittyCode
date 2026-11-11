"""Spike (items 1 + 5): concurrency, per-session memory, Execute output streaming.

Runs N = 1, 3, 5 sessions concurrently in this one Python process. Each session
gets its own cwd (sandbox/robust/conc/n<level>_s<i>) and runs one short turn: an
Execute command printing one line per second for --ticks seconds. That command
doubles as the item-5 ToolProgress streaming probe (raw wire notifications are
recorded for n*_s1).

Measured and written to fixtures/robust/concurrency.json:
  - process model: distinct droid.exe PID per Session, parented to this process
  - per-session droid process-tree memory before turn / peak during / idle after
    (working set + private bytes, ctypes, 250 ms sampling while turns run)
  - Python process memory over the whole run
  - open and turn latency per concurrency level
  - after the N=5 phase: all five sessions idle-open for --idle minutes with
    memory re-sampled, then closed with liveness polled
Per-session event recordings: fixtures/robust/concurrency_n<level>_s<i>.jsonl
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import time
from pathlib import Path

from droid_sdk import Autonomy, InteractionHandlers, Session, SessionConfig, ToolProgress

from _common import (
    DEFAULT_MODEL,
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
    TreeSampler,
    alive,
    make_tick_command,
    mb,
    process_table,
    session_droid_pid,
    tree_memory,
)


def tick_prompt(seconds: int) -> str:
    return (
        "Use the Execute tool to run exactly this command and nothing else: "
        + make_tick_command(seconds)
        + " . When it finishes, reply with the single word done."
    )


def droid_processes() -> dict[int, dict]:
    return {pid: info for pid, info in process_table().items() if info["exe"] == "droid.exe"}


def mem_brief(root_pid: int) -> dict:
    agg = tree_memory(root_pid)
    return {
        "alive": agg["alive"],
        "members": agg["member_count"],
        "rss_mb": mb(agg["rss"]),
        "private_mb": mb(agg["private"]),
        "member_list": [
            {"pid": m["pid"], "exe": m["exe"], "rss_mb": mb(m["memory"]["rss"]) if m["memory"] else None}
            for m in agg["members"]
        ],
    }


async def one_session(level: int, index: int, args, sampler: TreeSampler, keep_open: bool):
    label = f"n{level}_s{index}"
    cwd = ROBUST_SANDBOX / "conc" / label
    cwd.mkdir(parents=True, exist_ok=True)
    runtime, droid = make_runtime()
    record_raw = index == 1
    recorder = Recorder(
        ROBUST_FIXTURES / f"concurrency_{label}.jsonl",
        raw_path=ROBUST_FIXTURES / f"concurrency_{label}.raw.jsonl" if record_raw else None,
        echo=False,
    )
    recorder.meta(
        "Environment",
        **environment_info(droid, model=DEFAULT_MODEL, reasoning="low", autonomy="off", cwd=str(cwd),
                           level=level, index=index, ticks=args.ticks),
    )
    progress: list[dict] = []

    def on_event(event: object) -> None:
        if isinstance(event, ToolProgress):
            update = event.update
            progress.append(
                {
                    "turn_t": round(recorder.turn_now(), 2),
                    "type": update.type,
                    "text_len": len(update.text or ""),
                    "text": update.text,
                    "full_output_len": len(update.full_output or ""),
                    "value_snippet": update.value_snippet,
                    "terminal_id": update.terminal_id,
                }
            )

    session = Session(
        cwd=cwd,
        model=DEFAULT_MODEL,
        reasoning_effort=reasoning_effort(),
        config=SessionConfig(autonomy=Autonomy.OFF),
        interactions=InteractionHandlers(on_permission=make_permission_handler(recorder)),
        runtime=runtime,
    )
    result = None
    error = None
    out: dict = {"label": label, "cwd": str(cwd)}
    try:
        t0 = time.perf_counter()
        await open_recorded(session, recorder)
        out["open_s"] = round(time.perf_counter() - t0, 3)
        out["session_id"] = session.id
        pid = session_droid_pid(session)
        out["droid_pid"] = pid
        out["droid_ppid"] = process_table().get(pid, {}).get("ppid")
        out["python_pid"] = os.getpid()
        if pid:
            sampler.set_root(label, pid)
        await asyncio.sleep(2.0)
        out["mem_before_turn"] = mem_brief(pid)
        t0 = time.perf_counter()
        result = await run_turn(session, recorder, tick_prompt(args.ticks), timeout=args.ticks + 300, on_event=on_event)
        out["turn_s"] = round(time.perf_counter() - t0, 3)
        out["first_event_s"] = recorder.latency.get(1, {}).get("first_event")
        out["first_tool_progress_s"] = recorder.latency.get(1, {}).get("first_ToolProgress")
        await asyncio.sleep(3.0)
        out["mem_after_turn"] = mem_brief(pid)
        out["peak_during_turn"] = {k: mb(v) if k != "members" else v for k, v in sampler.peaks.get(label, {}).items()}
    except Exception as exc:
        error = f"{type(exc).__name__}: {exc}"
        recorder.meta("Error", error=type(exc).__name__, message=str(exc))
    out["error"] = error
    out["result"] = (
        None
        if result is None
        else {
            "type": type(result).__name__,
            "subtype": result.subtype,
            "success": result.success,
            "credits": result.usage.factory_credits if result.usage else None,
            "text": result.text[:60],
        }
    )
    out["tool_progress_events"] = progress
    out["event_counts"] = {str(k): v for k, v in recorder.counts.items()}
    if not keep_open:
        pid = out.get("droid_pid")
        try:
            await close_recorded(session, recorder)
        except Exception as exc:
            recorder.meta("CloseError", error=type(exc).__name__, message=str(exc))
        if pid:
            gone_after = None
            for waited in (0.5, 1.0, 2.0, 4.0):
                if not alive(pid):
                    gone_after = waited
                    break
                await asyncio.sleep(waited)
            out["droid_gone_within_s"] = gone_after
            out["droid_alive_final"] = alive(pid)
        sampler.set_root(label, None)
    recorder.close()
    print(f"[{label}] done: open={out.get('open_s')}s turn={out.get('turn_s')}s "
          f"result={out['result']['subtype'] if out['result'] else None} err={error}", flush=True)
    return out, session if keep_open else None


async def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--ticks", type=int, default=8)
    parser.add_argument("--idle", type=float, default=300.0, help="seconds the N=5 sessions stay idle-open")
    args = parser.parse_args()
    prepare_sandbox()
    ROBUST_FIXTURES.mkdir(parents=True, exist_ok=True)
    sampler = TreeSampler(interval=0.25)
    sampler.start()
    report: dict = {"environment": None, "process_model": {}, "levels": {}, "idle": None, "peaks": None}
    runtime, droid = make_runtime()
    report["environment"] = environment_info(droid, model=DEFAULT_MODEL, reasoning="low", ticks=args.ticks,
                                             idle_s=args.idle, python_pid=os.getpid())

    for level, keep in ((1, False), (3, False), (5, True)):
        before = droid_processes()
        t0 = time.perf_counter()
        pairs = await asyncio.gather(*(one_session(level, i, args, sampler, keep) for i in range(1, level + 1)))
        wall = time.perf_counter() - t0
        after = droid_processes()
        results = [p[0] for p in pairs]
        sessions = [p[1] for p in pairs]
        spawned = sorted(set(after) - set(before))
        report["levels"][str(level)] = {
            "wall_s": round(wall, 2),
            "sessions": results,
            "new_droid_pids": spawned,
            "new_droid_ppids": sorted({after[p]["ppid"] for p in spawned}),
            "turn_times_s": [r.get("turn_s") for r in results],
        }
        print(f"=== level {level}: wall {wall:.1f}s, new droid pids {spawned} ppids "
              f"{sorted({after[p]['ppid'] for p in spawned})} (this python={os.getpid()})", flush=True)
        if keep:
            report["_sessions_n5"] = sessions  # internal, popped before write

    sessions5 = report.pop("_sessions_n5", [])
    if sessions5:
        sampler.interval = 5.0
        idle_start = {s and r.get("label"): None for r, s in []}  # placeholder no-op
        mem_start = {}
        for res, session in zip(report["levels"]["5"]["sessions"], sessions5):
            pid = res.get("droid_pid")
            if pid:
                mem_start[res["label"]] = mem_brief(pid)
        print(f"idle phase: {args.idle}s with {len(sessions5)} open sessions", flush=True)
        await asyncio.sleep(args.idle)
        mem_end = {}
        for res, session in zip(report["levels"]["5"]["sessions"], sessions5):
            pid = res.get("droid_pid")
            if pid:
                mem_end[res["label"]] = mem_brief(pid)
        report["idle"] = {"seconds": args.idle, "mem_at_start": mem_start, "mem_at_end": mem_end,
                          "python_mb_at_end": mb(__import__("_robust_common").process_memory(os.getpid())["rss"])}
        print("idle done; closing 5 sessions", flush=True)
        closes = await asyncio.gather(
            *(close_recorded(s, Recorder(ROBUST_FIXTURES / f"close_{i}.jsonl", echo=False)) if False else session.close()
              for i, session in enumerate(sessions5)),
            return_exceptions=True,
        )
        report["close_errors"] = [f"{type(c).__name__}: {c}" for c in closes if isinstance(c, BaseException)]
        gone = {}
        for res, session in zip(report["levels"]["5"]["sessions"], sessions5):
            pid = res.get("droid_pid")
            if not pid:
                continue
            waited_total = 0.0
            while alive(pid) and waited_total < 6.0:
                await asyncio.sleep(0.5)
                waited_total += 0.5
            gone[res["label"]] = {"gone_after_s": waited_total, "alive_final": alive(pid)}
            sampler.set_root(res["label"], None)
        report["closed"] = gone

    sampler.stop()
    report["peaks"] = {label: {k: mb(v) if k != "members" else v for k, v in peak.items()}
                       for label, peak in sampler.peaks.items()}
    report["sample_count"] = len(sampler.samples)
    slim_samples = [
        {
            "t": p["t"],
            **{label: mb(t["rss"]) for label, t in p["trees"].items()},
            "python": mb(p["python"]["rss"]) if "python" in p else None,
        }
        for p in sampler.samples
    ]
    report["samples_rss_mb"] = slim_samples
    out_path = ROBUST_FIXTURES / "concurrency.json"
    report.pop("_sessions_n5", None)
    out_path.write_text(json.dumps(report, ensure_ascii=False, indent=2, default=str) + "\n",
                        encoding="utf-8", newline="\n")
    print(f"wrote {out_path}", flush=True)
    ok = all(
        all(r.get("result") and r["result"]["success"] for r in level["sessions"])
        for level in report["levels"].values()
    )
    print("all turns succeeded:", ok, flush=True)
    return 0 if ok else 1


if __name__ == "__main__":
    setup_console()
    raise SystemExit(asyncio.run(main()))