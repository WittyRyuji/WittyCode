"""WittyCode's own agent loop: tools, permissions, the loop that feeds a model its tool results, and the saved
conversation. Nothing here knows about a particular provider; ``core/chatgpt`` plugs the Responses API into it.
Design: ``docs/chatgpt-plan-harness.md``.
"""
