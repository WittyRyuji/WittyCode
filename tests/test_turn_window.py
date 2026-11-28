from copy import deepcopy
import json
from types import SimpleNamespace

from desktop.core.turn_delta import changed_blocks
from desktop.core.turn_window import view_window


def test_view_window_only_renders_the_tail_and_explicit_pages():
    source = [SimpleNamespace(id=str(i), version=1) for i in range(18)]
    rendered = []

    def render(turn):
        rendered.append(turn.id)
        return {"id": turn.id, "blocks": []}

    window = view_window()
    initial = window.sync(source, render)
    assert initial['all'] and initial['history']['more'] and rendered == ['13', '14', '15', '16', '17']
    generation = initial['history']['generation']
    assert window.sync(source, render) is None
    assert window.older(source, render, generation=generation - 1, before='13') is None
    page = window.older(source, render, generation=generation, before='13')
    assert page['prepend'] and [t['id'] for t in page['turns']] == ['8', '9', '10', '11', '12']
    assert window.older(source, render, generation=generation, before='13') is None
    source[-1].version += 1
    rendered.clear()
    assert window.sync(source, render)['turns'] == [{'id': '17', 'blocks': []}]
    assert rendered == ['17']
    window.older(source, render, generation=generation, before='8')
    assert not window.older(source, render, generation=generation, before='3')['history']['more']
    replacement = window.sync(list(source), render)
    assert replacement['all'] and len(replacement['turns']) == 5
    assert replacement['history']['generation'] > generation


def test_default_window_slides_without_resending_the_other_four_turns():
    source = [SimpleNamespace(id=str(i), version=1) for i in range(5)]
    window = view_window()
    render = lambda turn: {"id": turn.id, "blocks": []}
    full = window.sync(source, render)
    generation = full['history']['generation']
    source.append(SimpleNamespace(id='5', version=1))
    updated = window.sync(source, render)
    assert updated['trim'] == 1 and updated['history']['more']
    assert [turn['id'] for turn in updated['turns']] == ['5']
    assert window.ids == ['1', '2', '3', '4', '5']
    assert [turn['id'] for turn in window.older(source, render, generation=generation, before='1')['turns']] == ['0']
    source.append(SimpleNamespace(id='6', version=1))
    assert 'trim' not in window.sync(source, render)
    assert window.ids == [str(i) for i in range(7)]


def test_delta_omits_old_activity_and_sends_unicode_suffix():
    previous = {'id': 't', 'blocks': [
        {'id': 'b', 'kind': 'text', 'streaming': True, 'markdown': 'a' * 160_000 + '🙂'},
        {'id': 'a', 'kind': 'activity', 'entries': [
            *[{'id': str(i), 'kind': 'tool', 'output': 'same' * 500} for i in range(100)],
            {'id': 'running', 'kind': 'tool', 'status': 'running', 'output': 'x' * 1000},
        ]},
    ]}
    current = deepcopy(previous)
    current['blocks'][0]['markdown'] += '汉🚀'
    current['blocks'][0].pop('streaming')
    current['blocks'][1]['entries'][-1].update(output='x' * 1000 + 'z', status='done')
    delta = changed_blocks(current, previous)
    assert delta['blocks'][0] == {'id': 'b', 'kind': 'textPatch', 'remove': ['streaming'], 'suffix': '汉🚀'}
    assert delta['blocks'][1] == {'id': 'a', 'kind': 'activityPatch', 'entries': [
        {'id': 'running', 'kind': 'entryPatch', 'suffix': 'z', 'fields': {'status': 'done'}},
    ]}
    assert len(json.dumps(delta).encode()) < 300
    current['blocks'][1]['entries'].reverse()
    assert changed_blocks(current, previous)['blocks'][1]['order'][:2] == ['running', '99']
    assert changed_blocks(current, None) is current
