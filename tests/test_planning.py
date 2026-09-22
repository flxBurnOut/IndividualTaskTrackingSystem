"""Synthetic evidence and scope tests; no current user's schedules are loaded."""
import uuid

import pytest

from management.core import Core
from management.planning import validate_scoped_rules, warning_scan
from management.schemas import BusinessError


@pytest.fixture
def core(tmp_path):
    return Core(tmp_path / 'synthetic-planning')


def cmd(core, name, payload):
    state = core.query('state')
    return core.command(name, payload, request_id=str(uuid.uuid4()), epoch=state['epoch'], expected_revision=state['revision'])


def create(core, type, title='Synthetic', parent=None, data=None, status=None):
    payload = {'type': type, 'title': title, 'parent_id': parent, 'data': data or {}}
    if status:
        payload['status'] = status
    return cmd(core, 'create', payload)['result']['entity']


def rule(core, kind, parent=None, **data):
    return create(core, 'rule', parent=parent, data={'rule_kind': kind, **data})


def validate(core, blocks, day='2030-01-01'):
    with core.store.connect() as c:
        return validate_scoped_rules(core, c, day, blocks, {})


def scan(core, day):
    with core.store.connect() as c:
        return warning_scan(core, c, day)


def feedback(core, entity, day, dimensions):
    return cmd(core, 'record_feedback', {'target_id': entity['id'], 'business_date': day,
               'dimensions': dimensions, 'source_text': 'Synthetic explicit statement'})['result']['entity']


def test_capacity_scopes_include_all_descendants_only(core):
    domain = create(core, 'domain')
    project = create(core, 'project', parent=domain['id'])
    nested = create(core, 'project', parent=project['id'])
    task = create(core, 'task', parent=nested['id'])
    outside = create(core, 'task')
    r = rule(core, 'capacity', parent=domain['id'], minutes=30)
    result = validate(core, [{'target_id': task['id'], 'minutes': 20}, {'target_id': outside['id'], 'minutes': 120}])
    assert result['applied_rules'][0]['rule_id'] == r['id']
    assert result['applied_rules'][0]['known_minutes'] == 20
    with pytest.raises(BusinessError) as error:
        validate(core, [{'target_id': task['id'], 'minutes': 31}])
    assert error.value.code == 'capacity'


def test_global_capacity_type_and_task_kind_filters(core):
    study = create(core, 'task', data={'task_kind': 'study'})
    chore = create(core, 'task', data={'task_kind': 'chore'})
    event = create(core, 'event', data={'date': '2030-01-01', 'event_kind': 'lecture'})
    rule(core, 'capacity', minutes=30, target_types=['task'], task_kind=['study'])
    result = validate(core, [{'target_id': study['id'], 'minutes': 20}, {'target_id': chore['id'], 'minutes': 90}, {'target_id': event['id'], 'minutes': 60}])
    assert result['applied_rules'][0]['target_ids'] == [study['id']]
    assert result['applied_rules'][0]['known_minutes'] == 20


def test_protected_time_is_scoped_and_overnight(core):
    course = create(core, 'course')
    project = create(core, 'project', parent=course['id'])
    inside = create(core, 'task', parent=project['id'])
    outside = create(core, 'task')
    rule(core, 'protected_time', parent=course['id'], start='23:00', end='07:00')
    validate(core, [{'target_id': outside['id'], 'start': '23:30', 'end': '23:55'}])
    with pytest.raises(BusinessError) as error:
        validate(core, [{'target_id': inside['id'], 'start': '06:00', 'end': '06:30'}])
    assert error.value.code == 'schedule_conflict'
    validate(core, [{'target_id': inside['id'], 'start': '07:00', 'end': '07:30'}])


def test_unknown_duration_and_untimed_blocks_remain_diagnosed(core):
    task = create(core, 'task')
    rule(core, 'capacity', minutes=30)
    rule(core, 'protected_time', start='23:00', end='07:00')
    result = validate(core, [{'target_id': task['id']}])
    assert len(result['unknowns']) == 2
    assert all(x['target_ids'] == [task['id']] for x in result['unknowns'])


