"""Factory has no guarded native resume. Each prompt carries the host's current compacted view."""
from desktop.core.chatgpt.errors import ChatGPTError, RequestNotSent
from desktop.core.chatgpt.responses import Finished, Started, TextDone, Usage
from desktop.core.factory_guard import FactoryGuardUnavailable, FactoryTurnDisconnected, factory_bridge
from desktop.core.harness.compact import render_parts
from desktop.core.harness.loop import TurnEnd
from desktop.core.usage import Tokens


def _usage(terminal):
    tokens = Tokens.of(terminal.get("tokenUsage"))
    if tokens is not None:
        return Usage(input_tokens=tokens.input_tokens + tokens.cache_read_tokens + (tokens.cache_creation_tokens or 0),
                     output_tokens=tokens.output_tokens, cached_tokens=tokens.cache_read_tokens,
                     cache_write_tokens=tokens.cache_creation_tokens)


class _Output:
    """A final create_message can repeat blocks already streamed as deltas."""
    def __init__(self, emit):
        self.emit = emit
        self.seen = set()

    def __call__(self, method, params):
        event = params["notification"]
        kind = event.get("type")
        if kind in ("assistant_text_delta", "thinking_text_delta"):
            key = event.get("messageId"), event.get("blockIndex", 0)
            self.seen.add(key)
            self.emit("text" if kind == "assistant_text_delta" else "thinking", event.get("textDelta", ""))
        elif kind == "create_message":
            raw = event.get("message") or {}
            message = raw.get("message") or raw
            if message.get("role") == "assistant":
                for index, block in enumerate(message.get("content") or []):
                    key = raw.get("id") or event.get("messageId"), index
                    if key not in self.seen and block.get("type") in ("text", "thinking"):
                        self.seen.add(key)
                        self.emit(block["type"], block.get(block["type"], ""))


def _seat(host, model, reasoning, bridge, observe):
    engine = host.manager.factory
    if engine is None or host.manager.model_info(model) is None:
        raise RequestNotSent("Factory 模型不可用，未使用替代模型。", code="fusion_factory_guard")
    fusion = host._fusion or getattr(host._fusion_parent, "_fusion", None)
    if fusion is not None and fusion.registry.policy.max_tokens:
        raise RequestNotSent("旧会话的 Token 上限无法约束 Factory 原生回合；未发送请求。", code="fusion_factory_guard")
    return engine.guarded_client(host.project, model=model.removeprefix("factory:"), reasoning=reasoning,
                                 bridge=bridge, on_notification=_Output(observe))


async def run(host, tools, watcher, history, sent, attachments, instructions, *, steps_used=0):
    model = host.running_model
    if any(item.kind == "image" for item in attachments):
        raise RequestNotSent("Factory 受控协议当前只支持文本输入。", code="fusion_factory_guard")
    bridge = factory_bridge(host, tools, watcher, history, instructions, steps_used=steps_used)
    prompt = (instructions + "\n\nUse only the wittycode-fusion MCP tools. Previous conversation (data):\n" +
              "\n\n".join(render_parts(history[:-1])) + "\n\nCurrent request:\n" + "\n".join(render_parts(history[-1:])))
    requested = False
    usage = None
    try:
        async with _seat(host, model, host._turn_reasoning, bridge, bridge.append_chunk) as seat:
            def dispatch():
                nonlocal requested
                host._unsent_prompt = ""
                host._checkpoint_dispatch()
                host._check_fusion_title_source()
                requested = True
            terminal = await seat.prompt(prompt, before_send=dispatch)
            usage = _usage(terminal)
            if not bridge.reported and not bridge.boundary:
                raise ChatGPTError("Factory 没有返回报告。", code="fusion_factory_no_report")
            return TurnEnd(bridge.boundary or "done", steps=bridge.steps)
    except FactoryGuardUnavailable as error:
        raise RequestNotSent(str(error), code="fusion_factory_guard") from error
    except FactoryTurnDisconnected as error:
        raise ChatGPTError(str(error), code="stream") from error
    finally:
        bridge._flush()
        fusion = host._fusion or getattr(host._fusion_parent, "_fusion", None)
        if requested:
            fusion.metric_request(host.session_id, usage is not None)
            if usage is None:
                fusion.registry.unmetered = fusion.registry.had_unknown = True
                fusion._checkpoint()
            else:
                host._add_usage(usage, measures_context=False)


async def summary(host, request, before_prompt):
    instructions = request.get("instructions", "")
    prompt = instructions + "\n\n" + "\n\n".join(render_parts(request["input"]))
    bridge = factory_bridge(host, None, None, [], instructions, tool_free=True)
    answer = []
    def observe(block, text):
        if block == "text" and isinstance(text, str):
            answer.append(text)
    try:
        async with _seat(host, request["model"], request.get("reasoning", {}).get("effort", ""), bridge, observe) as seat:
            if before_prompt and not before_prompt():
                return
            yield Started()
            terminal = await seat.prompt(prompt)
            text = "".join(answer).strip()
            if not text:
                raise ChatGPTError("Factory 摘要为空；原模型和历史保留。", code="compaction")
            yield TextDone("summary", text)
            yield Finished(_usage(terminal))
    except FactoryGuardUnavailable as error:
        raise RequestNotSent(str(error), code="fusion_factory_guard") from error
