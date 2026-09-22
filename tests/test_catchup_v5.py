"""Synthetic course catch-up: shared tasks, cumulative evidence and explicit gates."""
import uuid
import pytest
from management.core import Core
from management.schemas import BusinessError


@pytest.fixture
def core(tmp_path):
    return Core(tmp_path/'space')


def cmd(core,name,p,request_id=None,state=None):
    state=state or core.query('state')
    return core.command(name,p,request_id=request_id or str(uuid.uuid4()),epoch=state['epoch'],expected_revision=state['revision'])


def create(core,kind,title='Synthetic',parent=None,**data):
    return cmd(core,'create',{'type':kind,'title':title,'parent_id':parent,'data':data})['result']['entity']


def register(core,course,**kwargs):
    p={'course_id':course['id'],'title':'Catch up unit A','unit':'课','total_quantity':8,
       'completion_gate':'Complete all eight recorded lessons and check the associated exercises.',
       'source_text':'Synthetic user explicitly reports falling behind.','reason':'self_reported'}
    p.update(kwargs)
    return cmd(core,'set_recovery_task',p)['result']


def report(core,task,quantity='absent',**kwargs):
    p={'task_id':task['id'],'version':task['version'],'business_date':'2001-01-01','source_text':'Synthetic explicit progress report.'}
    if quantity != 'absent':p['completed_quantity']=quantity
    p.update(kwargs)
    return cmd(core,'record_recovery_progress',p)['result']


def summary(core,**p):
    return core.query('recovery_summary',**p)


def test_register_3_of_8_atomically_creates_one_real_task_and_feedback_without_plan(core):
    course=create(core,'course')
    made=register(core,course,completed_quantity=3)
    task=made['entity']
    assert task['type']=='task' and task['data']['task_kind']=='catchup' and task['parent_id']==course['id']
    assert made['progress']['completed_quantity']==3 and made['progress']['ratio']==3/8
    assert made['progress']['completion'] is None and not made['progress']['completion_confirmed']
    assert 'catchup_completed_quantity' not in task['data']
    assert made['feedback']['data']['dimensions']=={}
    assert core.query('list',type='plan')['total']==0
    assert core.query('list',type='task')['total']==1


def test_unknowns_are_null_and_never_zero_or_failed(core):
    course=create(core,'course')
    made=register(core,course,total_quantity=None)
    result=summary(core,course_id=course['id'])
    p=result['items'][0]['progress']
    assert p['completed_quantity'] is None and p['total_quantity'] is None and p['ratio'] is None
    assert result['metrics']['quantity_unknown']==result['metrics']['total_unknown']==1
    assert result['groups'][0]['known_completed_quantity'] is None
    assert result['groups'][0]['known_total_quantity'] is None
    assert result['metrics']['completion_confirmed']==0
    assert made['feedback'] is None


def test_explicit_zero_is_known_and_zero_total_has_no_fake_percentage(core):
    course=create(core,'course')
    register(core,course,total_quantity=0,completed_quantity=0)
    p=summary(core,course_id=course['id'])['items'][0]['progress']
    assert p['quantity_known'] and p['total_known'] and p['remaining_quantity']==0
    assert p['ratio'] is None and p['completion'] is None


def test_queries_read_only_and_page_does_not_change_aggregate(core):
    course=create(core,'course')
    for i in range(4):register(core,course,title=f'Unit {i}',completed_quantity=i)
    before=core.query('state')
    first=summary(core,course_id=course['id'],limit=2)
    second=summary(core,course_id=course['id'],limit=2,offset=2)
    assert first['total']==4 and first['next_offset']==2 and second['next_offset'] is None
    assert first['metrics']==second['metrics'] and first['groups']==second['groups']
    assert first['coverage']['metrics_complete']
    assert core.query('state')==before


def test_quantity_reaching_total_does_not_infer_gate_mastery_attendance_or_submission(core):
    course=create(core,'course')
    task=register(core,course)['entity']
    report(core,task,8)
    result=summary(core,task_id=task['id'])
    p=result['items'][0]['progress']
    assert p['ratio']==1 and not p['completion_confirmed'] and p['completion'] is None
    assert result['metrics']['awaiting_confirmation']==1
    record=core.query('list',type='feedback')['items'][0]
    assert record['data']['dimensions']=={}
    assert core.query('get',id=task['id'])['entity']['status']=='pending'


