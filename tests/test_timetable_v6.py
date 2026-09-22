"""Confirmed synthetic timetable import; no personal schedules or model calls."""
import datetime as dt
import uuid
import pytest
from management.core import Core
from management.schemas import BusinessError
from management.timetable import occurs_on

@pytest.fixture
def core(tmp_path):return Core(tmp_path/'timetable-space')

def cmd(core,name,p,state=None,rid=None):
    state=state or core.query('state')
    return core.command(name,p,request_id=rid or str(uuid.uuid4()),epoch=state['epoch'],expected_revision=state['revision'])

def entity(core,kind,title='Synthetic',**kwargs):
    return cmd(core,'create',{'type':kind,'title':title,**kwargs})['result']['entity']

def row(key='lecture-a',**kwargs):
    p={'key':key,'title':'Synthetic lecture','weekday':2,'start':'09:30','end':'11:00'}
    p.update(kwargs);return p

def apply(core,**kwargs):
    p={'title':'Synthetic semester timetable','semester_start':'2030-01-07','semester_end':'2030-04-07',
       'timezone':'Asia/Shanghai','source_text':'Synthetic user-confirmed schedule','rows':[row()]}
    p.update(kwargs)
    return cmd(core,'apply_timetable',p)['result']

def updated_row(event,**changes):
    p={'key':event['data']['timetable_row_key'],'event_id':event['id'],'version':event['version']}
    p.update(changes);return p


def test_empty_container_can_hold_materials_without_guessed_semester_or_events(core):
    table=entity(core,'timetable',data={})
    result=core.query('timetable_week',timetable_id=table['id'],week_start='2030-01-07')
    assert not result['events'] and result['unknowns'][0]['code']=='timetable_incomplete'
    assert core.query('list',type='event')['total']==0
    before=core.query('state')['revision']
    with pytest.raises(BusinessError) as error:
        cmd(core,'apply_timetable',{'id':table['id'],'version':table['version'],'source_text':'Unknown dates','rows':[row()]})
    assert error.value.code=='timetable_incomplete' and core.query('state')['revision']==before


def test_confirmed_rows_create_existing_event_entities_no_plan_feedback_or_course(core):
    result=apply(core)
    event=result['rows'][0];table=result['entity']
    assert event['type']=='event' and event['data']['timetable_id']==table['id']
    assert event['data']['date']=='2030-01-09' and event['data']['recurrence']=='weekly'
    assert event['data']['until']=='2030-04-07' and event['data']['hard']
    assert event['data']['source_text']=='Synthetic user-confirmed schedule'
    assert event['data']['source_versions']=={}
    for kind in ('course','plan','feedback','task'):assert core.query('list',type=kind)['total']==0


def test_identical_upload_reuses_container_event_versions_and_business_identity(core):
    first=apply(core);second=apply(core)
    assert second['reused'] and first['entity']['id']==second['entity']['id']
    assert second['entity']['version']==first['entity']['version']
    assert second['rows']==first['rows'] and not second['created_ids'] and not second['updated_ids']
    assert core.query('list',type='event')['total']==1


def test_duplicate_row_keys_atomic_rejection(core):
    before=core.query('state')['revision']
    with pytest.raises(BusinessError) as error:apply(core,rows=[row(),row()])
    assert error.value.code=='timetable_duplicate_row'
    assert core.query('state')['revision']==before and core.query('list',type='timetable')['total']==0


def test_new_key_same_semantics_is_visible_conflict_not_second_class(core):
    first=apply(core)
    with pytest.raises(BusinessError) as error:
        apply(core,id=first['entity']['id'],version=first['entity']['version'],rows=[row('accidental-new-key')])
    assert error.value.code=='timetable_duplicate_semantics'
    assert core.query('list',type='event')['total']==1
    assert error.value.details['existing_key']=='lecture-a'


def test_similar_different_groups_not_auto_merged(core):
    result=apply(core,rows=[row('a',location='Room A'),row('b',location='Room B')])
    assert len(result['created_ids'])==2
    view=core.query('timetable_week',week_start='2030-01-07')
    assert len(view['events'])==2 and len(view['conflicts'])==1


def test_manual_series_time_change_updates_same_event_and_keeps_unlisted_rows(core):
    first=apply(core,rows=[row('a'),row('b',title='Tutorial',weekday=4,start='13:00',end='14:00')])
    a,b=first['rows'];table=first['entity']
    updated=apply(core,id=table['id'],version=table['version'],source_text='User explicitly moves lecture to noon.',rows=[updated_row(a,start='12:00',end='13:00')])
    assert updated['updated_ids']==[a['id']] and updated['preserved_row_ids']==[b['id']]
    actual={r['id']:r for r in updated['rows']}
    assert actual[a['id']]['version']==a['version']+1 and actual[a['id']]['data']['start']=='12:00'
    assert actual[a['id']]['data']['source_text']=='User explicitly moves lecture to noon.'
    assert actual[b['id']]==b


