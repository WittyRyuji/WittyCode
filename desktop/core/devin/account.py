"""The Devin account as the page shows it, read from ``devin auth status``.

The command prints (signed in)::

    Logged in (via Devin).

    Credentials:
      File:              C:\\Users\\...\\devin\\credentials.toml
    User:
      Name:              ...
      Email:             ...
    Account:
      Tier:              Devin Pro
      Plan:              Pro

and (signed out) ``Not logged in`` (exit code 0 either way). It takes about three seconds, so the engine asks once and
remembers; the sign-in itself (``devin auth login``) is the CLI's own browser flow and the tokens stay in Devin's
``credentials.toml``, which this program never reads.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

_LOGGED_IN = re.compile(r"^\s*logged in\b", re.IGNORECASE | re.MULTILINE)
_FIELD = re.compile(r"^\s*(Email|Tier|Plan|Name):\s+(.+?)\s*$", re.IGNORECASE | re.MULTILINE)


@dataclass(frozen=True, slots=True)
class DevinStatus:
    signed_in: bool = False
    email: str = ""
    plan: str = ""
    """``Devin Pro`` (the tier), or the plan's name when there is no tier line."""


def parse_status(text: str) -> DevinStatus:
    """What ``devin auth status`` said."""
    if not _LOGGED_IN.search(text or ""):
        return DevinStatus()
    fields = {key.lower(): value for key, value in _FIELD.findall(text)}
    return DevinStatus(True, fields.get("email", ""), fields.get("tier") or fields.get("plan", ""))


def account_json(
    binary: Any,
    status: DevinStatus | None = None,
    *,
    signing_in: bool = False,
    error: str | None = None,
) -> dict[str, Any]:
    """``binary``: a ``DevinBinary`` (``None``: no CLI). ``status``: ``None`` while not asked yet."""
    if binary is None:
        return {"cli": "missing", "auth": "signed-out"}
    data: dict[str, Any] = {"cli": "found", "cliPath": str(binary.path), "version": binary.version}
    if signing_in:
        data["auth"] = "signing-in"
    elif status is None:
        data["auth"] = "checking"
    else:
        data["auth"] = "signed-in" if status.signed_in else "signed-out"
        if status.signed_in:
            if status.email:
                data["email"] = status.email
            if status.plan:
                data["plan"] = status.plan
    if error:
        data["error"] = error
    return data
