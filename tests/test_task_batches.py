"""Transactional contracts for previewed manual batch creation and task splitting."""
import copy
import uuid

import pytest

from management.core import Core
from management.schemas import BusinessError


def command(core, name, payload, *, request_id=None, state=None):
    current = state or core.query('state')
    return core.command(name, payload, request_id=request_id or str(uuid.uuid4()),
                        epoch=current['epoch'], expected_revision=current['revision'])


def create(core, title, kind='task', **fields):
    return command(core, 'create', {'type': kind, 'title': title, **fields})['result']['entity']


def preview(core, *, mode='create', items=None, sequential=False, **fields):
    return core.query('preview_task_batch', mode=mode,
                      items=items if items is not None else rows(), sequential=sequential, **fields)


def apply(core, proposal, *, request_id=None, payload=None, state=None):
    return command(core, proposal['command'], payload if payload is not None else proposal['payload'],
                   request_id=request_id, state=state or proposal)


def rows(count=3):
    return [{'title': f'Child task {index + 1}', 'completion_gate': f'Checked result {index + 1}'} for index in range(count)]


def stored(core):
    """A complete business snapshot catches half-batches and hidden preview writes."""
    with core.store.connect() as connection:
        return {table: [tuple(row) for row in connection.execute(f'SELECT * FROM {table} ORDER BY rowid')]
                for table in ('entities', 'links', 'receipts', 'changes', 'meta')}


def tasks(core, parent_id=None):
    result = core.query('list', type='task', limit=100)['items']
    return [item for item in result if item['parent_id'] == parent_id]


def links(core):
    with core.store.connect() as connection:
        return {(row['source_id'], row['target_id'], row['kind']) for row in connection.execute('SELECT * FROM links')}


def expect_error(code, operation):
    with pytest.raises(BusinessError) as caught:
        operation()
    assert caught.value.code == code


def test_preview_is_read_only_and_exposes_reviewable_batch_without_guessed_fields(tmp_path):
    core = Core(tmp_path); before = stored(core)
    inputs = [{'title': 'One quick thought'}, {'title': 'Another thought', 'completion_gate': 'A stated result'}]
    proposal = preview(core, items=inputs)
    assert proposal['command'] == 'create_task_batch'
    assert proposal['payload']['preview_token']
    assert len(proposal['items']) == 2 and isinstance(proposal['warnings'], list)
    assert proposal['epoch'] == core.query('state')['epoch'] and proposal['revision'] == core.query('state')['revision']
    assert stored(core) == before
    assert inputs == [{'title': 'One quick thought'}, {'title': 'Another thought', 'completion_gate': 'A stated result'}]
    apply(core, proposal)
    result = tasks(core)
    assert {item['title'] for item in result} == {'One quick thought', 'Another thought'}
    assert all(item['status'] == 'active' for item in result)
    assert all(not item['data'].get(key) for item in result for key in ('due_date', 'scheduled_date', 'estimated_minutes'))
    assert links(core) == set()
    assert core.query('list', type='plan')['total'] == 0 and core.query('list', type='feedback')['total'] == 0


def test_create_batch_honors_selected_owner_explicit_fields_and_sequence(tmp_path):
    core = Core(tmp_path); owner = create(core, 'Synthetic course', 'course')
    inputs = rows(3)
    inputs[0].update(estimated_minutes=25, due_date='2035-02-07', scheduled_date='2035-02-06')
    proposal = preview(core, items=inputs, sequential=True, parent_id=owner['id'], parent_version=owner['version'])
    receipt = apply(core, proposal)
    result = {item['title']: item for item in tasks(core, owner['id'])}
    assert len(result) == 3
    first, second, third = [result[item['title']] for item in inputs]
    assert first['data']['estimated_minutes'] == 25
    assert first['data']['due_date'] == '2035-02-07' and first['data']['scheduled_date'] == '2035-02-06'
    assert links(core) == {(second['id'], first['id'], 'depends_on'), (third['id'], second['id'], 'depends_on')}
    assert core.query('get', id=owner['id'])['entity'] == owner
    assert receipt['revision'] == proposal['revision'] + 1


@pytest.mark.parametrize('mode,count', [('create', 1), ('create', 50), ('split', 2), ('split', 50)])
def test_supported_batch_size_boundaries(tmp_path, mode, count):
    core = Core(tmp_path)
    source = create(core, 'Original work', data={'completion_gate': 'Original full standard'}) if mode == 'split' else None
    fields = {'source_id': source['id'], 'source_version': source['version']} if source else {}
    proposal = preview(core, mode=mode, items=rows(count), **fields)
    assert proposal['command'] == ('split_task' if source else 'create_task_batch')
    apply(core, proposal)
    assert len(tasks(core, source['id'] if source else None)) == count


