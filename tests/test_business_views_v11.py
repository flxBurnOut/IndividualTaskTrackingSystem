"""Synthetic v11 ownership, grouped warnings and shared completion evidence."""
import datetime as dt
import uuid
import pytest
from management.core import Core
from management.schemas import BusinessError

DAY='2030-01-02'

def cmd(core,name,p,**opts):
    s=opts.pop('state',None) or core.query('state')
    return core.command(name,p,request_id=opts.pop('request_id',str(uuid.uuid4())),epoch=s['epoch'],expected_revision=s['revision'])['result']

def entity(core,kind,title='Synthetic',parent=None,data=None,**extra):
    return cmd(core,'create',{'type':kind,'title':title,'parent_id':parent,'data':data or {},**extra})['entity']

def complete(core,target,result='done',**extra):
    return cmd(core,'set_task_completion',{'target_id':target['id'],'target_version':target['version'],'business_date':DAY,'result':result,**extra})

def test_nested_ownership_is_same_in_plan_workspace_candidates_and_catchup(tmp_path):
    core=Core(tmp_path);course=entity(core,'course','Course name',data={'code':'SC3060'})
    parent=entity(core,'task','Chapter',parent=course['id'])
    task=entity(core,'task','Tutorial 4',parent=parent['id'],data={'due_date':DAY})
    found=core.query('daily_tasks',date=DAY)['items'][0]
    assert found['owner_id']==course['id'] and found['owner_label']=='SC3060'
    assert core.query('workspace_tasks',id=parent['id'])['items'][0]['owner_code']=='SC3060'
    plan=cmd(core,'create_plan',{'date':DAY,'mode':'no_precise_time','blocks':[{'target_id':task['id']}]})['entity']
    item=core.query('daily_review',date=DAY)['items'][0]
    assert item['owner_code']=='SC3060' and item['target_type']=='task'
    assert core.query('get',id=task['id'],display=True)['entity']['owner_id']==course['id']
    # Display fields do not become stored business facts.
    with core.store.connect() as c:assert 'owner_label' not in core.store.get(c,task['id'])


def test_daily_candidates_group_before_paging_and_sort_old_to_new(tmp_path):
    core=Core(tmp_path);b=entity(core,'course','B',data={'code':'BB0001'});a=entity(core,'course','A',data={'code':'AA0001'})
    for owner,day in [(b,'2030-01-01'),(a,'2030-01-02'),(a,'2029-12-20'),(b,'2029-12-22')]:
        entity(core,'task',day,parent=owner['id'],data={'scheduled_date':day})
    first=core.query('daily_tasks',date=DAY,limit=2)
    second=core.query('daily_tasks',date=DAY,limit=2,offset=first['next_offset'])
    assert [x['owner_code'] for x in first['items']]==['AA0001','AA0001']
    assert [x['candidate_date'] for x in first['items']]==['2029-12-20','2030-01-02']
    assert [x['candidate_date'] for x in second['items']]==['2029-12-22','2030-01-01']
    assert len(first['groups'])==1 and first['groups'][0]['owner_id']==a['id']


def test_completion_is_one_fact_shared_with_review_recovery_and_task_lists(tmp_path):
    core=Core(tmp_path);course=entity(core,'course','SC3060')
    task=cmd(core,'set_recovery_task',{'course_id':course['id'],'title':'Tutorial 4','unit':'题','total_quantity':8,
        'completion_gate':'Finish the specified questions','source_text':'User said Tutorial 4 is outstanding.'})['entity']
    cmd(core,'create_plan',{'date':DAY,'mode':'no_precise_time','blocks':[{'target_id':task['id']}]})
    result=complete(core,task)
    assert result['changed'] and result['feedback']['data']['dimensions']=={'completion':'done'}
    assert core.query('daily_review',date=DAY)['items'][0]['result']=='done'
    assert core.query('workspace_tasks',id=course['id'])['counts']=={'open':0,'done':1}
    p=core.query('recovery_summary',task_id=task['id'],as_of=DAY)['items'][0]['progress']
    assert p['completion_confirmed'] and p['completed_quantity'] is None
    assert not complete(core,task)['changed']
    assert core.query('list',type='feedback')['total']==1
    complete(core,task,'incomplete')
    assert core.query('daily_review',date=DAY)['items'][0]['result']=='incomplete'
    assert core.query('workspace_tasks',id=course['id'])['counts']['open']==1
    assert core.query('get',id=task['id'])['entity']['status']==task['status']


