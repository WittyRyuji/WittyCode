"""Public OAuth client identifiers supplied by the application publisher, never client secrets."""
from __future__ import annotations

import json
import os
from pathlib import Path
import re
from collections.abc import Mapping

CLIENT_ENV = {
    "github": "WITTYCODE_GITHUB_CLIENT_ID",
    "google-workspace": "WITTYCODE_GOOGLE_CLIENT_ID",
}
BUNDLED = Path(__file__).with_name("connector-clients.json")
_CLIENT_ID = re.compile(r"[A-Za-z0-9._-]{3,256}\Z")


def valid_client_id(value: object) -> bool:
    return isinstance(value, str) and _CLIENT_ID.fullmatch(value) is not None


def from_environment(environ: Mapping[str, str] | None = None) -> dict[str, str]:
    """Read publisher-supplied public identifiers for a release build, without recording their values in source."""
    environ = os.environ if environ is None else environ
    result = {}
    for provider, variable in CLIENT_ENV.items():
        value = environ.get(variable, "").strip()
        if value and not valid_client_id(value):
            raise ValueError(f"Invalid public client identifier in {variable}.")
        if value:
            result[provider] = value
    return result


def client_id(provider: str) -> str:
    """Prefer the bundled public identity; the environment is a development and packaging fallback."""
    if provider not in CLIENT_ENV:
        return ""
    try:
        raw = json.loads(BUNDLED.read_text(encoding="utf-8"))
        value = raw.get(provider) if isinstance(raw, dict) else None
        if valid_client_id(value):
            return value
    except (OSError, UnicodeError, ValueError):
        pass
    value = os.environ.get(CLIENT_ENV[provider], "").strip()
    return value if valid_client_id(value) else ""
