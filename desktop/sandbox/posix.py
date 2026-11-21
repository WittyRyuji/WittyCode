"""POSIX sandbox backends: Linux bubblewrap and macOS Seatbelt.

Implements the fixed backend contract of :mod:`desktop.sandbox.core`:
``probe()`` reports whether the native backend is usable and ``execute()``
runs one command inside it. There is no fallback to a plain subprocess —
an unsupported platform, a missing trusted helper or a failed launch is
an explicit error.

Linux uses bubblewrap (``bwrap``): fresh user, mount, pid, net, ipc and
uts namespaces. A small set of runtime dirs is bound read-only — the
whole ``/etc`` tree is not — the workspace is read-only unless
``writable``, ``readable`` grants are bound read-only, ``scratch`` is
writable, and each ``protected`` path is masked by a host-side read-only
object the guest cannot write (an empty directory for directories, an
empty regular file for files), so a protected entry is neither readable
nor writable and never leaks its original content. The child starts in
the workspace (``--chdir``). The network namespace is empty, which also
cuts off host services on loopback.

macOS uses ``sandbox-exec`` with a deterministic Seatbelt profile:
``(deny default)`` plus narrow allows; denies for ``protected`` paths are
emitted after the allows so they take precedence (Seatbelt matching is
last-match-wins). Nothing network-related is ever allowed.

The module is importable on every platform; platform-specific syscalls
run only inside ``probe()`` and ``execute()``. See
``desktop/sandbox/posix.md`` for the security model and limitations.
"""

from __future__ import annotations

import os
import shutil
import signal
import stat
import subprocess
import sys
from pathlib import Path
from typing import Iterable, Mapping, Sequence

from desktop.sandbox.core import BackendUnavailableError, PolicyError

__all__ = ["probe", "execute"]

# Backends are only ever these trusted system locations — fixed absolute
# paths whose resolved file and every ancestor directory must be root-
# owned and not writable by group/other. PATH or environment lookups are
# never consulted. Only the system install in /usr/bin qualifies: /bin is
# a symlink on usr-merged systems and /usr/local/bin is admin-writable
# but not a distribution-managed location.
_BWRAP_CANDIDATES = ("/usr/bin/bwrap",)
_SANDBOX_EXEC = "/usr/bin/sandbox-exec"
_TRUE_CANDIDATES = ("/usr/bin/true",)
_PROBE_TIMEOUT = 10.0

# The wrapper process itself is spawned with a completely empty
# environment: the sandboxed child's env then consists of exactly the
# --setenv entries. This avoids --clearenv (added in bubblewrap 0.5.0,
# missing on Ubuntu 20.04's 0.4.x) and cannot inherit anything.
_LAUNCH_ENV: dict[str, str] = {}

# Flags restricted to what bubblewrap 0.4.x (Ubuntu 20.04) already
# supports; cgroup namespaces are optional hardening and left out.
# The /etc tree is NOT bound — only a narrow whitelist of world-readable
# runtime files a dynamic tool needs (loader cache, NSS, hostname).
_LINUX_SYSTEM_DIRS = ("/usr", "/bin", "/sbin", "/lib", "/lib64")
_LINUX_ETC_FILES = ("/etc/ld.so.cache", "/etc/ld.so.conf", "/etc/ld.so.conf.d",
                    "/etc/nsswitch.conf", "/etc/passwd", "/etc/group",
                    "/etc/hosts", "/etc/localtime")
_MACOS_SYSTEM_READS = ("/System", "/usr", "/bin", "/sbin")
_MACOS_READ_LITERALS = ("/dev/null", "/dev/zero", "/dev/full",
                        "/dev/random", "/dev/urandom")
_MACOS_WRITE_LITERALS = ("/dev/null", "/dev/zero", "/dev/full")
_MASK_SUFFIX = "-posix-mask"
_MASK_FILE = "empty"
_SIGKILL = getattr(signal, "SIGKILL", 9)  # SIGKILL is always 9 on POSIX
_KILL_DRAIN = 5.0  # seconds to drain pipes after a group kill


def _posix_platform() -> str:
    if sys.platform.startswith("linux"):
        return "linux"
    if sys.platform == "darwin":
        return "darwin"
    return "unsupported"


def _path_ancestors_user_writable(path: str) -> bool:
    """True when any directory above *path* is not root-owned or is
    writable by group/other — i.e. a normal user could have planted or
    replaced the file."""
    current = os.path.dirname(path)
    while current and current != os.path.dirname(current):
        try:
            info = os.stat(current)
        except OSError:
            return True
        if info.st_uid != 0 or info.st_mode & 0o022:
            return True
        current = os.path.dirname(current)
    return False


