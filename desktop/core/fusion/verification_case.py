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
MAX_ATTEMPTS = 3
MAX_REPAIRS = 2
STAGE_LABELS = {'worker': '作者交付', 'runner': 'Runner 检查', 'review': 'QS 独立复核'}
DECISION_LABELS = {'continue': '继续（已批准新一轮额度）', 'pause': '暂停', 'stop': '停止'}


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
                                'reasons': result['reasons'], 'budget': {**asdict(band), 'seconds': seconds,
                                    'adaptive_steps': settings.fusion_steps_max_adaptive,
                                    'adaptive_seconds': policy.max_seconds_adaptive,
                                    'hard_steps': min(settings.max_steps, requested['turns'])
                                                  if type(requested['turns']) is int else settings.max_steps,
                                    'hard_seconds': min(step_budget.MAX_SECONDS, requested['seconds'])
                                                    if type(requested['seconds']) is int else step_budget.MAX_SECONDS},
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


def progress_id(path, data):
    return digest([path, hashlib.sha256(data).hexdigest()])


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
    if not repair:
        progress = {p['id']: (case['attempts'][stage] - 1, p) for p in h.get('progress', ())}
        decisions = case.get('decisions', ())
        if decisions and decisions[-1]['action'] == 'continue' and (review := h.get('budgetReview')):
            event = decisions[-1]
            for fact in review['facts']:
                if (event['review'], event['fact'], stage, version, recipient) == (
                        review['id'], fact['id'], fact['stage'], fact['revision'], fact['expert']) and fact['kind'] == 'progress':
                    progress[fact['id']] = (fact['attempt'], fact['progress'])
        if progress:
            prior['progress'] = [project(progress_proof(key, name, version, stage, recipient, attempt, p), p['result'])
                                 for attempt, p in progress.values()]
    return upstream, prior


def new_case():
    return {'next': 'worker', 'revision': 0, 'paused': '', 'receipts': {}, 'repairs': 0, 'spent': {}, 'attempts': {}}


def journal_facts(journal):
    return [{'kind': 'progress', 'id': p['id'], 'ref': p['result'], 'progress': p,
             **{k: journal[k] for k in ('attempt', 'revision', 'stage', 'expert')}} for p in journal.get('progress', ())]


def adaptive(unit):
    b = unit['budget']
    return b.get('adaptive_steps', False) or b.get('adaptive_seconds', False)


def usage(case, stage, start=0):
    rows = [row for row in case.get('history', [])[start:] if row['stage'] == stage]
    return {'steps': sum(row['steps'] for row in rows) if start else case['spent'].get(stage, 0),
            'seconds': sum(row['seconds'] for row in rows),
            'attempts': len(rows) if start else case['attempts'].get(stage, 0)}


DIMENSIONS = ('steps', 'seconds')
PLAN_DIMENSIONS = {'steps': 'turns', 'seconds': 'seconds'}
MAX_DIMENSION = {'steps': 2**53 - 1, 'seconds': step_budget.MAX_SECONDS}


def budget_sources(unit, requested):
    b = unit['budget']
    return {d: 'fixed' if type(requested.get(PLAN_DIMENSIONS[d])) is int or not b.get('adaptive_' + d)
            else 'adaptive' if requested.get(PLAN_DIMENSIONS[d]) == 'adaptive' else 'unknown' for d in DIMENSIONS}


def hard_limits(unit, *, case=None, stage=None, step_cap=None, seconds_cap=None):
    b = unit['budget']
    steps = b['hard_steps'] if b.get('adaptive_steps') else b['high']
    seconds = b['hard_seconds'] if b.get('adaptive_seconds') else b['seconds']
    limits = {'steps': min(steps, step_cap or steps), 'seconds': min(seconds, seconds_cap or seconds)}
    # Per-attempt safeguards are applied again at dispatch. A recorded extension is an aggregate authorization.
    if case is not None:
        limits.update(case.get('budgetTotals', {}).get(stage, {}))
    return limits


def remaining(case, unit, stage, **caps):
    b, grant = unit['budget'], case.get('grant', {})
    total, current = usage(case, stage), usage(case, stage, grant.get('start', 0))
    hard = {d: limit - total[d] for d, limit in hard_limits(unit, case=case, stage=stage, **caps).items()}
    soft = {d: (grant.get(d, b[d]) if grant.get('stage', stage) == stage else b[d]) - current[d]
            for d in DIMENSIONS if b.get('adaptive_' + d) or d in grant}
    return hard, soft, current['attempts']


