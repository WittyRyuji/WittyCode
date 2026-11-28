"""Offline checks of ``evals.inventory_resume_case.plan_meter``: fake transport, temp ledger, temp
account files — no network, no real account, no credentials."""

from __future__ import annotations

import asyncio
import json
import time
from pathlib import Path

import pytest

from evals.inventory_resume_case.plan_meter import (
    LIMIT,
    OFFICIAL_MODELS,
    OFFICIAL_RESPONSES,
    RESERVE,
    BudgetLedger,
    MeterBlocked,
    _MeteredHttp,
    make_engine,
)

MODEL = "gpt-test-model"
EFFORT = "high"
CATALOG = {
    "models": [
        {
            "slug": MODEL,
            "display_name": "Test model",
            "visibility": "list",
            "context_window": 400_000,
            "supported_reasoning_efforts": ["low", "medium", "high"],
        }
    ]
}


def _sse(payload: dict) -> list[str]:
    return [f"data: {json.dumps(payload)}", ""]


def _completed(inp: int = 10, out: int = 5, cached: int = 3, reasoning: int = 2) -> list[str]:
    return _sse({
        "type": "response.completed",
        "response": {
            "id": "resp_test",
            "status": "completed",
            "model": MODEL,
            "usage": {
                "input_tokens": inp,
                "output_tokens": out,
                "total_tokens": inp + out,
                "input_tokens_details": {"cached_tokens": cached},
                "output_tokens_details": {"reasoning_tokens": reasoning},
            },
        },
    })


class _FakeInner:
    """An ``HttpClient``-shaped fake: canned SSE lines or a canned error; records what was asked."""

    def __init__(self, lines: list[str] | None = None, error: BaseException | None = None, response: object = None) -> None:
        self.lines = lines or []
        self.error = error
        self.response = response
        self.calls: list[tuple[str, str]] = []

    async def stream_lines(self, method, url, *, headers=None, json_body=None, body=None, timeout=None):
        self.calls.append((method, url))
        if self.error is not None:
            raise self.error
        for line in self.lines:
            yield line

    async def request(self, method, url, **kwargs):
        self.calls.append((method, url))
        return self.response


def _metered(inner: _FakeInner, ledger: BudgetLedger, catalog: dict | None = None) -> _MeteredHttp:
    http = _MeteredHttp(inner, ledger, model=MODEL, effort=EFFORT)
    http.note_catalog(CATALOG if catalog is None else catalog)  # inject the fake model catalog
    return http


class _FakeResponse:
    """Minimal ``HttpResponse`` stand-in for GET /models."""

    def __init__(self, payload: dict, status: int = 200) -> None:
        self.status = status
        self._payload = payload

    def json(self):
        return self._payload


def _body(model: str = MODEL, effort: str | None = EFFORT) -> dict:
    request: dict = {"model": model, "input": [], "store": False, "stream": True}
    if effort:
        request["reasoning"] = {"effort": effort, "summary": "auto"}
    return request


async def _drain(http: _MeteredHttp, body: dict | None = None, url: str = OFFICIAL_RESPONSES) -> list[str]:
    out: list[str] = []
    async for line in http.stream_lines("POST", url, headers={"Authorization": "Bearer x"}, json_body=body or _body()):
        out.append(line)
    return out


def run(coro):
    return asyncio.run(coro)


# ------------------------------------------------------------------ ledger


def test_reservation_and_settlement(tmp_path: Path) -> None:
    ledger = BudgetLedger(tmp_path / "budget.db")
    http = _metered(_FakeInner(_completed()), ledger)
    run(_drain(http))
    totals = ledger.totals()
    assert totals == {"spent": 15, "pending": 0, "settled": 1, "limit": LIMIT}
    row = ledger.rows()[0]
    assert row["status"] == "settled" and row["used"] == 15
    assert row["input_tokens"] == 10 and row["output_tokens"] == 5
    assert row["cached_tokens"] == 3 and row["reasoning_tokens"] == 2
    assert row["model"] == MODEL and row["effort"] == EFFORT
    assert row["seconds"] >= 0 and row["outcome"] == "completed"


