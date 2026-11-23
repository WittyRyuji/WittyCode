"""The debate ledger: rounds are charged per real user request, when they start.

The topic string the model declares is free text — it cannot key the account. The ledger lives in
``saved.origin`` keyed by a fingerprint of the turn's ``user_block`` text (hashed, never stored), so a retry, a
rename or a subtask without its own user request lands on the same entry; a turn with a genuinely new user
request gets its own. ``run_debate`` reports each round the moment it starts, so a failed seat still spent it,
and the hard cap is re-read from the restored ledger after a reload.
"""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace

import pytest

from desktop.core.chatgpt.responses import Finished, Shape, Started, TextDone
from desktop.core.fusion import debate_bridge
from desktop.core.fusion.debate import DebateError, debate_band, run_debate
from desktop.core.fusion.registry import Policy


def _verdict(conclusion="", issue=""):
    return json.dumps({"conclusion": conclusion, "open_issue": issue})


def _seats(*replies):
    """A scripted responder: one reply per seat call, in order; an Exception reply fails that seat."""
    queue = list(replies)
    calls = []

    async def respond(seat, instructions, text):
        calls.append(seat)
        reply = queue.pop(0)
        if isinstance(reply, BaseException):
            raise reply
        return reply

    respond.calls = calls
    return respond


# ---------------------------------------------------------------- debate.py: bands, early stop, round accounting

def test_each_band_maps_its_default_and_cap():
    assert debate_band(0) == debate_band(60) == (0, 0)
    assert debate_band(61) == debate_band(70) == (1, 2)
    assert debate_band(71) == debate_band(80) == (2, 3)
    assert debate_band(81) == debate_band(95) == (3, 4)
    assert debate_band(96) == debate_band(100) == (3, 5)
    assert debate_band(-5) == (0, 0) and debate_band(400) == (3, 5)


def test_a_conclusion_stops_before_the_default():
    seat = _seats("objection", _verdict(conclusion="Settled."), "unused", "unused")
    result = asyncio.run(run_debate(75, "Shard?", seat))
    assert result.status == "concluded" and result.rounds == result.total_rounds == 1
    assert seat.calls == ["l2", "l1"], "one round of two seats, early stop"


def test_the_default_stops_when_no_new_issue_remains():
    # 75 -> (2, 3): a repeated (or empty) issue past the default buys no extra round.
    seat = _seats("c1", _verdict(issue="Is it hot?"),
                  "c2", _verdict(issue="is  it\tHOT?"),      # the same issue under another case and spacing
                  "c3", "unused")
    result = asyncio.run(run_debate(75, "Shard?", seat))
    assert result.status == "unresolved" and result.total_rounds == 2
    assert result.open_issue == "is  it\tHOT?"


def test_a_new_issue_extends_only_to_the_cap():
    seat = _seats("c1", _verdict(issue="q1"), "c2", _verdict(issue="q2"), "c3", _verdict(issue="q3"))
    result = asyncio.run(run_debate(75, "Shard?", seat))
    assert result.status == "unresolved" and result.total_rounds == 3, "the cap is a hard stop"


def test_a_failed_round_is_reported_as_started():
    started = []
    seat = _seats("c1", _verdict(issue="q1"), RuntimeError("seat died"))
    with pytest.raises(RuntimeError):
        asyncio.run(run_debate(75, "Shard?", seat, on_round=started.append))
    assert started == [1, 2], "the second round began — and therefore counts — before its L2 seat failed"


def test_a_rename_or_retry_keeps_the_spent_rounds():
    first = asyncio.run(run_debate(75, "Shard the store?", _seats(
        "c1", _verdict(issue="q1"), "c2", _verdict(issue="q1"))))   # stops at the default, 2 spent
    seat = _seats("c3", _verdict(conclusion="No."))
    again = asyncio.run(run_debate(75, "should we shard the store", seat,
                                 prior_rounds=first.total_rounds, prior_issues=["q1"]))
    assert again.total_rounds == 3 and seat.calls == ["l2", "l1"], "one round left, not a fresh account"


def test_a_spent_cap_skips_without_touching_the_responder():
    seat = _seats("c1", _verdict(conclusion="No."))
    result = asyncio.run(run_debate(75, "renamed topic", seat, prior_rounds=3))
    assert result.status == "skipped" and result.total_rounds == 3 and seat.calls == []


# ---------------------------------------------------------------- debate_bridge.py: the ledger key and account

def _controller(text="Is the cache design safe?", *, preference=75, origin=None, turn_id="t1"):
    """The smallest controller ``discuss`` needs: a real user request, a stub model stream, a durable origin."""
    policy = Policy("chatgpt:gpt-6.1-sol", ("chatgpt:gpt-6.1-sol",), "chatgpt:gpt-6-luna", 0,
                    max_seconds=30, preference=preference, reasoning={})
    return SimpleNamespace(
        _fusion=SimpleNamespace(registry=SimpleNamespace(policy=policy),
                                check_budget=lambda *a, **k: None,
                                record_request=lambda *a, **k: None,
                                reasoning_for=lambda *a, **k: None),
        _turn=SimpleNamespace(id=turn_id, user_block=SimpleNamespace(text=text), stopping=False),
        _close_requested=False,
        manager=SimpleNamespace(stream=None, shape=Shape()),
        saved=SimpleNamespace(origin={} if origin is None else origin, requests=[]),
        _add_usage=lambda *a, **k: None,
        _save_now=lambda **k: True,
    )


