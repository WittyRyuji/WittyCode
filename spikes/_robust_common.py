"""Shared helpers for the WittyCode Phase 0b robustness spikes.

Pure-ctypes Windows process utilities (no new dependencies):
- per-process memory (working set / private bytes) and process-tree listing
- a background sampler tracking a set of process trees over time
- a Win32 Job Object with JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE (cleanup spike)

Plus the robust sandbox/fixture paths and a helper to read the PID of the
droid.exe process behind a high-level Session.
"""

from __future__ import annotations

import ctypes
import os
import threading
import time
from ctypes import wintypes
from typing import Any

from _common import FIXTURES, SANDBOX

ROBUST_SANDBOX = SANDBOX / "robust"
ROBUST_FIXTURES = FIXTURES / "robust"

kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
PROCESS_SET_QUOTA = 0x0100
PROCESS_TERMINATE = 0x0001
TH32CS_SNAPPROCESS = 0x00000002
INVALID_HANDLE_VALUE = ctypes.c_void_p(-1).value
JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x00002000
JobObjectExtendedLimitInformation = 9


class PROCESS_MEMORY_COUNTERS_EX(ctypes.Structure):
    _fields_ = [
        ("cb", wintypes.DWORD),
        ("PageFaultCount", wintypes.DWORD),
        ("PeakWorkingSetSize", ctypes.c_size_t),
        ("WorkingSetSize", ctypes.c_size_t),
        ("QuotaPeakPagedPoolUsage", ctypes.c_size_t),
        ("QuotaPagedPoolUsage", ctypes.c_size_t),
        ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t),
        ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
        ("PagefileUsage", ctypes.c_size_t),
        ("PeakPagefileUsage", ctypes.c_size_t),
        ("PrivateUsage", ctypes.c_size_t),
    ]


class PROCESSENTRY32W(ctypes.Structure):
    _fields_ = [
        ("dwSize", wintypes.DWORD),
        ("cntUsage", wintypes.DWORD),
        ("th32ProcessID", wintypes.DWORD),
        ("th32DefaultHeapID", ctypes.c_size_t),
        ("th32ModuleID", wintypes.DWORD),
        ("cntThreads", wintypes.DWORD),
        ("th32ParentProcessID", wintypes.DWORD),
        ("pcPriClassBase", wintypes.LONG),
        ("dwFlags", wintypes.DWORD),
        ("szExeFile", wintypes.WCHAR * 260),
    ]


def process_table() -> dict[int, dict[str, Any]]:
    """{pid: {"ppid": int, "exe": str}} for every process (Toolhelp32 snapshot)."""
    snapshot = kernel32.CreateToolhelp32Snapshot(TH32CS_SNAPPROCESS, 0)
    if snapshot is None or snapshot == INVALID_HANDLE_VALUE:
        raise ctypes.WinError(ctypes.get_last_error())
    table: dict[int, dict[str, Any]] = {}
    try:
        entry = PROCESSENTRY32W()
        entry.dwSize = ctypes.sizeof(entry)
        ok = kernel32.Process32FirstW(snapshot, ctypes.byref(entry))
        while ok:
            table[entry.th32ProcessID] = {
                "ppid": entry.th32ParentProcessID,
                "exe": entry.szExeFile,
            }
            entry = PROCESSENTRY32W()
            entry.dwSize = ctypes.sizeof(entry)
            ok = kernel32.Process32NextW(snapshot, ctypes.byref(entry))
    finally:
        kernel32.CloseHandle(snapshot)
    return table


def descendants_of(root_pid: int, table: dict[int, dict[str, Any]] | None = None) -> dict[int, dict[str, Any]]:
    """Every process whose ancestor chain includes root_pid (root included)."""
    table = process_table() if table is None else table
    children: dict[int, list[int]] = {}
    for pid, info in table.items():
        children.setdefault(info["ppid"], []).append(pid)
    out: dict[int, dict[str, Any]] = {}
    stack = [root_pid]
    while stack:
        pid = stack.pop()
        if pid in out:
            continue
        info = table.get(pid)
        out[pid] = info if info is not None else {"ppid": None, "exe": None, "gone": True}
        stack.extend(children.get(pid, ()))
    return out


