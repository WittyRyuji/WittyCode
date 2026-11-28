"""Direct-entry credentials: only DPAPI ciphertext persists, and only their endpoint can use them."""
from __future__ import annotations

import json
import os
import sys
from urllib.error import HTTPError

import pytest

from desktop.core import secret_protection
from desktop.core.chatgpt.errors import ChatGPTError, RequestNotSent
from desktop.core.chatgpt.responses import Finished, TextDelta, TextDone
from desktop.core.llm.backend import ApiBackend
from desktop.core.provider_secrets import PROTECTED_KEY_FIELD, PROTECTED_KEYS_FIELD, seal
from desktop.core.providers import BACKUP_SUFFIX, ProviderError
from tests.fakes import until
from tests.llm_support import FakeApi, http_error
from tests.test_app_api import api_core, api_model_id, blocks, chunk, new_session
from tests.test_llm_backend import ask, request_for
from tests.test_providers import BEDROCK_DRAFT, Opener, draft, entries_of, make_store, model, put_settings, run

KEY = "demo-" + "provider-credential-for-tests"
NEXT_KEY = "demo-" + "provider-credential-replacement"
pytestmark = pytest.mark.skipif(sys.platform != "win32", reason="Windows current-user DPAPI")


def direct(**changes):
    return draft(**{"name": "Demo", "baseUrl": "https://example.test/v1", "keyMode": "protected", "keyHint": "",
                    "newApiKey": KEY, "models": [model("demo-chat", contextWindow=64000)], **changes})


def test_dpapi_save_and_reopen_without_plaintext_or_ciphertext_in_the_view(tmp_path):
    store = make_store(tmp_path)
    saved = store.save(direct())
    assert saved["keyMode"] == "protected" and saved["keyHint"] == ""
    assert "newApiKey" not in saved and KEY not in json.dumps(saved)
    (row,) = entries_of(store)
    blob = row[PROTECTED_KEY_FIELD]
    assert blob and blob not in json.dumps(saved)
    assert set(row).isdisjoint({"apiKey", "apiKeyHelper", "apiKeyHelperTtlMs", "newApiKey"})
    assert KEY not in store.models_file.read_text() and KEY not in store.sidecar.read_text()
    assert KEY not in json.dumps(dict(os.environ))
    assert store.secret_environment_names() == ()

    reopened = make_store(tmp_path)
    assert reopened.list() == [saved]
    (spec,) = reopened.api_models()
    assert KEY not in repr(spec) and blob not in repr(spec)
    assert reopened.api_key(spec) == KEY

    reopened.save({**saved, "name": "Renamed", "newApiKey": "", "models": [*saved["models"], model("added")]})
    assert [r[PROTECTED_KEY_FIELD] for r in entries_of(reopened)] == [blob, blob]
    backup = store.models_file.with_name(store.models_file.name + BACKUP_SUFFIX)
    assert PROTECTED_KEY_FIELD not in backup.read_text() and KEY not in backup.read_text()
    updated = reopened.save({**reopened.list()[0], "newApiKey": NEXT_KEY})
    assert NEXT_KEY not in json.dumps(updated) and NEXT_KEY not in reopened.models_file.read_text()
    assert [reopened.api_key(spec) for spec in reopened.api_models()] == [NEXT_KEY, NEXT_KEY]
    reopened.remove(saved["id"])
    assert reopened.list() == [] and entries_of(reopened) == []
    assert not run(reopened.test(saved))["ok"]