def reforecast(case, unit, stage, requested, demand, *, step_cap, seconds_cap):
    """Estimate and authorization are separate. Return a quote without changing any cursor or spending anything."""
    band = unit['budget']
    sources = budget_sources(unit, requested)
    totals = hard_limits(unit, case=case, stage=stage, step_cap=step_cap, seconds_cap=seconds_cap)
    used = usage(case, stage)
    grant = {'start': len(case.get('history', ())), 'repairs': case['repairs'], 'stage': stage}
    changes = {}
    for d, initial, run_cap in [('steps', 'high', step_cap), ('seconds', 'seconds', step_budget.MAX_SECONDS)]:
        left = max(0, math.floor(totals[d] - used[d]))
        if sources[d] != 'adaptive' and d not in demand and left:
            continue  # Keep the existing authorization for a fixed dimension that was not requested.
        needed = demand.get(d, band[initial])
        if not 1 <= needed <= run_cap:
            raise ValueError(f'The new {d} allowance must be between 1 and {run_cap} for one run; split larger work.')
        required = math.ceil(used[d] + needed)
        if required > MAX_DIMENSION[d]:
            raise ValueError(f'The absolute {d} limit cannot be raised; choose a compliant next action.')
        if required > totals[d]:
            changes[d] = {'old': totals[d], 'new': required, 'needed': needed, 'left': left, 'source': sources[d]}
        grant[d] = needed
    return grant, changes


def reached_limits(case, unit, stages, *, stage=None, cause='', **caps):
    """Persist the counter that actually prevented another dispatch, not just its diagnostic code."""
    hard, round_limits = [], []
    b, grant = unit['budget'], case.get('grant', {})
    is_adaptive = adaptive(unit)

    def hit(target, dimension, used, limit, owner=None):
        target.append({'dimension': dimension, 'used': used, 'limit': limit, 'stage': owner})

    for owner in stages:
        total, current = usage(case, owner), usage(case, owner, grant.get('start', 0))
        for d, limit in hard_limits(unit, case=case, stage=owner, **caps).items():
            if limit - total[d] < 1 or (owner == stage and d == 'seconds' and cause == 'time limit' and
                                       not b.get('adaptive_seconds') and d not in grant):
                hit(hard, d, total[d], limit, owner)
            elif b.get('adaptive_' + d) or d in grant:
                allowance = grant.get(d, b[d]) if grant.get('stage', owner) == owner else b[d]
                if allowance - current[d] < 1:
                    hit(round_limits, d, current[d], allowance, owner)
                elif owner == stage and (d == 'seconds' and cause == 'time limit' or d == 'steps' and cause.startswith('soft limit')):
                    last = case['history'][-1]
                    hit(round_limits, d, last[d], last['applied_budget'][d], owner)
        if current['attempts'] >= MAX_ATTEMPTS:
            hit(round_limits if is_adaptive else hard, 'attempts', current['attempts'], MAX_ATTEMPTS, owner)
    repairs = case['repairs'] - grant.get('repairs', 0)
    if repairs > MAX_REPAIRS:
        hit(round_limits if is_adaptive else hard, 'repairs', repairs, MAX_REPAIRS)
    return hard, round_limits


def hard_limit_reason(limits):
    dimensions = {limit['dimension'] for limit in limits}
    return 'hard limit' if dimensions & {'steps', 'attempts'} else 'hard time limit' if 'seconds' in dimensions else 'repair limit'


LIMIT_LABELS = {'steps': '步数', 'seconds': '时间', 'attempts': '阶段尝试次数', 'repairs': '返修次数'}


def format_limits(limits, *, chinese=False):
    labels = LIMIT_LABELS if chinese else {}
    stages = STAGE_LABELS if chinese else {}
    parts = []
    for hit in limits:
        dimension, stage = hit['dimension'], hit['stage']
        used = f"{hit['used']:.2f}".rstrip('0').rstrip('.') if dimension == 'seconds' else str(hit['used'])
        counter = f"{labels.get(dimension, dimension)} {used}/{hit['limit']}"
        if dimension == 'seconds':
            counter += ' 秒' if chinese else 's'
        parts.append((f'{stages.get(stage, stage)} ' if stage else '') + counter)
    return ('、' if chinese else ', ').join(parts)


def finding_fact(finding):
    # A new revision, reporter, or rewritten summary does not make the same work order new.
    evidence = sorted({' '.join(line.casefold().split()) for line in finding['evidence']})
    return {'kind': 'finding', 'id': digest(evidence), 'ref': finding['detail'], 'stage': finding['stage'],
            'revision': finding['revision'], 'expert': finding['expert']}


