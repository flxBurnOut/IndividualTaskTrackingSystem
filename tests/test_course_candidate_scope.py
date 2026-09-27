"""Scoped candidate validation and adoption share the same business boundary."""
import json
import uuid

import pytest

from management import conversations
from management.core import Core
from management.schemas import BusinessError
from management.storage import encode
from context_harness import ready


DAY = '2038-05-01'


def command(core, name, payload):
    state = core.query('state')
    return core.command(name, payload, request_id=str(uuid.uuid4()),
                        epoch=state['epoch'], expected_revision=state['revision'])['result']


def create(core, kind, title, **extra):
    return command(core, 'create', {'type': kind, 'title': title, **extra})['entity']


@pytest.fixture
def fixture(tmp_path):
    core = Core(tmp_path / 'courses')
    a = create(core, 'course', 'Course A')
    b = create(core, 'course', 'Course B')
    first = create(core, 'task', 'A task', parent_id=a['id'])
    other = create(core, 'task', 'B task', parent_id=b['id'])
    command(core, 'settings', {'settings': {'ai': {'enabled': True}}})
    return core, a, b, first, other


def begin(core, owner, *, explicit_empty=False):
    payload = {'scope': {'kind': 'course', 'entity_id': owner['id']},
               'text': 'Discuss only this course.'}
    if explicit_empty:
        payload['source_ids'] = []
    return command(core, 'send_message', payload)['job']


def proposal(name, payload):
    return {'summary': 'Synthetic scoped proposal', 'unknowns': [], 'sources': [],
            'actions': [{'command': name, 'payload': payload, 'reason': 'Explicit synthetic request'}]}


def completion(target, name='set_task_completion'):
    payload = {'target_id': target['id'], 'business_date': DAY}
    if name == 'set_task_completion':
        payload.update(target_version=target['version'], result='done')
    else:
        payload.update(dimensions={'completion': 'done'}, source_text='Explicit result')
    return proposal(name, payload)


def validate(core, job, candidate):
    candidate = {**candidate, 'actions': [
        {**{k:v for k,v in action.items() if k != 'payload'}, 'payload_json':json.dumps(action['payload'])}
        for action in candidate['actions']]}
    return core.query('validate_candidate', operation_id=job['input']['context_operation_id'],
                      proposal=candidate)


def pending(core, job, candidate):
    # A persisted candidate may predate this fix; adoption must independently
    # enforce the same scope even when no new validation tool is called.
    ready(core, job, planning=any(a['command'] in {'create_plan','revise_plan','add_to_plan'}
                                 for a in candidate['actions']))
    with core.store.lock, core.store.connect() as c:
        c.execute('BEGIN IMMEDIATE')
        c.execute("UPDATE jobs SET status='awaiting_review',result=? WHERE id=?",
                  (encode(candidate), job['id']))
        conversations.complete_job(core, c, core._job(c, job['id']), candidate)
        c.commit()


@pytest.mark.parametrize('name', ['record_feedback', 'set_task_completion'])
@pytest.mark.parametrize('explicit_empty', [False, True])
@pytest.mark.parametrize('stage', ['validate', 'adopt'])
def test_other_course_feedback_rejected_even_without_selected_materials(fixture, name, explicit_empty, stage):
    core, a, _, _, other = fixture
    job = begin(core, a, explicit_empty=explicit_empty)
    candidate = completion(other, name)
    if stage == 'adopt':
        pending(core, job, candidate)
    before = core.query('state')
    with pytest.raises(BusinessError) as exc:
        if stage == 'validate':
            validate(core, job, candidate)
        else:
            command(core, 'apply_proposal', {'id': job['id']})
    assert exc.value.code == 'course_scope'
    assert core.query('state') == before
    assert core.query('list', type='feedback')['total'] == 0
    assert core.query('get', id=other['id'])['entity'] == other


