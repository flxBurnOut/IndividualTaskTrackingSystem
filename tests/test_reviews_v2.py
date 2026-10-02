"""Structured review behavior over isolated synthetic plans and evidence."""
import json
import uuid

import pytest

from management.core import Core
from management.reviews import legacy_checkin, query_daily, query_weekly, submit_daily
from management.schemas import BusinessError


@pytest.fixture
def core(tmp_path):
    return Core(tmp_path / 'synthetic-review-v2')


def command(core, name, payload):
    state = core.query('state')
    return core.command(name, payload, request_id=str(uuid.uuid4()), epoch=state['epoch'], expected_revision=state['revision'])['result']


def task(core, title='Synthetic task'):
    return command(core, 'create', {'type': 'task', 'title': title})['entity']


def plan(core, targets, day='2030-01-01', mode='no_precise_time'):
    return command(core, 'create_plan', {'date': day, 'mode': mode, 'blocks': [
        {'target_id': target['id'], 'minutes': 20} for target in targets]})['entity']


def daily(core, day='2030-01-01'):
    with core.store.connect() as c:
        return query_daily(core, c, {'date': day})


def weekly(core, start='2030-01-01', end='2030-01-07', **extra):
    with core.store.connect() as c:
        return query_weekly(core, c, {'start': start, 'end': end, **extra})


def mutate(core, function, payload):
    with core.store.lock, core.store.connect() as c:
        c.execute('BEGIN IMMEDIATE')
        try:
            result = function(core, c, payload, str(uuid.uuid4()))
            core.store.set_meta(c, 'revision', core.store.meta(c, 'revision') + 1)
            c.commit()
            return result
        except Exception:
            c.rollback()
            raise


def submit(core, current_plan, answers):
    return mutate(core, submit_daily, {'date': current_plan['data']['date'], 'plan_id': current_plan['id'],
                  'plan_version': current_plan['version'], 'answers': answers})


def fact(core, target, dimensions, day='2030-01-01'):
    return command(core, 'record_feedback', {'target_id': target['id'], 'business_date': day,
                   'dimensions': dimensions, 'source_text': 'Synthetic explicit statement'})['entity']


def count(core, kind):
    return core.query('list', type=kind)['total']


def test_no_plan_queries_never_create_questions_or_reviews(core):
    before = core.query('state')
    for _ in range(3):
        result = daily(core)
        assert not result['has_plan'] and result['needs_codex'] and result['items'] == []
        assert weekly(core)['summary']['days_without_plan'] == 7
    assert core.query('state') == before
    assert count(core, 'checkin') == count(core, 'review') == 0


def test_actual_feedback_history_pages_all_original_facts_without_plan_metrics(core):
    target = task(core, 'Unplanned task')
    first = fact(core, target, {'completion': 'partial', 'actual_minutes': 12})
    other = fact(core, target, {'attendance': 'absent'})
    corrected = command(core, 'record_feedback', {'target_id': target['id'], 'business_date': '2030-01-01',
        'dimensions': {'completion': 'done', 'actual_minutes': 25},
        'source_text': 'Explicit correction; the full task is now complete.', 'supersedes_id': first['id']})['entity']
    fact(core, target, {'completion': 'incomplete'}, day='2030-01-02')
    command(core, 'delete_task', {'id': target['id'], 'version': target['version']})
    before = core.query('state')
    pages = [core.query('actual_feedback', date='2030-01-01', limit=1, offset=offset) for offset in range(3)]
    assert all(page['total'] == 3 and len(page['items']) == 1 for page in pages)
    assert [page['next_offset'] for page in pages] == [1, 2, None]
    rows = [page['items'][0] for page in pages]
    assert [row['id'] for row in rows] == [corrected['id'], other['id'], first['id']]
    assert all(row['target_title'] == 'Unplanned task' and row['target_archived'] for row in rows)
    assert rows[0]['supersedes_id'] == first['id']
    assert rows[2]['dimensions'] == first['data']['dimensions']
    assert rows[2]['source_text'] == first['data']['source_text'] and rows[2]['actual_minutes'] == 12
    assert rows[1]['actual_minutes'] is None and 'completion' not in rows[1]['dimensions']
    assert not daily(core)['has_plan'] and daily(core)['summary']['total'] == 0
    assert weekly(core)['coverage']['confirmed_completion_rate'] is None
    assert count(core, 'plan') == count(core, 'review') == 0
    assert core.query('state') == before


