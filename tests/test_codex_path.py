"""A new ``codex_path``: what the engine learned from the old program is dropped, and so is its idle app-server."""

from __future__ import annotations

from tests.codex_support import make_engine, run


def test_a_new_path_stops_an_idle_app_server_forgets_the_models_and_starts_the_next_one_on_use(tmp_path):
    async def scenario():
        harness = make_engine(tmp_path)
        engine = harness.engine
        await engine.list_models()
        assert len(harness.clients) == 1 and harness.clients[0].running and len(harness.requests("model/list")) == 1
        await engine.list_models()
        assert len(harness.requests("model/list")) == 1, "the list is kept until the path changes"

        assert await engine.path_changed() is True
        assert not harness.clients[0].running, "nobody uses it: the old program's app-server is stopped"
        await engine.list_models()
        assert len(harness.clients) == 2 and harness.clients[1].running, "the next request starts a server again (of the new path)"
        assert len(harness.requests("model/list")) == 2, "and reads the list again"
        await engine.close_all()

    run(scenario())


def test_a_new_path_leaves_the_app_server_alone_while_a_session_uses_it(tmp_path):
    async def scenario():
        harness = make_engine(tmp_path)
        controller = await harness.engine.open_new(tmp_path)
        assert controller.is_open and len(harness.clients) == 1
        assert await harness.engine.path_changed() is False, "stopping it would end the session"
        assert harness.clients[0].running
        await controller.close()
        assert await harness.engine.path_changed() is True and not harness.clients[0].running
        await harness.engine.close_all()

    run(scenario())


def test_a_new_path_with_no_server_started_yet_is_fine(tmp_path):
    async def scenario():
        harness = make_engine(tmp_path)
        assert await harness.engine.path_changed() is True and harness.clients == []
        await harness.engine.close_all()

    run(scenario())
