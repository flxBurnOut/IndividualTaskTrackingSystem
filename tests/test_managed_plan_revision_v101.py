"""Managed create/revise candidates: synthetic storage, no provider or service."""
import copy
import json
import threading
import uuid

import pytest

from management import ai, conversations
from management.core import Core
from management.plan_assistance import normalize
from management.schemas import BusinessError
from management.storage import encode
from context_harness import ready

DAY = '2030-01-01'


def command(core, name, payload, request_id=None):
    state = core.query('state')
    return core.command(name, payload, request_id=request_id or str(uuid.uuid4()),
                        epoch=state['epoch'], expected_revision=state['revision'])


def create(core, title):
    return command(core, 'create', {'type': 'task', 'title': title,
                                   'data': {'completion_gate': title + ' complete'}})['result']['entity']


def start(core):
    command(core, 'settings', {'settings': {'ai': {'enabled': True}}})
    return command(core, 'send_message', {'scope': {'kind': 'daily_plan', 'date': DAY},
                   'text': '请调整这一天的计划', 'request_plan': True})['result']['job']


def initial(core, tasks):
    return command(core, 'create_plan', {'date': DAY, 'mode': 'no_precise_time',
                    'blocks': [{'target_id': task['id']} for task in tasks]})['result']['entity']


def proposal(tasks, old=None):
    payload = {'date': DAY, 'mode': 'no_precise_time',
               'blocks': [{'target_id': task['id']} for task in tasks]}
    if old:
        payload.update(plan_id=old['id'], plan_version=old['version'])
    return {'summary': '合成计划候选', 'unknowns': [], 'sources': [],
            'actions': [{'command': 'revise_plan' if old else 'create_plan',
                         'payload': payload, 'reason': '用户明确请求'}]}


def normalize_only(core, job, candidate):
    with core.store.connect() as c:
        before = c.execute('SELECT count(*) FROM changes').fetchone()[0]
        result = normalize(core, c, job, candidate)
        assert c.execute('SELECT count(*) FROM changes').fetchone()[0] == before
    return result


def pending(core, job, candidate, *, normalize_candidate=True):
    ready(core, job, planning=True)
    with core.store.connect() as c:
        c.execute('BEGIN IMMEDIATE')
        candidate = normalize(core, c, job, candidate) if normalize_candidate else candidate
        c.execute("UPDATE jobs SET status='awaiting_review',result=? WHERE id=?", (encode(candidate), job['id']))
        conversations.complete_job(core, c, job, candidate)
        c.commit()
    return candidate


def fixture(tmp_path):
    core = Core(tmp_path)
    first, second = create(core, 'Original'), create(core, 'Remaining')
    old = initial(core, [first])
    return core, first, second, old


def test_first_plan_keeps_create_only_and_is_dry_run_until_adopted(tmp_path):
    core = Core(tmp_path)
    task = create(core, 'Known work')
    job = start(core)
    assert job['input']['allowed_commands'] == ['create_plan']
    assert job['input']['plan_baseline'] == {'plan_id': None, 'plan_version': None}
    before = core.query('state')
    result = pending(core, job, proposal([task]))
    assert result['plan_preview']['command'] == 'create_plan'
    assert core.query('state')['revision'] == before['revision']
    assert core.query('list', type='plan')['total'] == 0
    command(core, 'apply_proposal', {'id': job['id']})
    assert core.query('list', type='plan')['total'] == 1
    assert core.query('list', type='feedback')['total'] == 0


def test_existing_plan_revises_one_predecessor_and_replays_same_receipt(tmp_path):
    core, first, second, old = fixture(tmp_path)
    job = start(core)
    assert job['input']['allowed_commands'] == ['revise_plan']
    assert job['input']['plan_baseline'] == {'plan_id': old['id'], 'plan_version': old['version']}
    before = core.query('state')
    value = proposal([second], old)
    copy_before = copy.deepcopy(value)
    result = pending(core, job, value)
    assert value == copy_before
    assert result['plan_preview']['command'] == 'revise_plan'
    assert result['plan_preview']['plan_id'] == old['id']
    assert result['plan_preview']['saved'] is False
    assert core.query('state')['revision'] == before['revision']
    assert core.query('list', type='plan')['total'] == 1
    request_id = str(uuid.uuid4())
    applied = command(core, 'apply_proposal', {'id': job['id']}, request_id)
    replay = command(core, 'apply_proposal', {'id': job['id']}, request_id)
    assert replay['replayed'] and replay['result'] == applied['result']
    current = applied['result']['results'][0]['entity']
    assert current['data']['supersedes_id'] == old['id']
    assert current['data']['blocks'][0]['target_id'] == second['id']
    assert core.query('daily_review', date=DAY)['plan']['id'] == current['id']
    assert core.query('list', type='plan')['total'] == 2  # original + one revision
    assert core.query('get', id=old['id'])['entity'] == old
    assert core.query('get', id=first['id'])['entity'] == first
    assert core.query('list', type='feedback')['total'] == 0


