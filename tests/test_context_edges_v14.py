
"""Adversarial coverage for scope, evidence, quotas and original-operation recovery."""
import json,threading,uuid
import pytest
from management.core import Core
from management import context_service as cs,materials
from management.storage import encode,now
from management.schemas import BusinessError
from test_context_service_v14 import command,open_context,next_page
from test_context_materials_v14 import source,next_step,acknowledge
from test_context_candidates_v14 import running
from context_harness import rows,read,item


def proposal(actions=()):
    return {'summary':'Synthetic candidate','actions':list(actions),'sources':[],'unknowns':[]}


def test_normal_feedback_uses_large_course_as_index_not_full_material_job(tmp_path):
    core=Core(tmp_path/'data')
    course=command(core,'create',{'type':'course','title':'Course'})['entity']
    task=command(core,'create',{'type':'task','title':'Tutorial 5','parent_id':course['id']})['entity']
    with core.store.connect() as c:
        c.executemany("INSERT INTO entities VALUES (?,'asset',?,?,'pending',0,'{}',1,?,?)",
            ((f'a{i:05d}',f'Course file {i}',course['id'],now(),now()) for i in range(2000)))
    command(core,'settings',{'settings':{'ai':{'enabled':True}}})
    sent=command(core,'send_message',{'scope':{'kind':'course','entity_id':course['id']},'text':'Tutorial 5 已完成'})
    value=sent['job']['input'];op=value['context_operation_id']
    assert value['source_ids']==[] and not value['analyze_materials']
    assert cs.size(value['context'])<cs.ENVELOPE_BYTES
    assert next_page(core,op,'materials')['total']==2000
    with core.store.connect() as c:
        assert c.execute('SELECT count(*) FROM context_sources WHERE operation_id=?',(op,)).fetchone()[0]==0
    action={'command':'record_feedback','payload_json':encode({'target_id':task['id'],'business_date':'2026-09-26','dimensions':{'completion':'done'},'source_text':'Tutorial 5 已完成'}),'reason':'Explicit'}
    assert core.query('validate_candidate',operation_id=op,proposal=proposal([action]))['valid']


def test_more_than_32_lazy_material_bindings_are_all_processed(tmp_path):
    core=Core(tmp_path/'data');path=tmp_path/'shared.txt';path.write_text('Shared original')
    original=source(core,path)
    with core.store.connect() as c:
        c.executemany("INSERT INTO entities VALUES (?,'asset',?,NULL,'pending',0,?,1,?,?)",
            ((f's{i:02d}',f'Copy {i}',encode(original['data']),now(),now()) for i in range(41)))
    op=open_context(core,sources=[f's{i:02d}' for i in range(41)])['operation_id'];seen=[]
    while True:
        step=next_step(core,op)
        if step.get('all_steps_processed'):break
        if not step.get('step_key'):continue
        seen.append(step['source_id']);acknowledge(core,op,step)
    assert len(seen)==len(set(seen))==41
    with core.store.connect() as c:
        assert c.execute('SELECT count(*) FROM material_catalog').fetchone()[0]==1


def test_checkpoint_correction_archives_old_draft_and_rejects_stale_delivery(tmp_path):
    core=Core(tmp_path/'data');path=tmp_path/'original.txt';path.write_text('Exam is Friday')
    asset=source(core,path);op=open_context(core,sources=[asset['id']])['operation_id']
    step=next_step(core,op)
    acknowledge(core,op,step,{'facts':[{'key':'exam.day','value':'Thursday','evidence':'paragraph 1'}]})
    core.query('checkpoint_context',operation_id=op,result={'actions':[{'command':'create','payload_json':encode({'type':'task','title':'Old draft'}),'reason':'Draft'}]})
    reread=core.query('read_material',operation_id=op,source_id=asset['id'],chunk=0)
    result={'facts':[{'key':'exam.day','value':'Friday','evidence':'paragraph 1'}]}
    corrected=core.query('checkpoint_context',operation_id=op,step_key=step['step_key'],
        delivery_token=reread['delivery_token'],expected_fingerprint=reread['checkpoint_fingerprint'],result=result)
    assert corrected['remerge_required']
    with core.store.connect() as c:
        assert c.execute('SELECT count(*) FROM context_actions WHERE operation_id=?',(op,)).fetchone()[0]==0
        assert c.execute('SELECT count(*) FROM context_step_history WHERE operation_id=?',(op,)).fetchone()[0]==2
    with core.store.connect() as c:c.execute('UPDATE entities SET version=version+1 WHERE id=?',(asset['id'],))
    with pytest.raises(BusinessError) as err:
        core.query('checkpoint_context',operation_id=op,step_key=step['step_key'],delivery_token=step['delivery_token'],result={'notes':'Late new result'})
    assert err.value.code=='context_changed'


