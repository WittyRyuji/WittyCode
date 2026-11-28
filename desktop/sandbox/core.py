"""Shared policy layer for the standalone OS sandbox.

This module selects the platform backend, validates every policy argument and
owns the per-invocation scratch directory. It never falls back to a plain
subprocess: unsupported platforms, failed probes and launch failures raise
explicit errors instead.

Fixed contract with the platform backends ``desktop.sandbox.windows`` and
``desktop.sandbox.posix`` (each lazy-imported only when needed):

* ``probe() -> tuple[bool, str]`` — whether the backend and its native
  dependencies are usable on this machine.
* ``execute(workspace, argv, *, writable, protected, readable, scratch, env,
  timeout) -> subprocess.CompletedProcess[bytes]`` — run one command.

The network is always off inside the sandbox; no flag or environment
inheritance can change that.
"""

from __future__ import annotations

import importlib
import math
import os
import re
import shutil
import stat
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path
from types import ModuleType

__all__ = [
    "Policy",
    "SandboxError",
    "PolicyError",
    "BackendUnavailableError",
    "LaunchError",
    "backend_module_name",
    "load_backend",
    "probe",
    "validate_policy",
    "build_env",
    "run",
    "SCRATCH_PREFIX",
    "TEST_WORK",
]

SCRATCH_PREFIX = "wittycode-sandbox-"
TEST_WORK = "WITTYCODE_TEST_WORK"

_ENV_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_ENV_DENIED = re.compile(
    r"TOKEN|SECRET|PASSWORD|PASSWD|API_?KEY|ACCESS_?KEY|PRIVATE_?KEY|CREDENTIAL|AUTH"
    r"|_PROXY$|^PYTHON|^VIRTUAL_ENV|^CONDA_|^LD_|^DYLD_|^BASH_ENV$",
    re.IGNORECASE,
)
_ENV_RESERVED = frozenset({"PATH", "PATHEXT", "SYSTEMROOT", "WINDIR", "COMSPEC", "TEMP", "TMP", "TMPDIR"})

# Deny lists are split by how a grant may relate to them:
#   sensitive — no overlap in either direction (credential and core system dirs)
#   install   — the listed root itself and anything containing it is denied,
#               but a specific tool directory inside it may be granted read-only
#   broad     — shared roots that must not be granted or used as the workspace,
#               while ordinary private subdirectories below them stay allowed
_SENSITIVE_VARS = ("SystemRoot", "windir")
_INSTALL_VARS = ("ProgramFiles", "ProgramFiles(x86)", "CommonProgramFiles", "CommonProgramFiles(x86)",
                 "ProgramData", "PUBLIC")
_BROAD_VARS = ("APPDATA", "LOCALAPPDATA")
_CREDENTIAL_DIRS = (".ssh", ".gnupg", ".aws", ".azure", ".kube", ".docker", ".config", ".password-store")
# /private itself is not denied: on macOS the real per-user temp directories
# resolve below /private/var and are legitimate workspaces. The real system
# roots (/etc -> /private/etc, /var -> /private/var, ...) stay covered because
# every POSIX entry is resolved before comparison.
_POSIX_SENSITIVE = ("/usr", "/etc", "/bin", "/sbin", "/lib", "/lib64", "/boot", "/dev", "/proc", "/sys",
                    "/System", "/Library", "/root", "/Network", "/cores")
_POSIX_BROAD = ("/home", "/Users", "/var", "/opt", "/tmp", "/mnt", "/media", "/srv")

_MODULE_FILE = Path(__file__).resolve()


class SandboxError(Exception):
    """Base class for every failure reported by this module."""


class PolicyError(SandboxError):
    """A caller-supplied argument violates the sandbox policy."""


class BackendUnavailableError(SandboxError):
    """The platform backend is missing, incomplete or refused to probe."""


class LaunchError(SandboxError):
    """The backend could not start or complete the sandboxed child."""


@dataclass(frozen=True)
class Policy:
    """One validated run request; every path is resolved and canonical."""

    workspace: Path
    argv: tuple[str, ...]
    writable: bool
    protected: tuple[Path, ...]
    readable: tuple[Path, ...]
    timeout: float


def _inside(path: Path, root: Path) -> bool:
    """True when *path* is strictly below *root*."""
    return path != root and path.is_relative_to(root)


def _overlap(a: Path, b: Path) -> bool:
    return a == b or _inside(a, b) or _inside(b, a)


