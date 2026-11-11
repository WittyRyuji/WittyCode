"""The models of a Devin account, as ACP reports them.

A session's ``configOptions`` hold a ``model`` select (the ids the account may use: about fifty families, and seventy
``fusion-…-sidekick-…`` pairings of two models that are left out here), a ``thought_level`` select that exists only for
models that think (its values differ per model, ``none`` … ``max``) and a ``speed`` select. Choosing a model resets
``thought_level`` to that model's own default, so the levels each model offers are read by choosing every model once in a
scratch session. ``devin models list --format json`` adds the context windows and cost tiers (family by family).
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any

SUPPORTS_IMAGES = "cognition.ai/supportsImages"
FUSION_PREFIX = "fusion-"

GROUPS: tuple[tuple[str, str], ...] = (
    ("claude", "Anthropic"),
    ("gpt", "OpenAI"),
    ("gemini", "Google"),
    ("grok", "xAI"),
    ("kimi", "Moonshot"),
    ("glm", "Zhipu"),
    ("deepseek", "DeepSeek"),
    ("nemotron", "NVIDIA"),
    ("swe", "Cognition"),
    ("adaptive", "Cognition"),
    ("muse", "Meta"),
)


@dataclass(frozen=True, slots=True)
class DevinModel:
    id: str
    """The value ``session/set_config_option`` takes (``claude-opus-5-5-medium``)."""
    display_name: str = ""
    efforts: tuple[str, ...] = ()
    """The ``thought_level`` values this model offers (empty: it has none)."""
    default_effort: str = ""
    supports_images: bool = True
    context_window: int | None = None
    note: str = ""
    """``Free`` for the models that cost nothing on the account."""

    @property
    def group(self) -> str:
        lowered = self.id.lower()
        return next((name for prefix, name in GROUPS if lowered.startswith(prefix)), "")


def _text(value: Any) -> str:
    return value if isinstance(value, str) else ""


def option_of(options: Any, option_id: str) -> Mapping[str, Any] | None:
    for option in options or ():
        if isinstance(option, Mapping) and option.get("id") == option_id:
            return option
    return None


def option_values(option: Mapping[str, Any] | None) -> list[str]:
    values = []
    for item in (option.get("options") if option else None) or ():
        if isinstance(item, Mapping) and _text(item.get("value")):
            values.append(item["value"])
    return values


def current_value(options: Any, option_id: str) -> str:
    option = option_of(options, option_id)
    return _text(option.get("currentValue")) if option else ""


def thought_levels(options: Any) -> tuple[tuple[str, ...], str]:
    """``(the levels offered, the current one)`` for whichever model is selected in ``options``."""
    option = option_of(options, "thought_level")
    if option is None:
        return (), ""
    return tuple(option_values(option)), _text(option.get("currentValue"))


def model_entries(options: Any) -> list[tuple[str, str, bool]]:
    """``(id, name, supports images)`` of the ``model`` select, fusion pairings left out, in the account's order."""
    entries = []
    for item in (option_of(options, "model") or {}).get("options") or ():
        if not isinstance(item, Mapping) or not _text(item.get("value")) or item["value"].startswith(FUSION_PREFIX):
            continue
        meta = item.get("_meta") if isinstance(item.get("_meta"), Mapping) else {}
        entries.append((item["value"], _text(item.get("name")) or item["value"], meta.get(SUPPORTS_IMAGES) is not False))
    return entries


@dataclass(frozen=True, slots=True)
class FamilyInfo:
    window: int | None = None
    free: bool = False


def cli_info(listing: Any) -> dict[str, FamilyInfo]:
    """``devin models list --format json`` -> what it knows by ``model_uid`` (and by family slug, for the variants it names
    differently from ACP)."""
    info: dict[str, FamilyInfo] = {}
    for family in (listing.get("families") if isinstance(listing, Mapping) else None) or ():
        if not isinstance(family, Mapping):
            continue
        windows = []
        tiers = []
        for variant in family.get("variants") or ():
            if not isinstance(variant, Mapping) or not _text(variant.get("model_uid")):
                continue
            window = variant.get("max_context_tokens")
            window = int(window) if isinstance(window, (int, float)) and not isinstance(window, bool) and window > 0 else None
            free = variant.get("cost_tier") == "Free"
            info[variant["model_uid"]] = FamilyInfo(window, free)
            if window:
                windows.append(window)
            tiers.append(free)
        slug = _text(family.get("slug"))
        if slug and slug not in info and windows:
            info[slug] = FamilyInfo(min(windows), bool(tiers) and all(tiers))
    return info


def known_about(model_id: str, info: Mapping[str, FamilyInfo]) -> FamilyInfo:
    """Exactly this id, else the longest family slug it starts with."""
    found = info.get(model_id)
    if found is not None:
        return found
    best = ""
    for slug in info:
        if model_id.startswith(slug + "-") and len(slug) > len(best):
            best = slug
    return info.get(best, FamilyInfo())


def build_models(
    entries: Sequence[tuple[str, str, bool]],
    levels: Mapping[str, tuple[tuple[str, ...], str]],
    info: Mapping[str, FamilyInfo] | None = None,
) -> list[DevinModel]:
    models = []
    for model_id, name, images in entries:
        efforts, default = levels.get(model_id, ((), ""))
        known = known_about(model_id, info or {})
        models.append(
            DevinModel(
                id=model_id,
                display_name=name,
                efforts=efforts,
                default_effort=default,
                supports_images=images,
                context_window=known.window,
                note="Free" if known.free else "",
            )
        )
    return models


def window_of(models: Iterable[DevinModel], model_id: str) -> int | None:
    return next((m.context_window for m in models if m.id == model_id), None)
