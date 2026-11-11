"""Phase 0b A1-A5: process model and cross-session context isolation.

A1  Process model. (0) Probe the SDK's only shared-process hook -- two
    Sessions on one already-connected ProcessTransport -- and record how the
    second open fails; (1) compare the transport PIDs of two live sessions.
A2  Concurrent sessions A (canary X) and B (canary Y): recall turns must not
    leak the other session's codeword.
A3  After closing A and B, a fresh session C in the same Python process and
    the same cwd as A must not know any canary.
A4  Resume A by id while B is alive: A still knows X only. (B's own integrity
    is checked model-free by spikes/ctx_history_check.py.)
A5  Fork A -> A'; plant canary Z in A': A' knows X and Z; the resumed original
    A must not know Z.

Live turns: 8. Writes fixtures/context/isolation_{a,b}.jsonl (+ .raw.jsonl)
and fixtures/context/state.json (canaries, session ids, verdicts).
"""

from __future__ import annotations

import _pyguard  # noqa: F401  -- require Python 3.11.x

import argparse
import asyncio
import os
import time
from typing import Any

from droid_sdk import (
    Autonomy,
    InteractionHandlers,
    Runtime,
    Session,
    SessionConfig,
)
from droid_sdk.low_level import ProcessTransport

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
)
from _ctx_common import (
    CTX_FIXTURES,
    CTX_SANDBOX,
    PLANT_PROMPT,
    RECALL_PROMPT,
    UsageTracker,
    droid_pids,
    find_canaries,
    new_canary,
    save_json,
    transport_pid,
)
from find_droid import find_droid