def test_changed_row_requires_its_own_version_and_current_container_version(core):
    first=apply(core);table=first['entity'];event=first['rows'][0]
    with pytest.raises(BusinessError) as error:
        apply(core,id=table['id'],version=table['version'],rows=[row(start='12:00',end='13:00')])
    assert error.value.code=='timetable_row_version'
    with pytest.raises(BusinessError) as error:
        apply(core,rows=[updated_row(event,start='12:00',end='13:00')])
    assert error.value.code=='timetable_version'
    assert core.query('get',id=event['id'])['entity']==event


def test_old_upload_cannot_overwrite_later_generic_time_edit(core):
    first=apply(core);event=first['rows'][0]
    edited=cmd(core,'update',{'id':event['id'],'version':event['version'],'patch':{'data':{'start':'12:00','end':'13:00'}}})['result']['entity']
    with pytest.raises(BusinessError) as error:
        apply(core,id=first['entity']['id'],version=first['entity']['version'],rows=[updated_row(event,start='09:30',end='11:00')])
    assert error.value.code=='entity_conflict'
    assert core.query('get',id=event['id'])['entity']==edited


def test_boundary_change_requires_all_existing_rows_and_versions(core):
    first=apply(core,rows=[row('a'),row('b',title='Tutorial',weekday=4)])
    table=first['entity'];a,b=first['rows']
    with pytest.raises(BusinessError) as error:
        apply(core,id=table['id'],version=table['version'],semester_end='2030-03-31',rows=[updated_row(a)])
    assert error.value.code=='timetable_rows_required'
    changed=apply(core,id=table['id'],version=table['version'],semester_end='2030-03-31',rows=[updated_row(a),updated_row(b)])
    assert set(changed['updated_ids'])=={a['id'],b['id']}
    assert all(e['data']['until']=='2030-03-31' for e in changed['rows'])


def test_cancel_one_occurrence_and_undo_exception_do_not_change_series_or_renumber(core):
    first=apply(core);table=first['entity'];event=first['rows'][0]
    cancelled=apply(core,id=table['id'],version=table['version'],rows=[updated_row(event,exceptions={'2030-01-16':{'cancelled':True}})])
    event=cancelled['rows'][0]
    assert not occurs_on(event['data'],'2030-01-16') and occurs_on(event['data'],'2030-01-23')
    unchanged=apply(core,id=cancelled['entity']['id'],version=cancelled['entity']['version'],rows=[updated_row(event,exceptions={})])
    assert unchanged['reused'] and unchanged['rows'][0]['data']['exceptions']['2030-01-16']['cancelled']
    restored=apply(core,id=unchanged['entity']['id'],version=unchanged['entity']['version'],rows=[updated_row(event,remove_exceptions=['2030-01-16'])])
    assert occurs_on(restored['rows'][0]['data'],'2030-01-16')


def test_single_occurrence_time_replacement_keeps_base_time_and_other_exceptions(core):
    first=apply(core,rows=[row(exceptions={'2030-01-16':{'cancelled':True}})])
    table=first['entity'];event=first['rows'][0]
    changed=apply(core,id=table['id'],version=table['version'],rows=[updated_row(event,exceptions={'2030-01-23':{'start':'14:00','end':'15:00'}})])
    d=changed['rows'][0]['data']
    assert d['start']=='09:30' and d['exceptions']['2030-01-16']['cancelled']
    view=core.query('timetable_week',timetable_id=table['id'],week_start='2030-01-21')
    assert view['events'][0]['start_minute']==14*60


def test_row_pause_preserves_event_and_feedback_history_without_generating_completion(core):
    first=apply(core);table=first['entity'];event=first['rows'][0]
    feedback=cmd(core,'record_feedback',{'target_id':event['id'],'business_date':'2030-01-09','dimensions':{'attendance':'absent'},'source_text':'Only synthetic absence evidence.'})['result']['entity']
    paused=apply(core,id=table['id'],version=table['version'],rows=[updated_row(event,enabled=False)])
    assert paused['rows'][0]['id']==event['id'] and paused['rows'][0]['status']=='cancelled'
    assert not core.query('timetable_week',week_start='2030-01-07')['events']
    assert core.query('get',id=feedback['id'])['entity']==feedback
    assert core.query('list',type='feedback')['total']==1