def test_scoped_planning_rules_never_reject_reported_reality(core):
    task = create(core, 'task')
    rule(core, 'capacity', minutes=10)
    record = feedback(core, task, '2030-01-01', {'actual_minutes': 120, 'completion': 'partial'})
    assert record['data']['dimensions']['actual_minutes'] == 120


def test_expired_or_disabled_rules_do_not_apply(core):
    task = create(core, 'task')
    rule(core, 'capacity', minutes=0, effective_until='2029-12-31')
    rule(core, 'capacity', minutes=0, enabled=False)
    assert validate(core, [{'target_id': task['id'], 'minutes': 60}])['applied_rules'] == []


def test_warning_filters_apply_before_unknown_dates(core):
    exam = create(core, 'event', data={'event_kind': 'exam'})
    create(core, 'event', data={'event_kind': 'lecture'})
    create(core, 'task')
    rule(core, 'warning', target_types=['event'], event_kind='exam', days_before=14)
    risks = scan(core, '2030-01-01')
    assert [r['id'] for r in risks] == [exam['id']]
    assert risks[0]['severity'] == 'unknown'


def test_warning_task_kind_and_ancestor_scope(core):
    domain = create(core, 'domain')
    project = create(core, 'project', parent=domain['id'])
    target = create(core, 'task', parent=project['id'], data={'task_kind': 'assignment', 'due_date': '2030-01-22'})
    create(core, 'task', parent=project['id'], data={'task_kind': 'chore'})
    rule(core, 'warning', parent=domain['id'], target_types=['task'], task_kind=['assignment'], days_before=14)
    assert scan(core, '2030-01-07') == []
    assert scan(core, '2030-01-08')[0]['id'] == target['id']


@pytest.mark.parametrize('year,boundary,before', [(2029, '2029-02-28', '2029-02-27'), (2028, '2028-02-29', '2028-02-28')])
def test_calendar_month_warning_uses_real_month_boundary(core, year, boundary, before):
    create(core, 'task', data={'due_date': str(year) + '-03-31'})
    rule(core, 'warning', target_types=['task'], calendar_months_before=1)
    assert scan(core, before) == []
    assert len(scan(core, boundary)) == 1


def test_recurring_warning_uses_occurrences_and_cancellation(core):
    event = create(core, 'event', data={'date': '2030-01-03', 'event_kind': 'lecture', 'recurrence': 'weekly',
                                     'exceptions': {'2030-01-10': {'cancelled': True}}})
    rule(core, 'warning', target_types=['event'], event_kind='lecture', days_before=1)
    feedback(core, event, '2030-01-03', {'attendance': 'attended'})
    assert scan(core, '2030-01-09') == []
    risks = scan(core, '2030-01-16')
    assert len(risks) == 1 and risks[0]['occurrence_date'] == '2030-01-17'
    assert risks[0]['occurrence_count'] == 1


def test_attendance_only_closes_that_occurrence_not_mastery(core):
    event = create(core, 'event', data={'date': '2030-01-03', 'event_kind': 'exam', 'recurrence': 'weekly'})
    rule(core, 'warning', target_types=['event'], days_before=1)
    record = feedback(core, event, '2030-01-03', {'attendance': 'attended'})
    risks = scan(core, '2030-01-11')
    assert len(risks) == 1 and risks[0]['occurrence_date'] == '2030-01-10'
    assert record['data']['dimensions'] == {'attendance': 'attended'}
    assert core.query('get', id=event['id'])['entity']['status'] == 'active'


def test_absence_or_unrelated_feedback_does_not_close_event(core):
    event = create(core, 'event', data={'date': '2030-01-03', 'event_kind': 'lecture'})
    rule(core, 'warning', target_types=['event'])
    feedback(core, event, '2030-01-03', {'attendance': 'absent', 'mastery': 'verified'})
    assert len(scan(core, '2030-01-04')) == 1