def settle_budget(key, case, unit, stage, cause, *, step_cap=None, seconds_cap=None, estimate=()):
    """Apply limits after recording an attempt. Never revoke a valid final receipt."""
    next_stage = case['next']
    if next_stage not in STAGES:
        return
    if cause == 'cancelled':
        case.update(next='stopped', paused='cancelled')
        return
    if next_stage != stage and cause != 'repair':
        return  # A successful intermediate stage advances normally; check the next one before dispatch.
    # Preserve the other exhausted stages in the handoff; each stage is still authorized separately.
    needed = tuple(unit['roster']) if cause == 'repair' else (stage,)
    hard, round_limits = reached_limits(case, unit, needed, stage=stage, cause=cause,
                                        step_cap=step_cap, seconds_cap=seconds_cap)
    if any(hit['dimension'] not in DIMENSIONS for hit in hard):
        case.update(next='stopped', paused=hard_limit_reason(hard))
        return hard
    limits = [*hard, *round_limits]
    if limits or estimate:
        history = case.get('history', [])
        start = case.get('grant', {}).get('start', 0)
        prior = {f['id'] for row in history[:start] for f in row.get('facts', ())}
        used = {d.get('fact') for d in case.get('decisions', ())}
        if cause == 'repair':
            # Only a distinct current work order can authorize a new repair revision.
            prior.update(f['id'] for row in history[start:-1] for f in row.get('facts', ()) if f['kind'] == 'finding')
            candidates = [f for f in history[-1].get('facts', ()) if f['kind'] == 'finding'] if history else []
        else:
            candidates = [f for row in history[start:] for f in row.get('facts', ())
                          if f['kind'] == 'progress' and f['stage'] == next_stage and f['revision'] == case['revision']]
        facts = list({f['id']: f for f in candidates if f['id'] not in prior | used}.values())
        case['paused'] = case['paused'] or 'budget review'
        case['budgetReview'] = {'id': digest([key, unit['id'], len(history), next_stage, case['revision']]),
                                'stage': next_stage, 'thresholds': sorted({h['dimension'] for h in limits} | set(estimate)),
                                'facts': facts, 'limits': limits}
    return limits