def test_completion_checks_target_version_global_revision_and_deleted_target(tmp_path):
    core=Core(tmp_path);task=entity(core,'task');old=core.query('state')
    changed=cmd(core,'update',{'id':task['id'],'version':task['version'],'patch':{'title':'New title'}})['entity']
    with pytest.raises(BusinessError) as e:complete(core,task)
    assert e.value.code=='entity_conflict'
    with pytest.raises(BusinessError) as e:cmd(core,'set_task_completion',{'target_id':changed['id'],'target_version':changed['version'],'business_date':DAY,'result':'done'},state=old)
    assert e.value.code=='revision_conflict'
    cmd(core,'delete_task',{'id':changed['id'],'version':changed['version']})
    current=core.query('get',id=changed['id'])['entity']
    with pytest.raises(BusinessError):complete(core,current)
    assert core.query('list',type='feedback')['total']==0


def test_explicit_reopen_overrides_old_done_status_in_candidates_and_dependencies(tmp_path):
    core=Core(tmp_path);course=entity(core,'course','Course')
    task=entity(core,'task','Reopened',parent=course['id'],data={'scheduled_date':DAY},status='done')
    complete(core,task,'incomplete')
    assert core.query('daily_tasks',date=DAY)['items'][0]['id']==task['id']
    assert core.query('plan_context',date=DAY)['tasks'][0]['id']==task['id']
    assert core.query('workspace_tasks',id=course['id'])['counts']['open']==1
    cmd(core,'add_to_plan',{'date':DAY,'plan_id':None,'plan_version':None,'target_id':task['id']})
    assert core.query('daily_review',date=DAY)['items'][0]['result']=='incomplete'


def test_warning_current_and_past_groups_keep_history_and_shared_completion(tmp_path):
    core=Core(tmp_path);course=entity(core,'course','Course',data={'code':'IE2108'})
    entity(core,'rule',data={'rule_kind':'warning','days_before':7,'target_types':['task','milestone']})
    past=entity(core,'task','Past',parent=course['id'],data={'due_date':'2030-01-01'})
    future=entity(core,'milestone','Future',parent=course['id'],data={'due_date':'2030-01-05'})
    result=core.query('warnings',date=DAY)
    assert result['counts']=={'current':1,'past':1}
    assert result['items'][0]['id']==future['id'] and result['items'][0]['owner_code']=='IE2108'
    assert core.query('warnings',date=DAY,group='past')['items'][0]['id']==past['id']
    complete(core,future)
    assert core.query('warnings',date=DAY,group='current')['items']==[]
    assert core.query('warnings',date=DAY)['counts']['past']==1


def test_warning_today_finished_event_folds_by_timezone_not_date_guess(tmp_path,monkeypatch):
    import management.planning as module
    monkeypatch.setattr(module,'_warning_now',lambda:dt.datetime(2030,1,2,10,tzinfo=dt.timezone.utc))
    core=Core(tmp_path);entity(core,'rule',data={'rule_kind':'warning','days_before':1,'target_types':['event']})
    entity(core,'event','Finished',data={'date':DAY,'start':'09:00','end':'10:00','timezone':'Asia/Shanghai'})
    entity(core,'event','Upcoming',data={'date':DAY,'start':'20:00','end':'21:00','timezone':'Asia/Shanghai'})
    result=core.query('warnings',date=DAY)
    assert result['counts']=={'current':1,'past':1}
    assert result['items'][0]['title']=='Upcoming'


