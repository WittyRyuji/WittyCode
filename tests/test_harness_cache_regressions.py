"""Cache fixes exercised through the real controller and loopback HTTP, not a cache simulator."""
from __future__ import annotations

import asyncio
import copy
import datetime
import json

import pytest

from desktop.core.chatgpt.responses import Usage, history_item
from desktop.core.harness import prompt
from desktop.core.usage import UsageLedger
from tests.chatgpt_support import finish_turn, make_engine
from tests.fake_responses import FakeResponses, call, say, usage


@pytest.mark.parametrize("change", ["rules", "mode", "level", "date", "language", "skills"])
def test_changed_context_is_appended_without_rewriting_the_prefix(tmp_path, monkeypatch, change):
    async def go():
        project = tmp_path / "project"
        project.mkdir()
        rules = project / "AGENTS.md"
        rules.write_text("Use the old convention.", encoding="utf-8")
        with FakeResponses() as fake:
            fake.script(say("first"), say("second"), say("third"))
            engine = make_engine(tmp_path, fake)
            ctl = await engine.open_new(project, model="gpt-6.1-sol", reasoning="low")
            try:
                ctl.send_prompt("one")
                await finish_turn(ctl)
                prefix = copy.deepcopy(ctl.saved.items)
                if change == "rules":
                    rules.write_text("Use the NEW convention.", encoding="utf-8")
                elif change == "mode":
                    await ctl.update_settings(mode="spec")
                elif change == "level":
                    await ctl.update_settings(autonomy="ask")
                elif change == "language":
                    engine.settings.think_chinese = False
                elif change == "skills":
                    folder = project / ".wittycode" / "skills" / "demo"
                    folder.mkdir(parents=True)
                    (folder / "SKILL.md").write_text("---\nname: demo\ndescription: A demo skill.\n---\nRead the code.", encoding="utf-8")
                else:
                    class Tomorrow(datetime.date):
                        @classmethod
                        def today(cls):
                            return cls(2030, 1, 2)
                    monkeypatch.setattr(prompt.datetime, "date", Tomorrow)
                ctl.send_prompt("two")
                await finish_turn(ctl)
                first, second = fake.requests[:2]
                assert first["instructions"] == second["instructions"]
                assert first["tools"] == second["tools"]
                assert second["input"][:len(prefix)] == prefix
                updates = second["input"][len(prefix):-1]
                assert len(updates) == 1 and updates[0]["role"] == "developer"
                text = updates[0]["content"][0]["text"]
                assert "replace" in text.lower()
                if change == "rules":
                    assert "NEW convention" in text
                elif change == "mode":
                    assert "Plan mode is on" in text and ctl._tools.plan
                # An unchanged third turn must not append a redundant update.
                ctl.send_prompt("three")
                await finish_turn(ctl)
                assert sum(i.get("role") == "developer" for i in fake.requests[-1]["input"]) == 1
            finally:
                await engine.close_all()
    asyncio.run(go())


def test_prompt_and_phase_survive_reopening_and_usage_is_not_recounted(tmp_path):
    async def go():
        project = tmp_path / "project"
        project.mkdir()
        rules = project / "AGENTS.md"
        rules.write_text("old rule", encoding="utf-8")
        raw_usage = usage(12000, 50, cached=4000)
        raw_usage["input_tokens_details"]["cache_write_tokens"] = 7000
        answer = say("hello", used=raw_usage)
        for event in answer:
            if event["type"] == "response.output_item.done":
                event["item"]["phase"] = "final_answer"
        with FakeResponses() as fake:
            fake.script(answer, say("resumed"))
            engine = make_engine(tmp_path, fake)
            ctl = await engine.open_new(project, model="gpt-6.1-sol")
            ctl.send_prompt("one")
            await finish_turn(ctl)
            ledger = UsageLedger()
            ledger.feed(ctl.usage_series())
            total = ledger.total
            assert total.input_tokens == 1000 and total.cache_creation_tokens == 7000
            original = fake.requests[0]["instructions"]
            identifier = ctl.session_id
            await ctl.close()
            # The previous version omitted this field even when its per-model counters knew the complete value.
            legacy = engine.sessions.load(identifier)
            legacy.usage.pop("cacheWrite")
            engine.sessions.save(legacy)
            rules.write_text("new rule", encoding="utf-8")
            reopened = await engine.open_saved(project, identifier)
            assert not ledger.feed(reopened.usage_series()) and ledger.total == total
            reopened.send_prompt("two")
            await finish_turn(reopened)
            assert fake.requests[-1]["instructions"] == original
            prior = next(i for i in fake.requests[-1]["input"] if i.get("role") == "assistant")
            assert prior["phase"] == "final_answer" and prior["id"] == "msg_1" and prior["status"] == "completed"
            assert reopened.transcript.turns[-1].user_block.text == "two"
            await engine.close_all()
    asyncio.run(go())