def test_source_refresh_updates_job_version_cache_and_keeps_unchanged_work(tmp_path):
    core=Core(tmp_path/'data');path=tmp_path/'source.txt';path.write_text('Original text')
    asset=source(core,path);command(core,'settings',{'settings':{'ai':{'enabled':True}}})
    sent=command(core,'send_message',{'scope':{'kind':'general'},'text':'Read materials','source_ids':[asset['id']]})
    job=sent['job'];op=job['input']['context_operation_id'];step=next_step(core,op);acknowledge(core,op,step)
    with core.store.connect() as c:c.execute('UPDATE entities SET title=?,version=version+1 WHERE id=?',('New title',asset['id']))
    updated={'version':asset['version']+1}
    result=core.query('refresh_context',operation_id=op)
    assert result['changed_sources']==1
    value=core.query('job',id=job['id'])['job']['input']
    assert value['source_versions'][asset['id']]==updated['version']
    with core.store.connect() as c:
        record=c.execute('SELECT source_version,status FROM context_steps WHERE operation_id=?',(op,)).fetchone()
        assert (record['source_version'],record['status'])==(updated['version'],'completed')
    assert core.query('validate_candidate',operation_id=op,proposal=proposal())['valid']


def test_old_analysis_and_raw_material_use_current_budget(tmp_path):
    core=Core(tmp_path/'data');path=tmp_path/'past.txt';path.write_text('Original evidence')
    asset=source(core,path);command(core,'settings',{'settings':{'ai':{'enabled':True}}})
    sent=command(core,'send_message',{'scope':{'kind':'general'},'text':'Read material','source_ids':[asset['id']]})
    op=sent['job']['input']['context_operation_id'];step=next_step(core,op)
    acknowledge(core,op,step,{'facts':[{'key':'evidence','value':'Original','evidence':'paragraph 1'}]})
    with core.store.connect() as c:
        c.execute("UPDATE context_operations SET delivered_bytes=100000,phase='ready' WHERE id=?",(op,))
    fresh=open_context(core)['operation_id']
    facts=core.query('query_context',operation_id=fresh,collection='facts',filters={'job_id':sent['job']['id']})
    raw=core.query('read_material',operation_id=fresh,source_id=asset['id'],chunk=0,from_job=sent['job']['id'])
    assert facts['historical'] and len(facts['items'])==1
    assert raw['historical'] and raw['content']['text']=='Original evidence'
    with core.store.connect() as c:
        assert c.execute('SELECT delivered_bytes FROM context_operations WHERE id=?',(op,)).fetchone()[0]==100000


def test_changed_generation_rejects_previous_worker_checkpoint(running,tmp_path):
    core,job,_=running;op=json.loads(job['input'])['context_operation_id']
    path=tmp_path/'generation.txt';path.write_text('Data')
    asset=source(core,path)
    with core.store.connect() as c:c.execute('INSERT INTO context_sources(operation_id,entity_id,version) VALUES (?,?,?)',(op,asset['id'],asset['version']))
    step=next_step(core,op)
    with core.store.connect() as c:c.execute('UPDATE jobs SET generation=generation+1 WHERE id=?',(job['id'],))
    with pytest.raises(BusinessError) as err:acknowledge(core,op,step)
    assert err.value.code=='context_changed'


def test_input_checkpoints_consume_stage_budget_but_remain_durable(tmp_path):
    core=Core(tmp_path/'data');op=open_context(core)['operation_id']
    for i in range(8):
        core.query('checkpoint_context',operation_id=op,result={'notes':'x'*8500})
    with core.store.connect() as c:
        current=cs._operation(core,c,op)
        assert current['delivered_bytes']>cs.STAGE_BYTES and current['phase']=='budget_stop'
    with pytest.raises(BusinessError) as err:core.query('query_context',operation_id=op,collection='tasks')
    assert err.value.code=='context_checkpoint_required'
    cs.compacted(core,op)
    assert core.query('query_context',operation_id=op,collection='tasks')['coverage_complete']