def _canon(path: Path | str, what: str, *, directory: bool | None) -> Path:
    raw = str(path)
    if not raw or "\0" in raw:
        raise PolicyError(f"{what} must be a non-empty path without NUL: {raw!r}")
    try:
        resolved = Path(path).resolve(strict=True)
    except (OSError, RuntimeError, ValueError) as error:
        raise PolicyError(f"{what} does not exist or cannot be resolved: {path}") from error
    if directory is True and not resolved.is_dir():
        raise PolicyError(f"{what} is not a directory: {resolved}")
    if directory is False and not resolved.is_file():
        raise PolicyError(f"{what} is not a file: {resolved}")
    if directory is None and not stat.S_ISREG(resolved.lstat().st_mode) and not resolved.is_dir():
        raise PolicyError(f"{what} is not a regular file or directory: {resolved}")
    return resolved


def _is_root(path: Path) -> bool:
    return path == Path(path.anchor)


def _deny_lists() -> tuple[set[Path], set[Path], set[Path]]:
    def envdir(name: str) -> Path | None:
        value = os.environ.get(name)
        return Path(value).resolve() if value else None

    sensitive: set[Path] = set()
    install: set[Path] = set()
    broad: set[Path] = set()

    home = Path.home().resolve()
    broad.add(home)
    sensitive.update(home / name for name in _CREDENTIAL_DIRS)

    for names, target in ((_SENSITIVE_VARS, sensitive), (_INSTALL_VARS, install), (_BROAD_VARS, broad)):
        target.update(root for name in names if (root := envdir(name)) is not None)
    drive = os.environ.get("SystemDrive")
    if drive:
        broad.add((Path(drive + "\\") / "Users").resolve())

    if os.name == "posix":
        sensitive.update(Path(p).resolve() for p in _POSIX_SENSITIVE)
        broad.update(Path(p).resolve() for p in _POSIX_BROAD)
    return sensitive, install, broad


def _check_workspace(path: Path | str) -> Path:
    workspace = _canon(path, "workspace", directory=True)
    if _is_root(workspace):
        raise PolicyError(f"workspace must not be a filesystem root: {workspace}")
    sensitive, install, broad = _deny_lists()
    for root in sensitive | install:
        if _overlap(workspace, root):
            raise PolicyError(f"workspace overlaps a protected system location: {workspace}")
    for root in broad:
        if workspace == root or _inside(root, workspace):
            raise PolicyError(f"workspace must be a private directory, not {root} or broader: {workspace}")
    return workspace


def _check_readable(path: Path | str, workspace: Path, granted: list[Path]) -> Path:
    grant = _canon(path, "readable grant", directory=None)
    if _is_root(grant):
        raise PolicyError(f"readable grant must not be a filesystem root: {grant}")
    sensitive, install, broad = _deny_lists()
    for root in sensitive:
        if _overlap(grant, root):
            raise PolicyError(f"readable grant overlaps a protected location: {grant}")
    for root in install | broad:
        if grant == root or _inside(root, grant):
            raise PolicyError(f"readable grant is too broad: {grant}")
    if _overlap(grant, workspace):
        raise PolicyError(f"readable grant overlaps the workspace: {grant}")
    for other in granted:
        if _overlap(grant, other):
            raise PolicyError(f"readable grants must not overlap: {grant} vs {other}")
    return grant


def _check_protected(path: Path | str, workspace: Path) -> Path:
    protected = _canon(path, "protected path", directory=None)
    if not _inside(protected, workspace):
        raise PolicyError(f"protected path must exist inside the workspace: {protected}")
    return protected


def _check_argv(argv: tuple[str, ...] | list[str]) -> tuple[str, ...]:
    items = tuple(str(arg) for arg in argv)
    if not items:
        raise PolicyError("argv must not be empty")
    for item in items:
        if not item or "\0" in item:
            raise PolicyError(f"argv items must be non-empty and free of NUL: {item!r}")
    tool = Path(items[0])
    if not tool.is_absolute():
        raise PolicyError(f"the tool must be an absolute path (use --tool): {items[0]}")
    return (str(_canon(tool, "tool", directory=False)), *items[1:])


def _check_timeout(timeout: float) -> float:
    try:
        value = float(timeout)
    except (TypeError, ValueError) as error:
        raise PolicyError(f"timeout must be a number of seconds: {timeout!r}") from error
    if not math.isfinite(value) or value <= 0:
        raise PolicyError(f"timeout must be positive and finite: {timeout!r}")
    return value