@pytest.mark.parametrize('mode,count', [('create', 0), ('create', 51), ('split', 0), ('split', 1), ('split', 51)])
def test_out_of_range_batches_reject_without_writes(tmp_path, mode, count):
    core = Core(tmp_path); source = create(core, 'Original work')
    fields = {'source_id': source['id'], 'source_version': source['version']} if mode == 'split' else {}
    before = stored(core)
    expect_error('validation', lambda: preview(core, mode=mode, items=rows(count), **fields))
    assert stored(core) == before


@pytest.mark.parametrize('bad_item', [
    {'title': ''}, {'title': '   '}, {'title': 'Bad estimate', 'estimated_minutes': 0},
    {'title': 'Bad estimate', 'estimated_minutes': True}, {'title': 'Bad date', 'due_date': 'tomorrow'},
    {'title': 'Bad scheduled date', 'scheduled_date': '2035-02-30'},
])
def test_invalid_row_rejects_whole_preview(tmp_path, bad_item):
    core = Core(tmp_path); before = stored(core)
    expect_error('validation', lambda: preview(core, items=[rows(1)[0], bad_item]))
    assert stored(core) == before


@pytest.mark.parametrize('gate', [None, '', '   '])
def test_split_requires_an_explicit_completion_standard_for_every_child(tmp_path, gate):
    core = Core(tmp_path); source = create(core, 'Original work', data={'completion_gate': 'Original standard'})
    items = rows(2)
    if gate is None: items[1].pop('completion_gate')
    else: items[1]['completion_gate'] = gate
    before = stored(core)
    expect_error('validation', lambda: preview(core, mode='split', source_id=source['id'], source_version=source['version'], items=items))
    assert stored(core) == before


@pytest.mark.parametrize('state', ['done', 'cancelled', 'draft', 'archived', 'feedback_done'])
def test_split_rejects_closed_or_unavailable_source_state(tmp_path, state):
    core = Core(tmp_path)
    source = create(core, 'Original work', status=state if state in {'done', 'cancelled', 'draft'} else 'active')
    if state == 'archived':
        source = command(core, 'archive', {'id': source['id'], 'version': source['version'], 'archived': True})['result']['entity']
    if state == 'feedback_done':
        day = core.query('task_pool')['date']
        command(core, 'record_feedback', {'target_id': source['id'], 'business_date': day,
                'dimensions': {'completion': 'done'}, 'source_text': 'Explicit synthetic completion'})
    before = stored(core)
    expect_error('validation', lambda: preview(core, mode='split', source_id=source['id'], source_version=source['version']))
    assert stored(core) == before


def test_split_preserves_source_plan_feedback_and_successors_while_inheriting_prerequisites(tmp_path):
    core = Core(tmp_path)
    prerequisite = create(core, 'Prerequisite')
    source = create(core, 'Original work', data={'completion_gate': 'Original complete deliverable', 'description': 'Keep original detail'})
    successor = create(core, 'Successor')
    for first, second in [(source, prerequisite), (successor, source)]:
        command(core, 'link', {'source_id': first['id'], 'target_id': second['id'], 'kind': 'depends_on'})
    day = core.query('task_pool')['date']
    feedback = command(core, 'record_feedback', {'target_id': source['id'], 'business_date': day,
                       'dimensions': {'completion': 'partial', 'submission': 'not_submitted'}, 'source_text': 'Explicit partial result'})['result']['entity']
    plan = command(core, 'create_plan', {'date': day, 'mode': 'no_precise_time',
                   'blocks': [{'target_id': prerequisite['id']}, {'target_id': source['id']}, {'target_id': successor['id']}]})['result']['entity']
    originals = {item['id']: core.query('get', id=item['id'])['entity'] for item in (source, prerequisite, successor, feedback, plan)}
    proposal = preview(core, mode='split', source_id=source['id'], source_version=source['version'], sequential=True)
    apply(core, proposal)
    children = {item['title']: item for item in tasks(core, source['id'])}
    assert len(children) == 3
    expected = {(source['id'], prerequisite['id'], 'depends_on'), (successor['id'], source['id'], 'depends_on')}
    expected.update((child['id'], source['id'], 'contributes') for child in children.values())
    expected.update((child['id'], prerequisite['id'], 'depends_on') for child in children.values())
    first, second, third = [children[item['title']] for item in rows()]
    expected.update({(second['id'], first['id'], 'depends_on'), (third['id'], second['id'], 'depends_on')})
    assert links(core) == expected
    for identifier, original in originals.items():
        assert core.query('get', id=identifier)['entity'] == original
    assert all(child['status'] == 'active' and child['data']['completion_gate'] for child in children.values())
    assert core.query('list', type='feedback')['total'] == 1
    # Finishing all children is not evidence that the original full standard was met.
    for child in children.values():
        command(core, 'record_feedback', {'target_id': child['id'], 'business_date': day,
                'dimensions': {'completion': 'done'}, 'source_text': 'Explicit child-only completion'})
    assert core.query('get', id=source['id'])['entity'] == originals[source['id']]
    assert core.query('get', id=source['id'], display=True)['entity']['completion_state'] == 'partial'


