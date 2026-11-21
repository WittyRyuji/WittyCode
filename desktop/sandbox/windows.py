"""Windows 11 backend for desktop.sandbox: Microsoft MXC ProcessContainer.

Runs one command inside Microsoft's official MXC sandbox
(github.com/microsoft/mxc) through the signed/verified Node SDK
``@microsoft/mxc-sdk`` (typed V1 API) and the companion ``mxc.mts`` helper,
which must run on a trusted Node >= 24 interpreter.

Dependency resolution is explicit and never consults PATH or the workspace:

* ``WITTYCODE_MXC_NODE`` — absolute path to ``node.exe`` (>= 24, x64 Windows).
* ``WITTYCODE_MXC_SDK``  — directory containing
  ``node_modules/@microsoft/mxc-sdk`` (an integrity-verified extract of the
  official npm tarball; npm install scripts are never run).
* fixed host install roots, searched in order:
  ``%ProgramData%\\WittyCode\\mxc\\nodejs\\node.exe`` with sibling ``sdk\\``,
  then ``%LOCALAPPDATA%\\WittyCode\\mxc\\...``.

Containment is ``processcontainer``: filesystem grants are layered as
workspace (read-write or read-only) + scratch (read-write) + ``readable``
(read-only) + ``protected`` (denied), networking is denied in both directions
including host loopback, UI is disabled, and the caller's environment map is
applied verbatim. There is no fallback: when resolution or the native probe
fails, :func:`execute` refuses to launch anything.
"""

from __future__ import annotations

import base64
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
from pathlib import Path

__all__ = ["probe", "execute", "validate_runtime", "NODE_ENV", "SDK_ENV", "HELPER_NAME", "resolve_backend"]

NODE_ENV = "WITTYCODE_MXC_NODE"
SDK_ENV = "WITTYCODE_MXC_SDK"
HELPER_NAME = "mxc.mts"

_OUTPUT_CAP = 4 * 1024 * 1024  # bytes kept per stream; the rest is drained
_HELPER_PROBE_BUDGET = 30.0    # seconds the native probe may take
_KILL_GRACE = 15.0             # parent-side grace added to the workload timeout
_TIMEOUT_EXIT = 124


def _fixed_candidates() -> list[tuple[Path, Path]]:
    """``(node.exe, sdk dir)`` pairs under the documented install roots."""
    pairs: list[tuple[Path, Path]] = []
    for var in ("ProgramData", "LOCALAPPDATA"):
        base = os.environ.get(var)
        if base:
            root = Path(base) / "WittyCode" / "mxc"
            pairs.append((root / "nodejs" / "node.exe", root / "sdk"))
    return pairs


def _resolve_absolute(candidate: Path) -> Path | None:
    """Absolute, strictly resolved trusted path, or ``None`` for anything else."""
    if not candidate.is_absolute():
        return None
    try:
        return candidate.resolve(strict=True)
    except OSError:
        return None


def _resolve_node() -> Path | None:
    explicit = os.environ.get(NODE_ENV)
    candidates = [Path(explicit)] if explicit else [node for node, _ in _fixed_candidates()]
    for candidate in candidates:
        resolved = _resolve_absolute(candidate)
        if resolved is not None and resolved.is_file():
            return resolved
    return None


def _resolve_sdk() -> Path | None:
    def usable(root: Path) -> bool:
        return (root / "node_modules" / "@microsoft" / "mxc-sdk" / "package.json").is_file()

    explicit = os.environ.get(SDK_ENV)
    candidates = [Path(explicit)] if explicit else [sdk for _, sdk in _fixed_candidates()]
    for candidate in candidates:
        resolved = _resolve_absolute(candidate)
        if resolved is not None and usable(resolved):
            return resolved
    return None


def resolve_backend() -> tuple[Path, Path] | None:
    """The trusted ``(node.exe, sdk layout dir)`` pair, or ``None``."""
    node = _resolve_node()
    sdk = _resolve_sdk()
    if node is None or sdk is None:
        return None
    return node, sdk


def _helper() -> Path:
    return Path(__file__).resolve().with_name(HELPER_NAME)


def _helper_env(scratch: Path) -> dict[str, str]:
    """Minimal host-side environment for the trusted helper process."""
    env: dict[str, str] = {}
    for name in ("SystemRoot", "windir", "SystemDrive", "ComSpec", "PATHEXT"):
        value = os.environ.get(name)
        if value:
            env[name] = value
    system_root = env.get("SystemRoot") or r"C:\Windows"
    env["PATH"] = str(Path(system_root) / "System32")
    env["TEMP"] = env["TMP"] = str(scratch)
    return env


def _collect(stream, sink: list[bytearray]) -> None:
    buf = bytearray()
    while chunk := stream.read(65536):
        if len(buf) < _OUTPUT_CAP:
            buf += chunk[: _OUTPUT_CAP - len(buf)]
    sink.append(buf)


