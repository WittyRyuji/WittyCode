"""Session-scoped model policy and durable ownership of expert work.

This module does not call a model or execute a tool. The caller checkpoints each transition before performing any
side effect; a restored running task is interrupted, never replayed automatically.
"""
from __future__ import annotations

import math
import ntpath
import os
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping

from desktop.core.fusion.identity import display_name, load
from desktop.core.fusion.reasoning import reasoning_map

_IDENTITIES = load(None).names


class FusionError(ValueError):
    """A Fusion action cannot be performed under the session's policy."""


def _model(value: Any, *, external: bool = False) -> bool:
    return isinstance(value, str) and bool(value.strip()) and value.startswith(("chatgpt:", "api:", "devin:") if external else ("chatgpt:", "api:"))


@dataclass(frozen=True, slots=True)
class Policy:
    main: str
    workers: tuple[str, ...]
    light: str
    max_calls: int
    max_tokens: int = 0
    max_seconds: int = 900
    preference: int = 50  # 0 favours cost, 100 favours efficiency
    reasoning: Mapping[str, str] = field(default_factory=dict)

    def __post_init__(self) -> None:
        try:
            object.__setattr__(self, "reasoning", reasoning_map(self.reasoning))
        except ValueError as error:
            raise FusionError(str(error)) from error
        if not _model(self.main, external=True):
            raise FusionError("Choose a valid Fusion main model.")
        if not self.workers or not all(_model(model, external=True) for model in self.workers):
            raise FusionError("Choose at least one valid worker model.")
        if not _model(self.light, external=True):
            raise FusionError("Choose a valid lightweight model.")
        if (isinstance(self.max_calls, bool) or not isinstance(self.max_calls, int) or self.max_calls < 0
                or isinstance(self.max_tokens, bool) or not isinstance(self.max_tokens, int) or self.max_tokens < 0
                or isinstance(self.max_seconds, bool) or not isinstance(self.max_seconds, int) or not 1 <= self.max_seconds <= 86_400
                or isinstance(self.preference, bool) or not isinstance(self.preference, int) or not 0 <= self.preference <= 100):
            raise FusionError("The Fusion policy is invalid.")

    def allowed(self, model: str, role: str) -> bool:
        return model in self.workers if role == "worker" else role == "light" and model == self.light

    def to_json(self) -> dict[str, Any]:
        return {"main": self.main, "workers": list(self.workers), "light": self.light, "maxCalls": self.max_calls,
                "maxTokens": self.max_tokens, "maxSeconds": self.max_seconds, "preference": self.preference,
                "reasoning": dict(self.reasoning)}

    @classmethod
    def from_json(cls, raw: Any) -> Policy:
        if not isinstance(raw, Mapping) or not isinstance(raw.get("workers"), list):
            raise FusionError("The Fusion policy is missing or damaged.")
        return cls(raw.get("main"), tuple(raw["workers"]), raw.get("light"), raw.get("maxCalls"), raw.get("maxTokens", 0),
                   raw.get("maxSeconds", 900), raw.get("preference", 50), raw.get("reasoning", {}))


@dataclass(slots=True)
class Expert:
    id: str
    scope: str
    model: str
    role: str
    status: str = "idle"
    task: str = ""
    started_at: float = 0.0
    finished_at: float | None = None
    transferred_from: str = ""
    handoff_complete: bool = False
    aliases: tuple[str, ...] = ()
    workstream: str = ""
    paths: tuple[str, ...] = ()
    identity: str = ""

    @property
    def label(self) -> str:
        workstream = self.workstream or self.scope
        return display_name(self.identity, workstream) if self.identity else workstream

    def to_json(self) -> dict[str, Any]:
        return {"id": self.id, "scope": self.scope, "model": self.model, "role": self.role, "status": self.status,
                "task": self.task, "startedAt": self.started_at, "finishedAt": self.finished_at,
                "transferredFrom": self.transferred_from, "handoffComplete": self.handoff_complete, "aliases": list(self.aliases),
                "workstream": self.workstream, "paths": list(self.paths), "identity": self.identity}


