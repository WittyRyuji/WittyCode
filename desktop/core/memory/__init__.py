"""The shared memory of the user's coding agents (see ``store`` for the files, ``server`` for the MCP server).

Kept light on purpose: ``python -m desktop.core.memory`` is started once per conversation of every engine, so nothing
heavy may be imported here.
"""