def _spawn_helper(node: Path, host_dir: Path) -> subprocess.Popen:
    # cwd and TEMP stay on a host-private directory — never a guest grant.
    return subprocess.Popen(
        [str(node), "--no-warnings", str(_helper())],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        cwd=str(host_dir),
        env=_helper_env(host_dir),
    )


def _run_helper(node: Path, sdk: Path, payload: dict, scratch: Path, deadline: float) -> tuple[int, bytes, bytes]:
    """Drive ``mxc.mts``; on *deadline* seconds kill its tree and return what we have."""
    host_dir = Path(tempfile.mkdtemp(prefix=".mxc-host-", dir=str(scratch.parent)))
    try:
        proc = _spawn_helper(node, host_dir)
    except OSError:
        shutil.rmtree(host_dir, ignore_errors=True)
        raise
    out_parts: list[bytearray] = []
    err_parts: list[bytearray] = []
    threads = [
        threading.Thread(target=_collect, args=(proc.stdout, out_parts), daemon=True),
        threading.Thread(target=_collect, args=(proc.stderr, err_parts), daemon=True),
    ]
    for thread in threads:
        thread.start()
    timed_out = False
    try:
        assert proc.stdin is not None
        proc.stdin.write(json.dumps(payload).encode("utf-8"))
        proc.stdin.close()
        proc.wait(timeout=deadline)
    except subprocess.TimeoutExpired:
        timed_out = True
        taskkill = Path(os.environ.get("SystemRoot") or r"C:\Windows") / "System32" / "taskkill.exe"
        subprocess.run([str(taskkill), "/PID", str(proc.pid), "/T", "/F"],
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        proc.wait()
    for thread in threads:
        thread.join(timeout=10)
    proc.stdout.close()
    proc.stderr.close()
    shutil.rmtree(host_dir, ignore_errors=True)
    code = _TIMEOUT_EXIT if timed_out else proc.returncode
    return code, bytes(out_parts[0]) if out_parts else b"", bytes(err_parts[0]) if err_parts else b""


def _timeout_expired(argv: tuple[str, ...], timeout: float, out: bytes, err: bytes) -> subprocess.TimeoutExpired:
    error = subprocess.TimeoutExpired(cmd=list(argv), timeout=timeout)
    error.output = out
    error.stderr = err
    return error


def _ancestors(path: Path) -> list[Path]:
    out: list[Path] = []
    for parent in path.parents:
        if parent == parent.parent or parent == Path(parent.anchor):
            out.append(parent)
            break
        out.append(parent)
    return out


def _request(command: str, *, workspace: Path, writable: bool, protected: tuple[Path, ...],
             readable: tuple[Path, ...], scratch: Path, env: dict[str, str],
             timeout: float | None) -> dict:
    readwrite = [str(scratch)]
    readonly = [str(p) for p in readable]
    if writable:
        readwrite.append(str(workspace))
    else:
        readonly.append(str(workspace))
    granted = [Path(p) for p in (*readwrite, *readonly)]
    enumerate_paths = sorted({str(a) for p in granted for a in _ancestors(p)})
    request: dict = {
        "command": command,
        "containment": {"type": "processcontainer", "config": {
            "filesystem": {"enumeratePaths": enumerate_paths}}},
        "workingDirectory": str(workspace),
        # Windows requires LOCALAPPDATA to exist in a verbatim environment;
        # point it at the private scratch so no profile path leaks in.
        "environment": {"LOCALAPPDATA": str(scratch), **env},
        "inheritDefaultEnvironment": False,
        "filesystem": {
            "readwritePaths": readwrite,
            "readonlyPaths": readonly,
            "deniedPaths": [str(p) for p in protected],
            "clearPolicyOnExit": True,
        },
        "network": {
            "egress": {"default": "deny"},
            "ingress": {"default": "deny", "hostLoopback": "deny"},
        },
        "ui": {"disable": True, "clipboard": "none", "allowInputInjection": False},
    }
    if timeout is not None:
        request["timeoutMs"] = max(1, min(int(timeout * 1000), 0xFFFFFFFF))
    return request


def probe() -> tuple[bool, str]:
    """Whether Node+SDK resolve and the native ProcessContainer can serve us."""
    if os.name != "nt":
        return False, f"the Windows MXC backend cannot run on os.name={os.name!r}"
    if sys.getwindowsversion().build < 22000:
        return False, f"Windows 11 required; build {sys.getwindowsversion().build}"
    if not _helper().is_file():
        return False, f"helper missing: {_helper()}"
    resolved = resolve_backend()
    if resolved is None:
        return False, (f"MXC dependencies not resolved; set {NODE_ENV} to a Node >= 24 "
                       f"node.exe and {SDK_ENV} to a verified @microsoft/mxc-sdk layout")
    node, sdk = resolved
    scratch = Path(os.environ.get("TEMP") or os.environ.get("TMP") or ".")
    try:
        version = subprocess.run([str(node), "--version"], capture_output=True, timeout=10,
                                env=_helper_env(scratch))
    except OSError as error:
        return False, f"cannot run {NODE_ENV}={node}: {error}"
    if version.returncode != 0:
        return False, f"{node} is not a working node.exe"
    tag = version.stdout.strip().decode("ascii", "replace").lstrip("v")
    try:
        major, minor = (int(part) for part in tag.split(".")[:2])
    except ValueError:
        major, minor = 0, 0
    if (major, minor) < (24, 21):
        return False, f"{node} is {tag}; the SDK requires Node >= 24.21 (or 26.8+) for native stdio"
    payload = {"action": "probe", "sdkDir": str(sdk), "request": {
        "command": "cmd.exe /d /c exit 0", "containment": {"type": "processcontainer"}}}
    try:
        code, out, err = _run_helper(node, sdk, payload, scratch, _HELPER_PROBE_BUDGET)
    except OSError as error:
        return False, f"cannot launch MXC helper: {error}"
    if code != 0:
        return False, f"MXC probe failed (exit {code}): {(err or out)[:500]!r}"
    try:
        report = json.loads(out.decode("utf-8", "replace").strip().splitlines()[-1])
    except (ValueError, IndexError) as error:
        return False, f"unparseable MXC probe output: {out[:300]!r} ({error})"
    detail = json.dumps(report.get("detail"), ensure_ascii=False)
    return (True, f"MXC processcontainer ready {detail}") if report.get("ok") \
        else (False, f"MXC processcontainer unusable {detail}")


def validate_runtime(workspace: Path | str, scratch: Path | str | None = None) -> tuple[Path, Path]:
    """Refuse to run when trusted runtime paths are unresolved or overlap grants.

    Both directions of containment are checked against every trusted path: a
    trusted file/dir inside a guest grant, or a guest grant inside a trusted
    directory (for example the workspace placed under the SDK layout dir).
    Returns the resolved ``(node, sdk)`` pair on success.
    """
    resolved = resolve_backend()
    if resolved is None:
        raise FileNotFoundError(
            f"MXC backend not resolved ({NODE_ENV}/{SDK_ENV} unset or invalid); refusing to launch")
    node, sdk = resolved
    guests = [Path(workspace).resolve()]
    if scratch is not None:
        guests.append(Path(scratch).resolve())
    for name, path in {"node": node, "sdk": sdk, "helper": _helper().resolve()}.items():
        for guest in guests:
            if path.is_relative_to(guest) or guest.is_relative_to(path):
                raise RuntimeError(
                    f"trusted {name} and guest grants must not overlap: {path} vs {guest}")
    return node, sdk


def execute(workspace: Path, argv: tuple[str, ...], *, writable: bool,
            protected: tuple[Path, ...], readable: tuple[Path, ...],
            scratch: Path, env: dict[str, str], timeout: float) -> subprocess.CompletedProcess[bytes]:
    """Run ``argv`` inside an MXC ProcessContainer; never falls back."""
    node, sdk = validate_runtime(workspace, scratch)
    workspace, scratch = workspace.resolve(), scratch.resolve()
    request = _request(
        subprocess.list2cmdline(list(argv)), workspace=workspace, writable=writable,
        protected=protected, readable=readable, scratch=scratch, env=env, timeout=timeout)
    payload = {"action": "run", "sdkDir": str(sdk), "request": request}
    code, out, err = _run_helper(node, sdk, payload, scratch, timeout + _KILL_GRACE)
    if code == _TIMEOUT_EXIT:
        raise _timeout_expired(argv, timeout, out, err)
    if code != 0 or b"MXC_HELPER_ERROR" in err:
        raise RuntimeError(f"MXC helper failed (exit {code}): {err[:500]!r}")
    try:
        result = json.loads(out.decode("ascii", "strict").strip().splitlines()[-1])
    except (ValueError, IndexError) as error:
        raise RuntimeError(f"unparseable MXC helper frame: {out[:300]!r} ({error})") from error
    if not isinstance(result, dict) or "refused" not in result:
        raise RuntimeError(f"missing MXC helper frame: {out[:300]!r}")
    if result["refused"]:
        raise RuntimeError(f"native probe refused the request: {result.get('probeError')}")
    try:
        stdout = base64.b64decode(result.get("stdoutB64") or "", validate=True)
        stderr = base64.b64decode(result.get("stderrB64") or "", validate=True)
    except ValueError as error:
        raise RuntimeError(f"corrupt MXC helper streams ({error})") from error
    for warning in result.get("warnings") or []:
        stderr += f"\nmxc warning: {warning}".encode("utf-8", "replace")
    if result.get("timedOut"):
        raise _timeout_expired(argv, timeout, stdout, stderr)
    exit_code = result.get("exitCode")
    return subprocess.CompletedProcess(list(argv), int(exit_code) if exit_code is not None else 1,
                                       stdout, stderr)
