"""Phase 0b A-supplement: low-level history scan (no model calls).

For every session id recorded in fixtures/context/state.json, load the raw
history via droid_sdk.low_level.load_session and scan the message JSON in
memory for every known canary. Each session must contain only canaries from
its own scope; cross-session leakage fails the check. Dump text is never
written to disk or console -- only booleans and counts.

Writes fixtures/context/history_check.json. Exit 0 iff all checks pass.
"""

from __future__ import annotations

import _pyguard  # noqa: F401  -- require Python 3.11.x

import asyncio
from typing import Any

from _ctx_common import (
    CTX_FIXTURES,
    CTX_SANDBOX,
    find_canaries,
    history_dump,
    load_json,
    save_json,
)

# role -> (sandbox cwd, canaries that MUST be present, canaries whose absence
# is only recorded, not asserted)
SCOPES: dict[str, dict[str, Any]] = {
    "A": {"cwd": "a", "expect": ["X"]},
    "B": {"cwd": "b", "expect": ["Y"]},
    "A_fork": {"cwd": "a", "expect": ["X", "Z"]},
    "C": {"cwd": "a", "expect": []},
    "actions": {"cwd": "actions", "expect": ["payload_interrupt", "payload_cancel"]},
    # compact_src has the same id as compact_succ (compaction reuses the id);
    # whatever load_session returns for that id is the successor's history.
    "compact_succ": {"cwd": "compact", "expect": [], "soft_expect": ["c1", "c2", "c3"]},
    "rewind_src": {"cwd": "rewind", "expect": ["rw"]},
    "rewind_succ": {"cwd": "rewind", "expect": [], "forbid_extra_assert": True},
}

ALL_CANARY_ROLES = ["X", "Y", "Z", "payload_interrupt", "payload_cancel", "c1", "c2", "c3", "rw"]


async def main() -> int:
    state = load_json(CTX_FIXTURES / "state.json")
    canaries: dict[str, str] = state["canaries"]
    sessions: dict[str, str] = state["sessions"]
    results: dict[str, Any] = {}
    ok = True
    for role, spec in SCOPES.items():
        sid = sessions.get(role)
        if not sid:
            results[role] = {"pass": False, "error": "session id missing from state.json"}
            ok = False
            continue
        cwd = CTX_SANDBOX / spec["cwd"]
        try:
            info = await history_dump(sid, cwd)
        except Exception as exc:
            results[role] = {"pass": False, "error": f"{type(exc).__name__}: {str(exc)[:200]}"}
            ok = False
            continue
        found = find_canaries(info["dump"], canaries)
        expected = set(spec["expect"]) | set(spec.get("soft_expect", []))
        unexpected = sorted(n for n in ALL_CANARY_ROLES if found.get(n) and n not in expected)
        missing = sorted(n for n in spec["expect"] if not found.get(n))
        soft_missing = sorted(n for n in spec.get("soft_expect", []) if not found.get(n))
        passed = not unexpected and not missing
        results[role] = {
            "session_id": sid,
            "message_count": info["message_count"],
            "is_agent_loop_in_progress": info["is_agent_loop_in_progress"],
            "found": found,
            "unexpected": unexpected,
            "missing_expected": missing,
            "soft_missing": soft_missing,
            "pass": passed,
        }
        ok = ok and passed
        print(
            f"{role}: msgs={info['message_count']} found="
            f"{sorted(n for n in ALL_CANARY_ROLES if found.get(n))} unexpected={unexpected} "
            f"missing={missing} soft_missing={soft_missing} -> {'PASS' if passed else 'FAIL'}",
            flush=True,
        )
    save_json(CTX_FIXTURES / "history_check.json", {"results": results, "pass": ok})
    print(f"history check: {'PASS' if ok else 'FAIL'}", flush=True)
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))