"""Opt-in, text-only live Fusion transport matrix. With no arguments, list cells without reading accounts.

Run from the project root via desktop/core/test_runs.py -- <python> -m evals.live_fusion_matrix
--profile <application profile> --cell plan,devin,api. Repeat --cell to run additional distinct cells.
The profile is a read-only credential source, not the session store. No token refresh or Factory client.
"""
from __future__ import annotations

import argparse
import asyncio
from contextlib import aclosing
from itertools import product
import json
import logging
import os
from pathlib import Path
import time

from desktop.core.chatgpt.accounts import AccountStore
from desktop.core.chatgpt.engine import ChatGPTEngine
from desktop.core.chatgpt.http import HttpClient
from desktop.core.chatgpt.models import models_from_result
from desktop.core.devin.engine import DevinEngine
from desktop.core.fusion.gates import GATES
from desktop.core.fusion.registry import Policy
from desktop.core.fusion.runtime import native_model
from desktop.core.harness import todo
from desktop.core.harness.tools import ToolRegistry
from desktop.core.llm.backend import ApiBackend
from desktop.core.providers import ProviderStore
from desktop.core.settings import AppSettings

# deepseek-flash on the official endpoint is DeepSeek-V4.1-Flash-1017 (api-docs.deepseek.com).
TARGET = {'plan': ('gpt-6.1-terra', 'low'), 'api': ('deepseek-flash', 'low'), 'devin': ('swe-2-high', 'medium')}
CELLS = tuple(','.join(c) for c in product(TARGET, repeat=3))


class CurrentAccess:
    """An expired/rejected access token stops the trial; never rotate the desktop user's credentials."""
    def __init__(self, accounts):
        self.accounts = accounts

    async def access_token(self, client_id=None, *, rejected=None):
        record = self.accounts.get(client_id or self.accounts.active_id())
        if rejected or not record or not record.access_token or record.expires_at <= time.time() + 10:
            raise ValueError('Plan access unavailable without refresh')
        return record.access_token


class CheckedWire(HttpClient):
    def __init__(self, expected_url, model, backend):
        super().__init__()
        self.expected_url, self.model, self.backend, self.count = expected_url, model, backend, 0

    async def stream_lines(self, method, url, **kw):
        body = kw['json_body']
        effort = body.get('reasoning_effort') if self.backend == 'api' else (body.get('reasoning') or {}).get('effort')
        if (method != 'POST' or url != self.expected_url or body.get('model') != self.model or effort != 'low'
                or (body.get('thinking') or {}).get('type') == 'disabled' or self.count >= 8):
            raise ValueError('Refused unexpected destination, model, effort or request count')
        self.count += 1
        async with aclosing(super().stream_lines(method, url, **kw)) as lines:
            async for line in lines:
                yield line


