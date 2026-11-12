"""Documented context windows for exact API model ids, not guesses based on a model family.

The bundled table is shared with the editor. Explicit provider limits always win; a gateway may impose a smaller window
than the original model. No network request, credential or other agent's configuration is needed to read this table.
"""

from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path


@lru_cache(maxsize=1)
def _models() -> dict[str, dict]:
    return json.loads(Path(__file__).with_suffix(".json").read_text(encoding="utf-8"))["models"]


def known_window(model_id: str) -> int | None:
    return _models().get(model_id.strip(), {}).get("contextWindow")
