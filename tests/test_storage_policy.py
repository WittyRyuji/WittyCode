from __future__ import annotations

from desktop.core import storage_policy
from desktop.core.settings import AppSettings, load_settings, save_settings, settings_from_dict
from desktop.core.storage_policy import (
    CATEGORIES,
    DEFAULT_SHARES,
    StoragePolicy,
    check_write,
    policy_from,
    report,
    shares_from,
)


def policy(**over) -> StoragePolicy:
    base = {"max_bytes": 1000, "shares": dict(DEFAULT_SHARES), "alert_at": 80, "full_policy": "stop", "autoclean": ("cache", "index")}
    base.update(over)
    return StoragePolicy(**base)


def full_usage(cache=0, history=0, index=0, dump=0):
    return {"history": history, "dump": dump, "cache": cache, "index": index}


def test_storage_settings_persist(tmp_path):
    settings = AppSettings(
        storage_max_bytes=4 * 1024**3,
        storage_shares={"history": 50.0, "dump": 20.0, "cache": 20.0, "index": 10.0},
        storage_alert_at=70,
        storage_full_policy="autoclean",
        storage_autoclean=["cache", "index"],
    )
    path = tmp_path / "settings.json"
    save_settings(settings, path)
    loaded = load_settings(path)
    assert loaded == settings
    assert loaded.storage_shares == {"history": 50.0, "dump": 20.0, "cache": 20.0, "index": 10.0}


def test_storage_settings_fall_back_field_by_field():
    settings = settings_from_dict({
        "storage_max_bytes": -5,
        "storage_shares": {"history": 90.0, "dump": 10.0},  # missing categories
        "storage_alert_at": 3,
        "storage_full_policy": "wipe",
        "storage_autoclean": ["history", "nope"],
    })
    assert settings.storage_max_bytes == storage_policy.DEFAULT_MAX_BYTES
    assert settings.storage_shares == dict(DEFAULT_SHARES)
    assert settings.storage_alert_at == storage_policy.DEFAULT_ALERT_AT
    assert settings.storage_full_policy == "stop"
    assert settings.storage_autoclean == ["history"], "authorised picks survive; made-up categories drop"


def test_shares_must_total_100():
    assert shares_from({"history": 60, "dump": 15, "cache": 15, "index": 10}) is not None
    assert shares_from({"history": 60, "dump": 15, "cache": 15, "index": 11}) is None
    assert shares_from({"history": 59.5, "dump": 15, "cache": 15, "index": 10}) is None, "99.5% is not 100%"
    assert shares_from({"history": 33.4, "dump": 33.3, "cache": 33.3, "index": 0}) is not None
    assert shares_from({"history": 99, "dump": 0, "cache": 0, "index": 1}) is not None, "zeros are legal shares"
    assert shares_from({"history": 101, "dump": 0, "cache": -1, "index": 0}) is None
    assert shares_from({"history": "60", "dump": 15, "cache": 15, "index": 10}) is None
    assert shares_from({}) is None


def test_shares_total_is_an_exact_decimal_not_a_tolerance():
    """Splits the user writes are compared as decimals: 33.333×3+0.001 is 100; 99.999999% is not."""
    assert shares_from({"history": 33.333, "dump": 33.333, "cache": 33.333, "index": 0.001}) is not None
    assert shares_from({"history": 33.3, "dump": 33.3, "cache": 33.3, "index": 0.1}) is not None
    assert shares_from({"history": 0.3, "dump": 0.3, "cache": 0.3, "index": 99.1}) is not None
    assert shares_from({"history": 59.999999, "dump": 15, "cache": 15, "index": 10}) is None
    assert shares_from({"history": 0.1, "dump": 0.1, "cache": 0.1, "index": 99.6}) is None


def test_category_budget_never_borrows():
    p = policy()
    # cache's share of 1000 is 150: it is refused at 151 even while the other 850 bytes sit idle.
    verdict = check_write(p, full_usage(cache=140), "cache", 11)
    assert not verdict["allowed"] and verdict["reason"] == "over_category"
    verdict = check_write(p, full_usage(cache=140), "cache", 10)
    assert verdict["allowed"] and verdict["reason"] == "ok"