def process_memory(pid: int) -> dict[str, int] | None:
    """Working set / private bytes for one pid; None when the process is gone."""
    handle = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not handle:
        return None
    try:
        counters = PROCESS_MEMORY_COUNTERS_EX()
        counters.cb = ctypes.sizeof(counters)
        if not kernel32.K32GetProcessMemoryInfo(handle, ctypes.byref(counters), counters.cb):
            return None
        return {
            "rss": counters.WorkingSetSize,
            "private": counters.PrivateUsage,
            "peak_rss": counters.PeakWorkingSetSize,
            "peak_private": counters.PeakPagefileUsage,
        }
    finally:
        kernel32.CloseHandle(handle)


def alive(pid: int) -> bool:
    return process_memory(pid) is not None


def tree_memory(root_pid: int) -> dict[str, Any]:
    """Aggregate memory of root_pid plus all current descendants."""
    tree = descendants_of(root_pid)
    total_rss = 0
    total_private = 0
    members = []
    for pid, info in sorted(tree.items()):
        mem = process_memory(pid)
        members.append({"pid": pid, "exe": info.get("exe"), "ppid": info.get("ppid"), "memory": mem})
        if mem:
            total_rss += mem["rss"]
            total_private += mem["private"]
    return {
        "root": root_pid,
        "alive": alive(root_pid),
        "member_count": len(members),
        "rss": total_rss,
        "private": total_private,
        "members": members,
    }


def terminate(pid: int, exit_code: int = 1) -> bool:
    """TerminateProcess on a pid this spike started. False when already gone."""
    handle = kernel32.OpenProcess(PROCESS_TERMINATE, False, pid)
    if not handle:
        return False
    try:
        return bool(kernel32.TerminateProcess(handle, exit_code))
    finally:
        kernel32.CloseHandle(handle)


def mb(value: int | float | None) -> float | None:
    return None if value is None else round(value / (1024 * 1024), 1)


class TreeSampler:
    """Background thread sampling a set of labelled process trees + this process."""

    def __init__(self, interval: float = 0.25) -> None:
        self.interval = interval
        self._roots: dict[str, int] = {}
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._t0 = time.perf_counter()
        self.samples: list[dict[str, Any]] = []
        self.peaks: dict[str, dict[str, int]] = {}

    def set_root(self, label: str, pid: int | None) -> None:
        with self._lock:
            if pid is None:
                self._roots.pop(label, None)
            else:
                self._roots[label] = pid

    def labels(self) -> list[str]:
        with self._lock:
            return sorted(self._roots)

    def _bump(self, label: str, key: str, value: int) -> None:
        peak = self.peaks.setdefault(label, {"rss": 0, "private": 0, "members": 0})
        peak[key] = max(peak.get(key, 0), value)

    def _sample_once(self) -> None:
        with self._lock:
            roots = dict(self._roots)
        point: dict[str, Any] = {"t": round(time.perf_counter() - self._t0, 3), "trees": {}}
        for label, pid in roots.items():
            agg = tree_memory(pid)
            point["trees"][label] = {
                "alive": agg["alive"],
                "members": agg["member_count"],
                "rss": agg["rss"],
                "private": agg["private"],
            }
            self._bump(label, "rss", agg["rss"])
            self._bump(label, "private", agg["private"])
            self._bump(label, "members", agg["member_count"])
        self_mem = process_memory(os.getpid())
        if self_mem:
            point["python"] = {"rss": self_mem["rss"], "private": self_mem["private"]}
            self._bump("python", "rss", self_mem["rss"])
            self._bump("python", "private", self_mem["private"])
        self.samples.append(point)

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                self._sample_once()
            except Exception:
                pass
            self._stop.wait(self.interval)

    def start(self) -> None:
        if self._thread is None:
            self._thread = threading.Thread(target=self._run, name="tree-sampler", daemon=True)
            self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=5)
            self._thread = None