def test_event_feedback_uses_business_timezone_date(core):
    event = create(core, 'event', data={'date': '2029-12-31', 'start': '23:30', 'end': '23:59',
                                     'timezone': 'UTC', 'event_kind': 'exam'})
    rule(core, 'warning', target_types=['event'])
    assert scan(core, '2030-01-01')[0]['due_date'] == '2030-01-01'
    feedback(core, event, '2030-01-01', {'attendance': 'attended'})
    assert scan(core, '2030-01-02') == []


def test_explicit_end_status_and_missing_rule_do_not_remind(core):
    create(core, 'event', status='done', data={'date': '2030-01-03', 'event_kind': 'exam'})
    create(core, 'event', status='cancelled', data={'date': '2030-01-03'})
    rule(core, 'warning', target_types=['event'])
    assert scan(core, '2030-01-04') == []


def test_warning_scan_not_limited_to_first_task_page(core):
    rule(core, 'warning', target_types=['task'])
    for index in range(105):
        create(core, 'task', title='Synthetic ' + str(index), data={'due_date': '2030-01-01'})
    assert len(scan(core, '2030-01-02')) == 105


def test_huge_recurring_range_reports_incomplete_coverage(core, monkeypatch):
    import management.planning as planning
    monkeypatch.setattr(planning, 'MAX_OCCURRENCES_PER_ENTITY', 2)
    create(core, 'event', data={'date': '2030-01-01', 'recurrence': 'daily', 'event_kind': 'lecture'})
    rule(core, 'warning', target_types=['event'])
    risks = scan(core, '2030-01-05')
    assert any(r.get('occurrences_complete') is False and r['severity'] == 'unknown' for r in risks)


def test_same_millisecond_feedback_uses_insert_order_not_random_id(core, monkeypatch):
    event = create(core, 'event', data={'date': '2030-01-03', 'event_kind': 'exam'})
    rule(core, 'warning', target_types=['event'])
    import management.core as core_module
    ids = iter(['ffffffff-ffff-4fff-8fff-ffffffffffff', '00000000-0000-4000-8000-000000000001', '11111111-1111-4111-8111-111111111111'])
    monkeypatch.setattr(core_module, 'new_id', lambda: next(ids))
    first = feedback(core, event, '2030-01-03', {'attendance': 'attended'})
    corrected = feedback(core, event, '2030-01-03', {'attendance': 'absent'})
    with core.store.connect() as c:
        c.execute("UPDATE entities SET created_at='2030-01-03T00:00:00.000+00:00' WHERE id IN (?,?)", (first['id'], corrected['id']))
    assert len(scan(core, '2030-01-04')) == 1
    last = feedback(core, event, '2030-01-03', {'attendance': 'attended'})
    with core.store.connect() as c:
        c.execute("UPDATE entities SET created_at='2030-01-03T00:00:00.000+00:00' WHERE id=?", (last['id'],))
    assert scan(core, '2030-01-04') == []


def test_event_owner_scope_is_explicit_and_keeps_event_outside_tree(core):
    course = create(core, 'course')
    project = create(core, 'project', parent=course['id'])
    event = create(core, 'event', data={'event_kind':'exam','owner_id':project['id']})
    create(core, 'event', data={'event_kind':'exam'})
    unrelated = create(core, 'event', data={'event_kind':'exam'})
    cmd(core, 'link', {'source_id':unrelated['id'],'target_id':course['id'],'kind':'references'})
    rule(core, 'warning', parent=course['id'], target_types=['event'], event_kind='exam', days_before=14)
    risks = scan(core, '2030-01-01')
    assert [r['id'] for r in risks] == [event['id']]
    assert core.query('get',id=event['id'])['entity']['parent_id'] is None
    rule(core, 'capacity', parent=course['id'], target_types=['event'], minutes=30)
    with pytest.raises(BusinessError) as error:
        validate(core,[{'target_id':event['id'],'minutes':60}])
    assert error.value.code == 'capacity'
    assert validate(core,[{'target_id':unrelated['id'],'minutes':60}])['applied_rules'] == []
