"""Mutation checks for cancelling a streamed ChatGPT request: the persistent cancelled flag, the late connection, the bounded
queue between the worker thread and the reader, and the fast abort of an open connection (core/chatgpt/http.py).

    python sandbox/mutate_http_cancel.py            run every mutant
    python sandbox/mutate_http_cancel.py --check    only check that every pattern is found exactly once
    python sandbox/mutate_http_cancel.py --only abort:    run the mutants whose name contains the text
"""
from __future__ import annotations

import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PY = str(ROOT / ".venv" / "Scripts" / "python.exe")
HTTP = ROOT / "desktop" / "core" / "chatgpt" / "http.py"
TESTS = ["tests/test_chatgpt_http_cancel.py", "tests/test_chatgpt_http.py"]

M = [
    ("flag: the worker connects although the call was cancelled", HTTP, "                if self.cancelled.is_set():\n                    return\n                held = self._opener.open", "                held = self._opener.open"),
    ("flag: a response that arrives after the cancel is read", HTTP, "            if not self.cancelled.is_set():\n                self._response = response\n                return True", "            self._response = response\n            return True"),
    ("flag: a cancel does not set it", HTTP, "            self.cancelled.set()\n            response = self._response", "            response = self._response"),
    ("flag: the worker goes on delivering after a cancel", HTTP, "        while not self.cancelled.is_set():\n            try:\n                self._lines.put", "        while True:\n            try:\n                self._lines.put"),
    ("worker: what it holds is not closed when it exits", HTTP, "        finally:\n            _abort(held)\n", "        finally:\n            pass\n"),
    ("worker: a finished response waits for the reader to take the end", HTTP, "                _abort(held)  # (a consumer that is slow to take the end does not keep the connection)\n", ""),
    ("worker: not a daemon thread", HTTP, 'name="chatgpt-stream", daemon=True', 'name="chatgpt-stream"'),
    ("worker: the idle timeout is not handed to the opener", HTTP, "self._opener.open(self._request, timeout=self._timeout)", "self._opener.open(self._request)"),
    ("worker: the line end is kept", HTTP, 'raw.decode("utf-8", "replace").rstrip("\\r\\n")', 'raw.decode("utf-8", "replace")'),
    ("worker: a failure that is not a network error never reaches the reader", HTTP, '        except BaseException as error:  # the consumer must not hang on anything\n            self._put("error", error)\n', ""),
    ("worker: a network error is raised as it came", HTTP, 'self._put("error", _network_error(error))', 'self._put("error", error)'),
    ("queue: unbounded", HTTP, "queue.Queue(STREAM_QUEUE_LINES)", "queue.Queue()"),
    ("queue: a full queue is waited on without a look at the flag", HTTP, "self._lines.put(item, timeout=CANCEL_POLL_SECONDS)", "self._lines.put(item)"),
    ("queue: a cancel does not make room", HTTP, "        # (a worker waiting for room gets it at once, sees the flag and goes)\n        with contextlib.suppress(queue.Empty):\n            while True:\n                self._lines.get_nowait()\n", ""),
    ("queue: the wait for room is a minute", HTTP, "CANCEL_POLL_SECONDS = 0.05", "CANCEL_POLL_SECONDS = 60.0"),
    ("queue: the default bound is none", HTTP, "STREAM_QUEUE_LINES = 256", "STREAM_QUEUE_LINES = 0"),
    ("loop: a full queue with the loop gone is waited on for ever", HTTP, "                if self._loop.is_closed():  # (nobody will ever make room)\n", "                if False:\n"),
    ("loop: a gone loop does not stop the worker", HTTP, "            except RuntimeError:  # the loop is gone\n                self.cancel()\n                return False", "            except RuntimeError:  # the loop is gone\n                return True"),
    ("wake: the reader is not told about an item", HTTP, "            try:\n                self._loop.call_soon_threadsafe(self._wake.set)\n", "            try:\n                pass\n"),
    ("wake: set from the worker thread without the loop", HTTP, "self._loop.call_soon_threadsafe(self._wake.set)", "self._wake.set()"),
    ("wake: the flag is never cleared (the reader spins)", HTTP, "                    wake.clear()\n", ""),
    ("reader: leaving the generator does not cancel", HTTP, "        finally:\n            call.cancel()\n", "        finally:\n            pass\n"),
    ("abort: a cancel does not drop the open connection", HTTP, "        _drop_connection(response)\n        # (a worker waiting for room", "        # (a worker waiting for room"),
    ("abort: the handle is only shut down, not closed", HTTP, "        socket.socket._real_close(sock)\n", ""),
    ("abort: the connection of an error response is not registered", HTTP, "                held = error\n                if self._adopt(error):\n", "                held = error\n                if True:\n"),
    ("abort: the socket under an error response is not found", HTTP, "    except AttributeError:\n        return response.fp.fp.raw._sock", "    except AttributeError:\n        raise"),
    ("errors: a cut body is an internal error", HTTP, "ValueError, http.client.HTTPException) as error:\n            self._put(\"error\", _network_error(error))", "ValueError) as error:\n            self._put(\"error\", _network_error(error))"),
]

args = sys.argv[1:]
check_only = "--check" in args
only = args[args.index("--only") + 1] if "--only" in args else ""
survivors = []
for number, (name, path, old, new) in enumerate(M, 1):
    if only and only not in name:
        continue
    original = path.read_bytes()  # (restored byte for byte: the file's own line ends stay)
    text = original.decode("utf-8")
    if "\r\n" in text:
        old, new = old.replace("\n", "\r\n"), new.replace("\n", "\r\n")
    if text.count(old) != 1:
        print(f"[{number}] {name}: PATTERN FOUND {text.count(old)} TIMES", flush=True)
        survivors.append(name)
        continue
    if check_only:
        continue
    path.write_bytes(text.replace(old, new).encode("utf-8"))
    started = time.time()
    try:
        done = subprocess.run([PY, "-m", "pytest", "-q", "-x", "-p", "no:cacheprovider", *TESTS], cwd=ROOT, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=300)
        caught = done.returncode != 0
        tail = (done.stdout.strip().splitlines() or ["?"])[-1]
    except subprocess.TimeoutExpired:
        caught, tail = True, "timeout (hangs: caught)"
    finally:
        path.write_bytes(original)
    print(f"[{number}] {'caught  ' if caught else 'SURVIVED'} {name}  ({time.time() - started:.0f}s) {tail if not caught or 'error' in tail.lower() else ''}", flush=True)
    if not caught:
        survivors.append(name)

print(f"\n{len(M) - len(survivors)} of {len(M)} caught" if not check_only else f"{len(M) - len(survivors)} of {len(M)} patterns found once")
for name in survivors:
    print("  survivor:", name)
sys.exit(1 if survivors else 0)