def handoff(case, unit, stage, *, cause='', expert='', progress=(), detail='', automatic=False, binding_failed=False,
            limits=()):
    """Summarize the actual cursor, not an expert's proposed diagnosis. No new model request."""
    next_stage, paused = case['next'], case['paused']
    cause = cause or paused
    if binding_failed:
        code, kind, action = 'U07', 'binding', 'Request an explicitly revised plan; no alternate is frozen.'
    elif cause == 'cancelled':
        code, kind, action = 'U06', 'cancelled', 'Leave stopped work alone until Main inspects it.'
    elif paused in ('hard time limit', 'hard limit', 'repair limit'):
        code, kind, action = 'U05', 'hard_limit', 'Do not retry this dispatch.'
    elif cause == 'repair' or paused == 'repair':
        code, kind, action = 'U03', 'artifact_defect', 'Return the finding to the original author; recheck the new revision.'
    elif cause == 'missing evidence':
        code, kind, action = 'U01', 'missing_evidence', 'Supplement this stage only; do not redo the deliverable.'
    elif cause.startswith('soft limit') or cause == 'time limit':
        code, kind, action = 'U04', 'soft_budget', 'Inspect the retained progress before approving more steps.'
    elif cause.startswith('Observer:'):
        code, kind, action = 'U08', 'observer', 'Inspect the recorded finding before deciding whether to resume.'
    else:
        code, kind, action = 'U02', 'environment_or_context', 'Inspect the interruption or restore the prerequisite; do not repair the artifact.'
    terminal = next_stage == 'stopped'
    stages_left = [s for s in STAGES if s in unit['roster'] and s not in case['receipts']]
    if kind == 'artifact_defect':
        stages_left = [s for s in STAGES if s in unit['roster']]  # None of the old receipts certify a repair.
    hard, soft, _ = remaining(case, unit, stage)
    refs = [r['detail'] for r in case['receipts'].values() if r.get('detail')]
    refs.extend(p['result'] for p in progress if p.get('result'))
    if detail:
        refs.append(detail)
    finding = case.get('finding') if kind == 'artifact_defect' or cause == 'repair' else None
    if finding and finding.get('detail'):
        refs.append(finding['detail'])
    review = case.get('budgetReview')
    if review:
        refs.extend(f['ref'] for f in review['facts'])
    start = case.get('grant', {}).get('start', 0)
    history = case.get('history', [])
    last = history[-1] if history else {}
    journal = last.get('journal', {})
    tool_records = list(journal.get('calls', {}).values())
    refs.extend(row[k] for row in tool_records[-8:] for k in ('result', 'report') if row.get(k))
    pending = journal.get('savePending') or any(row['status'] == 'started' or row.get('outputPending') for row in tool_records)
    return {'unit': unit['id'], 'revision': case['revision'], 'stage': stage, 'expert': expert,
            'evidencePending': bool(pending),
            'diagnostic': code, 'category': kind, 'reason': paused, 'cause': cause,
            'completed': list(case['receipts']), 'evidenceRefs': list(dict.fromkeys(refs)), 'progress': list(progress),
            'inFlight': stage if cause in ('cancelled', 'interrupted', 'incomplete run', 'timeout', 'time limit',
                                           'process interrupted') or kind == 'observer' else None,
            'remaining': stages_left, 'stepsLeft': max(0, min(hard['steps'], soft.get('steps', math.inf))),
            'usage': {'lastAttempt': {k: last[k] for k in ('steps', 'seconds', 'tokens', 'applied_budget', 'stepsReserved') if k in last},
                      'round': {s: usage(case, s, start) for s in unit['roster']},
                      'total': {s: usage(case, s) for s in unit['roster']},
                      'roundLimits': {d: case.get('grant', {}).get(d, unit['budget'][d])
                                      for d in ('steps', 'seconds') if unit['budget'].get('adaptive_' + d)},
                      'attemptLimit': MAX_ATTEMPTS, 'repairLimit': MAX_REPAIRS,
                      'roundRepairs': case['repairs'] - case.get('grant', {}).get('repairs', 0), 'repairs': case['repairs']},
            'finding': finding, 'budgetReview': review, 'limits': list(limits),
            'recommendation': ('Stop; this frozen unit cannot run.' if terminal else
                               'Main must explicitly continue, pause or stop; continue requires a new retained fact.' if review else action),
            'nextOwner': 'Main' if terminal or review else unit['roster'][next_stage]['expert'],
            'nextExpert': None if terminal else expert if next_stage == stage else case['receipts'].get(next_stage, {}).get('expert'),
            'nextRevision': case['revision'] + int(not terminal and kind == 'artifact_defect'),
            'confirmation': ('new_plan_required' if terminal else 'budget_decision' if review else
                             'one_evidence_supplement' if automatic else 'explicit_resume')}


