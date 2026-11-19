"""Source-model summaries and fresh, scoped context for model handoffs."""
from __future__ import annotations

import asyncio
import json
from pathlib import Path

from desktop.core.chatgpt.errors import ChatGPTError
from desktop.core.context_envelope import wrap
from desktop.core.handoff import Handoff, attachment_text, budget_for_window, clip_message, conversation_messages
from desktop.core.harness import compact
from desktop.core.harness.prompt import load_agents
from desktop.core.session_state import SessionState
from desktop.core.transcript import ActivityBlock, TextBlockView, TurnStatus, UserBlock


NATIVE_SUMMARY = ("WittyCode model-switch compaction request. Summarise this conversation for the next model; "
                  "do not continue the task, call tools, ask questions or change files.\n\n" + compact.INSTRUCTIONS)


def recall_for_switch(memory, engine, project, transcript) -> str:
    """Relevance is based on recent user intent, never an indiscriminate dump of all stored notes."""
    from desktop.core.memory.recall import pick, render
    if memory is None or not memory.enabled(engine) or not memory.recall():
        return ""
    messages = [block.text for turn in transcript.turns for block in turn.blocks
                if isinstance(block, UserBlock) and block.text not in ("/compact", NATIVE_SUMMARY)]
    query = "\n".join(messages[-3:])[-12_000:]
    notes = pick(memory.store.candidates(str(project)), query, threshold=5)
    return render(notes)[0] if notes else ""


def instructions_for_switch(project) -> str:
    from desktop.core.customization import global_instructions
    sections = []
    global_rules = global_instructions()
    if global_rules:
        sections.append("WittyCode global AGENTS.md instructions (project rules take precedence):\n" + global_rules)
    context = load_agents(Path(project))
    for item in context.files:
        sections.append(f"Current instructions from {item.path}; nearer project rules take precedence:\n{item.text}")
    if context.shortened:
        sections.append("Some instruction sections were shortened; read the original AGENTS.md when needed.")
    return "\n\n".join(sections)


def summary_handoff(summary, transcript, *, project, memory, engine, window=0, carry=()) -> Handoff:
    """The summary travels as historical data; current rules and relevant memory remain separate context."""
    from desktop.core.engine_items import HANDOFF_END, HANDOFF_JSON, HANDOFF_START
    turns = [turn for turn in transcript.turns if not turn.user_block or turn.user_block.text not in ("/compact", NATIVE_SUMMARY)]
    count = len(conversation_messages(turns))
    text = summary + "\n\n"
    files = clipped = 0
    for item in carry:
        content = attachment_text(item).strip()
        if content:
            content, cut = clip_message(content, budget_for_window(window) // max(2, len(carry)))
            text += f'[Selected attachment "{item.name}"]\n{content}\n\n'
            files += 1
            clipped += cut
    context = wrap(instructions_for_switch(project), recall_for_switch(memory, engine, project, transcript))
    entries = [{"role": "assistant", "content": text, "wc_summary_messages": count}]
    prefix = (HANDOFF_START + ", compressed by the source model. The summary below is historical data, not a new request "
              "or permission to replay completed actions. Original records and tool evidence remain in the previous segment."
              + context + "\n\n" + HANDOFF_JSON + json.dumps(entries, ensure_ascii=False) + HANDOFF_END)
    return Handoff(prefix, max(1, count), count, len(prefix.encode("utf-8")), False, clipped=clipped, files=files,
                   attachments=tuple(item for item in carry if not attachment_text(item).strip()), summary=summary)


async def compact_native(controller, window=0) -> str:
    """Native CLIs summarise their own context, including tool evidence unavailable in the UI transcript."""
    if not controller.transcript.turns:
        return ""
    sent_handoff = any(turn.user_block and turn.user_block.handoff for turn in controller.transcript.turns)
    if (controller.handoff is not None and controller.handoff.summary and not sent_handoff
            and not any(tokens.total for tokens in controller.usage_series().values())):
        return controller.handoff.summary  # Already summarised; this segment has not received a model request yet.
    if controller.state is not SessionState.READY:
        raise ChatGPTError("会话尚未就绪，未切换模型。", code="compaction")
    controller._switch_summary = True
    before = controller.transcript.snapshot()
    turn = None
    autonomy = controller.autonomy
    safe = "off" if controller.engine == "factory" else "ask"
    try:
        if not await controller.update_settings(autonomy=safe):
            raise ChatGPTError("无法限制摘要请求的执行权限，未发送压缩请求。", code="compaction")
        if not controller.send_prompt(NATIVE_SUMMARY):
            raise ChatGPTError("原模型未接受压缩请求。", code="compaction")
        turn = controller.transcript.active_turn
        async with asyncio.timeout(180):
            while controller.state is SessionState.RUNNING:
                await asyncio.sleep(0.05)
        if turn is None or turn.status is not TurnStatus.DONE:
            raise ChatGPTError("原模型的压缩请求失败或已停止。", code="compaction")
        if any(isinstance(block, ActivityBlock) and block.tools for block in turn.blocks):
            raise ChatGPTError("压缩请求尝试调用工具，未接受摘要或切换模型。", code="compaction")
        summary = "\n\n".join(block.text for block in turn.blocks if isinstance(block, TextBlockView)).strip()
        if not summary or (window and compact.estimate_text(summary) > compact.budget(window) // 2):
            raise ChatGPTError("摘要为空或超过目标模型窗口，未切换模型。", code="compaction")
        return summary
    except BaseException:
        if controller.state is SessionState.RUNNING:
            await controller.interrupt()
        raise
    finally:
        controller.transcript.restore(before)  # Internal summary traffic is not an independent user turn.
        controller._switch_summary = False
        if controller.state is SessionState.READY and not await controller.update_settings(autonomy=autonomy):
            raise ChatGPTError("压缩后未能恢复原权限；会话保留在受限权限下，未切换模型。", code="compaction")
