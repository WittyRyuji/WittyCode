"""The model list the page shows, and the translation between the page's model ids and the engines' own.

Page ids: ChatGPT Plan's models are ``chatgpt:<id>``, Devin's ``devin:<id>``; an API provider's are
``api:<providerId>:<model entry id>``. An API provider's model is run by WittyCode's own agent loop (``core/llm``), in the
engine that also runs the ChatGPT plan's models, under the same id the page uses (``Target("chatgpt", "api:...")``).
An id of no known form (a model of an engine WittyCode no longer has) leads nowhere: ``Target("chatgpt", "")``.
Pure: no Qt.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

DEVIN_PREFIX = "devin:"
CHATGPT_PREFIX = "chatgpt:"
API_PREFIX = "api:"
OWN_ENGINE = "chatgpt"
ENGINE_PREFIXES = {"devin": DEVIN_PREFIX, "chatgpt": CHATGPT_PREFIX}
"""The page-id prefix of each engine's own models."""


def _split_api_id(page_id: str) -> tuple[str, str] | None:
    """``api:<providerId>:<model entry id>`` -> (providerId, entry id); None for any other id or a malformed one. Entry
    ids written by older versions start with ``custom:``; a provider id typed into an old file might hold a colon, so the
    split is at the first ``:custom:`` when there is one."""
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
    """Where a page model id leads: which engine runs it and the id that engine knows (``""``: none does)."""

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
        the list does not know (an entry of an engine that is gone is left out)."""
        catalog = cls()
        for raw in items:
            item = dict(raw)
            target = catalog.target(item["id"])
            if not target.model:
                continue
            catalog.models.append(item)
            catalog._targets[item["id"]] = target
            catalog._page_ids[(target.engine, target.model)] = item["id"]
        return catalog

    def target(self, page_id: str) -> Target:
        """Ids the list does not know yet (it loads a moment after the start) are translated by their prefix."""
        found = self._targets.get(page_id)
        if found is not None:
            return found
        for engine, prefix in ENGINE_PREFIXES.items():
            if page_id.startswith(prefix):
                return Target(engine, page_id[len(prefix) :])
        if _split_api_id(page_id) is not None:
            return Target(OWN_ENGINE, page_id)
        return Target(OWN_ENGINE, "")

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
        return f"{API_PREFIX}{parsed[0]}" if parsed is not None else ""

    def engine_of(self, page_id: str) -> str:
        return self.target(page_id).engine


def _text(value: Any) -> str:
    return str(getattr(value, "value", value) or "")


def _efforts(values: Iterable[Any] | None) -> list[str]:
    return [_text(v) for v in values or ()]


def build_catalog(
    *,
    devin_models: Sequence[Any] = (),
    chatgpt_models: Sequence[Any] = (),
    api_models: Sequence[Any] = (),
    provider_of: Callable[[str, str], str | None] | None = None,
    window_of: Callable[[str], int | None] | None = None,
) -> Catalog:
    """``devin_models``: ``core.devin.DevinModel`` objects. ``chatgpt_models``: ``core.chatgpt.models.ChatGPTModel`` objects
    (the ChatGPT plan, run by the application's own loop). ``api_models``: ``providers.ApiModelSpec`` objects (the API
    providers' models, run by the same loop). ``provider_of`` / ``window_of`` are accepted for the callers' convenience and
    not needed: an API model's spec names its provider and window."""
    catalog = Catalog()

    def add(item: dict[str, Any], target: Target, native: str) -> None:
        catalog.models.append(item)
        catalog._targets[item["id"]] = target
        catalog._page_ids[(target.engine, native)] = item["id"]

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
            "engine": OWN_ENGINE,
            "efforts": ["off", "low", "medium", "high"] if spec.reasoning else [],
            "defaultEffort": spec.reasoning,
            "window": spec.window or 0,
        }
        if not spec.images:
            item["noImages"] = True
        add(item, Target(OWN_ENGINE, spec.id), spec.id)
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
