"""Errors of the Devin engine, and how they are put into words for the user.

The JSON-RPC client is the one the Codex engine uses (same line-delimited stdio framing), so what it raises are its
``CodexError`` family; ``friendly_failure`` here words them with Devin's name.
"""

from __future__ import annotations

from desktop.core.codex.errors import (
    CodexError as DevinError,
)
from desktop.core.codex.errors import (
    CodexProcessError as DevinProcessError,
)
from desktop.core.codex.errors import (
    CodexRpcError as DevinRpcError,
)
from desktop.core.codex.errors import (
    CodexTimeoutError as DevinTimeoutError,
)


class DevinNotFoundError(DevinError):
    """No usable Devin command line was found."""

    def __init__(self, message: str = "") -> None:
        super().__init__(message or "找不到 Devin 命令行：请安装 Devin 桌面版或 Devin CLI，或在设置里指定 devin 的路径。")


class DevinSignedOutError(DevinError):
    """Devin has no account to run turns with."""

    def __init__(self, message: str = "") -> None:
        super().__init__(message or "Devin 还没有登录：请先在「设置 → 账号与模型」里登录。")


def friendly_failure(error: BaseException) -> str:
    """One sentence for the user (Chinese, like the rest of the page)."""
    if isinstance(error, (DevinNotFoundError, DevinSignedOutError)):
        return str(error)
    if isinstance(error, DevinProcessError):
        if error.closed:
            return "与 Devin 的连接已关闭。"
        code = f"（exit code {error.exit_code}）" if error.exit_code is not None else ""
        return f"Devin 进程异常退出{code}。"
    if isinstance(error, DevinTimeoutError):
        return f"Devin 没有及时响应：{error}。"
    if isinstance(error, DevinRpcError):
        return f"Devin 报错：{error.message.strip() or error.code}"
    if isinstance(error, DevinError):
        return f"Devin 出错：{error}"
    return f"{type(error).__name__}: {error}"


__all__ = [
    "DevinError",
    "DevinNotFoundError",
    "DevinProcessError",
    "DevinRpcError",
    "DevinSignedOutError",
    "DevinTimeoutError",
    "friendly_failure",
]