def test_pending_blocks_the_next_post(tmp_path: Path) -> None:
    ledger = BudgetLedger(tmp_path / "budget.db")
    post_id = ledger.begin(MODEL, EFFORT)  # reserved but never settled
    assert post_id == 1
    with pytest.raises(MeterBlocked):
        ledger.begin(MODEL, EFFORT)  # unknown usage -> everything after stops
    assert ledger.totals() == {"spent": RESERVE, "pending": 1, "settled": 0, "limit": LIMIT}


def test_pending_survives_reentry(tmp_path: Path) -> None:
    path = tmp_path / "budget.db"
    BudgetLedger(path).begin(MODEL, EFFORT)  # 'crash': no settle, object dropped
    ledger = BudgetLedger(path)  # a new process/side opens the same file
    with pytest.raises(MeterBlocked):
        ledger.begin(MODEL, EFFORT)
    assert ledger.totals()["pending"] == 1


def test_hard_limit(tmp_path: Path) -> None:
    ledger = BudgetLedger(tmp_path / "budget.db")
    post_id = ledger.begin(MODEL, EFFORT)
    ledger.settle(
        post_id, used=LIMIT - RESERVE + 1, input_tokens=LIMIT - RESERVE + 1, output_tokens=0,
        cached_tokens=0, reasoning_tokens=0, cache_write_tokens=None, seconds=1.0,
        response_id="r", outcome="completed",
    )
    with pytest.raises(MeterBlocked):
        ledger.begin(MODEL, EFFORT)


def test_multiple_sends_accumulate(tmp_path: Path) -> None:
    ledger = BudgetLedger(tmp_path / "budget.db")
    http = _metered(_FakeInner(_completed()), ledger)
    run(_drain(http))
    run(_drain(http))
    totals = ledger.totals()
    assert totals["settled"] == 2 and totals["spent"] == 30 and totals["pending"] == 0


# ------------------------------------------------------------------ transport guard


def test_wrong_model_refused(tmp_path: Path) -> None:
    ledger = BudgetLedger(tmp_path / "budget.db")
    inner = _FakeInner(_completed())
    http = _metered(inner, ledger)
    with pytest.raises(MeterBlocked):
        run(_drain(http, _body(model="other-model")))
    assert inner.calls == [] and ledger.rows() == []  # nothing sent, nothing reserved


def test_wrong_effort_refused(tmp_path: Path) -> None:
    ledger = BudgetLedger(tmp_path / "budget.db")
    inner = _FakeInner(_completed())
    http = _metered(inner, ledger)
    with pytest.raises(MeterBlocked):
        run(_drain(http, _body(effort="low")))
    assert inner.calls == []


def test_non_whitelist_post_refused(tmp_path: Path) -> None:
    ledger = BudgetLedger(tmp_path / "budget.db")
    inner = _FakeInner()
    http = _metered(inner, ledger)
    with pytest.raises(MeterBlocked):
        run(_drain(http, url="https://api.openai.com/v1/chat/completions"))
    with pytest.raises(MeterBlocked):
        run(http.request("POST", "https://auth.openai.com/api/accounts/oauth/token"))
    assert inner.calls == []


def test_request_allows_only_get_models(tmp_path: Path) -> None:
    ledger = BudgetLedger(tmp_path / "budget.db")
    inner = _FakeInner(response=_FakeResponse(CATALOG))
    http = _MeteredHttp(inner, ledger, model=MODEL, effort=EFFORT)
    response = run(http.request("GET", OFFICIAL_MODELS))
    assert response.status == 200 and http._window == 400_000
    with pytest.raises(MeterBlocked):
        run(http.request("POST", OFFICIAL_RESPONSES))  # non-streaming generation is refused too
    with pytest.raises(MeterBlocked):
        run(http.request("GET", "https://api.openai.com/v1/usage"))
    assert inner.calls == [("GET", OFFICIAL_MODELS)]


