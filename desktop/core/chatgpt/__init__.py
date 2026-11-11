"""The ChatGPT Plan engine: a conversation run by WittyCode's own agent loop (``core/harness``) against OpenAI's
Responses API, paid by the user's ChatGPT plan through "Sign in with ChatGPT".

This package talks to OpenAI (sign-in, tokens, the model list, the Responses stream) and adapts the loop to the
application (``engine`` and ``controller`` have the surface of the Codex and Devin ones). Design and the facts it rests on:
``docs/chatgpt-plan-harness.md``.
"""
