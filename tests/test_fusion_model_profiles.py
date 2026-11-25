"""Fusion model profiles: exact (model, effort) JSON records, the Artificial Analysis leaderboard fetch, and the
app's non-blocking prefetch on model selection. Every fetch is an injected fake; nothing here touches the network.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import subprocess
import sys
import threading
import time
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from desktop.core.fusion import aa_catalog, profiles
from desktop.core.settings import default_settings_path
from tests.app_support import MODEL, OTHER_MODEL, ready_core
from tests.fakes import until

# The served shape of https://artificialanalysis.ai/leaderboards/models: a group header row over the column row,
# the model's effort as a suffix of its name ("Max", "Xhigh", ", Default Fallback", "Reasoning").
LEADERBOARD = """<html><body><main>
<table><thead>
<tr><th></th><th>Intelligence</th><th>Price</th><th>Speed</th><th>Latency</th><th>Features</th><th></th></tr>
<tr><th>Model</th><th>Artificial Analysis Intelligence Index</th><th>Cost per TaskUSD</th>
<th>Time per TaskMinutes</th><th>First AnswerToken (s)</th><th>Context Window</th><th>Further Analysis</th></tr>
</thead><tbody>
<tr><td><a href="/models/gpt61sol">GPT-6.1-Sol Max</a></td><td>51.5</td><td>$1.75</td><td>12.4</td><td>80.8</td><td>1M</td><td>ModelProviders</td></tr>
<tr><td>GPT-6-Luna High</td><td>54.2</td><td>$2.30</td><td>15.0</td><td>71.2</td><td>1M</td><td>ModelProviders</td></tr>
<tr><td>GPT-6-Luna Low</td><td>48.0</td><td>$1.10</td><td>8.2</td><td>44.4</td><td>1M</td><td>ModelProviders</td></tr>
<tr><td>Claude Demo 5.2 Max, Default Fallback</td><td>65.0</td><td>$26.54</td><td>42.0</td><td>240.7</td><td>1M</td><td>ModelProviders</td></tr>
<tr><td>Mini (Chat)</td><td>20.1</td><td>$0.05</td><td>1.1</td><td>12.0</td><td>128K</td><td>ModelProviders</td></tr>
<tr><td>Nemotron 3 Ultra Reasoning</td><td>33.0</td><td>$0.40</td><td>5.0</td><td>30.0</td><td>256K</td><td>ModelProviders</td></tr>
<tr><td>Duo</td><td>40.0</td><td>$1.00</td><td>10.0</td><td>50.0</td><td>1M</td><td>ModelProviders</td></tr>
<tr><td>Duo</td><td>41.0</td><td>$1.20</td><td>9.0</td><td>51.0</td><td>1M</td><td>ModelProviders</td></tr>
<tr><td>—</td><td>—</td><td>—</td><td>—</td><td>—</td><td>—</td><td>—</td></tr>
</tbody></table>
</main></body></html>"""

# The same figures with the model in the second column: the parser finds it by the header, never by position.
LEADERBOARD_SHIFTED = """<table><thead>
<tr><th></th><th></th><th>Intelligence</th><th>Price</th></tr>
<tr><th>#</th><th>Model</th><th>Artificial Analysis Intelligence Index</th><th>Cost per TaskUSD</th>
<th>Time per TaskMinutes</th></tr>
</thead><tbody>
<tr><td>1</td><td>Solo Ranker High</td><td>33.0</td><td>$0.50</td><td>4.0</td></tr>
</tbody></table>"""


def entries() -> list[aa_catalog.Entry]:
    return aa_catalog.parse(LEADERBOARD)


def catalog_path(root: Path) -> Path:
    return root / "fusion-profiles" / "models.json"


def catalog_at(root: Path) -> dict:
    return json.loads(catalog_path(root).read_text(encoding="utf-8"))["models"]


def legacy_path(root: Path, model: str, effort: str) -> Path:
    digest = hashlib.sha256(f"{model}\x00{effort}".encode("utf-8")).hexdigest()
    return catalog_path(root).with_name(f"{digest}.json")


def seeded(root: Path, model: str = MODEL, effort: str = "high", *, legacy: bool = False, **fields) -> dict:
    record = {"model": model, "effort": effort, "source": profiles.SOURCE, "score": 50.0, "costIn": 1.0,
              "costOut": 8.0, "taskCostUSD": 2.5, "timeMinutes": 9.0, "fetchedAt": 1_760_000_000.0,
              "confidence": profiles.CONFIDENCE_EXACT}
    record.update(fields)
    path = legacy_path(root, model, effort) if legacy else catalog_path(root)
    path.parent.mkdir(parents=True, exist_ok=True)
    if legacy:
        payload = record
    else:
        models = catalog_at(root) if path.exists() else {}
        models.setdefault(model, {})[effort] = record
        payload = {"models": models}
    path.write_text(json.dumps(payload), encoding="utf-8")
    return record


# --------------------------------------------------------------------------- the records themselves

def test_ensure_fetches_and_stores_the_exact_pair(tmp_path):
    calls = []

    def fetch(model, effort):
        calls.append((model, effort))
        return {"score": 55.25, "taskCostUSD": 1.5, "timeMinutes": 9.5}

    record = profiles.ensure(tmp_path, MODEL, "high", fetch, clock=lambda: 1_761_234_567.0)
    assert calls == [(MODEL, "high")]
    assert record["model"] == MODEL and record["effort"] == "high"
    assert record["score"] == 55.25 and record["taskCostUSD"] == 1.5 and record["timeMinutes"] == 9.5
    assert record["source"] == profiles.SOURCE and record["confidence"] == profiles.CONFIDENCE_EXACT
    assert record["fetchedAt"] == 1_761_234_567.0
    assert catalog_at(tmp_path) == {MODEL: {"high": record}}
    assert profiles.load(tmp_path, MODEL, "high") == record


def test_a_stored_record_is_a_hit_and_sends_no_request(tmp_path):
    first = seeded(tmp_path)
    attempts = []

    def fetch(model, effort):
        attempts.append((model, effort))
        return {"score": 99.0}

    assert profiles.ensure(tmp_path, MODEL, "high", fetch) == first
    # A young fallback is a hit too: repeating the selection (reopen, another sign-in event) sends nothing.
    profiles.ensure(tmp_path, OTHER_MODEL, "low", lambda m, e: None)
    assert profiles.ensure(tmp_path, OTHER_MODEL, "low", fetch) == profiles.load(tmp_path, OTHER_MODEL, "low")
    assert attempts == []


def test_legacy_profiles_are_imported_together_without_fetching_or_mutating(tmp_path):
    high = seeded(tmp_path, legacy=True, provenance={"url": aa_catalog.URL, "label": "Demo High", "tier": "High", "cells": {"score": "50"}})
    low = seeded(tmp_path, OTHER_MODEL, "low", legacy=True, score=26.0)
    seeded(tmp_path, MODEL, "medium", legacy=True, confidence="invalid")
    folder = catalog_path(tmp_path).parent
    corrupt = folder / ("a" * 64 + ".json")
    corrupt.write_text("{broken", encoding="utf-8")
    wrong_name = folder / ("b" * 64 + ".json")
    wrong_name.write_text(json.dumps({**high, "effort": "extra"}), encoding="utf-8")
    directory = folder / ("c" * 64 + ".json")
    directory.mkdir()
    before = {path: path.read_bytes() for path in folder.iterdir() if path.is_file()}

    assert profiles.load(tmp_path, MODEL, "high") == high
    assert set(folder.iterdir()) == {*before, directory}  # A read before the migration is still read-only.
    calls = []
    assert profiles.ensure(tmp_path, MODEL, "high", lambda *pair: calls.append(pair)) == high
    assert calls == []
    assert catalog_at(tmp_path) == {MODEL: {"high": high}, OTHER_MODEL: {"low": low}}
    assert all(path.read_bytes() == data for path, data in before.items())
    seeded(tmp_path, legacy=True, score=99.0)  # A later write by a downgraded app is not authoritative.
    profiles.migrate(tmp_path)
    assert profiles.load(tmp_path, MODEL, "high") == high


def test_efforts_are_exact_and_never_mixed(tmp_path):
    high = seeded(tmp_path, MODEL, "high", score=80.0)
    seen = []

    def fetch(model, effort):
        seen.append(effort)
        return {"score": 31.0}

    medium = profiles.ensure(tmp_path, MODEL, "medium", fetch)
    assert seen == ["medium"] and medium["effort"] == "medium" and medium["score"] == 31.0
    assert profiles.load(tmp_path, MODEL, "high")["score"] == 80.0
    assert catalog_at(tmp_path) == {MODEL: {"high": high, "medium": medium}}


def test_nested_keys_keep_case_and_separators_exact(tmp_path):
    profiles.ensure(tmp_path, "m:e", "f", lambda m, e: {"score": 1.0})
    profiles.ensure(tmp_path, "m", "e:f", lambda m, e: {"score": 2.0})
    profiles.ensure(tmp_path, "api:a:b", "", lambda m, e: {"score": 3.0})
    profiles.ensure(tmp_path, "API:A:B", "", lambda m, e: {"score": 4.0})  # case-insensitive filesystems stay exact
    assert list(catalog_path(tmp_path).parent.glob("*.json")) == [catalog_path(tmp_path)]
    assert profiles.load(tmp_path, "m:e", "f")["score"] == 1.0
    assert profiles.load(tmp_path, "m", "e:f")["score"] == 2.0
    assert profiles.load(tmp_path, "api:a:b", "")["score"] == 3.0
    assert profiles.load(tmp_path, "API:A:B", "")["score"] == 4.0


def test_a_miss_offline_stores_a_mid_low_fallback(tmp_path):
    def fetch(model, effort):
        raise OSError("offline")

    record = profiles.ensure(tmp_path, MODEL, "low", fetch, clock=lambda: 222.0)
    assert record["confidence"] == profiles.CONFIDENCE_FALLBACK
    assert record["score"] == profiles.FALLBACK_SCORE
    assert record["taskCostUSD"] is None and record["timeMinutes"] is None
    assert record["fetchedAt"] == 222.0
    assert profiles.load(tmp_path, MODEL, "low") == record


def test_an_expired_fallback_retries_but_never_becomes_permanent(tmp_path):
    calls = []

    def offline(model, effort):
        calls.append(model)
        raise OSError("offline")

    first = profiles.ensure(tmp_path, MODEL, "low", offline, clock=lambda: 100.0)
    profiles.ensure(tmp_path, MODEL, "low", offline, clock=lambda: 100.0 + profiles.FALLBACK_RETRY_SECONDS - 1)
    assert len(calls) == 1  # Inside the window the estimate is trusted: no retry storm.
    retried = profiles.ensure(tmp_path, MODEL, "low", offline,
                              clock=lambda: 100.0 + profiles.FALLBACK_RETRY_SECONDS + 1)
    assert len(calls) == 2 and retried["confidence"] == profiles.CONFIDENCE_FALLBACK
    assert retried["fetchedAt"] == 100.0 + profiles.FALLBACK_RETRY_SECONDS + 1  # Throttle moved; still not a permanent no.
    upgraded = profiles.ensure(tmp_path, MODEL, "low", lambda m, e: {"score": 44.0},
                               clock=lambda: 100.0 + 3 * profiles.FALLBACK_RETRY_SECONDS)
    assert upgraded["confidence"] == profiles.CONFIDENCE_EXACT and upgraded["score"] == 44.0


def test_a_corrupt_or_malformed_catalog_is_a_miss_not_a_crash(tmp_path):
    seeded(tmp_path)
    catalog_path(tmp_path).write_text("{not json", encoding="utf-8")
    assert profiles.load(tmp_path, MODEL, "high") is None
    record = profiles.ensure(tmp_path, MODEL, "high", lambda m, e: {"score": 7.0})
    assert record["score"] == 7.0 and catalog_at(tmp_path) == {MODEL: {"high": record}}


def test_load_rejects_wrong_keys_and_non_finite_fields(tmp_path):
    path = catalog_path(tmp_path)
    base = seeded(tmp_path)
    for broken in ({**base, "model": OTHER_MODEL}, {**base, "effort": "low"}, {**base, "confidence": "sure"},
                   {**base, "fetchedAt": "yesterday"}, {**base, "score": float("inf")},
                   {**base, "taskCostUSD": float("nan")}):
        path.write_text(json.dumps({"models": {MODEL: {"high": broken}}}), encoding="utf-8")
        assert profiles.load(tmp_path, MODEL, "high") is None, broken


def test_a_failed_replace_leaves_the_previous_catalog_intact(tmp_path, monkeypatch):
    seeded(tmp_path)
    path = catalog_path(tmp_path)
    before = path.read_bytes()

    def failed_replace(*args):
        raise OSError("simulated write failure")

    with monkeypatch.context() as patch:
        patch.setattr(profiles.os, "replace", failed_replace)
        with pytest.raises(OSError, match="simulated write failure"):
            profiles.ensure(tmp_path, OTHER_MODEL, "low", lambda *_: {"score": 42.0})
    assert path.read_bytes() == before
    assert not list(path.parent.glob("*.tmp"))


def test_non_finite_offers_are_dropped(tmp_path):
    record = profiles.ensure(tmp_path, MODEL, "high", lambda m, e: {"score": float("nan"), "taskCostUSD": float("inf")})
    assert record["confidence"] == profiles.CONFIDENCE_EXACT and record["score"] is None and record["taskCostUSD"] is None


def test_concurrent_ensure_loses_nothing_and_fetches_once_per_key(tmp_path):
    keys = [(f"api:demo:m{i}", effort) for i in range(8) for effort in ("low", "high")]
    calls = []
    guard = threading.Lock()

    def fetch(model, effort):
        time.sleep(0.001)
        with guard:
            calls.append((model, effort))
        return {"score": len(model) + len(effort)}

    with ThreadPoolExecutor(max_workers=8) as pool:
        records = list(pool.map(lambda key: profiles.ensure(tmp_path, key[0], key[1], fetch), keys))
        list(pool.map(lambda key: profiles.ensure(tmp_path, key[0], key[1], fetch), keys))
    assert sorted(calls) == sorted(keys)
    assert list(catalog_path(tmp_path).parent.glob("*.json")) == [catalog_path(tmp_path)]
    stored = catalog_at(tmp_path)
    assert sum(len(efforts) for efforts in stored.values()) == len(keys)
    for record, (model, effort) in zip(records, keys):
        assert record["confidence"] == profiles.CONFIDENCE_EXACT
        assert stored[model][effort] == record
        assert profiles.load(tmp_path, model, effort) == record


def test_distinct_processes_merge_into_the_latest_catalog(tmp_path):
    worker = """