def validate_policy(
    *,
    workspace: Path | str,
    argv: tuple[str, ...] | list[str],
    writable: bool = False,
    protected: tuple[Path | str, ...] | list[Path | str] = (),
    readable: tuple[Path | str, ...] | list[Path | str] = (),
    timeout: float = 60.0,
) -> Policy:
    """Validate one run request; raises PolicyError on the first violation."""
    if not isinstance(writable, bool):
        raise PolicyError(f"writable must be a bool, got {type(writable).__name__}")
    ws = _check_workspace(workspace)
    args = _check_argv(argv)
    if writable and _inside(_MODULE_FILE, ws):
        raise PolicyError("a workspace containing this module must not be writable")
    if writable and _inside(Path(args[0]), ws):
        raise PolicyError(f"the tool must not live inside a writable workspace: {args[0]}")
    protected_paths = tuple(dict.fromkeys(_check_protected(p, ws) for p in protected))
    tool_path = Path(args[0])
    if any(_overlap(tool_path, protected) for protected in protected_paths):
        raise PolicyError(f"the tool must not sit under a protected path: {args[0]}")
    readable_paths: list[Path] = []
    for entry in readable:
        readable_paths.append(_check_readable(entry, ws, readable_paths))
    return Policy(
        workspace=ws,
        argv=args,
        writable=writable,
        protected=protected_paths,
        readable=tuple(readable_paths),
        timeout=_check_timeout(timeout),
    )


def backend_module_name() -> str:
    """The backend module for this platform; raises when none can exist."""
    if os.name == "nt":
        return "desktop.sandbox.windows"
    if os.name == "posix":
        return "desktop.sandbox.posix"
    raise BackendUnavailableError(f"No sandbox backend exists for os.name={os.name!r}.")


def load_backend(name: str | None = None) -> ModuleType:
    """Import the platform backend and check it honours the fixed contract."""
    name = name or backend_module_name()
    try:
        module = importlib.import_module(name)
    except ImportError as error:
        raise BackendUnavailableError(f"Cannot import sandbox backend {name!r}: {error}") from error
    for attr in ("probe", "execute"):
        if not callable(getattr(module, attr, None)):
            raise BackendUnavailableError(f"Sandbox backend {name!r} does not provide {attr}().")
    return module


def probe(*, backend: ModuleType | None = None) -> tuple[bool, str]:
    """Ask the platform backend whether it can run; never raises."""
    try:
        selected = backend if backend is not None else load_backend()
        ok, detail = selected.probe()
        return bool(ok), str(detail)
    except Exception as error:  # a probe is a report, not a crash
        return False, str(error)


def build_env(scratch: Path, overrides: dict[str, str] | None = None) -> dict[str, str]:
    """A clean minimal environment: nothing is inherited from the host.

    TEMP/TMP point at the per-run scratch; PATH is fixed to the ordinary
    system tool directories only — never the writable scratch and never a
    host toolchain — so bare names resolve predictably. Caller overrides must
    be ordinary names: reserved names and credential/proxy/host-interpreter
    variables are rejected.
    """
    env: dict[str, str] = {}
    if os.name == "nt":
        system_root = os.environ.get("SystemRoot") or r"C:\Windows"
        env.update({
            "PATH": str(Path(system_root) / "System32"),
            "SystemRoot": system_root,
            "windir": system_root,
            "ComSpec": os.environ.get("ComSpec") or str(Path(system_root) / "System32" / "cmd.exe"),
            "PATHEXT": ".COM;.EXE;.BAT;.CMD",
            "TEMP": str(scratch),
            "TMP": str(scratch),
        })
    else:
        env.update({"PATH": "/usr/bin:/bin", "TMPDIR": str(scratch)})
    for name, value in (overrides or {}).items():
        if not _ENV_NAME.match(name):
            raise PolicyError(f"invalid environment variable name: {name!r}")
        if name.upper() in _ENV_RESERVED:
            raise PolicyError(f"environment variable is managed by the sandbox: {name}")
        if _ENV_DENIED.search(name):
            raise PolicyError(f"environment variable is not allowed in the sandbox: {name}")
        if not isinstance(value, str):
            raise PolicyError(f"environment value must be a string: {name}")
        env[name] = value
    return env


def _default_scratch_root() -> Path:
    return Path(os.environ.get(TEST_WORK) or tempfile.gettempdir())