def test_explicit_full_gate_confirmation_writes_only_completion_dimension(core):
    course=create(core,'course')
    task=register(core,course)['entity']
    result=report(core,task,8,completion_confirmed=True)
    assert result['feedback']['data']['dimensions']=={'completion':'done'}
    assert result['progress']['completion_confirmed'] is True
    assert summary(core,course_id=course['id'])['metrics']['completion_confirmed']==1


def test_unknown_quantity_can_remain_unknown_when_gate_explicitly_confirmed(core):
    course=create(core,'course')
    task=register(core,course,total_quantity=None)['entity']
    result=report(core,task,completion_confirmed=True)
    assert result['progress']['completion_confirmed'] and result['progress']['completed_quantity'] is None
    assert result['progress']['latest_feedback_id'] is None


def test_known_partial_quantity_cannot_confirm_whole_gate_even_if_quantity_omitted(core):
    course=create(core,'course')
    task=register(core,course)['entity']
    report(core,task,3)
    with pytest.raises(BusinessError) as error:report(core,task,completion_confirmed=True)
    assert error.value.code=='recovery_incomplete'


def test_unchecked_completion_is_omission_but_false_is_explicit_incomplete(core):
    course=create(core,'course')
    task=register(core,course)['entity']
    report(core,task,3)
    assert summary(core,task_id=task['id'])['items'][0]['progress']['completion'] is None
    report(core,task,3,completion_confirmed=False)
    assert summary(core,task_id=task['id'])['items'][0]['progress']['completion']=='incomplete'


def test_quantity_only_update_preserves_other_explicit_feedback_dimensions(core):
    course=create(core,'course')
    task=register(core,course)['entity']
    cmd(core,'record_feedback',{'target_id':task['id'],'business_date':'2000-12-31','dimensions':{'attendance':'absent','mastery':'needs_review','submission':'not_submitted'},'source_text':'Independent confirmed dimensions.'})
    report(core,task,3)
    records=core.query('list',type='feedback')['items']
    original=next(r for r in records if 'attendance' in r['data']['dimensions'])
    assert original['data']['dimensions']=={'attendance':'absent','mastery':'needs_review','submission':'not_submitted'}
    assert summary(core,task_id=task['id'])['items'][0]['progress']['completion'] is None


def test_existing_task_reused_same_id_and_original_metadata_gate_parent_preserved(core):
    course=create(core,'course')
    parent=create(core,'task',title='Parent task',parent=course['id'])
    original=create(core,'task',title='Original unfinished lab',parent=parent['id'],completion_gate='Finish ALL lab questions.',task_kind='recurring_preparation',anchor_id='synthetic-anchor',notes='Preserved independent notes')
    made=register(core,course,original_task_id=original['id'],version=original['version'],title=original['title'],completion_gate=original['data']['completion_gate'],completed_quantity=3)
    updated=made['entity']
    assert updated['id']==original['id'] and updated['parent_id']==parent['id']
    assert updated['data']['task_kind']=='recurring_preparation'
    assert updated['data']['anchor_id']=='synthetic-anchor' and updated['data']['notes']==original['data']['notes']
    assert updated['data']['completion_gate']==original['data']['completion_gate']
    assert core.query('list',type='task')['total']==2
    assert summary(core,course_id=course['id'])['total']==1


def test_existing_empty_gate_can_be_filled_but_nonempty_gate_cannot_be_weakened(core):
    course=create(core,'course')
    original=create(core,'task',parent=course['id'])
    made=register(core,course,original_task_id=original['id'],version=original['version'])
    assert made['entity']['data']['completion_gate'].startswith('Complete all eight')
    with pytest.raises(BusinessError) as error:
        register(core,course,id=made['entity']['id'],version=made['entity']['version'],completion_gate='Just skim one lesson')
    assert error.value.code=='completion_gate'


def test_same_title_original_task_requires_explicit_reuse_instead_of_copy(core):
    course=create(core,'course')
    existing=create(core,'task',title='Catch up unit A',parent=course['id'])
    with pytest.raises(BusinessError) as error:register(core,course)
    assert error.value.code=='recovery_existing_task' and error.value.details['id']==existing['id']
    assert core.query('list',type='task')['total']==1