def _script(controller, *replies):
    """Seat stream replies in call order; an Exception reply fails its seat after the request started."""
    queue = list(replies)
    calls = []

    async def stream(request):
        calls.append(request)
        reply = queue.pop(0)
        yield Started()
        if isinstance(reply, BaseException):
            raise reply
        yield TextDone("m", reply)
        yield Finished(usage=None)

    controller.manager.stream = stream
    return calls


def _entries(controller):
    return debate_bridge._ledger(controller)


def test_the_fingerprint_follows_the_user_request_not_the_topic():
    a = debate_bridge._request_key(_controller("  Shard   the\nstore? "))
    b = debate_bridge._request_key(_controller("shard the store?"))
    c = debate_bridge._request_key(_controller("Another question entirely."))
    assert a == b and a != c and a.startswith("u:")
    assert debate_bridge._request_key(_controller("   ")) is None


def test_a_failed_seat_still_spent_the_round():
    async def go():
        controller = _controller()
        _script(controller, "challenge", RuntimeError("seat died"))
        with pytest.raises(DebateError):
            await debate_bridge.discuss(controller, "Should the cache be shared?")
        (key, entry), = _entries(controller).items()
        assert key.startswith("u:") and entry["rounds"] == 1, "the failed round counts"
    asyncio.run(go())


def test_a_rename_on_a_new_turn_reads_the_same_account():
    async def go():
        controller = _controller()
        _script(controller, "c1", _verdict(issue="q1"), "c2", _verdict(issue="q1"))
        first = await debate_bridge.discuss(controller, "Shard the store?")
        assert first.status == "unresolved" and first.total_rounds == 2
        controller._turn = SimpleNamespace(id="t2", user_block=SimpleNamespace(text="Is the cache design safe?"),
                                           stopping=False)
        calls = _script(controller, "c3", _verdict(issue="q2"), "unused", "unused")
        again = await debate_bridge.discuss(controller, "a completely renamed topic")
        assert again.total_rounds == 3 and len(calls) == 2, "only the one round left under the cap ran"
        (key, entry), = _entries(controller).items()
        assert key.startswith("u:") and entry["rounds"] == 3
        controller._turn = SimpleNamespace(id="t3", user_block=SimpleNamespace(text="Is the cache design safe?"),
                                           stopping=False)
        again = await debate_bridge.discuss(controller, "yet another name")
        assert again.status == "skipped", "the hard cap held across the renames"
    asyncio.run(go())


def test_a_genuinely_new_request_gets_its_own_account():
    async def go():
        controller = _controller()
        _script(controller, "c1", _verdict(issue="q1"), "c2", _verdict(issue="q2"), "c3", _verdict(issue="q3"))
        spent = await debate_bridge.discuss(controller, "Topic one")
        assert spent.total_rounds == 3
        controller._turn = SimpleNamespace(id="t2", user_block=SimpleNamespace(text="A different question?"),
                                           stopping=False)
        _script(controller, "n1", _verdict(conclusion="Fresh answer."))
        fresh = await debate_bridge.discuss(controller, "Topic one")
        assert fresh.status == "concluded" and fresh.total_rounds == 1, "a new user request, a new account"
        assert len(_entries(controller)) == 2
    asyncio.run(go())


def test_a_turn_without_a_user_request_charges_the_pending_entry():
    async def go():
        controller = _controller()
        _script(controller, "c1", _verdict(issue="q1"), "c2", _verdict(issue="q1"))
        await debate_bridge.discuss(controller, "Shard the store?")
        # A subtask-style turn has no user_block of its own: it must not mint a fresh account.
        controller._turn = SimpleNamespace(id="t9", user_block=None, stopping=False)
        _script(controller, "c3", _verdict(conclusion="Done."))
        result = await debate_bridge.discuss(controller, "any topic name")
        (key, entry), = _entries(controller).items()
        assert key.startswith("u:") and entry["rounds"] == 3 and result.status == "concluded"
    asyncio.run(go())


def test_the_ledger_survives_a_reload_and_still_enforces_the_cap():
    async def go():
        controller = _controller()
        _script(controller, "c1", _verdict(issue="q1"), "c2", _verdict(issue="q2"), "c3", _verdict(issue="q3"))
        spent = await debate_bridge.discuss(controller, "Shard?")
        assert spent.total_rounds == 3
        # The session comes back from disk: origin is JSON again, the controller and its caches are new.
        restored = _controller(origin=json.loads(json.dumps(controller.saved.origin)))
        restored._turn = SimpleNamespace(id="t2", user_block=SimpleNamespace(text="Is the cache design safe?"),
                                         stopping=False)
        calls = _script(restored, "unused")
        result = await debate_bridge.discuss(restored, "renamed after the reload")
        assert result.status == "skipped" and result.total_rounds == 3 and calls == []
    asyncio.run(go())


def test_the_ledger_never_stores_the_users_words():
    async def go():
        controller = _controller("Never store this sentence verbatim anywhere.")
        _script(controller, "c1", _verdict(conclusion="Done."))
        await debate_bridge.discuss(controller, "T")
        blob = json.dumps(controller.saved.origin)
        assert "verbatim" not in blob and "Never store this" not in blob
    asyncio.run(go())
