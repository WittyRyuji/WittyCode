"""Fusion's score/cost catalog and event-driven leaderboard refresh, with no external requests."""
from __future__ import annotations

import asyncio
import hashlib
import json
import subprocess
import sys
import threading
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace

import pytest

from desktop.core.fusion import aa_catalog, profiles
from desktop.core.providers import ProviderStore
from desktop.core.settings import AppSettings, default_settings_path
from tests.app_support import MODEL, OTHER_MODEL, make_core
from tests.fakes import until

# The served page has a group header and an actual column header; efforts are embedded in labels.
LEADERBOARD = """<table><thead>
<tr><th></th><th>Intelligence</th><th>Price</th><th>Speed</th></tr>
<tr><th>Model</th><th>Artificial Analysis Intelligence Index</th><th>Cost per TaskUSD</th><th>Time per TaskMinutes</th></tr>
</thead><tbody>
<tr><td>GPT-6.1-Sol Max</td><td>51.5</td><td>$1.75</td><td>12.4</td></tr>
<tr><td>GPT-6.1-Sol High</td><td>52.0</td><td>$1.25</td><td>10.2</td></tr>
<tr><td>GPT-6-Luna (high)</td><td>54.2</td><td>$2.30</td><td>15.0</td></tr>
<tr><td>GPT-6-Luna Low</td><td>48.0</td><td>$1.10</td><td>8.2</td></tr>
<tr><td>Claude Demo 5.2 (Max), Default Fallback</td><td>65</td><td>$26.54</td><td>42.0</td></tr>
<tr><td>Mini (Chat)</td><td>20</td><td>$0.05</td><td>1.1</td></tr>
<tr><td>Grok Demo Xhigh</td><td>42</td><td>–</td><td>3.0</td></tr>
<tr><td>Unscored Max</td><td>–</td><td>$1.05</td><td>3.0</td></tr>
<tr><td>DeepSeek V4.1 Flash 1017 Max</td><td>32</td><td>$0.60</td><td>8.0</td></tr>
<tr><td>DeepSeek V4 Flash Vision Max</td><td>40</td><td>$0.90</td><td>9.0</td></tr>
<tr><td>—</td><td>—</td><td>—</td><td>—</td></tr>
</tbody></table>"""
SOL = "chatgpt:gpt-6.1-sol"
UNKNOWN = {"score": None, "cost": None}
MEASURED = {"score": 51.5, "cost": 1.75}


def catalog_path(root: Path) -> Path:
    return root / "fusion-profiles" / "models.json"


def catalog_at(root: Path) -> dict:
    return json.loads(catalog_path(root).read_text(encoding="utf-8"))["models"]


def seed(root: Path, models: dict) -> None:
    path = catalog_path(root)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"models": models}), encoding="utf-8")


def old_row(model: str, effort: str, score: float, cost: float | None, confidence: str = "exact") -> dict:
    return {"model": model, "effort": effort, "score": score, "taskCostUSD": cost, "confidence": confidence,
            "timeMinutes": 12.0, "fetchedAt": 1_760_000_000.0}


def seed_hash(root: Path, model: str, effort: str, row: dict) -> Path:
    digest = hashlib.sha256(f"{model}\x00{effort}".encode("utf-8")).hexdigest()
    path = catalog_path(root).with_name(f"{digest}.json")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(row), encoding="utf-8")
    return path


def test_stored_catalog_contains_only_two_metrics_per_exact_model_id(tmp_path):
    one = profiles.ensure(tmp_path, MODEL, lambda _: {**MEASURED, "effort": "max", "fetchedAt": 22})
    two = profiles.ensure(tmp_path, "API:DEMO:MODEL", lambda _: {"score": 22, "cost": 0})
    assert one == MEASURED and two == {"score": 22, "cost": 0}
    assert catalog_at(tmp_path) == {MODEL: one, "API:DEMO:MODEL": two}
    assert profiles.load(tmp_path, "api:demo:model") is None
    asked = []
    assert profiles.ensure(tmp_path, MODEL, asked.append) == one
    assert asked == []


