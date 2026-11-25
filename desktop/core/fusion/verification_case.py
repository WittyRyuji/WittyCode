"""Frozen unit snapshots and their separate, checkpointed execution cursors.

Only the tool-facing facts are model input. The program owns the snapshot, roster, revision,
next stage and receipts. A restored in-flight stage pauses; it never replays itself.
"""
from __future__ import annotations

import hashlib
import json
import math
import time
import uuid
from dataclasses import asdict

from desktop.core.fusion import model_selection, step_budget
from desktop.core.fusion.expert_catalog import ExpertCatalogError

STAGES = ('worker', 'runner', 'review')


def _identity_keys(stable_id, name):
    return {stable_id.casefold(), name.casefold()}


def _different_experts(left, right):
    return _identity_keys(left.stable_id, left.name).isdisjoint(_identity_keys(right.stable_id, right.name))


def independent(roster):
    author, reviewer = roster['worker'], roster.get('review')
    return not reviewer or (author['actual_model'].casefold() != reviewer['actual_model'].casefold()
                            and _identity_keys(author['expert'], author['identity']).isdisjoint(
                                _identity_keys(reviewer['expert'], reviewer['identity'])))


def resolve(units, requested, *, policy, settings, pools, catalog, goal, canonical, effort, excluded=()):
    snapshot = catalog.snapshot()
    resolved = {}
    for item in units:
        preferred = snapshot.get(item['expert']) if item['expert'] else None
        tags = {tag.casefold() for tag in item['tags']}
        candidates = [entry for entry in snapshot.entries if entry.enabled and entry.work_type == item['work_type']
                      and tags <= {tag.casefold() for tag in entry.tags}
                      and (not item['expert'] or entry == preferred)]
        paths = list(dict.fromkeys(canonical(path) for path in item['paths']))
        needed = {'read', 'edit'} if paths else {'read'}
        def usable(entry, tools):
            try:
                return entry.enabled and tools <= catalog.resolve_version(entry.stable_id, entry.version).identity.tools
            except (ExpertCatalogError, ValueError, OSError):
                return False  # A bad body is not a reason to discard another feasible expert.

        result = model_selection.resolve_selection(policy.preference, item['J'], item['I'], item['C'], pools,
                                                   **{k: item[k] for k in ('risk', 'review_required', 'force_lead')})
        if not result['ok']:
            raise ValueError(f"{item['id']}: {result['reasons'][-1]}.")
        # Model feasibility (including native-id independence) is already solved jointly. Bind actual
        # versioned experts as a second joint choice: work type alone is not an author's identity.
        runner = reviewer = author = None
        checks = [entry for entry in snapshot.entries if entry.work_type == 'verify' and usable(entry, {'read', 'exec'})]
        reviews = [entry for entry in snapshot.entries if result['review']['required'] and entry.work_type == 'review'
                   and usable(entry, {'read'})]
        for candidate in candidates:
            if usable(candidate, needed):
                runner = next((entry for entry in checks if _different_experts(candidate, entry)), None)
                reviewer = next((entry for entry in reviews if _different_experts(candidate, entry)), None)
                if runner and (reviewer or not result['review']['required']):
                    author = candidate
                    break
        if author is None:
            raise ValueError(f"{item['id']}: no feasible author, Runner and independent review expert match the scope and tags.")
        roster = {}
        for stage, entry in [('worker', author), ('runner', runner), ('review', reviewer)]:
            choice = result[stage]
            if stage == 'review' and not choice['required']:
                continue
            if entry is None or not entry.enabled:
                raise ValueError(f"{item['id']}: no enabled {stage} expert.")
            roster[stage] = {**choice, 'identity': entry.name, 'expert': entry.stable_id, 'version': entry.version,
                             'hash': entry.content_hash, 'role': entry.role, 'effort': effort(choice['model'], choice['seat'])}
        if not independent(roster):
            raise ValueError(f"{item['id']}: author and reviewer must differ in both identity and native model.")
        size = step_budget.task_size(paths=len(paths), acceptance=item['acceptance'], text=item['task'])
        band = step_budget.resolve_curve04(step_budget.Side(settings.fusion_steps_min_adaptive, settings.fusion_steps_min),
                                           step_budget.Side(settings.fusion_steps_max_adaptive, settings.fusion_steps_max),
                                           size, settings.max_steps, difficulty=float(result['derived']['D']), plan=requested['turns'])
        seconds = step_budget.adaptive_seconds(band.high) if policy.max_seconds_adaptive else policy.max_seconds
        if type(requested['seconds']) is int:
            seconds = min(seconds, requested['seconds'])
        resolved[item['id']] = {**item, 'paths': paths, 'roster': roster, 'facts': result['facts'], 'derived': result['derived'],
                                'author': {'expert': author.stable_id, 'identity': author.name, 'actual_model': roster['worker']['actual_model']},
                                'reasons': result['reasons'], 'budget': {**asdict(band), 'seconds': seconds},
                                'resolver': result['version']}
    spec = {'format': 1, 'nonce': uuid.uuid4().hex, 'catalog': snapshot.revision, 'goal': goal,
            'budget': requested, 'units': resolved, 'excluded': list(excluded)}
    return digest(spec), {'spec': spec, 'cases': {key: new_case() for key in resolved}, 'activated': False}