def test_post_requires_verified_catalog(tmp_path: Path) -> None:
    ledger = BudgetLedger(tmp_path / "budget.db")
    inner = _FakeInner(_completed())
    http = _MeteredHttp(inner, ledger, model=MODEL, effort=EFFORT)  # no catalog proof injected
    with pytest.raises(MeterBlocked):
        run(_drain(http))
    assert inner.calls == [] and ledger.rows() == []


def test_catalog_rejects_unknown_or_oversized(tmp_path: Path) -> None:
    http = _MeteredHttp(_FakeInner(), BudgetLedger(tmp_path / "b.db"), model=MODEL, effort=EFFORT)
    for name, payload in (
        ("window_too_large", {"models": [{**CATALOG["models"][0], "context_window": RESERVE + 1}]}),
        ("window_missing", {"models": [{k: v for k, v in CATALOG["models"][0].items() if k != "context_window"}]}),
        ("model_absent", {"models": []}),
        ("effort_not_declared", {"models": [{**CATALOG["models"][0], "supported_reasoning_efforts": ["low"]}]}),
    ):
        with pytest.raises(MeterBlocked, match="plan_meter"):
            http.note_catalog(payload)
        assert http._window is None, name  # a bad catalog leaves no proof behind


def test_catalog_recheck_drops_old_proof(tmp_path: Path) -> None:
    ledger = BudgetLedger(tmp_path / "budget.db")
    http = _MeteredHttp(_FakeInner(response=_FakeResponse(CATALOG)), ledger, model=MODEL, effort=EFFORT)
    run(http.request("GET", OFFICIAL_MODELS))
    assert http._window == 400_000
    http._inner = _FakeInner(response=_FakeResponse({"models": []}))  # the model is gone: old proof dies
    with pytest.raises(MeterBlocked):
        run(http.request("GET", OFFICIAL_MODELS))
    assert http._window is None
    with pytest.raises(MeterBlocked):
        run(_drain(http))
    assert ledger.rows() == []
    http.note_catalog(CATALOG)
    http._inner = _FakeInner(response=_FakeResponse({}, status=503))  # a failed re-check also drops it
    run(http.request("GET", OFFICIAL_MODELS))
    assert http._window is None


def test_stream_lines_rejects_get(tmp_path: Path) -> None:
    ledger = BudgetLedger(tmp_path / "budget.db")
    inner = _FakeInner(_completed())
    http = _metered(inner, ledger)

    async def get_stream() -> list[str]:
        return [line async for line in http.stream_lines("GET", OFFICIAL_MODELS)]

    with pytest.raises(MeterBlocked):
        run(get_stream())
    assert inner.calls == [] and ledger.rows() == []


def test_usage_requires_strict_ints(tmp_path: Path) -> None:
    def lines(usage: dict) -> list[str]:
        return _sse({"type": "response.completed", "response": {"id": "r", "status": "completed", "model": MODEL, "usage": usage}})

    for name, usage in (
        ("missing_output", {"input_tokens": 10}),
        ("bool_input", {"input_tokens": True, "output_tokens": 5}),
        ("float_input", {"input_tokens": 10.5, "output_tokens": 5}),
    ):
        ledger = BudgetLedger(tmp_path / f"{name}.db")
        run(_drain(_metered(_FakeInner(lines(usage)), ledger)))
        row = ledger.rows()[0]
        assert row["status"] == "pending" and row["error"] == "invalid_usage", name


def test_http_error_leaves_pending_and_blocks(tmp_path: Path) -> None:
    ledger = BudgetLedger(tmp_path / "budget.db")
    inner = _FakeInner(error=RuntimeError("boom"))
    http = _metered(inner, ledger)
    with pytest.raises(RuntimeError):
        run(_drain(http))
    rows = ledger.rows()
    assert len(rows) == 1 and rows[0]["status"] == "pending" and rows[0]["error"] == "RuntimeError"
    with pytest.raises(MeterBlocked):
        run(_drain(_metered(_FakeInner(_completed()), ledger)))  # the 401-style retry cannot pass