def test_repeated_split_adds_children_without_replacing_earlier_children_or_links(tmp_path):
    core = Core(tmp_path); source = create(core, 'Original work')
    first = preview(core, mode='split', source_id=source['id'], source_version=source['version'], items=rows(2), sequential=True)
    apply(core, first)
    earlier = {item['id']: item for item in tasks(core, source['id'])}; earlier_links = links(core)
    other_rows = [{'title': 'Another part A', 'completion_gate': 'Result A'}, {'title': 'Another part B', 'completion_gate': 'Result B'}]
    later = preview(core, mode='split', source_id=source['id'], source_version=source['version'], items=other_rows)
    apply(core, later)
    current = {item['id']: item for item in tasks(core, source['id'])}
    assert len(current) == 4 and all(current[identifier] == item for identifier, item in earlier.items())
    new_ids = set(current) - set(earlier)
    assert links(core) == earlier_links | {(identifier, source['id'], 'contributes') for identifier in new_ids}


@pytest.mark.parametrize('target', ['source', 'parent'])
def test_preview_rejects_stale_entity_versions(tmp_path, target):
    core = Core(tmp_path); entity = create(core, 'Original', 'task' if target == 'source' else 'project')
    command(core, 'update', {'id': entity['id'], 'version': entity['version'], 'patch': {'title': 'Updated'}})
    fields = {target + '_id': entity['id'], target + '_version': entity['version']}
    before = stored(core)
    expect_error('entity_conflict', lambda: preview(core, mode='split' if target == 'source' else 'create', **fields))
    assert stored(core) == before


@pytest.mark.parametrize('kind', ['event', 'assessment'])
def test_batch_rejects_illegal_parent_type(tmp_path, kind):
    core = Core(tmp_path); parent = create(core, 'Not a task container', kind)
    before = stored(core)
    expect_error('parent_type', lambda: preview(core, parent_id=parent['id'], parent_version=parent['version']))
    assert stored(core) == before


def test_batch_rejects_archived_parent(tmp_path):
    core = Core(tmp_path); parent = create(core, 'Old project', 'project')
    parent = command(core, 'archive', {'id': parent['id'], 'version': parent['version'], 'archived': True})['result']['entity']
    before = stored(core)
    expect_error('validation', lambda: preview(core, parent_id=parent['id'], parent_version=parent['version']))
    assert stored(core) == before


@pytest.mark.parametrize('mode', ['create', 'split'])
def test_batch_retry_replays_original_receipt_and_rejects_changed_payload(tmp_path, mode):
    core = Core(tmp_path); source = create(core, 'Original') if mode == 'split' else None
    fields = {'source_id': source['id'], 'source_version': source['version']} if source else {}
    proposal = preview(core, mode=mode, **fields); request_id = str(uuid.uuid4())
    receipt = apply(core, proposal, request_id=request_id)
    create(core, 'Unrelated later task')
    before_retry = stored(core)
    retried = apply(core, proposal, request_id=request_id)
    assert retried['replayed'] is True and retried['result'] == receipt['result'] and retried['revision'] == receipt['revision']
    assert stored(core) == before_retry
    changed = copy.deepcopy(proposal['payload']); changed['items'][0]['title'] = 'Changed retry'
    expect_error('idempotency_conflict', lambda: apply(core, proposal, request_id=request_id, payload=changed))
    assert stored(core) == before_retry


@pytest.mark.parametrize('change', ['title', 'token', 'missing_token', 'sequence'])
def test_modified_preview_payload_is_rejected_atomically(tmp_path, change):
    core = Core(tmp_path); proposal = preview(core)
    payload = copy.deepcopy(proposal['payload'])
    if change == 'title': payload['items'][0]['title'] = 'Not previewed'
    elif change == 'sequence': payload['sequential'] = not payload['sequential']
    elif change == 'token': payload['preview_token'] = 'invalid-preview'
    else: payload.pop('preview_token')
    before = stored(core)
    expect_error('preview_conflict', lambda: apply(core, proposal, payload=payload))
    assert stored(core) == before


