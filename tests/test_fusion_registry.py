"""Fusion configuration and persistent expert affinity need no real models or CLIs."""
from __future__ import annotations

import json

import pytest

from desktop.core.fusion.registry import FusionError, FusionRegistry, Policy


def test_policy_pins_the_main_and_authorises_only_configured_models():
    policy = Policy(main="chatgpt:main", workers=("api:demo:worker",), light="chatgpt:small", max_calls=3)
    assert policy.main == "chatgpt:main"
    assert policy.allowed("api:demo:worker", "worker")
    assert policy.allowed("chatgpt:small", "light")
    assert not policy.allowed("chatgpt:other", "worker")
    with pytest.raises(FusionError):
        Policy(main="factory:external", workers=("api:demo:worker",), light="chatgpt:small", max_calls=3)
    with pytest.raises(FusionError):
        Policy(main="chatgpt:main", workers=("api:demo:worker",), light="chatgpt:small", max_calls=-1)


def test_same_module_reuses_its_original_expert_even_if_another_model_is_requested(tmp_path):
    policy = Policy(main="chatgpt:main", workers=("api:demo:worker", "chatgpt:other"), light="chatgpt:small", max_calls=3)
    registry = FusionRegistry(project=tmp_path, policy=policy)
    first = registry.assign("src/module", "api:demo:worker", "worker")
    assert registry.assign("src/module/file.py", "api:demo:worker", "worker").id == first.id
    with pytest.raises(FusionError, match="already assigned"):
        registry.assign("src/module/file.py", "chatgpt:other", "worker")
    assert registry.assign("src/other", "chatgpt:other", "worker").id != first.id


def test_widening_an_owned_scope_keeps_the_new_siblings_with_the_same_expert(tmp_path):
    policy = Policy(main="chatgpt:main", workers=("chatgpt:worker", "chatgpt:other"), light="chatgpt:small", max_calls=3)
    registry = FusionRegistry(project=tmp_path, policy=policy)
    expert = registry.assign("src/module/first.py", "chatgpt:worker", "worker")
    assert registry.assign("src/module", "chatgpt:worker", "worker").id == expert.id
    with pytest.raises(FusionError, match="already assigned"):
        registry.assign("src/module/other.py", "chatgpt:other", "worker")


def test_scope_validation_refuses_escape_links_and_ambiguous_inputs(tmp_path):
    policy = Policy(main="chatgpt:main", workers=("chatgpt:worker",), light="chatgpt:small", max_calls=3)
    registry = FusionRegistry(project=tmp_path / "project", policy=policy)
    registry.project.mkdir()
    for scope in ("", "../outside", "C:/elsewhere", "//server/share", "src/*", "src/../other"):
        with pytest.raises(FusionError):
            registry.assign(scope, "chatgpt:worker", "worker")


def test_changed_scope_link_does_not_silently_reassign_a_module(tmp_path, monkeypatch):
    policy = Policy(main="chatgpt:main", workers=("chatgpt:worker", "chatgpt:other"), light="chatgpt:small", max_calls=3)
    registry = FusionRegistry(project=tmp_path, policy=policy)
    target = {"value": "src/original"}
    monkeypatch.setattr(FusionRegistry, "_scope", lambda self, value: target["value"])
    expert = registry.assign("src/link", "chatgpt:worker", "worker")
    target["value"] = "src/different"
    with pytest.raises(FusionError, match="changed"):
        registry.assign("src/link", "chatgpt:other", "worker")
    assert list(registry.agents) == [expert.id]


def test_malformed_fusion_archive_never_opens_as_an_ordinary_session(tmp_path):
    from desktop.core.harness.store import SavedSession, SessionStore
    policy = Policy(main="chatgpt:main", workers=("chatgpt:worker",), light="chatgpt:small", max_calls=3)
    store = SessionStore(tmp_path)
    ident = "a" * 32
    store.save(SavedSession(id=ident, project=str(tmp_path), model="main", fusion=FusionRegistry(tmp_path, policy).to_json()))
    path = tmp_path / "sessions" / f"{ident}.json"
    original = json.loads(path.read_text(encoding="utf-8"))
    for damaged in (None, "corrupt", [], {}):
        path.write_text(json.dumps({**original, "fusion": damaged}), encoding="utf-8")
        assert store.load(ident) is None


def test_registry_round_trips_owner_and_budget_without_cross_session_reuse(tmp_path):
    policy = Policy(main="chatgpt:main", workers=("chatgpt:worker",), light="chatgpt:small", max_calls=2)
    first = FusionRegistry(project=tmp_path, policy=policy)
    expert = first.assign("src/module", "chatgpt:worker", "worker")
    first.begin(expert.id)
    first.finish(expert.id, status="done", tokens=123)
    restored = FusionRegistry.from_json(tmp_path, json.loads(json.dumps(first.to_json())))
    assert restored.assign("src/module/file.py", "chatgpt:worker", "worker").id == expert.id
    assert restored.calls == 1 and restored.tokens == 123
    unrelated = FusionRegistry(project=tmp_path, policy=policy)
    assert unrelated.assign("src/module", "chatgpt:worker", "worker").id != expert.id


def test_no_retry_of_unfinished_work_after_restore_and_budget_is_enforced(tmp_path):
    policy = Policy(main="chatgpt:main", workers=("chatgpt:worker",), light="chatgpt:small", max_calls=1)
    registry = FusionRegistry(project=tmp_path, policy=policy)
    expert = registry.assign("src", "chatgpt:worker", "worker")
    registry.begin(expert.id)
    restored = FusionRegistry.from_json(tmp_path, registry.to_json())
    assert restored.agents[expert.id].status == "interrupted"
    assert restored.unmetered and restored.had_unknown
    with pytest.raises(FusionError, match="budget"):
        restored.begin(expert.id)


def test_registry_round_trips_identity_and_new_attempts_and_loads_legacy(tmp_path):
    policy = Policy("chatgpt:main", ("chatgpt:worker",), "chatgpt:small", 0)
    registry = FusionRegistry(tmp_path, policy)
    legacy = registry.assign_workstream("Inventory", ("inventory.txt",), "chatgpt:worker", "worker")
    coder = registry.assign_workstream("Inventory", ("inventory.txt",), "chatgpt:worker", "worker", identity="Coder")
    for _ in range(2):
        runner = registry.assign_workstream("Inventory", (), "chatgpt:small", "light", identity="Runner", reuse=False)
        registry.begin(runner.id)
        registry.finish(runner.id, status="done", tokens=0)
    saved = registry.to_json()
    saved["agents"][0].pop("identity")
    restored = FusionRegistry.from_json(tmp_path, json.loads(json.dumps(saved)))
    assert restored.agents[legacy.id].identity == "" and restored.agents[legacy.id].label == "Inventory"
    assert restored.agents[coder.id].identity == "Coder" and restored.agents[coder.id].label == "Coder-Inventory"
    assert [item.label for item in restored.agents.values()].count("Runner-Inventory") == 2
    assert restored.assign_workstream("inventory", ("inventory.txt",), "chatgpt:worker", "worker", identity="Coder").id == coder.id
    saved["agents"][1]["identity"] = "Unknown"
    with pytest.raises(FusionError, match="identity"):
        FusionRegistry.from_json(tmp_path, saved)