def test_new_conversation_semantic_duplicate_reuses_task_without_resetting_progress(core):
    course=create(core,'course')
    first=register(core,course,completed_quantity=3)
    task=first['entity']
    report(core,task,5,business_date=core.query('recovery_summary')['as_of'])
    second=Core(core.store.root)
    reused=register(second,course,completed_quantity=3)
    assert reused['reused'] and reused['entity']['id']==task['id']
    assert reused['progress']['completed_quantity']==5
    assert second.query('list',type='task')['total']==1 and second.query('list',type='feedback')['total']==2


def test_lesson_key_deduplicates_and_changed_content_needs_explicit_version(core):
    course=create(core,'course')
    original=register(core,course,lesson_key='lecture-week-2')['entity']
    with pytest.raises(BusinessError) as error:
        register(core,course,lesson_key='lecture-week-2',total_quantity=9)
    assert error.value.code=='recovery_exists' and error.value.details['id']==original['id']


def test_explicit_incomplete_basis_requires_completion_not_absence_or_unknown(core):
    course=create(core,'course')
    original=create(core,'task',parent=course['id'])
    cmd(core,'record_feedback',{'target_id':original['id'],'business_date':'2000-01-01','dimensions':{'attendance':'absent'},'source_text':'Absent only; study unknown.'})
    with pytest.raises(BusinessError) as error:
        register(core,course,original_task_id=original['id'],version=original['version'],reason='confirmed_incomplete')
    assert error.value.code=='recovery_evidence'
    cmd(core,'record_feedback',{'target_id':original['id'],'business_date':'2000-01-01','dimensions':{'completion':'incomplete'},'source_text':'Original task explicitly incomplete.'})
    assert register(core,course,original_task_id=original['id'],version=original['version'],reason='confirmed_incomplete')['entity']['id']==original['id']


def test_source_required_for_registration_progress_and_correction(core):
    course=create(core,'course')
    with pytest.raises(BusinessError):register(core,course,source_text=' ')
    task=register(core,course)['entity']
    with pytest.raises(BusinessError):report(core,task,3,source_text='')
    first=report(core,task,3)
    with pytest.raises(BusinessError):report(core,task,2,correction_of=first['feedback']['id'],correction_reason='')
    assert core.query('list',type='feedback')['total']==1


def test_cumulative_reports_not_added_together_and_decrease_requires_current_evidence(core):
    course=create(core,'course')
    task=register(core,course)['entity']
    first=report(core,task,3)
    second=report(core,task,5)
    assert second['progress']['completed_quantity']==5
    before=core.query('state')['revision']
    with pytest.raises(BusinessError) as error:report(core,task,4)
    assert error.value.code=='recovery_correction'
    assert core.query('state')['revision']==before
    with pytest.raises(BusinessError):report(core,task,4,correction_of=first['feedback']['id'],correction_reason='Wrong prior version')
    corrected=report(core,task,4,correction_of=second['feedback']['id'],correction_reason='One lesson was counted twice.',source_text='User explicitly corrects cumulative count.')
    assert corrected['progress']['completed_quantity']==4
    assert core.query('get',id=second['feedback']['id'])['entity']['data']['catchup_progress']['completed_quantity']==5
    assert core.query('list',type='feedback')['total']==3


def test_clearing_known_quantity_to_unknown_requires_correction_not_zero(core):
    course=create(core,'course')
    task=register(core,course)['entity']
    previous=report(core,task,3)['feedback']
    with pytest.raises(BusinessError):report(core,task,None)
    corrected=report(core,task,None,correction_of=previous['id'],correction_reason='Quantity evidence is no longer reliable.')
    assert corrected['progress']['completed_quantity'] is None and corrected['progress']['ratio'] is None


def test_backdated_report_does_not_replace_newer_business_day_progress(core):
    course=create(core,'course')
    task=register(core,course)['entity']
    report(core,task,5,business_date='2001-01-03')
    report(core,task,3,business_date='2001-01-02')
    assert summary(core,task_id=task['id'])['items'][0]['progress']['completed_quantity']==5
    assert summary(core,task_id=task['id'],as_of='2001-01-02')['items'][0]['progress']['completed_quantity']==3


def test_future_feedback_not_in_current_summary(core):
    course=create(core,'course')
    task=register(core,course)['entity']
    report(core,task,8,business_date='2099-01-01',completion_confirmed=True)
    current=summary(core,task_id=task['id'])['items'][0]['progress']
    assert current['completed_quantity'] is None and not current['completion_confirmed']
    future=summary(core,task_id=task['id'],as_of='2099-01-01')['items'][0]['progress']
    assert future['completed_quantity']==8 and future['completion_confirmed']