@pytest.mark.parametrize('change', ['missing_id', 'missing_version', 'bool_version', 'wrong_version', 'foreign_plan', 'wrong_date', 'extra_action', 'task_write'])
def test_revision_requires_exact_identity_date_and_one_plan_action(tmp_path, change):
    core, first, second, old = fixture(tmp_path)
    job = start(core)
    value = proposal([second], old)
    action, payload = value['actions'][0], value['actions'][0]['payload']
    if change == 'missing_id': payload.pop('plan_id')
    elif change == 'missing_version': payload.pop('plan_version')
    elif change == 'bool_version': payload['plan_version'] = True
    elif change == 'wrong_version': payload['plan_version'] += 1
    elif change == 'foreign_plan': payload['plan_id'] = str(uuid.uuid4())
    elif change == 'wrong_date': payload['date'] = '2030-01-02'
    elif change == 'extra_action': value['actions'].append(copy.deepcopy(action))
    else: action.update(command='update', payload={'id': first['id'], 'version': 1, 'patch': {'title': 'not allowed'}})
    before = core.query('state')
    with core.store.connect() as c:
        audit = c.execute('SELECT count(*) FROM changes').fetchone()[0]
        with pytest.raises(BusinessError): normalize(core, c, job, value)
        assert c.execute('SELECT count(*) FROM changes').fetchone()[0] == audit
    assert core.query('state') == before
    assert core.query('get', id=old['id'])['entity'] == old


@pytest.mark.parametrize('mutation', ['new_plan', 'same_plan_new_version'])
def test_plan_changed_during_generation_does_not_retarget_candidate(tmp_path, mutation):
    core, first, second, old = fixture(tmp_path)
    job = start(core)
    if mutation == 'new_plan':
        command(core, 'revise_plan', proposal([first, second], old)['actions'][0]['payload'])
    else:
        archived = command(core, 'archive', {'id': old['id'], 'version': old['version'], 'archived': True})['result']['entity']
        command(core, 'archive', {'id': old['id'], 'version': archived['version'], 'archived': False})
    before = core.query('state')
    with pytest.raises(BusinessError) as error:
        normalize_only(core, job, proposal([second], old))
    assert error.value.code == 'plan_conflict'
    assert core.query('state') == before


def test_first_plan_candidate_conflicts_if_another_entrance_created_one(tmp_path):
    core = Core(tmp_path)
    task = create(core, 'Task')
    job = start(core)
    old = initial(core, [task])
    with pytest.raises(BusinessError) as error:
        normalize_only(core, job, proposal([task]))
    assert error.value.code == 'plan_conflict'
    assert core.query('list', type='plan')['total'] == 1
    assert core.query('get', id=old['id'])['entity'] == old


@pytest.mark.parametrize('omit', [True, False])
def test_revision_must_preserve_reported_history_exactly(tmp_path, omit):
    core, first, second, old = fixture(tmp_path)
    command(core, 'record_feedback', {'target_id': first['id'], 'business_date': DAY,
            'dimensions': {'completion': 'done'}, 'source_text': 'Synthetic explicit completion'})
    job = start(core)
    value = proposal([second], old)
    if not omit:
        changed = copy.deepcopy(old['data']['blocks'][0]); changed['minutes'] = 5
        value['actions'][0]['payload']['blocks'].insert(0, changed)
    with pytest.raises(BusinessError) as error:
        normalize_only(core, job, value)
    assert error.value.code == 'plan_history'
    value['actions'][0]['payload']['blocks'] = [*copy.deepcopy(old['data']['blocks']), {'target_id': second['id']}]
    result = pending(core, job, value)
    assert result['actions'][0]['payload']['blocks'][0] == old['data']['blocks'][0]
    changed = command(core, 'apply_proposal', {'id': job['id']})['result']['results'][0]['entity']
    assert changed['data']['blocks'][0] == old['data']['blocks'][0]


def test_new_feedback_after_preview_blocks_removing_now_protected_item(tmp_path):
    core, first, second, old = fixture(tmp_path)
    job = start(core)
    pending(core, job, proposal([second], old))
    command(core, 'record_feedback', {'target_id': first['id'], 'business_date': DAY,
            'dimensions': {'completion': 'done'}, 'source_text': 'Explicit result after preview'})
    with pytest.raises(BusinessError) as error:
        command(core, 'apply_proposal', {'id': job['id']})
    assert error.value.code in {'plan_history', 'context_changed', 'context_coverage'}
    assert core.query('list', type='plan')['total'] == 1


