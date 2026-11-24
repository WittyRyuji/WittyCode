"""Fusion seat capabilities. Catalog rows, settings validation and protocol contracts share this registry."""
from dataclasses import dataclass


@dataclass(frozen=True)
class Backend:
    name: str
    prefix: str
    guarded: bool = False


BACKENDS = (Backend("plan", "chatgpt:"), Backend("api", "api:"),
            Backend("devin", "devin:", True), Backend("factory", "factory:", True))
PREFIXES = tuple(b.prefix for b in BACKENDS)
GUARDED = tuple(b.prefix for b in BACKENDS if b.guarded)


def backend_for(page_id: str) -> Backend | None:
    return next((b for b in BACKENDS if page_id.startswith(b.prefix) and page_id[len(b.prefix):].strip()), None)
