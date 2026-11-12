"""Errors of talking to an engine's process over JSON-RPC (``desktop.core.jsonrpc``); each engine words them itself."""

from __future__ import annotations


class EngineError(Exception):
    """Anything that went wrong while talking to an engine's process."""


class EngineRpcError(EngineError):
    """The process answered a request with a JSON-RPC error."""

    def __init__(self, code: int, message: str, data: object = None) -> None:
        super().__init__(f"{message} ({code})")
        self.code = code
        self.message = message
        self.data = data


class EngineProcessError(EngineError):
    """The process could not be started, has exited, or was closed."""

    def __init__(self, message: str, *, exit_code: int | None = None, stderr: str = "", closed: bool = False) -> None:
        super().__init__(message)
        self.message = message
        self.exit_code = exit_code
        self.stderr = stderr
        self.closed = closed


class EngineTimeoutError(EngineError, TimeoutError):
    """A request got no answer in time."""
