"""Environment passed to agent-controlled subprocesses, never the application's credential environment."""
from __future__ import annotations

import os
import re
from collections.abc import Iterable, Mapping

_SECRET = re.compile(r"TOKEN|SECRET|PASSWORD|PASSWD|API_?KEY|ACCESS_?KEY|PRIVATE_?KEY|CREDENTIAL|AUTH", re.I)
_BASE = frozenset({
    "PATH", "PATHEXT", "SYSTEMROOT", "WINDIR", "COMSPEC", "TEMP", "TMP", "TMPDIR", "HOME", "USERPROFILE",
    "APPDATA", "LOCALAPPDATA", "PROGRAMDATA", "PROGRAMFILES", "PROGRAMFILES(X86)", "COMMONPROGRAMFILES",
    "HOMEDRIVE", "HOMEPATH", "LANG", "LC_ALL", "LC_CTYPE", "VIRTUAL_ENV", "CONDA_PREFIX", "NUMBER_OF_PROCESSORS",
})


def environment(*, minimal: bool = False, blocked: Iterable[str] = (), explicit: Mapping[str, str] | None = None) -> dict[str, str]:
    """MCP/hooks get a base allowlist; shell keeps ordinary build variables but strips credential-looking/configured names.

    Explicitly declared MCP variables are a user's opt-in to that server. This is defence in depth, not an OS sandbox:
    an approved arbitrary program can still read credential files the operating system permits it to read.
    """
    denied = {name.upper() for name in blocked}
    env = {key: value for key, value in os.environ.items() if key.upper() not in denied and not _SECRET.search(key)
           and (not minimal or key.upper() in _BASE)}
    for key, value in (explicit or {}).items():
        env = {k: v for k, v in env.items() if k.upper() != key.upper()}
        env[key] = value
    return env