def test_plan_context_prioritizes_today_and_excludes_other_day_hard_events(tmp_path):
    core=Core(tmp_path);today=entity(core,'task','Today',data={'scheduled_date':DAY})
    for i in range(105):entity(core,'task',f'Future {i}',data={'scheduled_date':'2030-02-01'})
    entity(core,'event','Yesterday',data={'date':'2030-01-01','start':'09:00','end':'10:00'})
    event=entity(core,'event','Today event',data={'date':DAY,'start':'09:00','end':'10:00'})
    result=core.query('plan_context',date=DAY)
    assert result['tasks'][0]['id']==today['id'] and result['coverage']['tasks_paged']
    assert [x['id'] for x in result['hard_events']]==[event['id']]


def test_unit_names_never_guess_lesson_numbers_from_dates(tmp_path):
    from management.presentation import Presenter
    core=Core(tmp_path)
    with core.store.connect() as c:
        display=Presenter(core,c)
        for title in ('Lecture 2026-09-14','Lecture 2026','2026-09-14'):
            t={'id':'synthetic','type':'task','parent_id':None,'title':title,'data':{'catchup_enabled':True}}
            assert display.fields(t)['learning_unit_label']==''
        t={'id':'synthetic','type':'task','parent_id':None,'title':'TUT01, PQ 2 and OQ 3','data':{}}
        assert display.fields(t)['learning_unit_label']=='Tutorial 1、PQ 2、OQ 3'


def test_revise_plan_loads_existing_plan_and_keeps_reported_blocks(tmp_path):
    core=Core(tmp_path);a=entity(core,'task','A');b=entity(core,'task','B')
    plan=cmd(core,'create_plan',{'date':DAY,'mode':'no_precise_time','blocks':[{'target_id':a['id']}]})['entity']
    complete(core,a)
    payload={'date':DAY,'plan_id':plan['id'],'plan_version':plan['version'],'mode':'no_precise_time','blocks':[{'target_id':a['id']},{'target_id':b['id']}]}
    revised=cmd(core,'revise_plan',payload)['entity']
    assert len(revised['data']['blocks'])==2
    assert core.query('daily_review',date=DAY)['items'][0]['result']=='done'
    with pytest.raises(BusinessError):cmd(core,'revise_plan',payload)
    with pytest.raises(BusinessError) as e:cmd(core,'revise_plan',{**payload,'plan_id':revised['id'],'plan_version':revised['version'],'blocks':[{'target_id':b['id']}]})
    assert e.value.code=='plan_history'

def test_completion_retry_and_review_reuse_do_not_duplicate_evidence(tmp_path):
    core=Core(tmp_path);target=entity(core,'task','Tutorial 6')
    plan=cmd(core,'create_plan',{'date':DAY,'mode':'no_precise_time','blocks':[{'target_id':target['id']}]})['entity']
    initial=core.query('state');request=str(uuid.uuid4())
    payload={'target_id':target['id'],'target_version':target['version'],'business_date':DAY,'result':'done','plan_id':plan['id'],'plan_version':plan['version']}
    first=core.command('set_task_completion',payload,request_id=request,epoch=initial['epoch'],expected_revision=initial['revision'])
    replay=core.command('set_task_completion',payload,request_id=request,epoch=initial['epoch'],expected_revision=initial['revision'])
    assert replay['replayed'] and replay['result']['feedback']['id']==first['result']['feedback']['id']
    review=cmd(core,'submit_daily_review',{'date':DAY,'plan_id':plan['id'],'plan_version':plan['version'],'answers':[{'target_id':target['id'],'result':'done'}]})
    assert review['changed_count']==0 and core.query('list',type='feedback')['total']==1
    outside=entity(core,'task','Other')
    with pytest.raises(BusinessError) as e:complete(core,outside,plan_id=plan['id'],plan_version=plan['version'])
    assert e.value.code=='review_target'