@pytest.mark.parametrize('name', ['record_feedback', 'set_task_completion'])
@pytest.mark.parametrize('explicit_empty', [False, True])
def test_same_course_completion_can_be_validated_and_adopted(fixture, name, explicit_empty):
    core, a, _, first, _ = fixture
    job = begin(core, a, explicit_empty=explicit_empty)
    candidate = completion(first, name)
    assert validate(core, job, candidate)['valid']
    assert core.query('list', type='feedback')['total'] == 0
    pending(core, job, candidate)
    command(core, 'apply_proposal', {'id': job['id']})
    records = core.query('list', type='feedback')['items']
    assert len(records) == 1
    assert records[0]['data']['target_id'] == first['id']


@pytest.mark.parametrize('name', ['create_plan', 'revise_plan', 'add_to_plan'])
def test_course_cannot_replace_a_plan_containing_other_course(fixture, name):
    core, a, _, first, other = fixture
    old = command(core, 'create_plan', {'date': DAY, 'mode': 'no_precise_time',
                   'blocks': [{'target_id': other['id']}]})['entity']
    job = begin(core, a)
    payload = {'date': DAY, 'mode': 'no_precise_time', 'blocks': [{'target_id': first['id']}]}
    if name != 'create_plan':
        payload.update(plan_id=old['id'], plan_version=old['version'])
    if name == 'add_to_plan':
        payload = {k:v for k,v in payload.items() if k not in {'blocks','mode'}}
        payload['target_id'] = first['id']
    pending(core, job, proposal(name, payload))
    before = core.query('state')
    with pytest.raises(BusinessError) as exc:
        command(core, 'apply_proposal', {'id': job['id']})
    assert exc.value.code == 'course_scope'
    assert core.query('state') == before
    assert core.query('daily_review', date=DAY)['plan']['id'] == old['id']


def test_daily_review_cannot_record_another_course_completion(fixture):
    core, a, _, _, other = fixture
    plan = command(core, 'create_plan', {'date':DAY, 'mode':'no_precise_time',
                   'blocks':[{'target_id':other['id']}]})['entity']
    candidate = proposal('submit_daily_review', {'date':DAY, 'plan_id':plan['id'],
                 'plan_version':plan['version'], 'answers':[{'target_id':other['id'],'result':'done'}]})
    job = begin(core, a)
    pending(core, job, candidate)
    with pytest.raises(BusinessError) as exc:
        command(core, 'apply_proposal', {'id':job['id']})
    assert exc.value.code == 'course_scope'
    assert core.query('list', type='feedback')['total'] == 0


def test_scope_failure_rolls_back_earlier_valid_feedback(fixture):
    core, a, _, first, other = fixture
    candidate = completion(first)
    candidate['actions'] += completion(other)['actions']
    job = begin(core, a)
    pending(core, job, candidate)
    before = core.query('state')
    with pytest.raises(BusinessError, match='范围'):
        command(core, 'apply_proposal', {'id':job['id']})
    assert core.query('state') == before
    assert core.query('list', type='feedback')['total'] == 0


@pytest.mark.parametrize('name', ['create_plan', 'revise_plan', 'add_to_plan'])
def test_same_course_plan_operations_remain_available(fixture, name):
    core, a, _, first, _ = fixture
    second = create(core, 'task', 'Another A task', parent_id=a['id'])
    old = command(core, 'create_plan', {'date':DAY,'mode':'no_precise_time',
                  'blocks':[{'target_id':first['id']}]})['entity']
    payload = {'date':DAY, 'mode':'no_precise_time', 'blocks':[{'target_id':second['id']}]}
    if name != 'create_plan':
        payload.update(plan_id=old['id'],plan_version=old['version'])
    if name == 'add_to_plan':
        payload = {k:v for k,v in payload.items() if k not in {'mode','blocks'}}
        payload['target_id'] = second['id']
    job = begin(core, a)
    pending(core, job, proposal(name, payload))
    command(core, 'apply_proposal', {'id':job['id']})
    current=core.query('daily_review',date=DAY)['plan']
    assert current['id'] != old['id']
    current=core.query('get',id=current['id'])['entity']
    assert second['id'] in {b['target_id'] for b in current['data']['blocks']}


