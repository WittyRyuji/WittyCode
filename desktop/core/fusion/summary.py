"""Tool-free, guarded ACP summary requests using the source model, never a cheaper fallback."""
from __future__ import annotations

from types import SimpleNamespace
import uuid

from desktop.core.chatgpt.errors import ChatGPTError
from desktop.core.chatgpt.responses import Finished, Started, TextDone, Usage
from desktop.core.devin.items import prompt_blocks, stats_tokens
from desktop.core.devin.models import current_value
from desktop.core.devin.policy import CANCELLED
from desktop.core.fusion.acp import AcpRun
from desktop.core.harness.compact import render_parts
from desktop.core.usage import Tokens


class AcpSummary(AcpRun):
    """Ephemeral native session: no task tools, no persisted main-session checkpoint and no execution replay."""

    def __init__(self, manager):
        host = SimpleNamespace(manager=manager, session_id="summary-" + uuid.uuid4().hex,
                               saved=SimpleNamespace(origin={}, items=[], turns=[]), _turn=None,
                               _save_now=lambda **_: None)
        super().__init__(host)
        self.tools = SimpleNamespace(declarations=lambda: [])
        self.answer = ""
        self.summary_active = False

    async def _request(self, method, params, request_id):
        return CANCELLED

    def _notify(self, method, params):
        if not self.summary_active or not isinstance(params, dict) or params.get("sessionId") != self.session:
            return
        if method == "_cognition.ai/turn_stats":
            key = params.get("turnClientMessageId")
            if isinstance(key, str) and (tokens := stats_tokens(params)) is not None:
                self._usage[key] = tokens
        elif method == "session/update":
            update = params.get("update", {})
            if update.get("sessionUpdate") == "agent_message_chunk":
                text = update.get("content", {}).get("text", "")
                if isinstance(text, str):
                    self.answer += text
            elif update.get("sessionUpdate") == "tool_call":
                self.violation = "摘要请求尝试调用工具，已停止；原模型和历史保留。"
                self._stop()

    async def stream(self, request):
        try:
            await self._open()
            model = request["model"].removeprefix("devin:")
            options = await self.client.request("session/set_config_option", {
                "sessionId": self.session, "configId": "model", "value": model,
            }, timeout=30)
            if current_value(options.get("configOptions"), "model") != model:
                raise ChatGPTError("ACP 未确认用于压缩的原模型，未切换。", code="compaction")
            effort = request.get("reasoning", {}).get("effort")
            if effort:
                options = await self.client.request("session/set_config_option", {
                    "sessionId": self.session, "configId": "thought_level", "value": effort,
                }, timeout=30)
                if current_value(options.get("configOptions"), "thought_level") != effort:
                    raise ChatGPTError("ACP 未确认摘要思考档位，未切换。", code="compaction")
            prompt = request.get("instructions", "") + "\n\nDo not call any tools. Return only the summary.\n\n" + "\n\n".join(render_parts(request.get("input", [])))
            self.summary_active = True
            yield Started()
            result = await self.client.request("session/prompt", {
                "sessionId": self.session, "prompt": prompt_blocks(prompt),
            }, timeout=180)
            if self.violation:
                raise ChatGPTError(self.violation, code="compaction")
            if result.get("stopReason") != "end_turn" or not self.answer.strip():
                raise ChatGPTError("ACP 摘要未完成，原模型和历史保留。", code="compaction")
            tokens = Tokens()
            for more in self._usage.values():
                tokens = tokens.plus(more)
            usage = Usage(input_tokens=tokens.input_tokens + tokens.cache_read_tokens + (tokens.cache_creation_tokens or 0),
                          output_tokens=tokens.output_tokens, cached_tokens=tokens.cache_read_tokens,
                          cache_write_tokens=tokens.cache_creation_tokens) if self._usage else None
            yield TextDone("summary", self.answer.strip())
            yield Finished(usage)
        finally:
            self.summary_active = False
            await self.close()


def summary_stream(manager):
    def stream(request):
        return AcpSummary(manager).stream(request)
    return stream
