"""API providers (Droid's ``customModels``): the store, the id rule, the connection test. No network, no real profile."""

from __future__ import annotations

import asyncio
import copy
import json
import socket
import sys
import threading
import urllib.error
from pathlib import Path

import pytest

from desktop.core import providers as providers_module
from desktop.core.providers import (
    BEDROCK_TEST_NOTE,
    MODELS_FILE_ENV,
    ProviderError,
    ProviderStore,
    custom_model_id,
    run_key_command,
)

SECRET = "sk-" + "SENTINEL-0123456789-abcdef-7Qd2"
MASKED = "sk-" + "\u2022" * 8 + "7Qd2"


class FakeResponse:
    status = 200

    def __init__(self, body: bytes) -> None:
        self._body = body

    def read(self, size: int = -1) -> bytes:
        return self._body

    def __enter__(self) -> "FakeResponse":
        return self

    def __exit__(self, *exc: object) -> None:
        return None


class Opener:
    def __init__(self, body: object = None, error: BaseException | None = None) -> None:
        self.body = body
        self.error = error
        self.requests: list = []
        self.timeouts: list = []

    def __call__(self, request, timeout=None):
        self.requests.append(request)
        self.timeouts.append(timeout)
        if self.error is not None:
            raise self.error
        raw = self.body if isinstance(self.body, bytes) else json.dumps(self.body).encode()
        return FakeResponse(raw)

    @property
    def headers(self) -> dict[str, str]:
        return {k.lower(): v for k, v in self.requests[-1].header_items()}


def make_store(tmp_path: Path, *, environ=None, registry=None, opener=None, run_command=None) -> ProviderStore:
    return ProviderStore(
        models_file=tmp_path / "home" / ".factory" / "settings.json",
        sidecar=tmp_path / "app" / "providers.json",
        environ={} if environ is None else environ,
        registry_env=lambda: {} if registry is None else registry,
        opener=opener,
        run_command=run_command,
    )