def test_usage_reads_cache_write_without_inventing_a_missing_counter():
    raw = usage(12000, 10, cached=4000)
    assert Usage.of(raw).tokens().cache_creation_tokens is None
    raw["input_tokens_details"]["cache_write_tokens"] = 7000
    tokens = Usage.of(raw).tokens()
    assert (tokens.input_tokens, tokens.cache_creation_tokens, tokens.cache_read_tokens) == (1000, 7000, 4000)
    assert tokens.total == 12010


@pytest.mark.parametrize("item", [
    {"type": "message", "role": "assistant", "id": "msg_demo", "phase": "commentary", "status": "completed", "content": [{"type": "output_text", "text": "read", "annotations": []}]},
    {"type": "function_call", "id": "fc_demo", "call_id": "call_demo", "name": "read_file", "arguments": "{}", "status": "completed"},
    {"type": "reasoning", "id": "rs_demo", "summary": [], "encrypted_content": "opaque", "status": "completed"},
    {"type": "compaction", "id": "cmp_demo", "encrypted_content": "opaque"},
    {"type": "reasoning", "summary": [], "wc_reasoning": "", "wc_signature": "signature", "wc_origin": "demo"},
])
def test_replay_preserves_every_protocol_field_and_owns_its_copy(item):
    kept = history_item(item)
    assert kept == item
    if "content" in kept:
        kept["content"][0]["text"] = "changed"
        assert item["content"][0]["text"] == "read"


def test_response_diagnostics_are_kept_without_logging_the_payload(tmp_path, caplog):
    async def go():
        project = tmp_path / "project"
        project.mkdir()
        answer = say("private answer")
        answer[-1]["response"].update({
            "reasoning": {"context": "all_turns", "effort": "low"},
            "input_transformations": [{"type": "reasoning_removed", "reason": "prefix_mismatch", "item_id": "private-id"}],
            "prompt_cache_diagnostics": {"type": "cache_miss", "reason": "input_changed", "comparison_reusable_tokens": 5629, "cache_missed_tokens": 5629, "private": "private answer"},
        })
        with FakeResponses() as fake:
            fake.script(answer)
            engine = make_engine(tmp_path, fake)
            ctl = await engine.open_new(project, model="gpt-6.1-sol")
            ctl.send_prompt("private prompt")
            await finish_turn(ctl)
            records = ctl.saved.requests
            assert len(records) == 1
            record = records[0]
            assert record["reasoning_context"] == "all_turns"
            assert record["cache_status"] == "cache_miss"
            assert record["cache_reasons"] == ["input_changed"]
            assert record["cache_missed_tokens"] == record["comparison_reusable_tokens"] == 5629
            serialised = json.dumps(records)
            assert all(secret not in serialised for secret in ("private answer", "private prompt", "private-id", str(project)))
            assert "instructions_hash" in record and "input_hash" in record
            assert engine.sessions.load(ctl.session_id).requests == records
            await engine.close_all()
    asyncio.run(go())
