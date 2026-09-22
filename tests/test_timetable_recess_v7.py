"""Synthetic teaching-week recess snapshots shared by all planning readers."""
import datetime as dt
import uuid

import pytest
from management.core import Core
from management.schemas import BusinessError
from management.timetable import normalize_timetable_defaults, occurs_on, validate_timetable_metadata
from management.planning import _occurrences as warning_occurrences
from management.recurring import _occurrences as preparation_occurrences


@pytest.fixture
def core(tmp_path):
    return Core(tmp_path / 'synthetic-recess-space')


def command(core, name, payload):
    state = core.query('state')
    return core.command(name, payload, request_id=str(uuid.uuid4()), epoch=state['epoch'], expected_revision=state['revision'])['result']


def row(key='lecture', **changes):
    value = {'key':key, 'title':'Synthetic lecture', 'weekday':2, 'start':'09:00', 'end':'10:00'}
    value.update(changes)
    return value


def adopt(core, **changes):
    value = {'title':'Synthetic teaching semester', 'semester_start':'2030-01-07', 'semester_end':'2030-02-17',
             'timezone':'Asia/Shanghai', 'week_numbering':'teaching', 'recess_weeks':['2030-01-21'],
             'source_text':'Explicit synthetic timetable and recess dates', 'rows':[row()]}
    value.update(changes)
    return command(core, 'apply_timetable', value)


def edit_row(event, **changes):
    value = {'key':event['data']['timetable_row_key'], 'event_id':event['id'], 'version':event['version']}
    value.update(changes)
    return value


def dates(data, start='2030-01-07', end='2030-02-17'):
    low = dt.date.fromisoformat(start); high = dt.date.fromisoformat(end)
    return [(low + dt.timedelta(days=n)).isoformat() for n in range((high-low).days+1)
            if occurs_on(data, low + dt.timedelta(days=n))]


def test_recess_skips_class_and_numbering_same_in_plan_warning_preparation_and_week(core):
    result = adopt(core, rows=[row(teaching_weeks=[1,3,5])]); event = result['rows'][0]
    expected = ['2030-01-09','2030-01-30','2030-02-13']
    assert dates(event['data']) == expected
    assert [day.isoformat() for day in warning_occurrences(event['data'], dt.date(2030,2,17))] == expected
    assert [day.isoformat() for _,day in preparation_occurrences(event,dt.date(2030,1,7),dt.date(2030,2,17))] == expected
    for day in ['2030-01-23','2030-01-30','2030-02-06','2030-02-13']:
        present = event['id'] in {item['id'] for item in core.query('plan_context',date=day)['hard_events']}
        assert present == (day in expected)
    recess = core.query('timetable_week',timetable_id=result['entity']['id'],week_start='2030-01-21')
    assert recess['is_recess'] and recess['week_number'] is None and not recess['events']
    after = core.query('timetable_week',timetable_id=result['entity']['id'],week_start='2030-01-28')
    assert not after['is_recess'] and after['week_number'] == 3 and after['week_numbering'] == 'teaching'
    assert after['events'][0]['data']['recess_weeks'] == ['2030-01-21']
    for kind in ('task','plan','feedback'):
        assert core.query('list',type=kind)['total'] == 0


def test_even_teaching_weeks_shift_after_recess_and_two_recesses_are_supported(core):
    result = adopt(core, recess_weeks=['2030-02-04','2030-01-21'], rows=[row(teaching_weeks=[2,4])])
    assert dates(result['rows'][0]['data']) == ['2030-01-16','2030-02-13']
    assert result['entity']['data']['recess_weeks'] == ['2030-01-21','2030-02-04']
    assert result['rows'][0]['data']['recess_weeks'] == result['entity']['data']['recess_weeks']


def test_every_week_excludes_recess_even_if_single_occurrence_override_says_not_cancelled(core):
    result = adopt(core, rows=[row(exceptions={'2030-01-23':{'cancelled':False,'start':'11:00','end':'12:00'}})])
    assert dates(result['rows'][0]['data']) == ['2030-01-09','2030-01-16','2030-01-30','2030-02-06','2030-02-13']


