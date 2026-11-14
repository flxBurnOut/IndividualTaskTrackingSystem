import json
import threading
import uuid
import pytest
from management.core import Core
from management.storage import encode,now
from management.schemas import BusinessError
from management import context_service as contexts


def command(core,name,p):
    state=core.query('state')
    return core.command(name,p,request_id=str(uuid.uuid4()),epoch=state['epoch'],expected_revision=state['revision'])['result']


@pytest.fixture
def core(tmp_path):
    return Core(tmp_path/'data')


def open_context(core,scope=None,sources=()):
    return core.query('prepare_context',goal='Synthetic test',scope=scope or {'kind':'general','date':'2026-09-25'},source_ids=list(sources))


def next_page(core,op,name,**p):
    try:return core.query('query_context',operation_id=op,collection=name,**p)
    except BusinessError as error:
        if error.code!='context_checkpoint_required':raise
        with core.store.connect() as c:
            if not contexts._operation(core,c,op)['job_id']:raise
        # Test-only provider acknowledgement; production only does this after
        # an actual contextCompaction item and completed turn.
        contexts.compacted(core,op)
        return core.query('query_context',operation_id=op,collection=name,**p)


@pytest.mark.parametrize('count',[100,1000,10000,100000])
def test_all_records_have_no_hidden_horizon(core,count):
    with core.store.connect() as c:
        c.execute('BEGIN')
        c.executemany("INSERT INTO entities VALUES (?, 'task', ?, NULL, 'pending', 0, '{}',1,?,?)",
            ((f't{i:06d}',f'Synthetic task {i}',now(),now()) for i in range(count)))
        c.commit()
    envelope=open_context(core);op=envelope['operation_id']
    assert contexts.size(envelope)<contexts.ENVELOPE_BYTES
    cursor=None;seen=0
    while True:
        page=next_page(core,op,'tasks',cursor=cursor,limit=200)
        assert contexts.size(page)<=contexts.MAX_PAGE_BYTES
        assert len(page['items'])>0
        seen+=len(page['items'])
        if page['next_cursor'] is None:break
        cursor=page['next_cursor']
    assert seen==count and page['coverage_complete']
    target=core.query('read_context_item',operation_id=op,id=f't{count-1:06d}') if contexts.size(envelope) else None
    assert target['item']['title']==f'Synthetic task {count-1}'
    assert contexts.size(open_context(core))<contexts.ENVELOPE_BYTES


def test_cursor_replay_after_lost_receipt_does_not_advance_coverage(core):
    for n in range(8):command(core,'create',{'type':'task','title':str(n)})
    op=open_context(core)['operation_id']
    first=next_page(core,op,'tasks',limit=2)
    replay=next_page(core,op,'tasks',limit=2)
    assert replay==first
    second=next_page(core,op,'tasks',limit=2,cursor=first['next_cursor'])
    replay=next_page(core,op,'tasks',limit=2,cursor=first['next_cursor'])
    assert replay==second
    assert core.query('operation_status',operation_id=op)['collections'][0]['delivered']==4


def test_changed_collection_invalidates_cursor_and_other_collection_survives(core):
    for n in range(3):command(core,'create',{'type':'task','title':str(n)})
    op=open_context(core)['operation_id'];first=next_page(core,op,'tasks',limit=1)
    command(core,'create',{'type':'note','title':'Unrelated'})
    assert next_page(core,op,'tasks',limit=1,cursor=first['next_cursor'])
    changed=next_page(core,op,'tasks',limit=1,cursor=first['next_cursor'])
    command(core,'create',{'type':'task','title':'New deadline','data':{'due_date':'2026-09-25'}})
    with pytest.raises(BusinessError,match='集合'):next_page(core,op,'tasks',cursor=changed['next_cursor'])
    refreshed=next_page(core,op,'tasks')
    assert refreshed['total']==4


