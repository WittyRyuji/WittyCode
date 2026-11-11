"""The subscription's models from model/list (pure parsing and the engine's cache)."""

from __future__ import annotations

from desktop.core.codex.models import CodexModel, model_from_entry, models_from_result
from tests.codex_support import finish_turn, make_engine, run

ENTRY = {
    "id": "gpt-6.1-sol",
    "model": "gpt-6.1-sol",
    "displayName": "GPT-6.1-Sol",
    "description": "Latest workhorse model.",
    "hidden": False,
    "supportedReasoningEfforts": [
        {"reasoningEffort": "low", "description": "Fast"},
        {"reasoningEffort": "medium", "description": ""},
        {"reasoningEffort": "xhigh", "description": ""},
        {"reasoningEffort": "max", "description": ""},
    ],
    "defaultReasoningEffort": "low",
    "inputModalities": ["text", "image"],
    "isDefault": True,
}


def test_a_model_list_entry_as_the_real_server_sends_it():
    model = model_from_entry(ENTRY)
    assert model == CodexModel(
        id="gpt-6.1-sol",
        display_name="GPT-6.1-Sol",
        efforts=("low", "medium", "xhigh", "max"),
        default_effort="low",
        context_window=None,
        description="Latest workhorse model.",
        is_default=True,
        supports_images=True,
    )
    text_only = model_from_entry({**ENTRY, "model": "luna", "inputModalities": ["text"]})
    assert text_only is not None and not text_only.supports_images
    older = model_from_entry({key: value for key, value in ENTRY.items() if key != "inputModalities"})
    assert older is not None and older.supports_images, "no inputModalities means text and images"
    assert model_from_entry({"displayName": "no id"}) is None


def test_hidden_models_and_duplicates_are_left_out():
    result = {"data": [ENTRY, {**ENTRY, "model": "secret", "hidden": True}, ENTRY], "nextCursor": None}
    assert [m.id for m in models_from_result(result)] == ["gpt-6.1-sol"]
    assert [m.id for m in models_from_result(result, include_hidden=True)] == ["gpt-6.1-sol", "secret"]
    assert models_from_result(None) == []


def test_the_engine_lists_and_caches_the_models_and_learns_their_windows(tmp_path):
    async def scenario():
        harness = make_engine(tmp_path)
        engine = harness.engine
        models = await engine.list_models()
        assert [(m.id, m.efforts, m.default_effort, m.supports_images) for m in models] == [
            ("gpt-6.1-sol", ("low", "medium", "high", "xhigh"), "low", True),
            ("gpt-6-luna", ("low", "medium", "high"), "medium", False),
        ]
        await engine.list_models()
        assert len(harness.requests("model/list")) == 1
        assert harness.requests("model/list")[0] == {"limit": 100, "includeHidden": False}
        controller = await engine.open_new(tmp_path, model="gpt-6.1-sol")
        await finish_turn(controller, "hello")
        assert controller.context.window == 272_000
        refreshed = await engine.list_models(refresh=True)
        assert refreshed[0].context_window == 272_000 and refreshed[1].context_window is None
        assert len(harness.requests("model/list")) == 2
        assert engine.model_info("gpt-6-luna").display_name == "GPT-6-Luna" and engine.model_info("nope") is None
        await engine.close_all()

    run(scenario())
