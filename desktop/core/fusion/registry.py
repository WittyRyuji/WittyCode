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
from copy import deepcopy
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping, Sequence

from desktop.core.fusion.backends import backend_for
from desktop.core.fusion import allocation, verification_case
from desktop.core.fusion.identity import display_name, load
from desktop.core.fusion.metrics import RETAINED_TASKS, retained_tasks
from desktop.core.fusion.reasoning import reasoning_map

_IDENTITIES = load(None).names
_MAX_ALLOCATIONS = 1024
"""Total frozen work items a session may carry; it also bounds dependency checks."""


class FusionError(ValueError):
    """A Fusion action cannot be performed under the session's policy."""


def _model(value: Any) -> bool:
    return isinstance(value, str) and backend_for(value) is not None


@dataclass(frozen=True, slots=True)
class Policy:
    main: str
    workers: tuple[str, ...]
    light: str
    max_calls: int
    max_tokens: int = 0
    max_seconds: int = 900
    preference: int = 50  # 0 favours cost, 100 favours quality
    reasoning: Mapping[str, str] = field(default_factory=dict)
    max_seconds_adaptive: bool = False  # Legacy/session-specific policies have an explicit hard limit.

    lights: tuple[str, ...] = ()  # First entry mirrors the original scalar for legacy routes.

    def __post_init__(self) -> None:
        if not isinstance(self.lights, (list, tuple)) or not all(_model(model) for model in self.lights):
            raise FusionError("Choose valid lightweight models.")
        object.__setattr__(self, "lights", tuple(dict.fromkeys((self.light, *self.lights))))
        try:
            object.__setattr__(self, "reasoning", reasoning_map(self.reasoning))
        except ValueError as error:
            raise FusionError(str(error)) from error
        if not _model(self.main):
            raise FusionError("Choose a valid Fusion main model.")
        if not self.workers or not all(_model(model) for model in self.workers):
            raise FusionError("Choose at least one valid worker model.")
        if not _model(self.light):
            raise FusionError("Choose a valid lightweight model.")
        if (isinstance(self.max_calls, bool) or not isinstance(self.max_calls, int) or self.max_calls < 0
                or isinstance(self.max_tokens, bool) or not isinstance(self.max_tokens, int) or self.max_tokens < 0
                or type(self.max_seconds_adaptive) is not bool
                or isinstance(self.max_seconds, bool) or not isinstance(self.max_seconds, int) or not 1 <= self.max_seconds <= 86_400
                or isinstance(self.preference, bool) or not isinstance(self.preference, int) or not 0 <= self.preference <= 100):
            raise FusionError("The Fusion policy is invalid.")

    def seat_model(self, seat: str) -> str:
        """The policy model of one seat: L1 main, L2 worker, L3 light."""
        if seat == "L1":
            return self.main
        if seat == "L2":
            return self.workers[0]
        if seat == "L3":
            return self.light
        raise FusionError("The Fusion seat is invalid.")

    def allowed(self, model: str, role: str, seat: str = "") -> bool:
        """A seated expert may only run its seat's policy model; tool permissions stay with the role."""
        if seat:
            return seat in allocation.SEATS and model == self.seat_model(seat)
        return model in self.workers if role == "worker" else role == "light" and model == self.light

    def to_json(self) -> dict[str, Any]:
        return {"main": self.main, "workers": list(self.workers), "light": self.light, "maxCalls": self.max_calls,
                "maxTokens": self.max_tokens, "maxSeconds": self.max_seconds, "maxSecondsAdaptive": self.max_seconds_adaptive,
                "preference": self.preference,
                "lights": list(self.lights), "reasoning": dict(self.reasoning)}

    @classmethod
    def from_json(cls, raw: Any) -> Policy:
        if not isinstance(raw, Mapping) or not isinstance(raw.get("workers"), list):
            raise FusionError("The Fusion policy is missing or damaged.")
        return cls(raw.get("main"), tuple(raw["workers"]), raw.get("light"), raw.get("maxCalls"), raw.get("maxTokens", 0),
                   raw.get("maxSeconds", 900), raw.get("preference", 50), raw.get("reasoning", {}),
                   raw.get("maxSecondsAdaptive", "maxSeconds" not in raw), raw.get("lights", ()))


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
    seat: str = ""
    replaced_status: str = ""  # Retired by a seat/role model change; successor is allocated on actual delegation.
    allocation_workstream: str = ""  # The exact frozen key's label for a legacy scope, before path normalization.

    plan: str = ""  # Immutable plan key; workstream is the unit id, stage selects one exact roster entry.
    stage: str = ""
    revision: int = 0  # Checks for a new author revision never inherit stale check history.

    @property
    def label(self) -> str:
        workstream = self.workstream or self.scope
        return display_name(self.identity, workstream) if self.identity else workstream

    def to_json(self) -> dict[str, Any]:
        return {"id": self.id, "scope": self.scope, "model": self.model, "role": self.role, "status": self.status,
                "task": self.task, "startedAt": self.started_at, "finishedAt": self.finished_at,
                "transferredFrom": self.transferred_from, "handoffComplete": self.handoff_complete, "aliases": list(self.aliases),
                "workstream": self.workstream, "paths": list(self.paths), "identity": self.identity, "seat": self.seat,
                **({"plan": self.plan, "stage": self.stage, "revision": self.revision} if self.plan else {}),
                **({"replacedStatus": self.replaced_status} if self.replaced_status else {}),
                **({"allocationWorkstream": self.allocation_workstream} if self.allocation_workstream else {})}


