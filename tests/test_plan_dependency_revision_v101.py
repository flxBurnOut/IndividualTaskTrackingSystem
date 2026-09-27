"""Plan retention preserves history, not a bypass around future dependencies."""
from copy import deepcopy
import datetime as dt
import uuid

import pytest

from management import daily_flow
from management.core import Core
from management.schemas import BusinessError


TODAY = '2030-01-07'
PAST = '2030-01-06'
FUTURE = '2030-01-08'


@pytest.fixture
def core(tmp_path, monkeypatch):
    class Clock:
        @staticmethod
        def now(zone):
            return dt.datetime(2030, 1, 7, 12, 0, tzinfo=zone)

    monkeypatch.setattr(daily_flow, 'datetime', Clock)
    return Core(tmp_path / 'dependency-revisions')


def command(core, name, payload, request_id=None):
    state = core.query('state')
    return core.command(name, payload, request_id=request_id or str(uuid.uuid4()),
                        epoch=state['epoch'], expected_revision=state['revision'])['result']


def task(core, title):
    return command(core, 'create', {'type': 'task', 'title': title,
        'data': {'completion_gate': title + ' original gate', 'estimated_minutes': 20}})['entity']


def depend(core, dependent, predecessor):
    command(core, 'link', {'source_id': dependent['id'], 'target_id': predecessor['id'], 'kind': 'depends_on'})


def plan(core, blocks, day=FUTURE, mode='no_precise_time'):
    return command(core, 'create_plan', {'date': day, 'mode': mode, 'blocks': blocks})['entity']


def revise_payload(old, blocks):
    return {'date': old['data']['date'], 'mode': old['data']['mode'],
            'plan_id': old['id'], 'plan_version': old['version'], 'blocks': deepcopy(blocks)}


def append_payload(old, target):
    return {'date': old['data']['date'], 'target_id': target['id'],
            'plan_id': old['id'], 'plan_version': old['version']}


def feedback(core, target, day, result='incomplete'):
    return command(core, 'record_feedback', {'target_id': target['id'], 'business_date': day,
        'dimensions': {'completion': result}, 'source_text': 'Explicit synthetic feedback.'})['entity']


def unchanged_after_rejection(core, name, payload, code='dependency'):
    before = core.query('state')
    with core.store.connect() as c:
        counts = {table: c.execute('SELECT count(*) FROM ' + table).fetchone()[0]
                  for table in ('entities', 'changes', 'receipts')}
    before_plan = core.query('daily_review', date=payload['date'])
    supplied = deepcopy(payload)
    request_id = str(uuid.uuid4())
    with pytest.raises(BusinessError) as error:
        command(core, name, payload, request_id)
    assert error.value.code == code
    assert core.query('state') == before
    assert core.query('daily_review', date=payload['date']) == before_plan
    assert not core.query('receipt', request_id=request_id)['found']
    assert payload == supplied
    with core.store.connect() as c:
        assert counts == {table: c.execute('SELECT count(*) FROM ' + table).fetchone()[0] for table in counts}


@pytest.mark.parametrize('timed', [False, True])
def test_reversing_retained_future_blocks_rechecks_dependency(core, timed):
    a, b = task(core, 'A'), task(core, 'B')
    depend(core, b, a)
    blocks = [{'target_id': a['id']}, {'target_id': b['id']}]
    if timed:
        blocks[0].update(start='09:00', end='09:20')
        blocks[1].update(start='10:00', end='10:20')
    old = plan(core, blocks, mode='standard' if timed else 'no_precise_time')
    unchanged_after_rejection(core, 'revise_plan', revise_payload(old, list(reversed(old['data']['blocks']))))
    assert core.query('get', id=old['id'])['entity'] == old


def test_removing_unfinished_predecessor_cannot_leave_retained_dependent(core):
    a, b = task(core, 'A'), task(core, 'B')
    depend(core, b, a)
    old = plan(core, [{'target_id': a['id']}, {'target_id': b['id']}])
    unchanged_after_rejection(core, 'revise_plan', revise_payload(old, old['data']['blocks'][1:]))


def test_changing_predecessor_time_rechecks_unchanged_dependent_time(core):
    a, b = task(core, 'A'), task(core, 'B')
    depend(core, b, a)
    old = plan(core, [{'target_id': a['id'], 'start': '09:00', 'end': '09:20'},
                     {'target_id': b['id'], 'start': '10:00', 'end': '10:20'}], mode='standard')
    changed = deepcopy(old['data']['blocks'])
    changed[0].update(start='11:00', end='11:20')
    unchanged_after_rejection(core, 'revise_plan', revise_payload(old, changed))


@pytest.mark.parametrize('append_predecessor', [False, True])
def test_append_rechecks_new_dependency_on_preexisting_future_block(core, append_predecessor):
    a, b, c = task(core, 'A'), task(core, 'B'), task(core, 'C')
    old = plan(core, [{'target_id': b['id']}])
    depend(core, b, a)
    unchanged_after_rejection(core, 'add_to_plan', append_payload(old, a if append_predecessor else c))


def test_revise_rechecks_new_dependency_without_any_block_change(core):
    a, b = task(core, 'A'), task(core, 'B')
    old = plan(core, [{'target_id': b['id']}])
    depend(core, b, a)
    unchanged_after_rejection(core, 'revise_plan', revise_payload(old, old['data']['blocks']))