def test_calendar_numbering_explicit_recess_cancels_without_shifting_selected_weeks(core):
    result = adopt(core, week_numbering='calendar', rows=[row(teaching_weeks=[1,3,5])])
    assert dates(result['rows'][0]['data']) == ['2030-01-09','2030-02-06']


def test_legacy_missing_rules_remain_calendar_and_reimport_is_true_noop(core):
    payload = {'title':'Legacy synthetic semester','semester_start':'2030-01-07','semester_end':'2030-02-17',
               'timezone':'Asia/Shanghai','source_text':'Legacy confirmed source','rows':[row(teaching_weeks=[1,3,5])]}
    first = command(core,'apply_timetable',payload)
    assert 'week_numbering' not in first['entity']['data'] and 'recess_weeks' not in first['rows'][0]['data']
    assert dates(first['rows'][0]['data']) == ['2030-01-09','2030-01-23','2030-02-06']
    second = command(core,'apply_timetable',payload)
    assert second['reused'] and second['entity'] == first['entity'] and second['rows'] == first['rows']
    week = core.query('timetable_week',timetable_id=first['entity']['id'],week_start='2030-01-21')
    assert week['week_numbering'] == 'calendar' and week['week_number'] == 3


def test_reordered_recess_dates_and_duplicate_upload_reuse_table_and_rows(core):
    first = adopt(core,recess_weeks=['2030-02-04','2030-01-21'])
    second = adopt(core,recess_weeks=['2030-01-21','2030-02-04'])
    assert second['reused'] and second['rows'] == first['rows'] and second['entity'] == first['entity']


def test_incomplete_container_accepts_rules_without_guessing_anchor_dates(core):
    result = command(core,'create',{'type':'timetable','title':'Pending semester',
        'data':{'week_numbering':'teaching','recess_weeks':['2030-01-21']}})
    assert not result['entity']['data'].get('semester_start')
    assert not core.query('timetable_week',timetable_id=result['entity']['id'],week_start='2030-01-21')['events']


def test_global_defaults_allow_multiple_semesters_and_entities_reject_outside_snapshot():
    defaults = normalize_timetable_defaults({'recess_weeks':['2031-01-20','2030-01-21']})
    assert defaults == {'week_numbering':'teaching','recess_weeks':['2030-01-21','2031-01-20']}
    with pytest.raises(BusinessError) as error:
        validate_timetable_metadata({'semester_start':'2030-01-07','semester_end':'2030-02-17',**defaults})
    assert error.value.code == 'timetable_recess'


@pytest.mark.parametrize('changes',[
    {'week_numbering':'odd'}, {'recess_weeks':None}, {'recess_weeks':'2030-01-21'},
    {'recess_weeks':['2030-01-22']}, {'recess_weeks':['2030-01-21','2030-01-21']},
    {'recess_weeks':['2030-01-07']}, {'recess_weeks':['2029-12-31']},
    {'recess_weeks':['2030-02-18']}, {'recess_weeks':[True]},
])
def test_invalid_recess_snapshot_is_atomic(core,changes):
    before = core.query('state')['revision']
    with pytest.raises(BusinessError):
        adopt(core,**changes)
    assert core.query('state')['revision'] == before
    assert core.query('list',type='event')['total'] == 0
    assert core.query('list',type='timetable')['total'] == 0


def test_last_partial_week_checks_actual_weekday_after_recess(core):
    result = adopt(core,semester_end='2030-02-13',rows=[row(teaching_weeks=[5])])
    assert dates(result['rows'][0]['data']) == ['2030-02-13']
    with pytest.raises(BusinessError) as error:
        adopt(core,title='Another synthetic semester',semester_end='2030-02-12',rows=[row(teaching_weeks=[5])])
    assert error.value.code == 'timetable_weeks'
    with pytest.raises(BusinessError):
        adopt(core,title='Too many teaching weeks',rows=[row(teaching_weeks=[6])])


