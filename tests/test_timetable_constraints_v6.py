"""One adopted weekly source governs all views and hard planning constraints.

All dates, sources and records are synthetic. No source parsing or AI/network is
required: these tests start at a user's explicit adoption boundary.
"""
import datetime as dt
import uuid

import pytest

from management.core import Core
from management.planning import _occurrences as warning_occurrences
from management.recurring import _occurrences as preparation_occurrences
from management.schemas import BusinessError


@pytest.fixture
def core(tmp_path):
    return Core(tmp_path / 'synthetic-timetable-constraints')


def command(core, name, payload):
    state = core.query('state')
    return core.command(name, payload, request_id=str(uuid.uuid4()), epoch=state['epoch'], expected_revision=state['revision'])


def create(core, kind, title='Synthetic', **data):
    return command(core, 'create', {'type': kind, 'title': title, 'data': data})['result']['entity']


def adopt(core, rows=None, **fields):
    payload = {'title': 'Synthetic semester timetable', 'semester_start': '2030-01-07',
               'semester_end': '2030-02-10', 'timezone': 'Asia/Shanghai',
               'source_text': 'Synthetic user-confirmed weekly source',
               'rows': rows or [{'key': 'lecture-a', 'title': 'Synthetic lecture', 'weekday': 2,
                                'start': '09:30', 'end': '11:00', 'teaching_weeks': [1, 3, 5]}]}
    payload.update(fields)
    return command(core, 'apply_timetable', payload)['result']


def context(core, day):
    return core.query('plan_context', date=day)


def event_ids(core, day):
    return {event['id'] for event in context(core, day)['hard_events']}


def change_data(core, entity, **fields):
    return command(core, 'update', {'id': entity['id'], 'version': entity['version'], 'patch': {'data': fields}})['result']['entity']


def test_selected_teaching_weeks_are_identical_in_daily_warning_and_preparation(core):
    result = adopt(core)
    event = result['rows'][0]
    expected = ['2030-01-09', '2030-01-23', '2030-02-06']
    dates = [dt.date(2030, 1, 7) + dt.timedelta(days=n) for n in range(35)]
    actual = [day.isoformat() for day in dates if event['id'] in event_ids(core, day.isoformat())]
    assert actual == expected
    assert [day.isoformat() for day in warning_occurrences(event['data'], dt.date(2030, 2, 10))] == expected
    assert [day.isoformat() for _, day in preparation_occurrences(event, dt.date(2030, 1, 7), dt.date(2030, 2, 10))] == expected
    assert core.query('list', type='task')['total'] == 0
    assert core.query('list', type='plan')['total'] == 0
    assert core.query('list', type='feedback')['total'] == 0


def test_even_weeks_do_not_force_first_week_origin_to_occur(core):
    event = adopt(core, rows=[{'key': 'even-lab', 'title': 'Even weeks lab', 'weekday': 2,
                             'start': '14:00', 'end': '16:00', 'teaching_weeks': [2, 4]}])['rows'][0]
    assert event['data']['date'] == '2030-01-09'
    assert event['id'] not in event_ids(core, '2030-01-09')
    assert event['id'] in event_ids(core, '2030-01-16')
    assert event['id'] not in event_ids(core, '2030-01-23')
    assert event['id'] in event_ids(core, '2030-01-30')
    assert [day.isoformat() for day in warning_occurrences(event['data'], dt.date(2030, 2, 10))] == ['2030-01-16', '2030-01-30']


def test_holiday_cancels_only_that_occurrence_and_does_not_renumber_weeks(core):
    event = adopt(core)['rows'][0]
    event = change_data(core, event, exceptions={
        '2030-01-23': {'cancelled': True},
        '2030-01-16': {'start': '12:00', 'end': '13:00'},
        '2030-02-06': {'start': '12:00', 'end': '13:00'},
    })
    assert event['id'] not in event_ids(core, '2030-01-16'), 'A time override must not resurrect a non-teaching week'
    assert event['id'] not in event_ids(core, '2030-01-23')
    assert event['id'] not in event_ids(core, '2030-01-30'), 'Holiday cancellation does not shift teaching-week numbers'
    occurrence = next(item for item in context(core, '2030-02-06')['hard_events'] if item['id'] == event['id'])
    assert (occurrence['start_minute'], occurrence['end_minute']) == (720, 780)
    assert [day.isoformat() for day in warning_occurrences(event['data'], dt.date(2030, 2, 10))] == ['2030-01-09', '2030-02-06']
    assert [day.isoformat() for _, day in preparation_occurrences(event, dt.date(2030, 1, 7), dt.date(2030, 2, 10))] == ['2030-01-09', '2030-02-06']