def test_named_keys_are_write_only_and_can_be_kept_renamed_or_removed(tmp_path, monkeypatch):
    opener = Opener({"data": []})
    store = make_store(tmp_path, opener=opener)
    pending = direct(newApiKey="", newApiKeys=[{"name": "Primary", "key": KEY}, {"name": "Secondary", "key": NEXT_KEY}],
                     models=[model("a"), model("b", selectedKeyIndex=1)])
    saved = store.save(pending)
    assert saved["availableKeys"] == [{"name": "Primary"}, {"name": "Secondary"}]
    assert run(store.test(saved))["ok"] and opener.headers["authorization"] == "Bearer " + KEY
    assert [store.api_key(spec) for spec in store.api_models()] == [KEY, NEXT_KEY]
    row = entries_of(store)[0]
    packed = row[PROTECTED_KEYS_FIELD]
    assert PROTECTED_KEY_FIELD not in row and packed not in json.dumps(saved)
    for key in (KEY, NEXT_KEY):
        assert key not in json.dumps(saved) and key not in store.models_file.read_text(encoding="utf-8")
    # Showing settings/model metadata does not decrypt keys; a damaged key cannot select a different one.
    broken = json.loads(packed)
    broken[1]["data"] = "invalid"
    second = entries_of(store)[1]
    put_settings(store, {"customModels": [row, {**second, PROTECTED_KEYS_FIELD: json.dumps(broken)}]})
    with monkeypatch.context() as no_decrypt:
        no_decrypt.setattr(secret_protection, "crypt", lambda *a, **kw: pytest.fail("decrypted metadata"))
        assert store.list() == [saved]
        specs = store.api_models()
    assert store.api_key(specs[0]) == KEY
    with pytest.raises(ProviderError, match="无法读取"):
        store.api_key(specs[1])
    changed = {**saved, "newApiKeys": [{"name": "Renamed", "storedIndex": 1}], "models": [model("a"), model("b")]}
    with pytest.raises(ProviderError, match="已更改"):
        store.save({**changed, "baseUrl": "https://different.test/v1"})
    updated = store.save(changed)
    assert updated["availableKeys"] == [{"name": "Renamed"}]
    assert [store.api_key(spec) for spec in store.api_models()] == [NEXT_KEY, NEXT_KEY]
    assert json.loads(entries_of(store)[0][PROTECTED_KEYS_FIELD])[0]["data"] == json.loads(packed)[1]["data"]
    backup = store.models_file.with_name(store.models_file.name + BACKUP_SUFFIX)
    assert PROTECTED_KEYS_FIELD not in backup.read_text(encoding="utf-8")


@pytest.mark.parametrize("mode, hint, expected", [
    ("env", "${DEMO_KEY}", "from-env"), ("none", "", None),
])
def test_switch_source_removes_old_protected_key_and_switching_back_needs_input(tmp_path, mode, hint, expected):
    store = make_store(tmp_path, environ={"DEMO_KEY": "from-env"})
    saved = store.save(direct())
    changed = store.save({**saved, "keyMode": mode, "keyHint": hint})
    assert PROTECTED_KEY_FIELD not in entries_of(store)[0]
    assert store.api_key(store.api_models()[0]) == expected
    with pytest.raises(ProviderError, match="输入 API Key"):
        store.save({**changed, "keyMode": "protected"})


def test_switching_to_bedrock_drops_the_protected_http_key(tmp_path):
    store = make_store(tmp_path)
    saved = store.save(direct())
    changed = store.save({**BEDROCK_DRAFT, "id": saved["id"], "newApiKey": KEY})
    assert changed["keyMode"] == "none"
    assert PROTECTED_KEY_FIELD not in entries_of(store)[0] and KEY not in store.models_file.read_text()