@dataclass(slots=True)
class FusionRegistry:
    project: Path
    policy: Policy
    agents: dict[str, Expert] = field(default_factory=dict)
    calls: int = 0
    tokens: int = 0
    unmetered: bool = False
    had_unknown: bool = False
    misroutes: int = 0
    task_metrics: list[dict] = field(default_factory=list)
    allocations: dict[tuple[str, str], dict[str, Any]] = field(default_factory=dict)
    """Frozen route items by ``(workstream.casefold(), role)``; each holds id, N, score, seat and the frozen scope."""
    preset_snapshot: dict[str, Any] | None = None  # Not persisted in the registry; committed with saved.origin.

    plans: dict[str, dict] = field(default_factory=dict)

    def binding(self, plan: str, unit: str, stage: str) -> dict:
        try:
            return self.plans[plan]['spec']['units'][unit]['roster'][stage]
        except (KeyError, TypeError) as error:
            raise FusionError('Unknown frozen unit or stage.') from error

    def authorized(self, model, role, seat, identity, unit, plan, stage):
        if not plan:
            return not stage and self.policy.allowed(model, role, seat)
        bound = self.binding(plan, unit, stage)
        pool = {'L1': (self.policy.main,), 'L2': self.policy.workers, 'L3': self.policy.lights}.get(seat, ())
        return (model in pool and (model, role, seat, identity) ==
                (bound['model'], bound['role'], bound['seat'], bound['identity']))

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

    def assign_workstream(self, workstream: str, paths: tuple[str, ...], model: str, role: str, *, identity: str = "",
                          reuse: bool = True, resume: bool = False, seat: str = "", recover: Expert | None = None,
                          plan: str = "", stage: str = "") -> Expert:
        if not self.authorized(model, role, seat, identity, workstream, plan, stage):
            raise FusionError("The requested model is not in the authorised pool for this role.")
        if identity and not plan and identity not in _IDENTITIES:
            raise FusionError("The expert identity is invalid.")
        label = self._workstream(workstream)
        if not isinstance(paths, (list, tuple)) or len(paths) > 128:
            raise FusionError("Provide up to 128 project-relative write paths; an empty list reserves the project.")
        canonical = tuple(dict.fromkeys(self._scope(path) for path in paths))
        if plan and canonical != tuple(self.plans[plan]['spec']['units'][label]['paths'] if stage == 'worker' else ()):
            raise FusionError('The delegation does not match the frozen write scope.')
        previous = self.workstream_expert(label, role, identity=identity, reuse=reuse, resume=resume, recover=recover,
                                          plan=plan, stage=stage)
        if previous is not None:
            if previous.seat and previous.seat != seat:
                raise FusionError("This workstream's seat is frozen; the route cannot reseat it.")
            if (previous.allocation_workstream and self._allocation_binding(label, role)
                    != allocation.key(previous.allocation_workstream, previous.role)):
                raise FusionError("This workstream already owns a different frozen work item.")
            if previous.status != "retired":
                if previous.model != model or previous.role != role:
                    raise FusionError(f"This workstream is already assigned to {previous.model} ({previous.role}); reuse it or ask before transferring it.")
                previous.workstream, previous.paths = previous.workstream or label, canonical
                previous.seat, previous.allocation_workstream = seat, ""
                return previous
        expert = Expert(uuid.uuid4().hex, canonical[0] if canonical else "__project__", model, role,
                        workstream=label, paths=canonical, identity=identity, seat=seat, plan=plan, stage=stage)
        if plan:
            expert.revision = self.plans[plan]['cases'][label]['revision']
        self._link_replacement(expert, previous)
        self.agents[expert.id] = expert
        return expert

    def workstream_expert(self, workstream: str, role: str, *, identity: str, reuse: bool, resume: bool,
                          recover: Expert | None = None, plan: str = "", stage: str = "") -> Expert | None:
        """Resolve an owner or pending predecessor without changing ownership or choosing a model."""
        revision = self.plans[plan]['cases'][workstream]['revision'] if plan and stage != 'worker' else None
        label = self._workstream(workstream).casefold()
        matches = [agent for agent in self.agents.values() if agent.identity == identity and agent.plan == plan and agent.stage == stage
                   and (revision is None or agent.revision == revision) and (agent.workstream or agent.scope).casefold() == label]
        owners = [agent for agent in matches if agent.status != "retired"]
        if any(owner.status == "running" for owner in owners):
            raise FusionError("This expert is already running.")
        if recover is not None:
            if (identity not in ("Runner", "QualitySampler") or recover.paths or recover.role != role
                    or not any(owner is recover for owner in owners)):
                raise FusionError("The original verification expert is no longer available.")
            return recover
        if resume and len(owners) > 1:
            raise FusionError("Several experts match this workstream; use a unique workstream before resuming.")
        if not (reuse or resume):
            return None
        if owners:
            return owners[-1]
        source = self._predecessor(self._pending_replacements([agent for agent in matches if agent.role == role]))
        if resume and source is None:
            raise FusionError("No existing expert matches this workstream and model role.")
        return source

    def _pending_replacements(self, candidates: list[Expert]) -> list[Expert]:
        handed_off = {agent.transferred_from for agent in self.agents.values()}
        return [agent for agent in candidates if agent.replaced_status and agent.id not in handed_off]

    @staticmethod
    def _predecessor(available: list[Expert]) -> Expert | None:
        if len(available) > 1:
            raise FusionError("Several archived experts match this workstream; no predecessor was selected.")
        return available[0] if available else None

    @staticmethod
    def _link_replacement(expert: Expert, source: Expert | None) -> None:
        if source is not None:
            expert.transferred_from = source.id
            if source.replaced_status in ("failed", "interrupted"):
                expert.status = "interrupted"

    def assign(self, scope: str, model: str, role: str, *, resume: bool = False, seat: str = "") -> Expert:
        if not self.policy.allowed(model, role, seat):
            raise FusionError("The requested model is not in the authorised pool for this role.")
        logical, canonical = self._logical(scope), self._scope(scope)
        binding = self._allocation_binding(scope, role)
        item = self.allocations.get(binding) if seat else None
        if item is not None and item["seat"] != seat:
            raise FusionError("This scope's seat is frozen; the route cannot reseat it.")
        frozen_label = item["workstream"] if item is not None else ""
        matches = [agent for agent in self.agents.values() if not agent.workstream and (
            self._overlaps(agent.scope, canonical) or any(self._overlaps(alias, logical) for alias in agent.aliases))]
        owners = [agent for agent in matches if agent.status != "retired"]
        if len(owners) > 1:
            raise FusionError("This scope overlaps multiple experts; the main agent must split the task.")
        if owners:
            owner = owners[0]
            if not self._overlaps(owner.scope, canonical):
                raise FusionError("The module path changed its resolved target; inspect it before assigning further work.")
            if owner.model != model or owner.role != role:
                raise FusionError(f"This scope is already assigned to another expert: {owner.model} ({owner.role}). Reuse it or ask the user before transferring it.")
            if owner.seat and seat and owner.seat != seat:
                raise FusionError("This scope's seat is frozen; the route cannot reseat it.")
            if seat and owner.allocation_workstream and allocation.key(owner.allocation_workstream, role) != binding:
                raise FusionError("This scope already owns a different frozen work item.")
            aliases = owner.aliases
            if not any(alias == logical or logical.startswith(alias + "/") for alias in aliases):
                aliases = (*[alias for alias in aliases if not alias.startswith(logical + "/")], logical)
                if len(aliases) > 128:
                    raise FusionError("This expert has too many distinct scope aliases; choose a broader concrete scope.")
            if owner.scope.startswith(canonical + "/"):
                owner.scope = canonical
            owner.aliases = aliases
            owner.seat, owner.allocation_workstream = seat, frozen_label
            return owner
        predecessors = self._pending_replacements(matches)
        for agent in predecessors:
            if agent.seat or (seat and agent.role == role):
                original = agent.aliases[0] if agent.aliases else agent.scope
                # Old checkpoints lost the spelling; after checking scope and seat, use the exact requested item.
                prior = allocation.key(agent.allocation_workstream, agent.role) if agent.allocation_workstream else None
                if (agent.role != role or (agent.seat and agent.seat != seat)
                        or agent.scope != canonical or original != logical or (prior is not None and binding != prior)):
                    raise FusionError("This scope overlaps a retired expert's frozen work item; resume its original scope and seat.")
        source = self._predecessor([agent for agent in predecessors if agent.role == role and self._overlaps(agent.scope, canonical)])
        if resume and source is None:
            raise FusionError("No existing expert matches this scope and model role.")
        expert = Expert(uuid.uuid4().hex, canonical, model, role, aliases=(logical,), seat=seat,
                        allocation_workstream=frozen_label)
        self._link_replacement(expert, source)
        self.agents[expert.id] = expert
        return expert

    def transfer(self, agent_id: str, model: str, role: str) -> Expert:
        old = self.agents.get(agent_id)
        if old is None or old.status == "retired":
            raise FusionError("The original expert is unavailable or already transferred.")
        if any(agent.status == "running" for agent in self.agents.values()):
            raise FusionError("Wait until the running expert stops before transferring ownership.")
        if old.plan:
            raise FusionError('This expert is bound to a frozen unit; transferring it requires a new plan.')
        if not self.policy.allowed(model, role, old.seat) or (old.model == model and old.role == role):
            raise FusionError("Choose a different model or role from the authorised pool.")
        if old.identity and role != old.role:
            raise FusionError("An identified expert must keep its model role.")
        replacement = Expert(uuid.uuid4().hex, old.scope, model, role, transferred_from=old.id, aliases=old.aliases,
                             workstream=old.workstream, paths=old.paths, identity=old.identity, seat=old.seat,
                             allocation_workstream=old.allocation_workstream if old.role == role else "")
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
        item = self.allocations.get(self._expert_allocation_binding(expert))
        if expert.plan:
            plan = self.plans[expert.plan]
            state = plan['cases'][expert.workstream]
            if (not plan['activated'] or state['next'] != expert.stage or not self.authorized(
                    expert.model, expert.role, expert.seat, expert.identity, expert.workstream, expert.plan, expert.stage)):
                raise FusionError('The frozen stage is not authorized to run.')
            if any(plan['cases'][dep]['next'] != 'done' for dep in plan['spec']['units'][expert.workstream]['deps']):
                raise FusionError('This unit waits on unverified dependencies.')
        elif item is not None and (pending := self._unfinished_deps(item["deps"])):
            raise FusionError(f"This work item waits on unfinished dependencies: {', '.join(pending)}.")
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

    @staticmethod
    def _allocation_scope_key(workstream: str, role: str) -> tuple[str, str]:
        return allocation.key(workstream.replace("\\", "/"), role)

    def _equivalent_allocation(self, workstream: str, role: str, *, seat: str = "") -> tuple[str, str] | None:
        scope_key = self._allocation_scope_key(workstream, role)
        return next((bind for bind, record in self.allocations.items()
                     if self._allocation_scope_key(*bind) == scope_key and (not seat or record["seat"] == seat)), None)

    def _allocation_binding(self, workstream: str, role: str) -> tuple[str, str]:
        # Keep exact lookups of older checkpoints with both spellings; normalize new aliases to the existing item.
        bind = allocation.key(workstream, role)
        return bind if bind in self.allocations else self._equivalent_allocation(workstream, role) or bind

    def allocation_for(self, workstream: str, role: str) -> dict[str, Any] | None:
        """The frozen route item binding this workstream and role, if the route declared one."""
        return self.allocations.get(self._allocation_binding(workstream, role))

    def _expert_allocation_binding(self, expert: Expert) -> tuple[str, str]:
        if expert.allocation_workstream:
            return allocation.key(expert.allocation_workstream, expert.role)
        if expert.seat and not expert.workstream:
            # Older records have only a normalized scope. Try that exact key before reconstructing by seat.
            logical = expert.aliases[0] if expert.aliases else expert.scope
            bind = self._allocation_binding(logical, expert.role)
            if self.allocations.get(bind, {}).get("seat") == expert.seat:
                return bind
            return self._equivalent_allocation(logical, expert.role, seat=expert.seat) or bind
        return self._allocation_binding(expert.workstream or expert.scope, expert.role)

    def _expert_owns_allocation(self, expert: Expert, bind: tuple[str, str]) -> bool:
        if expert.plan or expert.role != bind[1]:
            return False
        if self._expert_allocation_binding(expert) == bind:
            return True
        scope_key = self._allocation_scope_key(*bind)
        labels = (expert.workstream,) if expert.workstream else (expert.scope, *expert.aliases)
        return any(self._allocation_scope_key(label, expert.role) == scope_key for label in labels)

    def allocation_covers(self, record: Mapping[str, Any], scope: str, paths: tuple[str, ...] | None) -> bool:
        """The delegation's write scope stays inside the frozen one; ``paths=None`` is the legacy module scope."""
        given = tuple(self._scope(path) for path in (paths if paths is not None else (scope,)))
        return allocation.covers(record, given)

    def _unfinished_deps(self, deps: Sequence[str]) -> list[str]:
        """Dependency ids whose bound expert is not done; a dep without an owner has not even started."""
        owners = {self._expert_allocation_binding(agent): agent
                  for agent in self.agents.values() if agent.status != "retired" and not agent.plan}
        records = {record["id"]: record for record in self.allocations.values()}
        pending = []
        for dep in deps:
            record = records.get(dep)
            owner = None if record is None else owners.get(allocation.key(record["workstream"], record["role"]))
            if owner is None or owner.status != "done":
                pending.append(dep)
        return pending

    def freeze(self, items: Sequence[Mapping[str, Any]], N: int) -> int:
        """Freeze route items durably under the current preference: an identical re-freeze keeps the
        original record even after the preference moved; a changed item only passes as an explicit
        ``new_facts`` re-estimate before its workstream has an owner, otherwise it is refused."""
        sealed = []
        for raw in items:
            try:
                record = allocation.seal(raw, N)
                record["paths"] = tuple(self._scope(path) for path in record["paths"])
            except ValueError as error:
                raise FusionError(str(error)) from error
            sealed.append(record)
        binds = [self._allocation_binding(record["workstream"], record["role"]) for record in sealed]
        if len({self._allocation_scope_key(*bind) for bind in binds}) != len(binds) or len({record["id"] for record in sealed}) != len(sealed):
            raise FusionError("Each work item needs a distinct id, workstream and role.")
        if len(self.allocations) + len({bind for bind in binds if bind not in self.allocations}) > _MAX_ALLOCATIONS:
            raise FusionError(f"A session freezes at most {_MAX_ALLOCATIONS} work items.")
        pending = []
        for record, bind in zip(sealed, binds):
            existing = self.allocations.get(bind)
            if existing is not None:
                # Ignore a separator-only spelling change, keeping the original workstream in storage and receipts.
                if existing["workstream"].replace("\\", "/") == record["workstream"].replace("\\", "/"):
                    record["workstream"] = existing["workstream"]
                if allocation.same(existing, record):
                    continue  # An identical re-freeze keeps the original record, its N and its seat.
                if "new_facts" not in record:
                    raise FusionError("This workstream's item is already frozen; re-scoring, renaming or re-scoping it is not allowed.")
                # A preset change can retire the owner before its successor exists; it is still a dispatched item.
                if any((agent.status != "retired" or agent.replaced_status) and self._expert_owns_allocation(agent, bind)
                       for agent in self.agents.values()):
                    raise FusionError("This work item already has an owner; a changed estimate requires a new plan.")
                if not allocation.reestimate(existing, record):
                    raise FusionError("New facts may only re-estimate this item's score; changed ids, paths or dependencies require a new plan.")
            other = next((other for other_bind, other in self.allocations.items()
                          if other_bind != bind and other["id"] == record["id"]), None)
            if other is not None:
                raise FusionError(f"Work item id {record['id']!r} is already frozen under another workstream or role: "
                                  f"workstream {other['workstream']!r}, role {other['role']!r}. Reuse that frozen item unchanged.")
            pending.append((record, bind))
        if problem := allocation.check_deps([*self.allocations.values(), *(record for record, bind in pending
                                                                         if bind not in self.allocations)]):
            raise FusionError(problem)
        for record, bind in pending:
            self.allocations[bind] = record
        return len(sealed)

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

    def record_task(self, task: dict) -> None:
        self.task_metrics = [*self.task_metrics, task][-RETAINED_TASKS:]

    def to_json(self) -> dict[str, Any]:
        return {"version": 2, "policy": self.policy.to_json(), "agents": [agent.to_json() for agent in self.agents.values()],
                "calls": self.calls, "tokens": self.tokens, "unmetered": self.unmetered, "hadUnknown": self.had_unknown,
                "misroutes": self.misroutes, "metrics": self.task_metrics[-RETAINED_TASKS:],
                **({'unitPlans': deepcopy(self.plans)} if self.plans else {}),
                "allocations": [dict(record) for record in self.allocations.values()]}

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
        misroutes = raw.get("misroutes", 0)
        if type(misroutes) is not int or misroutes < 0:
            raise FusionError("The Fusion misroute count is invalid.")
        result = cls(project, Policy.from_json(raw.get("policy")), calls=calls, tokens=tokens, unmetered=unmetered,
                     had_unknown=had_unknown, misroutes=misroutes, task_metrics=retained_tasks(raw.get("metrics")))
        plans = raw.get('unitPlans', {})
        if not isinstance(plans, dict) or len(plans) > _MAX_ALLOCATIONS:
            raise FusionError('Invalid frozen plan ledger.')
        try:
            result.plans = {key: verification_case.restore(key, deepcopy(plan)) for key, plan in plans.items()}
        except (TypeError, ValueError, KeyError, AttributeError) as error:
            raise FusionError('Invalid frozen plan ledger.') from error
        allocations = raw.get("allocations", [])
        if not isinstance(allocations, list) or len(allocations) > _MAX_ALLOCATIONS:
            raise FusionError("The Fusion allocation records are invalid.")
        for entry in allocations:
            try:
                record = allocation.seal(entry, entry.get("N") if isinstance(entry, Mapping) else None)
                record["paths"] = tuple(result._scope(path) for path in record["paths"])
            except ValueError as error:
                raise FusionError("The Fusion allocation records are invalid.") from error
            bind = allocation.key(record["workstream"], record["role"])
            if bind in result.allocations or any(other["id"] == record["id"] for other in result.allocations.values()):
                raise FusionError("The Fusion allocation records overlap.")
            result.allocations[bind] = record
        if problem := allocation.check_deps(list(result.allocations.values())):
            raise FusionError("The Fusion allocation records are invalid.")
        for item in raw["agents"]:
            if not isinstance(item, Mapping):
                raise FusionError("The expert record is invalid.")
            ident, scope, model, role, status = (item.get(k) for k in ("id", "scope", "model", "role", "status"))
            seat = item.get("seat", "")
            plan, stage = item.get('plan', ''), item.get('stage', '')
            if (not isinstance(ident, str) or len(ident) != 32 or any(c not in "0123456789abcdef" for c in ident)
                    or role not in ("worker", "light") or not _model(model)
                    or seat not in ("", *allocation.SEATS)
                    or not isinstance(plan, str) or not isinstance(stage, str)
                    or (status != "retired" and not result.authorized(model, role, seat, item.get('identity'),
                                                                      item.get('workstream'), plan, stage))
                    or status not in ("idle", "running", "done", "failed", "interrupted", "retired")):
                raise FusionError("The expert record is invalid.")
            workstream = item.get("workstream", "")
            if workstream:
                workstream = result._workstream(workstream)
            elif workstream != "":
                raise FusionError("The expert workstream is invalid.")
            identity = item.get("identity", "")
            if plan:
                bound = result.binding(plan, workstream, stage)
                expected = result.plans[plan]['spec']['units'][workstream]['paths'] if stage == 'worker' else []
                if (identity != bound['identity'] or model != bound['model'] or item.get('paths') != expected
                        or role != bound['role'] or seat != bound['seat']):
                    raise FusionError('A saved expert does not match its frozen binding.')
            elif identity != "" and (identity not in _IDENTITIES or not workstream):
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
            frozen_label = item.get("allocationWorkstream", "")
            if not isinstance(frozen_label, str):
                raise FusionError("The expert's frozen work item is invalid.")
            if frozen_label:
                frozen = result.allocations.get(allocation.key(frozen_label, role))
                label, _ = result._allocation_scope_key(frozen_label, role)
                if (workstream or not seat or frozen is None
                        or not any(label == alias.casefold() or label.startswith(alias.casefold() + "/") for alias in aliases)):
                    raise FusionError("The expert's frozen work item is invalid.")
                # Older checkpoints could re-estimate an owned alias when its resolved scope had a different name.
                # Keep both records readable without silently changing the expert's seat or frozen item.
                if frozen["seat"] != seat and ("new_facts" not in frozen or label == canonical.casefold()):
                    raise FusionError("The expert's frozen work item is invalid.")
            # Identified workstreams may keep multiple finished attempts with the same label.
            if ident in result.agents or (not identity and status != "retired" and any(not other.identity and other.status != "retired" and (
                    (bool(workstream) and (other.workstream or other.scope).casefold() == workstream.casefold())
                    or (not workstream and not other.workstream and (result._overlaps(other.scope, canonical)
                        or any(result._overlaps(left, right) for left in other.aliases for right in aliases))))
                    for other in result.agents.values())):
                raise FusionError("The expert ownership records overlap.")
            task, started, finished, source = item.get("task", ""), item.get("startedAt", 0.0), item.get("finishedAt"), item.get("transferredFrom", "")
            handoff = item.get("handoffComplete", False)
            replaced = item.get("replacedStatus", "")
            if replaced not in ("", "idle", "done", "interrupted", "failed") or (replaced and status != "retired"):
                raise FusionError("The retired expert checkpoint is invalid.")
            if (not isinstance(handoff, bool) or (handoff and not source) or not isinstance(task, str)
                    or len(task) > (60_000 if identity else 50_000) or isinstance(started, bool) or not isinstance(started, (int, float))
                    or not math.isfinite(started) or started < 0 or not isinstance(source, str) or (source and (len(source) != 32 or any(c not in "0123456789abcdef" for c in source)))
                    or (finished is not None and (isinstance(finished, bool) or not isinstance(finished, (int, float)) or not math.isfinite(finished) or finished < 0))):
                raise FusionError("The expert task record is invalid.")
            if status == "running":
                result.unmetered = result.had_unknown = True
            result.agents[ident] = Expert(ident, canonical, model, role, "interrupted" if status == "running" else status,
                                          task, float(started), float(finished) if finished is not None else None,
                                          source, handoff, aliases, workstream, paths, identity,
                                          seat=seat, replaced_status=replaced, allocation_workstream=frozen_label, plan=plan, stage=stage, revision=item.get('revision', 0))
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
        for key, plan in result.plans.items():
            for name, case in plan['cases'].items():
                for stage, proof in case['receipts'].items():
                    owner = result.agents.get(proof['expert'])
                    if (owner is None or (owner.plan, owner.workstream, owner.stage, owner.revision) !=
                            (key, name, stage, case['revision']) or (owner.replaced_status or owner.status) != 'done'):
                        raise FusionError('Frozen evidence does not belong to a completed expert of this unit revision.')
        return result