def test_stream_without_terminal_leaves_pending(tmp_path: Path) -> None:
    ledger = BudgetLedger(tmp_path / "budget.db")
    http = _metered(_FakeInner(["data: {\"type\":\"response.created\"}", ""]), ledger)
    run(_drain(http))
    row = ledger.rows()[0]
    assert row["status"] == "pending" and row["error"] == "no_terminal_event"


def test_usage_missing_or_substituted_model_not_settled(tmp_path: Path) -> None:
    ledger = BudgetLedger(tmp_path / "budget.db")
    no_usage = _sse({"type": "response.completed", "response": {"id": "r", "status": "completed"}})
    http = _metered(_FakeInner(no_usage), ledger)
    run(_drain(http))
    assert ledger.rows()[0]["status"] == "pending" and ledger.rows()[0]["error"] == "invalid_usage"
    echoed = _completed()  # provider echoes a different model -> unknown usage, do not settle
    echoed[0] = echoed[0].replace(f'"model": "{MODEL}"', '"model": "other-model"')
    ledger2 = BudgetLedger(tmp_path / "budget2.db")
    run(_drain(_metered(_FakeInner(echoed), ledger2)))
    assert ledger2.rows()[0]["status"] == "pending" and ledger2.rows()[0]["error"] == "invalid_usage"


# ------------------------------------------------------------------ engine wiring


def _fake_account(folder: Path, *, expired: bool = False) -> Path:
    store_dir = folder / "account"
    (store_dir / "accounts").mkdir(parents=True)
    record = {
        "email": "eval@example.test",
        "issuer": "https://auth.openai.com",
        "subject": "sub-eval",
        "client_id": "oaiapp_eval",
        "ext_agent_host_id": "urn:uuid:00000000-0000-0000-0000-000000000001",
        "id_token": "id",
        "access_token": "access-token-for-test",
        "refresh_token": "refresh-token-for-test",
        "token_type": "Bearer",
        "expires_in": 1 if expired else 3600,
        "scopes": ["openid", "chatgpt.tokens.use.direct"],
        "saved_at": time.time() - (7200 if expired else 0),
    }
    (store_dir / "accounts" / "oaiapp_eval.json").write_text(json.dumps(record), encoding="utf-8")
    (store_dir / "state.json").write_text(json.dumps({"active": "oaiapp_eval"}), encoding="utf-8")
    return store_dir


def test_make_engine_wiring(tmp_path: Path) -> None:
    from desktop.core.chatgpt.engine import ChatGPTEngine

    account = _fake_account(tmp_path)
    root, budget = tmp_path / "side-a", tmp_path / "budget.db"
    engine = make_engine(account, root, budget, model=MODEL, reasoning=EFFORT)
    assert isinstance(engine, ChatGPTEngine)
    assert engine.sessions.root == root / "chatgpt"
    assert engine.accounts.root == account
    assert engine.accounts.active_id() == "oaiapp_eval"
    with pytest.raises(MeterBlocked):
        engine.accounts.save(engine.accounts.get("oaiapp_eval"))  # account storage is read-only

    async def tokens() -> str:
        return await engine.tokens.access_token()

    assert run(tokens()) == "access-token-for-test"  # stored token reused, no refresh

    engine.http._inner = _FakeInner(_completed())
    engine.http.note_catalog(CATALOG)  # proof, the way a real GET /models leaves it

    async def stream() -> list:
        out = []
        async for event in engine.stream({"model": MODEL, "reasoning": {"effort": EFFORT}, "input": []}):
            out.append(event)
        return out

    events = run(stream())
    assert type(events[-1]).__name__ == "Finished"
    assert BudgetLedger(budget).totals()["settled"] == 1


def test_expired_token_fails_closed(tmp_path: Path) -> None:
    from desktop.core.chatgpt.errors import AuthError

    account = _fake_account(tmp_path, expired=True)
    engine = make_engine(account, tmp_path / "side", tmp_path / "budget.db", model=MODEL, reasoning=EFFORT)

    async def tokens() -> str:
        return await engine.tokens.access_token()

    with pytest.raises(AuthError):  # a refresh would rewrite the account: refused instead
        run(tokens())
    assert BudgetLedger(tmp_path / "budget.db").rows() == []