@pytest.mark.parametrize("kind, bearer, header, prefix", [
    ("openai", False, "authorization", "Bearer "), ("anthropic", False, "x-api-key", ""),
    ("anthropic", True, "authorization", "Bearer "),
])
def test_unsaved_and_saved_probe_and_model_list_use_the_same_key(tmp_path, kind, bearer, header, prefix):
    opener = Opener({"data": [{"id": "demo-chat", "context_window": 64000}, {"id": "echo-" + KEY}]})
    store = make_store(tmp_path, opener=opener)
    pending = direct(kind=kind, bearer=bearer)
    result = run(store.test(pending))
    assert result["ok"] and result["models"] == ["demo-chat"] and KEY not in json.dumps(result)
    assert run(store.model_details(pending)) == [{"id": "demo-chat", "contextWindow": 64000, "contextSource": "server"}]
    assert not store.models_file.exists() and not store.sidecar.exists()
    assert opener.headers[header] == prefix + KEY
    saved = store.save(pending)
    assert run(store.test(saved))["ok"] and opener.headers[header] == prefix + KEY
    before = store.models_file.read_bytes()
    opener.body = {"data": ["demo-chat", "echo-" + NEXT_KEY]}
    assert run(store.list_models({**saved, "newApiKey": NEXT_KEY})) == ["demo-chat"]
    assert opener.headers[header] == prefix + NEXT_KEY and store.models_file.read_bytes() == before


def test_endpoint_edit_requires_explicit_key_for_save_test_and_fetch(tmp_path):
    opener = Opener({"data": [{"id": "demo-chat"}]})
    store = make_store(tmp_path, opener=opener)
    saved = store.save(direct())
    for edit in ({"baseUrl": "https://different.test/v1"}, {"baseUrl": "https://example.test/new"}, {"kind": "openai"}):
        moved = {**saved, **edit}
        with pytest.raises(ProviderError, match="已更改"):
            store.save(moved)
        assert "已更改" in run(store.test(moved))["text"]
        assert run(store.model_details(moved)) == []
    assert not opener.requests
    moved = {**saved, "baseUrl": "https://different.test/v1", "newApiKey": NEXT_KEY}
    assert run(store.test(moved))["ok"] and opener.headers["authorization"] == "Bearer " + NEXT_KEY
    assert store.save(moved)["baseUrl"] == moved["baseUrl"]


def test_protected_key_cannot_use_a_custom_host_header(tmp_path):
    opener = Opener({"data": []})
    store = make_store(tmp_path, opener=opener)
    pending = direct(headers=[{"k": "hOsT", "v": "other.example.test"}])
    with pytest.raises(ProviderError, match="Host"):
        store.save(pending)
    assert not run(store.test(pending))["ok"] and not opener.requests
    store.save(direct())
    (row,) = entries_of(store)
    put_settings(store, {"customModels": [{**row, "extraHeaders": {"HOST": "other.example.test"}}]})
    with pytest.raises(ProviderError, match="Host"):
        store.api_key(store.api_models()[0])


@pytest.mark.parametrize("kind, bearer, header", [
    ("openai", False, "AuThOrIzAtIoN"), ("anthropic", False, "X-API-KEY"),
    ("anthropic", True, "Api-Key"), ("generic-chat-completion-api", False, "X-Goog-Api-Key"),
])
def test_direct_entry_rejects_auth_headers_for_save_probe_fetch_and_runtime(tmp_path, kind, bearer, header):
    opener = Opener({"data": []})
    with FakeApi() as api:
        store = make_store(tmp_path, opener=opener, environ={"ALTERNATE_KEY": "demo-other"})
        config = direct(kind=kind, baseUrl=api.base_url, bearer=bearer)
        conflict = {**config, "headers": [{"k": header, "v": "${ALTERNATE_KEY}"}]}
        assert "Authorization" in run(store.test(conflict))["text"]
        for call in (store.list_models, store.model_details):
            with pytest.raises(ProviderError, match="Authorization"):
                run(call(conflict))
        with pytest.raises(ProviderError, match="Authorization"):
            store.save(conflict)
        assert not store.models_file.exists() and not opener.requests

        store.save(config)
        (row,) = entries_of(store)
        put_settings(store, {"customModels": [{**row, "extraHeaders": {header: "${ALTERNATE_KEY}"}}]})
        backend = ApiBackend(store)
        with pytest.raises(RequestNotSent, match="Authorization") as error:
            run(ask(backend, request_for(backend.models()[0].id)))
        assert KEY not in str(error.value) and not api.posts


