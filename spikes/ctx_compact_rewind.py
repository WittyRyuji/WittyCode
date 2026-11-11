"""Phase 0b A7 (compaction) and A8 (rewind).

A7  One session plants three canary facts in a single turn, then
    session.compact() runs and a recall turn checks which canaries survived.
    Context usage (session.context()) and cumulative token usage are recorded
    before and after. The post-compaction recall turn doubles as the
    prompt-cache "after compaction" data point for section B.
A8  A second session gets a baseline message (turn 1, no canary) and one
    canary (turn 2). rewind() back to the turn-1 user message must make the
    canary forgotten. rewind_info() output is recorded first.

Live turns: 5 (+1 summarization call inside compact()).
Writes fixtures/context/compact_rewind.jsonl (+ .raw.jsonl) and merges into
fixtures/context/state.json.
"""

from __future__ import annotations

import _pyguard  # noqa: F401  -- require Python 3.11.x

import argparse
import asyncio
import time
from typing import Any

from droid_sdk import (
    Autonomy,
    InteractionHandlers,
    Session,
    SessionConfig,
    UserMessage,
)

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
    CTX_FIXTURES,
    CTX_SANDBOX,
    PLANT_PROMPT,
    RECALL_PROMPT,
    UsageTracker,
    find_canaries,
    load_json,
    new_canary,
    save_json,
    transport_pid,
)

PLANT3 = (
    "Remember these three facts for the rest of our conversation:\n"
    "- the first codeword is '{c1}'\n"
    "- the second codeword is '{c2}'\n"
    "- the third codeword is '{c3}'\n"
    "Reply with exactly: remembered"
)
RECALL3 = (
    "What were the three codewords I asked you to remember? Reply with "
    "exactly three lines:\n"
    "first=<codeword or unknown>\n"
    "second=<codeword or unknown>\n"
    "third=<codeword or unknown>"
)
BASELINE = "Reply with exactly: ready"


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


def make_session(cwd, recorder, model, reasoning, runtime):
    return Session(
        cwd=cwd,
        model=model,
        reasoning_effort=reasoning_effort(reasoning),
        config=SessionConfig(autonomy=Autonomy.OFF),
        interactions=InteractionHandlers(on_permission=make_permission_handler(recorder)),
        runtime=runtime,
    )


