"""Offline checks for the opt-in live Fusion transport matrix (``evals.live_fusion_matrix``)."""
from __future__ import annotations

import asyncio
import sys

import pytest

from evals.live_fusion_matrix import __main__ as entry


def test_list_cells_prints_27_unique_without_credentials(monkeypatch, capsys):
    """A bare listing must not read accounts, providers, env dirs or build any engine."""
    for name in ("AccountStore", "ProviderStore", "ChatGPTEngine", "DevinEngine", "AppSettings", "HttpClient"):
        monkeypatch.setattr(entry, name, lambda *a, **k: pytest.fail(f"{name} must not be built when listing cells"))
    for var in ("WITTYCODE_TEST_WORK", "WITTYCODE_TEST_EVIDENCE"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setattr(sys, "argv", ["prog"])
    assert asyncio.run(entry.main()) is True
    lines = [line for line in capsys.readouterr().out.splitlines() if line.strip()]
    assert len(lines) == 27 and len(set(lines)) == 27
    for cell in lines:
        names = cell.split(",")
        assert len(names) == 3 and set(names) <= {"plan", "devin", "api"}


@pytest.mark.parametrize("cell", ["plan,factory,api", "factory,factory,factory", "devin,plan,factory"])
def test_factory_cells_are_rejected(monkeypatch, cell):
    monkeypatch.setattr(sys, "argv", ["prog", "--cell", cell, "--profile", "."])
    with pytest.raises(SystemExit):
        asyncio.run(entry.main())


def test_no_factory_in_matrix():
    assert not any("factory" in cell for cell in entry.CELLS)


def _collect(wire, method, url, body):
    async def go():
        out = []
        async for line in wire.stream_lines(method, url, json_body=body, headers={}):
            out.append(line)
        return out
    return asyncio.run(go())


@pytest.mark.parametrize("backend,good_body,bad_effort", [
    ("plan", {"model": "m", "reasoning": {"effort": "low"}}, {"reasoning": {"effort": "high"}}),
    ("api", {"model": "m", "reasoning_effort": "low"}, {"reasoning_effort": "high"}),
])
def test_checked_wire_refuses_bad_destination_model_effort(monkeypatch, backend, good_body, bad_effort):
    url = "https://matrix.invalid/responses"

    async def fake_lines(self, method, url, **kw):
        yield '{"wire": "ok"}'

    # An accepted request would reach the network; the sentinel keeps every assertion offline.
    monkeypatch.setattr(entry.HttpClient, "stream_lines", fake_lines)
    wire = entry.CheckedWire(url, "m", backend)
    assert _collect(wire, "POST", url, dict(good_body)) == ['{"wire": "ok"}']
    assert wire.count == 1

    refused = [
        ("GET", url, good_body),
        ("POST", url + "/other", good_body),
        ("POST", url, {**good_body, "model": "other-model"}),
        ("POST", url, {**good_body, **bad_effort}),
        ("POST", url, {**good_body, "thinking": {"type": "disabled"}}),
    ]
    for method, target, body in refused:
        with pytest.raises(ValueError):
            _collect(wire, method, target, body)
    assert wire.count == 1

    wire.count = 8
    with pytest.raises(ValueError):
        _collect(wire, "POST", url, dict(good_body))