async def trial(cell, profile, work, evidence):
    names = cell.split(',')
    roles = ('main', 'worker', 'light')
    row = {'cell': cell, 'status': 'failed', 'experts': {}, 'main_done': False}
    project = work / cell.replace(',', '-')
    project.mkdir()
    settings = AppSettings(max_steps=5, think_chinese=False, fusion_gates=dict.fromkeys(GATES, False))
    if 'devin' in names:
        settings.devin_path = json.loads((profile / 'settings.json').read_text(encoding='utf-8-sig')).get('devin_path', '')
    de = DevinEngine(settings)
    engine = ChatGPTEngine(settings, project / 'store')
    engine.registry = ToolRegistry()  # No filesystem, shell, remote MCP or skill tools in the test turn.
    todo.register(engine.registry)
    engine.devin = de
    plan = CheckedWire(engine.base_url + '/responses', TARGET['plan'][0], 'plan')
    engine.http = plan
    api = None
    host = None
    try:
        models = {}
        if 'plan' in names:
            engine.accounts = AccountStore(profile / 'chatgpt')
            engine.tokens = CurrentAccess(engine.accounts)
            token = await engine.tokens.access_token()
            r = await plan.request('GET', engine.base_url + '/models', headers={'Authorization': 'Bearer ' + token}, timeout=20)
            if r.status != 200:
                raise ValueError('Plan catalog unavailable')
            engine._models = models_from_result(r.json())  # No fallback catalog during a live trial.
            models['plan'] = 'chatgpt:' + TARGET['plan'][0]
        if 'api' in names:
            providers = ProviderStore(profile / 'models.json', profile / 'providers.json')
            spec, = [s for s in providers.api_models() if s.native == TARGET['api'][0] and s.kind in ('generic-chat-completion-api', 'openai')
                     and s.base_url.rstrip('/') in ('https://api.deepseek.com', 'https://api.deepseek.com/v1')]
            chat = spec.kind == 'generic-chat-completion-api'
            api = CheckedWire(spec.base_url.rstrip('/') + ('/chat/completions' if chat else '/responses'),
                              spec.native, 'api' if chat else 'responses')
            engine.api = ApiBackend(providers, http=api)
            models['api'] = spec.id
        if 'devin' in names:
            await de.list_models()
            models['devin'] = 'devin:' + TARGET['devin'][0]
        for n in set(names):
            info = engine.model_info(native_model(models[n]))
            if not info or TARGET[n][1] not in info.efforts:
                raise ValueError('Requested model/effort unavailable')
        ids = [models[n] for n in names]
        efforts = {f'{role}:{mid}': TARGET[n][1] for role, mid, n in zip(roles, ids, names)}
        policy = Policy(ids[0], (ids[1],), ids[2], max_calls=2, max_seconds=75, reasoning=efforts)
        folder = project / 'task'
        folder.mkdir()
        host = await engine.open_new(folder, model=native_model(ids[0]), reasoning=TARGET[names[0]][1])
        host.enable_fusion(policy)
        args = {'tasks': [{'workstream': role + ' check', 'paths': [role + '.txt'], 'role': role, 'model': mid,
                           'task': 'No tools or edits. Reply with exactly ' + role.upper() + '_OK.'}
                          for role, mid in zip(roles[1:], ids[1:])]}
        prompt = ('Live routing contract: actually call delegate_tasks exactly once with this JSON: ' + json.dumps(args) +
                  '. Do not simulate the experts. Once both results arrive, acknowledge them in one short sentence.')
        row['status'] = 'attempted'
        if not host.send_prompt(prompt):
            raise ValueError('Prompt rejected')
        async with asyncio.timeout(130):
            while host.busy or host._turn is not None:
                await asyncio.sleep(.1)
        row['main_done'] = bool(host.saved.turns and host.saved.turns[-1].status == 'done')
        output = '\n'.join(str(i.get('output', '')) for i in host.saved.items if i.get('type') == 'function_call_output')
        row['checks'] = {}
        for v in host.agents.values():
            role, c = v.record.role, v.controller
            if role not in roles[1:] or c is None:
                continue
            i = roles.index(role)
            answer = host._fusion._final_text(c).strip()
            checks = {'finished': bool(v.record.status == 'done' and c.saved.turns and c.saved.turns[-1].status == 'done'),
                      'model': v.record.model == ids[i] and c.model == native_model(ids[i]),
                      'effort': c.reasoning == TARGET[names[i]][1], 'receipt': bool(answer and answer in output)}
            row['checks'][role] = checks
            row['experts'][role] = all(checks.values())
        row['status'] = 'passed' if (row['main_done'] and host.model == native_model(ids[0]) and host.reasoning == TARGET[names[0]][1]
            and row['experts'] == {'worker': True, 'light': True} and len(host.agents) == 2) else 'not_verified'
    except Exception as error:
        row['error_type'] = type(error).__name__  # Never save model text, credentials or account/session IDs.
    finally:
        try:
            await engine.close_all()
        finally:
            await de.close_all()
        row['requests'] = {'plan': plan.count, 'api': api.count if api else 0}
        (evidence / (cell.replace(',', '-') + '.json')).write_text(json.dumps(row, indent=2), encoding='utf-8')
        print(json.dumps(row), flush=True)
    return row['status'] == 'passed'


async def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--cell', action='append', choices=CELLS)
    parser.add_argument('--profile', type=Path)
    args = parser.parse_args()
    if not args.cell:
        print('\n'.join(CELLS))
        return True
    if len(args.cell) != len(set(args.cell)) or not args.profile or not args.profile.is_dir():
        parser.error('Choose distinct cells and an existing credential profile')
    work, evidence = (Path(os.environ[k]) for k in ('WITTYCODE_TEST_WORK', 'WITTYCODE_TEST_EVIDENCE'))
    logging.disable(logging.CRITICAL)
    semaphore = asyncio.Semaphore(3)
    async def run(cell):
        async with semaphore:
            return await trial(cell, args.profile, work, evidence)
    return all(await asyncio.gather(*(run(c) for c in args.cell)))


if __name__ == '__main__':
    raise SystemExit(0 if asyncio.run(main()) else 1)