def test_utf8_nested_long_record_roundtrips(core):
    raw='中文表格\t😀\n'*3000
    item=command(core,'create',{'type':'note','title':'Long','data':{'content':raw}})['entity']
    op=open_context(core)['operation_id'];offset=0;version=None;pieces=[]
    while True:
        page=core.query('read_context_item',operation_id=op,id=item['id'],offset=offset,version=version)
        assert contexts.size(page)<=contexts.MAX_PAGE_BYTES
        pieces.append(page['json_fragment']);version=page['version'];offset=page['next_offset']
        if offset is None:break
    assert json.loads(''.join(pieces))['data']['content']==raw


def test_ordinary_reads_pass_cumulative_bytes_and_calls_without_compaction(core):
    item=command(core,'create',{'type':'note','title':'Evidence','data':{'content':'中文 evidence '*100}})['entity']
    op=open_context(core)['operation_id'];before=core.query('state')['revision']
    for _ in range(135):
        page=core.query('read_context_item',operation_id=op,id=item['id'])
        assert page['complete'] and contexts.size(page)<=contexts.MAX_PAGE_BYTES
    status=core.query('operation_status',operation_id=op)
    assert status['mode']=='ordinary_mcp' and status['budget_policy']=='advisory'
    assert not status['checkpoint_required'] and status['next_action']=='continue_reading'
    assert status['delivered_bytes']>contexts.STAGE_BYTES and status['tool_calls']>128
    assert status['stage']==0 and status['phase']=='reading' and status['notice']
    assert core.query('state')['revision']==before


@pytest.mark.parametrize('phase',['checkpointed','budget_stop'])
def test_legacy_ordinary_operation_restores_cursor_and_evidence_in_place(core,phase):
    with core.store.connect() as c:
        c.executemany("INSERT INTO entities VALUES (?, 'task', ?, NULL, 'pending', 0, '{}',1,?,?)",
            ((f't{i}',f'Synthetic {i}',now(),now()) for i in range(3)))
    op=open_context(core)['operation_id']
    first=core.query('query_context',operation_id=op,collection='tasks',limit=1)
    core.query('read_context_item',operation_id=op,id='t0')
    core.query('checkpoint_context',operation_id=op,summary='First item checked; continue original cursor.')
    before=core.query('state')
    with core.store.connect() as c:
        c.execute("UPDATE context_operations SET phase=?,delivered_bytes=100000,tool_calls=150 WHERE id=?",(phase,op))
        checkpoint=c.execute('SELECT checkpoint FROM context_operations WHERE id=?',(op,)).fetchone()[0]
        evidence=[tuple(r) for r in c.execute('SELECT * FROM context_reads WHERE operation_id=?',(op,))]
    core=Core(core.root)
    status=core.query('operation_status',operation_id=op)
    assert status['phase']=='reading' and status['stage']==0 and status['operation_id']==op
    assert status['next_action']=='continue_reading' and not status['checkpoint_required']
    assert status['delivered_bytes']>=100000 and status['tool_calls']>=150
    assert status['collections'][0]['next_cursor']==first['next_cursor']
    second=core.query('query_context',operation_id=op,collection='tasks',cursor=first['next_cursor'],limit=1)
    assert second['items'][0]['id']=='t1'
    with core.store.connect() as c:
        assert c.execute('SELECT checkpoint FROM context_operations WHERE id=?',(op,)).fetchone()[0]==checkpoint
        assert [tuple(r) for r in c.execute('SELECT * FROM context_reads WHERE operation_id=? AND entity_id=?',(op,'t0'))]==evidence
    assert core.query('state')['revision']==before['revision']


def test_ordinary_checkpoint_yield_does_not_wait_for_nonexistent_worker(core):
    op=open_context(core)['operation_id']
    saved=core.query('checkpoint_context',operation_id=op,summary='Saved progress',**{'yield':True})
    assert saved['saved'] and not saved['end_turn_for_compaction']
    assert saved['mode']=='ordinary_mcp' and saved['next_action']=='continue_reading'
    assert core.query('query_context',operation_id=op,collection='tasks')['coverage_complete']