def test_term_end_is_inclusive_and_weekdays_remain_source_local(core):
    # A source-local Sunday in week 1 appears on the business-local Monday in
    # week 2. Teaching-week filtering must happen before timezone conversion.
    event = adopt(core, semester_start='2029-12-31', semester_end='2030-01-06',
                  timezone='America/Los_Angeles', rows=[{'key': 'source-sunday', 'title': 'Source Sunday class',
                  'weekday': 6, 'start': '18:00', 'end': '19:00', 'teaching_weeks': [1]}])['rows'][0]
    assert event['data']['date'] == '2030-01-06'
    assert event['id'] in event_ids(core, '2030-01-07')
    assert event['id'] not in event_ids(core, '2030-01-14')
    occurrence = next(item for item in context(core, '2030-01-07')['hard_events'] if item['id'] == event['id'])
    assert occurrence['occurrence_date'] == '2030-01-06'
    assert (occurrence['start_minute'], occurrence['end_minute']) == (600, 660)


def test_international_date_line_event_is_not_omitted_or_called_complete(core):
    command(core, 'settings', {'settings': {'timezone': 'Pacific/Kiritimati'}})
    event = create(core, 'event', date='2030-01-06', start='23:00', end='23:30',
                   timezone='Etc/GMT+12', recurrence='none', hard=True)
    result = context(core, '2030-01-08')
    occurrence = next(item for item in result['hard_events'] if item['id'] == event['id'])
    assert (occurrence['start_minute'], occurrence['end_minute']) == (60, 90)
    assert occurrence['occurrence_date'] == '2030-01-06'
    assert result['coverage']['hard_constraints_complete']


def test_overnight_from_source_day_two_days_earlier_keeps_both_business_fragments(core):
    command(core, 'settings', {'settings': {'timezone': 'Pacific/Kiritimati'}})
    event = create(core, 'event', date='2030-01-06', start='21:30', end='00:30',
                   timezone='Etc/GMT+12', recurrence='none', hard=True)
    first = next(item for item in context(core, '2030-01-07')['hard_events'] if item['id'] == event['id'])
    second = next(item for item in context(core, '2030-01-08')['hard_events'] if item['id'] == event['id'])
    assert (first['start_minute'], first['end_minute']) == (1410, 1440)
    assert (second['start_minute'], second['end_minute']) == (0, 150)
    assert first['occurrence_date'] == second['occurrence_date'] == '2030-01-06'


@pytest.mark.parametrize('day,start,end', [('2030-03-10', '02:30', '03:30'), ('2030-11-03', '01:30', '02:30')])
def test_dst_nonexistent_or_ambiguous_clock_is_not_a_proven_free_slot(core, day, start, end):
    command(core, 'settings', {'settings': {'timezone': 'America/New_York'}})
    event = create(core, 'event', date=day, start=start, end=end, timezone='America/New_York', hard=True)
    result = context(core, day)
    assert any(item['id'] == event['id'] for item in result['unknowns'])
    assert not any(item['id'] == event['id'] and item['start_minute'] is not None for item in result['hard_events'])
    task = create(core, 'task')
    with pytest.raises(BusinessError) as error:
        command(core, 'create_plan', {'date': day, 'blocks': [{'target_id': task['id'], 'start': '12:00', 'end': '12:15'}]})
    assert error.value.code == 'uncertain_time'