def test_distinct_units_never_share_a_quantity_ratio_and_unknown_coverage_blocks_group_ratio(core):
    course=create(core,'course')
    register(core,course,title='Lessons',unit='课',total_quantity=8,completed_quantity=3)
    register(core,course,title='Other lessons',unit='课',total_quantity=4)
    register(core,course,title='Exercises',unit='题',total_quantity=10,completed_quantity=5)
    groups={g['unit']:g for g in summary(core,course_id=course['id'])['groups']}
    assert groups['课']['ratio'] is None and groups['课']['quantity_unknown']==1
    assert groups['课']['known_completed_quantity']==3 and groups['课']['known_total_quantity']==12
    assert groups['题']['ratio']==0.5


def test_topic_scope_and_cross_course_original_are_rejected(core):
    course=create(core,'course',title='Course A')
    other=create(core,'course',title='Course B')
    own_topic=create(core,'topic',parent=course['id'])
    foreign_topic=create(core,'topic',parent=other['id'])
    original=create(core,'task',parent=other['id'])
    with pytest.raises(BusinessError):register(core,course,topic_ids=[foreign_topic['id']])
    with pytest.raises(BusinessError):register(core,course,original_task_id=original['id'],version=original['version'])
    made=register(core,course,topic_ids=[own_topic['id']],lesson_topics=['Vectors','Projections'])
    assert made['entity']['data']['catchup_topic_ids']==[own_topic['id']]
    assert made['entity']['data']['catchup_lesson_topics']==['Vectors','Projections']


def test_total_revision_and_unit_change_require_explicit_semantics(core):
    course=create(core,'course')
    task=register(core,course)['entity']
    report(core,task,3)
    with pytest.raises(BusinessError) as error:register(core,course,id=task['id'],version=task['version'],unit='题')
    assert error.value.code=='recovery_unit_locked'
    with pytest.raises(BusinessError):register(core,course,id=task['id'],version=task['version'],total_quantity=10)
    changed=register(core,course,id=task['id'],version=task['version'],total_quantity=10,correction_reason='Syllabus confirms two additional lessons.',source_text='Synthetic syllabus correction.')
    assert changed['progress']['completed_quantity']==3 and changed['progress']['ratio']==0.3
    with pytest.raises(BusinessError) as error:register(core,course,id=changed['entity']['id'],version=changed['entity']['version'],total_quantity=2,correction_reason='Invalid below completed quantity')
    assert error.value.code=='recovery_total_below_progress'


def test_existing_definition_edit_cannot_overwrite_progress(core):
    course=create(core,'course')
    task=register(core,course)['entity']
    report(core,task,3)
    with pytest.raises(BusinessError) as error:register(core,course,id=task['id'],version=task['version'],completed_quantity=0)
    assert error.value.code=='recovery_progress_command'


def test_completion_correction_is_explicit_and_preserves_previous_confirmation(core):
    course=create(core,'course')
    task=register(core,course)['entity']
    done=report(core,task,8,completion_confirmed=True)
    with pytest.raises(BusinessError):report(core,task,completion_confirmed=False)
    corrected=report(core,task,completion_confirmed=False,correction_reason='The required checked exercises were not actually complete.')
    assert corrected['progress']['completion']=='incomplete' and corrected['progress']['completed_quantity']==8
    assert core.query('get',id=done['feedback']['id'])['entity']['data']['dimensions']['completion']=='done'


def test_can_explicitly_join_any_daily_plan_then_use_regular_review_without_duplicate_task(core):
    course=create(core,'course')
    task=register(core,course,estimated_minutes=30)['entity']
    plan=cmd(core,'add_to_plan',{'date':'2030-01-10','target_id':task['id'],'target_version':task['version'],'plan_id':None,'plan_version':None})['result']['entity']
    assert plan['data']['blocks'][0]['target_id']==task['id']
    assert plan['data']['blocks'][0]['completion_gate']==task['data']['completion_gate']
    daily=core.query('daily_review',date='2030-01-10')
    assert daily['summary']['total']==1
    assert core.query('list',type='task')['total']==1


