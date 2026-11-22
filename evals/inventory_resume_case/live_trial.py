"""Foreground entry for ONE live trial. The coordinator never opens a conversation inside test_runs.py.

Each invocation owns a new sandbox, one product subprocess and one evidence folder. The budget database is
explicit and persistent across both versions and invocations. Raw conversation files never leave scratch.
"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import zipfile

from .plan_meter import BudgetLedger, LIMIT, RESERVE

REPO = Path(__file__).resolve().parents[2]


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    for name in ('revision', 'account', 'ledger', 'model', 'reasoning', 'label'):
        p.add_argument('--' + name, required=True)
    p.add_argument('--mode', choices=('pre_commit', 'pre_receipt'), required=True)
    a = p.parse_args()
    if os.getenv('WITTYCODE_TEST_WORK'):
        p.error('Start this self-managed trial directly, never under another run.')
    if a.label not in ('A', 'B') or len(a.revision) != 40 or any(c not in '0123456789abcdef' for c in a.revision):
        p.error('An A/B label and full frozen commit are required.')
    budget = Path(a.ledger).resolve(); acct = Path(a.account).resolve()
    approved = (REPO / 'outputs' / 'resume-ab-v1').resolve()
    if budget.parent != approved or not (acct / 'state.json').is_file():
        p.error('Need a persistent suite ledger and an explicit signed-in account pointer.')
    ledger = BudgetLedger(budget)
    if ledger.totals()['pending'] or ledger.totals()['spent'] + RESERVE > LIMIT:
        p.error('Unsettled usage or insufficient remaining shared allowance; not starting a trial.')
    last_id = max((r['id'] for r in ledger.rows()), default=0)
    output = REPO / 'outputs' / 'test-runs'; output.mkdir(parents=True, exist_ok=True)
    evidence = Path(tempfile.mkdtemp(prefix='inventory-plan-', dir=output))
    dst = evidence / 'trial.json'
    report = {'version': a.label, 'commit': a.revision, 'mode': a.mode, 'model': a.model, 'reasoning': a.reasoning,
              'status': 'not_started', 'input': 'native reopen then continue', 'precrash_proposal': 'fixed',
              'resume_transport': 'Plan', 'approval_policy': 'identical simulated approvals; restricted inventory tool',
              'cleanup': 'pending'}
    summary_keys = ('status', 'end', 'passed', 'stopped', 'seconds', 'post_reopen_calls', 'prompts', 'journal', 'oracle')
    temp = tempfile.TemporaryDirectory(prefix='inventory-trial-', dir=REPO / 'sandbox')
    try:
        work = Path(temp.name); src = work / 'src'; arc = work / 'frozen.zip'; side = work / 'side.json'
        subprocess.run(['git', 'archive', '--format=zip', '-o', str(arc), a.revision, 'desktop', 'tests'], cwd=REPO, check=True)
        with zipfile.ZipFile(arc) as z:
            z.extractall(src)
        home = work / 'home'; home.mkdir(); scratch = work / 'tmp'; scratch.mkdir()
        env = {**os.environ, **dict.fromkeys(('TEMP','TMP','TMPDIR'), str(scratch)),
               **dict.fromkeys(('HOME','USERPROFILE','APPDATA','LOCALAPPDATA'), str(home)),
               'WITTYCODE_TEST_WORK': str(work), 'WITTYCODE_TEST_EVIDENCE': str(evidence),
               'WITTYCODE_SETTINGS': str(home / 'settings.json'), 'CODEX_HOME': str(home / '.codex'),
               'PYTHONDONTWRITEBYTECODE': '1', 'PYTHONIOENCODING': 'utf-8'}
        # No API credentials, inherited plugins or injected prompts are needed by this business-only product.
        for key in list(env):
            if ('API_KEY' in key.upper() or key.upper() in {'PYTHONPATH','WITTYCODE_CHATGPT_SHAPE','LEDGER_DIE_AT'}):
                env.pop(key)
        cmd = [sys.executable, '-B', '-m', 'evals.inventory_resume_case.live_side', '--source', str(src),
               '--work', str(work / 'run'), '--account', str(acct), '--budget-file', str(budget),
               '--mode', a.mode, '--model', a.model, '--reasoning', a.reasoning, '--out', str(side),
               '--side', a.label, '--seconds', '240']
        cp = subprocess.run(cmd, cwd=REPO, env=env, stdin=subprocess.DEVNULL, capture_output=True, timeout=335)
        if side.exists():
            result = json.loads(side.read_text(encoding='utf8'))
            report.update({key: result[key] for key in summary_keys if key in result})
            trace = [{k:v for k,v in e.items() if k in ('t','event','phase','decision','turn_status','pending_candidates','stopped','seconds','killed_desktop_process')} for e in result.get('trace', [])]
            report['trace'] = trace
            begin = next((e['t'] for e in trace if e['event']=='user_message' and e.get('phase')=='resumed'), None)
            end = next((e['t'] for e in trace if e['event']=='oracle'), None)
            report['resume_to_oracle_s'] = round(end-begin, 3) if begin is not None and end is not None else None
            if result.get('note'):
                report['error'] = str(result['note']).replace(str(work), '<scratch>').replace(str(acct), '<account>')[:350]
        else:
            report['status'] = 'cannot_run'; report['error'] = 'product child exited without side result'
        report['exit_code'] = cp.returncode
    except Exception as exc:
        report.update(status='cannot_run', error=type(exc).__name__)
    finally:
        try:
            temp.cleanup(); report['cleanup'] = 'done'
        except OSError as exc:
            report['cleanup'] = 'blocked: '+type(exc).__name__; print('Cleanup blocked:',temp.name, file=sys.stderr)
        rows = [r for r in ledger.rows() if r['id'] > last_id]
        report['provider_posts'] = [{k:v for k,v in r.items() if k in ('id','status','used','input_tokens','output_tokens','cached_tokens','reasoning_tokens','seconds','outcome','error')} for r in rows]
        complete = bool(rows) and all(r['status']=='settled' for r in rows)
        report['tokens'] = sum(r['used'] for r in rows) if complete else None
        if not complete:
            report['passed'] = None; report['status'] = 'unmetered' if rows else 'not_run'
        if report['status']=='scored' and not any(e.get('event')=='reopen' and e.get('turn_status')=='interrupted' for e in report.get('trace',[])):
            report.update(status='invalid_checkpoint', passed=None)
        report['budget_after'] = ledger.totals()
        dst.write_text(json.dumps(report, ensure_ascii=False, indent=2)+'\n', encoding='utf8')
    print(dst.relative_to(REPO)); print(json.dumps({k:report.get(k) for k in ('status','passed','tokens','resume_to_oracle_s','end','error','cleanup','budget_after')}))
    return 0 if report['status']=='scored' and report['cleanup']=='done' else 2


if __name__=='__main__':
    raise SystemExit(main())