def test_overlapping_fixed_rows_remain_visible_and_both_constrain_daily_plan(core):
    result = adopt(core, rows=[
        {'key': 'lecture-a', 'title': 'First fixed class', 'weekday': 2, 'start': '09:00', 'end': '10:00'},
        {'key': 'lab-b', 'title': 'Second fixed class', 'weekday': 2, 'start': '09:30', 'end': '10:30'},
    ])
    events = context(core, '2030-01-09')['hard_events']
    assert {event['id'] for event in events} == {row['id'] for row in result['rows']}
    assert sorted((event['start_minute'], event['end_minute']) for event in events) == [(540, 600), (570, 630)]
    windows = context(core, '2030-01-09')['free_windows']
    assert not any(window['start_minute'] < 630 and window['end_minute'] > 540 for window in windows)
    task = create(core, 'task')
    with pytest.raises(BusinessError) as error:
        command(core, 'create_plan', {'date': '2030-01-09', 'blocks': [{'target_id': task['id'], 'start': '10:10', 'end': '10:20'}]})
    assert error.value.code == 'schedule_conflict'


def test_same_adopted_course_clock_always_blocks_plan_even_outside_candidate_task_page(core):
    event = adopt(core)['rows'][0]
    tasks = [create(core, 'task', title=f'Synthetic candidate {index}') for index in range(105)]
    result = context(core, '2030-01-09')
    assert result['coverage']['tasks_paged']
    assert event['id'] in {item['id'] for item in result['hard_events']}
    with pytest.raises(BusinessError) as error:
        command(core, 'create_plan', {'date': '2030-01-09', 'blocks': [{'target_id': tasks[-1]['id'], 'start': '09:30', 'end': '10:00'}]})
    assert error.value.code == 'schedule_conflict'


def test_unknown_event_clock_keeps_source_calendar_date_without_invented_timezone_conversion(core):
    command(core, 'settings', {'settings': {'timezone': 'Pacific/Kiritimati'}})
    event = create(core, 'event', date='2030-01-08', timezone='Etc/GMT+12', time_kind='date_only', hard=True)
    result = context(core, '2030-01-08')
    assert any(item['id'] == event['id'] for item in result['unknowns'])
    assert any(item['id'] == event['id'] and item['start_minute'] is None for item in result['hard_events'])


def test_weekly_projection_matches_the_same_daily_hard_occurrences_without_writes(core):
    adopt(core, rows=[
        {'key': 'monday', 'title': 'Monday class', 'weekday': 0, 'start': '08:00', 'end': '09:00'},
        {'key': 'wednesday', 'title': 'Wednesday class', 'weekday': 2, 'start': '10:00', 'end': '11:00', 'teaching_weeks': [1, 3]},
    ])
    create(core, 'event', title='Other fixed appointment', date='2030-01-10', start='15:00', end='16:00', hard=True)
    before = core.query('state')
    weekly = core.query('timetable_week', week_start='2030-01-07')
    assert len(weekly['days']) == 7
    for day in weekly['days']:
        expected = context(core, day['date'])['hard_events']
        projection = lambda items: sorted((item['id'], item['occurrence_date'], item['start_minute'], item['end_minute']) for item in items)
        assert projection(day['events']) == projection(expected)
    assert core.query('state')['revision'] == before['revision']


