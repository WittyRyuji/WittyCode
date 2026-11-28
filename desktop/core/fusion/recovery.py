"""Durable recovery records for a frozen plan. None of these records execute an action."""
from __future__ import annotations

import json
import math
import re
import time
import unicodedata
from dataclasses import asdict, dataclass, replace
from typing import TYPE_CHECKING

from desktop.core.fusion import verification_case as ledger
from desktop.core.prompts import APPROVAL_OPTIONS, ActionView
from desktop.core.transcript import PermissionPrompt

if TYPE_CHECKING:
    from desktop.core.harness.store import SavedSession, SavedTurn

MAX_REQUESTS = 32
MAX_BACKOFF = 15 * 60
MAX_RETRIES = 2
RESUME_FOCUS = 'fusionResumeFocus'
_RESUME_TEXTS = frozenset({'继续', '继续原任务', '继续,上次没返回', '继续,上次没有返回', 'resume', 'continue'})
_PLAN_RECEIPT = re.compile(r'Work plan validated: ([0-9a-f]{64})\.')
_ROUTE_RECEIPTS = frozenset(f'Frozen route active: {kind}. Use delegate_task({{"unit":"id"}}).'
                            for kind in ('self', 'coder', 'split'))


@dataclass(frozen=True, slots=True)
class ResumeTarget:
    task: str
    kind: str
    plan: str = ''
    label: str = ''


def resume_text(text: str) -> bool:
    return unicodedata.normalize('NFKC', text).strip().rstrip('。.!！').casefold() in _RESUME_TEXTS


def task_id(turn: SavedTurn) -> str:
    return turn.fusion_task or ledger.digest(['fusion-task', turn.at, turn.start])


def resumable_plan(plan: dict) -> bool:
    """A completed/cancelled plan, or one with only hard-stopped dependency chains, is not a target."""
    if not plan.get('activated') or plan.get('cancelled'):
        return False
    cases = plan['cases']
    main = plan.get('mainRecovery', {})
    return bool(ledger.awaiting_user(main) or waiting(main) or main.get('resumeSignal') or any(
        case['next'] in ledger.STAGES and all(cases[dep]['next'] == 'done' for dep in plan['spec']['units'][name]['deps'])
        for name, case in cases.items()))


def can_start_manual(plan: dict) -> bool:
    main = plan.get('mainRecovery', {})
    return not (waiting(main) or ledger.awaiting_user(main)) and any(
        plan['cases'][name]['next'] in ledger.STAGES
        and all(plan['cases'][dep]['next'] == 'done' for dep in plan['spec']['units'][name]['deps'])
        for name in runnable(plan))


def _legacy_target(saved: SavedSession, start: int, end: int) -> tuple[str, str]:
    """Match application-generated outputs to actual tool calls in this one turn, never assistant prose."""
    if not 0 <= start < end <= len(saved.items) or end - start > 4096:
        return '', ''
    calls = {}
    kind = proposed = active = ''
    for item in saved.items[start:end]:
        ident = item.get('call_id')
        if not isinstance(ident, str):
            continue
        if item.get('type') == 'function_call' and item.get('name') in ('declare_topic', 'submit_work_plan', 'submit_route'):
            calls[ident] = item
            continue
        if item.get('type') != 'function_call_output' or (call := calls.pop(ident, None)) is None:
            continue
        if saved.tools.get(ident, {}).get('status') != 'done' or not isinstance(item.get('output'), str):
            continue
        line = item['output'].partition('\n')[0]
        try:
            args = json.loads(call.get('arguments', '{}'))
        except (ValueError, TypeError):
            continue
        if not isinstance(args, dict):
            continue
        if call['name'] == 'declare_topic' and args.get('intent') == 'execute' and not kind:
            if args.get('kind') == 'simple' and line.startswith('Declared simple: no debate.'):
                kind = 'simple'
            elif args.get('kind') == 'complex':
                kind = 'complex'
        elif call['name'] == 'submit_work_plan' and kind == 'complex' and (match := _PLAN_RECEIPT.fullmatch(line)):
            proposed = match[1]
        elif call['name'] == 'submit_route' and not args and proposed and line in _ROUTE_RECEIPTS:
            active = proposed
    return kind, active


