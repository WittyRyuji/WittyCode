"""Errors of the Codex engine, and how they are put into words for the user."""

from __future__ import annotations


class CodexError(Exception):
    """Anything that went wrong while talking to Codex."""


class CodexRpcError(CodexError):
    """The app-server answered a request with a JSON-RPC error."""

    def __init__(self, code: int, message: str, data: object = None) -> None:
        super().__init__(f"{message} ({code})")
        self.code = code
        self.message = message
        self.data = data


class CodexProcessError(CodexError):
    """The app-server process could not be started, has exited, or was closed."""

    def __init__(self, message: str, *, exit_code: int | None = None, stderr: str = "", closed: bool = False) -> None:
        super().__init__(message)
        self.message = message
        self.exit_code = exit_code
        self.stderr = stderr
        self.closed = closed


class CodexTimeoutError(CodexError, TimeoutError):
    """A request got no answer in time."""


class CodexNotFoundError(CodexError):
    """No usable Codex command line was found."""

    def __init__(self, message: str = "") -> None:
        super().__init__(
            message
            or "找不到 Codex 命令行：请安装 Codex CLI（npm i -g @openai/codex）或 Codex 桌面版，"
            "或在设置里指定 codex 的路径。"
        )


class CodexSignedOutError(CodexError):
    """Codex has no account to run turns with."""

    def __init__(self, message: str = "") -> None:
        super().__init__(message or "Codex 还没有登录：请先在「设置 → 账号与模型」里用 ChatGPT 登录。")


def friendly_failure(error: BaseException) -> str:
    """One sentence for the user (Chinese, like the rest of the page)."""
    if isinstance(error, (CodexNotFoundError, CodexSignedOutError)):
        return str(error)
    if isinstance(error, CodexProcessError):
        if error.closed:
            return "与 Codex 的连接已关闭。"
        code = f"（exit code {error.exit_code}）" if error.exit_code is not None else ""
        return f"Codex 进程异常退出{code}。"
    if isinstance(error, CodexTimeoutError):
        return f"Codex 没有及时响应：{error}。"
    if isinstance(error, CodexRpcError):
        return f"Codex 报错：{error.message.strip() or error.code}"
    if isinstance(error, CodexError):
        return f"Codex 出错：{error}"
    return f"{type(error).__name__}: {error}"