def page(data):
    return json.dumps(data, sort_keys=True, ensure_ascii=False, separators=(',', ':'))


def digest(spec):
    return hashlib.sha256(page(spec).encode()).hexdigest()


def stage_proof(key, unit, revision, stage, expert, status, summary, evidence):
    # A report's attestations, never its draft, reasoning, notes, tool previews or arbitrary linked outputs.
    return {'plan': key, 'unit': unit, 'revision': revision, 'stage': stage, 'expert': expert,
            'status': status, 'summary': summary, 'evidence': list(evidence)}


def progress_proof(key, name, revision, stage, expert, attempt, progress):
    return {'plan': key, 'unit': name, 'revision': revision, 'stage': stage, 'expert': expert, 'attempt': attempt,
            **{k: progress[k] for k in ('path', 'tool', 'observed', 'id')}}


def packet_records(key, plan, name, stage, recipient, project):
    """Grant only selected source pages. Never treat an o-… string in a report/handoff as a capability."""
    unit, case = plan['spec']['units'][name], plan['cases'][name]
    version = case['revision']

    def result(source, revision, role, receipt, status='done'):
        if receipt['revision'] != revision or not receipt['evidence']:
            raise ValueError('Stale or empty upstream evidence.')
        proof = stage_proof(key, source, revision, role, receipt['expert'], status, receipt['summary'], receipt['evidence'])
        return {'stage': role, 'revision': revision, 'detail': project(proof, receipt.get('detail'))}

    upstream, prior = {}, {}
    for dep in unit['deps']:
        source = plan['cases'][dep]
        if source['next'] != 'done':
            raise ValueError('Unverified dependency.')
        role = 'review' if 'review' in plan['spec']['units'][dep]['roster'] else 'runner'
        upstream[dep] = result(dep, source['revision'], role, source['receipts'][role])
    if stage == 'runner':
        upstream['author result'] = result(name, version, 'worker', case['receipts']['worker'])
    elif stage == 'review':
        upstream['runner evidence'] = result(name, version, 'runner', case['receipts']['runner'])

    h = case.get('handoff')
    if not case['paused']:
        return upstream, prior
    if not h:
        raise ValueError('Missing frozen handoff.')
    # Only a repair crosses revisions: the original author gets the checker's finding, not their run.
    repair = stage == 'worker' and case['paused'] == 'repair'
    if h['unit'] != name or (h['revision'], h['nextRevision']) != (version - int(repair), version):
        raise ValueError('Handoff is not bound to this unit revision.')
    if repair:
        f = case['finding']
        if h['diagnostic'] != 'U03' or (h['stage'], h['expert']) != (f['stage'], f['expert']):
            raise ValueError('Handoff is not bound to this finding.')
    elif h['stage'] != stage or h['expert'] != recipient:
        raise ValueError('Handoff is not bound to this stage owner.')
    if stage == 'worker' and version > 1:
        # Retrying an interrupted repair still needs the original finding, even if its first pack failed.
        f = case['finding']
        if f['stage'] not in ('runner', 'review'):
            raise ValueError('Repair requires a checker finding.')
        prior['finding'] = result(name, version - 1, f['stage'], f, 'concerns')
    # evidenceRefs in Main's diagnostic include raw outcomes and other stages. Do not forward that address book.
    prior['handoff'] = {k: h[k] for k in ('unit', 'stage', 'revision', 'diagnostic', 'completed', 'remaining',
                                         'inFlight', 'recommendation', 'confirmation', 'nextOwner', 'nextRevision')}
    if not repair and h.get('progress'):
        prior['progress'] = [project(progress_proof(key, name, version, stage, recipient, case['attempts'][stage] - 1, p),
                                     p['result']) for p in h['progress']]
    return upstream, prior