def resume_targets(saved: SavedSession, plans: dict, *, selected_task: str = '') -> tuple[ResumeTarget | None, tuple[ResumeTarget, ...]]:
    from desktop.core.chatgpt.history import user_parts

    # Existing checkpoint metadata wins. Reconstruct only from a bounded suffix of this already-loaded session.
    choices, seen = [], set()
    direct = None
    anchor = False
    for index in range(len(saved.turns) - 1, max(-1, len(saved.turns) - 65), -1):
        turn = saved.turns[index]
        if turn.finished is None or not 0 <= turn.start < len(saved.items):
            continue
        item = saved.items[turn.start]
        if item.get('type') != 'message' or item.get('role') != 'user':
            break  # a missing send record cannot establish what the user intended
        text, attachments, handoff = user_parts(item)
        if not turn.fusion_task and not turn.fusion_plan and not attachments and not handoff and resume_text(text):
            continue  # old unbound "continue" turns may have been misclassified; they never become the origin
        latest = not anchor
        anchor = True
        kind, key = turn.fusion_kind, turn.fusion_plan
        if not kind:
            end = saved.turns[index + 1].start if index + 1 < len(saved.turns) else len(saved.items)
            kind, old_key = ('complex', key) if key else _legacy_target(saved, turn.start, end)
            key = key or old_key
        task = task_id(turn)
        ident = key or task
        if ident in seen:
            continue
        seen.add(ident)
        if turn.fusion_outcome in ('cancelled', 'verified'):
            continue
        if kind == 'complex':
            plan = plans.get(key)
            if not plan or not resumable_plan(plan):
                continue
            text = plan['spec']['goal'] or text
        elif (kind != 'simple' or key or not latest and task != selected_task
              and turn.status not in ('interrupted', 'incomplete', 'failed', 'limit')):
            continue
        elif turn.fusion_resume_source:
            # A second manual resume has the same task id, but its user text is only "continue".
            origin = next((t for t in reversed(saved.turns[max(0, index - 64):index]) if not t.fusion_resume_source
                           and task_id(t) == task and 0 <= t.start < len(saved.items)), None)
            text = user_parts(saved.items[origin.start])[0] if origin else ''
        label = ' '.join(text.split())[:80] or ('冻结工单' if key else '简单任务')
        target = ResumeTarget(task, kind, key, label)
        choices.append(target)
        if latest:
            direct = target
    return (direct if len(choices) == 1 else None), tuple(choices)


def restored_focus(saved: SavedSession, plans: dict) -> ResumeTarget | None:
    row = saved.origin.get(RESUME_FOCUS)
    if not isinstance(row, dict) or not saved.turns or row.get('after') != saved.turns[-1].start:
        return None
    _, choices = resume_targets(saved, plans)
    return next((target for target in choices if target.plan and (target.plan, target.task) == (row.get('plan'), row.get('task'))), None)


def file_fact(project, relative):
    """A bounded read-only observation, not proof that an interrupted operation succeeded."""
    from pathlib import Path
    from desktop.core.fusion.registry import FusionRegistry

    try:
        path = (project / FusionRegistry._logical(relative)).resolve()
        if not path.is_relative_to(project) or path.is_symlink():
            return None
        with path.open('rb') as stream:
            data = stream.read(512 * 1024 + 1)
        if len(data) <= 512 * 1024:
            return {'path': Path(path.relative_to(project)).as_posix(), 'fingerprint': ledger.progress_id(relative, data)}
    except FileNotFoundError:
        return {'path': relative, 'fingerprint': 'absent'}
    except (OSError, ValueError, RuntimeError):
        pass
    return None


def owner_version(owner):
    return [owner.get('revision', 0), owner.get('next', 'main'), len(owner.get('decisions', ())) + len(owner.get('rebindings', ()))]


def action_key(call):
    return ledger.digest([call.name, call.args])


def authorization(owner, call):
    key = action_key(call)
    version = owner_version(owner)
    return next((row for row in reversed(tuple(owner.get('permissions', {}).values())) if row['actionKey'] == key
                 and (row['version'] == version or row['status'] in ('declined', 'consumed') and row['version'][:2] == version[:2])), None)


def permission_scope(call, tools):
    from desktop.core.harness.store import diff_to_json

    action = tools.action(call)
    scope = action.impact
    if action.kind == 'mcp':
        # Persist only the bounded, reviewable non-secret arguments. Ask for a user prerequisite for opaque actions.
        raw = json.dumps(call.args, ensure_ascii=False, sort_keys=True)
        if len(raw) > 1200 or re.search(r'(?i)secret|token|password|credential|api[_ -]?key|authorization|bearer', raw):
            return None
        action = replace(action, reason=f'调用参数：{raw}')
    display = asdict(replace(action, impact=scope, diffs=()))
    display.pop('tool_use_id')
    display['diffs'] = [diff_to_json(diff) for diff in action.diffs]
    return display