def test_different_clients_stale_revision_and_exact_request_replay_keep_single_evidence(core):
    course=create(core,'course')
    task=register(core,course)['entity']
    state=core.query('state');rid=str(uuid.uuid4())
    payload={'task_id':task['id'],'version':task['version'],'business_date':'2001-01-01','completed_quantity':3,'source_text':'Stable synthetic report'}
    first=cmd(core,'record_recovery_progress',payload,request_id=rid,state=state)
    other=Core(core.store.root)
    retry=cmd(other,'record_recovery_progress',payload,request_id=rid,state=state)
    assert retry['replayed'] and retry['result']==first['result']
    with pytest.raises(BusinessError) as error:cmd(other,'record_recovery_progress',payload,state=state)
    assert error.value.code=='revision_conflict'
    assert other.query('list',type='feedback')['total']==1


def test_scope_is_current_parent_tree_and_summary_stays_separate_after_move(core):
    first=create(core,'course',title='A');second=create(core,'course',title='B')
    task=register(core,first)['entity']
    cmd(core,'move',{'id':task['id'],'version':task['version'],'parent_id':second['id']})
    assert summary(core,course_id=first['id'])['total']==0
    assert summary(core,course_id=second['id'])['items'][0]['id']==task['id']
    with pytest.raises(BusinessError):summary(core,course_id=first['id'],task_id=task['id'])


def test_cancelled_tasks_visible_but_excluded_from_quantity_denominators(core):
    course=create(core,'course')
    task=register(core,course,completed_quantity=3)['entity']
    cmd(core,'update',{'id':task['id'],'version':task['version'],'patch':{'status':'cancelled'}})
    result=summary(core,course_id=course['id'])
    assert result['total']==1 and result['metrics']['cancelled_tasks']==1 and not result['groups']


@pytest.mark.parametrize('value',[-1,True,float('nan'),float('inf'),1_000_000_001])
def test_invalid_quantities_rejected_without_partial_task(core,value):
    course=create(core,'course')
    before=core.query('state')['revision']
    with pytest.raises(BusinessError):register(core,course,total_quantity=value)
    assert core.query('state')['revision']==before and core.query('list',type='task')['total']==0


def test_initial_progress_above_total_rolls_back_task_and_feedback_together(core):
    course=create(core,'course')
    before=core.query('state')['revision']
    with pytest.raises(BusinessError):register(core,course,completed_quantity=9)
    assert core.query('state')['revision']==before
    assert core.query('list',type='task')['total']==0 and core.query('list',type='feedback')['total']==0


def test_long_task_title_does_not_break_progress_evidence(core):
    course=create(core,'course')
    made=register(core,course,title='A'*300,completed_quantity=3)
    assert len(made['feedback']['title'])<=300 and made['progress']['completed_quantity']==3


def test_semantic_repeat_of_latest_quantity_source_day_reuses_evidence_but_new_source_is_retained(core):
    course=create(core,'course');task=register(core,course)['entity']
    first=report(core,task,3)
    again=report(core,task,3)
    assert again['reused'] and again['feedback']['id']==first['feedback']['id']
    other=report(core,task,3,source_text='New independent explicit evidence.')
    assert not other['reused'] and other['feedback']['id']!=first['feedback']['id']
    assert core.query('list',type='feedback')['total']==2


def test_same_correction_resubmitted_with_new_request_id_does_not_duplicate_or_fail(core):
    course=create(core,'course');task=register(core,course)['entity']
    original=report(core,task,5)['feedback']
    first=report(core,task,4,correction_of=original['id'],correction_reason='One unit counted twice.')
    repeat=report(core,task,4,correction_of=original['id'],correction_reason='One unit counted twice.')
    assert repeat['reused'] and repeat['feedback']['id']==first['feedback']['id']
    assert core.query('list',type='feedback')['total']==2


def test_quantity_correction_does_not_leave_false_gate_confirmation_or_erase_old_feedback(core):
    course=create(core,'course');task=register(core,course)['entity']
    old=report(core,task,8,completion_confirmed=True)['feedback']
    corrected=report(core,task,7,correction_of=old['id'],correction_reason='One lesson was not actually checked.')
    assert corrected['progress']['completion']=='done'  # original independent statement remains readable
    assert not corrected['progress']['completion_confirmed']
    assert corrected['progress']['issues']
    assert core.query('get',id=old['id'])['entity']['data']['dimensions']['completion']=='done'