def _trusted_tool(candidates: Iterable[str]) -> str | None:
    """First candidate resolving to a root-owned executable regular file
    whose whole ancestry is root-owned and not group/other writable."""
    for raw in candidates:
        try:
            real = os.path.realpath(raw)
            info = os.stat(real)
        except OSError:
            continue
        if not (stat.S_ISREG(info.st_mode) and info.st_uid == 0
                and os.access(real, os.X_OK)):
            continue
        if _path_ancestors_user_writable(real):
            continue
        return real
    return None


def _posix_under(path: str, root: str) -> bool:
    """True when *path* equals the directory *root* or sits below it."""
    return path == root or path.startswith(root.rstrip("/") + "/")


# ----------------------------------------------------------------------
# Policy compilers (pure, deterministic; exercised on any host).
# ----------------------------------------------------------------------

def _linux_base(uid: int, gid: int) -> list[str]:
    """The namespace flags, pseudo filesystems and read-only system
    runtime shared by execute() and the probe self-test — the probe
    verifies exactly the runtime execute() needs, nothing less."""
    args = [
        "--die-with-parent", "--new-session",
        "--unshare-user", "--uid", str(uid), "--gid", str(gid),
        "--unshare-ipc", "--unshare-pid", "--unshare-net", "--unshare-uts",
        "--dev", "/dev", "--proc", "/proc", "--tmpfs", "/tmp",
    ]
    for directory in _LINUX_SYSTEM_DIRS:
        args += ["--ro-bind-try", directory, directory]
    for item in _LINUX_ETC_FILES:
        args += ["--ro-bind-try", item, item]
    return args


def _mask_root(scratch: str) -> str:
    """Host-side mask directory, a sibling of scratch.

    It must NOT live inside scratch: scratch is bound writable, so a
    mask source under it would be guest-writable and a masked directory
    could be repopulated by the child. A sibling is bound into the
    sandbox only through the read-only mask binds themselves, so the
    guest cannot reach or modify it.
    """
    return scratch.rstrip("/") + _MASK_SUFFIX


def _linux_args(
    uid: int,
    gid: int,
    workspace: str,
    argv: Sequence[str],
    *,
    writable: bool,
    protected: Iterable[tuple[str, bool]],
    readable: Iterable[str],
    scratch: str,
    env: Mapping[str, str],
) -> list[str]:
    """Compile one bwrap argument vector; deterministic for fixed inputs.

    *protected* items are ``(path, is_dir)`` pairs. Mount order matters:
    the pseudo filesystems first, then the workspace, grants and scratch,
    then the protected masks so they sit on top of the workspace bind.
    """
    argv = tuple(argv)
    args = _linux_base(uid, gid)
    args += ["--bind" if writable else "--ro-bind", workspace, workspace]
    grants = sorted(dict.fromkeys(readable))
    for grant in grants:
        args += ["--ro-bind", grant, grant]
    args += ["--bind", scratch, scratch]

    # Masks come after the workspace bind. A protected directory is
    # covered by the host-side empty mask directory, a protected file by
    # the host-side empty regular file — both read-only binds whose
    # source lives outside every writable mount, so entries can be
    # neither read nor written. A path already covered by a masked
    # parent directory needs no second mount (and would fail, because
    # its destination no longer exists inside the mask).
    mask = _mask_root(scratch)
    mask_file = mask + "/" + _MASK_FILE
    masked_dirs: list[str] = []
    for path, is_dir in sorted(dict.fromkeys(protected)):
        if any(_posix_under(path, parent) for parent in masked_dirs):
            continue
        if is_dir:
            args += ["--ro-bind", mask, path]
            masked_dirs.append(path)
        else:
            args += ["--ro-bind", mask_file, path]

    # The tool must exist inside the sandbox; when it does not live under
    # an already-bound tree it gets its own read-only bind (best effort —
    # tools needing sibling resources require a `readable` grant).
    tool = argv[0]
    covered = (*_LINUX_SYSTEM_DIRS, workspace, scratch, *grants)
    if not any(_posix_under(tool, root) for root in covered):
        args += ["--ro-bind", tool, tool]

    args += ["--chdir", workspace]
    for name in sorted(env):
        args += ["--setenv", name, env[name]]
    return [*args, "--", *argv]


def _seatbelt_quote(path: str) -> str:
    return '"' + path.replace("\\", "\\\\").replace('"', '\\"') + '"'


