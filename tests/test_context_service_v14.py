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
        try:
            page=core.query('read_context_item',operation_id=op,id=item['id'],offset=offset,version=version)
        except BusinessError as e:
            assert e.code=='context_checkpoint_required';contexts.compacted(core,op);continue
        assert contexts.size(page)<=contexts.MAX_PAGE_BYTES
        pieces.append(page['json_fragment']);version=page['version'];offset=page['next_offset']
        if offset is None:break
    assert json.loads(''.join(pieces))['data']['content']==raw


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
