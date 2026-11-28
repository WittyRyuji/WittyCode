"""Standalone OS sandbox: shared policy, backend dispatch and scratch lifecycle.

Qt-free and safe to import without the platform backends installed; the
backend module (``desktop.sandbox.windows`` or ``desktop.sandbox.posix``) is
lazy-imported only when a command is actually run or probed. The network is
always off inside the sandbox. See ``desktop/sandbox/README.md``.
"""

from desktop.sandbox.core import (
    BackendUnavailableError,
    LaunchError,
    Policy,
    PolicyError,
    SandboxError,
    backend_module_name,
    build_env,
    load_backend,
    probe,
    run,
    validate_policy,
)

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
]