def test_range_ending_in_recess_has_no_phantom_teaching_week(core):
    result = adopt(core,semester_end='2030-01-27',rows=[row(teaching_weeks=[1,2])])
    assert dates(result['rows'][0]['data']) == ['2030-01-09','2030-01-16']
    with pytest.raises(BusinessError):
        adopt(core,title='No third teaching week',semester_end='2030-01-27',rows=[row(teaching_weeks=[3])])


def test_rule_change_requires_all_rows_and_current_versions_preserves_immutable_feedback(core):
    result = adopt(core,rows=[row('a'),row('b',title='Friday tutorial',weekday=4)])
    table = result['entity']; by_key = {event['data']['timetable_row_key']: event for event in result['rows']}
    a,b = by_key['a'],by_key['b']
    feedback = command(core,'record_feedback',{'target_id':a['id'],'business_date':'2030-01-09',
        'dimensions':{'attendance':'absent'},'source_text':'Synthetic attendance evidence'})['entity']
    with pytest.raises(BusinessError) as error:
        adopt(core,id=table['id'],version=table['version'],recess_weeks=['2030-01-28'],rows=[edit_row(a)])
    assert error.value.code == 'timetable_rows_required'
    with pytest.raises(BusinessError) as error:
        adopt(core,id=table['id'],version=table['version'],recess_weeks=['2030-01-28'],rows=[edit_row(a),row('b',title=b['title'],weekday=4)])
    assert error.value.code == 'timetable_row_version'
    updated = adopt(core,id=table['id'],version=table['version'],recess_weeks=['2030-01-28'],rows=[edit_row(a),edit_row(b)])
    assert set(updated['updated_ids']) == {a['id'],b['id']}
    assert all(e['data']['recess_weeks'] == ['2030-01-28'] for e in updated['rows'])
    assert updated['entity']['data']['recess_weeks'] == ['2030-01-28']
    assert core.query('get',id=feedback['id'])['entity'] == feedback


def test_shrinking_term_requires_removing_now_outside_recess_and_exception(core):
    first = adopt(core,rows=[row(exceptions={'2030-02-13':{'cancelled':True}})])
    table = first['entity']; event = first['rows'][0]
    with pytest.raises(BusinessError) as error:
        adopt(core,id=table['id'],version=table['version'],semester_end='2030-01-20',rows=[edit_row(event)])
    assert error.value.code == 'timetable_recess'
    with pytest.raises(BusinessError) as error:
        adopt(core,id=table['id'],version=table['version'],semester_end='2030-01-20',recess_weeks=[],rows=[edit_row(event)])
    assert error.value.code == 'timetable_exception'
    final = adopt(core,id=table['id'],version=table['version'],semester_end='2030-01-20',recess_weeks=[],
                  rows=[edit_row(event,remove_exceptions=['2030-02-13'])])
    assert final['rows'][0]['data']['exceptions'] == {} and final['entity']['data']['recess_weeks'] == []


def test_dst_validation_checks_actual_teaching_occurrence_after_recess(core):
    with pytest.raises(BusinessError) as error:
        adopt(core,semester_start='2030-02-18',semester_end='2030-03-17',timezone='America/New_York',
              recess_weeks=['2030-02-25'],rows=[row(weekday=6,start='02:30',end='03:30',teaching_weeks=[2])])
    assert error.value.code == 'timetable_dst'


def test_recess_is_evaluated_before_business_timezone_conversion(core):
    result = adopt(core,semester_start='2029-12-31',semester_end='2030-01-20',timezone='America/Los_Angeles',
                   recess_weeks=['2030-01-07'],rows=[row(weekday=6,start='18:00',end='19:00',teaching_weeks=[1,2])])
    event = result['rows'][0]
    # Source Sunday Jan 6 reaches business Monday Jan 7, inside its local recess week.
    assert event['id'] in {e['id'] for e in core.query('plan_context',date='2030-01-07')['hard_events']}
    assert event['id'] not in {e['id'] for e in core.query('plan_context',date='2030-01-14')['hard_events']}
    assert event['id'] in {e['id'] for e in core.query('plan_context',date='2030-01-21')['hard_events']}