def _seatbelt_profile(
    workspace: str,
    argv: Sequence[str],
    *,
    writable: bool,
    protected: Iterable[str],
    readable: Iterable[str],
    scratch: str,
) -> str:
    """Compile a deterministic Seatbelt profile.

    ``deny default`` blocks every operation not explicitly allowed —
    including all network operations (loopback included). Denies for
    protected paths are emitted last because the last matching rule wins.
    """
    lines = [
        "(version 1)",
        "(deny default)",
        "(deny network*)",
        "(allow process-fork)",  # forked children inherit the profile
        "(allow process-exec)",
        "(allow signal (target self))",
    ]
    reads = sorted(set(_MACOS_SYSTEM_READS) | {workspace, scratch} | set(readable))
    lines.append("(allow file-read* file-map-executable "
                 + " ".join(f"(subpath {_seatbelt_quote(path)})" for path in reads) + ")")
    lines.append(f"(allow file-read* file-map-executable (literal {_seatbelt_quote(argv[0])}))")
    lines.append("(allow file-read* "
                 + " ".join(f"(literal {_seatbelt_quote(path)})"
                            for path in _MACOS_READ_LITERALS) + ")")
    writes = [scratch, workspace] if writable else [scratch]
    lines.append("(allow file-write* "
                 + " ".join(f"(subpath {_seatbelt_quote(path)})" for path in writes) + ")")
    lines.append("(allow file-write* "
                 + " ".join(f"(literal {_seatbelt_quote(path)})"
                            for path in _MACOS_WRITE_LITERALS) + ")")
    for path in sorted(dict.fromkeys(protected)):
        lines.append("(deny file-read* file-map-executable file-write* "
                     f"(subpath {_seatbelt_quote(path)}))")
    return "\n".join(lines) + "\n"


# ----------------------------------------------------------------------
# probe() — verifies the trusted tool AND the kernel features it needs,
# by actually launching a trivial sandboxed command with the same
# namespace flags and system runtime execute() uses.
# ----------------------------------------------------------------------

def _probe_linux() -> tuple[bool, str]:
    bwrap = _trusted_tool(_BWRAP_CANDIDATES)
    if bwrap is None:
        return False, ("no trusted bubblewrap at "
                       + ", ".join(_BWRAP_CANDIDATES))
    true = _trusted_tool(_TRUE_CANDIDATES)
    if true is None:
        return False, "no trusted true(1) binary for the self-test"
    version = "bubblewrap"
    try:
        out = subprocess.run([bwrap, "--version"], capture_output=True,
                             text=True, timeout=_PROBE_TIMEOUT, env=_LAUNCH_ENV)
        if out.stdout.strip():
            version = out.stdout.strip()
    except (OSError, subprocess.TimeoutExpired):
        pass
    # Same flags and runtime binds as execute() — including the dynamic
    # loader under /lib*, without which a dynamically linked `true`
    # cannot run — plus the same --chdir cwd policy.
    args = _linux_base(os.getuid(), os.getgid()) + ["--chdir", "/", "--", true]
    try:
        result = subprocess.run(
            [bwrap, *args],
            capture_output=True, timeout=_PROBE_TIMEOUT, env=_LAUNCH_ENV)
    except (OSError, subprocess.TimeoutExpired) as error:
        return False, f"bwrap self-test could not run: {error}"
    if result.returncode != 0:
        detail = result.stderr.decode("utf-8", "replace").strip()
        return False, f"bwrap self-test failed (exit {result.returncode}): {detail}"
    return True, f"{bwrap} ({version}); namespace self-test passed"


def _probe_darwin() -> tuple[bool, str]:
    exe = _trusted_tool((_SANDBOX_EXEC,))
    if exe is None:
        return False, f"no trusted sandbox-exec at {_SANDBOX_EXEC}"
    profile = ("(version 1)(deny default)(allow process-fork)(allow process-exec)"
               "(allow file-read* file-map-executable "
               "(subpath \"/usr/bin\")(subpath \"/bin\"))")
    try:
        result = subprocess.run(
            [exe, "-p", profile, "/usr/bin/true"],
            capture_output=True, timeout=_PROBE_TIMEOUT, env=_LAUNCH_ENV)
    except (OSError, subprocess.TimeoutExpired) as error:
        return False, f"sandbox-exec self-test could not run: {error}"
    if result.returncode != 0:
        detail = result.stderr.decode("utf-8", "replace").strip()
        return False, f"sandbox-exec self-test failed (exit {result.returncode}): {detail}"
    return True, f"{exe}; Seatbelt self-test passed"


def probe() -> tuple[bool, str]:
    """Whether this host's POSIX backend can actually run; never raises."""
    platform = _posix_platform()
    try:
        if platform == "linux":
            return _probe_linux()
        if platform == "darwin":
            return _probe_darwin()
    except Exception as error:  # a probe reports; it must not crash
        return False, f"{platform} probe failed: {error}"
    return False, f"no POSIX sandbox backend for sys.platform={sys.platform!r}"


# ----------------------------------------------------------------------
# execute() — launch helpers; no fallback path exists.
# ----------------------------------------------------------------------

