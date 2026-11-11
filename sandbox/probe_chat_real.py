"""Two real requests through the Chat Completions adapter (core/llm/chat.py), to OpenAI's own /v1/chat/completions with the user's
OPENAI_API_KEY (read from the environment, never printed): a sentence and a tool call."""
import asyncio, json, sys, tempfile
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from desktop.core.chatgpt.responses import CallDone, Finished, ReasoningDone, TextDone, build_request, user_message
from desktop.core.llm.backend import ApiBackend
from desktop.core.providers import ProviderStore

tmp = Path(tempfile.mkdtemp(prefix="wc-chat-probe-"))
(tmp / "factory.json").write_text(json.dumps({"customModels": [{"model": "gpt-6-astra", "displayName": "Astra via chat", "baseUrl": "https://api.openai.com/v1", "apiKey": "${OPENAI_API_KEY}", "provider": "generic-chat-completion-api", "maxContextLimit": 1024000, "maxOutputTokens": 128000, "reasoningEffort": None}]}), encoding="utf-8")
store = ProviderStore(factory_settings=tmp / "factory.json", sidecar=tmp / "providers.json")
backend = ApiBackend(store)
(spec,) = store.api_models()
print("kind:", spec.kind, "window:", spec.window, "key set:", store.api_key(spec) is not None)
TOOL = {"name": "get_weather", "description": "Get the weather of a city.", "parameters": {"type": "object", "properties": {"city": {"type": "string"}}, "required": ["city"]}}

async def one(text, tools):
    request = build_request(model=spec.id, instructions="Be brief.", items=[user_message(text)], tools=tools, reasoning=None)
    try:
        async for event in backend.stream(request):
            if isinstance(event, TextDone): print("  text:", event.text[:100])
            elif isinstance(event, ReasoningDone): print("  thought:", len(event.text), "chars")
            elif isinstance(event, CallDone): print("  call:", event.name, event.arguments)
            elif isinstance(event, Finished):
                u = event.usage
                print("  finished:", event.incomplete or "ok", u and (u.input_tokens, u.cached_tokens, u.output_tokens))
    except Exception as error:
        print("  FAILED:", type(error).__name__, str(error)[:300])

async def main():
    print("-- sentence"); await one("Reply with exactly: OK", [])
    print("-- tool"); await one("What is the weather in Paris? Use the tool.", [TOOL])
asyncio.run(main())