def test_global_cap_refuses_on_top_of_shares():
    p = policy()
    # history already sits over its (since-shrunk) share; a write inside cache's own budget still hits the cap.
    verdict = check_write(p, full_usage(history=950), "cache", 60)
    assert not verdict["allowed"] and verdict["reason"] == "over_global"


def test_unknown_usage_fails_closed():
    p = policy()
    verdict = check_write(p, {"history": None, "dump": 0, "cache": 0, "index": 0}, "cache", 1)
    assert not verdict["allowed"] and verdict["reason"] == "usage_unknown"
    verdict = check_write(p, {"history": 0, "dump": 0, "cache": 0}, "cache", 1)  # index not reported at all
    assert not verdict["allowed"] and verdict["reason"] == "usage_unknown"


def test_unknown_category_and_bad_size_refuse():
    p = policy()
    assert check_write(p, full_usage(), "notes", 1)["reason"] == "unknown_category"
    assert check_write(p, full_usage(), "cache", -1)["reason"] == "unknown_category"


def test_budgets_never_exceed_the_cap():
    p = policy(max_bytes=101, shares={"history": 33.4, "dump": 33.3, "cache": 33.3, "index": 0.0})
    assert sum(p.budget(name) for name in CATEGORIES) <= p.max_bytes
    assert p.budget("index") == 0
    assert not check_write(p, full_usage(), "index", 1)["allowed"], "a zero share admits nothing"


def test_report_shows_allocation_against_real_occupancy():
    p = policy()
    out = report(p, full_usage(cache=200, history=100))
    rows = {row["id"]: row for row in out["categories"]}
    assert rows["cache"]["budget"] == 150 and rows["cache"]["used"] == 200 and rows["cache"]["over"]
    assert rows["history"]["budget"] == 600 and rows["history"]["used"] == 100
    assert out["total"] == {"used": 300, "known": True, "alerted": False}


def test_report_never_draws_unknown_as_zero():
    p = policy()
    out = report(p, {"history": None, "dump": 5, "cache": 0, "index": 0})
    rows = {row["id"]: row for row in out["categories"]}
    assert rows["history"]["used"] is None and not rows["history"]["known"]
    assert out["total"]["used"] is None and not out["total"]["known"] and not out["total"]["alerted"]


def test_alert_threshold_is_over_the_total_not_the_split():
    p = policy(alert_at=30)
    assert report(p, full_usage(history=300))["total"]["alerted"]
    assert check_write(p, full_usage(history=250), "history", 60)["overAlert"]
    assert not check_write(p, full_usage(history=200), "history", 60)["overAlert"]


def test_full_without_authorised_objects_still_refuses():
    p = policy(full_policy="autoclean")  # only cache/index authorised; both are empty here
    verdict = check_write(p, full_usage(history=600), "history", 1)
    assert not verdict["allowed"] and verdict["reason"] == "over_category"
    assert verdict["reclaimable"] == [], "history and dump are not touched unless the user picked them"
    # With authorised bytes present the caller gets the categories a clean pass may free.
    verdict = check_write(p, full_usage(history=600, cache=10), "history", 1)
    assert verdict["reclaimable"] == ["cache"]
    p = policy(full_policy="autoclean", autoclean=("history",))
    verdict = check_write(p, full_usage(history=600), "history", 1)
    assert verdict["reclaimable"] == ["history"], "explicit authorisation makes history eligible"


def test_stop_policy_names_no_reclaimable_categories():
    verdict = check_write(policy(full_policy="stop"), full_usage(cache=140), "cache", 11)
    assert verdict["reclaimable"] == []


def test_policy_from_settings_shape():
    raw = {"storage_max_bytes": 512 * 1024**2, "storage_shares": {"history": 40, "dump": 30, "cache": 20, "index": 10},
           "storage_alert_at": 90, "storage_full_policy": "autoclean", "storage_autoclean": ["index", "junk", "dump"]}
    p = policy_from(raw)
    assert p.max_bytes == 512 * 1024**2 and p.alert_at == 90 and p.full_policy == "autoclean"
    assert p.autoclean == ("dump", "index"), "canonical category order, unknown names dropped"
    assert p.shares["dump"] == 30.0
    assert policy_from(None) == StoragePolicy()


