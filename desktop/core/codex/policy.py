"""The page's Codex permission levels and approval buttons, in app-server terms.

Levels (``AppSettings.codex_autonomy``; the page's 只读 / 工作区 / 完全访问) are Codex's own three presets (its
``/approvals`` menu: Read Only, Agent, Full Access):

=========  ==============  ======================  ===========================================================
level      approvalPolicy  sandbox (thread/start)  in practice
=========  ==============  ======================  ===========================================================
readonly   on-request      read-only               reads and runs read-only commands; asks before it writes,
                                                   runs anything that needs more, or goes online
workspace  on-request      workspace-write         edits and runs commands inside the project; asks to leave it
full       never           danger-full-access      never asks, no sandbox
=========  ==============  ======================  ===========================================================

``turn/start`` repeats them as ``sandboxPolicy`` objects (``readOnly`` / ``workspaceWrite`` / ``dangerFullAccess``),
since a turn's settings become the thread's from then on. Value spellings are from the 0.165.0 schema and were accepted
by the binary (``thread/start`` with every policy x sandbox pair): ``untrusted`` | ``on-request`` | ``never`` and
``read-only`` | ``workspace-write`` | ``danger-full-access`` (not the camelCase forms some documentation shows).

Verified on this Windows machine: with Codex's Windows sandbox not set up (``windowsSandbox/readiness`` ->
``notConfigured``), ``thread/start`` answers a ``workspace-write`` request with ``sandbox: {"type": "readOnly"}``: the
workspace level then behaves like readonly (it asks before every write). ``full`` is honoured.

Approvals: the page's options keep Droid's values so the existing prompt UI works; Codex gets
``proceed_once -> accept``, ``proceed_always -> acceptForSession``, ``cancel -> decline`` (the agent is told no and goes
on), and a prompt that expires unanswered (turn ended, session closed, process gone) is answered ``cancel``.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from desktop.core.prompts import OptionView


@dataclass(frozen=True, slots=True)
class Permission:
    level: str
    approval: str
    """``approvalPolicy`` (thread/start, thread/resume, turn/start)."""
    sandbox: str
    """``sandbox`` (thread/start, thread/resume)."""
    sandbox_type: str
    """``sandboxPolicy.type`` (turn/start, and what the server reports back)."""


PERMISSIONS: dict[str, Permission] = {
    "readonly": Permission("readonly", "on-request", "read-only", "readOnly"),
    "workspace": Permission("workspace", "on-request", "workspace-write", "workspaceWrite"),
    "full": Permission("full", "never", "danger-full-access", "dangerFullAccess"),
}
DEFAULT_LEVEL = "readonly"
_ALIASES = {
    "read-only": "readonly",
    "read_only": "readonly",
    "readOnly": "readonly",
    "workspace-write": "workspace",
    "workspaceWrite": "workspace",
    "danger-full-access": "full",
    "dangerFullAccess": "full",
}
_LEVEL_OF_SANDBOX = {"readOnly": "readonly", "workspaceWrite": "workspace", "dangerFullAccess": "full", "externalSandbox": "full"}

APPROVAL_OPTIONS: tuple[OptionView, ...] = (
    OptionView("proceed_once", "允许（仅此一次）", "accept", "primary"),
    OptionView("proceed_always", "允许，本会话内同类操作不再询问", "acceptForSession", "normal"),
    OptionView("cancel", "拒绝", "decline", "danger"),
)
DECISIONS: dict[str, str] = {"proceed_once": "accept", "proceed_always": "acceptForSession", "cancel": "decline"}
EXPIRED_DECISION = "cancel"


def normalize_level(value: Any) -> str | None:
    """A permission level from the page's or Codex's spelling; ``None`` for anything else."""
    if not isinstance(value, str):
        return None
    text = value.strip()
    if text in PERMISSIONS:
        return text
    return _ALIASES.get(text)


def permission(level: Any) -> Permission:
    return PERMISSIONS[normalize_level(level) or DEFAULT_LEVEL]


def sandbox_policy(level: Any, project: str | Path) -> dict[str, Any]:
    """``turn/start.sandboxPolicy`` for a level."""
    kind = permission(level).sandbox_type
    if kind == "readOnly":
        return {"type": "readOnly", "networkAccess": False}
    if kind == "workspaceWrite":
        return {
            "type": "workspaceWrite",
            "writableRoots": [str(project)],
            "networkAccess": False,
            "excludeTmpdirEnvVar": False,
            "excludeSlashTmp": False,
        }
    return {"type": "dangerFullAccess"}


def sandbox_type(value: Any) -> str | None:
    """``readOnly`` ... from a reported ``SandboxPolicy`` object or a ``SandboxMode`` string."""
    if isinstance(value, Mapping):
        kind = value.get("type")
        return kind if isinstance(kind, str) else None
    if isinstance(value, str):
        level = normalize_level(value)
        return PERMISSIONS[level].sandbox_type if level else None
    return None


def level_of(sandbox: Any) -> str | None:
    """The level that matches what the server reports for a thread (its sandbox decides)."""
    kind = sandbox_type(sandbox)
    return _LEVEL_OF_SANDBOX.get(kind) if kind else None


def decision_for(value: str | None) -> str:
    return DECISIONS.get(value or "", EXPIRED_DECISION)