@pytest.mark.parametrize('params', [{}, {'date': 'invalid'}, {'date': '2030-01-01', 'limit': 0},
                                  {'date': '2030-01-01', 'limit': 101}, {'date': '2030-01-01', 'offset': -1},
                                  {'date': '2030-01-01', 'offset': True}])
def test_actual_feedback_history_requires_exact_date_and_bounded_paging(core, params):
    with pytest.raises(BusinessError) as error:
        core.query('actual_feedback', **params)
    assert error.value.code == 'validation'


def test_latest_valid_plan_uses_rowid_for_equal_timestamps(core, monkeypatch):
    import management.core as module
    a, b = task(core, 'Old target'), task(core, 'Current target')
    ids = iter(['ffffffff-ffff-4fff-8fff-ffffffffffff', '00000000-0000-4000-8000-000000000001'])
    monkeypatch.setattr(module, 'new_id', lambda: next(ids))
    old, current = plan(core, [a]), plan(core, [b])
    with core.store.connect() as c:
        c.execute("UPDATE entities SET created_at='2030-01-01T00:00:00.000Z' WHERE type='plan'")
    result = daily(core)
    assert result['plan']['id'] == current['id']
    assert [item['target_id'] for item in result['items']] == [b['id']]


def test_legacy_duplicate_blocks_have_one_review_item(core):
    t = task(core)
    current = plan(core, [t])
    current['data']['blocks'].append(dict(current['data']['blocks'][0]))
    with core.store.connect() as c:
        c.execute('UPDATE entities SET data=? WHERE id=?', (json.dumps(current['data']), current['id']))
    result = daily(core)
    assert len(result['items']) == result['summary']['total'] == 1
    assert result['summary']['unreported'] == 1
    assert result['items'][0]['planned_minutes'] == 20


def test_only_selected_done_and_incomplete_results_become_feedback(core):
    a, b, c = task(core, 'A'), task(core, 'B'), task(core, 'C')
    current = plan(core, [a, b, c])
    result = submit(core, current, [{'target_id': a['id'], 'result': 'done'}, {'target_id': b['id'], 'result': 'incomplete'}])
    assert result['changed_count'] == 2 and count(core, 'feedback') == 2 and count(core, 'review') == 1
    state = daily(core)
    assert {k: state['summary'][k] for k in ['done', 'incomplete', 'unreported']} == {'done': 1, 'incomplete': 1, 'unreported': 1}
    for row in core.query('list', type='feedback')['items']:
        assert set(row['data']['dimensions']) == {'completion'}
    assert core.query('get', id=a['id'])['entity']['status'] == 'active'


def test_repeated_answers_with_distinct_request_ids_do_not_duplicate(core):
    t = task(core)
    current = plan(core, [t])
    answers = [{'target_id': t['id'], 'result': 'done'}]
    first = submit(core, current, answers)
    for _ in range(8):
        again = submit(core, current, answers)
        assert again['changed_count'] == 0 and again['feedback_ids'] == []
        assert again['review']['id'] == first['review']['id']
        assert again['review']['version'] == first['review']['version']
    assert count(core, 'feedback') == count(core, 'review') == 1


def test_correction_keeps_other_dimensions_and_causal_history(core):
    t = task(core)
    current = plan(core, [t])
    old = fact(core, t, {'completion': 'partial', 'attendance': 'absent', 'submission': 'not_submitted', 'mastery': 'unknown', 'actual_minutes': 12})
    first = submit(core, current, [{'target_id': t['id'], 'result': 'done'}])
    latest = core.query('get', id=first['feedback_ids'][0])['entity']
    assert latest['data']['supersedes_id'] == old['id']
    assert latest['data']['dimensions'] == {'completion': 'done'}
    assert core.query('get', id=old['id'])['entity']['data']['dimensions']['actual_minutes'] == 12
    second = submit(core, current, [{'target_id': t['id'], 'result': 'incomplete'}])
    assert second['review']['id'] == first['review']['id'] and second['review']['version'] == 2
    assert core.query('get', id=second['feedback_ids'][0])['entity']['data']['supersedes_id'] == latest['id']
    with core.store.connect() as connection:
        history = connection.execute("SELECT before_value FROM changes WHERE entity_id=? AND action='daily_review'", (first['review']['id'],)).fetchall()
    assert len(history) == 1 and json.loads(history[0][0])['data']['results'][t['id']]['completion'] == 'done'


