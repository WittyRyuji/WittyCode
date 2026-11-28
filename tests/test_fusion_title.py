"""The Factory title must not outlive the credentials that opened its seat."""
from __future__ import annotations

import asyncio
import pickle
from types import SimpleNamespace

import pytest

from desktop.core.chatgpt.responses import Finished, Shape, Started, TextDone
from desktop.core.fusion import title


@pytest.mark.parametrize("change", ["same", "rotated", "removed"])
def test_factory_title_discards_result_if_credential_changes(monkeypatch, change):
    async def go():
        model = "factory:demo"
        factory = SimpleNamespace(settings=SimpleNamespace(factory_path="demo-factory.exe"))
        saved = SimpleNamespace(origin={}, title="Before request", requests=[], fusion={})
        fusion = SimpleNamespace(registry=SimpleNamespace(policy=SimpleNamespace(max_tokens=0)), check_budget=lambda _: None)
        writes, notices = [], []
        controller = SimpleNamespace(
            manager=SimpleNamespace(factory=factory, shape=Shape()), model=model, saved=saved,
            _close_requested=False, _fusion=fusion, _paused_fusion=None, history_ready=True,
            _summary_effort=lambda: None, _save_now=lambda **_: writes.append(saved.title) or True,
            _notify=notices.append,
        )
        initial = "fixture-" + "first"
        monkeypatch.setenv("FACTORY_API_KEY", initial)
        accepted = title.source(controller, model)
        assert title.source(controller, model) == accepted
        assert initial not in repr(accepted)
        with pytest.raises(TypeError, match="cannot be serialized"):
            pickle.dumps(accepted[-1])

        def channel(host, chosen, *, before_prompt):
            assert host is controller and chosen == model

            async def stream(request):
                assert before_prompt() and request.get("tools", []) == []
                yield Started()
                if change == "rotated":
                    monkeypatch.setenv("FACTORY_API_KEY", "fixture-" + "second")
                elif change == "removed":
                    monkeypatch.delenv("FACTORY_API_KEY")
                assert before_prompt() is (change == "same")
                yield TextDone("title", "库存核对")
                yield Finished(None)

            return stream

        monkeypatch.setattr(title, "seat_stream", channel)
        expected = "库存核对" if change == "same" else None
        assert await title.generate(controller, "Check inventory.", model, accepted, lambda: True) == expected
        assert writes == ["Before request", expected or "Before request"]
        assert notices == (["fusion_title"] if expected else [])
        assert saved.origin == {title.ATTEMPT: "attempted"}
        assert saved.requests == [{"kind": "title", "model": model, "usage": None}]

    asyncio.run(go())