@pytest.mark.parametrize("mode, hint", [("env", "${PRIMARY_KEY}"), ("none", "")])
def test_other_key_modes_still_allow_custom_auth_headers(tmp_path, mode, hint):
    opener = Opener({"data": []})
    store = make_store(tmp_path, opener=opener, environ={"PRIMARY_KEY": "primary", "OVERRIDE_KEY": "demo-header"})
    saved = store.save(draft(keyMode=mode, keyHint=hint, headers=[{"k": "Authorization", "v": "Bearer ${OVERRIDE_KEY}"}]))
    assert run(store.test(saved))["ok"] and opener.headers["authorization"] == "Bearer demo-header"
    assert run(ApiBackend(store).route(store.api_models()[0])).headers["Authorization"] == "Bearer demo-header"


def test_dpapi_failure_never_persists_plaintext_or_falls_back_to_other_sources(tmp_path, monkeypatch, caplog):
    opener = Opener({"data": []})
    store = make_store(tmp_path, opener=opener)
    saved = store.save(direct())
    old_config = store.models_file.read_bytes()
    old_sidecar = store.sidecar.read_bytes()

    def unavailable(*args, **kwargs):
        raise secret_protection.ProtectionError("failure detail " + NEXT_KEY)

    monkeypatch.setattr(secret_protection, "crypt", unavailable)
    unsaved = make_store(tmp_path / "new")
    with pytest.raises(ProviderError, match="无法加密"):
        unsaved.save(direct())
    assert not unsaved.models_file.exists() and not unsaved.sidecar.exists()
    with pytest.raises(ProviderError, match="无法加密") as err:
        store.save({**saved, "newApiKey": NEXT_KEY})
    assert NEXT_KEY not in str(err.value) and NEXT_KEY not in caplog.text
    assert store.models_file.read_bytes() == old_config and store.sidecar.read_bytes() == old_sidecar
    assert not run(store.test(saved))["ok"] and run(store.list_models(saved)) == []
    assert not opener.requests
    backend = ApiBackend(store)
    with pytest.raises(RequestNotSent, match="无法读取"):
        run(ask(backend, request_for(backend.models()[0].id)))
    assert run(store.test({**saved, "newApiKey": NEXT_KEY}))["ok"], "unsaved probes do not depend on DPAPI"


def test_inference_uses_dpapi_at_request_time_and_never_sends_key_after_manual_edit(tmp_path, monkeypatch):
    monkeypatch.setenv("NO_PROXY", "*")
    with FakeApi() as api, FakeApi() as other:
        api.script([
            {"id": "c1", "choices": [{"index": 0, "delta": {"content": "Hello."}, "finish_reason": "stop"}]}, "[DONE]",
        ])
        store = make_store(tmp_path)
        store.save(direct(baseUrl=api.base_url))
        backend = ApiBackend(make_store(tmp_path))
        request = request_for(backend.models()[0].id)
        assert "Hello." in repr(run(ask(backend, request)))
        ((path, headers, body),) = api.posts
        assert path == "/v1/chat/completions" and headers["authorization"] == "Bearer " + KEY
        assert KEY not in json.dumps(body)

        (row,) = entries_of(store)
        put_settings(store, {"customModels": [{**row, "baseUrl": other.base_url, "apiKey": "should-not-fall-back", "apiKeyHelper": "should-not-run"}]})
        (model_info,) = backend.models()
        with pytest.raises(RequestNotSent, match="无法读取"):
            run(ask(backend, request_for(model_info.id)))
        assert not other.posts