def test_legacy_ordinary_epoch_mismatch_does_not_resume(core):
    op=open_context(core)['operation_id']
    with core.store.connect() as c:
        c.execute("UPDATE context_operations SET phase='checkpointed',delivered_bytes=100000 WHERE id=?",(op,))
        core.store.set_meta(c,'epoch',str(uuid.uuid4()))
    with pytest.raises(BusinessError) as error:core.query('operation_status',operation_id=op)
    assert error.value.code=='epoch_mismatch'
    with core.store.connect() as c:
        row=c.execute('SELECT phase,delivered_bytes FROM context_operations WHERE id=?',(op,)).fetchone()
        assert tuple(row)==('checkpointed',100000)


def test_compaction_reset_is_not_available_for_ordinary_reads(core):
    op=open_context(core)['operation_id']
    before=core.query('operation_status',operation_id=op)
    with pytest.raises(BusinessError) as error:contexts.compacted(core,op)
    assert error.value.code=='context_compaction_mode'
    with core.store.connect() as c:
        current=contexts._operation(core,c,op)
        assert current['stage']==before['stage']==0
        assert current['delivered_bytes']>=before['delivered_bytes']


def test_epoch_change_rejects_old_references(core):
    op=open_context(core)['operation_id']
    with core.store.connect() as c:core.store.set_meta(c,'epoch',str(uuid.uuid4()))
    with pytest.raises(BusinessError) as e:next_page(core,op,'tasks')
    assert e.value.code=='epoch_mismatch'


def test_no_permanent_500_course_limit(core):
    course=command(core,'create',{'type':'course','title':'Synthetic'})['entity']
    with core.store.connect() as c:
        c.execute('BEGIN')
        c.executemany("INSERT INTO entities VALUES (?, 'task', ?, ?, 'done', 0, '{}',1,?,?)",
            ((f'past{i:05d}',f'Past {i}',course['id'],now(),now()) for i in range(1001)))
        c.commit()
    command(core,'settings',{'settings':{'ai':{'enabled':True}}})
    sent=command(core,'send_message',{'scope':{'kind':'course','entity_id':course['id']},'text':'解释这个课程','source_ids':[]})
    assert sent['job']['input']['context']['protocol']=='context/1'
    assert contexts.size(sent['job']['input']['context'])<contexts.ENVELOPE_BYTES


def test_last_page_urgent_and_new_member_must_be_checked(core):
    task=command(core,'create',{'type':'task','title':'Last page due','data':{'due_date':'2026-09-25'}})['entity']
    op=open_context(core)['operation_id']
    candidate={'summary':'plan','unknowns':[],'sources':[],'actions':[{'command':'create_plan','payload':{'date':'2026-09-25','mode':'no_precise_time','blocks':[{'target_id':task['id']}]},'reason':'requested'}]}
    with core.store.connect() as c:
        with pytest.raises(BusinessError) as e:contexts.validate(core,c,contexts._operation(core,c,op),candidate)
        assert e.value.code=='context_coverage'
    next_page(core,op,'deadlines')
    with core.store.connect() as c:contexts.validate(core,c,contexts._operation(core,c,op),candidate)
    command(core,'create',{'type':'note','title':'Unrelated change'})
    with core.store.connect() as c:contexts.validate(core,c,contexts._operation(core,c,op),candidate)
    command(core,'create',{'type':'task','title':'New critical','data':{'due_date':'2026-09-25'}})
    with core.store.connect() as c:
        with pytest.raises(BusinessError) as e:contexts.validate(core,c,contexts._operation(core,c,op),candidate)
        assert e.value.code=='context_coverage'


# Independent context views can share a bounded, versioned response.
def test_context_batch_details_fragments_queries_and_restart(core):
    body='Unicode 中文😀 and quotes \". '*1600
    with core.store.connect() as c:
        for i in range(5):
            c.execute("INSERT INTO entities VALUES (?, 'note', ?, NULL, 'pending', 0, ?,1,?,?)",
                (f'batch-{i}',f'Synthetic note {i}',encode({'body':body if i==0 else str(i)}),now(),now()))
    op=open_context(core)['operation_id']
    requests=[{'collection':'records','filters':{'type':'note'}},*[{'id':f'batch-{i}','version':1} for i in range(5)]]
    start=0;served=[]
    while True:
        result=core.query('read_context_batch',operation_id=op,requests=requests,start=start,byte_limit=6000)
        assert contexts.size(result)<=6000 and result['items']
        replay=Core(core.root).query('read_context_batch',operation_id=op,requests=requests,start=start,byte_limit=6000)
        assert replay['items']==result['items']
        served.extend(result['items']);start=result['next_request_index']
        if start is None:break
    assert [x['index'] for x in served]==list(range(len(requests)))
    long=next(x['result'] for x in served if x['index']==1)
    assert long['next_offset'] is not None and not long['complete']
    fragments=[long['json_fragment']]
    while long['next_offset'] is not None:
        long=core.query('read_context_batch',operation_id=op,requests=[{'id':'batch-0','offset':long['next_offset'],'version':long['version']}])['items'][0]['result']
        fragments.append(long['json_fragment'])
    assert json.loads(''.join(fragments))['data']['body']==body


