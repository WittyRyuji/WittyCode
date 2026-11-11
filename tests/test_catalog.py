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


def test_a_provider_id_with_a_colon_splits_at_droids_custom_marker():
    assert Catalog().target("api:we:ird:custom:Y-0") == Target("chatgpt", "api:we:ird:custom:Y-0")
    assert Catalog().source_of("api:we:ird:custom:Y-0") == "api:we:ird"


def test_an_api_id_without_the_marker_splits_at_the_first_colon_and_a_malformed_one_is_left_alone():
    assert Catalog().source_of("api:p1:thing") == "api:p1"
    for malformed in ("api:", "api:p1", "api::x", "api:p1:"):
        assert Catalog().target(malformed) == Target("droid", malformed), malformed
        assert Catalog().source_of(malformed) == "factory", malformed


def test_chatgpt_plan_models_have_their_own_prefix_source_and_engine():
    from desktop.core.chatgpt.models import ChatGPTModel

    catalog = build_catalog(
        [],
        chatgpt_models=[ChatGPTModel("gpt-6.1-sol", "GPT-6.1-Sol", ("low", "high"), "low", 400000), ChatGPTModel("text-only", "Text only", supports_images=False)],
    )
    sol = catalog.find("chatgpt:gpt-6.1-sol")
    assert sol == {"id": "chatgpt:gpt-6.1-sol", "label": "GPT-6.1-Sol", "source": "chatgpt", "efforts": ["low", "high"], "defaultEffort": "low", "window": 400000}
    assert catalog.find("chatgpt:text-only")["noImages"] is True
    assert catalog.target("chatgpt:gpt-6.1-sol") == Target("chatgpt", "gpt-6.1-sol")
    assert catalog.page_id("chatgpt", "gpt-6.1-sol") == "chatgpt:gpt-6.1-sol"
    # (before the list has loaded, the prefix alone decides)
    assert Catalog().target("chatgpt:x") == Target("chatgpt", "x") and Catalog().source_of("chatgpt:x") == "chatgpt"
    assert Catalog().engine_of("chatgpt:x") == "chatgpt"


def test_the_other_ids_are_as_they_were():
    catalog = Catalog()
    assert catalog.target("claude-haiku-5-5") == Target("droid", "claude-haiku-5-5")
    assert catalog.target("codex:gpt-5.3-codex") == Target("codex", "gpt-5.3-codex")
    assert catalog.source_of("claude-haiku-5-5") == "factory" and catalog.source_of("codex:gpt-5.3-codex") == "codex"


# ------------------------------------------------------------------------------------------------ the API providers' models


def spec(model_id: str = "api:p1:custom:Deep-0", **over) -> SimpleNamespace:
    base = dict(id=model_id, display="Deep", provider_id="p1", window=1_024_000, reasoning="", images=True)
    return SimpleNamespace(**{**base, **over})


def droid_custom(model_id: str, name: str) -> SimpleNamespace:
    return SimpleNamespace(
        id=model_id, display_name=name, is_custom=True, model_provider="openai", supported_reasoning_efforts=(), default_reasoning_effort="", disabled=False,
    )


def test_an_api_providers_model_is_run_by_the_own_loop_and_its_figures_are_the_users():
    catalog = build_catalog([], api_models=[spec(reasoning="high", window=64_000), spec("api:p1:custom:Plain-0", display="Plain", images=False, window=None)])
    deep = catalog.find("api:p1:custom:Deep-0")
    assert deep == {
        "id": "api:p1:custom:Deep-0", "label": "Deep", "source": "api:p1", "engine": "chatgpt",
        "efforts": ["off", "low", "medium", "high"], "defaultEffort": "high", "window": 64_000,
    }
    plain = catalog.find("api:p1:custom:Plain-0")
    assert plain["efforts"] == [] and plain["window"] == 0 and plain["noImages"] is True
    assert catalog.target("api:p1:custom:Deep-0") == Target("chatgpt", "api:p1:custom:Deep-0")
    assert catalog.page_id("chatgpt", "api:p1:custom:Deep-0") == "api:p1:custom:Deep-0"


def test_what_droid_lists_of_the_same_entries_is_left_out_and_what_only_droid_can_run_stays():
    droid = [droid_custom("custom:Deep-0", "Deep"), droid_custom("custom:Bedrock-0", "Bedrock")]
    catalog = build_catalog(droid, api_models=[spec()], provider_of=lambda custom_id, name: "p1" if custom_id == "custom:Deep-0" else "bedrock-group")
    ids = [item["id"] for item in catalog.models]
    assert ids == ["api:bedrock-group:custom:Bedrock-0", "api:p1:custom:Deep-0"] or sorted(ids) == sorted(["api:bedrock-group:custom:Bedrock-0", "api:p1:custom:Deep-0"])
    assert catalog.target("api:bedrock-group:custom:Bedrock-0") == Target("droid", "custom:Bedrock-0")
    assert catalog.target("api:p1:custom:Deep-0").engine == "chatgpt"


def test_a_conversation_droid_ran_with_the_model_before_still_names_it():
    catalog = build_catalog([], api_models=[spec()])
    assert catalog.page_id("droid", "custom:Deep-0") == "api:p1:custom:Deep-0"


def test_a_cached_list_keeps_who_runs_what():
    catalog = build_catalog([droid_custom("custom:Bedrock-0", "Bedrock")], api_models=[spec()], provider_of=lambda c, n: "bg")
    again = Catalog.from_items(catalog.models)
    assert again.target("api:p1:custom:Deep-0") == Target("chatgpt", "api:p1:custom:Deep-0")
    assert again.target("api:bg:custom:Bedrock-0") == Target("droid", "custom:Bedrock-0"), "no engine in the item: only Droid can run it"
    assert again.page_id("chatgpt", "api:p1:custom:Deep-0") == "api:p1:custom:Deep-0"
    old_cache = Catalog.from_items([{"id": "api:p1:custom:Deep-0", "label": "Deep", "source": "api:p1", "efforts": [], "defaultEffort": "", "window": 0}])
    assert old_cache.target("api:p1:custom:Deep-0") == Target("droid", "custom:Deep-0"), "a list cached before they were told apart is replaced when the new one loads"