def test_nonempty_original_done_feedback_requires_explicit_correction_before_registering_catchup(core):
    course=create(core,'course');task=create(core,'task',parent=course['id'])
    cmd(core,'record_feedback',{'target_id':task['id'],'business_date':'2000-01-01','dimensions':{'completion':'done'},'source_text':'Original explicit done.'})
    with pytest.raises(BusinessError) as error:register(core,course,original_task_id=task['id'],version=task['version'])
    assert error.value.code=='recovery_closed'


def test_renaming_existing_definition_cannot_collide_with_other_learning_unit(core):
    course=create(core,'course')
    first=register(core,course,title='First',lesson_key='L1')['entity']
    second=register(core,course,title='Second',lesson_key='L2')['entity']
    with pytest.raises(BusinessError) as error:register(core,course,id=second['id'],version=second['version'],title='First',lesson_key='L1')
    assert error.value.code=='recovery_exists' and error.value.details['id']==first['id']


def test_current_status_done_does_not_prove_historical_gate_and_unknown_feedback_is_not_incomplete(core):
    course=create(core,'course');task=register(core,course)['entity']
    report(core,task,8,business_date='2001-01-02',completion_confirmed=True)
    changed=cmd(core,'update',{'id':task['id'],'version':task['version'],'patch':{'status':'done'}})['result']['entity']
    before=summary(core,task_id=task['id'],as_of='2001-01-01')['items'][0]['progress']
    assert before['completion'] is None and before['completed_quantity'] is None and not before['completion_confirmed']
    cmd(core,'record_feedback',{'target_id':task['id'],'business_date':'2001-01-03','dimensions':{'completion':'unknown'},'source_text':'Confirmation currently uncertain.'})
    later=summary(core,task_id=task['id'],as_of='2001-01-03')['items'][0]['progress']
    assert later['completion']=='unknown' and not later['completion_confirmed']
    assert later['completed_quantity']==8


def test_reading_older_quantity_uses_explicit_current_definition_and_keeps_original_denominator(core):
    course=create(core,'course');task=register(core,course)['entity']
    report(core,task,3,business_date='2001-01-01')
    register(core,course,id=task['id'],version=task['version'],total_quantity=10,correction_reason='Scope corrected.',source_text='Explicit revised total.')
    value=summary(core,task_id=task['id'],as_of='2001-01-01')['items'][0]['progress']
    assert value['total_quantity']==10 and value['recorded_total_quantity']==8
    assert value['denominator_basis']=='current_task_definition'


def test_newer_regular_completion_confirmation_is_not_denied_by_older_partial_quantity(core):
    course=create(core,'course');task=register(core,course)['entity']
    report(core,task,3,business_date='2001-01-01')
    cmd(core,'record_feedback',{'target_id':task['id'],'business_date':'2001-01-02','dimensions':{'completion':'done'},'source_text':'All stated completion conditions explicitly met in regular daily review.'})
    result=summary(core,task_id=task['id'])['items'][0]['progress']
    assert result['completion_confirmed'] and result['completion']=='done'
    assert result['completed_quantity']==3 and result['ratio']==3/8
    assert any('此前记录' in issue for issue in result['issues'])


def test_same_day_completion_after_quantity_uses_record_order_not_timestamp_tie(core):
    course=create(core,'course');task=register(core,course)['entity']
    report(core,task,3)
    cmd(core,'record_feedback',{'target_id':task['id'],'business_date':'2001-01-01','dimensions':{'completion':'done'},'source_text':'Explicit later confirmation without updating quantity.'})
    assert summary(core,task_id=task['id'])['items'][0]['progress']['completion_confirmed']


def test_total_correction_retains_explicit_reason_source_and_old_quantity_evidence(core):
    course=create(core,'course');task=register(core,course)['entity']
    old=report(core,task,3)['feedback']
    result=register(core,course,id=task['id'],version=task['version'],total_quantity=10,correction_reason='Two units were missing from original scope.',source_text='Explicit syllabus count correction.')
    correction=result['entity']['data']['catchup_definition_correction']
    assert correction['reason']=='Two units were missing from original scope.'
    assert correction['source_text']=='Explicit syllabus count correction.'
    assert correction['previous_total_quantity']==8 and correction['new_total_quantity']==10
    assert core.query('get',id=old['id'])['entity']['data']['catchup_progress']['total_quantity']==8