def test_context_batch_late_error_rolls_back_read_coverage(core):
    item=command(core,'create',{'type':'task','title':'Synthetic task'})['entity']
    op=open_context(core)['operation_id']
    with pytest.raises(Exception):core.query('read_context_batch',operation_id=op,requests=[{'id':item['id']},{'id':'missing-record'}])
    with core.store.connect() as c:
        assert c.execute('SELECT count(*) FROM context_reads WHERE operation_id=?',(op,)).fetchone()[0]==0
        assert c.execute('SELECT count(*) FROM context_parts WHERE operation_id=?',(op,)).fetchone()[0]==0


def test_context_batch_version_epoch_and_arbitrary_command_guards(core):
    item=command(core,'create',{'type':'task','title':'Synthetic version'})['entity']
    op=open_context(core)['operation_id']
    with pytest.raises(Exception) as error:core.query('read_context_batch',operation_id=op,requests=[{'id':item['id'],'version':999}])
    assert error.value.code=='context_changed'
    for request in [{'command':'create'},{'id':item['id'],'command':'create'},{'id':item['id'],'offset':2}]:
        with pytest.raises(Exception) as error:core.query('read_context_batch',operation_id=op,requests=[request])
        assert error.value.code=='context_batch_parameters'
    with core.store.connect() as c:c.execute("UPDATE context_operations SET epoch='old-space' WHERE id=?",(op,))
    with pytest.raises(Exception) as error:core.query('read_context_batch',operation_id=op,requests=[{'id':item['id']}])
    assert error.value.code=='epoch_mismatch'



def test_detail_tail_is_not_whole_record_coverage(core):
    item=command(core,'create',{'type':'note','title':'Do not skip middle','data':{'content':'Evidence 中文 '*5000}})['entity']
    op=open_context(core)['operation_id'];raw=encode(item)
    first=core.query('read_context_item',operation_id=op,id=item['id'],byte_limit=4096)
    tail=core.query('read_context_item',operation_id=op,id=item['id'],offset=len(raw)-100,version=first['version'])
    assert tail['complete'] and not tail['coverage_complete']
    with core.store.connect() as c:
        assert c.execute('SELECT detail FROM context_reads WHERE operation_id=? AND entity_id=?',(op,item['id'])).fetchone()[0]==1
    page=first
    while page['next_offset'] is not None:
        page=core.query('read_context_item',operation_id=op,id=item['id'],offset=page['next_offset'],version=first['version'])
    assert page['coverage_complete']
    with core.store.connect() as c:
        assert c.execute('SELECT detail FROM context_reads WHERE operation_id=? AND entity_id=?',(op,item['id'])).fetchone()[0]==2
    for params in [{'offset':len(raw)+1,'version':1},{'offset':2},{'offset':-1,'version':1}]:
        with pytest.raises(BusinessError):core.query('read_context_item',operation_id=op,id=item['id'],**params)



def test_context_batch_never_returns_empty_nonterminal_due_to_tiny_budget(core):
    op=open_context(core)['operation_id']
    with pytest.raises(BusinessError) as error:
        core.query('read_context_batch',operation_id=op,requests=[{'collection':'records'}],byte_limit=2048)
    assert error.value.code=='context_batch_parameters'
    result=core.query('read_context_batch',operation_id=op,requests=[{'collection':'records'}],byte_limit=4096)
    assert len(result['items'])==1 and result['requests_complete']
