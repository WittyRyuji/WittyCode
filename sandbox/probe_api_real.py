"""One real request to each model in the user's settings.json through WittyCode's own API route (no Droid), a few words each.

Reads ~/.factory/settings.json and the keys from the user's environment variables (HKCU\\Environment included); prints the model, the
events and the usage, never a key. Usage: probe_api_real.py [model display name] [--tool]"""
from __future__ import annotations

import asyncio
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from desktop.core.chatgpt.responses import (  # noqa: E402
    CallDone,
    Finished,
    ReasoningDone,
    TextDone,
    build_request,
    user_message,
)
from desktop.core.llm.backend import ApiBackend  # noqa: E402
from desktop.core.providers import ProviderStore  # noqa: E402

WANT = next((a for a in sys.argv[1:] if not a.startswith("--")), "")
WITH_TOOL = "--tool" in sys.argv

store = ProviderStore()
backend = ApiBackend(store)
specs = [s for s in store.api_models() if not WANT or s.display == WANT]
print("models:", [(s.display, s.kind, s.base_url, s.window, s.max_output, s.reasoning) for s in specs])

TOOL = {"name": "get_weather", "description": "Get the weather of a city.", "parameters": {"type": "object", "properties": {"city": {"type": "string"}}, "required": ["city"]}}


async def one(spec) -> None:
    effort = spec.reasoning or None
    items = [user_message("What is the weather in Paris? Use the tool." if WITH_TOOL else "Reply with exactly: OK")]
    request = build_request(model=spec.id, instructions="Be brief.", items=items, tools=[TOOL] if WITH_TOOL else [], reasoning=effort)
    print(f"--- {spec.display} (effort {effort})")
    try:
        async for event in backend.stream(request):
            if isinstance(event, TextDone):
                print("  text:", event.text[:120])
            elif isinstance(event, ReasoningDone):
                print("  thought:", len(event.text), "chars")
            elif isinstance(event, CallDone):
                print("  call:", event.name, event.arguments)
            elif isinstance(event, Finished):
                usage = event.usage
                print("  finished:", event.incomplete or "ok", usage and (usage.input_tokens, usage.cached_tokens, usage.output_tokens, usage.reasoning_tokens))
    except Exception as error:
        print("  FAILED:", type(error).__name__, str(error)[:300])


async def main() -> None:
    for spec in specs[:1]:
        await one(spec)


asyncio.run(main())
print("env key visible to this process:", bool(os.environ.get("OPENAI_API_KEY")), "(the probe reads the registry as well)")
