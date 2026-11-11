"""The model list the page shows, and the translation between the page's model ids and the engines' own.

Page ids: Factory's models keep Droid's id (``claude-haiku-5-5``); the Codex subscription's are ``codex:<id>``; an API
provider's are ``api:<providerId>:<droid custom id>`` (Droid names a custom model ``custom:...``). Pure: no Qt.

An API provider's model is run by WittyCode's own agent loop (``core/llm``), in the engine that also runs the ChatGPT plan's
models, under the same id the page uses (``Target("chatgpt", "api:...")``). Only what no provider of ours owns and Droid lists
nevertheless (Bedrock, entries added with the CLI) stays Droid's.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

CODEX_PREFIX = "codex:"
DEVIN_PREFIX = "devin:"
CHATGPT_PREFIX = "chatgpt:"
API_PREFIX = "api:"
ENGINE_PREFIXES = {"codex": CODEX_PREFIX, "devin": DEVIN_PREFIX, "chatgpt": CHATGPT_PREFIX}
"""The page-id prefix of the engines whose models are not Droid's."""
OTHER_PROVIDER = "other"
"""Droid custom models that no provider of ours owns (added with the CLI): grouped under one name."""

GROUPS = {
    "anthropic": "Anthropic",
    "openai": "OpenAI",
    "google": "Google",
    "xai": "xAI",
    "factory": "Factory",
}


def _split_api_id(page_id: str) -> tuple[str, str] | None:
    """``api:<providerId>:<droid custom id>`` -> (providerId, droid custom id); None for any other id or a malformed one.
    Droid's custom ids start with ``custom:`` and ours (``p<hex>``, ``auto-<hex>``, ``other``) hold no colon, but a provider
    id typed into an old sidecar might: the split is at the first ``:custom:`` when there is one."""
    if not page_id.startswith(API_PREFIX):
        return None
    rest = page_id[len(API_PREFIX) :]
    marker = ":custom:"
    if marker in rest:
        provider, _, tail = rest.partition(marker)
        native = "custom:" + tail
    else:
        provider, _, native = rest.partition(":")
    return (provider, native) if provider and native else None


@dataclass(frozen=True, slots=True)
class Target:
    """Where a page model id leads: which engine runs it and the id that engine knows."""

    engine: str
    model: str


@dataclass(slots=True)
class Catalog:
    models: list[dict[str, Any]] = field(default_factory=list)
    _targets: dict[str, Target] = field(default_factory=dict)
    _page_ids: dict[tuple[str, str], str] = field(default_factory=dict)

    @classmethod
    def from_items(cls, items: Sequence[Mapping[str, Any]]) -> Catalog:
        """A list as it was cached: the target of every id follows from its prefix, the same way ``target`` reads an id
        the list does not know."""
        catalog = cls()
        for raw in items:
            item = dict(raw)
            parsed = _split_api_id(item["id"])
            if parsed is not None and item.get("engine") == "chatgpt":
                target = Target("chatgpt", item["id"])
            elif parsed is not None:  # (an entry only Droid can run: Bedrock, or added with the CLI)
                target = Target("droid", parsed[1])
            else:
                target = catalog.target(item["id"])
            catalog.models.append(item)
            catalog._targets[item["id"]] = target
            catalog._page_ids[(target.engine, target.model)] = item["id"]
        return catalog

    def target(self, page_id: str) -> Target:
        """Ids the list does not know yet (it loads a moment after the start, and not at all when Droid cannot list)
        are translated by their prefix: Codex's, an API provider's (run by the application's own loop, which knows the
        page's id), else Droid's as they are."""
        found = self._targets.get(page_id)
        if found is not None:
            return found
        for engine, prefix in ENGINE_PREFIXES.items():
            if page_id.startswith(prefix):
                return Target(engine, page_id[len(prefix) :])
        if _split_api_id(page_id) is not None:
            return Target("chatgpt", page_id)
        return Target("droid", page_id)

    @staticmethod
    def droid_id(page_id: str) -> str | None:
        """The id Droid knows an API provider's model by (``custom:...``); ``None`` for any other page id."""
        parsed = _split_api_id(page_id)
        return parsed[1] if parsed is not None else None

    def page_id(self, engine: str, model: str) -> str:
        found = self._page_ids.get((engine, model))
        if found is not None:
            return found
        if model.startswith(API_PREFIX):
            return model
        return ENGINE_PREFIXES.get(engine, "") + model

    def find(self, page_id: str) -> dict[str, Any] | None:
        return next((m for m in self.models if m["id"] == page_id), None)

    def source_of(self, page_id: str) -> str:
        found = self.find(page_id)
        if found is not None:
            return found["source"]
        for engine, prefix in ENGINE_PREFIXES.items():
            if page_id.startswith(prefix):
                return engine
        parsed = _split_api_id(page_id)
        return f"{API_PREFIX}{parsed[0]}" if parsed is not None else "factory"

    def engine_of(self, page_id: str) -> str:
        return self.target(page_id).engine


def _text(value: Any) -> str:
    return str(getattr(value, "value", value) or "")


def _efforts(values: Iterable[Any] | None) -> list[str]:
    return [_text(v) for v in values or ()]