def test_empty_submission_does_not_create_an_empty_review(core):
    current = plan(core, [task(core)])
    result = submit(core, current, [])
    assert result['review'] is None and result['changed_count'] == 0
    assert count(core, 'feedback') == count(core, 'review') == 0


@pytest.mark.parametrize('variant', ['duplicate', 'outside', 'unknown', 'extra_dimension'])
def test_invalid_answer_scope_is_atomic(core, variant):
    t, outside = task(core), task(core)
    current = plan(core, [t])
    good = {'target_id': t['id'], 'result': 'done'}
    answers = {
        'duplicate': [good, good], 'outside': [good, {'target_id': outside['id'], 'result': 'done'}],
        'unknown': [{'target_id': t['id'], 'result': None}],
        'extra_dimension': [{'target_id': t['id'], 'result': 'done', 'mastery': 'verified'}],
    }[variant]
    with pytest.raises(BusinessError):
        submit(core, current, answers)
    assert count(core, 'feedback') == count(core, 'review') == 0


def test_replaced_plan_and_wrong_version_are_rejected(core):
    t = task(core)
    old = plan(core, [t])
    current = plan(core, [t])
    for candidate in (old, {**current, 'version': current['version'] + 1}):
        with pytest.raises(BusinessError) as error:
            submit(core, candidate, [{'target_id': t['id'], 'result': 'done'}])
        assert error.value.code == 'review_plan_conflict'
    assert count(core, 'feedback') == 0


def test_submit_without_plan_explains_manual_feedback_and_writes_nothing(core):
    with pytest.raises(BusinessError) as error:
        mutate(core, submit_daily, {'date': '2030-01-01', 'plan_id': 'none', 'plan_version': 1, 'answers': []})
    assert error.value.code == 'review_no_plan' and error.value.details['needs_codex']
    assert '记录实际情况' in error.value.message and '文字小结' in error.value.message
    assert 'Codex' not in error.value.message
    assert core.query('list')['total'] == 0


def test_legacy_completion_stays_distinct_and_later_other_dimension_does_not_erase(core):
    t = task(core)
    plan(core, [t])
    old = fact(core, t, {'completion': 'partial'})
    fact(core, t, {'attendance': 'attended'})
    result = daily(core)
    item = result['items'][0]
    assert item['result'] is None and item['raw_result'] == item['original_completion'] == 'partial'
    assert item['feedback_id'] == old['id'] and item['reported']
    assert result['summary']['other_reported'] == 1 and result['summary']['unreported'] == 0
    assert result['summary']['original_results'] == {'partial': 1}


def test_object_done_status_does_not_invent_dated_completion(core):
    t = task(core)
    plan(core, [t])
    command(core, 'update', {'id': t['id'], 'version': t['version'], 'patch': {'status': 'done'}})
    assert daily(core)['items'][0]['result'] is None
    assert daily(core)['summary']['unreported'] == 1


def test_weekly_uses_latest_plan_and_day_target_denominator(core):
    t, obsolete = task(core, 'Current'), task(core, 'Obsolete')
    plan(core, [obsolete])
    monday = plan(core, [t])
    tuesday = plan(core, [t], '2030-01-02')
    submit(core, monday, [{'target_id': t['id'], 'result': 'done'}])
    submit(core, tuesday, [{'target_id': t['id'], 'result': 'incomplete'}])
    result = weekly(core, end='2030-01-03')
    assert result['summary']['planned'] == 2
    assert result['summary']['done'] == result['summary']['incomplete'] == 1
    assert result['summary']['days_with_plan'] == 2 and result['summary']['days_without_plan'] == 1
    assert result['summary']['unreported'] == 0
    assert result['coverage']['confirmed_completion_rate'] == .5
    assert result['coverage']['missing_plan_is_failure'] is False
    assert {item['target_id'] for item in result['items']} == {t['id']}