def make_session(cwd, recorder, model, reasoning, runtime):
    return Session(
        cwd=cwd,
        model=model,
        reasoning_effort=reasoning_effort(reasoning),
        config=SessionConfig(autonomy=Autonomy.OFF),
        interactions=InteractionHandlers(on_permission=make_permission_handler(recorder)),
        runtime=runtime,
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
    recorder.meta(
        "SessionClosed",
        session_id=session.id,
        close_seconds=round(time.perf_counter() - started, 3),
    )


def recall_check(reply, canaries, must_have, must_not):
    found = find_canaries(reply or "", canaries)
    ok = all(found[n] for n in must_have) and not any(found[n] for n in must_not)
    return ok, found


async def shared_transport_probe(recorder, model, reasoning):
    """A1 probe: can two Sessions share one connected ProcessTransport (one
    droid.exe)? No model turn is started; initialize_session is free."""
    probe_dir = CTX_SANDBOX / "probe"
    probe_dir.mkdir(parents=True, exist_ok=True)
    transport = ProcessTransport(exec_path=str(find_droid()), cwd=str(probe_dir))
    await transport.connect()
    probe: dict[str, Any] = {"transport_pid": transport.pid}
    runtime = Runtime(transport=transport)
    s1 = Session(cwd=probe_dir, model=model, reasoning_effort=reasoning_effort(reasoning), runtime=runtime)
    s2 = Session(cwd=probe_dir, model=model, reasoning_effort=reasoning_effort(reasoning), runtime=runtime)
    try:
        await asyncio.wait_for(s1.open(), 60)
        probe["first_open"] = "ok"
        probe["first_session_id"] = s1.id
        try:
            await asyncio.wait_for(s2.open(), 20)
            probe["second_open"] = "ok"
            probe["second_session_id"] = s2.id
        except Exception as exc:
            probe["second_open"] = f"{type(exc).__name__}: {str(exc)[:240]}"
    except Exception as exc:
        probe["first_open"] = f"{type(exc).__name__}: {str(exc)[:240]}"
    for s in (s2, s1):
        try:
            await asyncio.wait_for(s.close(), 30)
        except Exception as exc:
            probe.setdefault("close_errors", []).append(f"{type(exc).__name__}: {str(exc)[:160]}")
    try:
        await asyncio.wait_for(transport.close(), 30)
    except Exception:
        pass
    probe["sharing_supported"] = probe.get("second_open") == "ok"
    recorder.meta("SharedTransportProbe", **probe)
    print(f"A1 shared-transport probe: first={probe.get('first_open')} second={probe.get('second_open')}", flush=True)
    return probe


async def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--model", default=DEFAULT_MODEL)
    parser.add_argument("--reasoning", default=DEFAULT_REASONING)
    parser.add_argument("--timeout", type=float, default=120.0)
    args = parser.parse_args()

    runtime, droid = make_runtime()
    canaries: dict[str, str] = {}
    used: set[str] = set()
    for name in ("X", "Y", "Z"):
        canaries[name] = new_canary(used)
        used.add(canaries[name])

    rec_a = Recorder(CTX_FIXTURES / "isolation_a.jsonl", raw_path=CTX_FIXTURES / "isolation_a.raw.jsonl")
    rec_b = Recorder(CTX_FIXTURES / "isolation_b.jsonl", raw_path=CTX_FIXTURES / "isolation_b.raw.jsonl")
    env = environment_info(droid, model=args.model, reasoning=args.reasoning, autonomy="off")
    rec_a.meta("Environment", **env, scope="A1 probe + sessions A/A-fork/A-resumed/C")
    rec_b.meta("Environment", **env, scope="A2 session B")

    cases: dict[str, Any] = {}
    sessions: dict[str, str] = {}
    replies: dict[str, str] = {}
    pids: dict[str, Any] = {}
    fatal = None
    A = B = C = forked = resumed = None
    tracker_a = UsageTracker()
    tracker_b = UsageTracker()
    try:
        probe = await shared_transport_probe(rec_a, args.model, args.reasoning)

        a_dir = CTX_SANDBOX / "a"
        b_dir = CTX_SANDBOX / "b"
        a_dir.mkdir(parents=True, exist_ok=True)
        b_dir.mkdir(parents=True, exist_ok=True)

        A = make_session(a_dir, rec_a, args.model, args.reasoning, runtime)
        B = make_session(b_dir, rec_b, args.model, args.reasoning, runtime)
        await open_recorded(A, rec_a)
        A.on_notification(tracker_a.callback())
        await open_recorded(B, rec_b)
        B.on_notification(tracker_b.callback())
        sessions["A"], sessions["B"] = A.id, B.id
        pids.update({"A": transport_pid(A), "B": transport_pid(B), "all_droid_exe": droid_pids()})
        rec_a.meta("ProcessModel", **pids)
        cases["A1"] = {
            "shared_transport_probe": probe,
            "session_pids": pids,
            "pass": (not probe["sharing_supported"])
            and pids["A"] is not None
            and pids["B"] is not None
            and pids["A"] != pids["B"],
        }
        print(f"A1 pids: A={pids['A']} B={pids['B']} (machine-wide droid.exe: {pids['all_droid_exe']})", flush=True)

        # A2: plant + recall concurrently
        ra, rb = await asyncio.gather(
            run_turn(A, rec_a, PLANT_PROMPT.format(canary=canaries["X"]), timeout=args.timeout),
            run_turn(B, rec_b, PLANT_PROMPT.format(canary=canaries["Y"]), timeout=args.timeout),
        )
        qa, qb = await asyncio.gather(
            run_turn(A, rec_a, RECALL_PROMPT, timeout=args.timeout),
            run_turn(B, rec_b, RECALL_PROMPT, timeout=args.timeout),
        )
        replies.update({"A_plant": ra.text, "B_plant": rb.text, "A_recall": qa.text, "B_recall": qb.text})
        ok_a, found_a = recall_check(qa.text, canaries, ["X"], ["Y"])
        ok_b, found_b = recall_check(qb.text, canaries, ["Y"], ["X"])
        cases["A2"] = {
            "pass": bool(all(r.success for r in (ra, rb, qa, qb)) and ok_a and ok_b),
            "A_recall_found": found_a,
            "B_recall_found": found_b,
        }
        print(f"A2: A_found={found_a} B_found={found_b}", flush=True)

        # A5: fork A -> A', plant Z in A'
        fork_ok = None
        found_f = {}
        try:
            forked = await A.fork(title="ctx-fork-probe")
            rec_a.meta("Forked", source_id=sessions["A"], fork_id=forked.id)
            sessions["A_fork"] = forked.id
            forked.on_notification(rec_a.raw)
            forked.on_notification(tracker_a.callback())
            rz = await run_turn(forked, rec_a, PLANT_PROMPT.format(canary=canaries["Z"]), timeout=args.timeout)
            qf = await run_turn(forked, rec_a, RECALL_PROMPT, timeout=args.timeout)
            replies["fork_plant"], replies["fork_recall"] = rz.text, qf.text
            ok_f, found_f = recall_check(qf.text, canaries, ["X", "Z"], ["Y"])
            fork_ok = bool(rz.success and qf.success and ok_f)
        except Exception as exc:
            rec_a.meta("ForkFailed", error=f"{type(exc).__name__}: {str(exc)[:240]}")
            forked = None
            print(f"A5 fork failed: {type(exc).__name__}: {str(exc)[:200]}", flush=True)

        # A4 (+ A5 other side): resume original A by id while B is alive
        os.chdir(a_dir)  # Session.resume() has no cwd; the subprocess inherits ours
        resumed = Session.resume(
            sessions["A"],
            interactions=InteractionHandlers(on_permission=make_permission_handler(rec_a)),
            runtime=runtime,
        )
        await open_meta(resumed, rec_a, resume=True)
        resumed.on_notification(tracker_a.callback())
        qr = await run_turn(resumed, rec_a, RECALL_PROMPT, timeout=args.timeout)
        replies["A_resumed_recall"] = qr.text
        ok_r, found_r = recall_check(qr.text, canaries, ["X"], ["Y", "Z"])
        b_alive = transport_pid(B) is not None
        cases["A4"] = {
            "pass": bool(qr.success and ok_r and b_alive),
            "resumed_found": found_r,
            "B_open_during_resume": b_alive,
            "note": "B's own history integrity is verified model-free by ctx_history_check.py",
        }
        print(f"A4: resumed_found={found_r} B_alive={b_alive}", flush=True)
        if fork_ok is None:
            cases["A5"] = {"pass": None, "note": "fork RPC failed; see ForkFailed"}
        else:
            cases["A5"] = {
                "pass": bool(fork_ok and found_r.get("Z") is False),
                "fork_recall_found": found_f,
                "resumed_A_found": found_r,
            }
        print(f"A5: fork_found={found_f}", flush=True)

        await close_meta(resumed, rec_a)
        resumed = None
        if forked is not None:
            await close_meta(forked, rec_a)
            forked = None
        await close_recorded(B, rec_b)
        B = None

        # A3: fresh session C, same Python process, same cwd as A
        C = make_session(a_dir, rec_a, args.model, args.reasoning, runtime)
        await open_meta(C, rec_a)
        C.on_notification(tracker_a.callback())
        sessions["C"] = C.id
        qc = await run_turn(C, rec_a, RECALL_PROMPT, timeout=args.timeout)
        replies["C_recall"] = qc.text
        ok_c, found_c = recall_check(qc.text, canaries, [], ["X", "Y", "Z"])
        cases["A3"] = {"pass": bool(qc.success and ok_c), "C_found": found_c}
        print(f"A3: C_found={found_c}", flush=True)
        await close_meta(C, rec_a)
        C = None
    except Exception as exc:
        fatal = f"{type(exc).__name__}: {str(exc)[:300]}"
        print(f"FATAL: {fatal}", flush=True)
    finally:
        for s in (resumed, forked, C, B, A):
            if s is None:
                continue
            try:
                await asyncio.wait_for(s.close(), 30)
            except Exception:
                pass
        state = {
            "canaries": canaries,
            "sessions": sessions,
            "pids": pids,
            "replies": replies,
            "cases": cases,
            "fatal": fatal,
            "usage": {"A_lineage": tracker_a.summary(), "B": tracker_b.summary()},
        }
        save_json(CTX_FIXTURES / "state.json", state)
        rec_a.meta("Cases", cases=cases, fatal=fatal)
        rec_a.close()
        rec_b.close()
    for name in sorted(cases):
        verdict = cases[name].get("pass")
        print(f"case {name}: {'PASS' if verdict else ('INCONCLUSIVE' if verdict is None else 'FAIL')}", flush=True)
    ok = fatal is None and all(cases.get(n, {}).get("pass") for n in ("A1", "A2", "A3", "A4", "A5"))
    return 0 if ok else 1


if __name__ == "__main__":
    setup_console()
    raise SystemExit(asyncio.run(main()))