def test_specified_weeks_include_odd_even_and_last_day_without_renumber(core):
    result=apply(core,semester_end='2030-02-06',rows=[row(teaching_weeks=[1,3,5])])
    d=result['rows'][0]['data']
    assert occurs_on(d,'2030-01-09') and not occurs_on(d,'2030-01-16')
    assert occurs_on(d,'2030-02-06') and not occurs_on(d,'2030-02-13')
    assert not occurs_on(d,'2030-01-08')


@pytest.mark.parametrize('weeks',[[],[0],[53],[True],[1,1],[20]])
def test_unknown_invalid_or_outside_teaching_weeks_rejected_without_partial_import(core,weeks):
    with pytest.raises(BusinessError):apply(core,rows=[row(teaching_weeks=weeks)])
    assert core.query('list',type='event')['total']==0 and core.query('list',type='timetable')['total']==0


def test_explicit_unknown_weeks_not_expanded_to_every_week(core):
    with pytest.raises(BusinessError) as error:apply(core,rows=[row(weeks_unknown=True)])
    assert error.value.code=='timetable_weeks_unknown'


def test_semester_requires_monday_valid_timezone_and_at_most_52_weeks(core):
    for patch in ({'semester_start':'2030-01-08'},{'semester_end':'2031-01-06'},{'timezone':'Not/AZone'},{'semester_end':'2029-01-01'}):
        with pytest.raises(BusinessError):apply(core,**patch)
    result=apply(core,semester_end='2031-01-05')
    assert result['entity']['data']['semester_end']=='2031-01-05'


def test_no_more_than_100_rows_including_preserved_rows(core):
    with pytest.raises(BusinessError) as error:
        apply(core,rows=[row(str(i),title=f'Class {i}') for i in range(101)])
    assert error.value.code=='timetable_limit' and core.query('list',type='timetable')['total']==0
    first=apply(core,rows=[row(str(i),title=f'Class {i}') for i in range(100)])
    with pytest.raises(BusinessError):apply(core,id=first['entity']['id'],version=first['entity']['version'],rows=[row('extra',title='One more')])
    assert core.query('list',type='event')['total']==100


def test_missing_or_equal_times_are_not_guessed(core):
    for patch in ({'start':None},{'end':None},{'start':'09:30','end':'09:30'},{'weekday':True}):
        with pytest.raises(BusinessError):apply(core,rows=[row(**patch)])
    assert core.query('list',type='event')['total']==0


def test_nonselected_week_exception_does_not_reactivate_class(core):
    result=apply(core,rows=[row(teaching_weeks=[1,3],exceptions={'2030-01-16':{'start':'12:00','end':'13:00','cancelled':False}})])
    assert not occurs_on(result['rows'][0]['data'],'2030-01-16')


def test_explicit_source_versions_dict_stored_on_container_and_created_event(core):
    source=entity(core,'note',title='Synthetic timetable source',data={'content':'Only synthetic confirmed data.'})
    result=apply(core,source_versions={source['id']:source['version']})
    assert result['entity']['data']['source_versions']=={source['id']:source['version']}
    assert result['rows'][0]['data']['source_versions']=={source['id']:source['version']}
    assert result['rows'][0]['data']['timetable_source_version']==result['entity']['version']


def test_list_source_versions_compatibility_normalizes_to_dict_and_stale_source_fails(core):
    source=entity(core,'note',data={'content':'Synthetic'})
    result=apply(core,source_versions=[{'id':source['id'],'version':source['version']}])
    assert result['entity']['data']['source_versions']=={source['id']:1}
    cmd(core,'update',{'id':source['id'],'version':source['version'],'patch':{'data':{'content':'Revised source'}}})
    before=core.query('state')['revision']
    with pytest.raises(BusinessError) as error:apply(core,source_versions={source['id']:1})
    assert error.value.code=='source_changed' and core.query('state')['revision']==before


def test_ordinary_fixed_events_appear_in_global_week_and_conflict_with_filtered_timetable(core):
    first=apply(core)
    other=entity(core,'event',title='Synthetic fixed appointment',data={'date':'2030-01-09','start':'10:00','end':'12:00','time_kind':'exact','hard':True})
    all_view=core.query('timetable_week',week_start='2030-01-07')
    own=core.query('timetable_week',timetable_id=first['entity']['id'],week_start='2030-01-07')
    assert {e['id'] for e in all_view['events']}=={first['rows'][0]['id'],other['id']}
    assert len(own['events'])==1 and own['conflicts'][0]['other_event_id']==other['id']
    assert not ('free_windows' in all_view or 'available_minutes' in all_view)


