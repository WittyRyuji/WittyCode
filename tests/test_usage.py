"""The conversation's token total: ``Tokens`` and the ledger that sums sessions, resumes and engines."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from desktop.core.usage import Tokens, UsageLedger


def tokens(i=0, o=0, w=0, r=0) -> Tokens:
    return Tokens(i, o, w, r)


def test_tokens_read_what_each_engine_reports():
    attributes = SimpleNamespace(input_tokens=5, output_tokens=6, cache_creation_tokens=7, cache_read_tokens=8, thinking_tokens=1)
    assert Tokens.of(attributes) == Tokens(5, 6, 7, 8)
    no_writes = SimpleNamespace(input_tokens=1, output_tokens=2, cache_creation_tokens=None, cache_read_tokens=3)
    assert Tokens.of(no_writes) == Tokens(1, 2, None, 3), "an engine without cache writes: None, not 0"
    assert Tokens.of({"inputTokens": 1, "outputTokens": 2, "cacheCreationTokens": 3, "cacheReadTokens": 4}) == Tokens(1, 2, 3, 4), "a notification"
    assert Tokens.of({"input": 1, "output": 2, "cacheWrite": None, "cacheRead": 4}) == Tokens(1, 2, None, 4), "the page / cache JSON"
    assert Tokens.of(SimpleNamespace(input_tokens=9, output_tokens=1, cache_creation_tokens=0, cache_read_tokens=2)) == Tokens(9, 1, 0, 2)
    for nothing in (None, {}, {"inputTokens": "many"}, {"inputTokens": -3}, SimpleNamespace(), True):
        assert Tokens.of(nothing) is None, nothing


def test_the_page_json_is_what_usage_json_sends():
    assert Tokens(1, 2, None, 4).to_json() == {"input": 1, "output": 2, "cacheWrite": None, "cacheRead": 4}
    assert Tokens.of(Tokens(1, 2, 3, 4).to_json()) == Tokens(1, 2, 3, 4)


def test_total_counts_every_kind_of_token_once():
    assert Tokens(1, 2, 3, 4).total == 10
    assert Tokens(1, 2, None, 4).total == 7


def test_peak_is_the_larger_of_each_counter():
    assert tokens(5, 1, 0, 9).peak(tokens(3, 4, 0, 2)) == tokens(5, 4, 0, 9)
    assert Tokens(1, 1, None, 1).peak(Tokens(1, 1, None, 1)).cache_creation_tokens is None
    assert Tokens(1, 1, None, 1).peak(Tokens(1, 1, 5, 1)).cache_creation_tokens == 5
    assert tokens(1).peak(None) == tokens(1)


def test_since_is_what_was_added_and_none_when_a_counter_went_down():
    assert tokens(10, 20, 5, 100).since(tokens(4, 5, 5, 40)) == tokens(6, 15, 0, 60)
    assert tokens(3).since(tokens(4)) is None
    assert tokens(0, 0, 1, 0).since(tokens(0, 0, 2, 0)) is None, "cache writes count too"
    assert Tokens(5, 5, None, 5).since(Tokens(1, 1, None, 1)) == Tokens(4, 4, None, 4)


def test_a_series_adds_only_what_is_new_each_time():
    ledger = UsageLedger()
    assert ledger.feed({"self": tokens(100, 10)}) is True
    assert ledger.total == tokens(100, 10)
    assert ledger.feed({"self": tokens(100, 10)}) is False, "the same figure again adds nothing"
    ledger.feed({"self": tokens(150, 40)})
    assert ledger.total == tokens(150, 40), "100 + 50, 10 + 30"


def test_a_session_that_goes_on_after_a_restart_adds_only_the_part_after_the_cache():
    """The cache says: total 150/40, the engine had counted 150/40. The engine (resumed) now says 180/50."""
    ledger = UsageLedger(tokens(150, 40), {"self": tokens(150, 40)})
    ledger.feed({"self": tokens(180, 50)})
    assert ledger.total == tokens(180, 50)


def test_use_elsewhere_since_the_cache_is_added_too():
    """The cached copy is old: the session was used in the engine's own app meanwhile (the engine's total includes that)."""
    ledger = UsageLedger(tokens(100, 10), {"self": tokens(100, 10)})
    ledger.feed({"self": tokens(900, 90)})
    assert ledger.total == tokens(900, 90)


def test_a_session_nobody_cached_starts_from_what_the_engine_says():
    ledger = UsageLedger()
    ledger.feed({"self": tokens(700, 70, 5, 7000)})
    assert ledger.total == tokens(700, 70, 5, 7000)


def test_an_engine_change_keeps_the_total_and_counts_the_new_session_from_its_own_zero():
    ledger = UsageLedger()
    ledger.feed({"self": tokens(1000, 100, 0, 5000)})  # the own loop
    ledger.restart()
    ledger.feed({"self": Tokens(300, 30, None, 200)})  # Devin, a new session
    assert ledger.total == Tokens(1300, 130, 0, 5200)
    ledger.feed({"self": Tokens(400, 50, None, 900)})
    assert ledger.total == Tokens(1400, 150, 0, 5900)


def test_a_counter_that_went_down_is_a_series_starting_over_not_a_negative_number():
    ledger = UsageLedger()
    ledger.feed({"self": tokens(1000, 100)})
    ledger.feed({"self": tokens(10, 1)})
    assert ledger.total == tokens(1010, 101)


def test_sub_agents_are_series_of_their_own():
    ledger = UsageLedger()
    ledger.feed({"self": tokens(100), "agent:a1": tokens(40)})
    ledger.feed({"self": tokens(110), "agent:a1": tokens(70), "agent:a2": tokens(5)})
    assert ledger.total == tokens(185)
    ledger.feed({"self": tokens(110)})  # (after a restart the sub-agents are not shown again: nothing is taken away)
    assert ledger.total == tokens(185)


def test_the_ledger_survives_the_cache():
    ledger = UsageLedger()
    ledger.feed({"self": Tokens(7, 8, None, 9), "agent:x": tokens(1)})
    back = UsageLedger.from_json(ledger.to_json())
    assert back == ledger
    for broken in (None, "x", {"total": "many", "seen": 3}, {"seen": {"self": {"input": "a"}}}):
        assert UsageLedger.from_json(broken) == UsageLedger(), broken


@pytest.mark.parametrize("series", [{}, {"self": tokens()}])
def test_a_ledger_with_nothing_to_say_has_no_total_or_a_zero_one(series):
    ledger = UsageLedger()
    ledger.feed(series)
    assert ledger.total in (None, tokens())
