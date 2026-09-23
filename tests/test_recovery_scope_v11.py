"""Imported catch-up scope corrections retain evidence without reusing it."""
import datetime as dt
import json
import pytest
from management.core import Core
from management.schemas import BusinessError
from test_business_views_v11 import cmd,entity


def fixture(core):
    course=entity(core,'course','SC3060')
    task=cmd(core,'set_recovery_task',{'course_id':course['id'],'title':'Week 4 and Tutorial 3','unit':'项','total_quantity':2,
        'completion_gate':'Finish Week 4 reading and Tutorial 3','source_text':'Original imported record','lesson_key':'Lecture 4; Tutorial 3'})['entity']
    day=core.query('today')['date']
    payload={'id':task['id'],'version':task['version'],'title':'Lecture 4 与 Tutorial 4','completion_gate':'阅读 Lecture 4；独立完成 Tutorial 4',
        'lesson_key':'Lecture 4; Tutorial 4','source_text':'明确记录：Tutorial 3已完成，剩余Lecture 4与Tutorial 4。','correction_reason':'导入标题和完成条件滞后于已确认正文','business_date':day}
    return course,task,payload


def test_scope_correction_keeps_identity_ownership_history_and_resets_only_completion(tmp_path):
    core=Core(tmp_path);course,task,payload=fixture(core)
    old=cmd(core,'record_feedback',{'target_id':task['id'],'business_date':payload['business_date'],
        'dimensions':{'completion':'done','attendance':'attended','viewing':'viewed','submission':'submitted'},'source_text':'Previous scope explicit completion'})['entity']
    result=cmd(core,'correct_recovery_scope',payload);current=result['entity']
    assert current['id']==task['id'] and current['parent_id']==course['id']
    assert current['version']==task['version']+1
    assert result['feedback']['data']['dimensions']=={'completion':'unknown'}
    assert core.query('get',id=old['id'])['entity']==old
    assert not result['progress']['completion_confirmed'] and result['progress']['completion'] is None
    assert result['progress']['total_quantity'] is None and result['progress']['completed_quantity'] is None
    assert core.query('workspace_tasks',id=course['id'])['counts']=={'open':1,'done':0}
    correction=current['data']['catchup_scope_correction']
    assert correction['previous_scope']['completion_gate']==task['data']['completion_gate']
    assert correction['previous_scope']['total_quantity']==2 and correction['reason']==payload['correction_reason']
    with core.store.connect() as c:
        changes=list(c.execute("SELECT action,before_value,after_value FROM changes WHERE entity_id=? ORDER BY seq",(task['id'],)))
    assert changes[-1]['action']=='correct_recovery_scope'
    assert json.loads(changes[-1]['before_value'])['title']==task['title']


@pytest.mark.parametrize('quantity',[0,1])
def test_measured_quantity_scope_cannot_be_reinterpreted(tmp_path,quantity):
    core=Core(tmp_path);course,task,payload=fixture(core)
    cmd(core,'record_recovery_progress',{'task_id':task['id'],'version':task['version'],'business_date':payload['business_date'],'completed_quantity':quantity,'source_text':'Explicit quantity'})
    before=core.query('state')
    with pytest.raises(BusinessError) as e:cmd(core,'correct_recovery_scope',payload)
    assert e.value.code=='recovery_scope_has_quantities'
    assert core.query('get',id=task['id'])['entity']==task and core.query('state')==before


def test_source_reason_version_are_required_and_gui_metadata_is_opt_in(tmp_path):
    core=Core(tmp_path);course,task,payload=fixture(core)
    assert core.query('get',id=task['id'])['entity']==task
    assert core.query('get',id=task['id'],display=True)['entity']['owner_code']=='SC3060'
    for field in ('source_text','correction_reason','lesson_key','completion_gate'):
        invalid={**payload,field:''}
        with pytest.raises(BusinessError):cmd(core,'correct_recovery_scope',invalid)
    with pytest.raises(BusinessError) as e:cmd(core,'correct_recovery_scope',{**payload,'version':task['version']+1})
    assert e.value.code=='entity_conflict'
    assert core.query('get',id=task['id'])['entity']==task


def test_prior_dated_completion_does_not_apply_to_new_scope_before_reset_date(tmp_path):
    core=Core(tmp_path);course,task,payload=fixture(core)
    yesterday=(dt.date.fromisoformat(payload['business_date'])-dt.timedelta(days=1)).isoformat()
    cmd(core,'record_feedback',{'target_id':task['id'],'business_date':yesterday,'dimensions':{'completion':'done'},'source_text':'Old scope done yesterday'})
    current=cmd(core,'correct_recovery_scope',payload)['entity']
    historical=core.query('recovery_summary',task_id=task['id'],as_of=yesterday)['items'][0]['progress']
    assert not historical['completion_confirmed'] and historical['completion'] is None
    assert any('旧范围' in text for text in historical['issues'])
    cmd(core,'set_task_completion',{'target_id':current['id'],'target_version':current['version'],'business_date':payload['business_date'],'result':'done'})
    now=core.query('recovery_summary',task_id=task['id'])['items'][0]['progress']
    assert now['completion_confirmed']


def test_codex_course_candidate_cannot_correct_another_course_even_without_sources(tmp_path):
    from management.sources import apply_source_action
    from management.ai import ALLOWED_COMMANDS
    core=Core(tmp_path);course,task,payload=fixture(core);other=entity(core,'course','Other')
    assert 'correct_recovery_scope' in ALLOWED_COMMANDS
    assert 'correct_recovery_scope' in core.query('capabilities')['commands']
    with core.store.connect() as c:
        with pytest.raises(BusinessError) as e:apply_source_action(core,c,{'input':{'conversation_scope':{'kind':'course','entity_id':other['id']}}},{'command':'correct_recovery_scope','payload':payload},'scope-test')
    assert e.value.code=='course_scope'
    assert core.query('get',id=task['id'])['entity']==task
