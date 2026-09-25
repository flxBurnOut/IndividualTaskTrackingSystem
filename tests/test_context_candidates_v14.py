"""Candidate atomicity, recovery identity and complete local constraints."""
import json,threading,uuid
import pytest
from management import context_service as cs,conversation_progress,conversations
from management.context_driver import requeue,recover
from management.core import Core
from management.session_coordinator import handle,complete_candidate
from management.schemas import BusinessError
from management.storage import encode,now
from context_harness import rows,ready,item
from test_context_service_v14 import command


@pytest.fixture
def running(tmp_path):
    core=Core(tmp_path/'data');command(core,'settings',{'settings':{'ai':{'enabled':True}}})
    sent=command(core,'send_message',{'scope':{'kind':'general'},'text':'Synthetic batch work'})
    with core.store.connect() as c:
        c.execute("UPDATE jobs SET status='running' WHERE id=?",(sent['job']['id'],))
        job=core._job(c,sent['job']['id']);conversation_progress.start(c,job)
    publish=conversation_progress.Publisher(core,job,threading.Event(),threading.Event())
    publish({'phase':'thread_ready','provider_thread_id':'one-thread','provider_contract':'desktop_mcp_v3'})
    return core,job,publish


def wire_actions(count,prefix='item'):
    return [{'command':'create','payload_json':encode({'type':'task','title':f'{prefix} {i}'}),'reason':'Synthetic explicit request'} for i in range(count)]


def test_more_than_thirty_candidates_are_all_reviewable_and_applied_once(running):
    core,job,publish=running;value=json.loads(job['input']);op=value['context_operation_id']
    for index in range(5):
        core.query('checkpoint_context',operation_id=op,result={'actions':wire_actions(20,str(index))})
    result={'summary':'100 changes, awaiting confirmation','unknowns':[],'sources':[],'actions':[]}
    receipt=handle(core,'submit',{'conversation_id':value['conversation_id'],'epoch':job['epoch'],
        'job_id':job['id'],'generation':job['generation'],'proposal':result})
    assert receipt['received'] and core.query('list',type='task')['total']==0
    with core.store.connect() as c:
        from management.session_coordinator import get_candidate
        candidate=get_candidate(c,job['id']);assert candidate['actions_total']==100 and len(candidate['actions'])<=30
        complete_candidate(core,c,job,candidate)
    seen=[];after=0
    while True:
        page=core.query('candidate_actions',operation_id=op,after=after)
        assert cs.size(page)<=cs.MAX_PAGE_BYTES
        seen.extend(page['items']);after=page['next_after']
        if after is None:break
    assert len(seen)==100
    state=core.query('state');request=str(uuid.uuid4())
    one=core.command('apply_proposal',{'id':job['id']},request_id=request,epoch=state['epoch'],expected_revision=state['revision'])
    replay=core.command('apply_proposal',{'id':job['id']},request_id=request,epoch=state['epoch'],expected_revision=state['revision'])
    assert one['result']==replay['result'] and replay['replayed'] and one['result']['results_total']==100
    assert core.query('list',type='task')['total']==100


def test_bad_last_batch_rolls_back_every_business_action(running):
    core,job,publish=running;op=json.loads(job['input'])['context_operation_id']
    core.query('checkpoint_context',operation_id=op,result={'actions':wire_actions(15)})
    core.query('checkpoint_context',operation_id=op,result={'actions':[{'command':'update','payload_json':encode({'id':'nonexistent','version':1,'patch':{'title':'bad'}}),'reason':'Test last invalid'}]})
    with pytest.raises(BusinessError):
        core.query('validate_candidate',operation_id=op,proposal={'summary':'validate','unknowns':[],'sources':[],'actions':[]})
    assert core.query('list',type='task')['total']==0


def test_restart_and_lost_turn_return_reuse_operation_thread_and_generation(running):
    core,job,publish=running;value=json.loads(job['input']);op=value['context_operation_id']
    publish({'phase':'waiting_model','provider_turn_id':'same-turn'})
    core.query('checkpoint_context',operation_id=op,summary='Safe boundary',**{'yield':True})
    with core.store.connect() as c:
        recover(core,c);current=core._job(c,job['id'])
        assert current['status']=='queued' and current['generation']==job['generation']
        updated=json.loads(current['input'])
        assert updated['context_operation_id']==op and updated['provider_thread_id']=='one-thread'
        assert updated['context_continuation']
        assert c.execute('SELECT count(*) FROM jobs').fetchone()[0]==1
    assert core.query('conversation',id=value['conversation_id'])['conversation']['active_job_id']==job['id']


def test_explicit_cancel_and_epoch_block_late_checkpoints(running):
    core,job,_=running;op=json.loads(job['input'])['context_operation_id']
    command(core,'cancel_job',{'id':job['id']})
    with pytest.raises(BusinessError):core.query('checkpoint_context',operation_id=op,summary='late')
    with core.store.connect() as c:assert not requeue(core,c,job,restart=True)
    with core.store.connect() as c:core.store.set_meta(c,'epoch',str(uuid.uuid4()))
    with pytest.raises(BusinessError):core.query('operation_status',operation_id=op)


def test_finished_task_and_late_dependency_block_planning(tmp_path):
    core=Core(tmp_path/'data')
    parent=command(core,'create',{'type':'task','title':'Prerequisite'})['entity']
    target=command(core,'create',{'type':'task','title':'Chosen task'})['entity']
    command(core,'link',{'source_id':target['id'],'target_id':parent['id'],'kind':'depends_on'})
    with pytest.raises(BusinessError) as e:command(core,'create_plan',{'date':'2030-01-01','mode':'no_precise_time','blocks':[{'target_id':target['id']}]})
    assert e.value.code=='dependency'
    command(core,'record_feedback',{'target_id':target['id'],'business_date':'2029-12-31','dimensions':{'completion':'done'},'source_text':'Explicit'})
    envelope=core.query('prepare_context',goal='Read known tasks',scope={'kind':'daily_plan','date':'2030-01-01'})
    page=core.query('query_context',operation_id=envelope['operation_id'],collection='tasks')
    assert target['id'] not in {r['id'] for r in page['items']}
    with pytest.raises(BusinessError) as e:command(core,'create_plan',{'date':'2030-01-01','mode':'no_precise_time','blocks':[{'target_id':target['id']}]})
    assert e.value.code=='plan_target'


def test_related_new_deadline_invalidates_but_unrelated_future_task_does_not(running):
    core,job,_=running;op=json.loads(job['input'])['context_operation_id']
    due=command(core,'create',{'type':'task','title':'Known deadline','data':{'due_date':'2026-09-25'}})['entity']
    # Freeze this synthetic operation's business date, independent of wall clock.
    with core.store.connect() as c:c.execute("UPDATE context_operations SET scope=? WHERE id=?",(encode({'kind':'daily_plan','date':'2026-09-25'}),op))
    rows(core,job,'deadlines')
    target=command(core,'create',{'type':'task','title':'Unrelated future','data':{'due_date':'2031-01-01'}})['entity']
    proposal={'summary':'Plan','unknowns':[],'sources':[],'actions':[{'command':'create_plan','payload_json':encode({'date':'2026-09-25','mode':'no_precise_time','blocks':[{'target_id':due['id']}] }),'reason':'Synthetic'}]}
    core.query('validate_candidate',operation_id=op,proposal=proposal)
    command(core,'create',{'type':'task','title':'New urgent','data':{'due_date':'2026-09-25'}})
    with pytest.raises(BusinessError) as e:core.query('validate_candidate',operation_id=op,proposal=proposal)
    assert e.value.code=='context_coverage'
