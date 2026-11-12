"""What every engine's session controller shares with the application: its state, the labels the page shows for it, the
answer to a steer, the four settings of an open session, a saved session read without opening it, and the session limit."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

from desktop.core.transcript import Transcript
from desktop.core.usage import Tokens


class SessionState(str, Enum):
    OPENING = "opening"
    READY = "ready"
    RUNNING = "running"
    DEAD = "dead"
    CLOSED = "closed"


SESSION_STATE_LABELS: dict[SessionState, str] = {
    SessionState.OPENING: "正在连接…",
    SessionState.READY: "就绪",
    SessionState.RUNNING: "运行中",
    SessionState.DEAD: "连接已断开",
    SessionState.CLOSED: "已关闭",
}


class SessionLimitError(RuntimeError):
    """Raised when the configured number of open sessions is already reached."""


def eviction_message(title: str, *, reopenable: bool = True) -> str:
    """What the user is told when an idle session is closed to make room for another one."""
    text = f"已自动关闭最久未使用的会话「{title}」以腾出位置"
    return text + ("，可在侧边栏重新打开。" if reopenable else "。")


@dataclass(frozen=True, slots=True)
class SteerOutcome:
    """What became of a message given to a running turn."""

    ok: bool
    over: bool = False
    """The turn was already over: the message was not given to anything (it can go as a message of its own)."""
    message: str = ""


@dataclass(frozen=True, slots=True)
class LiveSettings:
    """The four settings of an open session, in the engine's own values."""

    model: str
    reasoning: str
    autonomy: str
    mode: str


@dataclass(slots=True)
class Peek:
    """A saved session read without opening it (no process of its own kept): what the cache keeps of it."""

    transcript: Transcript
    title: str | None = None
    settings: LiveSettings | None = None
    usage: Tokens | None = None
    context_used: int | None = None
    window: int | None = None
