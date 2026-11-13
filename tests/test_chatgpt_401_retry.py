"""401 recovery reuses the selected account, refreshes once and never repeats a started response."""
from __future__ import annotations

import asyncio
from dataclasses import replace

import pytest

from desktop.core.chatgpt.errors import ChatGPTError
from desktop.core.chatgpt.responses import build_request
from tests.chatgpt_support import make_engine
from tests.fake_responses import FakeResponses, say


@pytest.mark.parametrize("accepted", [True, False])
def test_401_refreshes_once_and_retries_only_before_any_event(tmp_path, monkeypatch, accepted):
    async def go():
        with FakeResponses(token="new-demo-token" if accepted else "not-accepted") as fake:
            fake.script(say("done"))
            engine = make_engine(tmp_path, fake)
            renewed = []
            async def refresh(client_id, record):
                renewed.append(client_id)
                updated = replace(record, access_token="new-demo-token")
                engine.accounts.save(updated)
                return updated.access_token
            monkeypatch.setattr(engine.tokens, "_renew", refresh)
            request = build_request(model="gpt-6.1-sol", instructions="", items=[], tools=[], reasoning=None)
            if accepted:
                result = [event async for event in engine.stream(request)]
                assert result[-1].response_id and fake.headers[0]["authorization"] == "Bearer new-demo-token"
            else:
                with pytest.raises(ChatGPTError) as caught:
                    _ = [event async for event in engine.stream(request)]
                assert caught.value.status == 401
            assert len(renewed) == 1
    asyncio.run(go())