def test_quota_stops_after_tool_return_and_continues_same_operation():
    from management.discussion_mcp import run
    class RPC:
        thread_id='original-thread';turn_id=None
        def __init__(self):
            self.calls=[];self.events=iter([
                {'method':'item/completed','params':{'threadId':self.thread_id,'turnId':'turn','item':{'type':'mcpToolCall'}}},
                {'method':'turn/completed','params':{'threadId':self.thread_id,'turn':{'id':'turn','status':'interrupted'}}}])
        def request(self,name,p):
            self.calls.append((name,p));return {'turn':{'id':'turn'}}
        def next_event(self):return next(self.events)
    class Progress:
        def candidate(self):return None
        def context_status(self):return {'phase':'budget_stop'}
    rpc=RPC();progress=Progress()
    result=run(rpc,{'prompt':'request','context_operation_id':'same'},'.',[],set(),threading.Event(),lambda *a,**k:None,progress.candidate)
    assert result['context_continuation']
    assert rpc.calls[-1]==('turn/interrupt',{'threadId':'original-thread','turnId':'turn'})


def test_tenthousandth_task_can_be_found_saved_and_receipted(tmp_path):
    core=Core(tmp_path/'data')
    with core.store.connect() as c:
        c.executemany("INSERT INTO entities VALUES (?,'task',?,NULL,'pending',0,'{}',1,?,?)",
            ((f't{i:05d}',f'Exercise {i}',now(),now()) for i in range(10000)))
    command(core,'settings',{'settings':{'ai':{'enabled':True}}})
    sent=command(core,'send_message',{'scope':{'kind':'daily_plan','date':'2030-01-10'},'text':'Arrange Exercise 9999 today'})
    job=sent['job'];op=job['input']['context_operation_id']
    found=rows(core,job,'tasks',search='Exercise 9999')
    assert [r['id'] for r in found]==['t09999']
    target=item(core,job,'t09999')
    for collection in ('deadlines','rules','events','plans'):rows(core,job,collection)
    candidate=proposal([{'command':'create_plan','payload':{'date':'2030-01-10','mode':'no_precise_time','blocks':[{'target_id':target['id'],'target_version':target['version']}]},'reason':'Explicit'}])
    with core.store.connect() as c:
        candidate,_=cs.validate(core,c,cs._operation(core,c,op),candidate)
        from management.session_coordinator import complete_candidate
        complete_candidate(core,c,core._job(c,job['id']),candidate)
    state=core.query('state');request=str(uuid.uuid4())
    receipt=core.command('apply_proposal',{'id':job['id']},request_id=request,epoch=state['epoch'],expected_revision=state['revision'])
    assert receipt['result']['results_total']==1
    plans=core.query('list',type='plan')['items']
    assert plans[0]['data']['blocks'][0]['target_id']=='t09999'
    with core.store.connect() as c:assert c.execute('SELECT 1 FROM receipts WHERE request_id=?',(request,)).fetchone()


def test_active_rule_full_body_and_timezone_change_are_checked(tmp_path):
    core=Core(tmp_path/'data')
    task=command(core,'create',{'type':'task','title':'Task'})['entity']
    rule=command(core,'create',{'type':'rule','title':'Hard capacity','data':{'rule_kind':'capacity','minutes':30,'notes':'long '*4000}})['entity']
    op=open_context(core,scope={'kind':'daily_plan','date':'2030-01-10'})['operation_id']
    next_page(core,op,'rules')
    candidate=proposal([{'command':'create_plan','payload_json':encode({'date':'2030-01-10','blocks':[{'target_id':task['id'],'minutes':60}]}),'reason':'Test'}])
    with pytest.raises(BusinessError) as err:core.query('validate_candidate',operation_id=op,proposal=candidate)
    assert err.value.code=='context_coverage'
    offset=0;version=None
    while True:
        page=read(core,'read_context_item',operation_id=op,id=rule['id'],offset=offset,version=version)
        offset=page.get('next_offset');version=page.get('version')
        if offset is None:break
    with pytest.raises(BusinessError) as err:core.query('validate_candidate',operation_id=op,proposal=candidate)
    assert err.value.code=='capacity'
    command(core,'settings',{'settings':{'timezone':'UTC'}})
    with pytest.raises(BusinessError) as err:core.query('validate_candidate',operation_id=op,proposal=proposal())
    assert err.value.code=='context_changed'


