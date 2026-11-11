"""The models a ChatGPT plan can run, from ``GET https://api.openai.com/v1/models``.

The documented shape is ``{"models": [{"slug", "display_name", "visibility", ...}]}`` and only ``visibility == "list"``
models are meant for a picker. The other fields (reasoning levels, context window, image input) are read when present
under the names Codex's own catalogs use, in either spelling; none of that was verified on a real account when this was
written (``docs/chatgpt-plan-harness.md`` section 4), so a model with no figures still works: no efforts means no
reasoning menu, no window means the context meter shows a dash.

``FALLBACK_MODELS`` is what the menu shows when the list cannot be read (offline, a refused request): ids Codex's
catalog is known to use. A fallback model the account cannot run fails with the server's error on the first message.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True, slots=True)
class ChatGPTModel:
    id: str
    display_name: str
    efforts: tuple[str, ...] = ()
    default_effort: str = ""
    context_window: int | None = None
    description: str = ""
    supports_images: bool = True


FALLBACK_MODELS: tuple[ChatGPTModel, ...] = (
    ChatGPTModel("gpt-6.1-sol", "GPT-6.1-Sol", ("low", "medium", "high", "xhigh"), "medium", description="Workhorse."),
    ChatGPTModel("gpt-6-astra", "GPT-6-Astra", ("low", "medium", "high", "xhigh"), "medium"),
    ChatGPTModel("gpt-6-luna", "GPT-6-Luna", ("low", "medium", "high"), "low", description="Fast."),
)


def _text(value: Any) -> str:
    return value.strip() if isinstance(value, str) else ""


def _tokens(value: Any) -> int | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or value <= 0:
        return None
    return int(value)


def _first(entry: Mapping[str, Any], *names: str) -> Any:
    return next((entry[name] for name in names if entry.get(name) not in (None, "", [])), None)


def _effort(option: Any) -> str:
    if isinstance(option, Mapping):
        return _text(_first(option, "effort", "reasoning_effort", "reasoningEffort"))
    return _text(option)


def model_from_entry(entry: Any) -> ChatGPTModel | None:
    if not isinstance(entry, Mapping):
        return None
    model_id = _text(_first(entry, "slug", "id", "model"))
    if not model_id:
        return None
    levels = _first(entry, "supported_reasoning_levels", "supported_reasoning_efforts", "supportedReasoningEfforts") or ()
    efforts = tuple(dict.fromkeys(effort for effort in map(_effort, levels) if effort)) if isinstance(levels, (list, tuple)) else ()
    modalities = _first(entry, "input_modalities", "inputModalities")
    return ChatGPTModel(
        id=model_id,
        display_name=_text(_first(entry, "display_name", "displayName")) or model_id,
        efforts=efforts,
        default_effort=_text(_first(entry, "default_reasoning_level", "default_reasoning_effort", "defaultReasoningEffort")),
        context_window=_tokens(_first(entry, "context_window", "contextWindow", "max_context_window")),
        description=_text(entry.get("description")),
        supports_images=True if not isinstance(modalities, (list, tuple)) else "image" in modalities,
    )


def models_from_entries(entries: Iterable[Any]) -> list[ChatGPTModel]:
    """The models meant for a picker, in the server's order."""
    models: list[ChatGPTModel] = []
    seen: set[str] = set()
    for entry in entries:
        if isinstance(entry, Mapping):
            visibility = entry.get("visibility")
            if visibility not in (None, "list") or entry.get("hidden"):
                continue
        model = model_from_entry(entry)
        if model is not None and model.id not in seen:
            seen.add(model.id)
            models.append(model)
    return models


def models_from_result(result: Any) -> list[ChatGPTModel]:
    if isinstance(result, Mapping):
        entries = result.get("models")
        if not isinstance(entries, list):
            entries = result.get("data")
        return models_from_entries(entries or ())
    if isinstance(result, list):
        return models_from_entries(result)
    return []