def permission_request(key, name, owner, call, tools, reason):
    scope = permission_scope(call, tools)
    if scope is None:
        return None
    from pathlib import Path

    def shown(path):
        if not path:
            return path
        path_obj = Path(path)
        return path_obj.relative_to(tools.context.project).as_posix() if path_obj.is_relative_to(tools.context.project) else path

    display = {**scope, 'tool_use_id': call.call_id, 'title': f"{name}：{scope['title']}", 'path': shown(scope['path']),
               'diffs': [{**diff, 'path': shown(diff['path'])} for diff in scope['diffs']],
               'reason': '；'.join(part for part in (reason, scope.get('reason')) if part)}
    # Match the full canonical input, not the necessarily abbreviated UI preview.
    fingerprint = action_key(call)
    version = owner_version(owner)
    ident = 'fusion-tool-' + ledger.digest([key, name, version, fingerprint])
    return {'id': ident, 'status': 'pending', 'actionKey': fingerprint, 'version': version,
            'call': call.call_id, 'display': display, 'scope': ledger.digest(scope)}


def permission_card(row):
    from desktop.core.harness.store import diff_from_json

    display = dict(row['display'])
    display['diffs'] = tuple(diff for raw in display.pop('diffs', ()) if (diff := diff_from_json(raw)) is not None)
    return PermissionPrompt(row['id'], (), (ActionView(**display),), (APPROVAL_OPTIONS[0], APPROVAL_OPTIONS[2]))


def validate_owner(key, name, owner):
    permissions = owner.get('permissions', {})
    if not isinstance(permissions, dict) or len(permissions) > MAX_REQUESTS:
        raise ValueError('Invalid Fusion permission ledger.')
    for ident, row in permissions.items():
        expected = 'fusion-tool-' + ledger.digest([key, name, row['version'], row['actionKey']])
        if (ident != expected or row['id'] != ident or row['status'] not in ('pending', 'approved', 'consumed', 'declined')
                or not isinstance(row['scope'], str)):
            raise ValueError('Invalid Fusion tool permission.')
        permission_card(row)
    if wait := owner.get('externalWait'):
        if (wait['status'] not in ('waiting', 'ready', 'released') or type(wait['retries']) is not int
                or not 1 <= wait['retries'] <= MAX_RETRIES or type(wait['until']) not in (float, int)
                or not math.isfinite(wait['until'])):
            raise ValueError('Invalid Fusion retry checkpoint.')


def pending_permissions(owner):
    return (row for row in owner.get('permissions', {}).values()
            if row['status'] == 'pending' and row['version'] == owner_version(owner))


def wait_record(owner, delay, *, now=None):
    """Only a trusted backend delay can create a timed wait; no model-specified polling."""
    if type(delay) not in (int, float) or not math.isfinite(delay) or not 0 < delay <= MAX_BACKOFF:
        return None
    old = owner.get('externalWait') or {}
    version = owner_version(owner)
    retries = old.get('retries', 0) if old.get('version') == version else 0
    if retries >= MAX_RETRIES:
        return None
    return {'version': version, 'until': (time.time() if now is None else now) + delay,
            'retries': retries + 1, 'status': 'waiting'}


def waiting(owner):
    wait = owner.get('externalWait') or {}
    return wait.get('status') == 'waiting' and wait.get('version') == owner_version(owner)


def incomplete_evidence(journal):
    return journal.get('savePending') or any(row['status'] == 'started' or row.get('outputPending') or row.get('truncated')
                                             for row in journal.get('calls', {}).values())


def progress_stamp(plan):
    """Ignore mere retries and model prose; only material cursor/retained evidence changes permit another pass."""
    facts = []
    for name, case in plan['cases'].items():
        seen = sorted({fid for row in case.get('history', ()) for fid in row.get('progressIds', ())})
        facts.append((name, case['next'], case['revision'], sorted(case['receipts']), seen,
                      [row['review'] for row in case.get('decisions', ()) if row['action'] == 'continue'],
                      [row['to']['model'] for row in case.get('rebindings', ())]))
    return ledger.digest(facts)


def runnable(plan):
    """Only evaluate unfinished branches; a waiting dependency suspends its consumers, not its siblings."""
    cases = plan['cases']
    suspended = {name for name, case in cases.items() if ledger.awaiting_user(case) or waiting(case)}
    while True:
        more = {name for name, unit in plan['spec']['units'].items() if set(unit['deps']) & suspended}
        if more <= suspended:
            break
        suspended.update(more)
    return [name for name, case in cases.items() if case['next'] != 'done' and name not in suspended]