def test_old_per_effort_catalog_collapses_without_inventing_scores_or_mixing_costs(tmp_path):
    fallback, measured_twenty, max_ref = "devin:unlisted", "api:demo:measured", "chatgpt:reference"
    seed(tmp_path, {SOL: {"high": old_row(SOL, "high", 48, 0.50), "max": old_row(SOL, "max", 51.5, 1.75)},
                    fallback: {"high": old_row(fallback, "high", 20, None, "fallback")},
                    measured_twenty: {"high": old_row(measured_twenty, "high", 20, 0.5)},
                    max_ref: {"high": old_row(max_ref, "high", 64, 0.5, "max_reference")}})
    before = catalog_path(tmp_path).read_bytes()
    assert profiles.load(tmp_path, SOL) == MEASURED
    assert catalog_path(tmp_path).read_bytes() == before

    def offline(_: str):
        raise OSError("offline")

    profiles.refresh(tmp_path, [], offline)
    assert catalog_at(tmp_path) == {SOL: MEASURED, fallback: UNKNOWN, measured_twenty: {"score": 20, "cost": 0.5},
                                    max_ref: {"score": 64, "cost": None}}


def test_hashed_records_are_imported_without_removing_them(tmp_path):
    path = seed_hash(tmp_path, SOL, "max", old_row(SOL, "max", 51.5, 1.75))
    invalid = seed_hash(tmp_path, SOL, "high", old_row(OTHER_MODEL, "high", 99, 1.0))
    broken = path.with_name("a" * 64 + ".json")
    broken.write_text("{broken", encoding="utf-8")
    before = {p: p.read_bytes() for p in (path, invalid, broken)}
    assert profiles.load(tmp_path, SOL) == MEASURED
    profiles.refresh(tmp_path, [], lambda _: None)
    assert catalog_at(tmp_path) == {SOL: MEASURED}
    assert all(path.read_bytes() == content for path, content in before.items())


def test_new_and_missing_references_can_be_refreshed_without_overwriting_known_values_with_a_miss(tmp_path):
    assert profiles.ensure(tmp_path, MODEL, lambda _: None) == UNKNOWN
    assert profiles.ensure(tmp_path, MODEL, lambda _: MEASURED) == MEASURED
    profiles.refresh(tmp_path, [OTHER_MODEL, MODEL], lambda model: {"score": None, "cost": 0.5} if model == OTHER_MODEL else None)
    assert catalog_at(tmp_path) == {MODEL: MEASURED, OTHER_MODEL: {"score": None, "cost": 0.5}}
    profiles.refresh(tmp_path, [], lambda model: {"score": 62, "cost": None} if model == MODEL else None)
    assert profiles.load(tmp_path, MODEL) == {"score": 62, "cost": None}  # Don't attach an older reference's cost.


def test_corruption_is_a_cache_miss_and_nonfinite_values_are_not_written(tmp_path):
    seed(tmp_path, {})
    catalog_path(tmp_path).write_text("{bad", encoding="utf-8")
    assert profiles.load(tmp_path, MODEL) is None
    assert profiles.ensure(tmp_path, MODEL, lambda _: {"score": float("nan"), "cost": -1}) == UNKNOWN
    assert catalog_at(tmp_path) == {MODEL: UNKNOWN}
    with pytest.raises(ValueError):
        profiles.load(tmp_path, "")


def test_a_failed_replace_leaves_the_previous_catalog_intact(tmp_path, monkeypatch):
    seed(tmp_path, {MODEL: MEASURED})
    path = catalog_path(tmp_path)
    before = path.read_bytes()

    def failed_replace(*args):
        raise OSError("simulated write failure")

    with monkeypatch.context() as patch:
        patch.setattr(profiles.os, "replace", failed_replace)
        with pytest.raises(OSError, match="simulated write failure"):
            profiles.ensure(tmp_path, OTHER_MODEL, lambda _: MEASURED)
    assert path.read_bytes() == before
    assert not list(path.parent.glob("*.tmp"))


def test_concurrent_reads_of_one_model_share_the_fetch(tmp_path):
    hits = []

    def fetch(model):
        hits.append(model)
        return MEASURED

    with ThreadPoolExecutor(max_workers=8) as pool:
        assert list(pool.map(lambda _: profiles.ensure(tmp_path, MODEL, fetch), range(8))) == [MEASURED] * 8
    assert hits == [MODEL]