def test_weekly_unknown_has_no_zero_completion_rate_and_read_creates_nothing(core):
    plan(core, [task(core)])
    before = core.query('state')
    result = weekly(core)
    assert result['coverage']['confirmed_completion_rate'] is None
    assert result['summary']['unreported'] == 1 and result['summary']['days_without_plan'] == 6
    assert core.query('state') == before and count(core, 'review') == 0


def test_weekly_paging_does_not_truncate_summary(core):
    targets = [task(core, str(i)) for i in range(3)]
    plan(core, targets)
    plan(core, targets, '2030-01-02')
    result = weekly(core, end='2030-01-02', limit=2)
    assert len(result['items']) == 2 and result['summary']['planned'] == 6
    assert result['coverage']['next_offset'] == 2 and not result['coverage']['items_complete']
    second = weekly(core, end='2030-01-02', limit=2, offset=2)
    assert second['summary'] == result['summary']


def test_legacy_checkin_repeated_plan_clicks_reuse_one_record(core):
    targets = [task(core, 'A'), task(core, 'B')]
    plan(core, targets)
    first = mutate(core, legacy_checkin, {'date': '2030-01-01'})
    for _ in range(5):
        again = mutate(core, legacy_checkin, {'date': '2030-01-01'})
        assert again['reused'] and again['entity']['id'] == first['entity']['id']
    assert count(core, 'checkin') == 1 and count(core, 'feedback') == 0


def test_legacy_without_plan_does_not_construct_questionnaire_from_backlog(core):
    task(core)
    result = mutate(core, legacy_checkin, {'date': '2030-01-01'})
    assert result == {'entity': None, 'needs_codex': True, 'reused': False}
    assert count(core, 'checkin') == 0


def test_legacy_explicit_targets_without_plan_remain_available_and_order_independent(core):
    a, b = task(core, 'A'), task(core, 'B')
    first = mutate(core, legacy_checkin, {'date': '2030-01-01', 'target_ids': [a['id'], b['id']]})
    again = mutate(core, legacy_checkin, {'date': '2030-01-01', 'target_ids': [b['id'], a['id']]})
    assert first['entity'] and not first['needs_codex']
    assert again['reused'] and again['entity']['id'] == first['entity']['id']
    assert count(core, 'checkin') == 1


def test_rest_plan_is_not_missing_plan_and_creates_no_empty_checkin(core):
    plan(core, [], mode='rest')
    result = daily(core)
    assert result['has_plan'] and not result['needs_codex'] and result['summary']['total'] == 0
    check = mutate(core, legacy_checkin, {'date': '2030-01-01'})
    assert check['entity'] is None and not check['needs_codex']
    assert count(core, 'checkin') == 0


def test_new_plan_gets_distinct_review_record_but_same_facts_are_not_copied(core):
    t = task(core)
    first_plan = plan(core, [t])
    first = submit(core, first_plan, [{'target_id': t['id'], 'result': 'done'}])
    second_plan = command(core,'revise_plan',{'date':'2030-01-01','plan_id':first_plan['id'],'plan_version':first_plan['version'],
        'mode':'no_precise_time','blocks':first_plan['data']['blocks']})['entity']
    second = submit(core, second_plan, [{'target_id': t['id'], 'result': 'done'}])
    assert second['changed_count'] == 0
    assert second['review']['id'] != first['review']['id']
    assert count(core, 'review') == 2 and count(core, 'feedback') == 1


def test_old_duplicate_checkins_reuse_latest_scope_without_deletion(core):
    t = task(core)
    current = plan(core, [t])
    body = {'type': 'checkin', 'title': 'Synthetic old checkin', 'data': {
        'date': '2030-01-01', 'source_kind': 'plan', 'questions': [
            {'id': 'question-1', 'target_id': t['id'], 'target_version': t['version'],
             'gate_id': 'completion', 'title': t['title'], 'question': 'Synthetic gate', 'number': 1}]}}
    original = mutate(core, lambda core, c, p, rid: core._create(c, p, rid), body)
    latest = mutate(core, lambda core, c, p, rid: core._create(c, p, rid), body)
    with core.store.connect() as c:
        c.execute("UPDATE entities SET created_at=? WHERE type='checkin'", (current['created_at'],))
    result = mutate(core, legacy_checkin, {'date': '2030-01-01'})
    assert result['reused'] and result['entity']['id'] == latest['id']
    assert count(core, 'checkin') == 2
    assert core.query('get', id=original['id'])['entity']['id'] == original['id']