def test_stale_revision_and_rebased_preview_are_both_rejected(tmp_path):
    core = Core(tmp_path); proposal = preview(core)
    create(core, 'Unrelated later change'); before = stored(core)
    expect_error('revision_conflict', lambda: apply(core, proposal))
    expect_error('preview_conflict', lambda: apply(core, proposal, state=core.query('state')))
    assert stored(core) == before


def test_wrong_epoch_does_not_write_or_accept_a_preview(tmp_path):
    core = Core(tmp_path); proposal = preview(core); before = stored(core)
    wrong_state = {'epoch': str(uuid.uuid4()), 'revision': proposal['revision']}
    expect_error('epoch_conflict', lambda: apply(core, proposal, state=wrong_state))
    assert stored(core) == before


@pytest.mark.parametrize('mode', ['create', 'split'])
def test_mid_batch_entity_failure_rolls_back_entities_links_receipts_and_revision(tmp_path, monkeypatch, mode):
    core = Core(tmp_path); source = create(core, 'Original') if mode == 'split' else None
    fields = {'source_id': source['id'], 'source_version': source['version']} if source else {}
    proposal = preview(core, mode=mode, sequential=True, **fields)
    before = stored(core); real_create = core._create; count = []
    def fail_second(connection, payload, request_id):
        count.append(payload['title'])
        if len(count) == 2: raise BusinessError('validation', 'Injected second item failure')
        return real_create(connection, payload, request_id)
    monkeypatch.setattr(core, '_create', fail_second)
    request_id = str(uuid.uuid4())
    expect_error('validation', lambda: apply(core, proposal, request_id=request_id))
    assert len(count) == 2 and stored(core) == before
    assert core.query('receipt', request_id=request_id)['found'] is False


@pytest.mark.parametrize('mode', ['create', 'split'])
def test_mid_batch_link_failure_rolls_back_entire_batch(tmp_path, monkeypatch, mode):
    core = Core(tmp_path); source = create(core, 'Original') if mode == 'split' else None
    fields = {'source_id': source['id'], 'source_version': source['version']} if source else {}
    proposal = preview(core, mode=mode, sequential=True, **fields)
    before = stored(core); real_dispatch = core._dispatch; count = []
    def fail_second_link(connection, name, payload, request_id, prepared=None):
        if name == 'link':
            count.append(payload.copy())
            if len(count) == 2: raise BusinessError('validation', 'Injected second link failure')
        return real_dispatch(connection, name, payload, request_id, prepared)
    monkeypatch.setattr(core, '_dispatch', fail_second_link)
    request_id = str(uuid.uuid4())
    expect_error('validation', lambda: apply(core, proposal, request_id=request_id))
    assert len(count) == 2 and stored(core) == before
    assert core.query('receipt', request_id=request_id)['found'] is False


def test_split_link_budget_allows_500_and_refuses_additional_sequence_links(tmp_path):
    core = Core(tmp_path); source = create(core, 'Original')
    for index in range(9):
        dependency = create(core, f'Prerequisite {index}')
        command(core, 'link', {'source_id': source['id'], 'target_id': dependency['id'], 'kind': 'depends_on'})
    baseline = stored(core)
    parameters = {'mode': 'split', 'source_id': source['id'], 'source_version': source['version'], 'items': rows(50)}
    expect_error('validation', lambda: preview(core, sequential=True, **parameters))
    assert stored(core) == baseline
    proposal = preview(core, **parameters)
    assert proposal['link_count'] == 500
    original_links = links(core)
    receipt = apply(core, proposal)
    assert len(tasks(core, source['id'])) == 50
    assert len(links(core) - original_links) == 500
    assert len(receipt['result']['links']) == 500


def test_batch_text_budget_counts_utf8_bytes_and_keeps_rejected_preview_read_only(tmp_path):
    core = Core(tmp_path)
    # Each individual standard is legal; thirteen CJK rows exceed 180 KiB in
    # UTF-8 even though the character count is well below that number.
    sizeable = [{'title': f'Work {index}', 'completion_gate': '验' * 5000} for index in range(13)]
    baseline = stored(core)
    expect_error('validation', lambda: preview(core, items=sizeable))
    assert stored(core) == baseline
    allowed = preview(core, items=sizeable[:10])
    apply(core, allowed)
    assert len(tasks(core)) == 10
    assert all(item['data']['completion_gate'] == '验' * 5000 for item in tasks(core))


