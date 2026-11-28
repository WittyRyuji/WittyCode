"""Worker I/O never mutates live state, overtakes an earlier write, or weakens a required checkpoint."""
import asyncio
from threading import Event, get_ident
from types import SimpleNamespace

import pytest

from desktop.core.chatgpt.controller import ChatGPTController, user_item
from desktop.core.chatgpt.errors import ChatGPTError
from desktop.core.harness.store import SavedTurn, SessionStore
from desktop.core.session_state import SessionState


def controller(tmp_path):
    manager = SimpleNamespace(sessions=SessionStore(tmp_path / 'archive'), _notify=lambda *_: None)
    ctl = ChatGPTController(manager, tmp_path / 'project', state=SessionState.READY, session_id='demo')
    ctl.saved.turns = [SavedTurn(start=0, at=1, finished=2)]
    ctl.saved.items = [user_item('before', ())]
    ctl._checkpoint_written = True
    return ctl


def test_checkpoint_io_is_off_loop_and_two_writes_keep_a_detached_order(tmp_path, monkeypatch):
    ctl = controller(tmp_path)
    store = ctl.manager.sessions
    written = store.save
    main = get_ident()
    entered, release = Event(), Event()
    seen = []

    def save(snapshot):
        assert get_ident() != main
        if not seen:
            entered.set()
            assert release.wait(5)
        seen.append(snapshot.items[0]['content'][0]['text'])
        written(snapshot)

    monkeypatch.setattr(store, 'save', save)

    async def run():
        first = asyncio.create_task(ctl._save_durable(required=True))
        try:
            assert await asyncio.to_thread(entered.wait, 3)
            ctl.saved.items[0]['content'][0]['text'] = 'after'
            second = asyncio.create_task(ctl._save_durable(required=True))
            await asyncio.sleep(0.02)
            assert not first.done() and not second.done(), 'the loop was responsive while I/O was held'
        finally:
            release.set()
        assert await first and await second

    asyncio.run(run())
    assert seen == ['before', 'after']
    assert store.load('demo').items[0]['content'][0]['text'] == 'after'


@pytest.mark.parametrize('cancellations', [1, 2])
def test_cancelled_checkpoint_does_not_overtake_inflight_io(tmp_path, monkeypatch, cancellations):
    ctl = controller(tmp_path)
    store = ctl.manager.sessions
    written = store.save
    entered, release = Event(), Event()

    def save(snapshot):
        entered.set()
        assert release.wait(5)
        written(snapshot)

    monkeypatch.setattr(store, 'save', save)

    async def run():
        task = asyncio.create_task(ctl._save_durable(required=True))
        try:
            assert await asyncio.to_thread(entered.wait, 3)
            for _ in range(cancellations):
                task.cancel()
                await asyncio.sleep(0)
                assert not task.done() and ctl._checkpoint is not None
        finally:
            release.set()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert ctl._checkpoint is None and store.load('demo') is not None

    asyncio.run(run())


def test_required_worker_checkpoint_reports_disk_failure_to_caller(tmp_path, monkeypatch):
    ctl = controller(tmp_path)
    monkeypatch.setattr(ctl.manager.sessions, 'save', lambda _: (_ for _ in ()).throw(OSError('synthetic disk failure')))
    with pytest.raises(ChatGPTError) as caught:
        asyncio.run(ctl._save_durable(required=True))
    assert caught.value.code == 'checkpoint' and ctl._save_failed
    assert ctl._checkpoint is None