def test_scoped_daily_answer_only_changes_its_course_in_a_mixed_plan(fixture):
    core, a, _, first, other = fixture
    plan=command(core,'create_plan',{'date':DAY,'mode':'no_precise_time',
                 'blocks':[{'target_id':first['id']},{'target_id':other['id']}]})['entity']
    job=begin(core,a)
    pending(core,job,proposal('submit_daily_review',{'date':DAY,'plan_id':plan['id'],
        'plan_version':plan['version'],'answers':[{'target_id':first['id'],'result':'done'}]}))
    command(core,'apply_proposal',{'id':job['id']})
    records=core.query('list',type='feedback')['items']
    assert len(records)==1 and records[0]['data']['target_id']==first['id']


def test_course_does_not_acquire_global_review_authority(fixture):
    core,a,_,_,_=fixture
    job=begin(core,a,explicit_empty=True)
    pending(core,job,proposal('save_review',{'start':DAY,'end':DAY,'text':'Global review'}))
    with pytest.raises(BusinessError) as exc:
        command(core,'apply_proposal',{'id':job['id']})
    assert exc.value.code=='course_scope'
    assert core.query('list',type='review')['total']==0


@pytest.mark.parametrize('mode', ['rest', 'no_precise_time'])
@pytest.mark.parametrize('name', ['create_plan','revise_plan','add_to_plan'])
def test_course_cannot_replace_an_empty_global_plan(fixture, mode, name):
    core,a,_,first,_=fixture
    plan=command(core,'create_plan',{'date':DAY,'mode':mode,'blocks':[]})['entity']
    job=begin(core,a,explicit_empty=True)
    payload={'date':DAY,'mode':'no_precise_time','blocks':[{'target_id':first['id']}]}
    if name!='create_plan':payload.update(plan_id=plan['id'],plan_version=plan['version'])
    if name=='add_to_plan':
        payload={k:v for k,v in payload.items() if k not in {'mode','blocks'}}
        payload['target_id']=first['id']
    candidate=proposal(name,payload)
    ready(core,job,planning=True)
    with pytest.raises(BusinessError) as error:validate(core,job,candidate)
    assert error.value.code=='course_scope'
    pending(core,job,candidate)
    with pytest.raises(BusinessError) as error:command(core,'apply_proposal',{'id':job['id']})
    assert error.value.code=='course_scope'
    assert core.query('daily_review',date=DAY)['plan']['id']==plan['id']


def test_course_cannot_declare_whole_day_rest(fixture):
    core,a,_,_,_=fixture
    job=begin(core,a)
    candidate=proposal('create_plan',{'date':DAY,'mode':'rest','blocks':[]})
    with pytest.raises(BusinessError) as error:validate(core,job,candidate)
    assert error.value.code=='course_scope'
    assert core.query('list',type='plan')['total']==0


def test_course_can_remove_its_own_nonrest_plan_targets(fixture):
    core,a,_,first,_=fixture
    plan=command(core,'create_plan',{'date':DAY,'mode':'no_precise_time',
                 'blocks':[{'target_id':first['id']}]})['entity']
    job=begin(core,a)
    candidate=proposal('revise_plan',{'date':DAY,'mode':'no_precise_time','blocks':[],
                      'plan_id':plan['id'],'plan_version':plan['version']})
    pending(core,job,candidate)
    command(core,'apply_proposal',{'id':job['id']})
    current=core.query('today',date=DAY)['current_plan_id']
    assert core.query('get',id=current)['entity']['data']['blocks']==[]


@pytest.mark.parametrize('foreign',[False,True])
def test_course_child_task_keeps_legal_parenting_without_materials(fixture, foreign):
    core,a,_,first,other=fixture
    parent=other if foreign else first
    job=begin(core,a,explicit_empty=True)
    candidate=proposal('create',{'type':'task','title':'Synthetic child','parent_id':parent['id']})
    if foreign:
        with pytest.raises(BusinessError) as error:validate(core,job,candidate)
        assert error.value.code=='course_scope'
    else:
        assert validate(core,job,candidate)['valid']
        pending(core,job,candidate)
        command(core,'apply_proposal',{'id':job['id']})
        assert core.query('list',type='task',parent_id=first['id'])['total']==1