def new_case():
    return {'next': 'worker', 'revision': 0, 'paused': '', 'receipts': {}, 'repairs': 0, 'spent': {}, 'attempts': {}}


def handoff(case, unit, stage, *, cause='', expert='', progress=(), detail='', automatic=False, binding_failed=False):
    """Summarize the actual cursor, not an expert's proposed diagnosis. No new model request."""
    next_stage, paused = case['next'], case['paused']
    cause = cause or paused
    if binding_failed:
        code, kind, action = 'U07', 'binding', 'Request an explicitly revised plan; no alternate is frozen.'
    elif cause == 'cancelled':
        code, kind, action = 'U06', 'cancelled', 'Leave stopped work alone until Main inspects it.'
    elif cause == 'repair' or paused == 'repair':
        code, kind, action = 'U03', 'artifact_defect', 'Return the finding to the original author; recheck the new revision.'
    elif cause == 'missing evidence':
        code, kind, action = 'U01', 'missing_evidence', 'Supplement this stage only; do not redo the deliverable.'
    elif paused in ('hard time limit', 'hard limit', 'repair limit') or cause == 'time limit':
        code, kind, action = 'U05', 'hard_limit', 'Do not retry this dispatch.'
    elif cause.startswith('soft limit'):
        code, kind, action = 'U04', 'soft_budget', 'Inspect the retained progress before approving more steps.'
    elif cause.startswith('Observer:'):
        code, kind, action = 'U08', 'observer', 'Inspect the recorded finding before deciding whether to resume.'
    else:
        code, kind, action = 'U02', 'environment_or_context', 'Inspect the interruption or restore the prerequisite; do not repair the artifact.'
    terminal = next_stage == 'stopped'
    remaining = [s for s in STAGES if s in unit['roster'] and s not in case['receipts']]
    if kind == 'artifact_defect':
        remaining = [s for s in STAGES if s in unit['roster']]  # None of the old receipts certify a repair.
    refs = [r['detail'] for r in case['receipts'].values() if r.get('detail')]
    refs.extend(p['result'] for p in progress if p.get('result'))
    if detail:
        refs.append(detail)
    return {'unit': unit['id'], 'revision': case['revision'], 'stage': stage, 'expert': expert,
            'diagnostic': code, 'category': kind, 'reason': paused, 'cause': cause,
            'completed': list(case['receipts']), 'evidenceRefs': list(dict.fromkeys(refs)), 'progress': list(progress),
            'inFlight': stage if cause in ('cancelled', 'interrupted', 'incomplete run', 'timeout', 'time limit',
                                           'process interrupted') or kind == 'observer' else None,
            'remaining': remaining, 'stepsLeft': max(0, unit['budget']['high'] - case['spent'].get(stage, 0)),
            'recommendation': 'Stop; request an explicit new plan revision to change this binding or budget.' if terminal else action,
            'nextOwner': 'Main' if terminal else unit['roster'][next_stage]['expert'],
            'nextExpert': None if terminal else expert if next_stage == stage else case['receipts'].get(next_stage, {}).get('expert'),
            'nextRevision': case['revision'] + int(not terminal and kind == 'artifact_defect'),
            'confirmation': ('new_plan_required' if terminal else 'one_evidence_supplement' if automatic else 'explicit_resume')}