def test_processes_merge_into_the_latest_catalog(tmp_path):
    worker = """
from pathlib import Path
import sys, time
from desktop.core.fusion import profiles
root, model, other = Path(sys.argv[1]), sys.argv[2], sys.argv[3]

def fetch(_):
    (root / model).touch()
    deadline = time.monotonic() + 10
    while not (root / other).exists():
        if time.monotonic() >= deadline:
            return None
        time.sleep(0.01)
    return {"score": 42, "cost": 0.5}

write = profiles._write

def slow_write(path, models):
    time.sleep(0.2)  # Widen the window between reading the catalog and replacing it.
    write(path, models)

profiles._write = slow_write
assert profiles.ensure(root, model, fetch)["score"] == 42
"""

    def run(pair):
        return subprocess.run([sys.executable, "-c", worker, str(tmp_path), *pair], capture_output=True, text=True, timeout=30)

    with ThreadPoolExecutor(max_workers=2) as pool:
        for completed in pool.map(run, [("first", "second"), ("second", "first")]):
            assert completed.returncode == 0, completed.stderr
    assert catalog_at(tmp_path) == {name: {"score": 42, "cost": 0.5} for name in ("first", "second")}


def test_team_snapshot_reads_one_reference_for_different_efforts(tmp_path):
    seed(tmp_path, {MODEL: MEASURED, OTHER_MODEL: UNKNOWN})
    path = catalog_path(tmp_path)
    before = path.read_bytes()
    snapshot = profiles.team_snapshot(tmp_path, {"main": (MODEL, "high"), "worker": (MODEL, "low"), "light": (OTHER_MODEL, "")})
    assert snapshot.count("capability 51.5, task cost $1.75 per benchmark task") == 2
    assert "not a measurement at the seat's effort or a billing quote" in snapshot
    assert "capability unknown, task cost unknown" in snapshot
    assert path.read_bytes() == before


def test_benchmark_parser_and_reference_selection():
    rows = aa_catalog.parse(LEADERBOARD)
    assert aa_catalog.offer(rows, SOL) == MEASURED  # Use the scored Max row, even if a lower tier happened to score higher.
    assert aa_catalog.offer(rows, "devin:gpt-6-luna-high") == {"score": 54.2, "cost": 2.30}
    assert aa_catalog.offer(rows, "api:demo:custom:Claude Demo 5.2") == {"score": 65, "cost": 26.54}
    assert aa_catalog.offer(rows, "devin:grok-demo-xhigh") == {"score": 42, "cost": None}
    assert aa_catalog.offer(rows, "chatgpt:mini-chat") == {"score": 20, "cost": 0.05}
    assert aa_catalog.offer(rows, "chatgpt:mini") is None
    assert aa_catalog.offer(rows, "devin:swe-2-1-medium") is None
    assert aa_catalog.offer(rows, "chatgpt:unscored") == {"score": None, "cost": 1.05}


def test_deepseek_flash_uses_only_the_v41_flash_1017_reference():
    rows = aa_catalog.parse(LEADERBOARD)
    assert aa_catalog.offer(rows, "deepseek-flash") == {"score": 32, "cost": 0.60}
    absent = [row for row in rows if row.name != "deepseekv41flash1017"]
    assert aa_catalog.offer(absent, "deepseek-flash") is None


def test_parser_finds_columns_by_header_and_keeps_ambiguous_cost_unknown():
    shifted = """<table><tr><th>#</th><th>Model</th><th>Cost per TaskUSD</th><th>Intelligence Index</th></tr>
    <tr><td>1</td><td>Solo High</td><td>$0.50</td><td>33</td></tr></table>"""
    rows = aa_catalog.parse(shifted)
    assert rows == [aa_catalog.Entry("solo", "high", 33, 0.5)]
    conflicting = aa_catalog.Entry("solo", "high", 33, 1.0)
    assert aa_catalog.offer([*rows, conflicting], "solo") == {"score": 33, "cost": None}