def test_followup_supersedes_old_revision_candidate_without_applying_it(tmp_path):
    core, first, second, old = fixture(tmp_path)
    job = start(core)
    pending(core, job, proposal([second], old))
    following = command(core, 'send_message', {'scope': {'kind': 'daily_plan', 'date': DAY},
                         'text': '请修改这一天的计划，保留原来的任务'})['result']['job']
    assert following['input']['allowed_commands'] == ['revise_plan']
    assert following['input']['plan_baseline']['plan_id'] == old['id']
    with pytest.raises(BusinessError) as error:
        command(core, 'apply_proposal', {'id': job['id']})
    assert error.value.code in {'stale_proposal', 'job_state'}
    assert core.query('list', type='plan')['total'] == 1


def test_pre_upgrade_create_candidate_is_converted_to_versioned_revision(tmp_path):
    core, first, second, old = fixture(tmp_path)
    job = start(core)
    job['input'].pop('plan_baseline')
    job['input']['allowed_commands'] = ['create_plan']
    with core.store.connect() as c:
        c.execute('UPDATE jobs SET input=? WHERE id=?', (encode(job['input']), job['id']))
    ready(core, job, planning=True)
    value = proposal([second])
    value['actions'][0]['payload']['supersedes_id'] = old['id']
    result = pending(core, job, value)
    action = result['actions'][0]
    assert action['command'] == 'revise_plan'
    assert (action['payload']['plan_id'], action['payload']['plan_version']) == (old['id'], old['version'])
    assert 'supersedes_id' not in action['payload']
    latest = command(core, 'apply_proposal', {'id': job['id']})['result']['results'][0]['entity']
    assert latest['data']['supersedes_id'] == old['id']
    assert core.query('list', type='plan')['total'] == 2


def test_pre_upgrade_staged_manifest_also_uses_revision_dispatch(tmp_path):
    from management.context_candidates import stage
    from management.context_service import _operation
    core, first, second, old = fixture(tmp_path)
    job = start(core)
    job['input'].pop('plan_baseline')
    job['input']['allowed_commands'] = ['create_plan']
    with core.store.connect() as c:
        c.execute('UPDATE jobs SET input=? WHERE id=?', (encode(job['input']), job['id']))
    ready(core, job, planning=True)
    value = proposal([second])
    value['actions'][0]['payload']['supersedes_id'] = old['id']
    with core.store.connect() as c:
        value = stage(c, _operation(core, c, job['input']['context_operation_id']), value)
    pending(core, job, value, normalize_candidate=False)
    latest = command(core, 'apply_proposal', {'id': job['id']})['result']['results'][0]['entity']
    assert latest['data']['supersedes_id'] == old['id']
    assert core.query('list', type='plan')['total'] == 2


def test_legacy_candidate_without_baseline_does_not_guess_latest_plan(tmp_path):
    core, first, second, old = fixture(tmp_path)
    job = start(core)
    job['input'].pop('plan_baseline')
    with pytest.raises(BusinessError) as error:
        normalize_only(core, job, proposal([second]))
    assert error.value.code == 'plan_conflict'


@pytest.mark.parametrize('text, expected', [
    ('请修改这一天的计划', DAY),
    ('请把这一天的计划改成先做已登记的练习', DAY),
    ('先不要修改计划，只讨论', None),
    ('不要把计划改成新的版本', None),
    ('我已经完成原计划第一项', None),
])
def test_revision_request_detection_does_not_expand_feedback_or_negation(text, expected):
    from management.plan_assistance import requested_day
    assert requested_day(text, {'kind': 'daily_plan', 'date': DAY}, DAY) == expected


@pytest.mark.parametrize('allowed', [['create_plan'], ['revise_plan']])
def test_background_schema_uses_only_the_allowed_plan_command(tmp_path, monkeypatch, allowed):
    from test_ai_conversations_v3 import RPC
    monkeypatch.setattr(ai, 'find_codex', lambda *args: 'synthetic-codex.exe')
    monkeypatch.setattr(ai, '_AppServer', RPC)
    monkeypatch.setattr(ai, 'project_directory', lambda settings: tmp_path)
    monkeypatch.setattr(RPC, 'resume_error', None)
    ai.generate({'prompt': 'Synthetic planning request', 'context': {}, 'plan_requested': True,
                 'allowed_commands': allowed}, {'enabled': True}, threading.Event())
    rpc = RPC.instances[-1]
    turn = next(params for name, params in rpc.requests if name == 'turn/start')
    schema = turn['outputSchema']['properties']['actions']
    assert schema['maxItems'] == 1
    assert schema['items']['properties']['command']['enum'] == allowed
    assert rpc.closed
