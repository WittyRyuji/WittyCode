"""The page's model ids and the engines' own (``core/catalog``), for ids the model list does not hold (yet), and the list built from
the engines' models and the API providers'."""

from __future__ import annotations

from types import SimpleNamespace

from desktop.core.catalog import Catalog, Target, build_catalog


def test_an_api_models_id_is_translated_by_its_prefix_when_the_list_does_not_know_it():
    catalog = Catalog()  # the list loads a moment after the start
    assert catalog.target("api:p123:custom:My-Model-0") == Target("chatgpt", "api:p123:custom:My-Model-0"), "the application's own loop runs it, under the page's id"
    assert catalog.target("api:other:custom:X-1") == Target("chatgpt", "api:other:custom:X-1")
    assert catalog.source_of("api:p123:custom:My-Model-0") == "api:p123"
    assert catalog.engine_of("api:p123:custom:My-Model-0") == "chatgpt"
    assert catalog.page_id("chatgpt", "api:p123:custom:My-Model-0") == "api:p123:custom:My-Model-0"


def test_a_provider_id_with_a_colon_splits_at_the_custom_marker():
    assert Catalog().target("api:we:ird:custom:Y-0") == Target("chatgpt", "api:we:ird:custom:Y-0")
    assert Catalog().source_of("api:we:ird:custom:Y-0") == "api:we:ird"


def test_an_api_id_without_the_marker_splits_at_the_first_colon_and_a_malformed_one_runs_nowhere():
    assert Catalog().source_of("api:p1:thing") == "api:p1"
    for malformed in ("api:", "api:p1", "api::x", "api:p1:"):
        assert Catalog().target(malformed) == Target("chatgpt", ""), malformed
        assert Catalog().source_of(malformed) == "", malformed


def test_chatgpt_plan_models_have_their_own_prefix_source_and_engine():
    from desktop.core.chatgpt.models import ChatGPTModel

    catalog = build_catalog(
        chatgpt_models=[ChatGPTModel("gpt-6.1-sol", "GPT-6.1-Sol", ("low", "high"), "low", 400000), ChatGPTModel("text-only", "Text only", supports_images=False)],
    )
    sol = catalog.find("chatgpt:gpt-6.1-sol")
    assert sol == {"id": "chatgpt:gpt-6.1-sol", "label": "GPT-6.1-Sol", "source": "chatgpt", "efforts": ["low", "high"], "defaultEffort": "low", "window": 400000}
    assert catalog.find("chatgpt:text-only")["noImages"] is True
    assert catalog.target("chatgpt:gpt-6.1-sol") == Target("chatgpt", "gpt-6.1-sol")
    assert catalog.page_id("chatgpt", "gpt-6.1-sol") == "chatgpt:gpt-6.1-sol"


def test_devin_models_keep_their_prefix_even_before_the_list_is_there():
    catalog = Catalog()
    assert catalog.target("devin:swe-2-high") == Target("devin", "swe-2-high")
    assert catalog.source_of("devin:swe-2-high") == "devin" and catalog.engine_of("devin:swe-2-high") == "devin"


def test_an_id_of_an_engine_this_version_does_not_have_runs_nowhere():
    catalog = Catalog()
    for old in ("claude-haiku-5-5", "codex:gpt-5.3-codex", "", "x"):
        assert catalog.target(old) == Target("chatgpt", ""), old
        assert catalog.source_of(old) == "", old
    cached = Catalog.from_items([{"id": "codex:gpt-5.3-codex", "label": "x", "source": "codex"}, {"id": "claude-haiku-5-5", "label": "y", "source": "factory"}])
    assert cached.models == [], "a cached entry of an engine that is gone is left out"


# ------------------------------------------------------------------------------------------------ the API providers' models


def spec(model_id: str = "api:p1:custom:Deep-0", **over) -> SimpleNamespace:
    base = dict(id=model_id, display="Deep", provider_id="p1", window=1_024_000, reasoning="", images=True)
    return SimpleNamespace(**{**base, **over})


def test_an_api_providers_model_is_run_by_the_own_loop_and_its_figures_are_the_users():
    catalog = build_catalog(api_models=[spec(reasoning="high", window=64_000), spec("api:p1:custom:Plain-0", display="Plain", images=False, window=None)])
    deep = catalog.find("api:p1:custom:Deep-0")
    assert deep == {
        "id": "api:p1:custom:Deep-0", "label": "Deep", "source": "api:p1", "engine": "chatgpt",
        "efforts": ["off", "minimal", "low", "medium", "high", "xhigh", "max"], "defaultEffort": "high", "window": 64_000,
    }
    plain = catalog.find("api:p1:custom:Plain-0")
    assert plain["efforts"] == [] and plain["window"] == 0 and plain["noImages"] is True
    assert catalog.target("api:p1:custom:Deep-0") == Target("chatgpt", "api:p1:custom:Deep-0")
    assert catalog.page_id("chatgpt", "api:p1:custom:Deep-0") == "api:p1:custom:Deep-0"


def test_a_cached_list_keeps_who_runs_what():
    catalog = build_catalog(api_models=[spec()])
    again = Catalog.from_items(catalog.models)
    assert again.target("api:p1:custom:Deep-0") == Target("chatgpt", "api:p1:custom:Deep-0")
    assert again.page_id("chatgpt", "api:p1:custom:Deep-0") == "api:p1:custom:Deep-0"