def test_legal_reorder_and_append_preserve_original_gates(core):
    a, b, c, d = [task(core, title) for title in ('A', 'B', 'C', 'D')]
    depend(core, b, a)
    old = plan(core, [{'target_id': a['id']}, {'target_id': b['id']}, {'target_id': c['id']}])
    blocks = old['data']['blocks']
    new = command(core, 'revise_plan', revise_payload(old, [blocks[2], blocks[0], blocks[1]]))['entity']
    assert [x['target_id'] for x in new['data']['blocks']] == [c['id'], a['id'], b['id']]
    appended = command(core, 'add_to_plan', append_payload(new, d))['entity']
    assert appended['data']['blocks'][:3] == new['data']['blocks']
    assert core.query('get', id=old['id'])['entity'] == old
    assert core.query('list', type='feedback')['total'] == 0


@pytest.mark.parametrize('remove', [False, True])
def test_actual_prior_completion_allows_retained_dependent_without_plan_predecessor(core, remove):
    a, b = task(core, 'A'), task(core, 'B')
    depend(core, b, a)
    old = plan(core, [{'target_id': a['id']}, {'target_id': b['id']}])
    actual = feedback(core, a, TODAY, 'done')
    blocks = old['data']['blocks'][1:] if remove else list(reversed(old['data']['blocks']))
    new = command(core, 'revise_plan', revise_payload(old, blocks))['entity']
    assert new['data']['blocks'] == blocks
    assert core.query('get', id=actual['id'])['entity'] == actual
    assert core.query('get', id=old['id'])['entity'] == old


def test_actual_completion_allows_append_after_new_dependency(core):
    a, b, c = task(core, 'A'), task(core, 'B'), task(core, 'C')
    old = plan(core, [{'target_id': b['id']}])
    depend(core, b, a)
    feedback(core, a, TODAY, 'done')
    new = command(core, 'add_to_plan', append_payload(old, c))['entity']
    assert new['data']['blocks'][0] == old['data']['blocks'][0]


def historical_plan(core, reason):
    a, b, extra = task(core, 'Unfinished prerequisite'), task(core, 'Historical item'), task(core, 'New item')
    day = PAST if reason == 'past_day' else TODAY
    block = {'target_id': b['id']}
    if reason == 'elapsed_today':
        block.update(start='10:00', end='11:00')
    old = plan(core, [block], day=day, mode='standard' if reason == 'elapsed_today' else 'no_precise_time')
    fact = feedback(core, b, day) if reason == 'reported' else None
    depend(core, b, a)  # Added later: do not retroactively reinterpret the old history.
    return a, b, extra, old, fact


@pytest.mark.parametrize('reason', ['reported', 'past_day', 'elapsed_today'])
@pytest.mark.parametrize('operation', ['revise_plan', 'add_to_plan'])
def test_history_is_preserved_without_retroactive_dependency_enforcement(core, reason, operation):
    _, _, extra, old, fact = historical_plan(core, reason)
    original = deepcopy(old['data']['blocks'])
    payload = (append_payload(old, extra) if operation == 'add_to_plan'
               else revise_payload(old, [*original, {'target_id': extra['id']}]))
    new = command(core, operation, payload)['entity']
    assert new['data']['blocks'][0] == original[0]
    assert core.query('get', id=old['id'])['entity'] == old
    assert [x['target_id'] for x in new['data']['blocks']] == [original[0]['target_id'], extra['id']]
    if fact:
        assert core.query('get', id=fact['id'])['entity'] == fact
        assert core.query('daily_review', date=old['data']['date'])['items'][0]['raw_result'] == 'incomplete'


@pytest.mark.parametrize('reason', ['reported', 'past_day', 'elapsed_today'])
@pytest.mark.parametrize('remove', [False, True])
def test_history_cannot_be_removed_or_edited_to_resolve_new_dependency(core, reason, remove):
    _, _, _, old, _ = historical_plan(core, reason)
    blocks = deepcopy(old['data']['blocks'])
    if remove:
        blocks.clear()
    else:
        blocks[0]['minutes'] = 31
    unchanged_after_rejection(core, 'revise_plan', revise_payload(old, blocks), 'plan_history')


@pytest.mark.parametrize('operation', ['revise_plan', 'add_to_plan'])
def test_historical_exemption_does_not_leak_to_future_block_in_same_plan(core, operation):
    a, history, future, extra = [task(core, title) for title in ('A', 'History', 'Future', 'Extra')]
    old = plan(core, [{'target_id': history['id'], 'start': '10:00', 'end': '11:00'},
                     {'target_id': future['id'], 'start': '14:00', 'end': '15:00'}], day=TODAY, mode='standard')
    depend(core, history, a)
    depend(core, future, a)
    payload = (append_payload(old, extra) if operation == 'add_to_plan'
               else revise_payload(old, old['data']['blocks']))
    unchanged_after_rejection(core, operation, payload)


def test_predecessor_planned_on_previous_day_is_not_actual_completion(core):
    a, b = task(core, 'A'), task(core, 'B')
    depend(core, b, a)
    plan(core, [{'target_id': a['id']}], day=TODAY)
    unchanged_after_rejection(core, 'create_plan', {'date': FUTURE, 'mode': 'no_precise_time',
                                                   'blocks': [{'target_id': b['id']}]})


def test_public_create_job_cannot_inject_internal_plan_baseline(core):
    command(core, 'settings', {'settings': {'ai': {'enabled': True}}})
    before = core.query('state')
    request_id = str(uuid.uuid4())
    with pytest.raises(BusinessError) as error:
        command(core, 'create_job', {'kind': 'ai', 'input': {
            'prompt': 'Synthetic plan request', 'plan_baseline': {'plan_id': None, 'plan_version': None}}}, request_id)
    assert error.value.code == 'proposal_scope'
    assert core.query('state') == before
    assert core.query('jobs')['total'] == 0
    assert not core.query('receipt', request_id=request_id)['found']