def test_http_and_stream_echoes_do_not_reach_errors_or_logs(tmp_path, monkeypatch, caplog):
    monkeypatch.setenv("NO_PROXY", "*")
    opener = Opener(error=HTTPError("https://example.test", 400, KEY, {}, None))
    store = make_store(tmp_path, opener=opener)
    assert KEY not in json.dumps(run(store.test(direct())))
    with FakeApi() as api:
        api.script(
            http_error(400, {"error": {"code": KEY, "message": "The key was " + KEY}}),
            [{"id": "echo", "choices": [{"index": 0, "delta": {"content": KEY}, "finish_reason": "stop"}]}],
            ["not-json: " + KEY],
            http_error(422, {"error": {"message": "Invalid settings", "details": {KEY: "echo"}}}),
        )
        store.save(direct(baseUrl=api.base_url))
        backend = ApiBackend(store)
        for _ in range(3):
            with pytest.raises(ChatGPTError, match="包含 API Key") as err:
                run(ask(backend, request_for(backend.models()[0].id)))
            assert KEY not in str(err.value) and KEY not in repr(err.value.body)
        with pytest.raises(ChatGPTError, match="Invalid settings") as err:
            run(ask(backend, request_for(backend.models()[0].id)))
        assert err.value.body is None
    assert KEY not in caplog.text


@pytest.mark.parametrize("key", ["d", "delta"])
def test_short_key_does_not_match_protocol_fields_or_event_types(tmp_path, monkeypatch, key):
    monkeypatch.setenv("NO_PROXY", "*")
    part = {"type": "output_text", "text": "Hello."}
    with FakeApi() as api:
        api.script([
            {"type": "response.output_text.delta", "item_id": "msg_1", "delta": "Hel"},
            {"type": "response.output_text.delta", "item_id": "msg_1", "delta": "lo."},
            {"type": "response.output_item.done", "item": {"type": "message", "role": "assistant", "id": "msg_1", "status": "completed", "content": [part]}},
            {"type": "response.completed", "response": {"id": "ok"}},
        ])
        store = make_store(tmp_path)
        store.save(direct(kind="openai", baseUrl=api.base_url, newApiKey=key))
        backend = ApiBackend(store)
        events = run(ask(backend, request_for(backend.models()[0].id)))
        assert [event.text for event in events if isinstance(event, TextDelta)] == ["Hel", "lo."]
        assert [event.text for event in events if isinstance(event, TextDone)] == ["Hello."]
        assert isinstance(events[-1], Finished)


@pytest.mark.parametrize("channel", ["content", "reasoning_content", "tool"])
def test_split_echo_does_not_reach_frontend_tools_or_archive(tmp_path, channel, caplog):
    async def scenario():
        with FakeApi() as api:
            head, tail = KEY[:12], KEY[12:]
            if channel == "tool":
                turn = [
                    chunk({"content": "Inspecting a file."}),
                    chunk({"tool_calls": [{"index": 0, "id": "call_echo", "function": {"name": "read_file", "arguments": '{"file_path":"' + head}}]}),
                    chunk({"tool_calls": [{"index": 0, "function": {"arguments": tail + '"}'}}]}, finish="tool_calls"),
                ]
            else:
                turn = [chunk({channel: "Partial output: " + head}), chunk({channel: tail}, finish="stop")]
            api.script([*turn, "[DONE]"])
            core, _, events, engine, _ = await api_core(
                tmp_path, api, apiKey=None, **{PROTECTED_KEY_FIELD: seal(KEY, "generic-chat-completion-api", api.base_url)},
            )
            try:
                cid = await new_session(core, events, tmp_path, api_model_id(events), autonomy="full")
                saved_id = core._by_cid[cid].controller.saved.id
                await core.call("session.send", {"id": cid, "text": "Use the fake model."})
                await until(lambda: any(b["kind"] == "error" for b in blocks(events, cid)))
                assert "包含 API Key" in json.dumps(blocks(events, cid), ensure_ascii=False)
                assert KEY not in json.dumps(events.items, ensure_ascii=False)
                assert len(api.posts) == 1, "no tool result or retry went to the fake model"
            finally:
                await core.shutdown()
            archived = engine.sessions.load(saved_id)
            assert archived is not None
            assert KEY not in json.dumps([archived.items, archived.requests], ensure_ascii=False)
            assert not any(item.get("type") in ("function_call", "function_call_output") for item in archived.items)

    run(scenario())
    assert KEY not in caplog.text