def test_course_titles_location_and_kind_are_preserved_in_projection(core):
    course=entity(core,'course',title='Synthetic Algebra')
    first=apply(core,rows=[row(owner_id=course['id'],event_kind='tutorial',location='Synthetic room')])
    event=core.query('timetable_week',week_start='2030-01-07')['events'][0]
    assert event['course_title']=='Synthetic Algebra' and event['owner_title']=='Synthetic Algebra'
    assert event['data']['event_kind']=='tutorial' and event['effective']['location']=='Synthetic room'
    assert event['timetable_title']==first['entity']['title']


def test_week_queries_are_readonly_and_same_hard_occurrences_as_plan_context(core):
    apply(core,rows=[row(teaching_weeks=[1,3])])
    before=core.query('state')
    week=core.query('timetable_week',week_start='2030-01-21')
    for day in week['days']:
        expected=core.query('plan_context',date=day['date'])['hard_events']
        assert [(e['id'],e['occurrence_date'],e['start_minute'],e['end_minute']) for e in day['events']]==[(e['id'],e['occurrence_date'],e['start_minute'],e['end_minute']) for e in expected]
    assert core.query('state')==before


def test_conflicts_report_partial_listing_count_instead_of_false_complete(core):
    result=apply(core,rows=[row(str(i),title=f'Class {i}') for i in range(25)])
    view=core.query('timetable_week',timetable_id=result['entity']['id'],week_start='2030-01-07')
    assert len(view['conflicts'])==200 and view['coverage']['conflicts_total']==300
    assert not view['coverage']['conflicts_complete'] and view['coverage']['events_complete']


def test_timetables_listing_and_detail_return_current_event_versions(core):
    first=apply(core)
    listing=core.query('timetables',limit=1)
    assert listing['total']==1 and listing['items'][0]['row_count']==1
    detail=core.query('timetables',id=first['entity']['id'])
    assert detail['entity']['id']==first['entity']['id'] and detail['rows']==first['rows']


def test_same_request_retry_and_stale_other_client_do_not_duplicate_import(core):
    p={'title':'Synthetic','semester_start':'2030-01-07','semester_end':'2030-04-07','timezone':'Asia/Shanghai','source_text':'Synthetic confirmed source','rows':[row()]}
    state=core.query('state');rid=str(uuid.uuid4())
    first=cmd(core,'apply_timetable',p,state=state,rid=rid)
    other=Core(core.store.root)
    replay=cmd(other,'apply_timetable',p,state=state,rid=rid)
    assert replay['replayed'] and replay['result']==first['result']
    with pytest.raises(BusinessError) as error:cmd(other,'apply_timetable',p,state=state)
    assert error.value.code=='revision_conflict'
    assert other.query('list',type='event')['total']==1


def test_wrong_event_id_cannot_hijack_an_unrelated_hard_event(core):
    first=apply(core);other=entity(core,'event',data={'date':'2030-01-09','start':'08:00','end':'09:00'})
    with pytest.raises(BusinessError) as error:apply(core,id=first['entity']['id'],version=first['entity']['version'],rows=[row(event_id=other['id'],version=other['version'])])
    assert error.value.code=='timetable_row_identity' and core.query('get',id=other['id'])['entity']==other


def test_hundred_rows_do_not_duplicate_long_container_source_beyond_transport_budget(core):
    from management.storage import encode
    full_source='已确认来源'*2400  # 12000 Chinese characters, retained once in full.
    result=apply(core,source_text=full_source,rows=[row(str(i),title=f'Class {i}') for i in range(100)])
    assert result['entity']['data']['source_text']==full_source
    assert all(e['data']['timetable_source_excerpt'] is True and len(e['data']['source_text'])<700 for e in result['rows'])
    assert all(e['data']['timetable_source_version']==result['entity']['version'] for e in result['rows'])
    assert len(encode(result).encode('utf-8'))<1024*1024
    repeated=apply(core,source_text=full_source,rows=[row(str(i),title=f'Class {i}') for i in range(100)])
    assert repeated['reused'] and repeated['rows']==result['rows']


def test_echoing_inherited_source_excerpt_does_not_erase_provenance_flag(core):
    first=apply(core,source_text='来源'*400)
    event=first['rows'][0];table=first['entity']
    same=apply(core,id=table['id'],version=table['version'],source_text='来源'*400,rows=[updated_row(event,source_text=event['data']['source_text'])])
    assert same['reused'] and same['rows'][0]['data']['timetable_source_excerpt'] is True
