"""Growth regressions: initial envelopes are constant; details remain reachable."""
import uuid
import pytest
from management import ai
from management.core import Core
from management.storage import encode
from context_harness import rows,item

DAY='2030-01-07'
def command(core,name,p):
    state=core.query('state')
    return core.command(name,p,request_id=str(uuid.uuid4()),epoch=state['epoch'],expected_revision=state['revision'])['result']
@pytest.fixture
def core(tmp_path):
    value=Core(tmp_path);command(value,'settings',{'settings':{'ai':{'enabled':True}}});return value
def create(core,kind,title,data=None,**kwargs):
    return command(core,'create',{'type':kind,'title':title,'data':data or {},**kwargs})['entity']
def send(core):
    return command(core,'send_message',{'scope':{'kind':'daily_plan','date':DAY},'text':'请生成每日计划','request_plan':True})['job']
def event(core,title,**values):
    return create(core,'event',title,{'date':DAY,'start':'10:00','end':'11:00','timezone':'Asia/Shanghai','hard':True,'recurrence':'weekly',**values})

def test_resolved_capture_history_cannot_block_a_new_plan(core):
    target=create(core,'task','Current work',{'completion_gate':'Full gate'})
    resolved=[create(core,'inbox',f'Resolved {n}',{'content':'Old history. '*2000},status='done') for n in range(10)]
    fresh=create(core,'inbox','New notice',{'content':'Keep the whole fresh notice'})
    job=send(core)
    assert [r['id'] for r in rows(core,job,'inbox')]==[fresh['id']]
    assert item(core,job,fresh['id'])==fresh
    assert {r['id'] for r in rows(core,job,'tasks')}=={target['id']}
    assert len(encode(job['input']['context']).encode())<24576
    assert len(ai._content(job['input'],{'create_plan'},True).encode())<ai.MAX_CONTEXT_BYTES
    assert core.query('get',id=resolved[0]['id'])['entity']==resolved[0]

def test_full_fixed_constraints_are_reachable_without_discarding_tasks(core):
    tasks=[create(core,'task',f'Work {i}',{'due_date':'2030-01-08'}) for i in range(140)]
    events=[event(core,f'Fixed {i}',notes='Complete constraint. '*600,start=f'{9+i:02d}:00',end=f'{10+i:02d}:00') for i in range(4)]
    job=send(core)
    assert {r['id'] for r in rows(core,job,'tasks')}=={r['id'] for r in tasks}
    schedule=item(core,job,'@schedule')
    assert {r['id'] for r in schedule['events']}=={r['id'] for r in events}
    for source in events:assert item(core,job,source['id'])['data']==source['data']
    assert len(encode(job['input']['context']).encode())<24576

def test_changed_occurrence_and_protected_history_remain_complete(core):
    fixed=event(core,'Rescheduled',exceptions={DAY:{'start':'13:00','end':'14:00'}})
    work=create(core,'task','Reported',{'completion_gate':'Original'})
    old=command(core,'create_plan',{'date':DAY,'mode':'no_precise_time','blocks':[{'target_id':work['id']}]})['entity']
    command(core,'record_feedback',{'target_id':work['id'],'business_date':DAY,'dimensions':{'completion':'done'},'source_text':'Explicit'})
    job=send(core);current=next(e for e in item(core,job,'@schedule')['events'] if e['id']==fixed['id'])
    assert current['data']['start']=='10:00' and current['effective']['start']=='13:00'
    assert (current['start_minute'],current['end_minute'])==(780,840)
    request=item(core,job,'@plan_request')
    assert request['existing_plan']==old and request['protected_blocks']==old['data']['blocks']
    assert item(core,job,'@daily_review')['summary']['done']==1
    assert work['id'] not in {r['id'] for r in rows(core,job,'tasks')}

def test_unresolved_inbox_pages_keep_every_body(core):
    entries=[create(core,'inbox',f'Unresolved {i}',{'content':f'Notice {i}'}) for i in range(112)]
    old=create(core,'inbox','Archived');command(core,'archive',{'id':old['id'],'version':1,'archived':True})
    job=send(core)
    assert {r['id'] for r in rows(core,job,'inbox')}=={r['id'] for r in entries}
    assert item(core,job,entries[-1]['id'])==entries[-1]

def test_large_mandatory_constraint_does_not_reject_sending(core):
    fixed=event(core,'Long constraint',notes='Required constraint. '*2800)
    job=send(core)
    assert len(encode(job['input']['context']).encode())<24576
    assert item(core,job,fixed['id'])==fixed
    assert len(core.query('conversation',scope={'kind':'daily_plan','date':DAY})['messages'])==1