def put_settings(store: ProviderStore, doc: object) -> None:
    store.models_file.parent.mkdir(parents=True, exist_ok=True)
    store.models_file.write_text(json.dumps(doc, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def get_settings(store: ProviderStore) -> dict:
    return json.loads(store.models_file.read_text(encoding="utf-8"))


def entries_of(store: ProviderStore) -> list[dict]:
    return get_settings(store)["customModels"]


def model(model_id: str, **over) -> dict:
    row = {"id": model_id, "displayName": "", "reasoning": "", "images": False}
    row.update(over)
    return row


def draft(**over) -> dict:
    base = {
        "id": "p1",
        "name": "OpenRouter",
        "kind": "generic-chat-completion-api",
        "baseUrl": "https://openrouter.ai/api/v1",
        "keyMode": "env",
        "keyHint": "${OPENROUTER_API_KEY}",
        "models": [model("openai/gpt-oss-120b")],
        "headers": [],
        "extraArgs": "",
    }
    base.update(over)
    return base


def entry(url: str, model_id: str, provider: str = "generic-chat-completion-api", **over) -> dict:
    row = {"model": model_id, "displayName": model_id, "baseUrl": url, "provider": provider}
    row.update(over)
    return row


def run(coro):
    return asyncio.run(coro)


# ------------------------------------------------------------------------------------------------ round trips


BEDROCK_DRAFT = draft(
    id="b1",
    name="AWS",
    kind="bedrock-converse",
    baseUrl="",
    keyMode="none",
    keyHint="",
    bedrock={"method": "aws-credentials", "region": "us-west-2", "profile": "my-prof"},
    models=[model("anthropic.claude-sonnet-4-5-20250929-v1:0", displayName="Sonnet 4.5 [Bedrock]", images=True)],
)

KIND_CASES = [
    pytest.param(
        draft(
            id="a1",
            name="Anthropic",
            kind="anthropic",
            baseUrl="https://api.anthropic.com",
            keyHint="${ANTHROPIC_API_KEY}",
            bearer=True,
            models=[model("claude-sonnet-4-5-20250929", displayName="Sonnet", contextWindow=200000, maxOutput=64000, reasoning="medium", images=True)],
        ),
        {
            "model": "claude-sonnet-4-5-20250929",
            "displayName": "Sonnet",
            "baseUrl": "https://api.anthropic.com",
            "apiKey": "${ANTHROPIC_API_KEY}",
            "authMode": "bearer",
            "provider": "anthropic",
            "maxOutputTokens": 64000,
            "maxContextLimit": 200000,
            "reasoningEffort": "medium",
            "noImageSupport": False,
        },
        id="anthropic",
    ),
    pytest.param(
        draft(
            id="o1",
            name="OpenAI",
            kind="openai",
            baseUrl="https://api.openai.com/v1",
            keyHint="${OPENAI_API_KEY}",
            models=[model("gpt-5.3-codex", images=True)],
        ),
        {
            "model": "gpt-5.3-codex",
            "displayName": "gpt-5.3-codex",
            "baseUrl": "https://api.openai.com/v1",
            "apiKey": "${OPENAI_API_KEY}",
            "provider": "openai",
            "noImageSupport": False,
        },
        id="openai",
    ),
    pytest.param(
        draft(
            headers=[{"k": "X-Team", "v": "core"}],
            extraArgs='{"temperature": 0.7, "top_p": 0.9}',
        ),
        {
            "model": "openai/gpt-oss-120b",
            "displayName": "openai/gpt-oss-120b",
            "baseUrl": "https://openrouter.ai/api/v1",
            "apiKey": "${OPENROUTER_API_KEY}",
            "provider": "generic-chat-completion-api",
            "noImageSupport": True,
            "extraHeaders": {"X-Team": "core"},
            "extraArgs": {"temperature": 0.7, "top_p": 0.9},
        },
        id="generic",
    ),
    pytest.param(
        BEDROCK_DRAFT,
        {
            "model": "anthropic.claude-sonnet-4-5-20250929-v1:0",
            "displayName": "Sonnet 4.5 [Bedrock]",
            "provider": "bedrock-converse",
            "noImageSupport": False,
            "bedrock": {"authMethod": "aws-credentials", "awsRegion": "us-west-2", "awsProfile": "my-prof"},
        },
        id="bedrock",
    ),
]


@pytest.mark.parametrize(("d", "expected"), KIND_CASES)
def test_round_trip_of_each_kind(tmp_path, d, expected):
    store = make_store(tmp_path)
    saved = store.save(d)

    assert entries_of(store) == [expected]
    assert saved["id"] == d["id"] and saved["name"] == d["name"] and saved["kind"] == d["kind"]
    assert store.list() == [saved]
    assert "status" not in saved
    # What the page received can be sent straight back without changing anything.
    before = get_settings(store)
    assert store.save(copy.deepcopy(saved)) == saved
    assert get_settings(store) == before


def test_saved_view_matches_the_page_shape(tmp_path):
    store = make_store(tmp_path)
    saved = store.save(
        draft(
            keyMode="command",
            keyHint="C:\\tools\\mint.cmd",
            keyTtlMs=60000,
            models=[model("m1", displayName="One", contextWindow=250000, maxOutput=16384, reasoning="high", images=True), model("m2")],
            headers=[{"k": "X-A", "v": "1"}],
            extraArgs='{"top_p": 0.9}',
        )
    )
    assert saved == {
        "id": "p1",
        "name": "OpenRouter",
        "kind": "generic-chat-completion-api",
        "baseUrl": "https://openrouter.ai/api/v1",
        "keyMode": "command",
        "keyHint": "C:\\tools\\mint.cmd",
        "keyTtlMs": 60000,
        "models": [
            {"id": "m1", "displayName": "One", "contextWindow": 250000, "maxOutput": 16384, "reasoning": "high", "images": True},
            {"id": "m2", "displayName": "m2", "reasoning": "", "images": False},
        ],
        "headers": [{"k": "X-A", "v": "1"}],
        "extraArgs": '{"top_p": 0.9}',
    }


def test_new_entries_follow_the_documented_field_order(tmp_path):
    store = make_store(tmp_path)
    store.save(draft(models=[model("m", contextWindow=1000, maxOutput=500, reasoning="low")]))
    assert list(entries_of(store)[0]) == [
        "model", "displayName", "baseUrl", "apiKey", "provider", "maxOutputTokens", "maxContextLimit", "reasoningEffort", "noImageSupport",
    ]


@pytest.mark.parametrize(
    ("d", "present", "absent"),
    [
        (draft(keyMode="env", keyHint="${GROQ_API_KEY}"), {"apiKey": "${GROQ_API_KEY}"}, ("apiKeyHelper", "apiKeyHelperTtlMs")),
        (draft(keyMode="command", keyHint="mint.cmd", keyTtlMs=300000), {"apiKeyHelper": "mint.cmd", "apiKeyHelperTtlMs": 300000}, ("apiKey",)),
        (draft(keyMode="command", keyHint="mint.cmd"), {"apiKeyHelper": "mint.cmd"}, ("apiKey", "apiKeyHelperTtlMs")),
        (draft(keyMode="none", keyHint="ignored"), {}, ("apiKey", "apiKeyHelper", "apiKeyHelperTtlMs")),
    ],
)
def test_key_modes_write_only_references(tmp_path, d, present, absent):
    store = make_store(tmp_path)
    saved = store.save(d)
    written = entries_of(store)[0]
    for name, value in present.items():
        assert written[name] == value
    for name in absent:
        assert name not in written
    assert saved["keyMode"] == d["keyMode"]
    assert saved["keyHint"] == ("" if d["keyMode"] == "none" else d["keyHint"])


def test_reasoning_and_images_rows(tmp_path):
    store = make_store(tmp_path)
    store.save(draft(kind="openai", models=[model("a", reasoning="high", images=True), model("b", reasoning="", images=False)]))
    first, second = entries_of(store)
    assert first["reasoningEffort"] == "high" and first["noImageSupport"] is False
    assert "reasoningEffort" not in second and second["noImageSupport"] is True


def test_missing_flags_read_with_droids_defaults(tmp_path):
    store = make_store(tmp_path)
    put_settings(store, {"customModels": [entry("https://a/v1", "g"), entry("https://b/v1", "o", "openai")]})
    generic, openai = store.list()
    assert generic["models"][0]["images"] is False
    assert openai["models"][0]["images"] is True


def test_reasoning_that_the_page_cannot_show_survives_a_save(tmp_path):
    store = make_store(tmp_path)
    put_settings(store, {"customModels": [entry("https://a/v1", "m", reasoningEffort="dynamic")]})
    (view,) = store.list()
    assert view["models"][0]["reasoning"] == ""
    store.save(view)
    assert entries_of(store)[0]["reasoningEffort"] == "dynamic"


def test_bedrock_keeps_a_non_converse_provider_and_unmanaged_aws_fields(tmp_path):
    store = make_store(tmp_path)
    original = {
        "model": "anthropic.claude-sonnet-4-5-20250929-v1:0",
        "displayName": "Sonnet [Bedrock]",
        "provider": "anthropic",
        "bedrock": {"awsRegion": "eu-west-1", "bedrockBaseUrl": "https://vpce.example", "requestMetadata": {"team": "core"}},
    }
    put_settings(store, {"customModels": [original]})
    (view,) = store.list()
    assert view["kind"] == "bedrock-converse"
    assert view["bedrock"] == {"method": "aws-credentials", "region": "eu-west-1", "profile": ""}
    store.save({**view, "models": [{**view["models"][0], "images": True}]})
    written = entries_of(store)[0]
    assert written["provider"] == "anthropic"
    assert written["bedrock"] == original["bedrock"]


def test_bedrock_credentials_command_needs_an_export_command(tmp_path):
    store = make_store(tmp_path)
    bad = {**BEDROCK_DRAFT, "bedrock": {"method": "credentials-command", "region": "us-west-2", "profile": ""}}
    with pytest.raises(ProviderError, match="awsCredentialExport"):
        store.save(bad)
    assert not store.models_file.exists()


# ------------------------------------------------------------------------------------------- legacy plaintext keys


def legacy_settings(store: ProviderStore) -> None:
    put_settings(
        store,
        {
            "customModels": [
                {
                    "model": "gpt-5.3-codex",
                    "displayName": "GPT",
                    "baseUrl": "https://api.openai.com/v1",
                    "apiKey": SECRET,
                    "provider": "openai",
                    "maxOutputTokens": 16384,
                    "futureField": {"keep": ["me"]},
                }
            ]
        },
    )


def test_legacy_inline_key_is_shown_masked_only(tmp_path):
    store = make_store(tmp_path)
    legacy_settings(store)
    listing = store.list()
    (view,) = listing
    assert view["keyMode"] == "inline" and view["keyHint"] == MASKED
    dumped = json.dumps(listing, ensure_ascii=False)
    assert SECRET not in dumped and SECRET[3:-4] not in dumped


def test_short_inline_keys_are_fully_hidden(tmp_path):
    store = make_store(tmp_path)
    put_settings(store, {"customModels": [entry("https://a/v1", "m", apiKey="abc123")]})
    (view,) = store.list()
    assert view["keyHint"] == "\u2022" * 8


def test_saving_an_inline_provider_keeps_its_key_and_gives_it_to_new_models(tmp_path):
    store = make_store(tmp_path)
    legacy_settings(store)
    (view,) = store.list()
    before = get_settings(store)

    assert store.save(view) == view
    assert get_settings(store) == before

    more = {**view, "models": [*view["models"], model("gpt-5.3-mini")]}
    store.save(more)
    first, second = entries_of(store)
    assert first["apiKey"] == SECRET and second["apiKey"] == SECRET
    assert first["futureField"] == {"keep": ["me"]}


@pytest.mark.parametrize(
    ("mode", "hint", "field"),
    [("env", "${OPENAI_API_KEY}", "apiKey"), ("command", "mint.cmd", "apiKeyHelper"), ("none", "", None)],
)
def test_switching_away_from_inline_removes_the_plaintext(tmp_path, mode, hint, field):
    store = make_store(tmp_path)
    legacy_settings(store)
    (view,) = store.list()
    saved = store.save({**view, "keyMode": mode, "keyHint": hint})
    assert SECRET not in store.models_file.read_text(encoding="utf-8")
    assert saved["keyMode"] == mode
    written = entries_of(store)[0]
    if field:
        assert written[field] == hint
    assert written["futureField"] == {"keep": ["me"]}


@pytest.mark.parametrize(
    "change",
    [
        pytest.param({"keyMode": "inline", "keyHint": SECRET}, id="inline-with-a-key"),
        pytest.param({"keyMode": "env", "keyHint": SECRET}, id="env-with-a-key"),
        pytest.param({"keyMode": "env", "keyHint": "OPENAI_API_KEY"}, id="env-without-braces"),
        pytest.param({"keyMode": "env", "keyHint": "${A B}"}, id="env-bad-name"),
        pytest.param({"headers": [{"k": "Authorization", "v": f"Bearer {SECRET}"}]}, id="authorization-header"),
        pytest.param({"headers": [{"k": "x-api-key", "v": SECRET}]}, id="api-key-header"),
        pytest.param({"baseUrl": f"https://user:{SECRET}@api.openai.com/v1"}, id="credentials-in-url"),
    ],
)
def test_a_draft_cannot_carry_a_plaintext_key(tmp_path, change):
    store = make_store(tmp_path)
    legacy_settings(store)
    (view,) = store.list()
    before = store.models_file.read_bytes()
    with pytest.raises(ProviderError) as caught:
        store.save({**view, **change})
    assert SECRET not in str(caught.value)
    assert store.models_file.read_bytes() == before


def test_inline_on_a_provider_without_a_stored_key_is_refused(tmp_path):
    store = make_store(tmp_path)
    with pytest.raises(ProviderError):
        store.save(draft(keyMode="inline", keyHint=""))
    assert not store.models_file.exists()


def test_secret_looking_headers_are_masked_and_kept(tmp_path):
    store = make_store(tmp_path)
    put_settings(
        store,
        {"customModels": [entry("https://gw/v1", "m", extraHeaders={"Authorization": f"Bearer {SECRET}", "X-Team": "core", "X-Api-Key": "${GW_KEY}"})]},
    )
    listing = store.list()
    (view,) = listing
    assert SECRET not in json.dumps(listing, ensure_ascii=False)
    by_name = {h["k"]: h["v"] for h in view["headers"]}
    assert by_name["X-Team"] == "core" and by_name["X-Api-Key"] == "${GW_KEY}"
    assert by_name["Authorization"].endswith("7Qd2") and SECRET not in by_name["Authorization"]
    store.save(view)
    assert entries_of(store)[0]["extraHeaders"]["Authorization"] == f"Bearer {SECRET}"
    store.save({**view, "headers": [{"k": "X-Team", "v": "core"}]})
    assert SECRET not in store.models_file.read_text(encoding="utf-8")


def test_headers_may_reference_variables(tmp_path):
    store = make_store(tmp_path)
    saved = store.save(draft(headers=[{"k": "Authorization", "v": "Bearer ${GW_TOKEN}"}]))
    assert entries_of(store)[0]["extraHeaders"] == {"Authorization": "Bearer ${GW_TOKEN}"}
    assert saved["headers"] == [{"k": "Authorization", "v": "Bearer ${GW_TOKEN}"}]


# ---------------------------------------------------------------------------------------------- the file on disk


def test_everything_else_in_the_file_is_kept(tmp_path):
    store = make_store(tmp_path)
    foreign = {
        "model": "kimi",
        "displayName": "Kimi \u6d4b\u8bd5",
        "baseUrl": "https://api.groq.com/openai/v1",
        "apiKey": "${GROQ_API_KEY}",
        "provider": "generic-chat-completion-api",
        "id": "custom:Kimi-\u6d4b\u8bd5-0",
        "index": 0,
        "enableThinking": True,
        "extraArgs": {"nested": {"a": [1, 2, {"b": None}]}},
        "somethingNew": "x",
    }
    junk = ["not an object", 7, {"model": "x", "provider": "bogus"}]
    original = {
        "model": "claude-opus-4-1",
        "reasoningEffort": "low",
        "permissionRules": {"version": 1, "rules": []},
        "customModels": [foreign, *junk],
        "afterwards": {"keep": "\u4e2d\u6587"},
    }
    put_settings(store, original)
    store.save(draft())

    on_disk = get_settings(store)
    assert {k: v for k, v in on_disk.items() if k != "customModels"} == {k: v for k, v in original.items() if k != "customModels"}
    assert on_disk["customModels"][: 1 + len(junk)] == [foreign, *junk]
    assert on_disk["customModels"][-1]["model"] == "openai/gpt-oss-120b"
    text = store.models_file.read_text(encoding="utf-8")
    assert text == json.dumps(on_disk, indent=2, ensure_ascii=False) + "\n"
    assert "\u4e2d\u6587" in text and "\\u4e2d" not in text
    assert list(on_disk) == list(original)


def test_a_provider_update_keeps_what_the_editor_does_not_own(tmp_path):
    store = make_store(tmp_path)
    original = entry(
        "https://api.example.com/v1",
        "m",
        apiKey="${K}",
        id="custom:Mine-0",
        index=3,
        enableThinking=True,
        thinkingMaxTokens=2048,
        baseModelId="claude-sonnet-4-5-20250929",
        useInRouter=True,
        future={"x": 1},
    )
    put_settings(store, {"customModels": [original]})
    (view,) = store.list()
    store.save({**view, "models": [{**view["models"][0], "displayName": "Renamed", "contextWindow": 99000}]})
    written = entries_of(store)[0]
    for name in ("id", "index", "enableThinking", "thinkingMaxTokens", "baseModelId", "useInRouter", "future"):
        assert written[name] == original[name]
    assert written["displayName"] == "Renamed" and written["maxContextLimit"] == 99000


def test_the_first_write_makes_one_backup_and_never_replaces_it(tmp_path):
    store = make_store(tmp_path)
    original = {"model": "x", "customModels": [], "other": 1}
    put_settings(store, original)
    first_bytes = store.models_file.read_bytes()
    backup = store.models_file.with_name("settings.json.wittycode-backup")

    store.save(draft())
    assert backup.read_bytes() == first_bytes
    store.save(draft(id="p2", name="Two", baseUrl="https://two.example/v1"))
    store.remove("p1")
    assert backup.read_bytes() == first_bytes
    assert sorted(p.name for p in store.models_file.parent.iterdir()) == ["settings.json", "settings.json.wittycode-backup"]


def test_no_backup_when_the_file_did_not_exist(tmp_path):
    store = make_store(tmp_path)
    store.save(draft())
    assert not store.models_file.with_name("settings.json.wittycode-backup").exists()


def test_the_file_and_its_folder_are_created(tmp_path):
    store = make_store(tmp_path)
    assert store.list() == []
    store.save(draft())
    text = store.models_file.read_text(encoding="utf-8")
    assert list(json.loads(text)) == ["customModels"]
    assert text.endswith("]\n}\n")


def test_an_empty_file_counts_as_no_settings(tmp_path):
    store = make_store(tmp_path)
    store.models_file.parent.mkdir(parents=True)
    store.models_file.write_text("  \n", encoding="utf-8")
    assert store.list() == []
    store.save(draft())
    assert len(entries_of(store)) == 1


@pytest.mark.parametrize(
    "content",
    ['{"customModels": [ // comment\n]}', '{"customModels": [}', "[1, 2]", '"text"', "\ufffd\ufffd{"],
    ids=["comment", "broken", "array-root", "string-root", "garbage"],
)
def test_an_unreadable_file_is_refused_and_not_touched(tmp_path, content):
    store = make_store(tmp_path)
    store.models_file.parent.mkdir(parents=True)
    store.models_file.write_bytes(content.encode("utf-8"))
    before = store.models_file.read_bytes()
    for action in (lambda: store.save(draft()), lambda: store.remove("p1"), store.list):
        with pytest.raises(ProviderError):
            action()
    assert store.models_file.read_bytes() == before
    assert sorted(p.name for p in store.models_file.parent.iterdir()) == ["settings.json"]
    assert not store.sidecar.exists()


def test_a_customModels_that_is_not_an_array_is_not_overwritten(tmp_path):
    store = make_store(tmp_path)
    put_settings(store, {"customModels": {"oops": True}})
    before = store.models_file.read_bytes()
    assert store.list() == []
    with pytest.raises(ProviderError):
        store.save(draft())
    assert store.models_file.read_bytes() == before


def test_a_failed_validation_writes_nothing(tmp_path):
    store = make_store(tmp_path)
    put_settings(store, {"customModels": [], "keep": 1})
    before = store.models_file.read_bytes()
    with pytest.raises(ProviderError):
        store.save(draft(name=""))
    assert store.models_file.read_bytes() == before
    assert not store.sidecar.exists()


def test_unchanged_saves_do_not_rewrite_the_file(tmp_path):
    store = make_store(tmp_path)
    saved = store.save(draft())
    mtime = store.models_file.stat().st_mtime_ns
    store.save(saved)
    assert store.models_file.stat().st_mtime_ns == mtime


# --------------------------------------------------------------------------------------------- grouping and ids


def test_entries_are_grouped_by_kind_and_address(tmp_path):
    store = make_store(tmp_path)
    put_settings(
        store,
        {
            "customModels": [
                entry("https://api.example.com/v1", "m1", "openai"),
                entry("https://API.example.com/v1/", "m2", "openai"),
                entry("https://api.example.com/v1", "m3"),
                entry("http://localhost:11434/v1", "q"),
                {"model": "br", "provider": "bedrock-converse", "bedrock": {"awsRegion": "us-west-2"}},
                {"model": "br2", "provider": "anthropic", "bedrock": {"awsRegion": "us-west-2"}},
                {"model": "br3", "provider": "bedrock-converse", "bedrock": {"awsRegion": "eu-west-1"}},
            ]
        },
    )
    views = store.list()
    assert [(v["kind"], [m["id"] for m in v["models"]]) for v in views] == [
        ("openai", ["m1", "m2"]),
        ("generic-chat-completion-api", ["m3"]),
        ("generic-chat-completion-api", ["q"]),
        ("bedrock-converse", ["br", "br2"]),
        ("bedrock-converse", ["br3"]),
    ]
    assert [v["name"] for v in views] == ["api.example.com", "api.example.com", "localhost:11434", "AWS Bedrock \u00b7 us-west-2", "AWS Bedrock \u00b7 eu-west-1"]
    assert len({v["id"] for v in views}) == 5
    assert all(v["id"].startswith("auto-") for v in views)


def test_auto_provider_ids_are_stable(tmp_path):
    first = make_store(tmp_path)
    put_settings(first, {"customModels": [entry("https://a.example/v1", "x"), entry("https://b.example/v1", "y")]})
    ids = {v["name"]: v["id"] for v in first.list()}

    reordered = make_store(tmp_path / "other")
    put_settings(reordered, {"customModels": [entry("https://b.example/v1", "y"), entry("https://a.example/v1", "x"), entry("https://c.example/v1", "z")]})
    again = {v["name"]: v["id"] for v in reordered.list()}
    assert again["a.example"] == ids["a.example"] and again["b.example"] == ids["b.example"]


def test_the_sidecar_remembers_names_and_ids(tmp_path):
    store = make_store(tmp_path)
    put_settings(store, {"customModels": [entry("https://a.example/v1", "x", "openai"), entry("https://b.example/v1", "y")]})
    store.sidecar.parent.mkdir(parents=True)
    store.sidecar.write_text(
        json.dumps(
            {
                "version": 1,
                "providers": [
                    {"id": "p-mine", "name": "Mine", "kind": "openai", "baseUrl": "https://a.example/v1/"},
                    {"id": "p-gone", "name": "Gone", "kind": "openai", "baseUrl": "https://gone.example/v1"},
                    {"id": 5, "name": "bad record"},
                ],
            }
        ),
        encoding="utf-8",
    )
    named, auto = store.list()
    assert (named["id"], named["name"]) == ("p-mine", "Mine")
    assert auto["name"] == "b.example" and auto["id"].startswith("auto-")

    store.save({**auto, "name": "Bee"})
    records = json.loads(store.sidecar.read_text(encoding="utf-8"))
    assert records["version"] == 1
    assert [r["id"] for r in records["providers"]] == ["p-mine", auto["id"]]
    assert records["providers"][1] == {"id": auto["id"], "name": "Bee", "kind": "generic-chat-completion-api", "baseUrl": "https://b.example/v1"}
    assert [v["name"] for v in store.list()] == ["Mine", "Bee"]


def test_a_broken_sidecar_only_loses_the_names(tmp_path):
    store = make_store(tmp_path)
    put_settings(store, {"customModels": [entry("https://a.example/v1", "x")]})
    store.sidecar.parent.mkdir(parents=True)
    store.sidecar.write_text("{not json", encoding="utf-8")
    (view,) = store.list()
    assert view["name"] == "a.example"


def test_saving_replaces_a_providers_entries_in_place(tmp_path):
    store = make_store(tmp_path)
    foreign = entry("https://other.example/v1", "f", "openai", apiKey="${F}")
    put_settings(
        store,
        {
            "customModels": [
                entry("https://a.example/v1", "a1", futureA=1),
                foreign,
                entry("https://a.example/v1", "a2"),
                entry("https://b.example/v1", "b1"),
            ]
        },
    )
    a, other, b = store.list()
    assert [m["id"] for m in a["models"]] == ["a1", "a2"]
    a_id = a["id"]

    store.save({**a, "keyMode": "none", "models": [*a["models"], model("a3")]})
    assert [e["model"] for e in entries_of(store)] == ["a1", "f", "a2", "a3", "b1"]
    assert entries_of(store)[0]["futureA"] == 1

    saved = store.save({**a, "keyMode": "none", "models": [model("a2")]})
    assert [e["model"] for e in entries_of(store)] == ["f", "a2", "b1"]
    assert saved["id"] == a_id
    assert entries_of(store)[0] == foreign


def test_models_with_new_ids_take_the_place_of_the_old_ones(tmp_path):
    store = make_store(tmp_path)
    foreign = entry("https://other.example/v1", "f", "openai")
    put_settings(store, {"customModels": [entry("https://a.example/v1", "old1"), foreign, entry("https://a.example/v1", "old2"), entry("https://z.example/v1", "z")]})
    (a, _, _) = store.list()
    store.save({**a, "keyMode": "none", "models": [model("new1"), model("new2")]})
    assert [e["model"] for e in entries_of(store)] == ["f", "new1", "new2", "z"]


def test_a_byte_order_mark_is_tolerated(tmp_path):
    store = make_store(tmp_path)
    store.models_file.parent.mkdir(parents=True)
    store.models_file.write_bytes(b"\xef\xbb\xbf" + json.dumps({"keep": 1, "customModels": [entry("https://a.example/v1", "m")]}).encode())
    assert [m["id"] for m in store.list()[0]["models"]] == ["m"]
    store.save(draft())
    assert get_settings(store)["keep"] == 1


def test_the_readers_of_ids_shrug_off_an_unreadable_file(tmp_path):
    store = make_store(tmp_path)
    store.models_file.parent.mkdir(parents=True)
    store.models_file.write_text("{broken", encoding="utf-8")
    assert store.custom_model_ids("anything") == []
    assert store.provider_of("custom:Anything-0") is None


def test_the_id_survives_a_change_of_address_and_kind(tmp_path):
    store = make_store(tmp_path)
    store.save(draft(id="keep-me"))
    moved = store.save(draft(id="keep-me", kind="openai", baseUrl="https://proxy.example/v1"))
    assert moved["id"] == "keep-me" and moved["kind"] == "openai"
    assert [v["id"] for v in store.list()] == ["keep-me"]
    assert [e["baseUrl"] for e in entries_of(store)] == ["https://proxy.example/v1"]
    records = json.loads(store.sidecar.read_text(encoding="utf-8"))["providers"]
    assert [r["id"] for r in records] == ["keep-me"]


def test_a_new_id_appends(tmp_path):
    store = make_store(tmp_path)
    store.save(draft())
    store.save(draft(id="p2", name="Two", baseUrl="https://two.example/v1", models=[model("t")]))
    assert [e["model"] for e in entries_of(store)] == ["openai/gpt-oss-120b", "t"]
    assert [v["name"] for v in store.list()] == ["OpenRouter", "Two"]


def test_two_providers_cannot_share_an_address(tmp_path):
    store = make_store(tmp_path)
    store.save(draft())
    before = store.models_file.read_bytes()
    with pytest.raises(ProviderError, match="OpenRouter"):
        store.save(draft(id="p2", name="Same place", baseUrl="https://openrouter.ai/api/v1/"))
    assert store.models_file.read_bytes() == before
    store.save(draft(id="p2", name="Other kind", kind="openai"))


def test_removing_a_provider(tmp_path):
    store = make_store(tmp_path)
    foreign = entry("https://x.example/v1", "x")
    put_settings(store, {"keep": True, "customModels": [foreign]})
    store.save(draft())
    store.save(draft(id="p2", name="Two", baseUrl="https://two.example/v1", models=[model("t")]))
    store.remove("p1")
    assert [e["model"] for e in entries_of(store)] == ["x", "t"]
    assert get_settings(store)["keep"] is True
    assert [r["id"] for r in json.loads(store.sidecar.read_text(encoding="utf-8"))["providers"]] == ["p2"]
    store.remove("p2")
    assert entries_of(store) == [foreign]


def test_removing_something_that_is_not_there_changes_nothing(tmp_path):
    store = make_store(tmp_path)
    store.remove("nope")
    assert not store.models_file.exists()
    put_settings(store, {"customModels": []})
    before = store.models_file.read_bytes()
    store.remove("nope")
    assert store.models_file.read_bytes() == before


# ------------------------------------------------------------------------------------------------- validation


@pytest.mark.parametrize(
    ("change", "fragment"),
    [
        ({"name": "  "}, "名字"),
        ({"kind": "gemini"}, "接口类型"),
        ({"baseUrl": ""}, "Base URL"),
        ({"baseUrl": "ftp://x.example"}, "http"),
        ({"baseUrl": "api.example.com/v1"}, "http"),
        ({"baseUrl": "https://"}, "http"),
        ({"keyMode": "magic"}, "密钥来源"),
        ({"keyMode": "command", "keyHint": " "}, "命令"),
        ({"keyMode": "command", "keyHint": "x", "keyTtlMs": -5}, "刷新间隔"),
        ({"models": []}, "至少"),
        ({"models": [model(" ")]}, "至少"),
        ({"models": [model("a"), model("a")]}, "重复"),
        ({"models": [model("a", contextWindow=0)]}, "上下文窗口"),
        ({"models": [model("a", maxOutput=-1)]}, "最大输出"),
        ({"models": [model("a", maxOutput=True)]}, "最大输出"),
        ({"models": [model("a", reasoning="extreme")]}, "推理"),
        ({"extraArgs": "{nope"}, "JSON"),
        ({"extraArgs": "[1]"}, "对象"),
        ({"extraArgs": "3"}, "对象"),
        ({"headers": [{"k": "X-A", "v": "1"}, {"k": "x-a", "v": "2"}]}, "重复"),
        ({"headers": [{"k": "bad name", "v": "1"}]}, "不合法"),
        ({"headers": [{"k": "", "v": "1"}]}, "名字"),
        ({"headers": [{"k": "X-A", "v": "1\r\nX-B: 2"}]}, "换行"),
    ],
)
def test_validation_errors_are_chinese_and_write_nothing(tmp_path, change, fragment):
    store = make_store(tmp_path)
    with pytest.raises(ProviderError) as caught:
        store.save(draft(**change))
    assert fragment in str(caught.value)
    assert any("\u4e00" <= ch <= "\u9fff" for ch in str(caught.value))
    assert not store.models_file.exists() and not store.sidecar.exists()


def test_what_the_editor_allows_is_accepted(tmp_path):
    store = make_store(tmp_path)
    store.save(draft(models=[model("a"), model(""), model("b", contextWindow=250000.0, maxOutput="")], headers=[{"k": "", "v": ""}], extraArgs="  "))
    assert [e["model"] for e in entries_of(store)] == ["a", "b"]
    assert entries_of(store)[1]["maxContextLimit"] == 250000
    assert "extraArgs" not in entries_of(store)[0]


def test_a_bedrock_draft_needs_no_url_or_key(tmp_path):
    store = make_store(tmp_path)
    store.save({**BEDROCK_DRAFT, "keyMode": "env", "keyHint": SECRET, "baseUrl": ""})
    written = entries_of(store)[0]
    assert "apiKey" not in written and "baseUrl" not in written
    assert SECRET not in store.models_file.read_text(encoding="utf-8")


def test_bearer_only_applies_to_anthropic_with_a_key(tmp_path):
    store = make_store(tmp_path)
    store.save(draft(bearer=True))
    assert "authMode" not in entries_of(store)[0]
    store.save(draft(kind="anthropic", baseUrl="https://gw.example", bearer=True, keyMode="none"))
    assert "authMode" not in entries_of(store)[0]


# --------------------------------------------------------------------------------------- Droid's model ids


@pytest.mark.parametrize(
    ("name", "ordinal", "expected"),
    [
        ("My Custom Model", 0, "custom:My-Custom-Model-0"),
        ("Kimi K2 [Groq]", 0, "custom:Kimi-K2-[Groq]-0"),
        ("  padded   name\t", 2, "custom:padded-name-2"),
        ("\u4e2d\u6587 \u6a21\u578b\u3000\u5b57", 1, "custom:\u4e2d\u6587-\u6a21\u578b-\u5b57-1"),
        ("a\u00a0b\ufeffc", 0, "custom:a-b-c-0"),
    ],
)
def test_the_id_rule(name, ordinal, expected):
    assert custom_model_id(name, ordinal) == expected


def test_distinct_names_all_end_in_zero_and_deleting_one_moves_no_other(tmp_path):
    store = make_store(tmp_path)
    put_settings(
        store,
        {
            "customModels": [
                entry("https://a/v1", "m1", displayName="Alpha One"),
                entry("https://a/v1", "m2", displayName="Beta"),
                entry("https://b/v1", "m3", displayName="Gamma"),
            ]
        },
    )
    a, b = store.list()
    assert store.custom_model_ids(a["id"]) == [("custom:Alpha-One-0", "m1"), ("custom:Beta-0", "m2")]
    assert store.custom_model_ids(b["id"]) == [("custom:Gamma-0", "m3")]

    store.save({**a, "keyMode": "none", "models": [a["models"][1]]})
    assert store.custom_model_ids(b["id"]) == [("custom:Gamma-0", "m3")]
    assert store.custom_model_ids(a["id"]) == [("custom:Beta-0", "m2")]


def test_equal_names_count_up_across_providers_and_skip_entries_droid_ignores(tmp_path):
    store = make_store(tmp_path)
    put_settings(
        store,
        {
            "customModels": [
                entry("https://a/v1", "m1", displayName="Same Name"),
                entry("https://a/v1", "ignored", "openai", displayName="Same Name", apiKey="YOUR_API_KEY"),
                entry("https://b/v1", "m2", displayName="Same  Name"),
                "junk",
                entry("https://a/v1", "m3"),
            ]
        },
    )
    a, b = store.list()
    assert store.custom_model_ids(a["id"]) == [("custom:Same-Name-0", "m1"), ("custom:m3-0", "m3")]
    assert store.custom_model_ids(b["id"]) == [("custom:Same-Name-1", "m2")]
    assert store.custom_model_ids("unknown") == []


def test_an_explicit_id_in_the_file_wins(tmp_path):
    store = make_store(tmp_path)
    put_settings(store, {"customModels": [entry("https://a/v1", "m1", id="custom:Chosen-7"), entry("https://a/v1", "m2")]})
    (view,) = store.list()
    assert store.custom_model_ids(view["id"]) == [("custom:Chosen-7", "m1"), ("custom:m2-0", "m2")]
    assert store.provider_of("custom:Chosen-7") == view["id"]


def test_provider_of(tmp_path):
    store = make_store(tmp_path)
    put_settings(
        store,
        {
            "customModels": [
                entry("https://a/v1", "m1", displayName="Alpha"),
                entry("https://b/v1", "m2", displayName="Beta"),
                entry("https://b/v1", "m3", displayName="Beta"),
            ]
        },
    )
    a, b = store.list()
    assert store.provider_of("custom:Alpha-0") == a["id"]
    assert store.provider_of("custom:Beta-0") == b["id"]
    assert store.provider_of("custom:Beta-1") == b["id"]
    assert store.provider_of("claude-sonnet-4-5-20250929") is None
    assert store.provider_of("custom:Nothing-0") is None
    assert store.provider_of("custom:Nothing-0", "Nothing") is None
    # the older position-based id and the bare-model form that Droid still resolves
    assert store.provider_of("custom:Beta-2") == b["id"]
    assert store.provider_of("custom:m1") == a["id"]
    # a display name that points at exactly one entry
    assert store.provider_of("something-else", "Alpha") == a["id"]
    assert store.provider_of("something-else", "Beta") is None


def test_window_of_reads_the_context_window_the_user_gave_a_model_by_any_id_droid_accepts(tmp_path):
    store = make_store(tmp_path)
    put_settings(
        store,
        {
            "customModels": [
                entry("https://a/v1", "m1", displayName="Alpha", maxContextLimit=64000),
                entry("https://b/v1", "m2", displayName="Beta"),
                entry("https://b/v1", "m3", displayName="Beta", maxContextLimit=-5),
                entry("https://b/v1", "m4", displayName="Gamma", maxContextLimit=128000),
            ]
        },
    )
    assert store.window_of("custom:Alpha-0") == 64000
    assert store.window_of("custom:m1") == 64000, "the bare-model form Droid still resolves"
    assert store.window_of("custom:Gamma-0") == 128000
    assert store.window_of("custom:Beta-0") is None, "no limit given"
    assert store.window_of("custom:Beta-1") is None, "a limit that is not a positive number is none"
    assert store.window_of("claude-sonnet-4-5-20250929") is None and store.window_of("custom:Nothing-0") is None
    assert store.window_of("something-else", "Alpha") == 64000, "a display name that points at exactly one entry"


def test_window_of_is_none_when_the_file_cannot_be_read(tmp_path):
    store = make_store(tmp_path)
    store.models_file.parent.mkdir(parents=True, exist_ok=True)
    store.models_file.write_text("{not json", encoding="utf-8")
    assert store.window_of("custom:Alpha-0") is None and store.provider_of("custom:Alpha-0") is None


def test_saved_models_get_the_ids_droid_will_use(tmp_path):
    store = make_store(tmp_path)
    store.save(draft(models=[model("m1", displayName="First model"), model("m2")]))
    assert store.custom_model_ids("p1") == [("custom:First-model-0", "m1"), ("custom:m2-0", "m2")]
    assert store.provider_of("custom:First-model-0") == "p1"


# ------------------------------------------------------------------------------------------------ environment


def test_env_status():
    store = ProviderStore(
        models_file=Path("unused.json"),
        sidecar=Path("unused-sidecar.json"),
        environ={"SET_VAR": "x", "EMPTY": ""},
        registry_env=lambda: {"NEW_VAR": "y", "EMPTY": "z", "BLANK": ""},
    )
    assert store.env_status(["SET_VAR", "NEW_VAR", "NOPE", "EMPTY", "BLANK"]) == {
        "SET_VAR": "set",
        "NEW_VAR": "pending",
        "NOPE": "unset",
        "EMPTY": "pending",
        "BLANK": "unset",
    }


def test_env_status_reads_the_registry_only_when_needed():
    calls = []
    store = ProviderStore(
        models_file=Path("unused.json"),
        sidecar=Path("unused-sidecar.json"),
        environ={"A": "1"},
        registry_env=lambda: calls.append(1) or {},
    )
    assert store.env_status(["A"]) == {"A": "set"} and calls == []
    store.env_status(["A", "B", "C"])
    assert calls == [1]


def test_a_broken_registry_reads_as_unset():
    def boom():
        raise OSError("no registry")

    store = ProviderStore(models_file=Path("unused.json"), sidecar=Path("unused-sidecar.json"), environ={}, registry_env=boom)
    assert store.env_status(["X"]) == {"X": "unset"}


def test_the_real_registry_reader_returns_a_mapping_of_text():
    values = providers_module._registry_environment()
    assert isinstance(values, dict)
    assert all(isinstance(k, str) and isinstance(v, str) for k, v in values.items())


def test_the_models_file_sits_next_to_the_settings_and_its_location_can_be_overridden(tmp_path, monkeypatch):
    target = tmp_path / "elsewhere" / "models.json"
    store = ProviderStore(sidecar=tmp_path / "providers.json", environ={MODELS_FILE_ENV: str(target)})
    assert store.models_file == target
    monkeypatch.setenv("WITTYCODE_SETTINGS", str(tmp_path / "wc" / "settings.json"))
    monkeypatch.setattr(Path, "home", lambda: tmp_path / "home")
    default = ProviderStore(sidecar=tmp_path / "providers.json", environ={})
    assert default.models_file == tmp_path / "wc" / "models.json"
    assert not default.models_file.exists(), "nothing to copy from an older install: no file is made"


def test_another_programs_settings_are_never_read(tmp_path, monkeypatch):
    legacy = tmp_path / "home" / ".factory" / "settings.json"
    legacy.parent.mkdir(parents=True)
    legacy.write_text(json.dumps({"customModels": [{"model": "m", "baseUrl": "https://example.com/v1"}]}), encoding="utf-8")
    monkeypatch.setenv("WITTYCODE_SETTINGS", str(tmp_path / "wc" / "settings.json"))
    monkeypatch.setattr(Path, "home", lambda: tmp_path / "home")
    store = ProviderStore(sidecar=tmp_path / "providers.json", environ={})
    assert store.list() == [] and not store.models_file.exists(), "WittyCode's own file only: nothing is copied from elsewhere"


def test_default_sidecar_sits_next_to_the_app_settings(tmp_path, monkeypatch):
    monkeypatch.setenv("WITTYCODE_SETTINGS", str(tmp_path / "wc" / "settings.json"))
    store = ProviderStore(models_file=tmp_path / "f.json", environ={})
    assert store.sidecar == tmp_path / "wc" / "providers.json"


# ----------------------------------------------------------------------------------------- test / list models


def conn(**over) -> dict:
    return draft(**over)


def test_a_successful_test_lists_the_models():
    opener = Opener({"data": [{"id": "gpt-a"}, {"id": "gpt-b"}, {"id": "gpt-a"}, {"object": "x"}]})
    store = ProviderStore(models_file=Path("u.json"), sidecar=Path("u2.json"), environ={"OPENROUTER_API_KEY": SECRET}, opener=opener)
    result = run(store.test(conn(headers=[{"k": "X-Team", "v": "core"}])))
    assert result["ok"] is True and result["models"] == ["gpt-a", "gpt-b"]
    assert isinstance(result["ms"], int)
    assert result["text"] == f"连接成功 · {result['ms']} ms · 2 个模型"
    request = opener.requests[0]
    assert request.full_url == "https://openrouter.ai/api/v1/models" and request.get_method() == "GET"
    assert opener.headers["authorization"] == f"Bearer {SECRET}"
    assert opener.headers["x-team"] == "core"
    assert opener.timeouts == [10.0]
    assert SECRET not in json.dumps(result, ensure_ascii=False)


def test_a_server_without_a_model_list_still_connects():
    store = ProviderStore(models_file=Path("u.json"), sidecar=Path("u2.json"), environ={"OPENROUTER_API_KEY": "k"}, opener=Opener(b"<html>hello</html>"))
    result = run(store.test(conn()))
    assert result["ok"] is True and result["models"] is None
    assert result["text"] == f"连接成功 · {result['ms']} ms"


def test_other_list_shapes(tmp_path):
    for body, expected in [
        ([{"id": "a"}, {"id": "b"}], ["a", "b"]),
        ({"models": [{"name": "models/gemini-2.5-pro"}, {"name": "models/g2"}]}, ["gemini-2.5-pro", "g2"]),
        ({"data": []}, []),
    ]:
        store = ProviderStore(models_file=Path("u.json"), sidecar=Path("u2.json"), environ={"OPENROUTER_API_KEY": "k"}, opener=Opener(body))
        assert run(store.list_models(conn())) == expected


def anthropic_draft(**over) -> dict:
    return conn(**{"kind": "anthropic", "baseUrl": "https://api.anthropic.com", "keyHint": "${ANTHROPIC_API_KEY}", **over})


def test_anthropic_requests():
    opener = Opener({"data": [{"id": "claude-x"}]})
    store = ProviderStore(models_file=Path("u.json"), sidecar=Path("u2.json"), environ={"ANTHROPIC_API_KEY": SECRET}, opener=opener)
    assert run(store.test(anthropic_draft()))["models"] == ["claude-x"]
    assert opener.requests[-1].full_url == "https://api.anthropic.com/v1/models"
    assert opener.headers["x-api-key"] == SECRET and "authorization" not in opener.headers
    assert opener.headers["anthropic-version"] == "2023-06-01"

    run(store.test(anthropic_draft(baseUrl="https://gw.example/anthropic/v1/")))
    assert opener.requests[-1].full_url == "https://gw.example/anthropic/v1/models"

    run(store.test(anthropic_draft(bearer=True)))
    assert opener.headers["authorization"] == f"Bearer {SECRET}" and "x-api-key" not in opener.headers
    assert opener.headers["anthropic-version"] == "2023-06-01"


def test_a_keyless_provider_sends_no_credentials():
    opener = Opener({"data": []})
    store = ProviderStore(models_file=Path("u.json"), sidecar=Path("u2.json"), environ={}, opener=opener)
    result = run(store.test(conn(baseUrl="http://localhost:11434/v1", keyMode="none", keyHint="")))
    assert result["ok"] is True
    assert "authorization" not in opener.headers and "x-api-key" not in opener.headers
    assert opener.requests[0].full_url == "http://localhost:11434/v1/models"


@pytest.mark.parametrize(
    ("error", "text"),
    [
        (urllib.error.HTTPError("u", 401, "Unauthorized", {}, None), "401：密钥无效"),
        (urllib.error.HTTPError("u", 403, "Forbidden", {}, None), "403：没有权限，密钥无效或没有开通这个接口"),
        (urllib.error.HTTPError("u", 404, "Not Found", {}, None), "404：没有找到 /models 接口，Base URL 可能不对"),
        (urllib.error.HTTPError("u", 429, "Too Many", {}, None), "429：请求太频繁，或者额度用完了"),
        (urllib.error.HTTPError("u", 503, "Unavailable", {}, None), "503：服务端出错了"),
        (urllib.error.HTTPError("u", 418, "I'm a teapot", {}, None), "418：I'm a teapot"),
        (urllib.error.URLError(ConnectionRefusedError(10061, "refused")), "连不上 localhost:11434：服务没有运行？"),
        (urllib.error.URLError(OSError("network is unreachable")), "连不上 localhost:11434：网络错误（OSError）"),
        (TimeoutError(), "连不上 localhost:11434：10 秒内没有响应"),
        (urllib.error.URLError(TimeoutError("timed out")), "连不上 localhost:11434：10 秒内没有响应"),
        (urllib.error.URLError(socket.gaierror(11001, "no such host")), "连不上 localhost:11434：找不到这个域名"),
        (urllib.error.URLError(__import__("ssl").SSLCertVerificationError("bad cert")), "localhost:11434 的 HTTPS 证书没有通过验证"),
        (ConnectionResetError("reset"), "连不上 localhost:11434：网络错误（ConnectionResetError）"),
    ],
)
def test_failures_are_one_line_chinese_reasons(error, text):
    store = ProviderStore(models_file=Path("u.json"), sidecar=Path("u2.json"), environ={}, opener=Opener(error=error))
    result = run(store.test(conn(baseUrl="http://localhost:11434/v1", keyMode="none", keyHint="")))
    assert result == {"ok": False, "text": text, "ms": None, "models": None}
    assert run(store.list_models(conn(baseUrl="http://localhost:11434/v1", keyMode="none", keyHint=""))) == []


def test_an_unexpected_error_is_reported_without_its_message():
    store = ProviderStore(models_file=Path("u.json"), sidecar=Path("u2.json"), environ={"OPENROUTER_API_KEY": SECRET}, opener=Opener(error=RuntimeError(f"boom {SECRET}")))
    result = run(store.test(conn()))
    assert result["ok"] is False and "RuntimeError" in result["text"] and SECRET not in json.dumps(result)


def test_garbage_drafts_never_raise():
    store = ProviderStore(models_file=Path("u.json"), sidecar=Path("u2.json"), environ={}, opener=Opener({"data": []}))
    assert run(store.test("nope"))["ok"] is False
    assert run(store.test({}))["ok"] is False
    assert run(store.list_models({"kind": 5})) == []
    assert run(store.test(conn(baseUrl="ftp://x")))["text"] == "Base URL 要以 http:// 或 https:// 开头"


def test_bedrock_cannot_be_tested_here(tmp_path):
    opener = Opener({"data": []})
    store = make_store(tmp_path, opener=opener)
    result = run(store.test(BEDROCK_DRAFT))
    assert result["ok"] is False and result["text"] == BEDROCK_TEST_NOTE == "Bedrock 暂不支持在这里测试连接，保存后在会话里试一下。"
    assert run(store.list_models(BEDROCK_DRAFT)) == []
    assert opener.requests == []


def test_an_unset_variable_stops_the_test_before_any_request():
    opener = Opener({"data": []})
    store = ProviderStore(models_file=Path("u.json"), sidecar=Path("u2.json"), environ={}, registry_env=lambda: {}, opener=opener)
    result = run(store.test(conn()))
    assert result["text"] == "环境变量 OPENROUTER_API_KEY 没有设置，请求没有带密钥" and result["ok"] is False
    assert opener.requests == []


def test_a_variable_set_after_the_app_started_asks_for_a_restart():
    opener = Opener({"data": []})
    store = ProviderStore(models_file=Path("u.json"), sidecar=Path("u2.json"), environ={}, registry_env=lambda: {"OPENROUTER_API_KEY": SECRET}, opener=opener)
    result = run(store.test(conn()))
    assert result["text"] == "环境变量 OPENROUTER_API_KEY 是在 WittyCode 启动之后设置的：重启 WittyCode 再试"
    assert opener.requests == [] and SECRET not in json.dumps(result)


def test_header_variables_are_expanded_or_reported():
    opener = Opener({"data": []})
    store = ProviderStore(models_file=Path("u.json"), sidecar=Path("u2.json"), environ={"TEAM": "core"}, opener=opener)
    d = conn(keyMode="none", keyHint="", headers=[{"k": "X-Team", "v": "t-${TEAM}"}])
    assert run(store.test(d))["ok"] is True and opener.headers["x-team"] == "t-core"
    result = run(store.test({**d, "headers": [{"k": "X-Team", "v": "${MISSING}"}]}))
    assert result["ok"] is False and "MISSING" in result["text"] and "X-Team" in result["text"]


def test_a_command_key_is_run_and_sent():
    asked = []
    opener = Opener({"data": [{"id": "m"}]})
    store = ProviderStore(
        models_file=Path("u.json"),
        sidecar=Path("u2.json"),
        environ={},
        opener=opener,
        run_command=lambda command: asked.append(command) or SECRET,
    )
    result = run(store.test(conn(keyMode="command", keyHint="mint-token.cmd --scope x")))
    assert result["ok"] is True and asked == ["mint-token.cmd --scope x"]
    assert opener.headers["authorization"] == f"Bearer {SECRET}"
    assert SECRET not in json.dumps(result)


def test_a_failing_key_command_is_a_test_failure():
    def failing(command: str) -> str:
        raise ProviderError("取密钥的命令失败了（退出码 2）。")

    opener = Opener({"data": []})
    store = ProviderStore(models_file=Path("u.json"), sidecar=Path("u2.json"), environ={}, opener=opener, run_command=failing)
    result = run(store.test(conn(keyMode="command", keyHint="x")))
    assert result["text"] == "取密钥的命令失败了（退出码 2）" and opener.requests == []


def test_the_stored_inline_key_is_used_for_a_test_but_never_returned(tmp_path):
    opener = Opener({"data": [{"id": "m"}]})
    store = make_store(tmp_path, opener=opener)
    legacy_settings(store)
    (view,) = store.list()
    result = run(store.test(view))
    assert result["ok"] is True
    assert opener.headers["authorization"] == f"Bearer {SECRET}"
    assert SECRET not in json.dumps(result, ensure_ascii=False)
    unknown = run(store.test({**view, "id": "not-saved"}))
    assert unknown["ok"] is False and SECRET not in json.dumps(unknown)


def test_a_stored_secret_header_is_used_for_a_test_but_never_returned(tmp_path):
    opener = Opener({"data": []})
    store = make_store(tmp_path, opener=opener)
    put_settings(store, {"customModels": [entry("https://gw/v1", "m", extraHeaders={"Authorization": f"Bearer {SECRET}"})]})
    (view,) = store.list()
    result = run(store.test({**view, "keyMode": "none", "keyHint": ""}))
    assert result["ok"] is True and opener.headers["authorization"] == f"Bearer {SECRET}"
    assert SECRET not in json.dumps(result, ensure_ascii=False)


# ---------------------------------------------------------------------------------------------- key commands


def python_command(tmp_path: Path, source: str) -> str:
    script = tmp_path / "helper.py"
    script.write_text(source, encoding="utf-8")
    return f'"{sys.executable}" "{script}"'


def test_the_real_key_command_runner(tmp_path):
    assert run_key_command(python_command(tmp_path, "print('  tok-123  ')")) == "tok-123"
    with pytest.raises(ProviderError, match="退出码 3"):
        run_key_command(python_command(tmp_path, "import sys; sys.exit(3)"))
    with pytest.raises(ProviderError, match="没有输出"):
        run_key_command(python_command(tmp_path, "pass"))


def test_the_key_command_runner_never_echoes_output(tmp_path):
    source = f"import sys; print({SECRET!r}); print({SECRET!r}, file=sys.stderr); sys.exit(1)"
    with pytest.raises(ProviderError) as caught:
        run_key_command(python_command(tmp_path, source))
    assert SECRET not in str(caught.value)


def test_a_key_command_that_hangs_is_stopped(tmp_path, monkeypatch):
    monkeypatch.setattr(providers_module, "COMMAND_TIMEOUT", 0.5)
    with pytest.raises(ProviderError, match="没有返回"):
        run_key_command(python_command(tmp_path, "import time; time.sleep(30)"))


# ------------------------------------------------------------------------------ no secret in any returned value


def test_no_returned_value_holds_the_secret(tmp_path):
    opener = Opener({"data": [{"id": "m"}]})
    store = make_store(tmp_path, environ={"OPENAI_API_KEY": SECRET, "OTHER": SECRET}, opener=opener)
    legacy_settings(store)
    put_settings(
        store,
        {
            "customModels": [
                *json.loads(store.models_file.read_text(encoding="utf-8"))["customModels"],
                entry("https://gw/v1", "g", extraHeaders={"Authorization": f"Bearer {SECRET}"}),
            ]
        },
    )
    seen: list[object] = []
    seen.append(store.list())
    for view in store.list():
        seen.append(store.save(view))
        seen.append(run(store.test(view)))
        seen.append(run(store.list_models(view)))
    seen.append(store.env_status(["OPENAI_API_KEY", "NOPE"]))
    seen.append([store.custom_model_ids(v["id"]) for v in store.list()])
    seen.append(run(store.test(draft(keyHint="${OPENAI_API_KEY}"))))
    text = json.dumps(seen, ensure_ascii=False)
    assert SECRET not in text and SECRET[3:-4] not in text


# ------------------------------------------------------------------------------------------------ the figures of a model


def test_the_window_and_the_answer_room_the_user_gave_a_model_are_read_back(tmp_path):
    store = make_store(tmp_path)
    put_settings(store, {"customModels": [entry("https://a.example/v1", "big", maxContextLimit=1_024_000, maxOutputTokens=128_000), entry("https://a.example/v1", "plain")]})
    ids = {native: custom_id for custom_id, native in store.custom_model_ids(store.list()[0]["id"])}
    assert store.limits_of(ids["big"]) == (1_024_000, 128_000) and store.window_of(ids["big"]) == 1_024_000
    assert store.limits_of(ids["plain"]) == (None, None) and store.window_of(ids["plain"]) is None
    assert store.limits_of("custom:nobody-0") == (None, None)


def test_a_figure_that_is_not_a_positive_whole_number_is_no_figure(tmp_path):
    store = make_store(tmp_path)
    put_settings(store, {"customModels": [entry("https://a.example/v1", "odd", maxContextLimit="big", maxOutputTokens=-5)]})
    ((custom_id, _),) = store.custom_model_ids(store.list()[0]["id"])
    assert store.limits_of(custom_id) == (None, None)


# ------------------------------------------------------------------------------------------------ what a server's list says of the models


def test_the_figures_servers_put_in_their_model_lists_are_found_wherever_they_keep_them():
    from desktop.core.providers import _model_details

    assert _model_details({"data": [
        {"id": "openrouter/x", "context_length": 131_072, "top_provider": {"max_completion_tokens": 16_384}},  # OpenRouter
        {"id": "claude-x", "max_input_tokens": 200_000, "max_tokens": 64_000},  # Anthropic
        {"id": "plain-openai", "object": "model", "owned_by": "openai"},  # OpenAI: ids only
        {"id": "vllm-x", "max_model_len": 32_768},
        {"id": "mistral-x", "max_context_length": 262_144},
    ]}) == [
        {"id": "openrouter/x", "contextWindow": 131_072, "maxOutput": 16_384},
        {"id": "claude-x", "contextWindow": 200_000, "maxOutput": 64_000},
        {"id": "vllm-x", "contextWindow": 32_768},
        {"id": "mistral-x", "contextWindow": 262_144},
    ]
    assert _model_details({"models": [{"name": "models/gemini-x", "inputTokenLimit": 1_048_576, "outputTokenLimit": 65_536}]}) == [
        {"id": "gemini-x", "contextWindow": 1_048_576, "maxOutput": 65_536}
    ]


def test_an_answer_limit_that_is_as_big_as_the_window_is_not_an_answer_limit_and_junk_is_ignored():
    from desktop.core.providers import _model_details

    assert _model_details({"data": [{"id": "a", "context_window": 8_000, "max_tokens": 8_000}]}) == [{"id": "a", "contextWindow": 8_000}]
    assert _model_details({"data": [{"id": "b", "context_window": "lots"}, {"id": "b", "context_window": 5}, {"id": "b", "context_window": 6}, 7, None, {"context_window": 9}]}) == [
        {"id": "b", "contextWindow": 5}
    ]
    assert _model_details("nope") == [] and _model_details({"data": "nope"}) == []


def test_the_page_gets_every_model_the_server_lists_with_the_figures_it_gave_for_some(tmp_path):
    opener = Opener({"data": [{"id": "with", "context_length": 64_000, "max_completion_tokens": 4_096}, {"id": "without"}]})
    store = ProviderStore(models_file=tmp_path / "f.json", sidecar=tmp_path / "s.json", environ={"OPENROUTER_API_KEY": "k"}, opener=opener)
    assert run(store.model_details(draft())) == [{"id": "with", "contextWindow": 64_000, "maxOutput": 4_096}, {"id": "without"}]
    broken = ProviderStore(models_file=tmp_path / "f.json", sidecar=tmp_path / "s.json", environ={"OPENROUTER_API_KEY": "k"}, opener=Opener(error=ConnectionRefusedError()))
    assert run(broken.model_details(draft())) == []


# ------------------------------------------------------------------------------------------------ concurrency


def test_concurrent_saves_do_not_lose_updates(tmp_path):
    store = make_store(tmp_path)
    errors: list[BaseException] = []

    def work(n: int) -> None:
        try:
            store.save(draft(id=f"p{n}", name=f"P{n}", baseUrl=f"https://h{n}.example/v1", models=[model(f"m{n}")]))
        except BaseException as error:  # noqa: BLE001 - surfaced below
            errors.append(error)

    threads = [threading.Thread(target=work, args=(n,)) for n in range(10)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert not errors
    assert sorted(e["model"] for e in entries_of(store)) == sorted(f"m{n}" for n in range(10))
    assert sorted(v["name"] for v in store.list()) == sorted(f"P{n}" for n in range(10))