def test_old_unbound_checkin_before_replacement_plan_is_not_reused(core):
    t = task(core)
    previous = plan(core, [t])
    body = {'type': 'checkin', 'title': 'Synthetic old checkin', 'data': {
        'date': '2030-01-01', 'questions': [{'id': 'q', 'target_id': t['id']}]}}
    old = mutate(core, lambda core, c, p, rid: core._create(c, p, rid), body)
    current = plan(core, [t])
    with core.store.connect() as c:
        c.execute("UPDATE entities SET created_at='2030-01-01T00:00:00.000Z' WHERE id IN (?,?)", (old['id'], current['id']))
    result = mutate(core, legacy_checkin, {'date': '2030-01-01'})
    assert not result['reused'] and result['entity']['id'] != old['id']
    assert count(core, 'checkin') == 2


def test_core_routes_queries_and_deduplicated_submission(core):
    t = task(core)
    current = plan(core, [t])
    result = core.query('daily_review', date='2030-01-01')
    assert result['plan']['id'] == current['id'] and result['epoch']
    payload = {'date': '2030-01-01', 'plan_id': current['id'], 'plan_version': current['version'],
               'answers': [{'target_id': t['id'], 'result': 'incomplete'}]}
    first = command(core, 'submit_daily_review', payload)
    second = command(core, 'submit_daily_review', payload)
    assert first['changed_count'] == 1 and second['changed_count'] == 0
    assert count(core, 'feedback') == count(core, 'review') == 1
    assert core.query('weekly_review', start='2030-01-01', end='2030-01-07')['summary']['incomplete'] == 1

def test_plan_and_explicit_same_targets_share_one_checkin_scope(core):
    t = task(core)
    plan(core, [t])
    first = mutate(core, legacy_checkin, {'date': '2030-01-01'})
    explicit = mutate(core, legacy_checkin, {'date': '2030-01-01', 'target_ids': [t['id']]})
    assert explicit['reused'] and explicit['entity']['id'] == first['entity']['id']
    assert count(core, 'checkin') == 1

def test_daily_feedback_lookup_ignores_unrelated_bodies_and_never_decodes_full_feedback(core, monkeypatch):
    planned, unrelated = task(core, 'Planned'), task(core, 'Unrelated')
    plan(core, [planned])
    chosen = fact(core, planned, {'completion': 'partial'})
    for index in range(12):
        command(core, 'record_feedback', {'target_id': unrelated['id'], 'business_date': '2030-01-01',
            'dimensions': {'completion': 'done'}, 'source_text': 'Synthetic irrelevant body ' + str(index) + ('x' * 32000)})
    original = core.store.entity
    def reject_full_feedback(row):
        if row is not None and 'type' in row.keys() and row['type'] == 'feedback':
            raise AssertionError('review lookup decoded a full feedback body')
        return original(row)
    monkeypatch.setattr(core.store, 'entity', reject_full_feedback)
    result = daily(core)
    assert result['items'][0]['feedback_id'] == chosen['id']
    assert result['items'][0]['raw_result'] == 'partial'
    assert result['summary']['other_reported'] == 1 and result['summary']['done'] == 0


def test_feedback_lookup_over_300_targets_uses_bounded_seeks_and_empty_set_reads_nothing(core):
    from management.reviews import _completion_feedback
    pairs = []
    for index in range(305):
        target = task(core, 'Synthetic scoped ' + str(index))
        day = '2030-01-%02d' % (index // 100 + 1)
        fact(core, target, {'completion': 'done'}, day)
        pairs.append((day, target['id']))
    statements = []
    with core.store.connect() as c:
        c.set_trace_callback(statements.append)
        assert _completion_feedback(core, c, '2030-01-01', '2030-01-04', []) == {}
        assert statements == []
        result = _completion_feedback(core, c, '2030-01-01', '2030-01-04', pairs + [pairs[0]])
    assert len(result) == 305 and len(statements) == 305
    assert all(set(record) == {'id', 'data'} and record['data'] == {'dimensions': {'completion': 'done'}} for record in result.values())
    assert all('LIMIT 1' in sql and len(sql) < 1000 for sql in statements)