def restore(key, plan):
    spec = plan['spec']
    if (spec['format'] != 1 or key != digest(spec) or set(plan['cases']) != set(spec['units'])
            or type(plan['activated']) is not bool):
        raise ValueError('Invalid frozen plan snapshot.')
    for name, case in plan['cases'].items():
        unit = spec['units'][name]
        if not independent(unit['roster']):
            raise ValueError('The stored review is not independent of its author.')
        if (case['next'] not in (*STAGES, 'done', 'stopped') or type(case['revision']) is not int or case['revision'] < 0
                or not isinstance(case['receipts'], dict) or not isinstance(case['paused'], str)
                or any(type(n) is not int or n < 0 for counts in (case['spent'], case['attempts']) for n in counts.values())):
            raise ValueError('Invalid frozen unit cursor.')
        stages = [s for s in STAGES if s in unit['roster']]
        if (set(case['receipts']) - set(stages) or not isinstance(case.get('history', []), list)
                or any(not isinstance(h, dict) or h.get('stage') not in stages
                       or type(h.get('seconds')) not in (int, float) or not math.isfinite(h['seconds']) or h['seconds'] < 0
                       for h in case.get('history', []))):
            raise ValueError('Invalid stage history.')
        for stage, proof in case['receipts'].items():
            if (not isinstance(proof, dict) or proof.get('revision') != case['revision'] or not isinstance(proof.get('expert'), str)
                    or not isinstance(proof.get('evidence'), list) or not proof['evidence']
                    or not all(isinstance(e, str) and e.strip() for e in proof['evidence'])):
                raise ValueError('Invalid frozen stage evidence.')
        if case['next'] in ('done', 'runner', 'review'):
            needed = stages if case['next'] == 'done' else stages[:stages.index(case['next'])]
            if any(s not in case['receipts'] or case['receipts'][s]['revision'] != case['revision']
                   or not case['receipts'][s]['evidence'] for s in needed):
                raise ValueError('A frozen stage has no verified receipt for this revision.')
        if 'reserved' in case:
            # The process may have sent work before dying. Charge the reservation, never replay it.
            reserved = case.pop('reserved')
            stage = case['next']
            if stage not in stages or type(reserved) is not int or not 0 < reserved <= unit['budget']['high']:
                raise ValueError('Invalid frozen stage reservation.')
            case['spent'][stage] = case['spent'].get(stage, 0) + reserved
            case['paused'] = 'interrupted; inspect before resuming'
            seconds = case.pop('reserved_seconds', 0)
            if type(seconds) is not int or not 0 <= seconds <= unit['budget']['seconds']:
                raise ValueError('Invalid frozen time reservation.')
            elapsed = time.time() - case.pop('reserved_at', 0)
            if math.isfinite(elapsed) and elapsed >= 0:
                seconds = min(seconds, math.ceil(elapsed))
            case.setdefault('history', []).append({'revision': case['revision'], 'stage': stage, 'reason': case['paused'],
                                                   'seconds': seconds, 'steps': reserved, 'tokens': None})
            if (case['spent'][stage] >= unit['budget']['high'] or case['attempts'][stage] >= 3
                    or sum(h['seconds'] for h in case['history'] if h['stage'] == stage) >= unit['budget']['seconds']):
                case['next'], case['paused'] = 'stopped', 'hard limit'
            case['handoff'] = handoff(case, unit, stage, cause='process interrupted', expert=case.pop('reserved_expert', ''))
            case['history'][-1]['handoff'] = case['handoff']
    return plan


def advance(case, unit, stage, report, *, accepted, interrupted, reason, steps, expert):
    """Only the report for this unit/revision/stage advances it. Missing proof is not a repair."""
    case['spent'][stage] = case['spent'].get(stage, 0) + max(1, steps)
    if not interrupted and accepted and report is not None and report.status in ('blocked', 'needs_context'):
        case['paused'] = report.status
    elif interrupted or not accepted or report is None or not report.evidence:
        case['paused'] = reason or ('interrupted' if interrupted else 'missing evidence')
    elif report.status == 'done':
        case['receipts'][stage] = {'revision': case['revision'], 'expert': expert, 'summary': report.summary,
                                   'evidence': list(report.evidence)}
        case['next'] = 'runner' if stage == 'worker' else 'review' if stage == 'runner' and 'review' in unit['roster'] else 'done'
        case['paused'] = ''
    elif report.status == 'concerns' and stage != 'worker':
        case['repairs'] += 1
        case['finding'] = {'revision': case['revision'], 'stage': stage, 'expert': expert,
                           'summary': report.summary, 'evidence': list(report.evidence)}
        case['next'], case['paused'] = 'worker', 'repair'  # Always the unit's original author and write scope.
        if case['repairs'] > 2:
            case['next'], case['paused'] = 'stopped', 'repair limit'
    else:
        case['paused'] = report.status
    cause = case['paused']
    if case['next'] == stage and (case['spent'][stage] >= unit['budget']['high'] or case['attempts'][stage] >= 3):
        case['next'], case['paused'] = 'stopped', 'hard limit'
    return cause  # An exhausted cap changes the disposition, not what interrupted this attempt.


def receipt(key, plan):
    lines = [f'Work plan validated: {key}.']
    units = plan['spec']['units']
    for name, unit in list(units.items())[:3]:
        roles = ' '.join(f"{stage}={r['expert']}@{r['version']}/{r['seat']}:{r['model']}" for stage, r in unit['roster'].items())
        b = unit['budget']
        lines.append(f"{name}: {roles}; steps {b['steps']}..{b['high']}, {b['seconds']}s.")
    if len(units) > 3:
        lines.append(f'{len(units) - 3} more units in retained details.')
    return '\n'.join(lines)
