"""Try sandbox/chatgpt_real_probe.py against the local fake of OpenAI's API (no network, no real account): the probe must run
to the end, report every variant and the tool round trip.

    python sandbox/chatgpt_probe_dryrun.py
"""
from __future__ import annotations

import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from tests.chatgpt_support import ACCESS_TOKEN, sign_in  # noqa: E402
from tests.fake_responses import FakeResponses, call, fail, say  # noqa: E402

with tempfile.TemporaryDirectory() as scratch, FakeResponses(token=ACCESS_TOKEN) as fake:
    root = Path(scratch) / "chatgpt"
    sign_in(root)
    # six variants (the second one is refused, as a service might), then the second request of the round trip
    fake.script(
        call("echo", {"text": "ok"}),
        fail("subscription_sharing_unsupported_capability", "namespace tools are not accepted", param="tools[0]"),
        call("echo", {"text": "ok"}),
        call("echo", {"text": "ok"}),
        call("echo", {"text": "ok"}),
        call("echo", {"text": "ok"}),
        say("done"),
    )
    done = subprocess.run(
        [sys.executable, str(ROOT / "sandbox" / "chatgpt_real_probe.py"), "--root", str(root), "--base-url", fake.base_url],
        cwd=ROOT, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=120,
    )
    print(done.stdout)
    print(done.stderr[-2000:])
    print("exit code:", done.returncode, "| requests the fake saw:", len(fake.requests))
    assert done.returncode == 0, "the probe did not finish"
    assert ACCESS_TOKEN not in done.stdout + done.stderr, "a token was printed"
    assert len(fake.requests) == 7