class IO_COUNTERS(ctypes.Structure):
    _fields_ = [
        ("ReadOperationCount", ctypes.c_uint64),
        ("WriteOperationCount", ctypes.c_uint64),
        ("OtherOperationCount", ctypes.c_uint64),
        ("ReadTransferCount", ctypes.c_uint64),
        ("WriteTransferCount", ctypes.c_uint64),
        ("OtherTransferCount", ctypes.c_uint64),
    ]


class JOBOBJECT_BASIC_LIMIT_INFORMATION(ctypes.Structure):
    _fields_ = [
        ("PerProcessUserTimeLimit", ctypes.c_int64),
        ("PerJobUserTimeLimit", ctypes.c_int64),
        ("LimitFlags", wintypes.DWORD),
        ("MinimumWorkingSetSize", ctypes.c_size_t),
        ("MaximumWorkingSetSize", ctypes.c_size_t),
        ("ActiveProcessLimit", wintypes.DWORD),
        ("Affinity", ctypes.c_size_t),
        ("PriorityClass", wintypes.DWORD),
        ("SchedulingClass", wintypes.DWORD),
    ]


class JOBOBJECT_EXTENDED_LIMIT_INFORMATION(ctypes.Structure):
    _fields_ = [
        ("BasicLimitInformation", JOBOBJECT_BASIC_LIMIT_INFORMATION),
        ("IoInfo", IO_COUNTERS),
        ("ProcessMemoryLimit", ctypes.c_size_t),
        ("JobMemoryLimit", ctypes.c_size_t),
        ("PeakProcessMemoryUsed", ctypes.c_size_t),
        ("PeakJobMemoryUsed", ctypes.c_size_t),
    ]


class KillOnCloseJob:
    """Win32 Job Object with JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE.

    Children spawned by a member process join the job automatically, so the
    whole droid.exe tree is terminated by the OS when the last job handle is
    closed - including when the owning Python process is killed with
    taskkill /F (handle closes on process death).
    """

    def __init__(self) -> None:
        self._handle = kernel32.CreateJobObjectW(None, None)
        if not self._handle:
            raise ctypes.WinError(ctypes.get_last_error())
        info = JOBOBJECT_EXTENDED_LIMIT_INFORMATION()
        info.BasicLimitInformation.LimitFlags = JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        if not kernel32.SetInformationJobObject(
            self._handle, JobObjectExtendedLimitInformation, ctypes.byref(info), ctypes.sizeof(info)
        ):
            kernel32.CloseHandle(self._handle)
            self._handle = None
            raise ctypes.WinError(ctypes.get_last_error())

    def assign(self, pid: int) -> None:
        proc = kernel32.OpenProcess(PROCESS_SET_QUOTA | PROCESS_TERMINATE, False, pid)
        if not proc:
            raise ctypes.WinError(ctypes.get_last_error())
        try:
            if not kernel32.AssignProcessToJobObject(self._handle, proc):
                raise ctypes.WinError(ctypes.get_last_error())
        finally:
            kernel32.CloseHandle(proc)

    def close(self) -> None:
        if self._handle:
            kernel32.CloseHandle(self._handle)
            self._handle = None


def session_droid_pid(session: Any) -> int | None:
    """PID of the droid.exe subprocess behind an open high-level Session."""
    client = getattr(session, "_client", None)
    transport = getattr(client, "_transport", None)
    return getattr(transport, "pid", None)


def make_tick_command(seconds: int = 8, step: float = 1.0) -> str:
    """PowerShell command printing one line per step for `seconds` iterations."""
    return (
        f"1..{seconds} | ForEach-Object "
        "{ Write-Output ('tick ' + $_); Start-Sleep -Seconds " + f"{step:g}" + " }"
    )