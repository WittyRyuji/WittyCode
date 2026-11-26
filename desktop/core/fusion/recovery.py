"""Durable recovery records for a frozen plan. None of these records execute an action."""
from __future__ import annotations

import json
import math
import re
import time
from dataclasses import asdict, replace

from desktop.core.fusion import verification_case as ledger
from desktop.core.prompts import APPROVAL_OPTIONS, ActionView
from desktop.core.transcript import PermissionPrompt

MAX_REQUESTS = 32
MAX_BACKOFF = 15 * 60
MAX_RETRIES = 2


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