def test_vector_pdf_has_image_evidence_outside_timetable_mode(tmp_path):
    from reportlab.pdfgen import canvas
    core=Core(tmp_path/'data');path=tmp_path/'vector.pdf';pdf=canvas.Canvas(str(path))
    pdf.drawString(40,780,'Enough plain text to avoid scan detection')
    pdf.rect(40,400,300,200);pdf.showPage();pdf.save()
    asset=source(core,path);op=open_context(core,sources=[asset['id']])['operation_id'];kinds=[]
    while True:
        step=next_step(core,op)
        if step.get('all_steps_processed'):break
        kinds.append(step['content']['kind']);acknowledge(core,op,step)
    assert kinds==['text','image']


def test_xlsx_rows_formulas_and_final_cells_continue_across_batches(tmp_path):
    from openpyxl import Workbook
    core=Core(tmp_path/'data');path=tmp_path/'schedule.xlsx'
    book=Workbook();sheet=book.active;sheet.title='课程安排'
    sheet.append(['Week','Task','Formula'])
    for i in range(1,64):sheet.append([i,'FINAL REQUIRED QUIZ' if i==63 else f'Tutorial {i}',f'=A{i+1}+1'])
    book.save(path);book.close()
    asset=source(core,path);op=open_context(core,sources=[asset['id']])['operation_id'];chunks=[]
    while True:
        step=next_step(core,op)
        if step.get('all_steps_processed'):break
        chunks.append(step['content']['text']);acknowledge(core,op,step)
    assert len(chunks)==64 and 'FINAL REQUIRED QUIZ' in chunks[-1]
    assert '=A64+1' in chunks[-1]


def test_office_chart_data_and_layout_gap_resume_independently(tmp_path):
    import zipfile
    from management.material_worker import parse_batch
    path=tmp_path/'chart.docx';work=tmp_path/'worker';work.mkdir()
    with zipfile.ZipFile(path,'w') as z:
        z.writestr('word/document.xml','<document><p><t>Read table and chart.</t></p></document>')
        z.writestr('word/charts/chart1.xml','<chart><v>40</v><v>60</v></chart>')
    cursor={};chunks=[]
    for i in range(10):
        batch=parse_batch(path,path.name,work,cursor=cursor,batch_units=1)
        chunks.extend((x['kind'],x['locator']) for x in batch['chunks'])
        cursor=batch['cursor']
        if batch['finished']:break
    assert [kind for kind,_ in chunks]==['text','text','gap']
    assert len(chunks)==len(set(chunks))


def test_single_medium_candidate_is_fragmented_and_cache_lifecycle_bounded(tmp_path):
    from management.context_driver import prune_caches
    core=Core(tmp_path/'data');op=open_context(core)['operation_id']
    task=command(core,'create',{'type':'task','title':'Index only'})['entity']
    next_page(core,op,'tasks')
    # 12-14 KiB used to fall between the page and large-record thresholds.
    payload={'type':'note','title':'Long note','data':{'content':'z'*12500}}
    with core.store.connect() as c:
        c.execute('INSERT INTO context_actions(operation_id,fingerprint,command,payload,reason) VALUES (?,?,?,?,?)',(op,'test','create',encode(payload),'Explicit'))
    offset=0;parts=[]
    while True:
        page=core.query('candidate_actions',operation_id=op,offset=offset)
        assert cs.size(page)<cs.MAX_PAGE_BYTES
        parts.append(page['json_fragment']);offset=page['next_offset']
        if offset is None:break
    assert json.loads(''.join(parts))['payload']==payload
    with core.store.connect() as c:c.execute("UPDATE context_operations SET updated_at='2000-01-01' WHERE id=?",(op,))
    prune_caches(core)
    with core.store.connect() as c:
        assert c.execute('SELECT count(*) FROM context_reads WHERE operation_id=?',(op,)).fetchone()[0]==0
        assert c.execute('SELECT last_page FROM context_queries WHERE operation_id=?',(op,)).fetchone()[0] is None
        assert c.execute('SELECT count(*) FROM context_actions WHERE operation_id=?',(op,)).fetchone()[0]==1
