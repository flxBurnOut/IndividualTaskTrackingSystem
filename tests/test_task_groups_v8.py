import pytest
from management.core import Core
from test_plan_assistance_v8 import cmd,task


def test_completed_history_has_independent_paging_and_cannot_hide_open_tasks(tmp_path):
    core=Core(tmp_path);owner=cmd(core,'create',{'type':'course','title':'Course'})['entity']
    opened=task(core,'Still actionable',parent_id=owner['id'])
    completed=[task(core,f'Completed {i}',parent_id=owner['id'],status='done') for i in range(65)]
    state=core.query('state')
    first=core.query('workspace_tasks',id=owner['id'])
    assert [t['id'] for t in first['items']]==[opened['id']] and first['counts']=={'open':1,'done':65}
    seen=[];offset=0
    while offset is not None:
        page=core.query('workspace_tasks',id=owner['id'],group='done',offset=offset,limit=30)
        seen.extend(x['id'] for x in page['items']);offset=page['next_offset']
    assert set(seen)=={t['id'] for t in completed} and len(seen)==65
    assert core.query('state')['revision']==state['revision']
    assert core.query('object_workspace',id=owner['id'],separate_tasks=True)['children']==[]


def test_explicit_status_changes_override_old_feedback_but_notes_do_not(tmp_path):
    core=Core(tmp_path);owner=cmd(core,'create',{'type':'course','title':'Course'})['entity'];t=task(core,parent_id=owner['id'])
    def feedback(value):cmd(core,'record_feedback',{'target_id':t['id'],'business_date':'2030-01-01','dimensions':{'completion':value},'source_text':'Explicit result'})
    def update(patch):
        current=core.query('get',id=t['id'])['entity'];cmd(core,'update',{'id':t['id'],'version':current['version'],'patch':patch})
    def done():return core.query('workspace_tasks',id=owner['id'])['counts']['done']
    feedback('incomplete');assert done()==0
    update({'status':'done'});assert done()==1
    feedback('incomplete');assert done()==0
    update({'data':{'notes':'Only edit notes'}});assert done()==0
    feedback('done');assert done()==1
    update({'status':'active'});assert done()==0
    assert core.query('list',type='feedback')['total']==3
    assert core.query('object_workspace',id=owner['id'])['summary']['done_tasks']==0


def test_archived_tasks_stay_out_of_both_groups(tmp_path):
    core=Core(tmp_path);owner=cmd(core,'create',{'type':'course','title':'Course'})['entity'];t=task(core,parent_id=owner['id'],status='done')
    cmd(core,'delete_task',{'id':t['id'],'version':t['version']})
    assert core.query('workspace_tasks',id=owner['id'])['counts']=={'open':0,'done':0}
    assert core.query('get',id=t['id'])['entity']['archived']


def test_completed_recovery_is_filtered_before_paging_without_changing_progress(tmp_path):
    from test_catchup_v5 import register,report
    core=Core(tmp_path);course=cmd(core,'create',{'type':'course','title':'Recovery course'})['entity']
    completed=[]
    for i in range(12):
        t=register(core,course,title=f'Finished lesson {i}')['entity'];report(core,t,8,completion_confirmed=True);completed.append(t)
    open_task=register(core,course,title='Quantity full but completion unconfirmed')['entity'];report(core,open_task,8)
    before=core.query('state');full=core.query('recovery_summary',course_id=course['id'])
    visible=core.query('recovery_summary',course_id=course['id'],open_only=True,limit=10)
    assert [t['id'] for t in visible['items']]==[open_task['id']]
    assert visible['total']==1 and visible['completed_hidden']==12 and visible['next_offset'] is None
    assert visible['metrics']==full['metrics'] and visible['groups']==full['groups']
    assert core.query('workspace_tasks',id=course['id'],group='done')['total']==12
    assert core.query('state')==before
    # An explicit reopen is visible even when an older completion is retained.
    cmd(core,'update',{'id':completed[0]['id'],'version':completed[0]['version'],'patch':{'status':'active'}})
    reopened=core.query('recovery_summary',course_id=course['id'],open_only=True)
    assert reopened['total']==2 and reopened['completed_hidden']==11