from pathlib import Path
import sys, time
from desktop.core.fusion import profiles

root, model, other = Path(sys.argv[1]), sys.argv[2], sys.argv[3]

def fetch(*pair):
    (root / model).touch()
    deadline = time.monotonic() + 10
    while not (root / other).exists():
        if time.monotonic() >= deadline:
            return None
        time.sleep(0.01)
    return {"score": 42}

write = profiles._write

def slow_write(path, models):
    time.sleep(0.2)  # Widen the window between reading the catalog and replacing it.
    write(path, models)

profiles._write = slow_write
assert profiles.ensure(root, model, "high", fetch)["score"] == 42
"""

    def run(pair):
        return subprocess.run([sys.executable, "-c", worker, str(tmp_path), *pair], capture_output=True, text=True, timeout=30)

    with ThreadPoolExecutor(max_workers=2) as pool:
        for completed in pool.map(run, [("first", "second"), ("second", "first")]):
            assert completed.returncode == 0, completed.stderr
    assert {model: levels["high"]["score"] for model, levels in catalog_at(tmp_path).items()} == {"first": 42, "second": 42}


def test_an_unknown_task_cost_stays_unknown(tmp_path):
    record = profiles.ensure(tmp_path, MODEL, "high", lambda m, e: {"score": 62.0})
    assert record["score"] == 62.0 and record["taskCostUSD"] is None
    assert "task cost unknown" in profiles.team_snapshot(tmp_path, {"main": (MODEL, "high")})


def test_team_snapshot_reads_only_local_records(tmp_path):
    seeded(tmp_path, MODEL, "high", score=51.5, taskCostUSD=1.75, timeMinutes=12.4, costIn=None, costOut=None)
    seeded(tmp_path, OTHER_MODEL, "low", confidence=profiles.CONFIDENCE_FALLBACK, source=profiles.CONFIDENCE_FALLBACK,
           score=profiles.FALLBACK_SCORE, taskCostUSD=None, timeMinutes=None, costIn=None, costOut=None)
    before = catalog_path(tmp_path).read_bytes()
    snapshot = profiles.team_snapshot(tmp_path, {"main": (MODEL, "high"), "worker": (OTHER_MODEL, "low"),
                                                 "light": ("devin:demo-tiny", "low")})
    assert f"main: {MODEL} @ high — capability 51.5, task cost $1.75 per task, ~12.4 min per task, confidence exact" in snapshot
    assert f"worker: {OTHER_MODEL} @ low — capability {profiles.FALLBACK_SCORE:g}, task cost unknown, confidence fallback" in snapshot
    assert "light: devin:demo-tiny @ low — no local profile" in snapshot
    assert list(catalog_path(tmp_path).parent.iterdir()) == [catalog_path(tmp_path)]
    assert catalog_path(tmp_path).read_bytes() == before  # Read-only: nothing written.


def test_ids_and_seats_are_validated(tmp_path):
    with pytest.raises(ValueError):
        profiles.ensure(tmp_path, " ", "high", lambda m, e: None)
    with pytest.raises(ValueError):
        profiles.ensure(tmp_path, MODEL, "x" * 65, lambda m, e: None)
    with pytest.raises(ValueError):
        profiles.load(tmp_path, 5, "high")
    with pytest.raises(ValueError):
        profiles.team_snapshot(tmp_path, {"main": (MODEL,)})
    with pytest.raises(ValueError):
        profiles.team_snapshot(tmp_path, {"main": ("", "high")})


# --------------------------------------------------------------------------- the leaderboard fetch

def test_parse_reads_the_leaderboard_table():
    rows = {(entry.name, entry.effort): entry for entry in entries()}
    sol = rows[("gpt61sol", "max")]  # "GPT-6.1-Sol Max": the effort is a suffix of the name.
    assert sol.score == 51.5 and sol.task_cost == 1.75 and sol.minutes == 12.4
    assert sol.label == "GPT-6.1-Sol Max" and sol.tier == "Max"  # The row's own evidence, kept raw.
    assert sol.cells == {"score": "51.5", "taskCostUSD": "$1.75", "timeMinutes": "12.4"}
    assert rows[("gpt6luna", "high")].score == 54.2 and rows[("gpt6luna", "high")].task_cost == 2.3
    assert rows[("gpt6luna", "low")].score == 48.0
    claude = rows[("claudedemo52", "max")]  # "Claude Demo 5.2 Max, Default Fallback"
    assert claude.score == 65.0 and claude.task_cost == 26.54 and claude.tier == "Max"
    assert rows[("minichat", None)].score == 20.1       # (Chat) is a variant name, not an effort.
    assert rows[("minichat", None)].tier == ""          # No declared tier: nothing is invented.
    assert rows[("nemotron3ultra", "reasoning")].score == 33.0
    assert all(entry.name and entry.name != "model" for entry in entries())  # Neither headers nor "—" are rows.


def test_parse_finds_the_model_column_by_header_not_position():
    assert aa_catalog.parse(LEADERBOARD_SHIFTED) == [
        aa_catalog.Entry(name="soloranker", effort="high", score=33.0, task_cost=0.5, minutes=4.0,
                         label="Solo Ranker High", tier="High",
                         cells={"score": "33.0", "taskCostUSD": "$0.50", "timeMinutes": "4.0"})]


def test_offer_is_exact_and_falls_back_on_ambiguity():
    rows = entries()
    single = aa_catalog.offer(rows, "chatgpt:gpt-6.1-sol", "max")
    assert single == {"source": "artificialanalysis", "score": 51.5, "taskCostUSD": 1.75, "timeMinutes": 12.4,
                      "provenance": {"url": aa_catalog.URL, "label": "GPT-6.1-Sol Max", "tier": "Max",
                                     "cells": {"score": "51.5", "taskCostUSD": "$1.75", "timeMinutes": "12.4"}}}
    assert aa_catalog.offer(rows, "devin:gpt-6-luna", "high")["score"] == 54.2
    assert aa_catalog.offer(rows, "api:demo:gpt-6-luna", "low")["score"] == 48.0
    assert aa_catalog.offer(rows, "chatgpt:gpt-6-luna", "medium") is None  # No medium row: tiers are not mixed.
    assert aa_catalog.offer(rows, "chatgpt:claude-demo-5.2", "max")["score"] == 65.0  # ", Default Fallback"
    assert aa_catalog.offer(rows, "chatgpt:nemotron-3-ultra", "reasoning")["score"] == 33.0
    assert aa_catalog.offer(rows, "chatgpt:duo", "high") is None          # Two identical names: ambiguous.
    assert aa_catalog.offer(rows, "chatgpt:unlisted", "high") is None
    assert aa_catalog.offer(rows, "chatgpt:gpt-6-luna", "") is None       # Only annotated rows: ambiguous for a bare ask.
    assert aa_catalog.offer(rows, "chatgpt:mini", "high") is None         # "Mini (Chat)" is a different model.
    assert aa_catalog.offer(rows, "chatgpt:minichat", "high") is None     # An unannotated row is not a high measurement.
    plain = aa_catalog.offer(rows, "chatgpt:minichat", "")
    assert plain is not None and plain["score"] == 20.1                   # ...but it is honest data for a bare ask.
    assert aa_catalog.offer(rows, "chatgpt:duo", "") is None             # Two unannotated twins are ambiguous too.


def test_unique_max_supplies_only_a_score_when_the_requested_score_is_missing():
    model = "chatgpt:gpt-6.1-sol"
    rows = entries()
    reference = aa_catalog.offer(rows, model, "high")
    assert reference == {"source": profiles.SOURCE, "score": 51.5, "scoreProvenance": {
        "url": aa_catalog.URL, "label": "GPT-6.1-Sol Max", "tier": "Max", "cells": {"score": "51.5"}}}
    assert aa_catalog.offer(rows, model, "") == reference
    exact = aa_catalog.Entry("gpt61sol", "high", 43.0, 0.5, 4.0, label="GPT-6.1-Sol High", tier="High")
    result = aa_catalog.offer([*rows, exact], model, "high")
    assert result["score"] == 43.0 and result["taskCostUSD"] == 0.5 and "scoreProvenance" not in result
    unscored = aa_catalog.Entry("gpt61sol", "high", None, 0.5, 4.0, label="GPT-6.1-Sol High", tier="High")
    result = aa_catalog.offer([*rows, unscored], model, "high")
    assert result["score"] == 51.5 and result["taskCostUSD"] == 0.5 and result["timeMinutes"] == 4.0
    assert result["provenance"]["tier"] == "High" and result["scoreProvenance"]["tier"] == "Max"
    maximum = next(row for row in rows if row.name == "gpt61sol" and row.effort == "max")
    assert aa_catalog.offer([*rows, maximum], model, "high") is None  # Ambiguous Max is not evidence.
    assert aa_catalog.offer([aa_catalog.Entry("gpt61sol", "max", None, 1.75, 12.4)], model, "high") is None
    assert aa_catalog.offer([aa_catalog.Entry("gpt61sol", "xhigh", 50.0, 1.75, 12.4)], model, "high") is None
    assert aa_catalog.offer(rows, "chatgpt:gpt-6.1-sol-mini", "high") is None  # Never another model variant.


def test_native_id_unwraps_subscription_api_and_channel_ids():
    assert aa_catalog.native_id("chatgpt:gpt-6.1-sol") == "gpt-6.1-sol"
    assert aa_catalog.native_id("devin:swe-2-high") == "swe-2-high"
    assert aa_catalog.native_id("api:demo:llama-4") == "llama-4"
    assert aa_catalog.native_id("api:demo:custom:my gpt 5.2") == "my gpt 5.2"
    assert aa_catalog.native_id("weird") == "weird"


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
    assert asked == [(aa_catalog.URL, aa_catalog.TIMEOUT_SECONDS)]
    with pytest.raises(ValueError):
        aa_catalog.download("http://artificialanalysis.ai/leaderboards/models", opener=opener)
    with pytest.raises(ValueError):
        aa_catalog.download("https://example.com/private.json", opener=opener)
    with pytest.raises(ValueError):
        aa_catalog.download(opener=lambda r, timeout: FakeResponse(b"x" * (aa_catalog.MAX_BYTES + 1)))
    assert asked == [(aa_catalog.URL, aa_catalog.TIMEOUT_SECONDS)]  # No second request went out.


def test_the_default_opener_cannot_be_redirected_off_the_host(monkeypatch):
    asked = []

    def fake_open(request, timeout):
        asked.append((request.full_url, timeout))
        return FakeResponse(b"<html></html>")

    monkeypatch.setattr(aa_catalog, "_urlopen", fake_open)
    assert aa_catalog.download() == "<html></html>"
    assert asked == [(aa_catalog.URL, aa_catalog.TIMEOUT_SECONDS)]  # The default path goes through _urlopen.

    handler = aa_catalog._SameHostRedirect()
    req = urllib.request.Request(aa_catalog.URL)
    same = handler.redirect_request(req, None, 302, "", {}, "https://artificialanalysis.ai/other/path")
    assert same is not None and same.full_url == "https://artificialanalysis.ai/other/path"  # Same host is followed.
    assert handler.redirect_request(req, None, 302, "", {}, "https://other.example/x") is None
    assert handler.redirect_request(req, None, 302, "", {}, "http://artificialanalysis.ai/x") is None


def test_source_downloads_once_and_stores_leaderboard_fields(tmp_path):
    downloads = []

    def loader():
        downloads.append(1)
        return LEADERBOARD

    fetch = aa_catalog.source(loader=loader)
    one = profiles.ensure(tmp_path, "chatgpt:gpt-6.1-sol", "max", fetch)
    two = profiles.ensure(tmp_path, "devin:gpt-6-luna", "low", fetch)
    assert downloads == [1]
    assert one["score"] == 51.5 and one["taskCostUSD"] == 1.75 and one["timeMinutes"] == 12.4
    assert one["confidence"] == profiles.CONFIDENCE_EXACT and one["source"] == profiles.SOURCE
    assert two["score"] == 48.0 and two["confidence"] == profiles.CONFIDENCE_EXACT
    assert profiles.load(tmp_path, "chatgpt:gpt-6.1-sol", "max") == one


def test_old_twenty_point_estimate_rechecks_immediately_and_stores_the_max_ceiling(tmp_path):
    model = "chatgpt:gpt-6.1-sol"
    seeded(tmp_path, model, "high", confidence="fallback", source="fallback", score=20.0,
           costIn=None, costOut=None, taskCostUSD=None, timeMinutes=None, fetchedAt=100.0)
    fetch = aa_catalog.source(loader=lambda: LEADERBOARD)
    record = profiles.ensure(tmp_path, model, "high", fetch, clock=lambda: 101.0)
    assert profiles.load(tmp_path, model, "high") == record
    assert record["effort"] == "high" and record["score"] == 51.5 and record["confidence"] == "max_reference"
    assert record["scoreProvenance"]["label"] == "GPT-6.1-Sol Max" and "provenance" not in record
    assert all(record[field] is None for field in ("costIn", "costOut", "taskCostUSD", "timeMinutes"))
    snapshot = profiles.team_snapshot(tmp_path, {"main": (model, "high")})
    assert "@ high — capability 51.5 (Max capability ceiling; not measured at this effort), task cost unknown" in snapshot

    asked = []

    def unscored(*pair):
        asked.append(pair)
        return {"score": None, "taskCostUSD": 0.5}

    assert profiles.ensure(tmp_path, model, "high", unscored, clock=lambda: 102.0) == record
    assert not asked
    after_retry = 101.0 + profiles.FALLBACK_RETRY_SECONDS
    retained = profiles.ensure(tmp_path, model, "high", unscored, clock=lambda: after_retry)
    assert retained == {**record, "fetchedAt": after_retry, "taskCostUSD": 0.5} and asked == [(model, "high")]
    upgraded = profiles.ensure(tmp_path, model, "high", lambda *_: {"score": 43.0, "taskCostUSD": 0.5},
                               clock=lambda: after_retry + profiles.FALLBACK_RETRY_SECONDS)
    assert upgraded["confidence"] == "exact" and upgraded["score"] == 43.0 and upgraded["taskCostUSD"] == 0.5
    assert "scoreProvenance" not in upgraded


def test_the_record_keeps_the_leaderboard_row_it_came_from(tmp_path):
    fetch = aa_catalog.source(loader=lambda: LEADERBOARD)
    stored = profiles.ensure(tmp_path, "chatgpt:gpt-6.1-sol", "max", fetch)
    record = profiles.load(tmp_path, "chatgpt:gpt-6.1-sol", "max")
    assert record == stored  # The evidence survives the JSON round trip unchanged.
    assert record["provenance"] == {"url": aa_catalog.URL, "label": "GPT-6.1-Sol Max", "tier": "Max",
                                    "cells": {"score": "51.5", "taskCostUSD": "$1.75", "timeMinutes": "12.4"}}
    assert record["fetchedAt"] > 0

    fallback = profiles.ensure(tmp_path, "chatgpt:unlisted", "high", fetch)
    assert fallback["confidence"] == profiles.CONFIDENCE_FALLBACK and "provenance" not in fallback

    broken = profiles.ensure(tmp_path, "devin:odd", "high",
                             lambda m, e: {"score": 5.0, "provenance": {"url": 7, "cells": "not-a-map"}})
    assert broken["score"] == 5.0 and "provenance" not in broken  # Malformed evidence is dropped, not stored.


# --------------------------------------------------------------------------- the app's call sites

def _recording_source(calls, delay=None):
    def fetch(model, effort):
        calls.append((model, effort))
        if delay is not None:
            delay.wait(30)
        return {"score": 42.0, "taskCostUSD": 1.5, "timeMinutes": 3.0}

    return fetch


def test_startup_imports_profiles_without_touching_the_api_models_file(tmp_path):
    async def go():
        root = default_settings_path().parent
        original = seeded(root, legacy=True)
        providers = root / "models.json"
        providers.write_text('{"customModels": []}\n', encoding="utf-8")
        before = providers.read_bytes()
        core, _, _, _, _ = await ready_core(tmp_path)
        try:
            await until(lambda: catalog_path(root).is_file())
            assert catalog_at(root) == {MODEL: {"high": original}}
            assert providers.read_bytes() == before
            assert legacy_path(root, MODEL, "high").is_file()
        finally:
            await core.shutdown()
    asyncio.run(go())


def test_choosing_a_default_model_prefetches_its_profile(tmp_path):
    async def go():
        core, _, _, _, _ = await ready_core(tmp_path)
        try:
            calls = []
            core._profiles_source = _recording_source(calls)
            assert (await core.call("session.choose", {"field": "model", "value": OTHER_MODEL})) == {"ok": True}
            effort = core.settings.reasoning_effort
            await until(lambda: profiles.load(default_settings_path().parent, OTHER_MODEL, effort) is not None)
            record = profiles.load(default_settings_path().parent, OTHER_MODEL, effort)
            assert record["confidence"] == profiles.CONFIDENCE_EXACT and record["taskCostUSD"] == 1.5
            await core.call("session.choose", {"field": "model", "value": OTHER_MODEL})
            await asyncio.sleep(0.05)
            assert calls == [(OTHER_MODEL, effort)]  # A repeated selection does not repeat the request.
        finally:
            await core.shutdown()
    asyncio.run(go())


def test_changing_the_reasoning_level_prefetches_that_tier(tmp_path):
    async def go():
        core, _, _, _, _ = await ready_core(tmp_path)
        try:
            calls = []
            core._profiles_source = _recording_source(calls)
            assert (await core.call("session.choose", {"field": "reasoning", "value": "high"})) == {"ok": True}
            await until(lambda: profiles.load(default_settings_path().parent, MODEL, "high") is not None)
            assert calls == [(MODEL, "high")]
        finally:
            await core.shutdown()
    asyncio.run(go())


def test_a_ready_session_prefetches_its_connected_model(tmp_path):
    async def go():
        core, _, events, _, _ = await ready_core(tmp_path)
        try:
            calls = []
            core._profiles_source = _recording_source(calls)
            cid = (await core.call("session.new", {"projectId": str(tmp_path / "proj")}))["id"]
            await until(lambda: profiles.load(default_settings_path().parent, MODEL, "medium") is not None)
            assert calls == [(MODEL, "medium")]
        finally:
            await core.shutdown()
    asyncio.run(go())


def test_selection_never_waits_on_the_fetch(tmp_path):
    async def go():
        core, _, _, _, _ = await ready_core(tmp_path)
        try:
            calls = []
            gate = threading.Event()
            core._profiles_source = _recording_source(calls, delay=gate)
            assert (await core.call("session.choose", {"field": "model", "value": OTHER_MODEL})) == {"ok": True}
            effort = core.settings.reasoning_effort
            assert profiles.load(default_settings_path().parent, OTHER_MODEL, effort) is None  # Still fetching.
            gate.set()
            await until(lambda: profiles.load(default_settings_path().parent, OTHER_MODEL, effort) is not None)
        finally:
            gate.set()
            await core.shutdown()
    asyncio.run(go())


def test_fusion_seats_prefetch_on_settings_and_ready(tmp_path):
    async def go():
        core, _, _, _, _ = await ready_core(tmp_path)
        try:
            calls = []
            core._profiles_source = _recording_source(calls)
            await core.call("settings.set", {"key": "fusionMain", "value": OTHER_MODEL})
            await core.call("settings.set", {"key": "fusionReasoning", "value": {"main:" + OTHER_MODEL: "low"}})
            root = default_settings_path().parent
            await until(lambda: profiles.load(root, OTHER_MODEL, "low") is not None)
            assert (OTHER_MODEL, "low") in calls
        finally:
            await core.shutdown()
    asyncio.run(go())