def restore(key, plan):
    from desktop.core.fusion import recovery

    spec = plan['spec']
    if (spec['format'] != 1 or key != digest(spec) or set(plan['cases']) != set(spec['units'])
            or type(plan['activated']) is not bool):
        raise ValueError('Invalid frozen plan snapshot.')
    recovery.validate_owner(key, 'Main', plan.get('mainRecovery', {}))
    for name, case in plan['cases'].items():
        recovery.validate_owner(key, name, case)
        unit = spec['units'][name]
        if not independent(unit['roster']):
            raise ValueError('The stored review is not independent of its author.')
        if (case['next'] not in (*STAGES, 'done', 'stopped') or type(case['revision']) is not int or case['revision'] < 0
                or not isinstance(case['receipts'], dict) or not isinstance(case['paused'], str)
                or any(type(n) is not int or n < 0 for counts in (case['spent'], case['attempts']) for n in counts.values())):
            raise ValueError('Invalid frozen unit cursor.')
        stages = [s for s in STAGES if s in unit['roster']]
        roster = dict(unit['roster'])
        seen = set()
        for row in case.get('rebindings', ()):
            stage, replacement = row['stage'], row['to']
            old = roster[stage]
            if (row['from'] != old or (stage, replacement['model']) in seen or set(old) != set(replacement)
                    or type(row['revision']) is not int or not 0 <= row['revision'] <= case['revision']
                    or any(replacement[k] != old[k] for k in old if k not in ('model', 'actual_model'))
                    or not all(isinstance(replacement[k], str) and replacement[k] for k in ('model', 'actual_model'))):
                raise ValueError('Invalid stage replan.')
            seen.update(((stage, old['model']), (stage, replacement['model'])))
            roster[stage] = replacement
            if not independent(roster):
                raise ValueError('A replanned check cannot reuse the author model.')
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
        history = case.get('history', [])
        decisions = case.get('decisions', [])
        if (not isinstance(decisions, list) or any(not isinstance(d, dict) or d.get('action') not in DECISION_LABELS
                or not isinstance(d.get('review'), str) or d.get('stage') not in stages
                or type(d.get('revision')) is not int or not 0 <= d['revision'] <= case['revision'] for d in decisions)):
            raise ValueError('Invalid frozen budget decisions.')
        grants = [d for d in decisions if d['action'] == 'continue']
        totals = {}
        for event in grants:
            for d, quote in event.get('changes', {}).items():
                if (d not in DIMENSIONS or type(quote.get('new')) is not int or not 1 <= quote['new'] <= MAX_DIMENSION[d]
                        or quote.get('source') not in ('adaptive', 'fixed')
                        or quote['source'] == 'fixed' and not event.get('userApproval')):
                    raise ValueError('Invalid frozen budget extension.')
                totals.setdefault(event['stage'], {})[d] = quote['new']
            if 'changes' in event:
                totals.setdefault(event['stage'], {})
        if case.get('budgetTotals', {}) != totals:
            raise ValueError('Budget totals have no matching authorization history.')
        if grant := case.get('grant'):
            allowed = MAX_DIMENSION if 'stage' in grant else {'steps': unit['budget']['high'], 'seconds': unit['budget']['seconds']}
            if (not grants or grant != grants[-1].get('budget') or type(grant.get('start')) is not int
                    or not 0 <= grant['start'] <= len(history) or type(grant.get('repairs')) is not int
                    or grant.get('stage', grants[-1]['stage']) != grants[-1]['stage']
                    or not 0 <= grant['repairs'] <= case['repairs']
                    or any(type(grant.get(d)) is not int or not 1 <= grant[d] <= allowed[d]
                           for d in allowed if d in grant or unit['budget'].get('adaptive_' + d))):
                raise ValueError('Invalid frozen budget grant.')
        elif grants:
            raise ValueError('Missing frozen budget grant.')
        if review := case.get('budgetReview'):
            if ('reserved' in case or not case['paused'] or case['next'] not in stages
                    or review.get('stage') != case['next']
                    or review.get('id') != digest([key, name, len(history), case['next'], case['revision']])
                    or not isinstance(review.get('facts'), list)
                    or not review.get('thresholds') or set(review['thresholds']) - {'steps', 'seconds', 'attempts', 'repairs'}):
                raise ValueError('Invalid pending frozen budget review.')
        if request := case.get('budgetRequest'):
            event = request.get('event') or {}
            if (request['id'] != 'fusion-budget-' + digest([key, name, event]) or
                    request.get('status') == 'pending' and (not review or event.get('review') != review['id'] or
                    event.get('stage') != case['next'] or event.get('revision') != case['revision'])):
                raise ValueError('Invalid frozen budget question.')
        if 'reserved' in case:
            # The process may have sent work before dying. Charge the reservation, never replay it.
            reserved = case.pop('reserved')
            stage = case['next']
            grant = case.get('grant', {})
            if grant.get('stage', stage) != stage:
                grant = {}
            if stage not in stages or type(reserved) is not int or not 0 < reserved <= max(unit['budget']['high'], grant.get('steps', 0)):
                raise ValueError('Invalid frozen stage reservation.')
            case['spent'][stage] = case['spent'].get(stage, 0) + reserved
            case['paused'] = 'interrupted; inspect before resuming'
            seconds = case.pop('reserved_seconds', 0)
            if type(seconds) is not int or not 0 <= seconds <= max(unit['budget']['seconds'], grant.get('seconds', 0)):
                raise ValueError('Invalid frozen time reservation.')
            elapsed = time.time() - case.pop('reserved_at', 0)
            if math.isfinite(elapsed) and elapsed >= 0:
                seconds = min(seconds, math.ceil(elapsed))
            owner = case.pop('reserved_expert', '')
            journal = case.pop('journal', {})
            if journal and (journal.get('expert') != owner or journal.get('revision') != case['revision'] or
                            journal.get('stage') != stage or journal.get('attempt') != case['attempts'].get(stage)):
                raise ValueError('The saved tool checkpoints belong to a different attempt.')
            facts = journal_facts(journal)
            case.setdefault('history', []).append({'revision': case['revision'], 'stage': stage, 'reason': case['paused'],
                                                   'seconds': seconds, 'steps': reserved, 'stepsReserved': True, 'tokens': None,
                                                   'journal': journal, 'facts': facts, 'progressIds': [f['id'] for f in facts]})
            limits = settle_budget(key, case, unit, stage, 'process interrupted')
            case['handoff'] = handoff(case, unit, stage, cause='process interrupted', limits=limits or (),
                                      progress=journal.get('progress', ()), expert=owner)
            case['history'][-1]['handoff'] = case['handoff']
    return plan


