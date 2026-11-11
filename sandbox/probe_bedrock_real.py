"""One real request to a model on Amazon Bedrock through WittyCode's own route (no Droid): a few words, and optionally a tool call.

    .\\.venv\\Scripts\\python.exe sandbox\\probe_bedrock_real.py --model anthropic.claude-sonnet-4-5-20250929-v1:0 --region us-west-2 [options]

Credentials come the way the application finds them: ``--method environment-api-key`` reads ``AWS_BEARER_TOKEN_BEDROCK`` (a Bedrock API
key), ``--method aws-credentials`` the AWS chain (environment variables, ``--profile``, SSO ...), ``--method credentials-command`` runs
``--export "<command>"`` (JSON with a ``Credentials`` object). The entry is written to a temporary settings file, so the user's own is
not touched. Prints the events and the usage, never a key or a token.

    --kind bedrock-converse | anthropic | openai     the entry's ``provider`` (default bedrock-converse)
    --effort low|medium|high                          ask for extended thinking (Claude)
    --tool                                            ask for a tool call (get_weather)
    --base-url URL                                    ``bedrockBaseUrl`` (a VPC endpoint, a proxy)
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from desktop.core.chatgpt.responses import CallDone, Finished, ReasoningDone, TextDone, build_request, user_message  # noqa: E402
from desktop.core.llm.backend import ApiBackend  # noqa: E402
from desktop.core.providers import ProviderStore  # noqa: E402

TOOL = {"name": "get_weather", "description": "Get the weather of a city.", "parameters": {"type": "object", "properties": {"city": {"type": "string"}}, "required": ["city"]}}


async def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--model", required=True)
    parser.add_argument("--region", default="")
    parser.add_argument("--kind", default="bedrock-converse", choices=["bedrock-converse", "anthropic", "openai"])
    parser.add_argument("--method", default="", choices=["", "aws-credentials", "credentials-command", "environment-api-key"])
    parser.add_argument("--profile", default="")
    parser.add_argument("--export", default="")
    parser.add_argument("--refresh", default="")
    parser.add_argument("--base-url", default="")
    parser.add_argument("--effort", default="")
    parser.add_argument("--tool", action="store_true")
    given = parser.parse_args()

    block = {k: v for k, v in {"authMethod": given.method, "awsRegion": given.region, "awsProfile": given.profile, "awsCredentialExport": given.export, "awsAuthRefresh": given.refresh, "bedrockBaseUrl": given.base_url}.items() if v}
    entry = {"model": given.model, "displayName": "Bedrock probe", "provider": given.kind, "bedrock": block, "maxOutputTokens": 4096}
    folder = Path(tempfile.mkdtemp(prefix="wc-bedrock-probe-"))
    settings = folder / "factory.json"
    settings.write_text(json.dumps({"customModels": [entry]}), encoding="utf-8")
    store = ProviderStore(factory_settings=settings, sidecar=folder / "providers.json")
    backend = ApiBackend(store)
    (spec,) = store.api_models()
    try:
        plan = store.bedrock_plan(spec)
    except Exception as error:
        print("no access:", error)
        return 2
    how = "Bedrock API key" if plan.access.bearer else f"signed with {plan.access.credentials.access_key[:4]}… (session token: {bool(plan.access.credentials.token)})"
    print(f"region {plan.region} | {how} | endpoint override: {plan.base_url or '-'}")
    request = build_request(
        model=spec.id,
        instructions="Be brief.",
        items=[user_message("What is the weather in Paris? Use the tool." if given.tool else "Reply with exactly: OK")],
        tools=[TOOL] if given.tool else [],
        reasoning=given.effort or None,
    )
    try:
        async for event in backend.stream(request):
            if isinstance(event, TextDone):
                print("  text:", event.text[:160])
            elif isinstance(event, ReasoningDone):
                print("  thought:", len(event.text), "chars")
            elif isinstance(event, CallDone):
                print("  call:", event.name, event.arguments)
            elif isinstance(event, Finished):
                usage = event.usage
                print("  finished:", event.incomplete or "ok", usage and (usage.input_tokens, usage.cached_tokens, usage.output_tokens))
    except Exception as error:
        print("  FAILED:", type(error).__name__, str(error)[:400])
        return 1
    return 0


sys.exit(asyncio.run(main()))