class FakeResponse:
    def __init__(self, data):
        self.data = data

    def read(self, limit):
        return self.data[:limit]

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False


def test_download_only_ever_asks_artificial_analysis():
    asked = []

    def opener(request, timeout):
        asked.append((request.full_url, timeout))
        return FakeResponse(b"<html></html>")

    assert aa_catalog.download(opener=opener) == "<html></html>"
    with pytest.raises(ValueError):
        aa_catalog.download("http://artificialanalysis.ai/leaderboards/models", opener=opener)
    with pytest.raises(ValueError):
        aa_catalog.download("https://example.com/private.json", opener=opener)
    with pytest.raises(ValueError):
        aa_catalog.download(opener=lambda r, timeout: FakeResponse(b"x" * (aa_catalog.MAX_BYTES + 1)))
    assert asked == [(aa_catalog.URL, aa_catalog.TIMEOUT_SECONDS)]


def test_the_default_opener_cannot_be_redirected_off_the_host(monkeypatch):
    monkeypatch.setattr(aa_catalog, "_urlopen", lambda request, timeout: FakeResponse(b"<html></html>"))
    assert aa_catalog.download() == "<html></html>"
    handler = aa_catalog._SameHostRedirect()
    req = urllib.request.Request(aa_catalog.URL)
    assert handler.redirect_request(req, None, 302, "", {}, "https://artificialanalysis.ai/other") is not None
    assert handler.redirect_request(req, None, 302, "", {}, "https://other.example/x") is None
    assert handler.redirect_request(req, None, 302, "", {}, "http://artificialanalysis.ai/x") is None


def test_batch_uses_one_public_page_for_all_models(tmp_path):
    downloads = []

    def loader():
        downloads.append(1)
        return LEADERBOARD

    profiles.refresh(tmp_path, [SOL, "devin:gpt-6-luna-high", "devin:unlisted"], aa_catalog.source(loader=loader))
    assert downloads == [1]
    assert catalog_at(tmp_path) == {SOL: MEASURED, "devin:gpt-6-luna-high": {"score": 54.2, "cost": 2.30},
                                   "devin:unlisted": UNKNOWN}


def test_a_failed_download_is_not_retried_until_a_new_batch(tmp_path):
    downloads = []

    def loader():
        downloads.append(1)
        if len(downloads) == 1:
            raise OSError("offline")
        return LEADERBOARD

    profiles.refresh(tmp_path, [SOL, "devin:unlisted"], aa_catalog.source(loader=loader))
    assert downloads == [1]
    assert catalog_at(tmp_path) == {SOL: UNKNOWN, "devin:unlisted": UNKNOWN}
    profiles.refresh(tmp_path, [], aa_catalog.source(loader=loader))
    assert downloads == [1, 1]
    assert profiles.load(tmp_path, SOL) == MEASURED


def test_startup_upgrades_old_estimates_and_uses_api_native_id_without_editing_provider_settings(tmp_path):
    async def go():
        root = default_settings_path().parent
        custom = "api:demo:custom:My Favorite"
        unlisted = "devin:unlisted"
        seed(root, {model: {"high": old_row(model, "high", 20, None, "fallback")} for model in (custom, unlisted)})
        provider_config = root / "models.json"
        provider_config.write_text('{"customModels": []}\n', encoding="utf-8")
        before = provider_config.read_bytes()
        providers = SimpleNamespace(list=lambda: [], api_models=lambda: [SimpleNamespace(id=custom, native="deepseek-flash")])
        # Only the profile lookup needs the provider for this test; the engine does not need an actual connection.
        settings = AppSettings(default_model=custom, fusion_main=SOL)
        core, _, _, _, _ = make_core(tmp_path, projects=False, plan=False, providers=providers, settings=settings)
        core._profiles_source = aa_catalog.source(loader=lambda: LEADERBOARD)
        deepseek = {"score": 32, "cost": 0.60}
        try:
            await core.call("app.init")
            await until(lambda: catalog_at(root).get(custom) == deepseek)
            assert catalog_at(root) == {custom: deepseek, SOL: MEASURED, unlisted: UNKNOWN}
            assert provider_config.read_bytes() == before
        finally:
            await core.shutdown()
    asyncio.run(go())


