import copy,json,threading,time,uuid
import pytest
from management.core import Core
from management import ai,conversations
from management.plan_assistance import normalize
from management.schemas import BusinessError
from management.storage import encode


def cmd(core,name,p):
    state=core.query('state')
    return core.command(name,p,request_id=str(uuid.uuid4()),epoch=state['epoch'],expected_revision=state['revision'])['result']


def task(core,title='Do the known task',**extra):
    return cmd(core,'create',{'type':'task','title':title,**extra})['entity']


def request(core,day='2030-01-01'):
    cmd(core,'settings',{'settings':{'ai':{'enabled':True}}})
    return cmd(core,'send_message',{'scope':{'kind':'daily_plan','date':day},'text':'请生成这一天的计划','request_plan':True})['job']


def proposal(targets,day='2030-01-01',mode='no_precise_time'):
    return {'summary':'先给可用的有序计划','unknowns':['其他课程未确认日期暂保留'],'sources':[],
        'actions':[{'command':'create_plan','payload':{'date':day,'mode':mode,'blocks':[{'target_id':t['id']} for t in targets]},'reason':'明确请求'}]}


def finish(core,job,result):
    from context_harness import ready
    ready(core,job,planning=True)
    with core.store.connect() as c:
        c.execute('BEGIN IMMEDIATE')
        result=normalize(core,c,job,result)
        c.execute("UPDATE jobs SET status='awaiting_review',result=? WHERE id=?",(encode(result),job['id']))
        conversations.complete_job(core,c,job,result);c.commit()
    return result


def test_large_task_bodies_do_not_drop_most_of_the_planning_universe(tmp_path):
    core=Core(tmp_path)
    tasks=[task(core,f'Existing task {i}',data={'notes':'Long imported history. '*400,'next_action':'Use the recorded materials','priority':'normal','due_date':'2030-01-02'}) for i in range(48)]
    cmd(core,'create',{'type':'event','title':'Future exam date unknown','data':{'hard':True}})
    job=request(core)
    from context_harness import rows
    assert {t['id'] for t in rows(core,job,'tasks')}=={t['id'] for t in tasks}
    assert len(encode(job['input']['context']))<42000
    assert job['input']['allowed_commands']==['create_plan']
    assert core.query('list',type='plan')['total']==0


def test_candidate_is_saveable_without_filling_unrelated_unknowns(tmp_path):
    core=Core(tmp_path);t=task(core,data={'completion_gate':'Whole original requirement'})
    cmd(core,'create',{'type':'event','title':'Undated future quiz','data':{'hard':True}})
    job=request(core);before=core.query('state')
    result=finish(core,job,proposal([t]))
    assert result['plan_preview']['validated'] and result['plan_preview']['items'][0]['title']==t['title']
    assert result['unknowns']==[] and result['planning_notes']
    assert core.query('state')['revision']==before['revision']
    assert core.query('list',type='plan')['total']==0
    receipt=cmd(core,'apply_proposal',{'id':job['id']})
    assert receipt['results'][0]['entity']['data']['blocks'][0]['completion_gate']=='Whole original requirement'
    assert core.query('daily_review',date='2030-01-01')['has_plan']
    assert core.query('list',type='feedback')['total']==0


def test_replan_preserves_reported_block_and_original_gate(tmp_path):
    core=Core(tmp_path);a=task(core,'Original');b=task(core,'Remaining')
    old=cmd(core,'create_plan',{'date':'2030-01-01','mode':'no_precise_time','blocks':[{'target_id':a['id'],'completion_gate':'Keep this'}]})['entity']
    cmd(core,'record_feedback',{'target_id':a['id'],'business_date':'2030-01-01','dimensions':{'completion':'done'},'source_text':'Explicitly finished'})
    job=request(core);finish(core,job,proposal([b]))
    result=cmd(core,'apply_proposal',{'id':job['id']})['results'][0]['entity']
    assert result['data']['supersedes_id']==old['id']
    assert result['data']['blocks'][0]==old['data']['blocks'][0]
    assert result['data']['blocks'][1]['target_id']==b['id']
    assert core.query('daily_review',date='2030-01-01')['summary']['done']==1


