"""Shared helpers for the WittyCode Phase 0b context/cache spikes (A/B/C).

Canaries, per-test sandbox/fixture paths, process inspection, raw token-usage
collection and low-level history scanning. New file; Phase 0 scripts do not
use it.
"""

from __future__ import annotations

import json
import random
import subprocess
import time
from pathlib import Path
from typing import Any

import _pyguard  # noqa: F401  -- abort on anything but Python 3.11.x

from _common import FIXTURES, SANDBOX, to_jsonable
from find_droid import find_droid

CTX_SANDBOX = SANDBOX / "context"
CACHE_SANDBOX = SANDBOX / "cache"
LONG_SANDBOX = SANDBOX / "long"
CTX_FIXTURES = FIXTURES / "context"
CACHE_FIXTURES = FIXTURES / "cache"
LONG_FIXTURES = FIXTURES / "long_session"

for _dir in (CTX_SANDBOX, CACHE_SANDBOX, LONG_SANDBOX, CTX_FIXTURES, CACHE_FIXTURES, LONG_FIXTURES):
    _dir.mkdir(parents=True, exist_ok=True)

_WORDS = (
    "amber basil cedar delta ember fjord garnet harbor ivory juniper "
    "krypton lumen magma nectar onyx prism quartz raven saffron topaz"
).split()


def new_canary(used: set[str]) -> str:
    """Unambiguous random codeword like 'falcon-7391'."""
    while True:
        value = f"{random.choice(_WORDS)}-{random.randint(1000, 9999)}"
        if value not in used:
            return value


PLANT_PROMPT = (
    "Remember this fact for the rest of our conversation: the codeword is "
    "'{canary}'. Reply with exactly: remembered"
)
RECALL_PROMPT = (
    "What codeword did I ask you to remember in this conversation? Reply with "
    "exactly two lines. Line 1: the codeword, or 'none' if you were never "
    "given one. Line 2: any other codewords mentioned to you in this "
    "conversation, or 'none'."
)


def find_canaries(text: str, canaries: dict[str, str]) -> dict[str, bool]:
    """Which named canary values appear verbatim in text."""
    return {name: (value in text) for name, value in canaries.items()}


def save_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(to_jsonable(data), ensure_ascii=False, indent=1) + "\n",
        encoding="utf-8",
        newline="\n",
    )


def load_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def transport_pid(session: Any) -> int | None:
    """PID of the droid.exe behind an open high-level Session (SDK-private chain)."""
    client = getattr(session, "_client", None)
    transport = getattr(client, "_transport", None)
    pid = getattr(transport, "pid", None)
    return pid if isinstance(pid, int) else None


def droid_pids() -> list[int]:
    """All droid.exe PIDs on this machine (may include the Factory app's own)."""
    try:
        out = subprocess.run(
            ["tasklist", "/FI", "IMAGENAME eq droid.exe", "/FO", "CSV", "/NH"],
            capture_output=True,
            text=True,
            timeout=20,
        )
    except (OSError, subprocess.TimeoutExpired):
        return []
    pids: list[int] = []
    for line in out.stdout.splitlines():
        parts = [p.strip().strip('"') for p in line.split('","')]
        if len(parts) >= 2 and parts[0].lower() == "droid.exe":
            try:
                pids.append(int(parts[1]))
            except ValueError:
                pass
    return pids


def process_memory_bytes(pid: int) -> int | None:
    """Working set of a live process, or None if it is gone."""
    try:
        out = subprocess.run(
            [
                "powershell",
                "-NoProfile",
                "-Command",
                f"$p = Get-Process -Id {pid} -ErrorAction SilentlyContinue; "
                "if ($p) { $p.WorkingSet64 }",
            ],
            capture_output=True,
            text=True,
            timeout=30,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    text = out.stdout.strip()
    return int(text) if text.isdigit() else None


class UsageTracker:
    """Collects raw token-usage notifications.

    session_token_usage_changed and agent_turn_completed carry the cache
    fields (lastCallTokenUsage, per-turn cacheRead/cacheCreation) that the
    high-level stream drops.
    """

    def __init__(self) -> None:
        self.t0 = time.perf_counter()
        self.usage_events: list[dict[str, Any]] = []
        self.turn_results: list[dict[str, Any]] = []

    def _t(self) -> float:
        return round(time.perf_counter() - self.t0, 3)

    def callback(self):
        def cb(notification: Any) -> None:
            if not isinstance(notification, dict):
                return
            kind = notification.get("type")
            if kind == "session_token_usage_changed":
                self.usage_events.append(
                    {
                        "t": self._t(),
                        "tokenUsage": notification.get("tokenUsage"),
                        "lastCallTokenUsage": notification.get("lastCallTokenUsage"),
                    }
                )
            elif kind == "agent_turn_completed":
                self.turn_results.append(
                    {
                        "t": self._t(),
                        "reason": notification.get("reason"),
                        "tokenUsage": notification.get("tokenUsage"),
                        "cumulativeTokenUsage": notification.get("cumulativeTokenUsage"),
                        "durationMs": notification.get("durationMs"),
                    }
                )

        return cb

    def summary(self) -> dict[str, Any]:
        return {"usage_events": self.usage_events, "turn_results": self.turn_results}


async def history_dump(session_id: str, cwd: Path) -> dict[str, Any]:
    """Low-level load_session (no model call).

    Returns the message count plus the full JSON dump for IN-MEMORY canary
    scanning only -- never write the dump to disk or console (it contains the
    hidden llm_only context text).
    """
    from droid_sdk.low_level import DroidClient, ProcessTransport

    client = DroidClient(
        transport=ProcessTransport(exec_path=str(find_droid()), cwd=str(cwd))
    )
    await client.connect()
    try:
        result = await client.load_session(session_id=session_id)
        messages = result.session.messages
        dump = json.dumps(to_jsonable(messages), ensure_ascii=False)
        return {
            "session_id": session_id,
            "message_count": len(messages),
            "is_agent_loop_in_progress": result.is_agent_loop_in_progress,
            "dump": dump,
        }
    finally:
        try:
            await client.close_session(reason="other")
        finally:
            await client.close()