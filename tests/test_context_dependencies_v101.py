"""Dependency evidence remains bounded, directional and current across reads."""
import json
import uuid

import pytest

from management.core import Core
from management import context_service as cs
from management.schemas import BusinessError

DAY = '2038-05-01'


def command(core, name, payload):
    state = core.query('state')
    return core.command(name, payload, request_id=str(uuid.uuid4()), epoch=state['epoch'],
                        expected_revision=state['revision'])['result']


def task(core, title):
    return command(core, 'create', {'type': 'task', 'title': title})['entity']


def link(core, dependent, predecessor):
    command(core, 'link', {'source_id': dependent['id'], 'target_id': predecessor['id'], 'kind': 'depends_on'})


def read(core, name, **params):
    try:
        return core.query(name, **params)
    except BusinessError as error:
        if error.code != 'context_checkpoint_required':
            raise
        cs.compacted(core, params['operation_id'])  # Synthetic provider acknowledgement.
        return core.query(name, **params)


def details(core, op, identifier, byte_limit=None):
    offset, version, fragments = 0, None, []
    while True:
        result = read(core, 'read_context_item', operation_id=op, id=identifier,
                      offset=offset, version=version, byte_limit=byte_limit)
        assert cs.size(result) <= cs.MAX_PAGE_BYTES
        if 'item' in result:
            return result['item']
        fragments.append(result['json_fragment'])
        offset, version = result['next_offset'], result['version']
        if offset is None:
            return json.loads(''.join(fragments))


def context(core):
    op = core.query('prepare_context', goal='Synthetic dependency planning',
                    scope={'kind': 'daily_plan', 'date': DAY})['operation_id']
    for name in ('deadlines', 'rules', 'events', 'plans'):
        cursor = None
        while True:
            page = read(core, 'query_context', operation_id=op, collection=name, cursor=cursor)
            cursor = page['next_cursor']
            if cursor is None:
                break
    return op


def proposal(*entities):
    return {'summary': 'Synthetic plan', 'unknowns': [], 'sources': [], 'actions': [
        {'command': 'create_plan', 'reason': 'Synthetic explicit request',
         'payload_json': json.dumps({'date': DAY, 'mode': 'no_precise_time',
                                    'blocks': [{'target_id': e['id']} for e in entities]})}]}


@pytest.fixture
def setup(tmp_path):
    core = Core(tmp_path / 'data')
    a, b = task(core, 'Prerequisite'), task(core, 'Dependent')
    link(core, b, a)
    return core, a, b


def test_index_and_body_link_to_directional_dependency_evidence(setup):
    core, a, b = setup
    before = core.query('state')['revision']
    op = context(core)
    page = core.query('query_context', operation_id=op, collection='tasks')
    entry = next(row for row in page['items'] if row['id'] == b['id'])
    body = core.query('read_context_item', operation_id=op, id=b['id'])
    assert body['item'] == core.query('get', id=b['id'])['entity']
    assert entry['dependencies'] == body['dependencies']
    graph = details(core, op, body['dependencies']['id'])
    assert graph['entity_id'] == b['id'] and graph['as_of_date'] == DAY
    assert [row['id'] for row in graph['predecessors']] == [a['id']]
    assert not graph['predecessors'][0]['completed']
    assert graph['predecessors'][0]['completion_state'] == 'unknown'
    assert graph['dependents'] == []
    reverse = details(core, op, 'dependencies:' + a['id'])
    assert reverse['predecessors'] == []
    assert [row['id'] for row in reverse['dependents']] == [b['id']]
    assert core.query('state')['revision'] == before


def test_dependency_pages_cover_every_predecessor_and_reject_changed_links(tmp_path):
    core = Core(tmp_path / 'data')
    target = task(core, 'Many prerequisites')
    predecessors = [task(core, f'{index} ' + 'Synthetic prerequisite ' * 7) for index in range(25)]
    for predecessor in predecessors:
        link(core, target, predecessor)
    op = context(core)
    identifier = 'dependencies:' + target['id']
    first = core.query('read_context_item', operation_id=op, id=identifier, byte_limit=2048)
    assert not first['complete'] and first['next_offset']
    graph = details(core, op, identifier, byte_limit=2048)
    assert {r['id'] for r in graph['predecessors']} == {e['id'] for e in predecessors}
    current_entity_version = core.query('get', id=target['id'])['entity']['version']
    added = task(core, 'New prerequisite')
    link(core, target, added)
    assert core.query('get', id=target['id'])['entity']['version'] == current_entity_version
    with pytest.raises(BusinessError) as error:
        read(core, 'read_context_item', operation_id=op, id=identifier,
             offset=first['next_offset'], version=first['version'])
    assert error.value.code == 'context_changed'


def test_candidate_requires_current_complete_dependency_read_and_never_applies(setup):
    core, a, b = setup
    op = context(core)
    before = core.query('state')['revision']
    with pytest.raises(BusinessError) as error:
        core.query('validate_candidate', operation_id=op, proposal=proposal(a, b))
    assert error.value.code == 'context_coverage'
    assert error.value.details['id'] == 'dependencies:' + b['id']
    details(core, op, 'dependencies:' + b['id'])
    assert core.query('validate_candidate', operation_id=op, proposal=proposal(a, b))['valid']
    assert core.query('state')['revision'] == before
    assert core.query('list', type='plan')['total'] == 0


