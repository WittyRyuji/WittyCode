"""Check the parts of the ChatGPT Plan route that were written from OpenAI's documentation and never run on a real account
(docs/chatgpt-plan-harness.md, section 4). Run it AFTER signing in once with "Continue with ChatGPT" in WittyCode.

    python sandbox/chatgpt_real_probe.py                       use the account WittyCode saved (%APPDATA%\\WittyCode\\chatgpt)
    python sandbox/chatgpt_real_probe.py --model gpt-6-luna    the model to ask (default: the first one the list offers)
    python sandbox/chatgpt_real_probe.py --root <folder>       another credentials folder

It sends a handful of tiny requests (a few dozen tokens each, low reasoning effort) to the user's own ChatGPT plan, and
prints for each variant of the request ``responses.Shape`` whether the service accepted it. Tokens are never printed. The
raw model list is saved to sandbox/chatgpt_probe/models.json. Nothing is changed in the account except that the access
token is renewed when it is about to expire (the application does the same).
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from dataclasses import replace
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from desktop.core.chatgpt.engine import ChatGPTEngine  # noqa: E402
from desktop.core.chatgpt.errors import ChatGPTError  # noqa: E402
from desktop.core.chatgpt.responses import (  # noqa: E402
    CallDone,
    Finished,
    ItemDone,
    ReasoningDone,
    Shape,
    TextDone,
    build_request,
    call_output,
    user_message,
)
from desktop.core.settings import AppSettings  # noqa: E402

OUT = ROOT / "sandbox" / "chatgpt_probe"
ECHO = {
    "name": "echo",
    "description": "Repeat the given text back.",
    "parameters": {"type": "object", "properties": {"text": {"type": "string"}}, "required": ["text"]},
}
INSTRUCTIONS = "You are a test. Be brief."
ASK = "Call the echo tool once with the text 'ok', then say done."


async def ask(engine: ChatGPTEngine, request: dict) -> tuple[list, str]:
    """(events, problem): ``problem`` is empty when the request was accepted and the stream ended."""
    events: list = []
    try:
        async for event in engine.stream(request):
            events.append(event)
    except ChatGPTError as error:
        return events, f"{error.status or ''} {error.code or ''} {error} [request {error.request_id or '?'}]".strip()
    return events, ""


def summary(events: list) -> str:
    kinds = []
    for event in events:
        if isinstance(event, CallDone):
            kinds.append(f"call {event.name}({event.arguments})")
        elif isinstance(event, TextDone):
            kinds.append(f"text {event.text[:40]!r}")
        elif isinstance(event, ReasoningDone):
            kinds.append(f"reasoning({len(event.text)} chars)")
        elif isinstance(event, Finished):
            u = event.usage
            kinds.append(f"finished {event.incomplete or 'ok'} usage={u}" if u else f"finished {event.incomplete or 'ok'} (no usage)")
    return "; ".join(kinds)


async def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", default=str(Path(os.environ.get("APPDATA", "")) / "WittyCode" / "chatgpt"))
    parser.add_argument("--model", default="")
    parser.add_argument("--base-url", default="", help="another origin for /models and /responses (a local fake, to try this script)")
    args = parser.parse_args()

    options = {"base_url": args.base_url} if args.base_url else {}
    engine = ChatGPTEngine(AppSettings(), args.root, shape=Shape(), **options)
    view = engine.cached_account()
    print(f"account: auth={view['auth']} plan_enabled={view['planEnabled']} email={view['email'] or '-'}")
    if view["auth"] != "signed-in" or not view["planEnabled"]:
        print("Sign in with 'Continue with ChatGPT' in WittyCode first (and allow the plan).")
        return 2

    OUT.mkdir(exist_ok=True)
    token = await engine.tokens.access_token()
    response = await engine.http.request("GET", f"{engine.base_url}/models", headers={"Authorization": f"Bearer {token}"}, timeout=20.0)
    print(f"\nGET /models -> HTTP {response.status}")
    if response.status == 200:
        raw = response.json()
        (OUT / "models.json").write_text(json.dumps(raw, indent=2, ensure_ascii=False), encoding="utf-8")
        entries = raw.get("models") if isinstance(raw, dict) else raw
        print(f"  top-level keys: {sorted(raw) if isinstance(raw, dict) else type(raw).__name__}; {len(entries or [])} entries")
        for entry in (entries or [])[:3]:
            print(f"  fields of {entry.get('slug') or entry.get('id')}: {sorted(entry)}")
    models = await engine.list_models(refresh=True)
    print("  parsed:", [(m.id, m.efforts, m.default_effort, m.context_window, m.supports_images) for m in models])
    model = args.model or (models[0].id if models else "")
    if not model:
        print("no model to ask")
        return 3
    effort = "low" if (next((m for m in models if m.id == model), None) and "low" in next(m for m in models if m.id == model).efforts) else None
    print(f"\nasking {model} (reasoning effort {effort or 'not sent'})\n")

    base = Shape()
    variants = [
        ("default shape (flat tools, encrypted reasoning, summary auto, parallel calls)", base),
        ("tools grouped in a namespace", replace(base, flat_tools=False)),
        ("no encrypted reasoning asked for", replace(base, encrypted_reasoning=False)),
        ("no reasoning summary asked for", replace(base, reasoning_summary=None)),
        ("parallel_tool_calls false", replace(base, parallel_tool_calls=False)),
        ("prompt_cache_key sent", replace(base, cache_key=True)),
    ]
    accepted: dict[str, bool] = {}
    first_events: list = []
    for label, shape in variants:
        request = build_request(model=model, instructions=INSTRUCTIONS, items=[user_message(ASK)], tools=[ECHO], reasoning=effort, shape=shape, conversation="wittycode-probe")
        events, problem = await ask(engine, request)
        accepted[label] = not problem
        if label == variants[0][0]:
            first_events = events
        print(f"[{'ok ' if not problem else 'REFUSED'}] {label}")
        print(f"      {problem or summary(events)}")

    # the second request of a tool round trip: what the model said (reasoning items included) plus the tool's output
    call = next((e for e in first_events if isinstance(e, CallDone)), None)
    if call is not None:
        from desktop.core.chatgpt.responses import history_item

        items = [user_message(ASK)] + [h for h in (history_item(e.item) for e in first_events if isinstance(e, ItemDone)) if h is not None] + [call_output(call.call_id, "ok")]
        kinds = [i.get("type") for i in items]
        request = build_request(model=model, instructions=INSTRUCTIONS, items=items, tools=[ECHO], reasoning=effort, shape=base, conversation="wittycode-probe")
        events, problem = await ask(engine, request)
        print(f"\n[{'ok ' if not problem else 'REFUSED'}] second request of the round trip (history: {kinds})")
        print(f"      {problem or summary(events)}")
    else:
        print("\n(the default request produced no tool call, so the round trip was not tried)")

    print("\nWrite the results into docs/chatgpt-plan-harness.md section 4; set WITTYCODE_CHATGPT_SHAPE (a JSON object) for any REFUSED variant that is the default.")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