def _create_scratch(root: Path | str) -> Path:
    base = _canon(root, "scratch root", directory=True)
    return Path(tempfile.mkdtemp(prefix=SCRATCH_PREFIX, dir=base))


def _dir_identity(path: Path) -> tuple[int, int]:
    info = path.lstat()
    if (not stat.S_ISDIR(info.st_mode) or not info.st_ino
            or getattr(info, "st_file_attributes", 0) & stat.FILE_ATTRIBUTE_REPARSE_POINT):
        raise OSError(f"Not a verifiable ordinary directory: {path}")
    return info.st_dev, info.st_ino


def _remove_scratch(scratch: Path, identity: tuple[int, int]) -> None:
    """Remove this run's scratch; refuses a replaced path or anything it did not make."""
    try:
        current = _dir_identity(scratch)
    except FileNotFoundError:
        return
    if current != identity or scratch.resolve() != scratch:
        raise OSError(f"scratch directory was replaced; refusing cleanup: {scratch}")
    if not scratch.name.startswith(SCRATCH_PREFIX):
        raise OSError(f"Refusing to remove unexpected directory: {scratch}")

    def onerror(function, path, exc_info):
        error = exc_info[1]
        if isinstance(error, FileNotFoundError):
            return
        if os.name == "nt" and function is os.unlink and isinstance(error, PermissionError):
            file = Path(path)
            try:
                file_info = file.lstat()
                attrs = getattr(file_info, "st_file_attributes", 0)
                if (stat.S_ISREG(file_info.st_mode) and file_info.st_nlink == 1
                        and attrs & stat.FILE_ATTRIBUTE_READONLY and not attrs & stat.FILE_ATTRIBUTE_REPARSE_POINT):
                    file.chmod(file_info.st_mode | stat.S_IWRITE)
                    file.unlink(missing_ok=True)
                    return
            except FileNotFoundError:
                return
        raise error

    shutil.rmtree(scratch, onerror=onerror)


def run(
    argv: tuple[str, ...] | list[str],
    *,
    workspace: Path | str,
    writable: bool = False,
    protected: tuple[Path | str, ...] | list[Path | str] = (),
    readable: tuple[Path | str, ...] | list[Path | str] = (),
    env: dict[str, str] | None = None,
    timeout: float = 60.0,
    scratch_root: Path | str | None = None,
    backend: ModuleType | None = None,
) -> subprocess.CompletedProcess[bytes]:
    """Run *argv* in the OS sandbox and return its CompletedProcess.

    Creates a fresh scratch directory for this invocation and always removes
    it. Every unsupported mode, failed probe and failed launch is an explicit
    error — there is no fallback to a plain subprocess. ``env`` entries are
    extra whitelisted variables on top of the clean minimal environment.
    """
    policy = validate_policy(workspace=workspace, argv=argv, writable=writable,
                             protected=protected, readable=readable, timeout=timeout)
    selected = backend if backend is not None else load_backend()
    validator = getattr(selected, "validate_runtime", None)
    if callable(validator):
        try:
            validator(policy.workspace)
        except SandboxError:
            raise
        except Exception as error:
            raise BackendUnavailableError(f"Backend runtime check failed: {error}") from error
    try:
        ok, detail = selected.probe()
    except Exception as error:
        raise BackendUnavailableError(f"Backend probe failed: {error}") from error
    if not ok:
        name = getattr(selected, "__name__", type(selected).__name__)
        raise BackendUnavailableError(f"Sandbox backend {name!r} is unavailable: {detail}")

    root = Path(scratch_root) if scratch_root is not None else _default_scratch_root()
    scratch = _create_scratch(root)
    identity = _dir_identity(scratch)
    try:
        if _inside(scratch, policy.workspace) or any(_inside(scratch, grant) for grant in policy.readable):
            raise PolicyError(f"scratch must not sit inside the workspace or a read-only grant: {scratch}")
        child_env = build_env(scratch, overrides=env)
        try:
            return selected.execute(
                policy.workspace,
                policy.argv,
                writable=policy.writable,
                protected=policy.protected,
                readable=policy.readable,
                scratch=scratch,
                env=child_env,
                timeout=policy.timeout,
            )
        except (subprocess.TimeoutExpired, SandboxError):
            raise
        except Exception as error:
            raise LaunchError(f"Sandboxed launch failed: {error}") from error
    finally:
        _remove_scratch(scratch, identity)
