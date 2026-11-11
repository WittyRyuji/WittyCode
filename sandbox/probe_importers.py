"""Run the importers on this machine's real data and print counts and times only (never a title, a path or a message).

    .venv\\Scripts\\python.exe sandbox\\probe_importers.py           every app
    .venv\\Scripts\\python.exe sandbox\\probe_importers.py codex     one app

It only reads (the importers open nothing for writing). For each app: whether its folder is there, how many candidate
notes ``memories()`` gives, how many conversations ``sessions()`` lists (how many of those counts are exact), and for
the three newest how many messages ``read()`` gives and how long it took; what each call's ``report`` counted, and any
problem sentence (they are the importers' own words, not the user's).
"""

from __future__ import annotations

import sys
import time
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from desktop.core.importers import ImportFailed, sources, to_saved_session  # noqa: E402
from desktop.core.harness.store import pair_calls  # noqa: E402


def counts(report) -> str:
    return f"secret lines {report.secret_lines}, bad records {report.bad_records}, skipped {report.skipped}" + (
        f", problems {report.problems}" if report.problems else ""
    )


def main() -> int:
    wanted = set(sys.argv[1:])
    for source in sources():
        if wanted and source.app not in wanted:
            continue
        print(f"== {source.label} ({source.app})")
        if not source.available():
            print("   not available")
            continue
        started = time.perf_counter()
        try:
            notes = source.memories()
        except Exception as error:  # the probe must report, not stop
            print(f"   memories(): ERROR {type(error).__name__}")
            notes = []
        took = time.perf_counter() - started
        print(f"   memories(): {len(notes)} candidates in {took:.2f}s; {counts(source.report)}")
        if notes:
            kinds = Counter(note.kind for note in notes)
            scopes = Counter(note.scope for note in notes)
            longest = max(len(note.text) for note in notes)
            keys = len({note.key for note in notes})
            print(f"      kinds {dict(kinds)}, scopes {dict(scopes)}, longest text {longest}, distinct keys {keys}")
        started = time.perf_counter()
        try:
            listed = source.sessions()
        except Exception as error:
            print(f"   sessions(): ERROR {type(error).__name__}")
            listed = []
        took = time.perf_counter() - started
        exact = sum(1 for info in listed if info.exact)
        print(f"   sessions(): {len(listed)} conversations ({exact} with exact counts) in {took:.2f}s; {counts(source.report)}")
        order = all(listed[i].updated >= listed[i + 1].updated for i in range(len(listed) - 1))
        print(f"      newest first: {order}; with a folder: {sum(1 for info in listed if info.project)}; distinct keys: {len({i.key for i in listed})}")
        for number, info in enumerate(listed[:3], 1):
            started = time.perf_counter()
            try:
                messages = source.read(info)
            except ImportFailed as error:
                print(f"      #{number}: read() failed: {error.message}")
                continue
            except Exception as error:
                print(f"      #{number}: read() ERROR {type(error).__name__}")
                continue
            took = time.perf_counter() - started
            roles = Counter(message.role for message in messages)
            tools = sum(message.text.count("[工具]") for message in messages)
            saved = to_saved_session(info, messages, model="gpt-x", reasoning="medium", autonomy="code")
            paired = pair_calls(saved.items) is saved.items
            print(
                f"      #{number}: listed {info.messages}{'' if info.exact else '+'} / read {len(messages)} messages "
                f"({dict(roles)}, {tools} tool lines) in {took:.2f}s, {info.size // 1024} KiB; "
                f"saved turns {len(saved.turns)}, items {len(saved.items)}, valid history {paired}; {counts(source.report)}"
            )
    return 0


if __name__ == "__main__":
    sys.exit(main())
