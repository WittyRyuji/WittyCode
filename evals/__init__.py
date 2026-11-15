"""A small set of tasks for measuring WittyCode's own agent loop: does a change to the harness solve more of them, with
fewer tokens, in less time?

A task is a small project folder, a message for the agent and a check that says whether the work was done
(``taskset``). ``run`` gives each task to the loop the application itself runs (``desktop.core.chatgpt.controller``, with
its tools, permissions, compaction and request diagnostics), stands in for the user (``runner.Pilot``) and writes down
what the task took (``metrics``): whether the check passes, how the turn ended, requests, tokens of each kind, time by
where it went, approvals a user would have had to give. ``report`` puts a run, or two runs side by side, into a table.

Only ``python -m evals run`` calls a model, and it stops at the token budget it was given (a run is meant to cost about a
million tokens; five million is the most it accepts). ``list``, ``verify`` and ``compare`` call nothing.
"""

SCHEMA = 1
"""The version of ``result.json``; ``report.load`` refuses another."""
