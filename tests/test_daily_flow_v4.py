import uuid
import pytest
from management.core import Core
from management.schemas import BusinessError

@pytest.fixture
def core(tmp_path): return Core(tmp_path/'daily-v4')
def cmd(core,name,p):
    s=core.query('state')
    return core.command(name,p,request_id=str(uuid.uuid4()),epoch=s['epoch'],expected_revision=s['revision'])['result']
def task(core,title='Task',**data):return cmd(core,'create',{'type':'task','title':title,'data':data})['entity']
def join(core,t,day='2030-01-07',snapshot='read'):
    plan=core.query('daily_tasks',date=day)['plan'] if snapshot=='read' else snapshot
    return cmd(core,'add_to_plan',{'date':day,'target_id':t['id'],'plan_id':plan['id'] if plan else None,'plan_version':plan['version'] if plan else None})
def fact(core,t,result,day='2030-01-07'):
    return cmd(core,'record_feedback',{'target_id':t['id'],'business_date':day,'dimensions':{'completion':result},'source_text':'Explicit test statement'})

def test_candidates_are_read_only_dated_and_paged(core):
    a=task(core,'Scheduled',scheduled_date='2030-01-07')
    b=task(core,'Due',due_date='2030-01-07')
    c=task(core,'Overdue',due_date='2030-01-06')
    task(core,'Later',scheduled_date='2030-01-08');task(core,'Unscheduled')
    before=core.query('state');r=core.query('daily_tasks',date='2030-01-07',limit=2)
    assert r['total']==3 and len(r['items'])==2 and r['next_offset']==2
    rest=core.query('daily_tasks',date='2030-01-07',limit=2,offset=2)
    assert {e['id'] for e in r['items']+rest['items']}=={a['id'],b['id'],c['id']}
    assert core.query('state')==before and not core.query('daily_review',date='2030-01-07')['has_plan']

def test_explicit_join_then_review_and_repeated_click(core):
    a=task(core,scheduled_date='2030-01-07')
    first=join(core,a)['entity']; assert first['data']['mode']=='no_precise_time'
    assert 'minutes' not in first['data']['blocks'][0]
    assert core.query('daily_tasks',date='2030-01-07')['items']==[]
    assert [r['target_id'] for r in core.query('daily_review',date='2030-01-07')['items']]==[a['id']]
    assert join(core,a)['reused'] and core.query('list',type='plan')['total']==1

def test_append_preserves_completed_task_snapshot_and_feedback(core):
    a=task(core,completion_gate='Original gate',estimated_minutes=25);b=task(core,'New')
    old=join(core,a)['entity'];fact(core,a,'done')
    current=core.query('get',id=a['id'])['entity']
    cmd(core,'update',{'id':a['id'],'version':current['version'],'patch':{'status':'done','data':{'completion_gate':'Later gate'}}})
    new=join(core,b)['entity']
    assert new['data']['blocks'][0]==old['data']['blocks'][0]
    assert new['data']['supersedes_id']==old['id']
    review=core.query('daily_review',date='2030-01-07')
    assert review['summary']['done']==1 and len(review['items'])==2
    assert core.query('get',id=old['id'])['entity']==old

def test_plan_snapshot_cannot_silently_append_to_replacement(core):
    a=task(core);b=task(core,'B');old=join(core,a)['entity']
    join(core,b)
    with pytest.raises(BusinessError,match='计划已变化'):join(core,task(core,'C'),snapshot=old)

def test_feedback_completion_hides_only_explicitly_finished(core):
    a=task(core,'A',due_date='2030-01-01');b=task(core,'B',due_date='2030-01-01')
    fact(core,a,'done');fact(core,b,'incomplete')
    assert [x['id'] for x in core.query('daily_tasks',date='2030-01-08')['items']]==[b['id']]
    # Future feedback cannot hide a historical candidate.
    assert {x['id'] for x in core.query('daily_tasks',date='2030-01-06')['items']}=={a['id'],b['id']}
    fact(core,a,'incomplete',day='2030-01-08')
    assert core.query('daily_tasks',date='2030-01-08')['total']==2

def test_rest_and_node_require_explicit_decision(core):
    a=task(core)
    cmd(core,'create_plan',{'date':'2030-01-07','mode':'rest','blocks':[]})
    with pytest.raises(BusinessError,match='休整'):join(core,a)
    owner=cmd(core,'create',{'type':'project','title':'Project'})['entity']
    m=cmd(core,'create',{'type':'milestone','title':'Deadline','parent_id':owner['id']})['entity']
    with pytest.raises(BusinessError,match='实际任务'):join(core,m)

def test_capacity_and_dependencies_still_apply(core):
    a=task(core,estimated_minutes=20);b=task(core,'B',estimated_minutes=20)
    cmd(core,'create',{'type':'rule','title':'Capacity','data':{'rule_kind':'capacity','minutes':30}})
    join(core,a)
    with pytest.raises(BusinessError,match='容量'):join(core,b)
    assert core.query('daily_review',date='2030-01-07')['summary']['total']==1


def test_empty_dates_are_unknown_not_overdue(core):
    task(core,'Unknown',scheduled_date='',due_date='')
    task(core,'Future with blank due',scheduled_date='2030-01-08',due_date='')
    assert core.query('daily_tasks',date='2030-01-07')['items']==[]


def test_reviewed_complete_predecessor_allows_next_task_and_correction_reopens(core):
    a=task(core,'Predecessor');b=task(core,'Dependent')
    cmd(core,'link',{'source_id':b['id'],'target_id':a['id'],'kind':'depends_on'})
    fact(core,a,'done',day='2030-01-06')
    assert join(core,b)['entity']['data']['blocks'][0]['target_id']==b['id']
    fact(core,a,'incomplete',day='2030-01-08')
    with pytest.raises(BusinessError,match='前置任务'):join(core,b,day='2030-01-08')
    with pytest.raises(BusinessError,match='前置任务'):join(core,b,day='2030-01-05')
