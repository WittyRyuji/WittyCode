"""The models of the Codex subscription, from ``model/list``.

``model/list`` (codex-cli 0.165.0, verified signed out: it answers without an account) returns
``{"data": [{"id", "model", "displayName", "description", "hidden", "supportedReasoningEfforts":
[{"reasoningEffort", "description"}], "defaultReasoningEffort", "inputModalities": ["text", "image"], "isDefault", ...}],
"nextCursor"}``. It has no context window: that only arrives with ``thread/tokenUsage/updated``
(``modelContextWindow``), and the engine remembers it per model. Ids are Codex's own (the page adds ``codex:``).
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True, slots=True)
class CodexModel:
    id: str
    display_name: str
    efforts: tuple[str, ...] = ()
    """The reasoning efforts it supports, weakest first (``low``, ``medium``, ``high``, ``xhigh``, ``max`` ...)."""
    default_effort: str = ""
    context_window: int | None = None
    description: str = ""
    is_default: bool = False
    supports_images: bool = True


def _text(value: Any) -> str:
    return value.strip() if isinstance(value, str) else ""


def _effort(option: Any) -> str:
    if isinstance(option, Mapping):
        return _text(option.get("reasoningEffort"))
    return _text(option)


def _tokens(value: Any) -> int | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or value <= 0:
        return None
    return int(value)


def model_from_entry(entry: Any) -> CodexModel | None:
    if not isinstance(entry, Mapping):
        return None
    model_id = _text(entry.get("model")) or _text(entry.get("id"))
    if not model_id:
        return None
    efforts = tuple(dict.fromkeys(effort for effort in map(_effort, entry.get("supportedReasoningEfforts") or ()) if effort))
    modalities = entry.get("inputModalities")
    # Older catalogs have no inputModalities: the app-server documentation says to assume text and images then.
    supports_images = True if not isinstance(modalities, (list, tuple)) else "image" in modalities
    return CodexModel(
        id=model_id,
        display_name=_text(entry.get("displayName")) or model_id,
        efforts=efforts,
        default_effort=_text(entry.get("defaultReasoningEffort")),
        context_window=_tokens(entry.get("contextWindow")),
        description=_text(entry.get("description")),
        is_default=bool(entry.get("isDefault")),
        supports_images=supports_images,
    )


def models_from_result(result: Any, *, include_hidden: bool = False) -> list[CodexModel]:
    data = result.get("data") if isinstance(result, Mapping) else None
    return models_from_entries(data or (), include_hidden=include_hidden)


def models_from_entries(entries: Iterable[Any], *, include_hidden: bool = False) -> list[CodexModel]:
    models: list[CodexModel] = []
    seen: set[str] = set()
    for entry in entries:
        if isinstance(entry, Mapping) and entry.get("hidden") and not include_hidden:
            continue
        model = model_from_entry(entry)
        if model is not None and model.id not in seen:
            seen.add(model.id)
            models.append(model)
    return models