def advance(case, unit, stage, report, *, accepted, interrupted, reason, steps, expert):
    """Only the report for this unit/revision/stage advances it. Missing proof is not a repair."""
    case['spent'][stage] = case['spent'].get(stage, 0) + steps
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
    else:
        case['paused'] = report.status
    return case['paused']  # A budget disposition must not overwrite the original diagnosis.


def awaiting_user(case):
    from desktop.core.fusion.recovery import pending_permissions

    return (any((case.get(field) or {}).get('status') == 'pending' for field in ('budgetRequest', 'recoveryRequest'))
            or any(pending_permissions(case)))


def result_lines(plan):
    """Stable user-facing result and only the actionable next states for Main."""
    lines, actions = [], []
    cases = plan['cases']
    reasons = {'U01': '缺少验收证据', 'U02': '环境或上下文中断', 'U03': '检查发现交付缺陷',
               'U04': '本轮额度用尽', 'U05': '达到硬上限', 'U06': '已取消或权限被拒',
               'U07': '冻结绑定失效', 'U08': 'Observer 暂停'}
    for name, case in cases.items():
        stage = case['next']
        deps = [dep for dep in plan['spec']['units'][name]['deps'] if cases[dep]['next'] != 'done']
        decision = (case.get('decisions') or [{}])[-1].get('action')
        saved_pause = False
        h = case.get('handoff', {})
        reason = reasons.get(h.get('diagnostic'), '待检查')
        label = STAGE_LABELS.get(stage, '')
        if stage == 'done':
            status = '已验证'
        elif stage == 'stopped':
            if case['paused'] in ('hard limit', 'hard time limit', 'repair limit'):
                reason = '达到硬上限' + ('；有检查工单' if h.get('finding') else '')
            status = '已停止' if decision == 'stop' else f'已停止（{reason}）'
            if h.get('diagnostic') == 'U07':
                actions.append(f'{name}: use replan=true to inspect an authorized same-seat fallback')
        elif deps:
            status = '依赖阻塞，等待 ' + '、'.join(deps) + ' 验证通过'
        elif awaiting_user(case):
            status = f'等待用户回答续作问题；下一阶段：{label}'
        elif review := case.get('budgetReview'):
            saved_pause = decision == 'pause' and case['decisions'][-1]['review'] == review['id']
            limit = '、'.join(LIMIT_LABELS[d] for d in review['thresholds'])
            status = f'本轮额度用尽（{limit}），' + ('Main 已决定暂停' if saved_pause else '待 Main 决策')
            status += f'；下一阶段：{label}'
            if h.get('finding'):
                status += '；已有检查工单'
            elif h.get('diagnostic') in ('U01', 'U02', 'U08'):
                status += f'；{reason}'
            if not review['facts']:
                status += '；无新的可核查证据，不能续额' if adaptive(plan['spec']['units'][name]) else '；需诊断或复核固定额度'
            if not saved_pause and review['facts']:
                actions.append(f'{name}: estimate remaining work and inspect budget review {review["id"]}')
        elif case['paused']:
            status = f'已暂停（{reason}）；下一阶段：{label}'
            if decision == 'continue' and (h.get('budgetReview') or {}).get('id') == case['decisions'][-1]['review']:
                status = f'已续额，等待显式派工；下一阶段：{label}'
            elif case['paused'] == 'repair':
                actions.append(f'{name}: inspect the current finding before explicitly resuming its author')
        else:
            status = f'可派工；下一阶段：{label}'
            actions.append(f'{name}: {stage} is ready')
        if decision and not (case.get('budgetReview') and decision == 'pause' and saved_pause):
            status += f'；Main 最近决策：{DECISION_LABELS[decision]}'
        if (case['paused'] or stage == 'stopped') and (limits := h.get('limits')):
            status += f'；已用/上限：{format_limits(limits, chinese=True)}'
        if h.get('evidencePending') and stage != 'done':
            status += '；部分证据或工具结果待核对'
        lines.append(f'{name} v{case["revision"]}：{status}。')
    return tuple(lines), tuple(actions)


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
