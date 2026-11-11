"""Mutation checks for the harness security fixes: protected folder through links (R1), stopping a tool that runs in a
worker thread (R2), a regular expression that backtracks (R6).

    python sandbox/mutate_harness_security.py            run every mutant
    python sandbox/mutate_harness_security.py --check    only check that every pattern is found exactly once
"""
from __future__ import annotations

import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PY = str(ROOT / ".venv" / "Scripts" / "python.exe")
H = ROOT / "desktop" / "core" / "harness"
SHELL, PERM, TOOLS, FS = H / "shell.py", H / "permissions.py", H / "tools.py", H / "fs_tools.py"
LINKS = ["tests/test_harness_security_links.py"]
CANCEL = ["tests/test_harness_security_cancel.py"]
GREP = ["tests/test_harness_security_grep.py"]

# (2026-11-07: five entries were taken out: the code they mutate was replaced by the re-review fixes, whose own mutants are in sandbox/mutate_harness_rereview.py)
M = [
    # R1
    ("R1 decide: no check of where arguments lead", PERM, "        leads = protected_argument(command, cwd or ctx.project.resolve(), ctx.protected)", "        leads = None", LINKS),
    ("R1 decide: arguments read against the project, not cwd", PERM, "        leads = protected_argument(command, cwd or ctx.project.resolve(), ctx.protected)", "        leads = protected_argument(command, ctx.project.resolve(), ctx.protected)", LINKS),
    ("R1 shell tool: no check of where arguments lead", SHELL, "    leads = protected_argument(command, cwd, ctx.protected)", "    leads = None", LINKS),
    ("R1 links are not followed", SHELL, "        return (path if path.is_absolute() else cwd / path).resolve()", "        return Path(os.path.abspath(path if path.is_absolute() else cwd / path))", LINKS),
    ("R1 a wildcard folder counts as known", SHELL, "            if index < len(segments) - 1:\n                return None", "            if False:\n                return None", LINKS),
    ("R1 wildcards are not cut off", SHELL, "    fixed = _fixed_part(expanded)", "    fixed = expanded", LINKS),
    ("R1 no environment expansion", SHELL, "    expanded = _DEVICE_PREFIX.sub(\"\", _expand_environment(value))", "    expanded = _DEVICE_PREFIX.sub(\"\", value)", LINKS),
    ("R1 extended-length prefix kept", SHELL, "    expanded = _DEVICE_PREFIX.sub(\"\", _expand_environment(value))", "    expanded = _expand_environment(value)", LINKS),
    ("R1 network paths are resolved", SHELL, "    if re.match(r\"[\\\\/]{2}\", expanded):\n        return None", "    if False:\n        return None", LINKS),
    ("R1 drive-relative names count as known", SHELL, "        if path.drive and not path.is_absolute():\n            return None", "        if False:\n            return None", LINKS),
    ("R1 lists are not split", SHELL, "_GLUED = re.compile(r\"[;|&,]+\")", "_GLUED = re.compile(r\"[;|&]+\")", LINKS),
    ("R1 nested commands are not looked into", SHELL, "_NESTED_DEPTH = 2", "_NESTED_DEPTH = 0", LINKS),
    ("R1 read-only: an unknown target counts as inside", PERM, "        if target is None or not fs_tools.is_within(target, project):", "        if target is not None and not fs_tools.is_within(target, project):", LINKS),
    ("R1 read-only: only the whole token is checked", PERM, "    for value in argument_pieces(token):", "    for value in argument_pieces(token)[:1]:", LINKS),
    ("R1 wildcard matching is case-sensitive", SHELL, "fnmatch.fnmatch(name, segments[index])", "fnmatch.fnmatchcase(name, segments[index])", LINKS),
    ("R1 the expansion lists the current folder", SHELL, "    folder = resolved_argument(\"/\".join(segments[:index]) or \".\", cwd)", "    folder = resolved_argument(\".\", cwd)", LINKS),
    # R2
    ("R2 cancellation does not set abort", TOOLS, "        call.abort.set()\n", "", CANCEL),
    ("R2 cancellation does not wait for the thread", TOOLS, "        deadline = time.monotonic() + THREAD_WAIT_SECONDS", "        deadline = time.monotonic()", CANCEL),
    ("R2 the wait is not bounded", TOOLS, "            if remaining <= 0:\n                break", "            if False:\n                break", CANCEL),
    ("R2 no barrier before the folders are made", FS, "            _refuse_if_stopped(ctx)\n            path.parent.mkdir", "            path.parent.mkdir", CANCEL),
    ("R2 no lock per file", FS, "    with writer.lock:\n        yield", "    yield", CANCEL),
    ("R2 a new lock for every call", FS, "        writer = _writers.get(key)", "        writer = None", CANCEL),
    ("R2 no atomic replace", FS, "    temporary = _temporary_beside(path, data)", "    temporary = None", CANCEL),
    ("R2 a refused replace leaves its temporary file", FS, "            if not replaced:\n                with contextlib.suppress(OSError):\n                    os.unlink(temporary)", "            if not replaced:\n                pass", CANCEL),
    # R6
    ("R6 every pattern in a process", FS, "    if _plain_text(pattern):\n        out, truncated", "    if False:\n        out, truncated", GREP),
    ("R6 every pattern in this process", FS, "    if _plain_text(pattern):\n        out, truncated", "    if True:\n        out, truncated", GREP),
    ("R6 + is not a meta character", FS, "_REGEX_META = frozenset(\"\\\\^$.|?*+()[]{}\")", "_REGEX_META = frozenset(\"\\\\^$.|?*()[]{}\")", GREP),
    ("R6 no time limit", FS, "        if time.monotonic() >= deadline:\n            raise ToolError(\n                f\"The search took too long", "        if False:\n            raise ToolError(\n                f\"The search took too long", GREP),
    ("R6 a stop does not end the search", FS, "        if abort.is_set():\n            raise ToolError(\"Stopped by the user.\")\n        if time.monotonic() >= deadline:", "        if time.monotonic() >= deadline:", GREP),
    ("R6 the process is not killed", FS, "                process.kill()\n", "", GREP),
    ("R6 protected folders not passed on", FS, "        \"blocked\": [_fold(str(root)) for root in protected_roots(ctx)],", "        \"blocked\": [],", GREP),
    ("R6 a failed start is not reported", FS, "        except OSError as error:\n            raise ToolError(f\"The search could not be started", "        except ZeroDivisionError as error:\n            raise ToolError(f\"The search could not be started", GREP),
    ("R6 a process even when already stopped", FS, "    if abort.is_set():\n        raise ToolError(\"Stopped by the user.\")\n    folder = ", "    folder = ", GREP),
]

