"""WittyCode's own connection to a model API: the API providers' models run in the application's agent loop (``core/harness``),
not in somebody else's program.

The loop speaks one language: a request made of Responses-style ``items`` (``chatgpt.responses.build_request``) and a stream of
neutral events (``TextDelta`` ... ``Finished``). The ChatGPT plan's route is that language on the wire. For the other APIs one
adapter each translates the request into the API's own wire format and the API's stream back into the same events, so the loop,
the tools, the compaction and the saved conversations do not know which API answered:

- ``chat``       Chat Completions (OpenAI-compatible servers: DeepSeek, OpenRouter, Ollama, LM Studio, vLLM ...)
- ``anthropic``  Messages (Claude, and gateways that speak it)
- ``responses``  OpenAI's Responses API with an API key

What an adapter keeps of the model's answer that the other APIs cannot read (DeepSeek's ``reasoning_content``, Claude's signed
thinking blocks) goes into the ``reasoning`` item under ``wc_*`` keys; only an adapter of the same origin sends it back.
"""