def _launch_linux(workspace: Path, argv: tuple[str, ...], *, writable: bool,
                  protected: tuple[Path, ...], readable: tuple[Path, ...],
                  scratch: Path, env: dict[str, str], timeout: float):
    bwrap = _trusted_tool(_BWRAP_CANDIDATES)
    if bwrap is None:
        raise BackendUnavailableError(
            "bubblewrap not available at trusted paths " + ", ".join(_BWRAP_CANDIDATES))
    specs = tuple((str(path), path.is_dir()) for path in protected)
    mask_host = Path(_mask_root(str(scratch)))
    created = False
    try:
        if specs:
            mask_host.mkdir()   # fails on a pre-existing path — never reused
            created = True
            (mask_host / _MASK_FILE).touch()
        args = _linux_args(os.getuid(), os.getgid(), str(workspace), argv,
                           writable=writable, protected=specs,
                           readable=[str(path) for path in readable],
                           scratch=str(scratch), env=env)
        # stdin is never inherited and close_fds (default) keeps every
        # other host descriptor out; the child gets only --setenv entries.
        result = subprocess.run(
            [bwrap, *args], stdin=subprocess.DEVNULL, capture_output=True,
            timeout=timeout, env=_LAUNCH_ENV)
        return subprocess.CompletedProcess(argv, result.returncode,
                                           result.stdout, result.stderr)
    finally:
        # remove only the mask root this call created: a failed mkdir
        # must not sweep a pre-existing directory, and a removal error
        # other than "already gone" is surfaced, not swallowed
        if created:
            try:
                shutil.rmtree(mask_host)
            except FileNotFoundError:
                pass


def _kill_group(proc: "subprocess.Popen[bytes]") -> None:
    """Best-effort kill of the tool's process group on timeout.

    Descendants that escaped the group (own session) may survive; they
    stay inside the Seatbelt profile so this is a resource issue, not a
    containment one — see posix.md.
    """
    try:
        os.killpg(proc.pid, _SIGKILL)
    except (AttributeError, ProcessLookupError, PermissionError, OSError):
        try:
            proc.kill()
        except OSError:
            pass


def _launch_darwin(workspace: Path, argv: tuple[str, ...], *, writable: bool,
                   protected: tuple[Path, ...], readable: tuple[Path, ...],
                   scratch: Path, env: dict[str, str], timeout: float):
    exe = _trusted_tool((_SANDBOX_EXEC,))
    if exe is None:
        raise BackendUnavailableError(f"no trusted sandbox-exec at {_SANDBOX_EXEC}")
    profile = _seatbelt_profile(str(workspace), argv, writable=writable,
                                protected=[str(path) for path in protected],
                                readable=[str(path) for path in readable],
                                scratch=str(scratch))
    proc = subprocess.Popen(
        [exe, "-p", profile, *argv], stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        env=dict(env), cwd=str(workspace), start_new_session=True)
    try:
        stdout, stderr = proc.communicate(timeout=timeout)
    except subprocess.TimeoutExpired as first:
        _kill_group(proc)
        try:
            stdout, stderr = proc.communicate(timeout=_KILL_DRAIN)
        except subprocess.TimeoutExpired as second:
            # a descendant that escaped the group can hold the pipes open
            # forever; report what was captured instead of hanging.
            proc.kill()
            stdout = second.output or first.output or b""
            stderr = second.stderr or first.stderr or b""
            try:
                proc.wait(timeout=_KILL_DRAIN)
            except subprocess.TimeoutExpired:
                pass
        raise subprocess.TimeoutExpired(list(argv), timeout,
                                        output=stdout, stderr=stderr)
    return subprocess.CompletedProcess(argv, proc.returncode, stdout, stderr)


def execute(
    workspace: Path,
    argv: tuple[str, ...],
    *,
    writable: bool,
    protected: tuple[Path, ...],
    readable: tuple[Path, ...],
    scratch: Path,
    env: dict[str, str],
    timeout: float,
) -> subprocess.CompletedProcess[bytes]:
    """Run ``argv`` inside this platform's sandbox backend.

    ``core.run`` already validated the policy. A tool hidden by a
    ``protected`` mask could never start, so that combination is rejected
    here instead of producing a cryptic launch failure.
    """
    tool = str(argv[0])
    if any(_posix_under(tool, str(path)) for path in protected):
        raise PolicyError(f"the tool must not sit under a protected path: {tool}")
    platform = _posix_platform()
    if platform == "unsupported":
        raise BackendUnavailableError(
            f"no POSIX sandbox backend for sys.platform={sys.platform!r}")
    if platform == "linux":
        return _launch_linux(workspace, argv, writable=writable, protected=protected,
                             readable=readable, scratch=scratch, env=env, timeout=timeout)
    return _launch_darwin(workspace, argv, writable=writable, protected=protected,
                          readable=readable, scratch=scratch, env=env, timeout=timeout)