def test_startup_checks_available_models_once_and_using_them_does_not_lookup_again(tmp_path):
    async def go():
        core, _, events, _, _ = make_core(tmp_path, settings=AppSettings())
        root = default_settings_path().parent
        asked = []

        def fetch(model):
            asked.append(model)
            return None  # Even unknown models must not be retried on selection or session ready.

        core._profiles_source = fetch
        try:
            await core.call("app.init")
            await until(lambda: profiles.load(root, OTHER_MODEL) == UNKNOWN)
            assert set(asked) == {m["id"] for m in core.catalog.models}
            assert len(asked) == len(set(asked))
            startup_calls = asked[:]
            await core.call("app.init")
            await until(lambda: len(events.of("models")) >= 2)
            await core.call("session.new", {"projectId": str(tmp_path / "proj")})
            await core.call("session.choose", {"field": "model", "value": OTHER_MODEL})
            await core.call("settings.set", {"key": "fusionMain", "value": OTHER_MODEL})
            await core.call("settings.set", {"key": "fusionReasoning", "value": {"main:" + OTHER_MODEL: "low"}})
            await asyncio.gather(*list(core._tasks))
            assert asked == startup_calls
        finally:
            await core.shutdown()
    asyncio.run(go())


def test_adding_models_gets_one_fresh_batch_without_blocking_the_provider_save(tmp_path, monkeypatch):
    async def go():
        root = default_settings_path().parent
        store = ProviderStore(root / "models.json", sidecar=root / "providers.json")
        draft = {"id": "demo", "name": "Demo", "kind": "openai", "baseUrl": "https://example.invalid/v1",
                 "keyMode": "none", "models": [{"id": "gpt-6.1-sol", "displayName": "Custom Sol"}]}
        store.save(draft)
        saved_id = store.api_models()[0].id
        seed(root, {saved_id: UNKNOWN})
        gate = threading.Event()
        threads = []

        def open_page(*args, **kwargs):
            threads.append(threading.get_ident())
            if len(threads) == 2:
                gate.wait(15)
                # The existing model must not be re-scored by the addition batch.
                return FakeResponse(LEADERBOARD.replace("51.5", "99").encode())
            return FakeResponse(LEADERBOARD.encode())

        monkeypatch.setattr(aa_catalog, "_urlopen", open_page)
        core, _, events, _, _ = make_core(tmp_path, providers=store, projects=False)
        try:
            await core.call("app.init")
            await until(lambda: profiles.load(root, saved_id) == MEASURED)
            assert len(threads) == 1
            resaved = await core.call("providers.save", {"draft": {**draft, "name": "Renamed"}})
            assert resaved["name"] == "Renamed"
            await until(lambda: len(events.of("models")) >= 2)
            assert len(threads) == 1

            rows = [*draft["models"], {"id": "gpt-6-luna", "displayName": "Custom Luna"},
                    {"id": "grok-demo", "displayName": "Custom Grok"}]
            saved = await asyncio.wait_for(core.call("providers.save", {"draft": {**draft, "models": rows}}), 10)
            assert len(saved["models"]) == 3
            await until(lambda: len(threads) >= 2)
            ids = {spec.native: spec.id for spec in store.api_models()}
            assert profiles.load(root, ids["gpt-6-luna"]) is None
            gate.set()
            await until(lambda: profiles.load(root, ids["gpt-6-luna"]) == {"score": 54.2, "cost": 2.30})
            assert profiles.load(root, ids["grok-demo"]) == {"score": 42, "cost": None}
            assert profiles.load(root, saved_id) == MEASURED
            assert len(threads) == 2 and threading.get_ident() not in threads

            # A new native model can reuse the old display-based page ID, but not its score.
            rows[0] = {**rows[0], "id": "unlisted-model"}
            await core.call("providers.save", {"draft": {**draft, "models": rows}})
            assert next(spec.id for spec in store.api_models() if spec.native == "unlisted-model") == saved_id
            await until(lambda: profiles.load(root, saved_id) == UNKNOWN)
            assert len(threads) == 3
        finally:
            gate.set()
            await core.shutdown()
    asyncio.run(go())
