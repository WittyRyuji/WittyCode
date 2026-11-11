"""Reading the model list of ``GET /v1/models``: only the documented fields are certain, the rest is read when present."""

from __future__ import annotations

from desktop.core.chatgpt.models import FALLBACK_MODELS, ChatGPTModel, model_from_entry, models_from_entries, models_from_result


def entry(slug: str = "gpt-x", **more):
    return {"slug": slug, "display_name": "GPT X", "visibility": "list", **more}


def test_the_documented_fields_make_a_model_and_the_rest_is_optional():
    model = model_from_entry(entry())
    assert model == ChatGPTModel("gpt-x", "GPT X", (), "", None, "", True)


def test_a_model_without_a_display_name_is_called_by_its_id():
    assert model_from_entry({"slug": "gpt-y"}).display_name == "gpt-y"


def test_reasoning_levels_a_default_and_a_window_are_read_in_either_spelling():
    snake = model_from_entry(entry(supported_reasoning_levels=[{"effort": "low"}, {"effort": "high"}], default_reasoning_level="low", context_window=400000, description=" Fast. "))
    camel = model_from_entry(entry(supportedReasoningEfforts=[{"reasoningEffort": "low"}, "high"], defaultReasoningEffort="low", contextWindow=400000, description="Fast."))
    assert (snake.efforts, snake.default_effort, snake.context_window, snake.description) == (("low", "high"), "low", 400000, "Fast.")
    assert (camel.efforts, camel.default_effort, camel.context_window) == (("low", "high"), "low", 400000)


def test_a_level_listed_twice_or_empty_counts_once_or_not_at_all():
    model = model_from_entry(entry(supported_reasoning_levels=[{"effort": "low"}, {"effort": "low"}, {"effort": ""}, {}, "medium"]))
    assert model.efforts == ("low", "medium")


def test_a_window_that_is_not_a_positive_number_is_unknown():
    for bad in (0, -5, True, "400000", None):
        assert model_from_entry(entry(context_window=bad)).context_window is None, bad
    assert model_from_entry(entry(context_window=400000.0)).context_window == 400000


def test_pictures_are_supported_unless_the_model_lists_its_inputs_without_them():
    assert model_from_entry(entry()).supports_images is True
    assert model_from_entry(entry(input_modalities=["text"])).supports_images is False
    assert model_from_entry(entry(input_modalities=["text", "image"])).supports_images is True
    assert model_from_entry(entry(inputModalities=["text"])).supports_images is False


def test_an_entry_without_an_id_or_that_is_not_an_object_is_no_model():
    assert model_from_entry({"display_name": "No id"}) is None
    assert model_from_entry({"slug": "  "}) is None
    assert model_from_entry("gpt-x") is None and model_from_entry(None) is None


def test_the_picker_gets_the_listed_models_once_each_in_the_servers_order():
    entries = [entry("a"), entry("hidden-one", visibility="hide"), entry("b", visibility=None), entry("a"), entry("c", hidden=True), entry("d", visibility="list"), "junk"]
    assert [m.id for m in models_from_entries(entries)] == ["a", "b", "d"]


def test_the_result_is_read_under_models_or_data_or_as_a_bare_list():
    assert [m.id for m in models_from_result({"models": [entry("a")]})] == ["a"]
    assert [m.id for m in models_from_result({"data": [entry("b")]})] == ["b"]
    assert [m.id for m in models_from_result([entry("c")])] == ["c"]
    assert [m.id for m in models_from_result({"models": [entry("m")], "data": [entry("d")]})] == ["m"], "models first"


def test_a_result_that_is_nothing_like_a_list_gives_no_models():
    assert models_from_result({"models": "oops"}) == []
    assert models_from_result({}) == [] and models_from_result("x") == [] and models_from_result(None) == []


def test_the_fallback_is_what_the_menu_shows_when_the_list_cannot_be_read():
    assert [m.id for m in FALLBACK_MODELS] == ["gpt-6.1-sol", "gpt-6-astra", "gpt-6-luna"]
    assert all(m.efforts and m.default_effort in m.efforts for m in FALLBACK_MODELS)