def build_catalog(
    droid_models: Sequence[Any],
    *,
    codex_models: Sequence[Any] = (),
    devin_models: Sequence[Any] = (),
    chatgpt_models: Sequence[Any] = (),
    api_models: Sequence[Any] = (),
    provider_of: Callable[[str, str], str | None] | None = None,
    window_of: Callable[[str], int | None] | None = None,
) -> Catalog:
    """``droid_models``: the SDK's ``ModelInfo`` list. ``codex_models``: ``core.codex.CodexModel``-like objects
    (``id, display_name, efforts, default_effort, context_window``). ``devin_models``: ``core.devin.DevinModel`` objects.
    ``chatgpt_models``: ``core.chatgpt.models.ChatGPTModel`` objects (the ChatGPT plan run by the application's own harness).
    ``api_models``: ``providers.ApiModelSpec`` objects (the API providers' models, run by the same harness; the Droid models
    of the same entries are left out). ``provider_of(custom_id, display_name)`` says which API provider owns a custom model."""
    catalog = Catalog()
    owned = {parsed[1] for spec in api_models if (parsed := _split_api_id(spec.id)) is not None}

    def add(item: dict[str, Any], target: Target, native: str) -> None:
        catalog.models.append(item)
        catalog._targets[item["id"]] = target
        catalog._page_ids[(target.engine, native)] = item["id"]

    for info in droid_models:
        if getattr(info, "is_custom", False) and info.id in owned:
            continue
        if getattr(info, "is_custom", False):
            owner = (provider_of(info.id, info.display_name) if provider_of else None) or OTHER_PROVIDER
            page_id = f"{API_PREFIX}{owner}:{info.id}"
            window = window_of(info.id) if window_of else None
            source = f"{API_PREFIX}{owner}"
            group = None
        else:
            page_id = info.id
            window = None
            source = "factory"
            group = GROUPS.get(_text(info.model_provider).lower(), _text(info.model_provider).title() or None)
        item: dict[str, Any] = {
            "id": page_id,
            "label": info.display_name or info.id,
            "source": source,
            "efforts": _efforts(info.supported_reasoning_efforts),
            "defaultEffort": _text(info.default_reasoning_effort),
            "window": window or 0,
        }
        if group:
            item["group"] = group
        multiplier = getattr(info, "token_multiplier", None)
        if source == "factory" and isinstance(multiplier, (int, float)) and multiplier:
            item["note"] = f"×{multiplier:g}"
        if getattr(info, "no_image_support", False):
            item["noImages"] = True
        if getattr(info, "disabled", False):
            item["disabled"] = True
            reason = getattr(info, "disabled_reason", None)
            if reason:
                item["disabledReason"] = str(reason)
        add(item, Target("droid", info.id), info.id)

    for model in codex_models:
        add(
            {
                "id": CODEX_PREFIX + model.id,
                "label": model.display_name or model.id,
                "source": "codex",
                "efforts": list(model.efforts),
                "defaultEffort": model.default_effort or "",
                "window": getattr(model, "context_window", None) or 0,
            },
            Target("codex", model.id),
            model.id,
        )

    for model in devin_models:
        item = {
            "id": DEVIN_PREFIX + model.id,
            "label": model.display_name or model.id,
            "source": "devin",
            "efforts": list(model.efforts),
            "defaultEffort": model.default_effort or "",
            "window": model.context_window or 0,
        }
        if model.group:
            item["group"] = model.group
        if model.note:
            item["note"] = model.note
        if not model.supports_images:
            item["noImages"] = True
        add(item, Target("devin", model.id), model.id)

    for model in chatgpt_models:
        item = {
            "id": CHATGPT_PREFIX + model.id,
            "label": model.display_name or model.id,
            "source": "chatgpt",
            "efforts": list(model.efforts),
            "defaultEffort": model.default_effort or "",
            "window": model.context_window or 0,
        }
        if not model.supports_images:
            item["noImages"] = True
        add(item, Target("chatgpt", model.id), model.id)

    for spec in api_models:
        item = {
            "id": spec.id,
            "label": spec.display,
            "source": f"{API_PREFIX}{spec.provider_id}",
            "engine": "chatgpt",
            "efforts": ["off", "low", "medium", "high"] if spec.reasoning else [],
            "defaultEffort": spec.reasoning,
            "window": spec.window or 0,
        }
        if not spec.images:
            item["noImages"] = True
        add(item, Target("chatgpt", spec.id), spec.id)
        parsed = _split_api_id(spec.id)
        if parsed is not None:  # (a conversation that Droid ran with this model before still names it that way)
            catalog._page_ids.setdefault(("droid", parsed[1]), spec.id)
    return catalog


def settings_for_model(catalog: Catalog, current: Mapping[str, str], page_id: str) -> tuple[dict[str, str], list[str]]:
    """What a session's settings become when its model changes: a reasoning level the new model does not offer is
    replaced by its own default; the permission scale belongs to the engine. Returns (settings, names that changed)."""
    settings = {**current, "model": page_id}
    changed: list[str] = []
    model = catalog.find(page_id)
    if model is None:
        return settings, changed
    efforts = model["efforts"]
    if efforts and settings.get("reasoning") not in efforts:
        settings["reasoning"] = model["defaultEffort"] or efforts[0]
        changed.append("reasoning")
    elif not efforts and settings.get("reasoning"):
        settings["reasoning"] = ""
        changed.append("reasoning")
    return settings, changed