def test_partial_dependency_read_does_not_pass_coverage(setup):
    core, a, b = setup
    for index in range(8):
        target = task(core, f'Other prerequisite {index}')
        command(core, 'record_feedback', {'target_id': target['id'], 'business_date': DAY,
                                         'dimensions': {'completion': 'done'}, 'source_text': 'Synthetic confirmed'})
        link(core, b, target)
    op = context(core)
    first = core.query('read_context_item', operation_id=op, id='dependencies:' + b['id'], byte_limit=2048)
    assert not first['complete']
    with pytest.raises(BusinessError) as error:
        core.query('validate_candidate', operation_id=op, proposal=proposal(a, b))
    assert error.value.code == 'context_coverage'


def test_changed_dependency_completion_invalidates_evidence(setup):
    core, a, b = setup
    op = context(core)
    details(core, op, 'dependencies:' + b['id'])
    command(core, 'record_feedback', {'target_id': a['id'], 'business_date': DAY,
                                     'dimensions': {'completion': 'done'}, 'source_text': 'Synthetic explicit completion'})
    with pytest.raises(BusinessError) as error:
        core.query('validate_candidate', operation_id=op, proposal=proposal(b))
    assert error.value.code == 'context_changed'
    current = details(core, op, 'dependencies:' + b['id'])
    assert current['predecessors'][0]['completed']
    assert current['predecessors'][0]['completion_state'] == 'done'
    assert core.query('validate_candidate', operation_id=op, proposal=proposal(b))['valid']


def test_unrelated_dependency_change_does_not_invalidate_selected_evidence(setup):
    core, a, b = setup
    op = context(core)
    details(core, op, 'dependencies:' + b['id'])
    x, y = task(core, 'Unrelated A'), task(core, 'Unrelated B')
    link(core, y, x)
    assert core.query('validate_candidate', operation_id=op, proposal=proposal(a, b))['valid']


def test_planned_predecessor_does_not_become_completed(setup):
    core, a, b = setup
    command(core, 'create_plan', {'date': DAY, 'mode': 'no_precise_time', 'blocks': [{'target_id': a['id']}]})
    graph = details(core, context(core), 'dependencies:' + b['id'])
    assert graph['predecessors'][0]['completed'] is False


def test_append_checks_dependency_evidence_for_retained_future_blocks(setup):
    core, a, b = setup
    command(core, 'record_feedback', {'target_id': a['id'], 'business_date': DAY,
                                     'dimensions': {'completion': 'done'}, 'source_text': 'Synthetic explicit completion'})
    plan = command(core, 'create_plan', {'date': DAY, 'mode': 'no_precise_time', 'blocks': [{'target_id': b['id']}]})['entity']
    extra = task(core, 'Additional task')
    op = context(core)
    candidate = {'summary': 'Append', 'unknowns': [], 'sources': [], 'actions': [
        {'command': 'add_to_plan', 'reason': 'Synthetic request', 'payload_json': json.dumps(
            {'date': DAY, 'plan_id': plan['id'], 'plan_version': plan['version'], 'target_id': extra['id']})}]}
    with pytest.raises(BusinessError) as error:
        core.query('validate_candidate', operation_id=op, proposal=candidate)
    assert error.value.code == 'context_coverage'
    details(core, op, 'dependencies:' + b['id'])
    current_a = core.query('get', id=a['id'])['entity']
    command(core, 'update', {'id': a['id'], 'version': current_a['version'], 'patch': {'title': 'Corrected prerequisite'}})
    with pytest.raises(BusinessError) as error:
        core.query('validate_candidate', operation_id=op, proposal=candidate)
    assert error.value.code == 'context_changed'
    details(core, op, 'dependencies:' + b['id'])
    assert core.query('validate_candidate', operation_id=op, proposal=candidate)['valid']
    assert core.query('get', id=plan['id'])['entity'] == plan


def test_historical_blocks_do_not_require_new_dependency_evidence(setup):
    core, a, b = setup
    plan = command(core, 'create_plan', {'date': DAY, 'mode': 'no_precise_time',
                                       'blocks': [{'target_id': a['id']}, {'target_id': b['id']}]})['entity']
    command(core, 'record_feedback', {'target_id': b['id'], 'business_date': DAY,
                                     'dimensions': {'completion': 'incomplete'}, 'source_text': 'Synthetic past attempt'})
    op = context(core)
    candidate = {'summary': 'Preserve reported history', 'unknowns': [], 'sources': [], 'actions': [
        {'command': 'revise_plan', 'reason': 'Synthetic request', 'payload_json': json.dumps(
            {'date': DAY, 'mode': 'no_precise_time', 'plan_id': plan['id'], 'plan_version': plan['version'],
             'blocks': [plan['data']['blocks'][1]]})}]}
    assert core.query('validate_candidate', operation_id=op, proposal=candidate)['valid']
    assert core.query('get', id=plan['id'])['entity'] == plan