@pytest.mark.parametrize('extra', ['status', 'parent_id', 'catchup_enabled', 'source_text'])
def test_batch_rejects_extra_row_fields_instead_of_silently_discarding_user_intent(tmp_path, extra):
    core = Core(tmp_path); items = rows(2); items[1][extra] = 'unreviewed value'
    baseline = stored(core)
    expect_error('validation', lambda: preview(core, items=items))
    assert stored(core) == baseline


def test_batch_rejects_unknown_top_level_fields(tmp_path):
    core = Core(tmp_path); baseline = stored(core)
    expect_error('validation', lambda: preview(core, auto_complete_parent=True))
    assert stored(core) == baseline


@pytest.mark.parametrize('invalid_mode', [[], {}, 'unknown'])
def test_preview_rejects_malformed_mode_as_a_business_validation_error(tmp_path, invalid_mode):
    core = Core(tmp_path); baseline = stored(core)
    expect_error('validation', lambda: preview(core, mode=invalid_mode))
    assert stored(core) == baseline


@pytest.mark.parametrize('state', ['cancelled', 'draft', 'archived'])
def test_split_refuses_unavailable_inherited_prerequisite(tmp_path, state):
    core = Core(tmp_path); source = create(core, 'Original'); dependency = create(core, 'Required first')
    command(core, 'link', {'source_id': source['id'], 'target_id': dependency['id'], 'kind': 'depends_on'})
    if state == 'archived':
        command(core, 'archive', {'id': dependency['id'], 'version': dependency['version'], 'archived': True})
    else:
        command(core, 'update', {'id': dependency['id'], 'version': dependency['version'], 'patch': {'status': state}})
    baseline = stored(core)
    expect_error('validation', lambda: preview(core, mode='split', source_id=source['id'], source_version=source['version']))
    assert stored(core) == baseline


def test_split_keeps_recovery_scope_and_progress_only_on_the_original_task(tmp_path):
    core = Core(tmp_path); course = create(core, 'Course', 'course')
    source = command(core, 'set_recovery_task', {'course_id': course['id'], 'title': 'Lecture 1-3',
        'completion_gate': 'Read and independently check all three lectures', 'unit': '讲',
        'total_quantity': 3, 'completed_quantity': 1, 'source_text': 'Explicitly one of three read',
        'reason': 'self_reported'})['result']['entity']
    original = core.query('get', id=source['id'])['entity']
    original_feedback = core.query('list', type='feedback')['items']
    summary_before = core.query('recovery_summary', task_id=source['id'])['items']
    proposal = preview(core, mode='split', source_id=source['id'], source_version=source['version'], items=rows(2))
    assert any('不分摊' in warning for warning in proposal['warnings'])
    apply(core, proposal)
    children = tasks(core, source['id'])
    assert len(children) == 2
    assert all(not any(key.startswith('catchup_') for key in child['data']) for child in children)
    assert all(child['data'].get('task_kind') != 'catchup' for child in children)
    assert core.query('get', id=source['id'])['entity'] == original
    assert core.query('list', type='feedback')['items'] == original_feedback
    assert core.query('recovery_summary', task_id=source['id'])['items'] == summary_before


def test_batch_respects_existing_hierarchy_depth_boundary(tmp_path):
    core = Core(tmp_path); chain = [create(core, 'Root task')]
    for index in range(30):
        chain.append(create(core, f'Depth {index + 2}', parent_id=chain[-1]['id']))
    allowed = preview(core, mode='split', source_id=chain[-1]['id'], source_version=chain[-1]['version'], items=rows(2))
    apply(core, allowed)
    deepest = tasks(core, chain[-1]['id'])[0]
    baseline = stored(core)
    expect_error('depth', lambda: preview(core, mode='split', source_id=deepest['id'], source_version=deepest['version'], items=rows(2)))
    assert stored(core) == baseline


@pytest.mark.parametrize('mode', ['create', 'split'])
def test_generic_undo_rejects_batch_without_archiving_a_partial_set(tmp_path, mode):
    core = Core(tmp_path); source = create(core, 'Original') if mode == 'split' else None
    fields = {'source_id': source['id'], 'source_version': source['version']} if source else {}
    receipt = apply(core, preview(core, mode=mode, **fields))
    baseline = stored(core)
    expect_error('undo_scope', lambda: command(core, 'undo', {'request_id': receipt['request_id']}))
    assert stored(core) == baseline
