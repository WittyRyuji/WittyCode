"""A role-only/start-only API response is accepted work, not a reason to blindly resend on EOF."""
from __future__ import annotations

import asyncio

import pytest

from desktop.core.chatgpt.errors import ChatGPTError
from desktop.core.chatgpt.responses import user_message
from desktop.core.harness.loop import AgentLoop, Observer
from desktop.core.llm.bedrock import read_converse
from desktop.core.llm.chat import read_chat
from tests.llm_support import event_frame, lines_of
from tests.test_harness_loop import FakeTools


@pytest.mark.parametrize("kind", ["chat", "converse", "converse_partial", "converse_bad_crc", "converse_bad_length"])
def test_an_accepted_empty_api_stream_is_not_resent_after_eof(kind):
    requests = []
    async def chunks():
        frame = event_frame("messageStart", {"role": "assistant"})
        if kind == "converse_partial":
            frame = frame[:15]
        elif kind == "converse_bad_crc":
            frame = frame[:-1] + bytes([frame[-1] ^ 1])
        elif kind == "converse_bad_length":
            frame = b"\xff" * 12
        yield frame
    async def stream(request):
        requests.append(request)
        if kind == "chat":
            events = read_chat(lines_of([{"id": "demo-response", "choices": [{"delta": {"role": "assistant"}}]}]))
        else:
            events = read_converse(chunks())
        async for event in events:
            yield event
    loop = AgentLoop(stream, FakeTools(), Observer(), model="demo-model", reasoning=None, instructions="", retry_delays=(0, 0))
    with pytest.raises(ChatGPTError) as caught:
        asyncio.run(loop.run([user_message("go")]))
    assert caught.value.code == "stream"
    assert len(requests) == 1