@pytest.mark.parametrize('bad',['wrong_date','foreign_action','invented_id','overlap'])
def test_invalid_candidate_never_leaks_a_plan_or_audit_record(tmp_path,bad):
    core=Core(tmp_path);t=task(core);other=task(core,'Other');job=request(core);value=proposal([t]);payload=value['actions'][0]['payload']
    if bad=='wrong_date':payload['date']='2030-01-02'
    elif bad=='foreign_action':value['actions'][0]['command']='update'
    elif bad=='invented_id':payload['blocks'][0]['target_id']='invented'
    else:payload.update(mode='standard',blocks=[{'target_id':t['id'],'start':'10:00','end':'11:00'},{'target_id':other['id'],'start':'10:30','end':'11:30'}])
    with core.store.connect() as c:
        before=c.execute('SELECT count(*) FROM changes').fetchone()[0]
        with pytest.raises(BusinessError):normalize(core,c,job,value)
        assert c.execute('SELECT count(*) FROM changes').fetchone()[0]==before
    assert core.query('list',type='plan')['total']==0


def test_other_client_edit_requires_reread_before_adoption(tmp_path):
    core=Core(tmp_path);t=task(core);job=request(core);finish(core,job,proposal([t]))
    cmd(core,'update',{'id':t['id'],'version':t['version'],'patch':{'title':'Changed target'}})
    with pytest.raises(BusinessError) as error:cmd(core,'apply_proposal',{'id':job['id']})
    assert error.value.code=='context_changed'
    assert core.query('list',type='plan')['total']==0


@pytest.mark.parametrize('text,scope,explicit,expected',[
    ('生成今天的计划',{'kind':'general'},False,'2030-01-01'),
    ('帮我安排明天',{'kind':'general'},False,'2030-01-02'),
    ('今天干什么合适',{'kind':'general'},False,'2030-01-01'),
    ('请为 2030-01-07 生成每日计划',{'kind':'general'},False,'2030-01-07'),
    ('生成计划',{'kind':'daily_plan','date':'2030-01-03'},False,'2030-01-03'),
    ('先不要生成计划，只讨论',{'kind':'daily_plan','date':'2030-01-03'},True,None),
    ('为什么生成计划需要这些信息',{'kind':'daily_plan','date':'2030-01-03'},False,None),
    ('生成这个课程的学习计划',{'kind':'course','entity_id':'c'},False,None),
])
def test_direct_requests_and_explicit_discussion_are_distinct(text,scope,explicit,expected):
    from management.plan_assistance import requested_day
    assert requested_day(text,scope,'2030-01-01',explicit)==expected


def test_empty_model_text_is_not_misreported_as_a_generated_plan(tmp_path):
    core=Core(tmp_path);task(core);job=request(core)
    with core.store.connect() as c,pytest.raises(BusinessError) as error:
        normalize(core,c,job,{'summary':'Please answer a long questionnaire','unknowns':[],'actions':[]})
    assert error.value.code=='plan_missing_candidate'
    assert core.query('list',type='plan')['total']==0


def test_preserved_timed_history_does_not_require_unknown_future_dates(tmp_path):
    core=Core(tmp_path);a=task(core,'Reported work');b=task(core,'Next action')
    old=cmd(core,'create_plan',{'date':'2030-01-01','mode':'standard','blocks':[{'target_id':a['id'],'start':'10:00','end':'11:00'}]})['entity']
    cmd(core,'record_feedback',{'target_id':a['id'],'business_date':'2030-01-01','dimensions':{'completion':'done'},'source_text':'Actual result'})
    cmd(core,'create',{'type':'event','title':'Undated later assessment','data':{'hard':True}})
    job=request(core);finish(core,job,proposal([b]))
    actual=cmd(core,'apply_proposal',{'id':job['id']})['results'][0]['entity']
    assert actual['data']['blocks'][0]==old['data']['blocks'][0]
    assert 'start' not in actual['data']['blocks'][1]