check_only = "--check" in sys.argv[1:]
only = [a for a in sys.argv[1:] if not a.startswith("--")]
survivors = []
for number, (name, path, old, new, tests) in enumerate(M, 1):
    if only and str(number) not in only:
        continue
    original = path.read_bytes()
    text = original.decode("utf-8")
    if text.count(old) != 1:
        print(f"[{number}] {name}: PATTERN FOUND {text.count(old)} TIMES", flush=True)
        survivors.append(name)
        continue
    if check_only:
        continue
    path.write_bytes(text.replace(old, new).encode("utf-8"))
    started = time.time()
    try:
        done = subprocess.run([PY, "-m", "pytest", "-q", "-x", "-p", "no:cacheprovider", *tests], cwd=ROOT, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=240)
        caught = done.returncode != 0
        lines = done.stdout.strip().splitlines() or ["?"]
        failed = next((line for line in lines if line.startswith(("FAILED", "ERROR"))), lines[-1])
    except subprocess.TimeoutExpired:
        caught, failed = True, "timeout (hangs: caught)"
    finally:
        path.write_bytes(original)
    print(f"[{number}] {'caught  ' if caught else 'SURVIVED'} {name}  ({time.time() - started:.0f}s) {failed[:160]}", flush=True)
    if not caught:
        survivors.append(name)
print("survivors:", survivors if survivors else "none")