def test_storage_rpc_and_capacity_gate(tmp_path):
    """The page's RPC: the view carries real occupancy where the application measures it, edits are
    validated, and the archive layer's gate fails closed while any category is unmeasured."""
    import asyncio

    import pytest

    from desktop.core.app_core import AppError
    from desktop.core.cache import Cache
    from tests.app_support import make_core

    cache_dir = tmp_path / "cache"
    (cache_dir / "sessions").mkdir(parents=True)
    (cache_dir / "index.json").write_bytes(b"x" * 64)
    (cache_dir / "sessions" / "a.json").write_bytes(b"x" * 36)
    core, service, *_ = make_core(tmp_path, plan=False, cache=Cache(cache_dir))
    try:
        view = asyncio.run(core.call("storage.get", {}))
        rows = {c["id"]: c for c in view["categories"]}
        assert rows["cache"]["used"] == 100 and rows["cache"]["known"], "the app's own cache folder is measured"
        assert rows["index"]["used"] == 0 and rows["index"]["known"], "no archive index or memory yet: a known empty"
        assert rows["history"]["used"] == 0 and rows["history"]["known"]
        assert rows["dump"]["used"] == 0 and rows["dump"]["known"]
        assert view["total"]["used"] == 100 and view["total"]["known"]
        managed = view["managed"]["archive"]
        assert managed["root"].endswith("session") and managed["unmetered"] == []
        assert managed["cleanup"]["history"] and managed["cleanup"]["index"], "the cleanup scope is stated, not implied"

        verdict = core.check_capacity("cache", 1)
        assert verdict["allowed"] and verdict["reason"] == "ok", "fully metered usage no longer blocks the gate"

        # A real archive write shows up as real occupancy on the next view — evidence, not a demo.
        from desktop.core.session_archive import SessionArchive

        archive = SessionArchive.create(core.session_archive_root(), meter=core.archive_meter())
        view = asyncio.run(core.call("storage.get", {}))
        rows = {c["id"]: c for c in view["categories"]}
        manifest = (core.session_archive_root() / archive.sid / "manifest.json").stat().st_size
        assert rows["history"]["used"] == manifest and rows["history"]["known"]
        assert rows["index"]["used"] > 0, "the archive index, WAL sidecars and ledger are counted"
        managed = view["managed"]["archive"]
        assert managed["ledger"] and managed["boundary"] is not None and managed["unmetered"] == []

        # Over the user's configured cap the gate refuses — live policy, no fake defaults.
        core.settings.storage_max_bytes = 1
        verdict = core.check_capacity("history", 1)
        assert not verdict["allowed"] and verdict["reclaimable"] == []

        view = asyncio.run(core.call("storage.set", {"key": "alertAt", "value": 55}))
        assert view["alertAt"] == 55 and core.settings.storage_alert_at == 55
        view = asyncio.run(core.call("storage.set", {"key": "shares", "value": {"history": 50, "dump": 20, "cache": 20, "index": 10}}))
        assert view["shares"]["dump"] == 20.0
        with pytest.raises(AppError):
            asyncio.run(core.call("storage.set", {"key": "shares", "value": {"history": 99, "dump": 0, "cache": 0, "index": 0}}))
        view = asyncio.run(core.call("storage.set", {"key": "fullPolicy", "value": "autoclean"}))
        assert view["fullPolicy"] == "autoclean"
        view = asyncio.run(core.call("storage.set", {"key": "autoclean", "value": ["cache", "junk"]}))
        assert view["autoclean"] == ["cache"], "unknown categories are dropped, not authorised"
        with pytest.raises(AppError):
            asyncio.run(core.call("storage.set", {"key": "autoclean", "value": "cache"}))
        with pytest.raises(AppError):
            asyncio.run(core.call("storage.set", {"key": "maxBytes", "value": 12}))
    finally:
        asyncio.run(core.shutdown())