async def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--model", default=DEFAULT_MODEL)
    parser.add_argument("--reasoning", default=DEFAULT_REASONING)
    parser.add_argument("--timeout", type=float, default=180.0)
    args = parser.parse_args()

    used: set[str] = set()
    canaries: dict[str, str] = {}
    for name in ("c1", "c2", "c3", "rw"):
        canaries[name] = new_canary(used)
        used.add(canaries[name])

    runtime, droid = make_runtime()
    recorder = Recorder(
        CTX_FIXTURES / "compact_rewind.jsonl", raw_path=CTX_FIXTURES / "compact_rewind.raw.jsonl"
    )
    recorder.meta(
        "Environment",
        **environment_info(droid, model=args.model, reasoning=args.reasoning, autonomy="off", scope="A7+A8"),
    )
    tracker = UsageTracker()
    cases: dict[str, Any] = {}
    sessions: dict[str, str] = {}
    replies: dict[str, str] = {}

    # ---------------- A7: compaction ----------------
    s = None
    try:
        c_dir = CTX_SANDBOX / "compact"
        c_dir.mkdir(parents=True, exist_ok=True)
        s = make_session(c_dir, recorder, args.model, args.reasoning, runtime)
        await open_meta(s, recorder, phase="A7")
        s.on_notification(tracker.callback())
        sessions["compact_src"] = s.id
        recorder.meta("Process", pid=transport_pid(s))
        r1 = await run_turn(
            s,
            recorder,
            PLANT3.format(c1=canaries["c1"], c2=canaries["c2"], c3=canaries["c3"]),
            timeout=args.timeout,
        )
        replies["compact_plant"] = r1.text
        ctx_before = await s.context()
        cum_before = tracker.turn_results[-1]["cumulativeTokenUsage"] if tracker.turn_results else None
        outcome = await s.compact()
        recorder.meta(
            "Compacted",
            old_id=sessions["compact_src"],
            new_id=outcome.session.id,
            removed_count=outcome.removed_count,
        )
        succ = outcome.session
        sessions["compact_succ"] = succ.id
        succ.on_notification(recorder.raw)
        succ.on_notification(tracker.callback())
        ctx_after = await succ.context()
        r2 = await run_turn(succ, recorder, RECALL3, timeout=args.timeout)
        replies["compact_recall"] = r2.text
        survivors = find_canaries(r2.text or "", {k: canaries[k] for k in ("c1", "c2", "c3")})
        cases["A7"] = {
            "pass": bool(r1.success and r2.success),
            "removed_count": outcome.removed_count,
            "survivors": survivors,
            "survivor_count": sum(1 for v in survivors.values() if v),
            "context_before": to_jsonable(ctx_before),
            "context_after": to_jsonable(ctx_after),
            "cumulative_usage_before": cum_before,
        }
        s = None  # retired by compaction; closing the source is a no-op
        await close_meta(succ, recorder)
    except Exception as exc:
        cases["A7"] = {"pass": None, "error": f"{type(exc).__name__}: {str(exc)[:240]}"}
        print(f"A7 failed: {type(exc).__name__}: {str(exc)[:200]}", flush=True)
        if s is not None:
            try:
                await close_meta(s, recorder)
            except Exception:
                pass
            s = None
    print(f"A7: {cases['A7']}", flush=True)

    # ---------------- A8: rewind ----------------
    s2 = None
    try:
        r_dir = CTX_SANDBOX / "rewind"
        r_dir.mkdir(parents=True, exist_ok=True)
        s2 = make_session(r_dir, recorder, args.model, args.reasoning, runtime)
        await open_meta(s2, recorder, phase="A8")
        s2.on_notification(tracker.callback())
        sessions["rewind_src"] = s2.id
        captured: dict[str, str] = {}

        def on_event(event):
            if isinstance(event, UserMessage) and not str(event.id).startswith("context-") and "msg_id" not in captured:
                captured["msg_id"] = event.id

        rb = await run_turn(s2, recorder, BASELINE, timeout=args.timeout, on_event=on_event)
        replies["rewind_baseline"] = rb.text
        rp = await run_turn(s2, recorder, PLANT_PROMPT.format(canary=canaries["rw"]), timeout=args.timeout)
        replies["rewind_plant"] = rp.text
        info = await s2.rewind_info(captured["msg_id"])
        recorder.meta(
            "RewindInfo",
            message_id=captured["msg_id"],
            available=len(info.available_files),
            created=[f.file_path for f in info.created_files],
            evicted=[f.file_path for f in info.evicted_files],
        )
        outcome2 = await s2.rewind(captured["msg_id"], title="ctx-rewind-probe")
        recorder.meta(
            "Rewound",
            old_id=sessions["rewind_src"],
            new_id=outcome2.session.id,
            restored=outcome2.restored_count,
            deleted=outcome2.deleted_count,
            failed_restore=outcome2.failed_restore_count,
            failed_delete=outcome2.failed_delete_count,
        )
        rew = outcome2.session
        sessions["rewind_succ"] = rew.id
        rew.on_notification(recorder.raw)
        rew.on_notification(tracker.callback())
        r3 = await run_turn(rew, recorder, RECALL_PROMPT, timeout=args.timeout)
        replies["rewind_recall"] = r3.text
        found = find_canaries(r3.text or "", canaries)
        cases["A8"] = {
            "pass": bool(rb.success and rp.success and r3.success and not found["rw"]),
            "recall_found": found,
            "baseline_msg_id": captured["msg_id"],
        }
        s2 = None  # retired by rewind
        await close_meta(rew, recorder)
    except Exception as exc:
        cases["A8"] = {"pass": None, "error": f"{type(exc).__name__}: {str(exc)[:240]}"}
        print(f"A8 failed: {type(exc).__name__}: {str(exc)[:200]}", flush=True)
        if s2 is not None:
            try:
                await close_meta(s2, recorder)
            except Exception:
                pass
            s2 = None
    print(f"A8: {cases['A8']}", flush=True)

    recorder.meta("UsageTracker", **tracker.summary())
    recorder.close()

    state_path = CTX_FIXTURES / "state.json"
    state = load_json(state_path) if state_path.exists() else {}
    state.setdefault("canaries", {}).update(canaries)
    state.setdefault("sessions", {}).update(sessions)
    state.setdefault("cases", {}).update(cases)
    state["usage_compact_rewind"] = tracker.summary()
    save_json(state_path, state)
    for name in ("A7", "A8"):
        verdict = cases.get(name, {}).get("pass")
        print(
            f"case {name}: {'PASS' if verdict else ('INCONCLUSIVE' if verdict is None else 'FAIL')}",
            flush=True,
        )
    ok = all(cases.get(n, {}).get("pass") for n in ("A7", "A8"))
    return 0 if ok else 1


if __name__ == "__main__":
    setup_console()
    raise SystemExit(asyncio.run(main()))