def test_discussion_candidate_apply_receipt_and_reopened_client_share_one_schedule(core):
    from management import conversations
    from management.storage import encode
    table = command(core, 'create', {'type':'timetable', 'title':'Synthetic upload discussion'})['result']['entity']
    source = command(core, 'add_source', {'owner_id':table['id'], 'kind':'notice',
        'text':'Synthetic confirmed source: teaching week 1 Monday is 2030-01-07; Mondays 09:00-10:00 through 2030-02-03, every week.'})['result']['entity']
    command(core, 'settings', {'settings':{'ai':{'enabled':True}}})
    scope = {'kind':'timetable', 'entity_id':table['id']}
    sent = command(core, 'send_message', {'scope':scope, 'text':'Use the supplied dates and weekly class; prepare a candidate for my confirmation.'})['result']
    job_id = sent['job']['id']
    assert sent['job']['input']['source_owner_scope']==table['id']
    from context_harness import rows
    assert [r['id'] for r in rows(core,sent['job'],'materials')]==[source['id']]
    assert core.query('list', type='event')['total'] == 0
    candidate = {'summary':'Synthetic model candidate; no model invoked', 'unknowns':[], 'sources':[source['id']],
        'actions':[{'command':'apply_timetable', 'payload':{
            'id':table['id'], 'version':table['version'], 'title':table['title'],
            'semester_start':'2030-01-07', 'semester_end':'2030-02-03', 'timezone':'Asia/Shanghai',
            'source_text':'Synthetic notice explicitly states term dates and Monday class.',
            'rows':[{'key':'monday-lecture', 'title':'Synthetic Monday lecture', 'weekday':0,
                     'start':'09:00', 'end':'10:00', 'source_text':'Saved notice: Mondays 09:00-10:00'}]}}]}
    from context_harness import ready
    ready(core,job_id)
    with core.store.lock, core.store.connect() as c:
        c.execute('BEGIN IMMEDIATE')
        job = core._job(c, job_id)
        c.execute("UPDATE jobs SET status='awaiting_review',result=? WHERE id=?", (encode(candidate), job_id))
        assert conversations.complete_job(core, c, job, candidate)
        c.commit()
    # A separate entrance recovers the persisted conversation/candidate, then
    # adopts it in the same transaction and receipt path as the native client.
    reopened = Core(core.root)
    before = reopened.query('state')
    request_id = str(uuid.uuid4())
    applied = reopened.command('apply_proposal', {'id':job_id}, request_id=request_id,
        epoch=before['epoch'], expected_revision=before['revision'])
    adopted = applied['result']['results'][0]
    operation=sent['job']['input']['context_operation_id']
    assert adopted['entity']['data']['source_context_operation']==operation
    assert adopted['rows'][0]['data']['source_context_operation']==operation
    with core.store.connect() as c:
        assert dict(c.execute('SELECT entity_id,version FROM context_sources WHERE operation_id=?',(operation,)))=={source['id']:source['version']}
    assert core.query('receipt', request_id=request_id)['found']
    assert core.query('job', id=job_id)['job']['status'] == 'applied'
    replay = core.command('apply_proposal', {'id':job_id}, request_id=request_id,
        epoch=before['epoch'], expected_revision=before['revision'])
    assert replay['replayed'] and replay['result'] == applied['result']
    assert core.query('list', type='event')['total'] == 1
    assert core.query('list', type='plan')['total'] == 0
    assert core.query('list', type='feedback')['total'] == 0
    assert adopted['rows'][0]['id'] in event_ids(reopened, '2030-01-07')
    history = reopened.query('conversation', scope=scope)
    assert history['conversation']['id'] == sent['conversation']['id']
    assert any(message['proposal_state'] == 'applied' for message in history['messages'] if message['role'] == 'assistant')


@pytest.mark.parametrize('change', [
    {'start':'10:00', 'end':'10:00'},
    {'exceptions':{'2030-01-09':{'start':'10:00', 'end':'10:00'}}},
])
def test_other_editor_cannot_turn_timetable_zero_duration_into_all_day_block(core, change):
    event = adopt(core)['rows'][0]
    state = core.query('state')
    with pytest.raises(BusinessError) as error:
        change_data(core, event, **change)
    assert error.value.code == 'timetable_time'
    unchanged = core.query('get', id=event['id'])['entity']
    assert unchanged['data'] == event['data'] and unchanged['version'] == event['version']
    assert core.query('state')['revision'] == state['revision']


@pytest.mark.parametrize('cancelled', ['false', 0, None])
def test_non_boolean_holiday_does_not_silently_erase_a_hard_event(core, cancelled):
    event = adopt(core)['rows'][0]
    with pytest.raises(BusinessError) as error:
        change_data(core, event, exceptions={'2030-01-09':{'cancelled':cancelled}})
    assert error.value.code == 'validation'
    assert event['id'] in event_ids(core, '2030-01-09')
    restored = change_data(core, event, exceptions={'2030-01-09':{'cancelled':False}})
    assert restored['id'] in event_ids(core, '2030-01-09')


@pytest.mark.parametrize('day', ['2030-01-08', '2030-02-13'])
def test_single_class_exception_cannot_escape_confirmed_weekday_or_term(core, day):
    event = adopt(core)['rows'][0]
    with pytest.raises(BusinessError) as error:
        change_data(core, event, exceptions={day:{'cancelled':True}})
    assert error.value.code == 'timetable_exception'
    assert core.query('get', id=event['id'])['entity']['version'] == event['version']
