"""Spike (item 7): Factory desktop update simulation against a fake install root.

Builds sandbox/robust/fakeapp/app-0.208.0/resources/bin/droid.exe as a copy of
the real exe (the real Factory folder is never touched), runs detection against
the fake root, opens a session from the copied exe (no model turn), then:

  - tries to delete/rename the running exe and rename its app folder
  - adds app-0.209.0 (the "update") and checks detection switches to it
  - checks the running session still answers RPCs after the folder rename
  - closes the session and confirms the exe can then be deleted
  - removes the fake root entirely

Writes fixtures/robust/app_update.jsonl.
"""

from __future__ import annotations

import asyncio
import shutil
import time
from pathlib import Path

from droid_sdk import Runtime, Session

from _common import Recorder, environment_info, prepare_sandbox, setup_console
from _robust_common import ROBUST_FIXTURES, ROBUST_SANDBOX, alive, session_droid_pid
from find_droid import bundled_candidates, droid_version, find_droid

FAKE_ROOT = ROBUST_SANDBOX / "fakeapp"
OLD_VER = "app-0.208.0"
NEW_VER = "app-0.209.0"


def exe_in(version: str) -> Path:
    return FAKE_ROOT / version / "resources" / "bin" / "droid.exe"


def main() -> int:
    asyncio.run(amain())


async def amain() -> int:
    prepare_sandbox()
    ROBUST_FIXTURES.mkdir(parents=True, exist_ok=True)
    recorder = Recorder(ROBUST_FIXTURES / "app_update.jsonl")
    real = find_droid()
    recorder.meta("Environment", real_executable=str(real), real_version=droid_version(real),
                  fake_root=str(FAKE_ROOT))
    failures: list[str] = []

    def step(name: str, **data) -> None:
        recorder.meta("Step", step=name, **data)
        print(f"[{name}] {data}", flush=True)

    if FAKE_ROOT.exists():
        shutil.rmtree(FAKE_ROOT, ignore_errors=True)
    exe208 = exe_in(OLD_VER)
    exe208.parent.mkdir(parents=True, exist_ok=True)
    started = time.perf_counter()
    shutil.copyfile(real, exe208)
    copy_s = time.perf_counter() - started
    step("copy", size_mb=round(exe208.stat().st_size / 1048576, 1), seconds=round(copy_s, 2))

    found = [(k, str(p)) for k, p in bundled_candidates(root=FAKE_ROOT)]
    step("detect_initial", found=found)
    if len(found) != 1 or OLD_VER not in found[0][1]:
        failures.append("initial detection did not find exactly app-0.208.0")

    try:
        version = droid_version(exe208)
        step("copied_version", version=version)
    except Exception as exc:
        failures.append(f"copied exe --version failed: {exc}")
        step("copied_version", error=str(exc))

    cwd = ROBUST_SANDBOX / "fakeapp_cwd"
    cwd.mkdir(parents=True, exist_ok=True)
    session = Session(cwd=cwd, runtime=Runtime(executable=exe208))
    session_id = None
    pid = None
    try:
        t0 = time.perf_counter()
        await session.open()
        open_s = time.perf_counter() - t0
        session_id = session.id
        pid = session_droid_pid(session)
        step("session_open", session_id=session_id, droid_pid=pid, open_s=round(open_s, 2),
             exe=str(exe208))

        try:
            exe208.unlink()
            step("delete_running_exe", result="SUCCEEDED (no lock!)")
            failures.append("could delete a running droid.exe")
        except OSError as exc:
            step("delete_running_exe", result=f"blocked: {type(exc).__name__} winerror={exc.winerror}")

        renamed = exe208.with_name("droid.exe.renamed")
        try:
            exe208.rename(renamed)
            step("rename_running_exe", result="SUCCEEDED")
            renamed.rename(exe208)
            step("rename_back", result="ok")
        except OSError as exc:
            step("rename_running_exe", result=f"blocked: {type(exc).__name__} winerror={exc.winerror}")

        exe209 = exe_in(NEW_VER)
        exe209.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(real, exe209)
        found2 = [(k, str(p)) for k, p in bundled_candidates(root=FAKE_ROOT)]
        step("detect_after_update", found=found2)
        if not found2 or NEW_VER not in found2[0][1]:
            failures.append("detection did not switch to app-0.209.0")

        t0 = time.perf_counter()
        tools = await session.list_tools()
        step("running_session_rpc", tool_count=len(tools), seconds=round(time.perf_counter() - t0, 2))

        old_dir = FAKE_ROOT / OLD_VER
        moved_dir = FAKE_ROOT / (OLD_VER + ".old")
        try:
            old_dir.rename(moved_dir)
            step("rename_running_app_folder", result="SUCCEEDED (folder rename works while exe runs)")
            found3 = [(k, str(p)) for k, p in bundled_candidates(root=FAKE_ROOT)]
            step("detect_after_folder_rename", found=found3)
            tools = await session.list_tools()
            step("session_alive_after_folder_rename", tool_count=len(tools))
        except OSError as exc:
            step("rename_running_app_folder", result=f"blocked: {type(exc).__name__} winerror={exc.winerror}")
            moved_dir = old_dir
    finally:
        if session_id is not None:
            t0 = time.perf_counter()
            await session.close()
            step("session_closed", seconds=round(time.perf_counter() - t0, 3),
                 droid_alive_after_close=alive(pid) if pid else None)

    candidate = FAKE_ROOT / (OLD_VER + ".old") / "resources" / "bin" / "droid.exe"
    if not candidate.exists():
        candidate = exe208
    try:
        candidate.unlink()
        step("delete_after_close", result="SUCCEEDED (lock released)")
    except OSError as exc:
        step("delete_after_close", result=f"still blocked: {type(exc).__name__} winerror={exc.winerror}")
        failures.append("exe still locked after session.close()")

    recorder.meta("Done", failures=failures)
    recorder.close()
    shutil.rmtree(FAKE_ROOT, ignore_errors=True)
    shutil.rmtree(cwd, ignore_errors=True)
    step_done = not failures
    print(f"failures: {failures}", flush=True)
    return 0 if step_done else 1


if __name__ == "__main__":
    setup_console()
    raise SystemExit(main())