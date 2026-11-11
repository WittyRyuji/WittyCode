"""Mutation checks for Amazon Bedrock in the own loop and the retry of transient failures (2026-11-11): the AWS signature and credential
chain (``llm/aws.py``), the Converse / InvokeModel requests and the event stream (``llm/bedrock.py``), the backend that sends them
(``llm/backend.py``), the entries and the plan in ``providers.py``, and ``errors.is_transient`` with the retry in the loop. Each mutant
undoes one part; the tests must catch it.

    python sandbox/mutate_bedrock.py            run every mutant, in a temporary copy of the repository
    python sandbox/mutate_bedrock.py --check    only check that every pattern is found exactly once (read only)
    python sandbox/mutate_bedrock.py 3 7        run just those mutants

The machinery is that of ``mutate_harness_g678.py`` (this working tree is never written to); only the table and the tests differ.
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import mutate_harness_g678 as base  # noqa: E402

CORE = Path("desktop") / "core"
AWS, BED, BACKEND = (CORE / "llm" / name for name in ("aws.py", "bedrock.py", "backend.py"))
PROV, ERR, LOOP, CTL, HTTP = CORE / "providers.py", CORE / "chatgpt" / "errors.py", CORE / "harness" / "loop.py", CORE / "chatgpt" / "controller.py", CORE / "chatgpt" / "http.py"

NEW = ["tests/test_llm_aws.py", "tests/test_llm_bedrock.py", "tests/test_llm_bedrock_backend.py", "tests/test_harness_loop.py"]
OLDER = ["tests/test_llm_backend.py", "tests/test_chatgpt_errors.py", "tests/test_chatgpt_controller.py", "tests/test_providers.py"]

M = [
    # ---- the signature
    ("sign: the path is signed as it is sent (not encoded once more)", AWS, 'quote(parts.path or "/", safe="/~")', '(parts.path or "/")'),
    ("sign: the method is signed as given", AWS, "[method.upper(), ", "[method, "),
    ("sign: the host is the name without its port", AWS, 'signed["host"] = parts.netloc', 'signed["host"] = parts.hostname or ""'),
    ("sign: the session token is not signed", AWS, '    if credentials.token:\n        signed["x-amz-security-token"] = credentials.token\n', ""),
    ("sign: the session token is not sent", AWS, '        sent["X-Amz-Security-Token"] = credentials.token\n', "        pass\n"),
    ("sign: a header value keeps its inner spaces", AWS, '" ".join(str(value).split())', "str(value)"),
    ("sign: the headers are signed in the order given", AWS, "    names = sorted(signed)\n", "    names = list(signed)\n"),
    ("sign: the query is signed in the order given", AWS, 'for name, value in sorted(pairs))', "for name, value in pairs)"),
    ("sign: the body is not part of the signature", AWS, "payload = hashlib.sha256(body).hexdigest()", 'payload = hashlib.sha256(b"").hexdigest()'),
    ("sign: the key does not go through the service", AWS, 'for part in (region, service, "aws4_request"):', 'for part in (region, "aws4_request"):'),
    # ---- credentials in JSON
    ("json: only the top level is read", AWS, 'inner = data.get("Credentials") if isinstance(data, dict) and isinstance(data.get("Credentials"), dict) else data', "inner = data"),
    ("expiry: milliseconds are seconds", AWS, "return value / 1000 if value > 1e11 else float(value)", "return float(value)"),
    ("credentials: ones about to expire are used", AWS, "return self.expires is None or self.expires - now > RENEW_BEFORE", "return True"),
    # ---- the chain
    ("region: AWS_REGION is not read", AWS, 'settings.region or self._lookup("AWS_REGION") or self._lookup("AWS_DEFAULT_REGION")', 'settings.region or self._lookup("AWS_DEFAULT_REGION")'),
    ("region: the profile's is not read", AWS, '            found = self._profile_section(profile).get("region", "")\n', '            found = ""\n'),
    ("access: the bearer token does not come first without a method", AWS, "        if not method:\n            bearer = self._bearer(required=False)\n            if bearer:\n                return Access(bearer=bearer)\n", ""),
    ("access: a missing API key is not said", AWS, "        if required and not token:\n", "        if False:\n"),
    ("access: the export command is not a method", AWS, '        if method == "credentials-command":\n            return Access(credentials=self._exported(settings))\n', ""),
    ("access: the export command is not tried after the chain", AWS, "        if found is None and not method and settings.credential_export:\n            found = self._exported(settings)\n", ""),
    ("access: the login command never runs", AWS, "        if found is None and settings.auth_refresh:\n", "        if False:\n"),
    ("chain: a profile named by the model does not beat the environment", AWS, "        if not profile:\n            keys = self._environment_keys()", "        if True:\n            keys = self._environment_keys()"),
    ("chain: half the keys are keys", AWS, "        if access and secret:\n", "        if access or secret:\n"),
    ("chain: the profile is read every time", AWS, "        cached = self._cached(key)\n        if cached is not None:\n            return cached\n        found = self._from_profile(", "        found = self._from_profile("),
    ("profile: the credentials file does not win over the config", AWS, "        shared = _ini(credentials_file)\n        if shared.has_section(profile):\n            merged.update(dict(shared.items(profile)))\n", ""),
    ("profile: the default profile is only [profile default]", AWS, 'for section in ([profile] if profile == "default" else []) + [f"profile {profile}"]:', 'for section in [f"profile {profile}"]:'),
    ("profile: a process is not asked", AWS, '        if values.get("credential_process"):', "        if False:"),
    ("profile: SSO is not looked at", AWS, '        if values.get("sso_session") or values.get("sso_start_url"):', "        if False:"),
    ("profile: a role to assume is not said to be unsupported", AWS, '        if values.get("role_arn"):', "        if False:"),
    ("profile: one the model names that is not there is not an error", AWS, '            if explicit:\n                raise AwsError(f"~/.aws 里没有 profile「{profile}」。")\n', ""),
    ("sso: the cache file is named by another hash", AWS, 'hashlib.sha1(cache_key.encode("utf-8")).hexdigest()', 'hashlib.sha256(cache_key.encode("utf-8")).hexdigest()'),
    ("sso: an expired login is used", AWS, "(expires is not None and expires <= self._clock())", "False"),
    ("sso: a refused login is not said", AWS, "        if status in (401, 403):\n", "        if False:\n"),
    ("sso: the role is not encoded", AWS, "quote(role, safe='')", "role"),
    ("sso: the token goes in another header", AWS, '{"x-amz-sso_bearer_token": token, ', '{"x-amz-sso-bearer-token": token, '),
    ("sso: the older profile is found by its name, not its start URL", AWS, 'return values.get("sso_start_url", ""), values', 'return "", values'),
    # ---- the request
    ("converse: neighbours of one role are separate messages", BED, '        if messages and messages[-1]["role"] == role:', "        if False:"),
    ("converse: an empty result is sent empty", BED, "output if output.strip() else NO_OUTPUT", "output"),
    ("converse: a call's input is lost", BED, '"input": _arguments(item.get("arguments"))}}])', '"input": {}}}])'),
    ("converse: another origin's thinking is sent", BED, '            if item.get("wc_origin") != origin:\n                continue\n', ""),
    ("converse: a redacted block is not sent back", BED, '            if item.get("wc_data"):', "            if False:"),
    ("converse: a cache point goes to every model", BED, "caches(model)", "True"),
    ("converse: the last message has no cache point", BED, "    if messages and cache:\n", "    if False:\n"),
    ("converse: the system prompt has a cache point for every model", BED, '[{"text": instructions}, *([CACHE_POINT] if cache else [])]', '[{"text": instructions}, CACHE_POINT]'),
    ("converse: Claude 3.7 takes no cache point", BED, "(?:3-5-haiku|3-7-sonnet|", "(?:3-5-haiku|"),
    ("converse: the model may not choose a tool", BED, '            "toolChoice": {"auto": {}},\n', ""),
    ("converse: every model is asked to think", BED, "if is_claude(model) else None", "else None"),
    ("converse: thinking leaves no room for the answer", BED, "        max_tokens = max_tokens or DEFAULT_MAX_OUTPUT\n", ""),
    ("converse: the room is always set", BED, "    if max_tokens:\n        body[", "    if True:\n        body["),
    ("converse: the tags are not sent", BED, '    if metadata:\n        body["requestMetadata"] = dict(metadata)\n', ""),
    ("converse: extraArgs replace an object of ours", BED, "if isinstance(value, Mapping) and isinstance(body.get(key), dict):", "if False:"),
    ("invoke: the model stays in the body", BED, '    body.pop("model", None)\n', "    pass\n"),
    ("invoke: the stream flag stays in the body", BED, '    body.pop("stream", None)\n', "    pass\n"),
    ("invoke: no version", BED, 'return {"anthropic_version": ANTHROPIC_VERSION, **body}', "return body"),
    ("url: the model id is not encoded", BED, "quote(model, safe='')", "model"),
    ("url: the user's endpoint is not used", BED, 'return override.rstrip("/") if override else f"https://bedrock-runtime.{region}.amazonaws.com"', 'return f"https://bedrock-runtime.{region}.amazonaws.com"'),
    # ---- the event stream
    ("frames: the message checksum is not checked", BED, 'if zlib.crc32(data[:8]) != prelude_crc or zlib.crc32(data[:-4]) != struct.unpack_from(">I", data, len(data) - 4)[0]:', "if zlib.crc32(data[:8]) != prelude_crc:"),
    ("frames: no checksum is checked", BED, 'if zlib.crc32(data[:8]) != prelude_crc or zlib.crc32(data[:-4]) != struct.unpack_from(">I", data, len(data) - 4)[0]:', "if False:"),
    ("frames: a byte-array header is not known", BED, "    if kind in (6, 7):", "    if kind == 7:"),
    ("frames: any length is a frame", BED, "            if total < 16 or total > MAX_FRAME:", "            if False:"),
    ("frames: a stream cut in a frame is not an error", BED, '    if buffer:\n        raise ChatGPTError("The stream ended before the response was complete.", code="stream")\n', ""),
    ("exception: no status", BED, "status=_EXCEPTION_STATUS.get(name)", "status=None"),
    ("read: an exception frame is an event", BED, '        if frame.kind != "event":\n            raise failure_of(frame)\n        payload = load_json(frame.payload.decode("utf-8", "replace"))\n        if payload is None:', '        payload = load_json(frame.payload.decode("utf-8", "replace"))\n        if payload is None:'),
    ("read: a call's arguments are lost", BED, '                if found is not None:\n                    found.text += str(delta["toolUse"].get("input") or "")', "                if found is not None:\n                    pass"),
    ("read: the words are not kept", BED, 'block_at(index, "text").text += delta["text"]', 'block_at(index, "text")'),
    ("read: the signature is not kept", BED, '                if isinstance(piece.get("signature"), str):\n                    found.signature += piece["signature"]\n', ""),
    ("read: a redacted block is a thought", BED, "if found.redacted and not found.text:", "if False:"),
    ("read: the cache is not counted in the input", BED, 'count(usage.get("inputTokens")) + cached + written', 'count(usage.get("inputTokens"))'),
    ("read: the cache write is not told", BED, 'written if "cacheWriteInputTokens" in usage else None', "None"),
    ("read: a cut answer is a whole one", BED, '"max_tokens": "max_output_tokens", "content_filtered"', '"content_filtered"'),
    ("read: a stream that ends early is an answer", BED, "    if not (stopped or finished):\n", "    if False:\n"),
    ("invoke: the chunk is not found", BED, 'encoded = payload.get("bytes")', 'encoded = payload.get("bytez")'),
    # ---- the backend
    ("backend: Bedrock models are asked as any other", BACKEND, "        if spec.bedrock is not None:\n            async for event in self._bedrock(spec, request):", "        if False:\n            async for event in self._bedrock(spec, request):"),
    ("backend: a Bedrock API key is not sent", BACKEND, '        if plan.access.bearer:\n            sent = {**headers, "Authorization": f"Bearer {plan.access.bearer}"}\n        elif', "        if False:\n            sent = headers\n        elif"),
    ("backend: the tags are not sent as a header", BACKEND, "            if plan.metadata:\n                headers[", "            if False:\n                headers["),
    ("backend: the user's headers are dropped", BACKEND, 'headers: dict[str, str] = {"Content-Type": "application/json", **user_headers}', 'headers: dict[str, str] = {"Content-Type": "application/json"}'),
    ("backend: the other service name is used for Mantle", BACKEND, 'service = "bedrock-mantle" if mantle and "bedrock-mantle" in urlsplit(url).netloc else "bedrock"', 'service = "bedrock"'),
    ("failure: an expired login is not said", BACKEND, '    elif code in _EXPIRED or "security token" in text and "expired" in text:', "    elif False:"),
    ("failure: a refused API key is a missing permission", BACKEND, '    elif "api key" in text:', "    elif False:"),
    ("failure: a refusal is not said", BACKEND, '    elif code == "AccessDeniedException" or status == 403:', "    elif False:"),
    ("failure: the profile cure is not said", BACKEND, '    elif "on-demand throughput" in text:', "    elif False:"),
    ("failure: a missing model is not said", BACKEND, '    elif status == 404 or code == "ResourceNotFoundException":', "    elif False:"),
    ("failure: a throttle is not said", BACKEND, '    elif status == 429 or code in ("ThrottlingException", "throttlingException", "ServiceQuotaExceededException"):', "    elif False:"),
    # ---- the entries and the plan
    ("providers: thinking is turned on by any kind", PROV, 'if entry.get("enableThinking") is True and entry["provider"] in ("anthropic", BEDROCK)', 'if entry.get("enableThinking") is True'),
    ("providers: thinking has no budget of its own", PROV, '(_positive_int(entry.get("thinkingMaxTokens")) or DEFAULT_THINKING)', "DEFAULT_THINKING"),
    ("providers: any method name is a method", PROV, 'method=method if method in BEDROCK_METHODS else "",', 'method=method or "",'),
    ("plan: every wire sends the tags", PROV, 'sends_tags = spec.kind in (BEDROCK, "anthropic")', "sends_tags = True"),
    ("plan: the tags are not checked", PROV, "        _check_metadata(metadata)\n", ""),
    ("plan: the region keeps its ${VAR}", PROV, "region=expand(raw.region),", "region=raw.region,"),
    ("plan: the endpoint keeps its ${VAR}", PROV, "base_url=expand(raw.base_url),", "base_url=raw.base_url,"),
    ("plan: any number of tags", PROV, "    if len(pairs) > MAX_METADATA:\n", "    if False:\n"),
    # ---- failures that may pass, and the retry
    ("errors: a quota is a hiccup", ERR, 'if code in PERMANENT_CODES or code.startswith(("subscription_sharing_", "chatpass_")):', "if False:"),
    ("errors: what the source says is ignored", ERR, "    if error.transient is not None:\n        return error.transient\n", ""),
    ("errors: a code alone is not a reason", ERR, "return error.status in TRANSIENT_STATUSES or code in TRANSIENT_CODES", "return error.status in TRANSIENT_STATUSES"),
    ("errors: Retry-After in milliseconds is seconds", ERR, 'seconds = float(wait) / 1000 if wait else', "seconds = float(wait) if wait else"),
    ("errors: AWS's capital Message is not read", ERR, ' or _text(parsed.get("Message"))', ""),
    ("errors: AWS's error type is not the code", ERR, '    if code is None and headers.get("x-amzn-errortype"):', "    if False:"),
    ("errors: a reworded failure forgets what may pass", ERR, "transient=self.transient, retry_after=self.retry_after)", ")"),
    ("http: a broken connection is not a hiccup", HTTP, 'code="network", transient=True)', 'code="network")'),
    ("loop: a begun answer is asked again", LOOP, "                if self._events:\n                    raise  # (part of the answer was shown and acted on: asking again would not be the same request)\n", ""),
    ("loop: it asks again for ever", LOOP, "elif repeats < len(self._retry_delays) and is_transient(error):", "elif is_transient(error):"),
    ("loop: it asks again whatever went wrong", LOOP, "elif repeats < len(self._retry_delays) and is_transient(error):", "elif repeats < len(self._retry_delays):"),
    ("loop: the server's wait is obeyed to any length", LOOP, "return min(max(float(asked), base), RETRY_LONGEST)", "return max(float(asked), base)"),
    ("loop: the server's wait may be shorter than ours", LOOP, "return min(max(float(asked), base), RETRY_LONGEST)", "return min(float(asked), RETRY_LONGEST)"),
    ("loop: the pause has no jitter", LOOP, "return base * (1 + 0.25 * self._jitter())", "return base"),
    ("loop: the user is not told", LOOP, '                    self.observer.notice(f"{self._why(error)}，{delay:.0f} 秒后自动重试（第 {repeats}/{len(self._retry_delays)} 次）。")\n', ""),
    ("loop: there is no pause", LOOP, "                    await self._pause(delay)\n", "                    pass\n"),
    ("loop: the history may be shortened over and over", LOOP, "                    if shortened or not await self._prepare(history, overflow=True):", "                    if not await self._prepare(history, overflow=True):"),
    ("loop: a throttle is not called one", LOOP, 'if status == 429 or "rate" in code.lower() or "throttl" in code.lower():', "if False:"),
    ("controller: the notice never reaches the page", CTL, "        if self.current:\n            self.ctl.transcript.add_notice(text)\n", "        if False:\n            self.ctl.transcript.add_notice(text)\n"),
]

if __name__ == "__main__":
    base.M[:] = M
    base.NEW[:] = NEW
    base.OLDER[:] = OLDER
    sys.exit(base.main())