@dataclass(slots=True)
class FusionRegistry:
    project: Path
    policy: Policy
    agents: dict[str, Expert] = field(default_factory=dict)
    calls: int = 0
    tokens: int = 0
    unmetered: bool = False
    had_unknown: bool = False

    def __post_init__(self) -> None:
        self.project = Path(self.project).resolve(strict=False)

    @staticmethod
    def _logical(value: Any) -> str:
        if not isinstance(value, str) or not value.strip() or any(c in value for c in "*?\0"):
            raise FusionError("A concrete project-relative module scope is required.")
        raw = value.strip().replace("\\", "/")
        if raw.startswith("/") or ntpath.splitdrive(raw)[0] or any(part in ("", ".", "..") for part in raw.split("/")):
            raise FusionError("A module scope must stay inside the project.")
        return os.path.normcase(raw).replace("\\", "/")

    def _scope(self, value: Any) -> str:
        raw = self._logical(value)
        target = (self.project / raw).resolve(strict=False)
        try:
            target.relative_to(self.project)
        except ValueError as error:
            raise FusionError("A module scope resolves outside the project.") from error
        return os.path.normcase(str(target.relative_to(self.project))).replace("\\", "/")

    @staticmethod
    def _overlaps(left: str, right: str) -> bool:
        return left == right or left.startswith(right + "/") or right.startswith(left + "/")

    @staticmethod
    def _workstream(value: Any) -> str:
        if not isinstance(value, str) or not value.strip() or len(value) > 160 or any(ord(c) < 32 for c in value):
            raise FusionError("Provide a short, stable responsibility or deliverable name, not one expert per file.")
        return value.strip()

    @staticmethod
    def write_paths(expert: Expert) -> tuple[str, ...]:
        return expert.paths if expert.workstream else (expert.scope,)

    def assign_workstream(self, workstream: str, paths: tuple[str, ...], model: str, role: str, *, identity: str = "", reuse: bool = True) -> Expert:
        if not self.policy.allowed(model, role):
            raise FusionError("The requested model is not in the authorised pool for this role.")
        if identity and identity not in _IDENTITIES:
            raise FusionError("The expert identity is invalid.")
        label = self._workstream(workstream)
        if not isinstance(paths, (list, tuple)) or len(paths) > 128:
            raise FusionError("Provide up to 128 project-relative write paths; an empty list reserves the project.")
        canonical = tuple(dict.fromkeys(self._scope(path) for path in paths))
        owners = [agent for agent in self.agents.values() if agent.status != "retired" and agent.identity == identity
                  and (agent.workstream or agent.scope).casefold() == label.casefold()]
        if owners and reuse:
            owner = owners[-1]
            if owner.model != model or owner.role != role:
                raise FusionError(f"This workstream is already assigned to {owner.model} ({owner.role}); reuse it or ask before transferring it.")
        if any(owner.status == "running" for owner in owners):
            raise FusionError("This expert is already running.")
        if owners and reuse:
            owner.workstream, owner.paths = owner.workstream or label, canonical
            return owner
        expert = Expert(uuid.uuid4().hex, canonical[0] if canonical else "__project__", model, role,
                        workstream=label, paths=canonical, identity=identity)
        self.agents[expert.id] = expert
        return expert

    def assign(self, scope: str, model: str, role: str) -> Expert:
        if not self.policy.allowed(model, role):
            raise FusionError("The requested model is not in the authorised pool for this role.")
        logical, canonical = self._logical(scope), self._scope(scope)
        owners = [agent for agent in self.agents.values() if not agent.workstream and agent.status != "retired" and (
            self._overlaps(agent.scope, canonical) or any(self._overlaps(alias, logical) for alias in agent.aliases))]
        if len(owners) > 1:
            raise FusionError("This scope overlaps multiple experts; the main agent must split the task.")
        if owners:
            owner = owners[0]
            if not self._overlaps(owner.scope, canonical):
                raise FusionError("The module path changed its resolved target; inspect it before assigning further work.")
            if owner.model != model or owner.role != role:
                raise FusionError(f"This scope is already assigned to another expert: {owner.model} ({owner.role}). Reuse it or ask the user before transferring it.")
            aliases = owner.aliases
            if not any(alias == logical or logical.startswith(alias + "/") for alias in aliases):
                aliases = (*[alias for alias in aliases if not alias.startswith(logical + "/")], logical)
                if len(aliases) > 128:
                    raise FusionError("This expert has too many distinct scope aliases; choose a broader concrete scope.")
            if owner.scope.startswith(canonical + "/"):
                owner.scope = canonical
            owner.aliases = aliases
            return owner
        expert = Expert(uuid.uuid4().hex, canonical, model, role, aliases=(logical,))
        self.agents[expert.id] = expert
        return expert

    def transfer(self, agent_id: str, model: str, role: str) -> Expert:
        old = self.agents.get(agent_id)
        if old is None or old.status == "retired":
            raise FusionError("The original expert is unavailable or already transferred.")
        if any(agent.status == "running" for agent in self.agents.values()):
            raise FusionError("Wait until the running expert stops before transferring ownership.")
        if not self.policy.allowed(model, role) or (old.model == model and old.role == role):
            raise FusionError("Choose a different model or role from the authorised pool.")
        if old.identity and role != old.role:
            raise FusionError("An identified expert must keep its model role.")
        replacement = Expert(uuid.uuid4().hex, old.scope, model, role, transferred_from=old.id, aliases=old.aliases,
                             workstream=old.workstream, paths=old.paths, identity=old.identity)
        old.status = "retired"
        self.agents[replacement.id] = replacement
        return replacement

    def begin(self, agent_id: str, *, resume: bool = False) -> Expert:
        if (self.policy.max_calls and self.calls >= self.policy.max_calls) or (self.policy.max_tokens and (self.unmetered or self.tokens >= self.policy.max_tokens)):
            raise FusionError("The Fusion budget is exhausted or usage is unknown.")
        expert = self.agents.get(agent_id)
        if expert is None:
            raise FusionError("The expert is not part of this conversation.")
        if expert.status == "retired":
            raise FusionError("This expert was transferred and can no longer be assigned new work.")
        if expert.status == "running":
            raise FusionError("This expert is already running.")
        if expert.status in ("interrupted", "failed") and not resume:
            raise FusionError("The previous result may be incomplete; inspect the workspace and explicitly resume.")
        paths = self.write_paths(expert)
        for other in self.agents.values():
            if other.status != "running":
                continue
            if self.policy.max_tokens:
                raise FusionError("This legacy token-limited session runs experts serially to protect its shared budget.")
            occupied = self.write_paths(other)
            if (expert.identity and not paths) or (other.identity and not occupied):
                continue  # An empty structured brief has no declared writes, not a whole-project reservation.
            if not paths or not occupied or any(self._overlaps(left, right) for left in paths for right in occupied):
                raise FusionError("Another expert is running with overlapping write paths; wait or split the conflicting work.")
        self.calls += 1
        expert.status = "running"
        expert.started_at, expert.finished_at = time.time(), None
        return expert

    def finish(self, agent_id: str, *, status: str, tokens: int | None) -> None:
        expert = self.agents.get(agent_id)
        if expert is None or expert.status != "running" or status not in ("done", "failed", "interrupted"):
            raise FusionError("The expert has no matching running task.")
        if tokens is None:
            self.unmetered = True
            self.had_unknown = True
        elif isinstance(tokens, bool) or not isinstance(tokens, int) or tokens < 0:
            raise FusionError("The expert's usage is invalid.")
        else:
            self.tokens += tokens
        expert.status = status
        expert.finished_at = time.time()

    def to_json(self) -> dict[str, Any]:
        return {"version": 2, "policy": self.policy.to_json(), "agents": [agent.to_json() for agent in self.agents.values()],
                "calls": self.calls, "tokens": self.tokens, "unmetered": self.unmetered, "hadUnknown": self.had_unknown}

    @classmethod
    def from_json(cls, project: Path, raw: Any) -> FusionRegistry:
        if not isinstance(raw, Mapping) or raw.get("version") not in (1, 2) or not isinstance(raw.get("agents"), list):
            raise FusionError("The Fusion state is missing or damaged.")
        calls, tokens, unmetered = raw.get("calls"), raw.get("tokens"), raw.get("unmetered")
        had_unknown = raw.get("hadUnknown", unmetered)
        if (isinstance(calls, bool) or not isinstance(calls, int) or calls < 0 or isinstance(tokens, bool)
                or not isinstance(tokens, int) or tokens < 0 or not isinstance(unmetered, bool) or not isinstance(had_unknown, bool)
                or (unmetered and not had_unknown)):
            raise FusionError("The Fusion budget state is invalid.")
        result = cls(project, Policy.from_json(raw.get("policy")), calls=calls, tokens=tokens, unmetered=unmetered, had_unknown=had_unknown)
        for item in raw["agents"]:
            if not isinstance(item, Mapping):
                raise FusionError("The expert record is invalid.")
            ident, scope, model, role, status = (item.get(k) for k in ("id", "scope", "model", "role", "status"))
            if (not isinstance(ident, str) or len(ident) != 32 or any(c not in "0123456789abcdef" for c in ident)
                    or role not in ("worker", "light") or not _model(model, external=True)
                    or (status != "retired" and not result.policy.allowed(model, role))
                    or status not in ("idle", "running", "done", "failed", "interrupted", "retired")):
                raise FusionError("The expert record is invalid.")
            workstream = item.get("workstream", "")
            if workstream:
                workstream = result._workstream(workstream)
            elif workstream != "":
                raise FusionError("The expert workstream is invalid.")
            identity = item.get("identity", "")
            if identity != "" and (identity not in _IDENTITIES or not workstream):
                raise FusionError("The expert identity is invalid.")
            paths_raw = item.get("paths", [])
            if not isinstance(paths_raw, list) or len(paths_raw) > 128 or (paths_raw and not workstream):
                raise FusionError("The expert write paths are invalid.")
            paths = tuple(result._scope(path) for path in paths_raw)
            if any(path != result._logical(raw_path) for path, raw_path in zip(paths, paths_raw)):
                raise FusionError("A saved write path changed its resolved target.")
            canonical = result._scope(scope)
            if canonical != result._logical(scope):
                raise FusionError("The saved module path changed its resolved target.")
            aliases_raw = item.get("aliases") or [scope]
            if not isinstance(aliases_raw, list) or len(aliases_raw) > 128:
                raise FusionError("The expert scope aliases are invalid.")
            aliases = tuple(result._logical(alias) for alias in aliases_raw)
            # Identified workstreams may keep multiple finished attempts with the same label.
            if ident in result.agents or (not identity and status != "retired" and any(not other.identity and other.status != "retired" and (
                    (bool(workstream) and (other.workstream or other.scope).casefold() == workstream.casefold())
                    or (not workstream and not other.workstream and (result._overlaps(other.scope, canonical)
                        or any(result._overlaps(left, right) for left in other.aliases for right in aliases))))
                    for other in result.agents.values())):
                raise FusionError("The expert ownership records overlap.")
            task, started, finished, source = item.get("task", ""), item.get("startedAt", 0.0), item.get("finishedAt"), item.get("transferredFrom", "")
            handoff = item.get("handoffComplete", False)
            if (not isinstance(handoff, bool) or (handoff and not source) or not isinstance(task, str)
                    or len(task) > (60_000 if identity else 50_000) or isinstance(started, bool) or not isinstance(started, (int, float))
                    or not math.isfinite(started) or started < 0 or not isinstance(source, str) or (source and (len(source) != 32 or any(c not in "0123456789abcdef" for c in source)))
                    or (finished is not None and (isinstance(finished, bool) or not isinstance(finished, (int, float)) or not math.isfinite(finished) or finished < 0))):
                raise FusionError("The expert task record is invalid.")
            if status == "running":
                result.unmetered = result.had_unknown = True
            result.agents[ident] = Expert(ident, canonical, model, role, "interrupted" if status == "running" else status,
                                          task, float(started), float(finished) if finished is not None else None,
                                          source, handoff, aliases, workstream, paths, identity)
        for expert in result.agents.values():
            seen = {expert.id}
            source = expert.transferred_from
            while source:
                previous = result.agents.get(source)
                if source in seen or previous is None or previous.status != "retired" or previous.identity != expert.identity or (
                        (previous.workstream or previous.scope).casefold() != expert.workstream.casefold() if expert.workstream
                        else previous.workstream or not result._overlaps(previous.scope, expert.scope)):
                    raise FusionError("The expert handoff chain is invalid.")
                seen.add(source)
                source = previous.transferred_from
        return result
